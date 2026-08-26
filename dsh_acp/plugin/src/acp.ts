/**
 * Automation-only Agent Client Protocol server over JSON-RPC stdio.
 *
 * The bridge exposes fresh harness sessions to trusted programmatic clients. It
 * carries prompt text/images, committed assistant text/images, cancellation,
 * and one-shot permission decisions; presentation and human-interaction
 * features stay with the harness's UI modules.
 *
 * @module @myagents/dsh-acp-host/acp
 * @license Portions derived from DeepSeek Harness under the MIT License; see
 * THIRD_PARTY_NOTICES.md.
 */

import type { Context } from '@deepseek-ai/cordis'
import { Buffer } from 'node:buffer'
import { randomUUID } from 'node:crypto'
import { constants as FS_CONSTANTS, realpathSync } from 'node:fs'
import { lstat, open, realpath } from 'node:fs/promises'
import { basename, isAbsolute, relative, resolve, sep } from 'node:path'
import { Readable, Writable } from 'node:stream'
import { TextDecoder } from 'node:util'
import Schema from '@deepseek-ai/schemastery'
import { createUserMessage, errorChain } from '@deepseek-ai/dsh-llm'
import {
  AgentSideConnection,
  ndJsonStream,
  PROTOCOL_VERSION,
  RequestError,
  type Agent as AcpAgent,
  type AuthenticateRequest,
  type CancelNotification,
  type CloseSessionRequest,
  type CloseSessionResponse,
  type InitializeRequest,
  type InitializeResponse,
  type LoadSessionRequest,
  type LoadSessionResponse,
  type NewSessionRequest,
  type NewSessionResponse,
  type PromptRequest,
  type PromptResponse,
  type SessionNotification,
  type StopReason,
  type Stream,
} from '@agentclientprotocol/sdk'
import type { Agent, AgentSetup } from '@deepseek-ai/dsh-agent'
import {
  isAppendSurfaceEvent,
  SessionId,
  type SessionEvent,
  type SessionHeader,
  type TurnEndReason,
} from '@deepseek-ai/dsh-session'
// Type import also declaration-merges the approval waterfall answered below.
import type { ApprovalOutcome } from '@deepseek-ai/dsh-user-approval'
import { AcpContentError, admitAcpPrompt, assistantBlockToAcp, supportsAcpImagePrompts } from './acp-content.ts'
import { turnEndToStopReason } from './acp-codec.ts'

export const name = 'myagents-acp'
/** The bridge creates and owns agents; every other concern is carried by the agent composition. */
export const inject = ['agents']

/**
 * The single continuable-subagent teardown the bridge needs. Declared
 * structurally so this package does not depend on the subagent seam for one
 * shutdown hook; an absent service means nothing continuable was materialized.
 */
interface ContinuableDrain {
  /**
   * Close admission below exact host-owned parents, then dispose only their
   * continuable descendants child-first.
   */
  drainContinuableDescendants(parents: readonly Agent[]): Promise<void>
}

/** Preserve invalid-parameter detail in the SDK wire error message. */
function invalidParams(detail: string): RequestError {
  return RequestError.invalidParams(undefined, detail)
}

/** Preserve failed-turn detail; plain handler errors become a generic wire internal error. */
function internalError(detail: string): RequestError {
  return RequestError.internalError(undefined, detail)
}

/** Plugin config: the provider/model selection used for each ACP-created agent. */
export interface AcpConfig {
  /** Provider route for created agents. */
  provider: string
  /** Model name for created agents. */
  model: string
  /** Runtime-only transport override; production uses stdio. */
  stream?: Stream
  /** Runtime-only composition applied before every created or resumed agent is published. */
  setup: AgentSetup
  /** Standard ACP implementation identity advertised during initialize. */
  agentInfo: NonNullable<InitializeResponse['agentInfo']>
  /** Canonical root of the product-owned, plaintext JSONL session artifacts. */
  sessionsRoot?: string
}

// The runtime-only `stream` seam is intentionally absent from Loader config.
export const Config = Schema.object({
  provider: Schema.string().required(),
  model: Schema.string().required(),
  setup: Schema.any().required(),
  agentInfo: Schema.any().required(),
  sessionsRoot: Schema.string(),
}) as unknown as Schema<AcpConfig>

/** One committed surface event waiting for ordered ACP delivery. */
interface LiveOutputBatch {
  items: number
  bytes: number
  deliver: () => Promise<void>
  containFailure: (error: Error) => void
}

/** Per-session protocol state. */
interface SessionRecord {
  agent: Agent
  /** ACP-visible lifecycle; only ready sessions accept prompts. */
  state: 'loading' | 'ready' | 'closing'
  /** Exact owned-agent disposer; resolves after registry, loop, and session teardown. */
  dispose: () => Promise<void>
  /** Ordered assistant-output delivery; every task contains its own failure. */
  outputTail: Promise<void>
  /** Whether one committed batch is currently being written to the client. */
  outputActive: boolean
  /** Payload-bearing batches retained behind the active client write. */
  outputQueue: LiveOutputBatch[]
  /** Exact update count retained by the active batch and outputQueue. */
  pendingOutputItems: number
  /** Exact serialized wire bytes retained only by outputQueue. */
  pendingOutputBytes: number
  /** A turn whose output was rejected at the bounded live-delivery seam. */
  failedOutputTurn: number | undefined
  /** Live events held behind a durable replay, then released in append order. */
  loadingEvents: SessionEvent[] | undefined
  /** Exact serialized bytes retained by loadingEvents. */
  loadingBytes: number
  /** Contained conversion failure from a live update drained by session/load. */
  loadError: Error | undefined
  /** Bounded presentation metadata for unresolved bridge-owned tool calls. */
  toolCalls: Map<string, ToolCallMetadata>
  /** Permission RPCs still pending in the ACP SDK for this session. */
  pendingPermissions: number
  /** Single-flight close; cleared after failure so the tracked owner can be retried or quiesced. */
  closing: Promise<void> | undefined
  /** In-flight admission/turn/output lifecycle for exact settlement. */
  inflight: {
    resolve: (reason: StopReason) => void
    reject: (error: Error) => void
    /** Set only after rich-content admission succeeds and the message is built. */
    messageId: string | undefined
    /** Whether this prompt has entered the Agent's durable inbox interval. */
    messageQueued: boolean
    turn: number | undefined
    /** The correlated turn's ending, set at turn/end and settled at whole-agent idle. */
    endReason: TurnEndReason | undefined
    /** Admission quiescence gate, including any attachment write already in progress. */
    admissionDone: Promise<void>
    finishAdmission: () => void
    admissionController: AbortController
    cancelRequested: boolean
    settlementStarted: boolean
    /** Conversion failure for committed output owned by this prompt's turn. */
    outputError: Error | undefined
    /** Interval-wide failure outside the correlated turn. */
    agentError: Error | undefined
    /** Correlated failure used only when no durable turn ending lands. */
    turnError: Error | undefined
  } | undefined
}

interface ToolCallMetadata {
  title: string
  kind: 'read' | 'edit' | 'search' | 'execute' | 'fetch' | 'other'
  rawInput: unknown
  /** Whether rawInput contains the complete arguments required for informed consent. */
  complete: boolean
}

/** Optional durable-history seam supplied by the host composition. */
interface SessionHistoryQuery {
  readSession(sessionId: SessionId): Promise<{ session: SessionHeader; events: SessionEvent[] }>
}

/** Public subset of DSH SessionPersistence needed for pre-materialization admission. */
interface SessionPersistenceGuard {
  list(signal?: AbortSignal): Promise<SessionHeader[]>
  locate(meta: SessionHeader): { kind: string; path: string } | undefined
}

interface ArtifactStatIdentity {
  dev: bigint
  ino: bigint
  size: bigint
  mtimeNs: bigint
  ctimeNs: bigint
}

interface DurableArtifactPreflight {
  path: string
  identity: ArtifactStatIdentity
  header: SessionHeader
}

class DurableArtifactNotFoundError extends Error {}
class DurableArtifactCwdError extends Error {}

/** Classify a tool name without exposing model reasoning or tool output. */
function toolKind(name: string): 'read' | 'edit' | 'search' | 'execute' | 'fetch' | 'other' {
  if (name === 'read' || name === 'read_image') return 'read'
  if (name === 'glob' || name === 'grep') return 'search'
  if (name === 'write' || name === 'edit') return 'edit'
  if (name === 'bash') return 'execute'
  if (name === 'fetch' || name.startsWith('web_')) return 'fetch'
  return 'other'
}

const MAX_TOOL_INPUT_CHARS = 16_384
const MAX_TRACKED_TOOL_CALLS = 256
/** Fairness and connection bounds for SDK permission RPCs with no request cancellation. */
const MAX_PENDING_PERMISSIONS_PER_SESSION = 8
const MAX_PENDING_PERMISSIONS_PER_CONNECTION = 16
/** Security bounds for payload-bearing work retained behind one active ACP write. */
const MAX_PENDING_LIVE_UPDATES = 64
const MAX_PENDING_LIVE_BYTES = 49_152
/** Durable history is bounded independently of live delivery and load-time arrivals. */
const MAX_DURABLE_REPLAY_EVENTS = 4_096
const MAX_DURABLE_REPLAY_BYTES = 16_777_216
/** Loading must not turn an unresponsive replay client into an unbounded event sink. */
const MAX_LOADING_LIVE_EVENTS = 256
const MAX_LOADING_LIVE_BYTES = 1_048_576

function serializedBytes(value: object): number {
  return Buffer.byteLength(JSON.stringify(value), 'utf8')
}

function containsPath(parent: string, candidate: string): boolean {
  const relation = relative(parent, candidate)
  return relation === ''
    || (!isAbsolute(relation) && relation !== '..' && !relation.startsWith(`..${sep}`))
}

function isPersistenceGuard(value: unknown): value is SessionPersistenceGuard {
  return typeof value === 'object' && value !== null
    && typeof (value as { list?: unknown }).list === 'function'
    && typeof (value as { locate?: unknown }).locate === 'function'
}

function statIdentity(stat: {
  dev: bigint
  ino: bigint
  size: bigint
  mtimeNs: bigint
  ctimeNs: bigint
}): ArtifactStatIdentity {
  return {
    dev: stat.dev,
    ino: stat.ino,
    size: stat.size,
    mtimeNs: stat.mtimeNs,
    ctimeNs: stat.ctimeNs,
  }
}

function sameStatIdentity(left: ArtifactStatIdentity, right: ArtifactStatIdentity): boolean {
  return left.dev === right.dev
    && left.ino === right.ino
    && left.size === right.size
    && left.mtimeNs === right.mtimeNs
    && left.ctimeNs === right.ctimeNs
}

function headerLineMatches(value: unknown, expected: SessionHeader): boolean {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const line = value as Record<string, unknown>
  if (line.type !== 'session'
    || line.id !== expected.id
    || line.version !== expected.version
    || line.createdAt !== expected.createdAt
    || line.cwd !== expected.cwd
    || line.delegationDepth !== (expected.delegationDepth ?? 0)) return false
  for (const name of ['parentSession', 'seedLength', 'origin', 'agentPreset'] as const) {
    if (line[name] !== expected[name]) return false
  }
  return !Object.hasOwn(line, 'sandboxMode') && !Object.hasOwn(line, 'approvalPolicy')
}

function parseArtifactLine(
  bytes: Buffer,
  lineIndex: number,
  expected: SessionHeader,
): void {
  if (bytes.length > 0 && bytes[bytes.length - 1] === 0x0d) {
    throw new Error('durable replay artifact must use LF-delimited JSON records')
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))
  } catch {
    throw new Error(`durable replay artifact contains invalid JSON or UTF-8 at line ${lineIndex + 1}`)
  }
  if (lineIndex === 0) {
    if (!headerLineMatches(parsed, expected)) {
      throw new Error('durable replay artifact header does not match persistence metadata')
    }
    return
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error(`durable replay artifact event line ${lineIndex + 1} is not an object`)
  }
  const event = parsed as Record<string, unknown>
  const expectedSeq = lineIndex - 1
  if (typeof event.type !== 'string' || event.type.length === 0
    || event.type === 'text-chunks'
    || event.type === 'reasoning-chunks'
    || event.type === 'tool-call-chunks'
    || event.seq !== expectedSeq) {
    throw new Error(`durable replay artifact must contain one contiguous event per line (seq ${expectedSeq})`)
  }
  if (expectedSeq >= MAX_DURABLE_REPLAY_EVENTS) {
    throw new Error(
      `durable replay exceeds ${MAX_DURABLE_REPLAY_EVENTS} events or ${MAX_DURABLE_REPLAY_BYTES} artifact bytes`,
    )
  }
}

async function scanArtifactLines(
  handle: Awaited<ReturnType<typeof open>>,
  size: bigint,
  expected: SessionHeader,
): Promise<void> {
  if (size <= 0n) throw new Error('durable replay artifact is empty')
  const stream = handle.createReadStream({
    autoClose: false,
    start: 0,
    end: Number(size) - 1,
  })
  let fragments: Buffer[] = []
  let fragmentBytes = 0
  let lineIndex = 0
  for await (const rawChunk of stream) {
    const chunk = Buffer.isBuffer(rawChunk) ? rawChunk : Buffer.from(rawChunk as Uint8Array)
    let cursor = 0
    for (;;) {
      const newline = chunk.indexOf(0x0a, cursor)
      if (newline < 0) break
      const tail = chunk.subarray(cursor, newline)
      const line = fragments.length === 0
        ? tail
        : Buffer.concat([...fragments, tail], fragmentBytes + tail.length)
      fragments = []
      fragmentBytes = 0
      parseArtifactLine(line, lineIndex, expected)
      lineIndex += 1
      cursor = newline + 1
    }
    if (cursor < chunk.length) {
      const fragment = chunk.subarray(cursor)
      fragments.push(fragment)
      fragmentBytes += fragment.length
    }
  }
  if (fragmentBytes !== 0) {
    throw new Error('durable replay artifact must end with a newline')
  }
  if (lineIndex === 0) throw new Error('durable replay artifact has no header line')
}

async function assertArtifactIdentity(
  preflight: DurableArtifactPreflight,
  sessionsRoot: string,
): Promise<void> {
  const current = await lstat(preflight.path, { bigint: true })
  if (current.isSymbolicLink() || !current.isFile()) {
    throw new Error('durable replay artifact is no longer a regular non-symlink file')
  }
  const canonical = await realpath(preflight.path)
  if (canonical !== preflight.path || !containsPath(sessionsRoot, canonical)) {
    throw new Error('durable replay artifact escaped the canonical sessions root')
  }
  if (!sameStatIdentity(preflight.identity, statIdentity(current))) {
    throw new Error('durable replay artifact changed after bounded preflight')
  }
}

async function preflightDurableArtifact(
  persistence: SessionPersistenceGuard,
  sessionsRoot: string,
  sessionId: SessionId,
  cwd: string,
): Promise<DurableArtifactPreflight> {
  const headers = (await persistence.list()).filter(header => header.id === sessionId)
  if (headers.length === 0) throw new DurableArtifactNotFoundError(`unknown session: ${sessionId}`)
  if (headers.length !== 1) throw new Error(`duplicate durable session identity: ${sessionId}`)
  const header = headers[0] as SessionHeader
  if (header.cwd !== cwd) throw new DurableArtifactCwdError(`cwd does not match persisted session: ${cwd}`)
  const location = persistence.locate(header)
  if (location?.kind !== 'jsonl' || !isAbsolute(location.path)
    || basename(location.path) !== 'session.jsonl') {
    throw new Error('durable replay requires a public plaintext JSONL artifact location')
  }
  const rootStat = await lstat(sessionsRoot)
  if (rootStat.isSymbolicLink() || !rootStat.isDirectory() || await realpath(sessionsRoot) !== sessionsRoot) {
    throw new Error('durable replay sessions root must be a canonical non-symlink directory')
  }
  const located = resolve(location.path)
  if (located !== location.path || !containsPath(sessionsRoot, located)) {
    throw new Error('durable replay artifact location escaped the canonical sessions root')
  }
  const beforePath = await lstat(located, { bigint: true })
  if (beforePath.isSymbolicLink() || !beforePath.isFile() || await realpath(located) !== located) {
    throw new Error('durable replay artifact must be a canonical regular non-symlink file')
  }
  const before = statIdentity(beforePath)
  if (before.size > BigInt(MAX_DURABLE_REPLAY_BYTES)) {
    throw new Error(
      `durable replay exceeds ${MAX_DURABLE_REPLAY_EVENTS} events or ${MAX_DURABLE_REPLAY_BYTES} artifact bytes`,
    )
  }

  const noFollow = FS_CONSTANTS.O_NOFOLLOW
  if (typeof noFollow !== 'number') throw new Error('durable replay requires O_NOFOLLOW support')
  const handle = await open(located, FS_CONSTANTS.O_RDONLY | noFollow)
  try {
    const opened = await handle.stat({ bigint: true })
    if (!opened.isFile() || !sameStatIdentity(before, statIdentity(opened))) {
      throw new Error('durable replay artifact changed before bounded scan')
    }
    await scanArtifactLines(handle, before.size, header)
    const afterRead = await handle.stat({ bigint: true })
    if (!sameStatIdentity(before, statIdentity(afterRead))) {
      throw new Error('durable replay artifact changed during bounded scan')
    }
  } finally {
    await handle.close()
  }
  const preflight = { path: located, identity: before, header }
  await assertArtifactIdentity(preflight, sessionsRoot)
  return preflight
}

/** Reject a durable seed before any of its content can be replayed to the ACP client. */
function assertDurableReplayWithinLimits(events: readonly SessionEvent[], stage: string): void {
  if (events.length > MAX_DURABLE_REPLAY_EVENTS) {
    throw new Error(
      `durable replay exceeds ${MAX_DURABLE_REPLAY_EVENTS} events or ${MAX_DURABLE_REPLAY_BYTES} serialized bytes (${stage})`,
    )
  }
  // Exact UTF-8 size of JSON.stringify(events): brackets plus commas plus each
  // independently serialized event. Stop as soon as the hard cap is crossed.
  let bytes = 2
  for (const [index, event] of events.entries()) {
    if (index > 0) bytes += 1
    bytes += serializedBytes(event)
    if (bytes > MAX_DURABLE_REPLAY_BYTES) {
      throw new Error(
        `durable replay exceeds ${MAX_DURABLE_REPLAY_EVENTS} events or ${MAX_DURABLE_REPLAY_BYTES} serialized bytes (${stage})`,
      )
    }
  }
}

/** Preserve bounded valid JSON arguments as structured ACP input. */
function toolInput(raw: string): Pick<ToolCallMetadata, 'rawInput' | 'complete'> {
  if (raw.length > MAX_TOOL_INPUT_CHARS) {
    return {
      rawInput: { truncated: true, preview: raw.slice(0, MAX_TOOL_INPUT_CHARS) },
      complete: false,
    }
  }
  try {
    return { rawInput: JSON.parse(raw) as unknown, complete: true }
  } catch {
    return { rawInput: raw, complete: true }
  }
}

function toolMetadata(name: string, rawInput: string): ToolCallMetadata {
  return { title: name, kind: toolKind(name), ...toolInput(rawInput) }
}

function toolPresentation(metadata: ToolCallMetadata): Omit<ToolCallMetadata, 'complete'> {
  return { title: metadata.title, kind: metadata.kind, rawInput: metadata.rawInput }
}

/** Reject malformed Loader materialization before it reaches the ACP wire. */
function validateAgentInfo(value: unknown): void {
  if (typeof value !== 'object' || value === null) {
    throw new TypeError('acp: agentInfo must be a standard ACP implementation identity')
  }
  const info = value as Record<string, unknown>
  if (typeof info.name !== 'string' || info.name.length === 0
    || typeof info.version !== 'string' || info.version.length === 0
    || (info.title !== undefined && info.title !== null && typeof info.title !== 'string')
    || (info._meta !== undefined && info._meta !== null
      && (typeof info._meta !== 'object' || Array.isArray(info._meta)))) {
    throw new TypeError('acp: agentInfo must be a standard ACP implementation identity')
  }
}

/** Retain only the newest unambiguous unresolved calls; evicted calls become impossible to authorize. */
function rememberToolCall(
  toolCalls: Map<string, ToolCallMetadata>,
  callId: string,
  metadata: ToolCallMetadata,
): void {
  const existing = toolCalls.get(callId)
  if (existing !== undefined) {
    // Until a result settles the first call, the id cannot identify which
    // tool/arguments an approval belongs to. Preserve its FIFO position and
    // first presentation solely as diagnostic context, but make it
    // permanently non-authorizable for this unresolved interval.
    if (existing.complete) toolCalls.set(callId, { ...existing, complete: false })
    return
  }
  toolCalls.set(callId, metadata)
  while (toolCalls.size > MAX_TRACKED_TOOL_CALLS) {
    const oldest = toolCalls.keys().next()
    if (oldest.done) return
    toolCalls.delete(oldest.value)
  }
}

/** Project one durable tool lifecycle event into its safe ACP activity shape. */
function toolActivity(event: SessionEvent): SessionNotification['update'] | undefined {
  if (event.type === 'tool/call') {
    const metadata = toolMetadata(event.data.name, event.data.arguments)
    return {
      sessionUpdate: 'tool_call',
      toolCallId: event.data.callId,
      ...toolPresentation(metadata),
      status: 'in_progress',
    }
  }
  if (event.type === 'tool/result') {
    if (!isAppendSurfaceEvent(event)) return undefined
    return {
      sessionUpdate: 'tool_call_update',
      toolCallId: event.data.message.content[0].toolCallId,
      status: event.data.message.content[0].isError === true ? 'failed' : 'completed',
    }
  }
  return undefined
}

/** Keep only events that can affect the public live projection or permission binding. */
function affectsLiveAcpProjection(event: SessionEvent): boolean {
  if (event.type === 'tool/call') return true
  if (event.type === 'tool/result') return isAppendSurfaceEvent(event)
  if (event.type !== 'assistant/message' || !isAppendSurfaceEvent(event)) return false
  return event.data.message.content.some(block =>
    (block.type === 'text' && block.text.length > 0) || block.type === 'image')
}

/**
 * Mount the automation-only ACP server.
 * @param ctx - Cordis context carrying the agent factory and session events.
 * @param config - Initial provider/model selection and optional test transport.
 */
export function apply(ctx: Context, config: AcpConfig): void {
  if (typeof config.provider !== 'string' || config.provider.length === 0
    || typeof config.model !== 'string' || config.model.length === 0) {
    throw new TypeError('myagents acp: provider and model must be non-empty')
  }
  if (typeof config.setup !== 'function') {
    throw new TypeError('myagents acp: setup must be a function')
  }
  validateAgentInfo(config.agentInfo)
  // ACP handlers execute outside this plugin's injection scope, so capture the
  // injected service during apply rather than reading it lazily in a callback.
  const agents = ctx.agents
  const sessionQuery = ctx.get('sessionQuery') as SessionHistoryQuery | undefined
  const persistenceCandidate = ctx.get('sessionPersistence')
  const sessionPersistence = isPersistenceGuard(persistenceCandidate) ? persistenceCandidate : undefined
  const sessionsRoot = config.sessionsRoot
  const loadEnabled = sessionQuery !== undefined
    && sessionPersistence !== undefined
    && sessionsRoot !== undefined
    && isAbsolute(sessionsRoot)
    && resolve(sessionsRoot) === sessionsRoot
  const hostWorkspace = realpathSync.native(process.cwd())
  if (hostWorkspace !== process.cwd()) {
    throw new TypeError(`myagents acp: process cwd must be canonical: ${hostWorkspace}`)
  }
  const logger = ctx.logger
  const sessions = new Map<SessionId, SessionRecord>()
  let closed = false
  let pendingPermissions = 0
  let conn: AgentSideConnection
  let imagePromptEnabled = false

  /** Return the bridge-owned record for an agent, rejecting same-id impostors. */
  const ownedRecord = (agent: Agent): SessionRecord | undefined => {
    const record = sessions.get(agent.session.id)
    return record?.agent === agent ? record : undefined
  }

  const assertOpen = (): void => {
    if (closed) throw internalError('the ACP bridge has been disposed')
  }

  const requireSession = (sessionId: SessionId): SessionRecord => {
    const record = sessions.get(sessionId)
    if (record === undefined) throw invalidParams(`unknown session: ${sessionId}`)
    return record
  }

  /** Forget a failed teardown record only after its exact registry owner has retired. */
  const forgetRetiredRecord = (record: SessionRecord): void => {
    const sessionId = record.agent.session.id
    if (agents.get(sessionId) === record.agent) return
    if (sessions.get(sessionId) === record) sessions.delete(sessionId)
  }

  /** Quiesce one session forest child-first while retaining failed owners for retry/bridge teardown. */
  const closeRecord = (record: SessionRecord, detail: string, bestEffortDescendants = false): Promise<void> => {
    record.state = 'closing'
    record.loadingEvents = undefined
    record.loadingBytes = 0
    record.toolCalls.clear()
    if (record.closing !== undefined) return record.closing
    const closing = (async () => {
      const inflight = record.inflight
      if (inflight !== undefined) {
        inflight.cancelRequested = true
        inflight.admissionController.abort(new Error(detail))
        settleAfterQuiescence(record, inflight)
      }
      record.agent.cancel({ kind: 'user' })
      await inflight?.admissionDone
      await record.agent.whenIdle()
      await record.outputTail
      const subagents = ctx.get('subagents') as ContinuableDrain | undefined
      if (subagents !== undefined) {
        try {
          await subagents.drainContinuableDescendants([record.agent])
        } catch (error: unknown) {
          if (!bestEffortDescendants) throw error
          logger.warn(`acp: continuable subagent teardown failed: ${String(error)}`)
        }
      }
      await record.dispose()
    })()
    record.closing = closing
    void closing.catch(() => {
      if (record.closing === closing) record.closing = undefined
    })
    return closing
  }

  /** Send one ordered protocol update while containing transport-only failure. */
  const notify = async (notification: SessionNotification): Promise<void> => {
    try {
      await conn.sessionUpdate(notification)
    /* v8 ignore start -- the ACP SDK contains notification-handler failures; only a transport write failure reaches this guard. */
    } catch (error: unknown) {
      logger.warn(`acp: session/update failed: ${String(error)}`)
    }
    /* v8 ignore stop */
  }

  /** Reserve one committed batch while bounding only the backlog behind the active write. */
  const queueLiveOutput = (
    record: SessionRecord,
    turn: number,
    items: number,
    bytes: number,
    deliver: () => Promise<void>,
  ): boolean => {
    if (items === 0) return true
    if (record.failedOutputTurn === turn) return false
    if (items > MAX_PENDING_LIVE_UPDATES
      || record.pendingOutputItems + items > MAX_PENDING_LIVE_UPDATES
      || (record.outputActive && record.pendingOutputBytes + bytes > MAX_PENDING_LIVE_BYTES)) {
      const failure = new Error(
        `live update backlog exceeded ${MAX_PENDING_LIVE_UPDATES} items or ${MAX_PENDING_LIVE_BYTES} bytes`,
      )
      record.failedOutputTurn = turn
      record.toolCalls.clear()
      if (record.state === 'loading') {
        record.loadingEvents = undefined
        record.loadingBytes = 0
      }
      const inflight = record.inflight?.turn === turn ? record.inflight : undefined
      if (inflight !== undefined) inflight.outputError ??= failure
      record.agent.cancel({ kind: 'user' })
      logger.warn(`acp: ${failure.message}`)
      return false
    }

    const inflight = record.inflight?.turn === turn ? record.inflight : undefined
    const batch: LiveOutputBatch = {
      items,
      bytes,
      deliver,
      containFailure: (failure) => {
        if (inflight !== undefined) inflight.outputError ??= failure
        if (record.state === 'loading') record.loadError ??= failure
        logger.warn(`acp: assistant output conversion failed: ${errorChain(failure)}`)
      },
    }
    record.pendingOutputItems += items
    if (record.outputActive) {
      record.pendingOutputBytes += bytes
      record.outputQueue.push(batch)
      return true
    }

    // The active batch is already committed in the Session and is governed by
    // the model/output and attachment contracts. The byte cap applies to
    // additional payload-bearing closures retained behind its client write.
    record.outputActive = true
    record.outputTail = (async () => {
      let current = batch
      for (;;) {
        try {
          await current.deliver()
        } catch (error: unknown) {
          // Content conversion and declared-size verification throw Error values.
          current.containFailure(error as Error)
        }
        record.pendingOutputItems -= current.items
        const next = record.outputQueue.shift()
        if (next === undefined) {
          // Clear this synchronously with the empty-queue observation so a
          // re-entrant append starts a new drain instead of becoming stranded.
          record.outputActive = false
          return
        }
        record.pendingOutputBytes -= next.bytes
        current = next
      }
    })().catch((error: unknown) => {
      /* v8 ignore next -- every batch contains delivery failure; this guards queue bookkeeping defects. */
      logger.warn(`acp: live output drain failed: ${errorChain(error)}`)
    })
    return true
  }

  const rejectFromError = (
    inflight: NonNullable<SessionRecord['inflight']>,
    reason: Extract<TurnEndReason, { kind: 'error' }>,
  ): void => {
    inflight.reject(internalError(`turn failed: ${reason.error.message}`))
  }

  /**
   * Settle one exact prompt only after admission, agent activity, and ordered
   * assistant delivery have all reached quiescence.
   */
  const settleAfterQuiescence = (
    record: SessionRecord,
    inflight: NonNullable<SessionRecord['inflight']>,
  ): void => {
    if (inflight.settlementStarted) return
    inflight.settlementStarted = true
    void (async () => {
      await inflight.admissionDone
      if (inflight.messageQueued) {
        await record.agent.whenIdle()
        // Teardown deliberately stops public event projection before it
        // cancels the Agent. Recover the authoritative correlated turn/end
        // from the durable log after idle so connection disposal cannot turn
        // a real aborted/completed ending into a synthetic cancellation.
        if (inflight.endReason === undefined && inflight.turn !== undefined) {
          const ending = record.agent.session.events.findLast(event =>
            event.type === 'turn/end' && event.data.turn === inflight.turn)
          if (ending?.type === 'turn/end') inflight.endReason = ending.data.reason
        }
        // session/event enqueues synchronously before the agent becomes idle;
        // reading the live tail here includes every committed output task.
        await record.outputTail
      }
      /* v8 ignore next -- this prompt owns the slot until this exact settlement clears it. */
      if (record.inflight !== inflight) return
      // Only a cancellation that wins before durable inbox admission can
      // synthesize `cancelled`: once queued, the real turn/end is authoritative
      // even if cancel races the final output drain.
      if (!inflight.messageQueued) {
        record.inflight = undefined
        if (inflight.cancelRequested) {
          inflight.resolve('cancelled')
        } else {
          inflight.reject(internalError('prompt settled before durable inbox admission'))
        }
        return
      }
      if (inflight.outputError !== undefined) {
        record.inflight = undefined
        inflight.reject(internalError(`assistant output delivery failed: ${inflight.outputError.message}`))
        return
      }
      if (inflight.agentError !== undefined) {
        record.inflight = undefined
        inflight.reject(internalError(`turn failed: ${inflight.agentError.message}`))
        return
      }
      const end = inflight.endReason
      if (end === undefined) {
        record.inflight = undefined
        if (inflight.turnError === undefined) {
          inflight.reject(internalError('queued prompt settled without a turn ending'))
        } else {
          inflight.reject(internalError(`turn failed: ${inflight.turnError.message}`))
        }
      } else if (end.kind === 'error') {
        record.inflight = undefined
        rejectFromError(inflight, end)
      } else {
        const stopReason = turnEndToStopReason(end)
        record.inflight = undefined
        inflight.resolve(stopReason)
      }
    })()
    /* v8 ignore start -- admissionDone only resolves, and the queued path's idle/output gates contain their own failures. */
      .catch((error: unknown) => {
        if (record.inflight !== inflight) return
        record.inflight = undefined
        inflight.reject(internalError(`prompt settlement failed: ${errorChain(error)}`))
      })
    /* v8 ignore stop */
  }

  /** Project one owned session event onto its ordered live ACP stream. */
  const projectLiveEvent = (record: SessionRecord, event: SessionEvent): void => {
    try {
      if (event.type === 'assistant/message' && isAppendSurfaceEvent(event)) {
        const jobs: Array<{ bytes: number; deliver: () => Promise<void> }> = []
        for (const block of event.data.message.content) {
          if (block.type === 'text' && block.text.length > 0) {
            const notification: SessionNotification = {
              sessionId: record.agent.session.id,
              update: {
                sessionUpdate: 'agent_message_chunk',
                content: { type: 'text', text: block.text },
              },
            }
            jobs.push({ bytes: serializedBytes(notification), deliver: () => notify(notification) })
          } else if (block.type === 'image') {
            const emptyNotification: SessionNotification = {
              sessionId: record.agent.session.id,
              update: {
                sessionUpdate: 'agent_message_chunk',
                content: { type: 'image', data: '', mimeType: block.attachment.mediaType },
              },
            }
            const reservedBytes = serializedBytes(emptyNotification) + 4 * Math.ceil(block.attachment.bytes / 3)
            jobs.push({
              bytes: reservedBytes,
              deliver: async () => {
                const content = await assistantBlockToAcp(ctx, block)
                if (content === undefined) throw new Error('assistant image conversion returned no ACP content')
                const notification: SessionNotification = {
                  sessionId: record.agent.session.id,
                  update: { sessionUpdate: 'agent_message_chunk', content },
                }
                if (serializedBytes(notification) > reservedBytes) {
                  throw new Error('assistant image exceeded its declared attachment size')
                }
                await notify(notification)
              },
            })
          }
        }
        const bytes = jobs.reduce((total, job) => total + job.bytes, 0)
        queueLiveOutput(record, event.data.turn, jobs.length, bytes, async () => {
          for (const job of jobs) await job.deliver()
        })
      } else {
        const update = toolActivity(event)
        if (update !== undefined && (event.type === 'tool/call' || event.type === 'tool/result')) {
          const notification: SessionNotification = {
            sessionId: record.agent.session.id,
            update,
          }
          const queued = queueLiveOutput(
            record,
            event.data.turn,
            1,
            serializedBytes(notification),
            () => notify(notification),
          )
          if (queued && event.type === 'tool/call') {
            rememberToolCall(record.toolCalls, event.data.callId, toolMetadata(event.data.name, event.data.arguments))
          }
        }
        if (event.type === 'tool/result' && isAppendSurfaceEvent(event)) {
          record.toolCalls.delete(event.data.message.content[0].toolCallId)
        }
      }
    } finally {
      const inflight = record.inflight
      if (inflight !== undefined && event.type === 'turn/end' && inflight.turn === event.data.turn) {
        inflight.endReason = event.data.reason
      }
    }
  }

  // Emit append-origin assistant text/images plus safe tool lifecycle metadata.
  // Raw chunks, replacements, reasoning, tool output, plans, titles, and retry
  // markers stay off the automation wire. One per-session chain preserves
  // event order.
  ctx.on('session/event', (session, event: SessionEvent) => {
    const record = sessions.get(session.header.id)
    if (record === undefined || record.agent.session !== session) return
    if (record.state === 'closing') return
    if (record.state === 'loading') {
      if (!affectsLiveAcpProjection(event)) return
      const queue = record.loadingEvents
      if (queue === undefined) return
      const bytes = serializedBytes(event)
      if (queue.length >= MAX_LOADING_LIVE_EVENTS
        || record.loadingBytes + bytes > MAX_LOADING_LIVE_BYTES) {
        record.loadingEvents = undefined
        record.loadingBytes = 0
        record.agent.cancel({ kind: 'user' })
        return
      }
      queue.push(event)
      record.loadingBytes += bytes
      return
    }
    projectLiveEvent(record, event)
  })

  ctx.on('agent/inbox/claimed', ({ agent, message, turn }) => {
    const record = ownedRecord(agent)
    const inflight = record?.inflight
    if (inflight !== undefined && inflight.messageId === message.id) inflight.turn = turn
  })

  ctx.on('agent/error', ({ agent, turn, error }) => {
    const record = ownedRecord(agent)
    const inflight = record?.inflight
    if (record === undefined || inflight === undefined || !inflight.messageQueued) return
    if (inflight.turn === turn) {
      inflight.turnError ??= new Error(errorChain(error))
      return
    }
    inflight.agentError = new Error(errorChain(error))
    settleAfterQuiescence(record, inflight)
  })

  // Permission requests are a machine policy channel for ACP clients such as
  // dsh-subagent-acp. The bridge offers one-shot choices only and never infers a
  // durable grant from an unknown client response.
  ctx.on('approval/request', (request, next) => {
    const record = ownedRecord(request.agent)
    const callId = request.callId
    if (record === undefined || callId === undefined) return next()
    const metadata = record.toolCalls.get(callId)
    if (record.state !== 'ready'
      || metadata === undefined
      || !metadata.complete
      || metadata.title !== request.toolName
      || record.pendingPermissions >= MAX_PENDING_PERMISSIONS_PER_SESSION
      || pendingPermissions >= MAX_PENDING_PERMISSIONS_PER_CONNECTION) {
      return Promise.resolve('unavailable')
    }
    record.pendingPermissions += 1
    pendingPermissions += 1
    return Promise.resolve().then(() => conn.requestPermission({
      sessionId: record.agent.session.id,
      toolCall: { toolCallId: callId, ...toolPresentation(metadata) },
      options: [
        { optionId: 'allow-once', name: 'Allow once', kind: 'allow_once' },
        { optionId: 'reject-once', name: 'Reject', kind: 'reject_once' },
      ],
    }))
      .then(({ outcome }): ApprovalOutcome => {
        const current = record.toolCalls.get(callId)
        if (closed || record.state !== 'ready' || sessions.get(record.agent.session.id) !== record) {
          return 'unavailable'
        }
        if (current !== metadata || !current.complete || current.title !== request.toolName) {
          return 'unavailable'
        }
        if (outcome.outcome === 'cancelled') return 'cancelled'
        if (outcome.optionId === 'allow-once') return 'allowed-once'
        if (outcome.optionId === 'reject-once') return 'rejected'
        return 'unavailable'
      }, (): ApprovalOutcome => 'unavailable')
      .finally(() => {
        record.pendingPermissions -= 1
        pendingPermissions -= 1
      })
  })

  const makeAgent = (connection: AgentSideConnection): AcpAgent => {
    conn = connection
    return {
      async initialize(_params: InitializeRequest): Promise<InitializeResponse> {
        // Single-version agent: the spec's "same version if supported, else
        // the latest supported" both resolve to this server's one version.
        imagePromptEnabled = await supportsAcpImagePrompts(ctx, config.provider, config.model)
        return {
          protocolVersion: PROTOCOL_VERSION,
          agentInfo: config.agentInfo,
          agentCapabilities: {
            promptCapabilities: { image: imagePromptEnabled, audio: false, embeddedContext: false },
            sessionCapabilities: { close: {} },
            ...loadEnabled ? { loadSession: true } : {},
          },
          authMethods: [],
        }
      },

      authenticate(_params: AuthenticateRequest): Promise<void> {
        return Promise.resolve()
      },

      async newSession(params: NewSessionRequest): Promise<NewSessionResponse> {
        assertOpen()
        validateSessionParams(params, hostWorkspace)
        const sessionId = SessionId(randomUUID())
        // No preset composition: the ACP bundle keeps the model-facing rows in
        // the host plane, so this agent reads them from the global layer. A
        // deployment that configures a roster has to join one here first
        // (@deepseek-ai/dsh-agent-presets README, "Composing a child agent").
        const handle = await agents.create({
          sessionId,
          meta: { cwd: params.cwd },
          agentOptions: agentOptions(config),
          setup: config.setup,
        })
        /* v8 ignore next 4 -- a real stdio close can race an in-flight create. */
        if (closed) {
          await handle.dispose()
          throw internalError('connection closed during session/new')
        }
        sessions.set(sessionId, {
          agent: handle.agent,
          state: 'ready',
          dispose: () => handle.dispose(),
          outputTail: Promise.resolve(),
          outputActive: false,
          outputQueue: [],
          pendingOutputItems: 0,
          pendingOutputBytes: 0,
          failedOutputTurn: undefined,
          loadingEvents: undefined,
          loadingBytes: 0,
          loadError: undefined,
          toolCalls: new Map(),
          pendingPermissions: 0,
          closing: undefined,
          inflight: undefined,
        })
        return { sessionId }
      },

      async loadSession(params: LoadSessionRequest): Promise<LoadSessionResponse> {
        assertOpen()
        if (!loadEnabled || sessionQuery === undefined
          || sessionPersistence === undefined || sessionsRoot === undefined) {
          throw RequestError.methodNotFound('session/load')
        }
        validateSessionParams(params, hostWorkspace)
        const sessionId = SessionId(params.sessionId)
        if (sessions.has(sessionId)) throw invalidParams(`session is already active: ${sessionId}`)
        let artifact: DurableArtifactPreflight
        try {
          artifact = await preflightDurableArtifact(sessionPersistence, sessionsRoot, sessionId, params.cwd)
        } catch (error: unknown) {
          if (error instanceof DurableArtifactNotFoundError) {
            throw RequestError.resourceNotFound(`session:${sessionId}`)
          }
          if (error instanceof DurableArtifactCwdError) throw invalidParams(error.message)
          throw internalError(`unable to preflight session artifact: ${errorChain(error)}`)
        }
        let snapshot: Awaited<ReturnType<SessionHistoryQuery['readSession']>>
        try {
          snapshot = await sessionQuery.readSession(sessionId)
        } catch (error: unknown) {
          if ((error as { code?: unknown }).code === 'SESSION_QUERY_SESSION_NOT_FOUND') {
            throw RequestError.resourceNotFound(`session:${sessionId}`)
          }
          throw internalError(`unable to read session history: ${errorChain(error)}`)
        }
        if (snapshot.session.cwd !== params.cwd) {
          throw invalidParams(`cwd does not match persisted session: ${params.cwd}`)
        }
        try {
          assertDurableReplayWithinLimits(snapshot.events, 'preflight')
          await assertArtifactIdentity(artifact, sessionsRoot)
        } catch (error: unknown) {
          throw internalError(errorChain(error))
        }
        const setup: AgentSetup = async (agentCtx) => {
          const assertResumedCwd = (): void => {
            if (agentCtx.agent?.session.header.cwd !== params.cwd) {
              throw invalidParams(`cwd does not match resumed session: ${params.cwd}`)
            }
          }
          // A persisted header can change between the preflight query and the
          // factory load. Reject it before trusted product setup observes the
          // wrong workspace, then recheck at the exact publication commit.
          assertResumedCwd()
          const setupCommit = await config.setup(agentCtx)
          return {
            commit: () => {
              assertResumedCwd()
              if (typeof setupCommit === 'object' && setupCommit !== null) setupCommit.commit()
            },
          }
        }
        const handle = await agents.resume({
          resumeSessionId: sessionId,
          agentOptions: agentOptions(config),
          setup,
        })
        /* v8 ignore next 4 -- a real stdio close can race an in-flight resume. */
        if (closed) {
          await handle.dispose()
          throw internalError('connection closed during session/load')
        }
        if (handle.agent.session.header.cwd !== params.cwd) {
          try {
            await handle.dispose()
          } catch (error: unknown) {
            throw internalError(`resumed session cwd mismatch cleanup failed: ${errorChain(error)}`)
          }
          throw invalidParams(`cwd does not match resumed session: ${params.cwd}`)
        }
        let replayEvents: readonly SessionEvent[]
        try {
          await assertArtifactIdentity(artifact, sessionsRoot)
          // Snapshot and recheck the exact public seed accepted by the factory.
          // No await separates this from record publication, so later live
          // appends enter the bounded loadingEvents path instead of escaping.
          replayEvents = [...handle.agent.session.events]
          assertDurableReplayWithinLimits(replayEvents, 'resumed seed')
        } catch (error: unknown) {
          try {
            await handle.dispose()
          } catch (cleanupError: unknown) {
            throw internalError(
              `${errorChain(error)}; resumed owner cleanup failed: ${errorChain(cleanupError)}`,
            )
          }
          throw internalError(errorChain(error))
        }
        const record: SessionRecord = {
          agent: handle.agent,
          state: 'loading',
          dispose: () => handle.dispose(),
          outputTail: Promise.resolve(),
          outputActive: false,
          outputQueue: [],
          pendingOutputItems: 0,
          pendingOutputBytes: 0,
          failedOutputTurn: undefined,
          loadingEvents: [],
          loadingBytes: 0,
          loadError: undefined,
          toolCalls: new Map(),
          pendingPermissions: 0,
          closing: undefined,
          inflight: undefined,
        }
        sessions.set(sessionId, record)
        try {
          // Replay the exact seed accepted by `agents.resume`, not the
          // preflight observation: another writer may have advanced durable
          // history between those operations.
          for (const event of replayEvents) {
            if (record.state !== 'loading') throw new Error('session closed during load')
            if (event.type === 'tool/call') {
              rememberToolCall(record.toolCalls, event.data.callId, toolMetadata(event.data.name, event.data.arguments))
            } else if (event.type === 'tool/result' && isAppendSurfaceEvent(event)) {
              record.toolCalls.delete(event.data.message.content[0].toolCallId)
            }
            if (event.type === 'user/message' || event.type === 'assistant/message') {
              // Durable logs also carry replacement copies for model-only
              // context rewrites. Only append-origin messages belong in the
              // human transcript replayed to an ACP client.
              if (!isAppendSurfaceEvent(event)) continue
              if (event.type === 'user/message' && event.data.source.kind !== 'user') continue
              const message = event.type === 'user/message' ? event.data : event.data.message
              const sessionUpdate = event.type === 'user/message' ? 'user_message_chunk' : 'agent_message_chunk'
              for (const block of message.content) {
                // Another pipelined request can close the record after an earlier replay await.
                if (record.state !== 'loading') throw new Error('session closed during load')
                const content = await assistantBlockToAcp(ctx, block)
                if (content !== undefined) {
                  // Loading is a request, not a best-effort live notification:
                  // any replay write failure rolls the resumed owner back.
                  await conn.sessionUpdate({ sessionId, update: { sessionUpdate, content } })
                  // session/close can run while the client handles this replay update.
                  if (record.state !== 'loading') throw new Error('session closed during load')
                }
              }
              continue
            }
            const update = toolActivity(event)
            if (update !== undefined) {
              await conn.sessionUpdate({ sessionId, update })
              // session/close can run while the client handles this replay update.
              if (record.state !== 'loading') throw new Error('session closed during load')
            }
          }
          while (true) {
            if (record.state !== 'loading') throw new Error('session closed during load')
            const queued = record.loadingEvents
            if (queued === undefined) throw new Error('live output exceeded the session/load replay buffer')
            record.loadingEvents = []
            record.loadingBytes = 0
            for (const event of queued) {
              projectLiveEvent(record, event)
              // queueLiveOutput can fail the loading buffer synchronously.
              if (record.loadingEvents === undefined) {
                throw new Error('live output exceeded the session/load replay buffer')
              }
            }
            await record.outputTail
            if (record.loadError !== undefined) throw record.loadError
            // session/close and loading-buffer overflow can race the output drain.
            if (record.state !== 'loading') throw new Error('session closed during load')
            if (record.loadingEvents === undefined) {
              throw new Error('live output exceeded the session/load replay buffer')
            }
            if (record.loadingEvents.length > 0) continue
            record.loadingEvents = undefined
            record.state = 'ready'
            break
          }
        } catch (error: unknown) {
          const closedDuringLoad = record.state === 'closing'
          try {
            await closeRecord(record, closedDuringLoad ? 'ACP session closed during load' : 'ACP session load failed')
          } catch (cleanupError: unknown) {
            forgetRetiredRecord(record)
            throw internalError(
              `unable to replay session history: ${errorChain(error)}; cleanup failed: ${errorChain(cleanupError)}`,
            )
          }
          if (sessions.get(sessionId) === record) sessions.delete(sessionId)
          if (closedDuringLoad) throw internalError('session closed during load')
          throw internalError(`unable to replay session history: ${errorChain(error)}`)
        }
        return {}
      },

      async prompt(params: PromptRequest): Promise<PromptResponse> {
        assertOpen()
        const record = requireSession(SessionId(params.sessionId))
        if (record.state === 'loading') throw invalidParams('session is loading')
        if (record.state === 'closing') throw invalidParams('session is closing')
        if (record.inflight !== undefined) {
          throw invalidParams('a prompt is already in flight for this session')
        }
        const completion = Promise.withResolvers<StopReason>()
        const admission = Promise.withResolvers<void>()
        const admissionController = new AbortController()
        const inflight: NonNullable<SessionRecord['inflight']> = {
          resolve: completion.resolve,
          reject: completion.reject,
          messageId: undefined,
          messageQueued: false,
          turn: undefined,
          endReason: undefined,
          admissionDone: admission.promise,
          finishAdmission: admission.resolve,
          admissionController,
          cancelRequested: false,
          settlementStarted: false,
          outputError: undefined,
          agentError: undefined,
          turnError: undefined,
        }
        // Reserve the one-prompt slot before the first asynchronous route or
        // attachment operation so concurrent prompts and cancellation observe
        // admission as genuinely in flight.
        record.inflight = inflight

        let admissionFailed = false
        let admissionFailure: unknown
        try {
          // Do not persist rich content for a retired destination. Re-check
          // after admission too because an agent-loop reload may race storage.
          if (agents.get(record.agent.id) !== record.agent) {
            throw internalError('prompt was not queued: the agent was disposed outside the bridge')
          }
          const content = await admitAcpPrompt(
            ctx,
            record.agent,
            params.prompt,
            imagePromptEnabled,
            admissionController.signal,
          )
          // No await may separate this final abort check from followup: a
          // cancellation that wins admission must never enqueue a late turn.
          admissionController.signal.throwIfAborted()
          if (agents.get(record.agent.id) !== record.agent) {
            throw internalError('prompt was not queued: the agent was disposed outside the bridge')
          }
          const message = createUserMessage({ content, source: { kind: 'user' } })
          inflight.messageId = message.id
          inflight.messageQueued = true
          try {
            record.agent.followup(message)
          } catch (error: unknown) {
            // The typed same-process seam may fail synchronously before durable
            // inbox receipt; restore the pre-operation boundary for mapping.
            inflight.messageQueued = false
            throw error
          }
        } catch (error: unknown) {
          admissionFailed = true
          admissionFailure = error
        } finally {
          inflight.finishAdmission()
        }

        if (inflight.cancelRequested) {
          settleAfterQuiescence(record, inflight)
          return { stopReason: await completion.promise }
        }
        if (admissionFailed) {
          record.inflight = undefined
          if (admissionFailure instanceof AcpContentError) {
            throw admissionFailure.kind === 'invalid'
              ? invalidParams(admissionFailure.message)
              : internalError(admissionFailure.message)
          }
          if (admissionFailure instanceof RequestError) throw admissionFailure
          // The admission codec and same-process agent seam throw Error values.
          const detail = (admissionFailure as Error).message
          throw internalError(`prompt was not queued: ${detail}`)
        }

        settleAfterQuiescence(record, inflight)
        const stopReason = await completion.promise
        return { stopReason }
      },

      cancel(params: CancelNotification): Promise<void> {
        const record = sessions.get(SessionId(params.sessionId))
        if (record === undefined) return Promise.resolve()
        const inflight = record.inflight
        if (inflight !== undefined) {
          inflight.cancelRequested = true
          inflight.admissionController.abort(new Error('ACP prompt cancelled'))
          settleAfterQuiescence(record, inflight)
        }
        // Admission is not Agent work. Preserve unrelated producers until this
        // prompt has entered the durable inbox; without a prompt, cancellation
        // continues to target autonomous work on the addressed Agent.
        if (inflight === undefined || inflight.messageQueued) record.agent.cancel({ kind: 'user' })
        return Promise.resolve()
      },

      async closeSession(params: CloseSessionRequest): Promise<CloseSessionResponse> {
        assertOpen()
        const sessionId = SessionId(params.sessionId)
        const record = requireSession(sessionId)
        try {
          await closeRecord(record, 'ACP session closed')
        } catch (error: unknown) {
          forgetRetiredRecord(record)
          throw internalError(`session close failed: ${errorChain(error)}`)
        }
        if (sessions.get(sessionId) === record) sessions.delete(sessionId)
        return {}
      },
    }
  }

  /* v8 ignore next 4 -- production stdio wiring; tests inject config.stream. */
  const stream: Stream = config.stream ?? ndJsonStream(
    Writable.toWeb(process.stdout) as WritableStream<Uint8Array>,
    Readable.toWeb(process.stdin) as ReadableStream<Uint8Array>,
  )
  conn = new AgentSideConnection(makeAgent, stream)

  let quiescing: Promise<void> | undefined
  const quiesce = (): Promise<void> => {
    if (quiescing !== undefined) return quiescing
    closed = true
    const records = [...sessions.values()]
    sessions.clear()
    // Stop the bridge's own work before any await: a descendant drain can block
    // on persistence or scoped cleanup, and the top-level agents must not keep
    // running model and tool calls for its whole duration.
    for (const record of records) {
      const inflight = record.inflight
      if (inflight !== undefined) {
        inflight.cancelRequested = true
        inflight.admissionController.abort(new Error('ACP bridge disposed'))
        settleAfterQuiescence(record, inflight)
      }
      record.agent.cancel({ kind: 'user' })
    }
    quiescing = (async () => {
      // Join a session/close already in flight. A failed close clears its
      // single-flight slot and is retried below under connection-owned
      // best-effort descendant cleanup; a successful one is reused exactly.
      await Promise.allSettled(records.map(record => record.closing ?? Promise.resolve()))
      const disposals = await Promise.allSettled(records.map(record =>
        closeRecord(record, 'ACP bridge disposed', true)))
      const failures: unknown[] = []
      for (const result of disposals) {
        if (result.status === 'rejected') failures.push(result.reason as unknown)
      }
      if (failures.length > 0) {
        // The production consumer logs this AggregateError through `String`,
        // which renders only its message. Embed every per-session diagnostic,
        // including nested causes and aggregate members, in that message.
        const detail = failures.map(failure => errorChain(failure)).join('; ')
        throw new AggregateError(
          failures,
          `ACP agent teardown failed for ${failures.length} session(s): ${detail}`,
        )
      }
    })()
    return quiescing
  }

  /* v8 ignore start -- production transport rejection and teardown failure. */
  void conn.closed
    .catch((error: unknown) => {
      logger.warn(`acp: connection closed with an error: ${String(error)}`)
    })
    .then(quiesce)
    .catch((error: unknown) => {
      logger.warn(`acp: connection-close teardown failed: ${String(error)}`)
    })
  /* v8 ignore stop */

  ctx.effect(() => quiesce, 'acp.connection')
}

/**
 * Build per-agent options from plugin config without assigning absent optional fields.
 * @param config - ACP provider/model configuration.
 * @returns the configured fields only.
 */
function agentOptions(config: AcpConfig): { provider: string; model: string } {
  return { provider: config.provider, model: config.model }
}

/** Reject session features outside the automation contract. */
function validateSessionParams(params: NewSessionRequest, hostWorkspace: string): void {
  if (params.cwd !== hostWorkspace) {
    throw invalidParams(`cwd must exactly equal the canonical host workspace: ${hostWorkspace}`)
  }
  if (params.additionalDirectories !== undefined && params.additionalDirectories.length > 0) {
    throw invalidParams('additionalDirectories is not supported')
  }
  if (params.mcpServers.length > 0) throw invalidParams('mcpServers is not supported')
}

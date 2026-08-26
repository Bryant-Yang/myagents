import { randomUUID } from 'node:crypto'
import { Buffer } from 'node:buffer'
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PROTOCOL_VERSION } from '@agentclientprotocol/sdk'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type { ImageAttachmentRef } from '@deepseek-ai/dsh-attachment'
import { freezeMessage, MessageId } from '@deepseek-ai/dsh-llm'
import { SessionId, type SessionEvent, type SessionHeader } from '@deepseek-ai/dsh-session'
import { makeBridgeHarness, type BridgeHarness } from './harness.ts'

const MAX_DURABLE_REPLAY_EVENTS = 4_096
const MAX_DURABLE_REPLAY_BYTES = 16_777_216

function boundedReplaySeed(count: number, privateText = ''): SessionEvent[] {
  if (count < 4) throw new Error('a bounded replay fixture needs at least four events')
  const seed: SessionEvent[] = [
    { type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } },
    {
      type: 'user/message', seq: 1, time: 2,
      data: freezeMessage({
        id: MessageId('private-replay-fixture'), role: 'user',
        content: [{ type: 'text', text: privateText }],
        source: { kind: 'plugin', plugin: 'test' },
      }),
      surfaceOp: 'append',
    },
    { type: 'step/start', seq: 2, time: 3, data: { turn: 1, step: 1 } },
  ]
  while (seed.length < count - 1) {
    const seq = seed.length
    seed.push({
      type: 'assistant/chunk', seq, time: seq + 1,
      data: { turn: 1, step: 1, chunk: { type: 'text-delta', index: 0, text: '' } },
    })
  }
  seed.push({ type: 'session/end-seed', seq: count - 1, time: count, data: {} })
  return seed
}

function headerLine(header: SessionHeader): Record<string, unknown> {
  return {
    type: 'session',
    version: header.version,
    id: header.id,
    createdAt: header.createdAt,
    cwd: header.cwd,
    delegationDepth: header.delegationDepth ?? 0,
  }
}

function artifactText(header: SessionHeader, events: readonly SessionEvent[]): string {
  return `${[JSON.stringify(headerLine(header)), ...events.map(event => JSON.stringify(event))].join('\n')}\n`
}

function replaySeedWithArtifactBytes(header: SessionHeader, bytes: number): SessionEvent[] {
  const empty = boundedReplaySeed(4)
  const overhead = Buffer.byteLength(artifactText(header, empty), 'utf8')
  if (bytes < overhead) throw new Error('requested replay fixture is smaller than its envelope')
  const seed = boundedReplaySeed(4, 'x'.repeat(bytes - overhead))
  if (Buffer.byteLength(artifactText(header, seed), 'utf8') !== bytes) {
    throw new Error('durable replay byte fixture is not exact')
  }
  return seed
}

interface DurableFixture {
  header: SessionHeader
  sessionsRoot: string
  artifact: string
  persistence: {
    list: () => Promise<SessionHeader[]>
    locate: (meta: SessionHeader) => { kind: 'jsonl'; path: string }
  }
  rewrite: (events: readonly SessionEvent[]) => void
  rewriteRaw: (content: string) => void
}

const fixtureRoots: string[] = []

function durableFixture(
  sessionId: SessionId,
  cwd: string,
  events: readonly SessionEvent[],
): DurableFixture {
  const created = mkdtempSync(join(tmpdir(), 'dsh-acp-load-'))
  const root = realpathSync.native(created)
  fixtureRoots.push(root)
  const sessionsRoot = join(root, 'sessions')
  const artifactDir = join(sessionsRoot, 'project', 'session')
  mkdirSync(artifactDir, { recursive: true })
  const artifact = join(artifactDir, 'session.jsonl')
  const header: SessionHeader = {
    version: 1,
    id: sessionId,
    createdAt: 1,
    cwd,
    delegationDepth: 0,
  }
  const rewriteRaw = (content: string): void => { writeFileSync(artifact, content) }
  const rewrite = (next: readonly SessionEvent[]): void => { rewriteRaw(artifactText(header, next)) }
  rewrite(events)
  return {
    header,
    sessionsRoot,
    artifact,
    persistence: {
      list: () => Promise.resolve([header]),
      locate: () => ({ kind: 'jsonl', path: artifact }),
    },
    rewrite,
    rewriteRaw,
  }
}

function replaySeed(): SessionEvent[] {
  return [
    { type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } },
    {
      type: 'user/message', seq: 1, time: 2,
      data: freezeMessage({
        id: MessageId('direct-user'), role: 'user',
        content: [{ type: 'text', text: 'visible user input' }], source: { kind: 'user' },
      }),
      surfaceOp: 'append',
    },
    {
      type: 'user/message', seq: 2, time: 3,
      data: freezeMessage({
        id: MessageId('workspace-instructions'), role: 'user',
        content: [{ type: 'text', text: 'secret AGENTS instructions' }],
        source: { kind: 'plugin', plugin: 'workspace-context', form: 'instructions' },
      }),
      surfaceOp: 'append',
    },
    {
      type: 'user/message', seq: 3, time: 4,
      data: freezeMessage({
        id: MessageId('compaction-checkpoint'), role: 'user',
        content: [{ type: 'text', text: 'private compacted model context' }],
        source: { kind: 'plugin', plugin: 'compact' },
      }),
      surfaceOp: 'append',
    },
    { type: 'step/start', seq: 4, time: 5, data: { turn: 1, step: 1 } },
    {
      type: 'assistant/message', seq: 5, time: 6,
      data: {
        turn: 1,
        step: 1,
        message: freezeMessage({
          id: MessageId('visible-assistant'), role: 'assistant',
          content: [{ type: 'text', text: 'visible assistant answer' }],
          source: { kind: 'model', provider: 'mock', model: 'mock' },
        }),
      },
      surfaceOp: 'append',
    },
    {
      type: 'assistant/message', seq: 6, time: 7,
      data: {
        turn: 1,
        step: 1,
        message: freezeMessage({
          id: MessageId('model-only-replacement'), role: 'assistant',
          content: [{ type: 'text', text: 'SECRET replacement model context' }],
          source: { kind: 'model', provider: 'mock', model: 'mock' },
        }),
      },
      surfaceOp: { op: 'replace', start: 5, end: 5 },
      sourceEventSeqs: [5],
    },
    { type: 'step/end', seq: 7, time: 8, data: { turn: 1, step: 1 } },
    { type: 'turn/end', seq: 8, time: 9, data: { turn: 1, reason: { kind: 'completed' } } },
  ]
}

describe('ACP durable load validation', () => {
  let harness: BridgeHarness | undefined

  afterEach(async () => {
    await harness?.dispose()
    harness = undefined
    for (const root of fixtureRoots.splice(0)) rmSync(root, { recursive: true, force: true })
  })

  async function mismatchedResume(
    disposeFailure = false,
    forwardSetup = true,
  ): Promise<{ sessionId: SessionId; cwd: string }> {
    const sessionId = SessionId(randomUUID())
    const cwd = process.cwd()
    const durable = durableFixture(sessionId, cwd, [])
    harness = await makeBridgeHarness({
      config: { sessionsRoot: durable.sessionsRoot },
      beforeAcp(ctx) {
        ctx.provide('sessionQuery', {
          readSession: () => Promise.resolve({ session: durable.header, events: [] }),
        } as never)
        ctx.provide('sessionPersistence', durable.persistence as never)
      },
    })
    const create = harness.ctx.agents.create.bind(harness.ctx.agents)
    vi.spyOn(harness.ctx.agents, 'resume').mockImplementation(async (options) => {
      const handle = await create({
        sessionId: options.resumeSessionId,
        meta: { cwd: `${cwd}/different` },
        ...options.agentOptions === undefined ? {} : { agentOptions: options.agentOptions },
        ...!forwardSetup || options.setup === undefined ? {} : { setup: options.setup },
      })
      if (disposeFailure) {
        const originalDispose = handle.dispose.bind(handle)
        handle.dispose = async () => {
          await originalDispose()
          throw new Error('resume cleanup failed')
        }
      }
      return handle
    })
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    return { sessionId, cwd }
  }

  async function resumable(
    seed: SessionEvent[] | ((header: SessionHeader) => SessionEvent[]) = replaySeed(),
  ): Promise<{
    sessionId: SessionId
    cwd: string
    replaceSeed: (next: SessionEvent[]) => void
    durable: DurableFixture
  }> {
    const sessionId = SessionId(randomUUID())
    const cwd = process.cwd()
    const initialDurable = durableFixture(sessionId, cwd, [])
    let activeSeed = typeof seed === 'function' ? seed(initialDurable.header) : seed
    initialDurable.rewrite(activeSeed)
    harness = await makeBridgeHarness({
      config: { sessionsRoot: initialDurable.sessionsRoot },
      beforeAcp(ctx) {
        ctx.provide('sessionQuery', {
          readSession: () => Promise.resolve({ session: initialDurable.header, events: activeSeed }),
        } as never)
        ctx.provide('sessionPersistence', initialDurable.persistence as never)
      },
    })
    const create = harness.ctx.agents.create.bind(harness.ctx.agents)
    vi.spyOn(harness.ctx.agents, 'resume').mockImplementation(options => create({
      sessionId: options.resumeSessionId,
      meta: { cwd },
      seed: activeSeed,
      ...options.agentOptions === undefined ? {} : { agentOptions: options.agentOptions },
      ...options.setup === undefined ? {} : { setup: options.setup },
    }))
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    return {
      sessionId,
      cwd,
      durable: initialDurable,
      replaceSeed: (next) => {
        activeSeed = next
        initialDurable.rewrite(next)
      },
    }
  }

  it('replays only direct user input while preserving assistant history order', async () => {
    const { sessionId, cwd } = await resumable()

    await expect(harness!.client.loadSession({ sessionId, cwd, mcpServers: [] })).resolves.toEqual({})
    expect(harness!.updates).toEqual([
      { sessionUpdate: 'user_message_chunk', content: { type: 'text', text: 'visible user input' } },
      { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'visible assistant answer' } },
    ])
  })

  it('accepts the exact durable replay event-count boundary', async () => {
    const { sessionId, cwd } = await resumable(boundedReplaySeed(MAX_DURABLE_REPLAY_EVENTS))

    await expect(harness!.client.loadSession({ sessionId, cwd, mcpServers: [] })).resolves.toEqual({})
    expect(harness!.updates).toEqual([])
  })

  it('rejects durable replay above the event-count boundary before creating an owner', async () => {
    const { sessionId, cwd } = await resumable(boundedReplaySeed(MAX_DURABLE_REPLAY_EVENTS + 1))

    await expect(harness!.client.loadSession({ sessionId, cwd, mcpServers: [] }))
      .rejects.toThrow(/durable replay exceeds 4096 events or 16777216 artifact bytes/)
    expect(harness!.ctx.agents.resume).not.toHaveBeenCalled()
    expect(harness!.ctx.agents.get(sessionId)).toBeUndefined()
    expect(harness!.updates).toEqual([])
  })

  it('accepts the exact durable replay artifact-byte boundary', async () => {
    const { sessionId, cwd } = await resumable(
      header => replaySeedWithArtifactBytes(header, MAX_DURABLE_REPLAY_BYTES),
    )

    await expect(harness!.client.loadSession({ sessionId, cwd, mcpServers: [] })).resolves.toEqual({})
    expect(harness!.updates).toEqual([])
  })

  it('rejects durable replay above the artifact-byte boundary before creating an owner', async () => {
    const { sessionId, cwd } = await resumable(
      header => replaySeedWithArtifactBytes(header, MAX_DURABLE_REPLAY_BYTES + 1),
    )

    await expect(harness!.client.loadSession({ sessionId, cwd, mcpServers: [] }))
      .rejects.toThrow(/durable replay exceeds 4096 events or 16777216 artifact bytes/)
    expect(harness!.ctx.agents.resume).not.toHaveBeenCalled()
    expect(harness!.ctx.agents.get(sessionId)).toBeUndefined()
    expect(harness!.updates).toEqual([])
  })

  it('rechecks a larger resumed seed, disposes its owner, and leaks no replay updates', async () => {
    const sessionId = SessionId(randomUUID())
    const cwd = process.cwd()
    const preflightSeed = boundedReplaySeed(4)
    const resumedSeed = boundedReplaySeed(MAX_DURABLE_REPLAY_EVENTS + 1)
    const durable = durableFixture(sessionId, cwd, preflightSeed)
    harness = await makeBridgeHarness({
      config: { sessionsRoot: durable.sessionsRoot },
      beforeAcp(ctx) {
        ctx.provide('sessionQuery', {
          readSession: () => Promise.resolve({ session: durable.header, events: preflightSeed }),
        } as never)
        ctx.provide('sessionPersistence', durable.persistence as never)
      },
    })
    const create = harness.ctx.agents.create.bind(harness.ctx.agents)
    vi.spyOn(harness.ctx.agents, 'resume').mockImplementation(options => create({
      sessionId: options.resumeSessionId,
      meta: { cwd },
      seed: resumedSeed,
      ...options.agentOptions === undefined ? {} : { agentOptions: options.agentOptions },
      ...options.setup === undefined ? {} : { setup: options.setup },
    }))
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })

    await expect(harness.client.loadSession({ sessionId, cwd, mcpServers: [] }))
      .rejects.toThrow(/durable replay exceeds 4096 events or 16777216 serialized bytes/)
    expect(harness.ctx.agents.get(sessionId)).toBeUndefined()
    expect(harness.updates).toEqual([])
  })

  it('rejects a symlinked durable artifact before sessionQuery materializes it', async () => {
    const { sessionId, cwd, durable } = await resumable()
    const readSession = vi.spyOn(
      harness!.ctx.get('sessionQuery') as { readSession: () => Promise<unknown> },
      'readSession',
    )
    const outside = join(durable.sessionsRoot, '..', 'outside.jsonl')
    writeFileSync(outside, readFileSync(durable.artifact))
    rmSync(durable.artifact)
    symlinkSync(outside, durable.artifact)

    await expect(harness!.client.loadSession({ sessionId, cwd, mcpServers: [] }))
      .rejects.toThrow(/regular non-symlink file/)
    expect(readSession).not.toHaveBeenCalled()
    expect(harness!.ctx.agents.resume).not.toHaveBeenCalled()
  })

  it('rejects packed storage rows instead of trusting an unproved one-event-per-line layout', async () => {
    const { sessionId, cwd, durable } = await resumable()
    const readSession = vi.spyOn(
      harness!.ctx.get('sessionQuery') as { readSession: () => Promise<unknown> },
      'readSession',
    )
    durable.rewriteRaw([
      JSON.stringify(headerLine(durable.header)),
      JSON.stringify({ type: 'text-chunks', seq: 0, chunks: [] }),
      '',
    ].join('\n'))

    await expect(harness!.client.loadSession({ sessionId, cwd, mcpServers: [] }))
      .rejects.toThrow(/one contiguous event per line/)
    expect(readSession).not.toHaveBeenCalled()
    expect(harness!.ctx.agents.resume).not.toHaveBeenCalled()
  })

  it('rejects an artifact changed after bounded scan but before session materialization', async () => {
    const seed = replaySeed()
    const { sessionId, cwd, durable } = await resumable(seed)
    const query = harness!.ctx.get('sessionQuery') as {
      readSession: () => Promise<{ session: SessionHeader; events: SessionEvent[] }>
    }
    vi.spyOn(query, 'readSession').mockImplementation(async () => {
      writeFileSync(durable.artifact, `${readFileSync(durable.artifact, 'utf8')} `)
      return { session: durable.header, events: seed }
    })

    await expect(harness!.client.loadSession({ sessionId, cwd, mcpServers: [] }))
      .rejects.toThrow(/artifact changed after bounded preflight/)
    expect(harness!.ctx.agents.resume).not.toHaveBeenCalled()
  })

  it('rejects a pipelined prompt until durable replay finishes', async () => {
    const { sessionId, cwd } = await resumable()
    const updateStarted = Promise.withResolvers<undefined>()
    const releaseUpdate = Promise.withResolvers<undefined>()
    let first = true
    harness!.onSessionUpdate = async () => {
      if (!first) return
      first = false
      updateStarted.resolve(undefined)
      await releaseUpdate.promise
    }

    const loading = harness!.client.loadSession({ sessionId, cwd, mcpServers: [] })
    await updateStarted.promise
    await expect(harness!.client.prompt({
      sessionId,
      prompt: [{ type: 'text', text: 'must not enter while replaying' }],
    })).rejects.toThrow(/session is loading/)

    releaseUpdate.resolve(undefined)
    await expect(loading).resolves.toEqual({})
  })

  it('finishes frozen replay before releasing live output appended while an image is loading', async () => {
    const { sessionId, cwd, replaceSeed } = await resumable([])
    const image = await harness!.attachments!.saveImage({ data: Uint8Array.of(9), mediaType: 'image/png' })
    replaceSeed(imageReplaySeed(image))
    const readStarted = Promise.withResolvers<undefined>()
    const releaseRead = Promise.withResolvers<undefined>()
    harness!.attachments!.beforeRead = async () => {
      readStarted.resolve(undefined)
      await releaseRead.promise
    }

    const loading = harness!.client.loadSession({ sessionId, cwd, mcpServers: [] })
    await readStarted.promise
    const agent = harness!.ctx.agents.get(sessionId)!
    agent.session.append('turn/start', { turn: 2 })
    agent.session.append('step/start', { turn: 2, step: 1 })
    agent.session.append('assistant/message', {
      turn: 2,
      step: 1,
      message: freezeMessage({
        id: MessageId('live-during-replay'), role: 'assistant',
        content: [{ type: 'text', text: 'LIVE' }],
        source: { kind: 'model', provider: 'mock', model: 'mock' },
      }),
    }, { surfaceOp: 'append' })
    agent.session.append('step/end', { turn: 2, step: 1 })
    agent.session.append('turn/end', { turn: 2, reason: { kind: 'completed' } })

    expect(harness!.updates).toEqual([
      { sessionUpdate: 'user_message_chunk', content: { type: 'text', text: 'OLD-U' } },
    ])
    releaseRead.resolve(undefined)
    await expect(loading).resolves.toEqual({})
    expect(harness!.updates).toEqual([
      { sessionUpdate: 'user_message_chunk', content: { type: 'text', text: 'OLD-U' } },
      { sessionUpdate: 'agent_message_chunk', content: { type: 'image', data: 'CQ==', mimeType: 'image/png' } },
      { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'LIVE' } },
    ])
  })

  it('rejects load and disposes its owner when queued live image conversion fails', async () => {
    const { sessionId, cwd, replaceSeed } = await resumable([])
    const image = await harness!.attachments!.saveImage({ data: Uint8Array.of(9), mediaType: 'image/png' })
    replaceSeed(imageReplaySeed(image))
    const readStarted = Promise.withResolvers<undefined>()
    const releaseRead = Promise.withResolvers<undefined>()
    let firstRead = true
    harness!.attachments!.beforeRead = async () => {
      if (!firstRead) return
      firstRead = false
      readStarted.resolve(undefined)
      await releaseRead.promise
    }

    const loading = harness!.client.loadSession({ sessionId, cwd, mcpServers: [] })
    await readStarted.promise
    const agent = harness!.ctx.agents.get(sessionId)!
    const missing: ImageAttachmentRef = {
      attachmentId: `sha256:${'a'.repeat(64)}` as never,
      mediaType: 'image/png',
      bytes: 1,
      width: 1,
      height: 1,
    }
    agent.session.append('assistant/message', {
      turn: 2,
      step: 1,
      message: freezeMessage({
        id: MessageId('missing-live-image'), role: 'assistant',
        content: [{ type: 'image', attachment: missing }],
        source: { kind: 'model', provider: 'mock', model: 'mock' },
      }),
    }, { surfaceOp: 'append' })

    releaseRead.resolve(undefined)
    await expect(loading).rejects.toThrow(/cannot deliver assistant image/)
    expect(harness!.ctx.agents.get(sessionId)).toBeUndefined()
    expect(harness!.updates).toEqual([
      { sessionUpdate: 'user_message_chunk', content: { type: 'text', text: 'OLD-U' } },
      { sessionUpdate: 'agent_message_chunk', content: { type: 'image', data: 'CQ==', mimeType: 'image/png' } },
    ])
  })

  it('bounds live events retained behind a blocked durable replay', async () => {
    const { sessionId, cwd, replaceSeed } = await resumable([])
    const image = await harness!.attachments!.saveImage({ data: Uint8Array.of(9), mediaType: 'image/png' })
    replaceSeed(imageReplaySeed(image))
    const readStarted = Promise.withResolvers<undefined>()
    const releaseRead = Promise.withResolvers<undefined>()
    harness!.attachments!.beforeRead = async () => {
      readStarted.resolve(undefined)
      await releaseRead.promise
    }

    const loading = harness!.client.loadSession({ sessionId, cwd, mcpServers: [] })
    await readStarted.promise
    const agent = harness!.ctx.agents.get(sessionId)!
    const cancel = vi.spyOn(agent, 'cancel')
    for (let index = 0; index < 256; index += 1) {
      appendLiveAssistant(agent, index)
    }
    expect(cancel).not.toHaveBeenCalled()
    appendLiveAssistant(agent, 256)
    expect(cancel).toHaveBeenCalledTimes(1)

    releaseRead.resolve(undefined)
    await expect(loading).rejects.toThrow(/live output exceeded the session\/load replay buffer/)
    expect(harness!.ctx.agents.get(sessionId)).toBeUndefined()
    expect(harness!.updates.some(update =>
      update.sessionUpdate === 'agent_message_chunk'
      && update.content.type === 'text'
      && update.content.text.startsWith('LIVE-'))).toBe(false)
  })

  it('does not spend the loading buffer on raw assistant chunks that never reach ACP', async () => {
    const { sessionId, cwd, replaceSeed } = await resumable([])
    const image = await harness!.attachments!.saveImage({ data: Uint8Array.of(9), mediaType: 'image/png' })
    replaceSeed(imageReplaySeed(image))
    const readStarted = Promise.withResolvers<undefined>()
    const releaseRead = Promise.withResolvers<undefined>()
    harness!.attachments!.beforeRead = async () => {
      readStarted.resolve(undefined)
      await releaseRead.promise
    }

    const loading = harness!.client.loadSession({ sessionId, cwd, mcpServers: [] })
    await readStarted.promise
    const agent = harness!.ctx.agents.get(sessionId)!
    const cancel = vi.spyOn(agent, 'cancel')
    for (let index = 0; index < 1_000; index += 1) {
      agent.session.append('assistant/chunk', {
        turn: 2,
        step: 1,
        chunk: { type: 'text-delta', index: 0, text: `raw-${index}` },
      })
    }
    expect(cancel).not.toHaveBeenCalled()

    releaseRead.resolve(undefined)
    await expect(loading).resolves.toEqual({})
    expect(harness!.updates).toEqual([
      { sessionUpdate: 'user_message_chunk', content: { type: 'text', text: 'OLD-U' } },
      { sessionUpdate: 'agent_message_chunk', content: { type: 'image', data: 'CQ==', mimeType: 'image/png' } },
    ])
  })

  it('closes a loading owner without letting the load report success', async () => {
    const { sessionId, cwd } = await resumable()
    const updateStarted = Promise.withResolvers<undefined>()
    const releaseUpdate = Promise.withResolvers<undefined>()
    harness!.onSessionUpdate = async () => {
      updateStarted.resolve(undefined)
      await releaseUpdate.promise
    }

    const loading = harness!.client.loadSession({ sessionId, cwd, mcpServers: [] })
    await updateStarted.promise
    await expect(harness!.client.closeSession({ sessionId })).resolves.toEqual({})
    expect(harness!.ctx.agents.get(sessionId)).toBeUndefined()

    releaseUpdate.resolve(undefined)
    await expect(loading).rejects.toThrow(/session closed during load/)
  })

  it('rejects and disposes a resumed owner whose durable cwd differs from preflight', async () => {
    const { sessionId, cwd } = await mismatchedResume()

    await expect(harness!.client.loadSession({ sessionId, cwd, mcpServers: [] }))
      .rejects.toThrow(/cwd does not match resumed session/)
    expect(harness!.ctx.agents.get(sessionId)).toBeUndefined()
  })

  it('reports resumed-cwd cleanup failure only after the owner is contained', async () => {
    // Simulate a non-conforming factory that ignores the supplied setup seam;
    // the independent post-resume check must still contain its published owner.
    const { sessionId, cwd } = await mismatchedResume(true, false)

    await expect(harness!.client.loadSession({ sessionId, cwd, mcpServers: [] }))
      .rejects.toThrow(/resumed session cwd mismatch cleanup failed: resume cleanup failed/)
    expect(harness!.ctx.agents.get(sessionId)).toBeUndefined()
  })
})

function imageReplaySeed(image: ImageAttachmentRef): SessionEvent[] {
  return [
    { type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } },
    {
      type: 'user/message', seq: 1, time: 2,
      data: freezeMessage({
        id: MessageId('old-user'), role: 'user',
        content: [{ type: 'text', text: 'OLD-U' }], source: { kind: 'user' },
      }),
      surfaceOp: 'append',
    },
    { type: 'step/start', seq: 2, time: 3, data: { turn: 1, step: 1 } },
    {
      type: 'assistant/message', seq: 3, time: 4,
      data: {
        turn: 1,
        step: 1,
        message: freezeMessage({
          id: MessageId('old-image'), role: 'assistant',
          content: [{ type: 'image', attachment: image }],
          source: { kind: 'model', provider: 'mock', model: 'mock' },
        }),
      },
      surfaceOp: 'append',
    },
    { type: 'step/end', seq: 4, time: 5, data: { turn: 1, step: 1 } },
    { type: 'turn/end', seq: 5, time: 6, data: { turn: 1, reason: { kind: 'completed' } } },
  ]
}

function appendLiveAssistant(agent: Agent, index: number): void {
  agent.session.append('assistant/message', {
    turn: 2,
    step: 1,
    message: freezeMessage({
      id: MessageId(`live-buffer-${index}`), role: 'assistant',
      content: [{ type: 'text', text: `LIVE-${index}` }],
      source: { kind: 'model', provider: 'mock', model: 'mock' },
    }),
  }, { surfaceOp: 'append' })
}

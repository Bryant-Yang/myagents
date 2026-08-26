/**
 * myagents-owned ACP surface layered over the stock DSH base bundle.
 *
 * The official profile owns process boot and the shared runtime services. This
 * Loader plugin validates the product topology, freezes one model selection,
 * applies the myagents permission profile, and publishes only the ACP surface.
 * @module @myagents/dsh-acp-host
 */

import { lstatSync, realpathSync } from 'node:fs'
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { installModelSelection, type AgentSetup, type ModelSelection } from '@deepseek-ai/dsh-agent'
import { effectiveSandboxMode, setSandboxMode } from '@deepseek-ai/dsh-sandbox-policy'
import type { PreToolDecision, ToolDefinition, ToolExecution } from '@deepseek-ai/dsh-tools'
import { effectiveApprovalPolicy, setApprovalPolicy } from '@deepseek-ai/dsh-user-approval'
import * as ProductAcp from './acp.ts'

export const name = 'myagents-dsh-acp-host'

export type Profile = 'workspace-write' | 'read-only'

export const READ_ONLY_TOOLS = ['read', 'glob', 'grep'] as const

export const HOST_VERSION = '0.1.0'
export const DSH_RUNTIME_VERSION = '0.1.1-rc.2'
export const COMPATIBILITY_REVISION = 1

export interface Config {
  profile: Profile
}

export const Config: z<Config> = z.object({
  profile: z.union([z.const('workspace-write'), z.const('read-only')]).required(),
})

interface HostPaths {
  sessions: string
}

function canonicalPath(path: string): string {
  let cursor = resolve(path)
  const suffix: string[] = []
  for (;;) {
    try {
      lstatSync(cursor)
      break
    } catch (error: unknown) {
      const code = (error as NodeJS.ErrnoException | null)?.code
      if (code !== 'ENOENT' && code !== 'ENOTDIR') throw error
    }
    const parent = dirname(cursor)
    if (parent === cursor) break
    suffix.unshift(basename(cursor))
    cursor = parent
  }
  return resolve(realpathSync.native(cursor), ...suffix)
}

function requiredCanonicalAbsolute(name: string): string {
  const value = process.env[name]
  if (value === undefined || value.length === 0 || !isAbsolute(value)) {
    throw new Error(`myagents DSH host: ${name} must be an absolute path`)
  }
  const canonical = canonicalPath(value)
  if (canonical !== value) {
    throw new Error(`myagents DSH host: ${name} must be a canonical absolute path: ${canonical}`)
  }
  return canonical
}

function containsPath(parent: string, candidate: string): boolean {
  const relation = relative(parent, candidate)
  return relation === ''
    || (!isAbsolute(relation) && relation !== '..' && !relation.startsWith(`..${sep}`))
}

function assertDisjoint(leftName: string, left: string, rightName: string, right: string): void {
  if (containsPath(left, right) || containsPath(right, left)) {
    throw new Error(`myagents DSH host: ${leftName} and ${rightName} must not overlap`)
  }
}

function derivedStateDirectory(name: string, parent: string, ...segments: string[]): string {
  const expected = resolve(parent, ...segments)
  if (!containsPath(parent, expected)) {
    throw new Error(`myagents DSH host: ${name} escaped DSH_ACP_PERSISTENCE_DIR`)
  }
  let cursor = parent
  for (const segment of segments) {
    cursor = join(cursor, segment)
    try {
      const stat = lstatSync(cursor)
      if (stat.isSymbolicLink()) {
        throw new Error(`myagents DSH host: ${name} must not contain symlinks: ${cursor}`)
      }
      if (!stat.isDirectory()) {
        throw new Error(`myagents DSH host: ${name} must be a directory: ${cursor}`)
      }
    } catch (error: unknown) {
      const code = (error as NodeJS.ErrnoException | null)?.code
      if (code === 'ENOENT' || code === 'ENOTDIR') break
      throw error
    }
  }
  const canonical = canonicalPath(expected)
  if (canonical !== expected || !containsPath(parent, canonical)) {
    throw new Error(`myagents DSH host: ${name} must stay canonically within DSH_ACP_PERSISTENCE_DIR`)
  }
  return canonical
}

const FORBIDDEN_NODE_ENV = new Set([
  'NODE_OPTIONS',
  'NODE_PATH',
  'NODE_COMPILE_CACHE',
  'NODE_V8_COVERAGE',
])

function rejectRuntimeInjection(): void {
  for (const [name, value] of Object.entries(process.env)) {
    if (value === undefined || value.length === 0) continue
    if (FORBIDDEN_NODE_ENV.has(name) || name.startsWith('TS_NODE_')) {
      throw new Error(`myagents DSH host: environment must not inject the Node runtime via ${name}`)
    }
  }
  for (const name of ['DSH_ACP_SOURCE_ROOT', 'DSH_ACP_ENV_DIR', 'TSX_TSCONFIG_PATH']) {
    if (process.env[name] !== undefined) {
      throw new Error(`myagents DSH host: official profile must not inherit source-only ${name}`)
    }
  }
}

function hostPaths(): HostPaths {
  rejectRuntimeInjection()
  const persistence = requiredCanonicalAbsolute('DSH_ACP_PERSISTENCE_DIR')
  const sessions = derivedStateDirectory('sessions directory', persistence, 'sessions')
  const runtimeHome = derivedStateDirectory('runtime-home', persistence, 'runtime-home')
  const attachmentHome = derivedStateDirectory('attachment-home', persistence, 'attachment-home')
  const agentsHome = derivedStateDirectory('agents directory', persistence, 'runtime-home', 'agents')
  const settingsFile = requiredCanonicalAbsolute('DSH_ACP_SETTINGS_FILE')
  const credentialsFile = requiredCanonicalAbsolute('DSH_ACP_CREDENTIALS_FILE')
  const dshHome = requiredCanonicalAbsolute('DSH_HOME')
  const workspace = canonicalPath(process.cwd())
  if (workspace !== process.cwd()) {
    throw new Error(`myagents DSH host: workspace must be a canonical absolute path: ${workspace}`)
  }
  const entry = canonicalPath(fileURLToPath(import.meta.url))
  const pluginRoot = resolve(dirname(entry), '..')
  const executable = canonicalPath(process.execPath)

  const exactPaths: ReadonlyArray<[name: string, expected: string]> = [
    ['DSH_ACP_RUNTIME_HOME', runtimeHome],
    ['DSH_ACP_ATTACHMENT_HOME', attachmentHome],
    ['DSH_AGENTS_HOME', agentsHome],
  ]
  for (const [name, expected] of exactPaths) {
    if (requiredCanonicalAbsolute(name) !== expected) {
      throw new Error(`myagents DSH host: ${name} must equal ${expected}`)
    }
  }

  assertDisjoint('DSH_ACP_PERSISTENCE_DIR', persistence, 'workspace', workspace)
  assertDisjoint('DSH_ACP_PERSISTENCE_DIR', persistence, 'DSH_HOME', dshHome)
  assertDisjoint('DSH_ACP_PERSISTENCE_DIR', persistence, 'myagents DSH plugin root', pluginRoot)
  assertDisjoint('DSH_ACP_PERSISTENCE_DIR', persistence, 'Node executable', executable)
  assertDisjoint('DSH_HOME', dshHome, 'workspace', workspace)
  assertDisjoint('workspace', workspace, 'myagents DSH plugin root', pluginRoot)
  assertDisjoint('workspace', workspace, 'Node executable', executable)
  assertDisjoint('DSH_ACP_SETTINGS_FILE', settingsFile, 'workspace', workspace)
  assertDisjoint('DSH_ACP_CREDENTIALS_FILE', credentialsFile, 'workspace', workspace)
  assertDisjoint('DSH_ACP_SETTINGS_FILE', settingsFile, 'DSH_ACP_CREDENTIALS_FILE', credentialsFile)

  for (const [label, path] of [
    ['sessions directory', sessions],
    ['runtime-home', runtimeHome],
    ['attachment-home', attachmentHome],
    ['agents directory', agentsHome],
  ] as const) {
    assertDisjoint(label, path, 'workspace', workspace)
    assertDisjoint(label, path, 'myagents DSH plugin root', pluginRoot)
    assertDisjoint(label, path, 'Node executable', executable)
  }
  for (const [label, path] of [
    ['DSH_ACP_SETTINGS_FILE', settingsFile],
    ['DSH_ACP_CREDENTIALS_FILE', credentialsFile],
  ] as const) {
    assertDisjoint(label, path, 'sessions directory', sessions)
    assertDisjoint(label, path, 'attachment-home', attachmentHome)
    assertDisjoint(label, path, 'agents directory', agentsHome)
    assertDisjoint(label, path, 'myagents DSH plugin root', pluginRoot)
    assertDisjoint(label, path, 'Node executable', executable)
  }

  return { sessions }
}

export function hostAgentInfo(profile: Profile) {
  return {
    name: 'dsh-myagents-acp',
    title: 'DeepSeek Harness for myagents',
    version: HOST_VERSION,
    _meta: {
      'deepseek.ai/dsh-myagents-profile': profile,
      'deepseek.ai/dsh-myagents-policy-revision': 1,
      'deepseek.ai/dsh-myagents-read-only-tools': [...READ_ONLY_TOOLS],
      'deepseek.ai/dsh-runtime-version': DSH_RUNTIME_VERSION,
      'deepseek.ai/dsh-compatibility-revision': COMPATIBILITY_REVISION,
    },
  }
}

function safeDefinitions(ctx: Context): ReadonlyMap<string, ToolDefinition> {
  const definitions = new Map<string, ToolDefinition>()
  for (const toolName of READ_ONLY_TOOLS) {
    const definition = ctx.tools.get(toolName)
    if (definition === undefined) {
      throw new Error(`myagents DSH host: required safe tool is unavailable: ${toolName}`)
    }
    definitions.set(toolName, definition)
  }
  return definitions
}

function isSafeCall(
  ctx: Context,
  definitions: ReadonlyMap<string, ToolDefinition>,
  exec: Readonly<ToolExecution>,
): boolean {
  if (exec.agent === undefined) return false
  const expected = definitions.get(exec.name)
  return expected !== undefined && ctx.tools.get(exec.name, exec.agent) === expected
}

export function profileSetup(profile: Profile): AgentSetup {
  return (agentCtx) => {
    const agent = agentCtx.agent
    if (agent === undefined) throw new Error('myagents DSH host: agent setup has no owner')
    const definitions = safeDefinitions(agentCtx)
    setSandboxMode(agent.session, profile)
    setApprovalPolicy(agent.session, profile === 'read-only' ? 'never' : 'ask')

    if (profile === 'read-only') {
      agentCtx.tools.restrict({ allow: READ_ONLY_TOOLS })
      agentCtx.tools.guard((exec) => isSafeCall(agentCtx, definitions, exec)
        ? undefined
        : 'read-only profile permits only the built-in read, glob, and grep tools')
    } else {
      agentCtx.on('tools/pre-execute', async (exec, next): Promise<PreToolDecision> => {
        const downstream = await next()
        if (downstream.kind !== 'allow' || isSafeCall(agentCtx, definitions, exec)) return downstream
        return { kind: 'ask', reason: `workspace-write profile requires one-shot approval for ${exec.name}` }
      })
    }

    return {
      commit(): void {
        for (const [toolName, definition] of definitions) {
          if (agentCtx.tools.get(toolName, agent) !== definition) {
            throw new Error(`myagents DSH host: safe tool identity changed before publication: ${toolName}`)
          }
        }
        if (effectiveSandboxMode(agent.session.events) !== profile) {
          throw new Error(`myagents DSH host: sandbox profile attestation failed: ${profile}`)
        }
        const expectedApproval = profile === 'read-only' ? 'never' : 'ask'
        if (effectiveApprovalPolicy(agent.session.events) !== expectedApproval) {
          throw new Error(`myagents DSH host: approval profile attestation failed: ${expectedApproval}`)
        }
        if (profile === 'read-only') {
          const visible = agentCtx.tools.schemas(agent).map((tool) => tool.name).sort()
          const expected = [...READ_ONLY_TOOLS].sort()
          if (visible.length !== expected.length
            || visible.some((toolName, index) => toolName !== expected[index])) {
            throw new Error(`myagents DSH host: read-only tool closure attestation failed: ${visible.join(',')}`)
          }
        }
      },
    }
  }
}

function explicitModelSelection(): ModelSelection | undefined {
  const provider = process.env['DSH_ACP_PROVIDER']
  const model = process.env['DSH_ACP_MODEL']
  if ((provider === undefined) !== (model === undefined)) {
    throw new Error('myagents DSH host: DSH_ACP_PROVIDER and DSH_ACP_MODEL must be configured together')
  }
  return provider === undefined || model === undefined ? undefined : { provider, model }
}

async function frozenModelSelection(
  ctx: Context,
  explicit: ModelSelection | undefined,
): Promise<Readonly<ModelSelection>> {
  const defaultModel = ctx.get('agentDefaultModel')
  const selection = explicit ?? defaultModel?.currentSelection()
  if (selection === undefined || selection.provider.length === 0 || selection.model.length === 0) {
    throw new Error('myagents DSH host: model selection must contain a provider and model')
  }
  const llm = ctx.get('llm')
  if (llm === undefined) throw new Error('myagents DSH host: llm service is unavailable')
  await llm.resolveModelInfo(selection.provider, selection.model)
  return Object.freeze({ ...selection })
}

async function awaitModelSettingsRegistration(ctx: Context): Promise<void> {
  const settings = ctx.get('settings') as { get(namespace: never): unknown } | undefined
  if (settings === undefined) throw new Error('myagents DSH host: settings service is unavailable')
  for (let attempt = 0; attempt < 16; attempt += 1) {
    if (settings.get('agent-default-model' as never) !== undefined) return
    await new Promise<void>(resolve => { setImmediate(resolve) })
  }
  throw new Error('myagents DSH host: agent-default-model settings did not become ready')
}

/** Mount the product transport after the stock base services have settled. */
export async function apply(ctx: Context, config: Config): Promise<void> {
  const paths = hostPaths()
  const explicitSelection = explicitModelSelection()
  const appExit = ctx.get('appExit') as ((code: number) => void) | undefined
  if (appExit === undefined) {
    throw new Error('myagents DSH host: official dsh profile launcher did not provide appExit')
  }
  // Let the ACP reader finish its EOF turn, then enter the official launcher's
  // bounded SIGTERM path (documented as a normal zero-code supervisor stop).
  // Calling appExit directly can leave Node's top-level profile await pending
  // after the last stdio handle closes.
  const onInputEnd = (): void => {
    setImmediate(() => { process.kill(process.pid, 'SIGTERM') })
  }
  process.stdin.once('end', onInputEnd)
  ctx.effect(() => () => { process.stdin.off('end', onInputEnd) })

  await ctx.inject([
    'agents',
    'agentDefaultModel',
    'attachments',
    'llm',
    'sessionPersistence',
    'sessionQuery',
    'settings',
    'tools',
  ], async (runtimeCtx) => {
    await awaitModelSettingsRegistration(runtimeCtx)
    const selection = await frozenModelSelection(runtimeCtx, explicitSelection)
    const setup: AgentSetup = (agentCtx) => {
      installModelSelection(agentCtx, { current: selection, assembled: undefined })
      return profileSetup(config.profile)(agentCtx)
    }
    await runtimeCtx.plugin(ProductAcp, {
      provider: selection.provider,
      model: selection.model,
      setup,
      agentInfo: hostAgentInfo(config.profile),
      sessionsRoot: paths.sessions,
    })
  })
}

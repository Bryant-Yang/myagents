import { spawn } from 'node:child_process'
import { mkdir, mkdtemp, realpath, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { isAbsolute, join } from 'node:path'
import { Readable, Writable } from 'node:stream'
import {
  ClientSideConnection,
  ndJsonStream,
  PROTOCOL_VERSION,
  type Agent as AcpAgent,
  type Client,
  type RequestPermissionRequest,
  type RequestPermissionResponse,
  type SessionNotification,
} from '@agentclientprotocol/sdk'
import { afterEach, describe, expect, it } from 'vitest'

const sourceInput = process.env['MYAGENTS_DSH_SOURCE_ROOT']
const testHomeInput = process.env['MYAGENTS_DSH_TEST_HOME']
if (sourceInput === undefined || !isAbsolute(sourceInput)) {
  throw new Error('MYAGENTS_DSH_SOURCE_ROOT must name the stock DSH checkout')
}
if (testHomeInput === undefined || !isAbsolute(testHomeInput)) {
  throw new Error('MYAGENTS_DSH_TEST_HOME must name the temporary installed profile home')
}
const sourceRoot = await realpath(sourceInput)
const testHome = await realpath(testHomeInput)
const dshBin = join(sourceRoot, 'apps/cli/lib/bin.js')
const dirs: string[] = []

afterEach(async () => {
  for (const dir of dirs.splice(0)) await rm(dir, { recursive: true, force: true })
})

async function topology(label: string) {
  const root = await realpath(await mkdtemp(join(tmpdir(), `dsh-bundle-${label}-`)))
  dirs.push(root)
  const workspace = join(root, 'workspace')
  const persistence = join(root, 'state')
  const config = join(root, 'config')
  await mkdir(workspace)
  await mkdir(config)
  const settings = join(config, 'settings.yaml')
  const credentials = join(config, '.credentials.yaml')
  await writeFile(credentials, '', { mode: 0o600 })
  return { root, workspace, persistence, settings, credentials }
}

type EnvOverrides = Record<string, string | undefined>

function childEnv(
  paths: Awaited<ReturnType<typeof topology>>,
  overrides: EnvOverrides = {},
): NodeJS.ProcessEnv {
  const runtimeHome = join(paths.persistence, 'runtime-home')
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    DEEPSEEK_API_KEY: 'keyless-acp-myagents-smoke',
    DSH_HOME: testHome,
    DSH_ACP_PROFILE: 'read-only',
    DSH_ACP_PERSISTENCE_DIR: paths.persistence,
    DSH_ACP_RUNTIME_HOME: runtimeHome,
    DSH_ACP_ATTACHMENT_HOME: join(paths.persistence, 'attachment-home'),
    DSH_ACP_SETTINGS_FILE: paths.settings,
    DSH_ACP_CREDENTIALS_FILE: paths.credentials,
    DSH_AGENTS_HOME: join(runtimeHome, 'agents'),
    DSH_TELEMETRY_DISABLED: '1',
  }
  for (const name of [
    'DSH_ACP_PROVIDER',
    'DSH_ACP_MODEL',
    'DSH_ACP_SOURCE_ROOT',
    'DSH_ACP_ENV_DIR',
    'TSX_TSCONFIG_PATH',
    'NODE_OPTIONS',
    'NODE_PATH',
    'NODE_COMPILE_CACHE',
    'NODE_V8_COVERAGE',
  ]) delete env[name]
  for (const name of Object.keys(env)) if (name.startsWith('TS_NODE_')) delete env[name]
  for (const [name, value] of Object.entries(overrides)) {
    if (value === undefined) delete env[name]
    else env[name] = value
  }
  return env
}

async function runHost(
  paths: Awaited<ReturnType<typeof topology>>,
  overrides: EnvOverrides = {},
): Promise<{ code: number | null; stderr: string }> {
  const child = spawn(process.execPath, [dshBin, '--profile', 'myagents'], {
    cwd: paths.workspace,
    env: childEnv(paths, overrides),
    stdio: ['pipe', 'ignore', 'pipe'],
  })
  const stderr: Buffer[] = []
  child.stderr.on('data', chunk => stderr.push(chunk as Buffer))
  child.stdin.end()
  const code = await new Promise<number | null>(resolve => child.once('exit', resolve))
  return { code, stderr: Buffer.concat(stderr).toString('utf8') }
}

async function handshake(image = false): Promise<void> {
  const paths = await topology(image ? 'vision' : 'text')
  if (image) {
    await writeFile(paths.settings, [
      'agent-default-model:',
      '  provider: deepseek-official',
      '  model: deepseek-v4-flash-vision-exp',
      '',
    ].join('\n'))
  }
  const child = spawn(process.execPath, [dshBin, '--profile', 'myagents'], {
    cwd: paths.workspace,
    env: childEnv(paths),
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  const stderr: Buffer[] = []
  child.stderr.on('data', chunk => stderr.push(chunk as Buffer))
  const exited = new Promise<number | null>(resolve => child.once('exit', resolve))
  try {
    const stream = ndJsonStream(
      Writable.toWeb(child.stdin) as WritableStream<Uint8Array>,
      Readable.toWeb(child.stdout) as ReadableStream<Uint8Array>,
    )
    const client = new ClientSideConnection((_agent: AcpAgent): Client => ({
      sessionUpdate(_params: SessionNotification): Promise<void> { return Promise.resolve() },
      requestPermission(_params: RequestPermissionRequest): Promise<RequestPermissionResponse> {
        return Promise.resolve({ outcome: { outcome: 'cancelled' } })
      },
    }), stream)
    const initialized = await client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    expect(initialized.agentInfo?._meta).toMatchObject({
      'deepseek.ai/dsh-runtime-version': '0.1.2-alpha.2',
      'deepseek.ai/dsh-compatibility-revision': 2,
      'deepseek.ai/dsh-myagents-profile': 'read-only',
    })
    expect(initialized.agentCapabilities).toMatchObject({
      promptCapabilities: { image },
      loadSession: true,
      sessionCapabilities: { close: {} },
    })
    const { sessionId } = await client.newSession({ cwd: paths.workspace, mcpServers: [] })
    await client.closeSession({ sessionId })
    child.stdin.end()
    const code = await Promise.race([
      exited,
      new Promise<never>((_resolve, reject) => {
        setTimeout(() => { reject(new Error('official profile did not exit after ACP stdin closed')) }, 5_000)
      }),
    ])
    expect(code).toBe(0)
  } catch (error: unknown) {
    throw new Error(`${error instanceof Error ? error.message : String(error)}\nstderr:\n${Buffer.concat(stderr).toString('utf8')}`)
  } finally {
    if (child.exitCode === null) {
      child.kill('SIGKILL')
      await exited
    }
  }
}

describe('installed official DSH profile bundle', () => {
  it('loads built JavaScript and exposes initialize/new/close over ACP', () => handshake(false), 30_000)
  it.each([1, 2, 3])(
    'uses the same installed contract for an image-capable model (run %s)',
    () => handshake(true),
    30_000,
  )

  it('rejects a partial explicit provider/model override before ACP publication', async () => {
    const paths = await topology('partial-model')
    const result = await runHost(paths, { DSH_ACP_PROVIDER: 'deepseek-official' })
    expect(result.code).not.toBe(0)
    expect(result.stderr).toContain('DSH_ACP_PROVIDER and DSH_ACP_MODEL must be configured together')
  })

  it.each([
    'DSH_ACP_PERSISTENCE_DIR',
    'DSH_ACP_RUNTIME_HOME',
    'DSH_ACP_ATTACHMENT_HOME',
    'DSH_ACP_SETTINGS_FILE',
    'DSH_ACP_CREDENTIALS_FILE',
    'DSH_AGENTS_HOME',
  ])('rejects relative product path %s', async (name) => {
    const paths = await topology(`relative-${name.toLowerCase()}`)
    const result = await runHost(paths, { [name]: 'relative-path' })
    expect(result.code).not.toBe(0)
    expect(result.stderr).toContain(`${name} must be an absolute path`)
  })

  it('rejects derived product-state topology drift', async () => {
    const paths = await topology('topology-drift')
    for (const [name, value] of [
      ['DSH_ACP_RUNTIME_HOME', join(paths.root, 'other-runtime')],
      ['DSH_ACP_ATTACHMENT_HOME', join(paths.root, 'other-attachments')],
      ['DSH_AGENTS_HOME', join(paths.root, 'other-agents')],
    ] as const) {
      const result = await runHost(paths, { [name]: value })
      expect(result.code).not.toBe(0)
      expect(result.stderr).toContain(`${name} must equal`)
    }
  })

  it.each(['DSH_ACP_SOURCE_ROOT', 'DSH_ACP_ENV_DIR', 'TSX_TSCONFIG_PATH'])(
    'rejects obsolete source-only runtime injection via %s',
    async (name) => {
      const paths = await topology(`source-env-${name.toLowerCase()}`)
      const result = await runHost(paths, { [name]: sourceRoot })
      expect(result.code).not.toBe(0)
      expect(result.stderr).toContain(`official profile must not inherit source-only ${name}`)
    },
  )

  it.each(['NODE_OPTIONS', 'NODE_PATH', 'TS_NODE_REQUIRE'])(
    'rejects ambient Node runtime injection via %s',
    async (name) => {
      const paths = await topology(`node-env-${name.toLowerCase()}`)
      const value = name === 'NODE_OPTIONS' ? '--no-warnings' : paths.root
      const result = await runHost(paths, { [name]: value })
      expect(result.code).not.toBe(0)
      expect(result.stderr).toContain(`must not inject the Node runtime via ${name}`)
    },
  )

  it.each([
    ['sessions directory', 'sessions'],
    ['runtime-home', 'runtime-home'],
    ['attachment-home', 'attachment-home'],
  ])('rejects a symlinked derived %s', async (expected, child) => {
    const paths = await topology(`symlink-${child}`)
    await mkdir(paths.persistence)
    const { symlink } = await import('node:fs/promises')
    await symlink(paths.workspace, join(paths.persistence, child))
    const result = await runHost(paths)
    expect(result.code).not.toBe(0)
    expect(result.stderr).toContain(`${expected} must not contain symlinks`)
  })

  it('rejects state and config overlap with the workspace', async () => {
    const paths = await topology('overlap')
    const state = join(paths.workspace, 'state')
    const stateResult = await runHost(paths, {
      DSH_ACP_PERSISTENCE_DIR: state,
      DSH_ACP_RUNTIME_HOME: join(state, 'runtime-home'),
      DSH_ACP_ATTACHMENT_HOME: join(state, 'attachment-home'),
      DSH_AGENTS_HOME: join(state, 'runtime-home', 'agents'),
    })
    expect(stateResult.code).not.toBe(0)
    expect(stateResult.stderr).toContain('DSH_ACP_PERSISTENCE_DIR and workspace must not overlap')

    const settingsResult = await runHost(paths, {
      DSH_ACP_SETTINGS_FILE: join(paths.workspace, 'settings.yaml'),
    })
    expect(settingsResult.code).not.toBe(0)
    expect(settingsResult.stderr).toContain('DSH_ACP_SETTINGS_FILE and workspace must not overlap')
  })
})

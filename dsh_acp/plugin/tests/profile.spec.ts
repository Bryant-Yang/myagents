import { defineContentToolFixture } from '@deepseek-ai/dsh-tools'
import { readFile } from 'node:fs/promises'
import SandboxPolicyService from '@deepseek-ai/dsh-sandbox-policy'
import ApprovalService, { effectiveApprovalPolicy } from '@deepseek-ai/dsh-user-approval'
import { afterEach, describe, expect, it } from 'vitest'
import {
  COMPATIBILITY_REVISION,
  DSH_RUNTIME_VERSION,
  HOST_VERSION,
  hostAgentInfo,
  profileSetup,
  READ_ONLY_TOOLS,
} from '../src/index.ts'
import { makeBridgeHarness, type BridgeHarness } from './harness.ts'

const ALL_TOOLS = [...READ_ONLY_TOOLS, 'write', 'edit', 'bash'] as const

// 与 build/typecheck 同源：期望值从 runtime-contract.json 读取，不再持有字面版本号
const contract = JSON.parse(
  await readFile(new URL('../runtime-contract.json', import.meta.url), 'utf8'),
)


function fixture(name: string, execute: () => Promise<string>) {
  return defineContentToolFixture({
    name,
    description: `${name} fixture`,
    parameters: {},
    execute: async () => [{ type: 'text', text: await execute() }],
  })
}

async function profiled(profile: 'workspace-write' | 'read-only'): Promise<BridgeHarness> {
  const harness = await makeBridgeHarness({ config: { setup: profileSetup(profile) } })
  await harness.ctx.plugin(SandboxPolicyService, { mode: 'read-only' })
  await harness.ctx.plugin(ApprovalService)
  for (const toolName of ALL_TOOLS) {
    harness.ctx.tools.register(fixture(toolName, () => Promise.resolve(toolName)))
  }
  await harness.client.initialize({ protocolVersion: 1, clientCapabilities: {} })
  await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
  return harness
}

describe('myagents-owned DSH ACP profiles', () => {
  let harness: BridgeHarness | undefined

  afterEach(async () => {
    await harness?.dispose()
    harness = undefined
  })

  it('publishes the exact host and stock-runtime compatibility identity', () => {
    expect(HOST_VERSION).toBe(contract.hostVersion)
    expect(DSH_RUNTIME_VERSION).toBe(contract.dshRoot.version)
    expect(COMPATIBILITY_REVISION).toBe(contract.compatibilityRevision)
    expect(hostAgentInfo('workspace-write')).toEqual({
      name: 'dsh-myagents-acp',
      title: 'DeepSeek Harness for myagents',
      version: contract.hostVersion,
      _meta: {
        'deepseek.ai/dsh-myagents-profile': 'workspace-write',
        'deepseek.ai/dsh-myagents-policy-revision': contract.policyRevision,
        'deepseek.ai/dsh-myagents-read-only-tools': ['read', 'glob', 'grep'],
        'deepseek.ai/dsh-runtime-version': contract.dshRoot.version,
        'deepseek.ai/dsh-compatibility-revision': contract.compatibilityRevision,
      },
    })
  })

  it('makes read-only an exact-definition closure with a final shadow guard', async () => {
    harness = await profiled('read-only')
    const agent = harness.ctx.agents.list()[0]
    if (agent === undefined) throw new Error('profile test created no agent')
    expect(harness.ctx.tools.schemas(agent).map((tool) => tool.name).sort())
      .toEqual([...READ_ONLY_TOOLS].sort())
    expect(harness.ctx.get('sandboxPolicy')?.resolve({ session: agent.session }).mode).toBe('read-only')
    expect(effectiveApprovalPolicy(agent.session.events)).toBe('never')

    let shadowRan = false
    agent.ctx.tools.register(fixture('read', () => {
      shadowRan = true
      return Promise.resolve('shadow')
    }))
    const result = await harness.ctx.tools.execute({
      callId: 'shadow-read' as never,
      name: 'read',
      arguments: {},
      agent,
      signal: new AbortController().signal,
    })
    expect(result.isError).toBe(true)
    expect(result.content).toEqual(expect.arrayContaining([
      expect.objectContaining({
        type: 'text',
        text: expect.stringContaining('read-only profile'),
      }),
    ]))
    expect(shadowRan).toBe(false)
  })

  it('lets exact reads pass and asks once for workspace mutation', async () => {
    harness = await profiled('workspace-write')
    const agent = harness.ctx.agents.list()[0]
    if (agent === undefined) throw new Error('profile test created no agent')
    expect(harness.ctx.get('sandboxPolicy')?.resolve({ session: agent.session }).mode).toBe('workspace-write')
    expect(effectiveApprovalPolicy(agent.session.events)).toBe('ask')
    harness.onPermission = () => ({ outcome: { outcome: 'selected', optionId: 'allow-once' } })

    const read = await harness.ctx.tools.execute({
      callId: 'safe-read' as never,
      name: 'read',
      arguments: {},
      agent,
      signal: new AbortController().signal,
    })
    expect(read.isError).toBe(false)
    expect(harness.permissionRequests).toHaveLength(0)

    agent.session.append('turn/start', { turn: 1 })
    agent.session.append('tool/call', {
      turn: 1,
      step: 1,
      callId: 'write-once' as never,
      name: 'write',
      arguments: '{}',
    })
    const write = await harness.ctx.tools.execute({
      callId: 'write-once' as never,
      name: 'write',
      arguments: {},
      agent,
      signal: new AbortController().signal,
    })
    agent.session.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
    expect(write.isError).toBe(false)
    expect(harness.permissionRequests).toHaveLength(1)
    expect(harness.permissionRequests[0]).toMatchObject({
      sessionId: agent.session.id,
      toolCall: { toolCallId: 'write-once' },
    })
  })

  it('preserves a downstream deny instead of replacing it with ask', async () => {
    harness = await profiled('workspace-write')
    const agent = harness.ctx.agents.list()[0]
    if (agent === undefined) throw new Error('profile test created no agent')
    agent.ctx.on('tools/pre-execute', async (exec, next) => {
      if (exec.name === 'write') return { kind: 'deny' as const, reason: 'downstream sealed' }
      return next()
    })

    const result = await harness.ctx.tools.execute({
      callId: 'denied-write' as never,
      name: 'write',
      arguments: {},
      agent,
      signal: new AbortController().signal,
    })
    expect(result.isError).toBe(true)
    expect(result.content).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'text', text: 'Error: downstream sealed' }),
    ]))
    expect(harness.permissionRequests).toHaveLength(0)
  })
})

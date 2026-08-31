import { afterEach, describe, expect, it, vi } from 'vitest'
import { PROTOCOL_VERSION, type RequestPermissionResponse } from '@agentclientprotocol/sdk'
import { ToolCallId, createToolResultMessage } from '@deepseek-ai/dsh-llm'
import type { Agent } from '@deepseek-ai/dsh-agent'
import { SessionId } from '@deepseek-ai/dsh-session'
import ApprovalService, { type ApprovalRequest } from '@deepseek-ai/dsh-user-approval'
import { makeBridgeHarness, type BridgeHarness } from './harness.ts'

describe('ACP machine permission policy', () => {
  let harness: BridgeHarness | undefined

  afterEach(async () => {
    await harness?.dispose()
    harness = undefined
  })

  async function ownedRequest(overrides: Partial<ApprovalRequest> = {}): Promise<ApprovalRequest> {
    if (harness === undefined) throw new Error('missing harness')
    await harness.ctx.plugin(ApprovalService)
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    const { sessionId } = await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
    const agent = harness.ctx.agents.get(SessionId(sessionId))!
    agent.session.append('turn/start', { turn: 1 })
    const request = { agent, toolName: 'bash', callId: ToolCallId('call-9'), ...overrides }
    if (request.callId !== undefined) {
      agent.session.append('tool/call', {
        turn: 1,
        step: 1,
        callId: request.callId,
        name: request.toolName,
        arguments: '{"cmd":"pwd"}',
      })
    }
    return request
  }

  it('maps the two advertised one-shot choices', async () => {
    harness = await makeBridgeHarness()
    harness.onPermission = () => ({ outcome: { outcome: 'selected', optionId: 'allow-once' } })
    const request = await ownedRequest()
    await expect(harness.ctx.approval.request(request)).resolves.toBe('allowed-once')
    expect(harness.permissionRequests[0]).toMatchObject({
      sessionId: request.agent.session.id,
      toolCall: {
        toolCallId: 'call-9',
        title: 'bash',
        kind: 'execute',
        rawInput: { cmd: 'pwd' },
      },
      options: [
        { optionId: 'allow-once', kind: 'allow_once' },
        { optionId: 'reject-once', kind: 'reject_once' },
      ],
    })

    harness.onPermission = () => ({ outcome: { outcome: 'selected', optionId: 'reject-once' } })
    await expect(harness.ctx.approval.request(request)).resolves.toBe('rejected')

    request.agent.session.append('tool/result', {
      turn: 1,
      step: 1,
      message: createToolResultMessage({ callId: request.callId!, content: [], isError: false }),
    }, { surfaceOp: 'append' })
    harness.onPermission = () => ({ outcome: { outcome: 'selected', optionId: 'allow-once' } })
    await expect(harness.ctx.approval.request(request)).resolves.toBe('unavailable')
    expect(harness.permissionRequests).toHaveLength(2)
  })

  it('bounds unresolved tool metadata and refuses authorization after FIFO eviction', async () => {
    harness = await makeBridgeHarness()
    harness.onPermission = () => ({ outcome: { outcome: 'selected', optionId: 'allow-once' } })
    const oldest = await ownedRequest()

    for (let index = 0; index < 256; index += 1) {
      oldest.agent.session.append('tool/call', {
        turn: 1,
        step: 1,
        callId: ToolCallId(`flood-${index}`),
        name: 'read',
        arguments: `{"path":"file-${index}"}`,
      })
      if ((index + 1) % 32 === 0) {
        await vi.waitFor(() => {
          expect(harness!.updates.filter(update => update.sessionUpdate === 'tool_call')).toHaveLength(index + 2)
        })
      }
    }

    await expect(harness.ctx.approval.request(oldest)).resolves.toBe('unavailable')
    expect(harness.permissionRequests).toHaveLength(0)

    await expect(harness.ctx.approval.request({
      agent: oldest.agent,
      toolName: 'read',
      callId: ToolCallId('flood-255'),
    })).resolves.toBe('allowed-once')
    expect(harness.permissionRequests.at(-1)?.toolCall).toMatchObject({
      toolCallId: 'flood-255',
      title: 'read',
      kind: 'read',
      rawInput: { path: 'file-255' },
    })
  })

  it('does not offer allow-once when tool arguments are truncated', async () => {
    harness = await makeBridgeHarness()
    harness.onPermission = () => ({ outcome: { outcome: 'selected', optionId: 'allow-once' } })
    const request = await ownedRequest()
    const callId = ToolCallId('oversized-call')
    request.agent.session.append('tool/call', {
      turn: 1,
      step: 1,
      callId,
      name: 'bash',
      arguments: JSON.stringify({ cmd: 'x'.repeat(17_000) }),
    })

    await expect(harness.ctx.approval.request({
      agent: request.agent,
      toolName: 'bash',
      callId,
    })).resolves.toBe('unavailable')
    expect(harness.permissionRequests).toHaveLength(0)
  })

  it('binds approval to the exact tool name recorded for the call id', async () => {
    harness = await makeBridgeHarness()
    harness.onPermission = () => ({ outcome: { outcome: 'selected', optionId: 'allow-once' } })
    const request = await ownedRequest({ toolName: 'read' })

    await expect(harness.ctx.approval.request({
      agent: request.agent,
      toolName: 'bash',
      callId: request.callId!,
    })).resolves.toBe('unavailable')
    expect(harness.permissionRequests).toHaveLength(0)
  })

  it('refuses an unresolved duplicate call id instead of authorizing either observation', async () => {
    harness = await makeBridgeHarness()
    harness.onPermission = () => ({ outcome: { outcome: 'selected', optionId: 'allow-once' } })
    const request = await ownedRequest({ toolName: 'read' })
    request.agent.session.append('tool/call', {
      turn: 1,
      step: 1,
      callId: request.callId!,
      name: 'bash',
      arguments: '{"cmd":"rm -rf target"}',
    })

    await expect(harness.ctx.approval.request(request)).resolves.toBe('unavailable')
    await expect(harness.ctx.approval.request({
      agent: request.agent,
      toolName: 'bash',
      callId: request.callId!,
    })).resolves.toBe('unavailable')
    expect(harness.permissionRequests).toHaveLength(0)
  })

  it('refuses a same-tool duplicate call id with different arguments', async () => {
    harness = await makeBridgeHarness()
    harness.onPermission = () => ({ outcome: { outcome: 'selected', optionId: 'allow-once' } })
    const request = await ownedRequest({ toolName: 'read' })
    request.agent.session.append('tool/call', {
      turn: 1,
      step: 1,
      callId: request.callId!,
      name: 'read',
      arguments: '{"path":"different-secret"}',
    })

    await expect(harness.ctx.approval.request(request)).resolves.toBe('unavailable')
    expect(harness.permissionRequests).toHaveLength(0)
  })

  it('bounds uncancellable permission RPCs across cancelled asks and session close', async () => {
    harness = await makeBridgeHarness()
    const clientAnswers: Array<PromiseWithResolvers<RequestPermissionResponse>> = []
    harness.onPermission = () => {
      const answer = Promise.withResolvers<RequestPermissionResponse>()
      clientAnswers.push(answer)
      return answer.promise
    }
    const first = await ownedRequest({ toolName: 'read' })
    const firstAgent = first.agent
    const { sessionId: secondSessionId } = await harness.client.newSession({
      cwd: process.cwd(),
      mcpServers: [],
    })
    const secondAgent = harness.ctx.agents.get(SessionId(secondSessionId))!
    secondAgent.session.append('turn/start', { turn: 1 })

    for (let index = 0; index < 16; index += 1) {
      const agent = index < 8 ? firstAgent : secondAgent
      const callId = index === 0 ? first.callId! : ToolCallId(`pending-${index}`)
      if (index > 0) {
        agent.session.append('tool/call', {
          turn: 1,
          step: 1,
          callId,
          name: 'read',
          arguments: `{"path":"pending-${index}"}`,
        })
      }
      const controller = new AbortController()
      const decision = harness.ctx.approval.request({
        agent,
        toolName: 'read',
        callId,
        signal: controller.signal,
      })
      await vi.waitFor(() => { expect(harness!.permissionRequests).toHaveLength(index + 1) })
      controller.abort()
      await expect(decision).resolves.toBe('cancelled')
    }

    const overflowId = ToolCallId('pending-overflow')
    firstAgent.session.append('tool/call', {
      turn: 1,
      step: 1,
      callId: overflowId,
      name: 'read',
      arguments: '{"path":"overflow"}',
    })
    await expect(harness.ctx.approval.request({ agent: firstAgent, toolName: 'read', callId: overflowId }))
      .resolves.toBe('unavailable')
    expect(harness.permissionRequests).toHaveLength(16)

    await harness.client.closeSession({ sessionId: firstAgent.session.id })
    await harness.client.closeSession({ sessionId: secondAgent.session.id })
    for (const answer of clientAnswers) {
      answer.resolve({ outcome: { outcome: 'selected', optionId: 'allow-once' } })
    }
    await Promise.all(clientAnswers.map(answer => answer.promise))
    expect(harness.permissionRequests).toHaveLength(16)
  })

  it('ignores a late allow answer after the owning session closes', async () => {
    harness = await makeBridgeHarness()
    const clientAnswer = Promise.withResolvers<RequestPermissionResponse>()
    harness.onPermission = () => clientAnswer.promise
    const request = await ownedRequest({ toolName: 'read' })

    const decision = harness.ctx.approval.request(request)
    await vi.waitFor(() => { expect(harness!.permissionRequests).toHaveLength(1) })
    await harness.client.closeSession({ sessionId: request.agent.session.id })
    clientAnswer.resolve({ outcome: { outcome: 'selected', optionId: 'allow-once' } })

    await expect(decision).resolves.toBe('unavailable')
  })

  it('invalidates a pending allow answer when the call id becomes an unresolved duplicate', async () => {
    harness = await makeBridgeHarness()
    const clientAnswer = Promise.withResolvers<RequestPermissionResponse>()
    harness.onPermission = () => clientAnswer.promise
    const request = await ownedRequest({ toolName: 'read' })

    const decision = harness.ctx.approval.request(request)
    await vi.waitFor(() => { expect(harness!.permissionRequests).toHaveLength(1) })
    request.agent.session.append('tool/call', {
      turn: 1,
      step: 1,
      callId: request.callId!,
      name: 'bash',
      arguments: '{"cmd":"dangerous"}',
    })
    clientAnswer.resolve({ outcome: { outcome: 'selected', optionId: 'allow-once' } })

    await expect(decision).resolves.toBe('unavailable')
  })

  it('invalidates a pending allow answer when the exact tool call settles', async () => {
    harness = await makeBridgeHarness()
    const clientAnswer = Promise.withResolvers<RequestPermissionResponse>()
    harness.onPermission = () => clientAnswer.promise
    const request = await ownedRequest({ toolName: 'read' })

    const decision = harness.ctx.approval.request(request)
    await vi.waitFor(() => { expect(harness!.permissionRequests).toHaveLength(1) })
    request.agent.session.append('tool/result', {
      turn: 1,
      step: 1,
      message: createToolResultMessage({ callId: request.callId!, content: [], isError: false }),
    }, { surfaceOp: 'append' })
    clientAnswer.resolve({ outcome: { outcome: 'selected', optionId: 'allow-once' } })

    await expect(decision).resolves.toBe('unavailable')
  })

  it('maps cancellation and unknown choices without granting access', async () => {
    harness = await makeBridgeHarness()
    const request = await ownedRequest()
    await expect(harness.ctx.approval.request(request)).resolves.toBe('cancelled')
    harness.onPermission = () => ({ outcome: { outcome: 'selected', optionId: 'unknown-grant' } })
    await expect(harness.ctx.approval.request(request)).resolves.toBe('unavailable')
  })

  it('fails closed when the client errors the permission request', async () => {
    harness = await makeBridgeHarness()
    const request = await ownedRequest()
    harness.onPermission = () => { throw new Error('client gone') }
    await expect(harness.ctx.approval.request(request)).resolves.toBe('unavailable')
  })

  it('delegates a same-id foreign agent', async () => {
    harness = await makeBridgeHarness()
    const request = await ownedRequest()
    const foreign = {
      session: { id: request.agent.session.id, events: [{ type: 'turn/start' }], append: () => ({}) },
    } as unknown as Agent
    await expect(harness.ctx.approval.request({ agent: foreign, toolName: 'bash', callId: ToolCallId('call') }))
      .resolves.toBe('unavailable')
    expect(harness.permissionRequests).toHaveLength(0)
  })

  it('delegates requests that have no protocol tool-call identity', async () => {
    harness = await makeBridgeHarness()
    const request = await ownedRequest()
    await expect(harness.ctx.approval.request({ agent: request.agent, toolName: request.toolName }))
      .resolves.toBe('unavailable')
    expect(harness.permissionRequests).toHaveLength(0)
  })
})

import { afterEach, describe, expect, it, vi } from 'vitest'
import { PROTOCOL_VERSION } from '@agentclientprotocol/sdk'
import {
  ToolCallId,
  createToolResultMessage,
  createUserMessage,
  freezeMessage,
  MessageId,
  type StreamChunk,
} from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'
import { defineContentToolFixture } from '@deepseek-ai/dsh-tools'
import ApprovalService from '@deepseek-ai/dsh-user-approval'
import { makeBridgeHarness, textResponse, type BridgeHarness } from './harness.ts'

function toolCallResponse(): StreamChunk[] {
  return [
    { type: 'block-start', index: 0, blockType: 'tool-call' },
    { type: 'tool-call-delta', index: 0, id: ToolCallId('call-1'), name: 'echo', argumentsDelta: '{}' },
    { type: 'block-end', index: 0, block: { type: 'tool-call', id: ToolCallId('call-1'), name: 'echo', arguments: '{}' } },
    { type: 'finish', reason: { kind: 'tool-calls' } },
  ]
}

describe('ACP automation output boundary', () => {
  let harness: BridgeHarness | undefined

  afterEach(async () => {
    await harness?.dispose()
    harness = undefined
  })

  it('emits safe tool activity and committed output without reasoning or raw tool output', async () => {
    harness = await makeBridgeHarness({ script: [toolCallResponse(), textResponse('done')] })
    harness.ctx.tools.register(defineContentToolFixture({
      name: 'echo',
      description: 'Return a deterministic result.',
      parameters: {},
      execute: () => Promise.resolve([{ type: 'text', text: 'tool result' }]),
    }))
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    const { sessionId } = await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
    await harness.client.prompt({ sessionId, prompt: [{ type: 'text', text: 'go' }] })

    await vi.waitFor(() => { expect(harness!.updates).toHaveLength(3) })
    expect(harness.updates).toEqual([
      {
        sessionUpdate: 'tool_call',
        toolCallId: 'call-1',
        title: 'echo',
        kind: 'other',
        rawInput: {},
        status: 'in_progress',
      },
      {
        sessionUpdate: 'tool_call_update',
        toolCallId: 'call-1',
        status: 'completed',
      },
      {
        sessionUpdate: 'agent_message_chunk',
        content: { type: 'text', text: 'done' },
      },
    ])
  })

  it('ignores events from agents the bridge does not own', async () => {
    harness = await makeBridgeHarness({ script: [textResponse('foreign')] })
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
    const { agent } = await harness.ctx.agents.create({
      sessionId: SessionId('foreign'),
      agentOptions: { provider: 'mock', model: 'mock' },
    })
    agent.followup(createUserMessage({ content: [{ type: 'text', text: 'go' }], source: { kind: 'user' } }))
    await agent.whenIdle()
    expect(harness.updates).toHaveLength(0)
  })

  it('delivers output from a bridge-owned session driven by another in-process producer', async () => {
    harness = await makeBridgeHarness({ script: [textResponse('external')] })
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    const { sessionId } = await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
    const agent = harness.ctx.agents.get(SessionId(sessionId))!

    agent.followup(createUserMessage({ content: [{ type: 'text', text: 'go' }], source: { kind: 'plugin', plugin: 'test' } }))
    await agent.whenIdle()

    expect(harness.updates).toEqual([{
      sessionUpdate: 'agent_message_chunk',
      content: { type: 'text', text: 'external' },
    }])
  })

  it('delivers one fast committed text-and-image batch larger than the slow-client byte budget', async () => {
    harness = await makeBridgeHarness()
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    const { sessionId } = await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
    const agent = harness.ctx.agents.get(SessionId(sessionId))!
    const imageData = new Uint8Array(40_000).fill(7)
    const image = await harness.attachments!.saveImage({ data: imageData, mediaType: 'image/png' })
    const text = 'x'.repeat(50_000)

    agent.session.append('turn/start', { turn: 1 })
    agent.session.append('step/start', { turn: 1, step: 1 })
    agent.session.append('assistant/message', {
      turn: 1,
      step: 1,
      message: freezeMessage({
        id: MessageId('large-fast-live-output'),
        role: 'assistant',
        content: [{ type: 'text', text }, { type: 'image', attachment: image }],
        source: { kind: 'model', provider: 'mock', model: 'mock' },
      }),
    }, { surfaceOp: 'append' })
    agent.session.append('step/end', { turn: 1, step: 1 })
    agent.session.append('turn/end', { turn: 1, reason: { kind: 'completed' } })

    await harness.client.closeSession({ sessionId })
    expect(harness.updates).toEqual([
      { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text } },
      {
        sessionUpdate: 'agent_message_chunk',
        content: { type: 'image', data: Buffer.from(imageData).toString('base64'), mimeType: 'image/png' },
      },
    ])
  })

  it('does not publish a model-only assistant replacement on the live wire', async () => {
    harness = await makeBridgeHarness()
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    const { sessionId } = await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
    const agent = harness.ctx.agents.get(SessionId(sessionId))!
    agent.session.append('turn/start', { turn: 1 })
    agent.session.append('step/start', { turn: 1, step: 1 })
    const original = agent.session.append('assistant/message', {
      turn: 1,
      step: 1,
      message: freezeMessage({
        id: MessageId('visible-live-assistant'),
        role: 'assistant',
        content: [{ type: 'text', text: 'visible live answer' }],
        source: { kind: 'model', provider: 'mock', model: 'mock' },
      }),
    }, { surfaceOp: 'append' })
    agent.session.append('assistant/message', {
      turn: 1,
      step: 1,
      message: freezeMessage({
        id: MessageId('model-only-live-replacement'),
        role: 'assistant',
        content: [{ type: 'text', text: 'SECRET live replacement' }],
        source: { kind: 'model', provider: 'mock', model: 'mock' },
      }),
    }, {
      surfaceOp: { op: 'replace', start: original.seq, end: original.seq },
      sourceEventSeqs: [original.seq],
    })
    agent.session.append('step/end', { turn: 1, step: 1 })
    agent.session.append('turn/end', { turn: 1, reason: { kind: 'completed' } })

    await harness.client.closeSession({ sessionId })
    expect(harness.updates).toEqual([{
      sessionUpdate: 'agent_message_chunk',
      content: { type: 'text', text: 'visible live answer' },
    }])
  })

  it('does not publish a replacement tool result as a second terminal update', async () => {
    harness = await makeBridgeHarness()
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    const { sessionId } = await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
    const agent = harness.ctx.agents.get(SessionId(sessionId))!
    const callId = ToolCallId('live-replaced-result')
    agent.session.append('turn/start', { turn: 1 })
    agent.session.append('step/start', { turn: 1, step: 1 })
    agent.session.append('tool/call', {
      turn: 1,
      step: 1,
      callId,
      name: 'echo',
      arguments: '{}',
    })
    const original = agent.session.append('tool/result', {
      turn: 1,
      step: 1,
      message: createToolResultMessage({
        callId,
        content: [{ type: 'text', text: 'visible tool result' }],
        isError: false,
      }),
    }, { surfaceOp: 'append' })
    agent.session.append('tool/result', {
      ...original.data,
      message: freezeMessage({
        ...original.data.message,
        content: [{
          ...original.data.message.content[0],
          content: [{ type: 'text', text: 'SECRET replacement tool result' }],
        }] satisfies typeof original.data.message.content,
      }),
    }, {
      surfaceOp: { op: 'replace', start: original.seq, end: original.seq },
      sourceEventSeqs: [original.seq],
    })
    agent.session.append('step/end', { turn: 1, step: 1 })
    agent.session.append('turn/end', { turn: 1, reason: { kind: 'completed' } })

    await harness.client.closeSession({ sessionId })
    expect(harness.updates.filter(update => update.sessionUpdate === 'tool_call_update')).toEqual([{
      sessionUpdate: 'tool_call_update',
      toolCallId: callId,
      status: 'completed',
    }])
  })

  it('contains output conversion failure outside an ACP prompt', async () => {
    harness = await makeBridgeHarness({ script: [[
      { type: 'block-start', index: 0, blockType: 'image' },
      {
        type: 'block-end',
        index: 0,
        block: {
          type: 'image',
          attachment: {
            attachmentId: `sha256:${'a'.repeat(64)}` as never,
            mediaType: 'image/png',
            bytes: 1,
            width: 1,
            height: 1,
          },
        },
      },
      { type: 'finish', reason: { kind: 'stop' } },
    ]] })
    const warn = vi.spyOn(harness.ctx.logger, 'warn')
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    const { sessionId } = await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
    const agent = harness.ctx.agents.get(SessionId(sessionId))!

    agent.followup(createUserMessage({ content: [{ type: 'text', text: 'go' }], source: { kind: 'plugin', plugin: 'test' } }))
    await agent.whenIdle()
    await vi.waitFor(() => { expect(warn).toHaveBeenCalledWith(expect.stringContaining('output conversion failed')) })
    expect(harness.updates).toEqual([])
  })

  it('accepts exactly 64 blocked live updates, then cancels and rejects the owning prompt', async () => {
    harness = await makeBridgeHarness({ script: ['hang'] })
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    const { sessionId } = await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
    const agent = harness.ctx.agents.get(SessionId(sessionId))!
    const cancel = vi.spyOn(agent, 'cancel')
    const updateStarted = Promise.withResolvers<undefined>()
    const releaseUpdate = Promise.withResolvers<undefined>()
    let first = true
    harness.onSessionUpdate = async () => {
      if (!first) return
      first = false
      updateStarted.resolve(undefined)
      await releaseUpdate.promise
    }
    const prompt = harness.client.prompt({ sessionId, prompt: [{ type: 'text', text: 'hold output' }] })
    await vi.waitFor(() => { expect(harness!.adapter.requests).toHaveLength(1) })

    for (let index = 0; index < 64; index += 1) {
      agent.session.append('tool/call', {
        turn: 1,
        step: 1,
        callId: ToolCallId(`blocked-${index}`),
        name: 'read',
        arguments: `{"path":"file-${index}"}`,
      })
    }
    expect(cancel).not.toHaveBeenCalled()
    agent.session.append('tool/call', {
      turn: 1,
      step: 1,
      callId: ToolCallId('blocked-overflow'),
      name: 'read',
      arguments: '{"path":"overflow"}',
    })

    try {
      expect(cancel).toHaveBeenCalledTimes(1)
    } finally {
      releaseUpdate.resolve(undefined)
      if (cancel.mock.calls.length === 0) agent.cancel({ kind: 'user' })
    }
    await updateStarted.promise
    await expect(prompt).rejects.toThrow(/live update backlog exceeded/)
    expect(harness.updates.filter(update => update.sessionUpdate === 'tool_call')).toHaveLength(64)
  })

  it('rejects a queued live tool payload whose full title, call id, and arguments exceed the byte cap', async () => {
    harness = await makeBridgeHarness({ script: ['hang'] })
    await harness.ctx.plugin(ApprovalService)
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    const { sessionId } = await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
    const agent = harness.ctx.agents.get(SessionId(sessionId))!
    const cancel = vi.spyOn(agent, 'cancel')
    const updateStarted = Promise.withResolvers<undefined>()
    const releaseUpdate = Promise.withResolvers<undefined>()
    harness.onSessionUpdate = async () => {
      updateStarted.resolve(undefined)
      await releaseUpdate.promise
    }
    const prompt = harness.client.prompt({ sessionId, prompt: [{ type: 'text', text: 'hold output' }] })
    await vi.waitFor(() => { expect(harness!.adapter.requests).toHaveLength(1) })
    const callId = ToolCallId(`call-${'c'.repeat(20_000)}`)
    const toolName = `tool-${'t'.repeat(20_000)}`

    agent.session.append('tool/call', {
      turn: 1,
      step: 1,
      callId: ToolCallId('active-small-call'),
      name: 'read',
      arguments: '{"path":"small"}',
    })
    await updateStarted.promise

    agent.session.append('tool/call', {
      turn: 1,
      step: 1,
      callId,
      name: toolName,
      arguments: JSON.stringify({ payload: 'a'.repeat(16_500) }),
    })
    const approval = harness.ctx.approval.request({ agent, toolName, callId })

    try {
      expect(cancel).toHaveBeenCalledTimes(1)
    } finally {
      releaseUpdate.resolve(undefined)
    }
    await expect(approval).resolves.toBe('unavailable')
    await expect(prompt).rejects.toThrow(/live update backlog exceeded/)
    expect(harness.updates).toEqual([{
      sessionUpdate: 'tool_call',
      toolCallId: 'active-small-call',
      title: 'read',
      kind: 'read',
      rawInput: { path: 'small' },
      status: 'in_progress',
    }])
    expect(harness.permissionRequests).toEqual([])
  })

  // `session/update` is a JSON-RPC notification, so a client-side handler
  // failure never reaches the bridge; this pins that the prompt still settles
  // normally with such a client. The bridge's own write-failure guard is
  // transport-level and documented untestable at `notify`.
  it('settles the prompt normally when the client rejects update notifications', async () => {
    harness = await makeBridgeHarness({ script: [textResponse('answer')] })
    await harness.client.initialize({ protocolVersion: PROTOCOL_VERSION, clientCapabilities: {} })
    const { sessionId } = await harness.client.newSession({ cwd: process.cwd(), mcpServers: [] })
    harness.onSessionUpdateError = () => {}
    await expect(harness.client.prompt({ sessionId, prompt: [{ type: 'text', text: 'go' }] }))
      .resolves.toEqual({ stopReason: 'end_turn' })
  })
})

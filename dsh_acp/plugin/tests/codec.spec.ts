import { describe, expect, it } from 'vitest'
import type { TurnEndReason } from '@deepseek-ai/dsh-session'
import { turnEndToStopReason } from '../src/acp-codec.ts'

declare module '@deepseek-ai/dsh-session/types' {
  interface TurnEndReasonMap {
    'acp-test-extension': { kind: 'acp-test-extension' }
  }
}

describe('ACP codec', () => {
  it.each([
    [{ kind: 'completed' }, 'end_turn'],
    [{ kind: 'max-tokens' }, 'max_tokens'],
    [{ kind: 'aborted', reason: { kind: 'user' } }, 'cancelled'],
    [{ kind: 'interrupted' }, 'cancelled'],
    [{ kind: 'blocked' }, 'refusal'],
  ] satisfies Array<[TurnEndReason, string]>)('maps %o to %s', (reason, expected) => {
    expect(turnEndToStopReason(reason)).toBe(expected)
  })

  it('fails closed for a merge-extended turn ending', () => {
    expect(() => turnEndToStopReason({ kind: 'acp-test-extension' }))
      .toThrow('unsupported ACP turn ending: acp-test-extension')
  })

  it('fails closed for a failed turn ending', () => {
    expect(() => turnEndToStopReason({ kind: 'error', error: { message: 'failed', code: 'UNKNOWN' } }))
      .toThrow('unsupported ACP turn ending: error')
  })
})

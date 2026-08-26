/**
 * Pure translation between the harness lifecycle and the automation-only ACP wire.
 * @module @myagents/dsh-acp-host/acp-codec
 * @license Portions derived from DeepSeek Harness under the MIT License; see
 * THIRD_PARTY_NOTICES.md.
 */

import type { StopReason } from '@agentclientprotocol/sdk'
import type { TurnEndReason } from '@deepseek-ai/dsh-session'

/**
 * Map a harness turn ending to ACP's terminal reason vocabulary.
 * @param reason - harness turn outcome.
 * @returns the closest legal ACP stop reason.
 * @throws TypeError when the turn failed or carries an unknown extension reason.
 */
export function turnEndToStopReason(reason: TurnEndReason): StopReason {
  switch (reason.kind) {
    case 'completed':
      return 'end_turn'
    case 'max-tokens':
      return 'max_tokens'
    case 'aborted':
    case 'interrupted':
      return 'cancelled'
    case 'blocked':
      return 'refusal'
    case 'error':
      throw new TypeError('unsupported ACP turn ending: error')
    default:
      throw new TypeError(`unsupported ACP turn ending: ${String((reason as { kind?: unknown }).kind)}`)
  }
}

# ADR-0020：能力受限的上下文预算、压缩与恢复

- 状态：Accepted
- 日期：2026-08-28
- Owner：Bryant Yang
- 里程碑：M7.2

## 1. 背景

有状态 adapter 已通过 timeline cursor 只接收增量，首次 bootstrap 也只发送最近
`history_limit` 条；这些机制避免重复投递，却不会缩小 agent 原生 session 内已经
累积的上下文。直接模型 Host 的 `NativeAgentRuntime._messages` 会持续增长，第三方
ACP、RPC 与 app-server 是否支持安全 compaction 则各不相同。

把“截断首次历史”称为压缩会掩盖真实风险；在通用 Orchestrator 中按 agent 名调用
未经验证的 reset、session/new 或厂商私有命令，又会破坏单写者、权限和 no-replay。

## 2. 决策

### 2.1 capability-first，不猜 transport

上下文治理使用两个可选 adapter capability：

- `context_snapshot()` 只返回策略名、是否可压缩、内部消息数与字符数；
- `compact_context(ContextPolicy)` 在 adapter 自己的单写锁内完成压缩，返回有界
  统计和私有摘要。

通用层不按 agent/provider 名分支。未声明 capability 的有状态 adapter 在
`/context` 中显示“由 transport 管理”，`/compact` 必须 fail-closed；无状态 adapter
显示每轮有界快照。首版只有 myagents 自有、无工具的直接模型 Host 声明真实压缩，
agent Host 和第三方 worker 不因本功能获得 reset、shell、文件或网络能力。

### 2.2 安全边界与自动策略

默认 `ContextPolicy` 在内部上下文达到 48,000 字符时，于下一次回合或 workflow
阶段边界自动压缩，保留最近 4 条内部消息；用户可用 `/compact [@agent]` 在空闲
边界手动触发。字符是诚实可获得的预算单位，不伪装成跨 provider 精确 token。

压缩调用直接使用原生 model provider，不经过 Agent 工具循环，不携带 tools，且
`/yolo` 不影响它。摘要只在 provider 返回权威成功 terminal 后替换旧消息；
`content_filter`、长度截断等非成功 reason 与拒绝、空摘要、超时、断流、取消或
未知终态一样，均保留原 runtime context，不自动重试，也不把摘要调用误记成用户
prompt 已提交。

checkpoint 摘要必须覆盖当前 cursor 边界前的完整 runtime context；近期 4 条内部
消息同时以原文留在 live runtime 中提升回答质量。不能只摘要被淘汰的旧前缀，否则
进程重启后从 checkpoint 边界恢复会静默丢失近期原文所承载的事实。
若待压缩全文超过 `source_characters` 安全输入上限，必须在模型调用前拒绝并保持
原 context/cursor，不得截掉中段后仍生成绑定完整 boundary 的 checkpoint。

自动压缩发生在 target 的 Orchestrator delivery lock 内、真正用户 prompt 之前。
压缩失败时本轮在 pre-prompt 边界失败，cursor 不推进；其他 adapter 和 room 不受
影响。

### 2.3 timeline 与 durable checkpoint 分离

完整 `timeline.jsonl` 保持 append-only，压缩不得删除、改写或把摘要追加成聊天
消息。成功摘要作为 `ContextCheckpoint` 写入房间私有 `state.json`：

- `boundary_seq`：该摘要对应的持久 cursor 边界；
- `generation`：按 agent 单调递增；
- `created_at`、source/retained message 统计；
- 最多 8,000 字符的 summary，继续受房间 0700 / state 0600 保护。

先完成 runtime 压缩、再原子写 checkpoint。若持久化失败，内存与恢复事实已分叉，
整个房间立即 fail-closed，直到重启检查存储；不得继续派发或假称压缩成功。

fresh runtime 恢复时，从 checkpoint `boundary_seq` 继续选择 timeline 增量，并把
摘要作为明确标记的背景注入一次。摘要不是新用户指令；正常 restored native
session 不重复注入。post-submit poisoned session 仍以当前 no-replay cursor 为准，
只可注入较旧摘要，绝不回放 checkpoint 后的已提交消息。

HostBackend 切换必须清除 Host checkpoint：摘要不能跨 model/agent backend 复用。
第三方 execution profile 切换仍按各自 ADR 建立 fresh session，不因本 ADR 获得
跨 profile 摘要或重放。

### 2.4 TUI

- `/context` 显示当前 room 的自动阈值、每个注册 target 的上下文所有权、可用计数
  和 checkpoint 代次，不显示摘要正文、prompt、凭据或 chain-of-thought；
- `/compact` 默认 target 为 `@host`，也接受一个显式 `@agent`；
- 运行中或有排队 command 时拒绝手动压缩；
- 成功只显示前后字符数，并明确“完整聊天时间线未删除”；
- 自动压缩作为安全 status/info 进入当前活动过程，但摘要正文不进入 events。

## 3. 不选择的方案

- 不把 `history_limit`、cursor 或 heartbeat 合并称为 compaction；
- 不在 Orchestrator 中按 agent 名调用私有 `/compact`、reset 或 session/new；
- 不删除或重写 timeline；
- 不跨 backend、transport 或 execution profile 复用摘要；
- 不自动 fallback 到新协议或重放已提交 prompt；
- 不为统一显示估算虚假的 token/费用；
- 不让摘要器获得工具、权限处理器或 `/yolo` 扩权。

## 4. 验收

1. `ContextPolicy`、命令边界、计数与 checkpoint schema 严格校验。
2. fake OpenAI-compatible provider 证明摘要请求无 tools、权威 terminal 后才替换。
3. 摘要源超限、空摘要、非成功/未知终态和 checkpoint 写失败分别保持旧上下文
   或使房间 fail-closed。
4. 自动压缩只在 delivery lock 内、用户 prompt 前触发，失败不推进 cursor。
5. fresh runtime 从覆盖完整边界的持久摘要 + checkpoint 后增量恢复，旧全文不被
   重新 bootstrap，压缩时保留的近期事实也不会因重启丢失。
6. 未获证 ACP/RPC/app-server adapter 显示 transport-managed，手动压缩明确拒绝。
7. HostBackend 切换清除旧 checkpoint；损坏 checkpoint 打开房间时 fail loudly。
8. `/context`、`/compact` 不进入 timeline；运行/排队边界、反馈文案和补全有 TUI
   自动验收。
9. 既有路由、权限、session restore、讨论、协作、workflow 和 no-replay 门禁通过。

## 5. 后果与后续

myagents 首次拥有真实、可见、可恢复的上下文压缩，而不是只做传输减量。以后某个
第三方 adapter 只有在原生 compact/rotate、权限、恢复和 no-replay 均有独立证据
后，才能实现同一 capability。token/费用、结构化 handoff artifact 与真实长会话
质量压测属于后续工作，不能由字符预算或 fake provider 冒充完成。

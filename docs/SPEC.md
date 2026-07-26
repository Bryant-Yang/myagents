# myagents 行为规格（SPEC）

<!-- harness:behaviour-evidence=canonical-source -->

> 作者：Bryant Yang　最近更新：2026-07-26
>
> 本文是关键用户行为与独立证据的唯一事实源。工程边界见
> [`../HARNESS.md`](../HARNESS.md)。

## 0. 阶段

| 阶段 | 状态 | 交付 |
| --- | --- | --- |
| M0 | 完成 | Textual TUI、显式 @ 路由、host、JSONL adapter |
| M1 | 完成 | 通用 ACP client/adapter 与 fake server contract tests |
| M2 | 完成 | Kimi ACP 接入、增量 history、权限 UI、统一回收、真实 E2E |
| M3 | 规划 | history/session 恢复与受控外部入口 |

## 1. 角色

- **用户**：在统一 TUI 点名 agent、批准/拒绝权限并验收结果。
- **worker agent**：通过 ACP 或 JSONL adapter 接收任务并流式返回事件。
- **host**：仅在无显式 @ 时进行语义路由，或被 `@host` 点名做总结/仲裁。
- **Orchestrator**：唯一消息中心，维护共享 history、投递顺序与生命周期。

## 2. 关键行为用例

### UC-ROUTE-001 显式路由与并发扇出

- **角色 / 触发**：用户输入一个或多个已注册的 `@agent`。
- **前置条件**：agent 已在 `AGENT_SPECS` 注册。
- **主流程**：消息写入 history；显式 targets 去重；不同 agent 并发处理；回复
  回到共享时间线。
- **异常分支**：未知 mention 被忽略；单个 agent 失败编码为 error/history，
  不拖垮其他 target。
- **验收**：显式 @ 绕过 host；多个 target 各执行一次；并发消息使用正确快照。
- **独立证据来源**：接入 Harness 前已存在的 `tests/test_basic.py`
  路由、fan-out、快照和失败回退测试。
- **人工验收边界**：TUI 中多段流式回复的可读性与交错体验由用户验收。
- **里程碑**：M0。

### UC-ACP-001 有状态增量上下文

- **角色 / 触发**：用户连续多次 `@` 同一个 ACP agent。
- **前置条件**：adapter 声明 `stateful_session=True`。
- **主流程**：首次仅 bootstrap 最近 `history_limit` 条；后续只发 cursor 后的新
  消息并过滤 agent 自己回复；成功后推进 cursor。
- **异常分支**：失败不推进 cursor；同 agent 并发 dispatch 在 delivery lock 内
  串行；不同 agent 仍可并行。
- **验收**：不重复旧消息、不丢跨 agent 消息、不乱序、首次发送有界。
- **独立证据来源**：`tests/test_phase2.py` fake stateful adapter；Codex 在实现后
  独立构造过并发复现，确认修复前第二轮重复 first、修复后回归通过。
- **人工验收边界**：长会话 token/内存增长和 compaction 策略尚未验收。
- **里程碑**：M2。

### UC-PERM-001 权限请求与选择

- **角色 / 触发**：Kimi ACP 在工具调用前发 `session/request_permission`。
- **前置条件**：TUI 已注入 agent-aware 异步权限处理器。
- **主流程**：弹窗显示来源 agent、工具标题和 options；用户选择；client 只接受
  本次 options 内非空 optionId。
- **异常分支**：无处理器、取消、异常、None、空或未知 optionId 全部 cancelled。
- **验收**：等待用户时 read loop 不阻塞；合法 allow/reject 能回传；畸形结果
  fail-closed。
- **独立证据来源**：`tests/fake_acp_server.py` + ACP/Phase 2 contract tests；
  2026-07-26 在临时目录用真实 Kimi Code CLI 0.29.1 + Textual TUI 验证实际
  `allow_once / allow_always / reject_once` options，选择 `allow_once` 后探针
  文件内容正确；严格 optionId 成员校验落地后再次真实复验通过。
- **人工验收边界**：任何真实工具写入与 `auto` 模式必须由用户逐次授权；自动化
  测试不能代替风险接受。
- **里程碑**：M2。

### UC-LIFE-001 取消与退出回收

- **角色 / 触发**：用户取消流、退出 TUI，或 agent 超时/断线。
- **前置条件**：子进程使用独立进程组，adapter 拥有 session。
- **主流程**：发送 cancel；等待原 prompt 结束；TUI 先收尾权限 Future，再
  `aclose`；SIGTERM 超时后 SIGKILL。
- **异常分支**：cancel 不确认则连接作废并在下轮重建；断线使 pending request
  立即失败。
- **验收**：下一轮不与旧 prompt 重叠；fake 子孙进程和 ACP server 均无残留。
- **独立证据来源**：接入 Harness 前已有的 basic/ACP/Phase 2 生命周期测试；
  2026-07-26 两次真实 Kimi TUI E2E 退出后 `pgrep -fl '^kimi acp$'` 为空。
- **人工验收边界**：真实 Kimi cancel 响应时延和长任务中的不可逆工具副作用尚未
  验证。
- **里程碑**：M1–M2。

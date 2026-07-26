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
| M2.5 | 完成 | 持久房间（timeline/state/seq cursor）、ACP session 恢复、owner lease、持久确认时序 |
| M3 | 完成 | 内部 command bus、私有 Unix 控制 socket、MCP stdio 外部入口（五个 `myagents_*` 工具） |

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

### UC-ROOM-001 持久房间与重启恢复

- **角色 / 触发**：用户在同一 workdir 重启 TUI，或第二个进程试图打开同一
  房间。
- **前置条件**：房间状态位于
  `${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/<room_id>`，不污染
  workdir。
- **主流程**：timeline 以单调 `seq` append-only 落盘；`state.json` 保存
  每个 stateful agent 的 seq cursor 与 ACP `session_id`（原子写）；TUI
  启动按 seq 恢复显示历史；persistent Orchestrator 构造末尾获取
  owner.lock（flock 非阻塞）单写者 lease，`aclose()` 释放。
- **异常分支**：timeline/state 损坏、schema 不支持、workdir 不匹配、
  cursor 越过 timeline、agent entry 缺字段或非法值，全部 fail loudly，
  不静默覆盖或 bootstrap 成默认值；lease 冲突抛 `RoomBusyError`（含
  持有者 PID 提示），进程异常退出由 OS 释放 flock，stale owner.lock
  不阻塞下次获取；用户消息 append 失败则 TUI 不显示该消息并显示持久化
  错误；`aclose()` 后新 dispatch 与排队中的 delivery 一律抛
  `OrchestratorClosedError`，不再写 timeline、不再 start/load/prompt。
- **验收**：重启后 seq 续接、历史按序恢复显示且 timeline 不重复 append；
  同进程/跨进程第二 writer 被拒；checkpoint/append 写失败不假提交。
- **独立证据来源**：`tests/test_storage.py`（timeline/state/lease/权限/
  fail loudly）、`tests/test_m25.py`（重启恢复、lease 冲突与跨进程、
  TUI 恢复显示、持久确认时序、close 排队保护）。
- **人工验收边界**：真实桌面环境中两个 TUI 实例竞争同一房间的交互体验
  尚未验收。
- **里程碑**：M2.5。

### UC-ACP-002 ACP session 恢复与原子 checkpoint

- **角色 / 触发**：TUI 重启或 adapter 进程重建后，用户再次 `@` 同一个
  ACP agent。
- **前置条件**：`state.json` 已记录该 agent 的 seq cursor 与
  `session_id`；adapter 暴露 `stream_prepared` restore 原语。
- **主流程**：`session/load` 命中记录的 session（restored）或复用活跃
  session 时保留 cursor，继续纯增量；cursor/session_id 的 checkpoint 在
  prompt 前一次性原子落盘，成功后才更新内存 cursor；prompt 成功后先持久
  推进 `delivered_upto` 再更新内存。
- **异常分支**：load 失败或 agent 不声明 loadSession 时回退新 session，
  cursor 归零并按 `history_limit` 有界 bootstrap（不沿用旧 cursor 静默
  跳过 history），实际新 session id 落盘；checkpoint 写失败穿透
  dispatch（零 prompt、adapter reset、内存/磁盘不假提交），不伪装成
  agent 调用失败；prompt 失败 cursor 不推进，下轮补发。
- **验收**：load 成功后 prompt 不含旧内容；回退路径 bootstrap 有界；
  同一房间只有一个 writer 持有 session。
- **独立证据来源**：`tests/test_m25.py` 真实 `AcpAdapter` + fake ACP
  server 的 load 成功/失败/无 capability/重连/checkpoint 失败/prompt
  失败用例；`tests/test_acp.py` 的 `stream_prepared` restore 契约测试。
- **真实验收证据**：`scripts/e2e-m3-real.py` 已于 2026-07-26 用真实
  `kimi acp` 完成两次独立 TUI/ACP 生命周期，第二轮复用同一 session id，
  timeline 无重复且退出无残留。长会话 compaction 后的 restore 行为
  仍未验收。
- **里程碑**：M2.5。

### UC-CTRL-001 外部命令注入（command bus + MCP stdio）

- **角色 / 触发**：本机另一个 coding agent（MCP host）经
  `myagents_mcp.py` stdio bridge 向运行中的 TUI 房间提交消息、查询
  状态、读取时间线。
- **前置条件**：TUI 已运行并持有目标房间；bridge 以显式 `--workdir`
  启动（必填），`--state-root` 仅测试注入；bridge 只读 endpoint 发现
  并每次实际连接验证，绝不创建 Orchestrator/TUI、不获取 lease、不直写
  timeline、不自动拉起 TUI。
- **主流程**：`myagents_get_room` 返回房间/workdir/PID/agent transport；
  `myagents_send_message` 提交消息并立即返回 `command_id`（可选
  `request_id` 幂等去重）；命令按 FIFO 经同一个 CommandBus 进入同一个
  `Orchestrator.dispatch`，与 TUI 输入共享单写者；
  `myagents_get_command` / `myagents_wait_command`（≤30s，超时不取消）
  观察 queued → running → terminal；agent 最终回复落盘后进入共享时间线
  并实时显示在 TUI；`myagents_read_timeline` 按
  `after_seq`/`limit`（≤200）分页，不越界、不丢 seq。
- **异常分支**：TUI 未运行、endpoint stale/损坏/权限非 0600/房间不
  匹配，全部 fail closed 并返回含启动命令的可操作 tool error；未知
  method、非法字段、超上限请求返回稳定错误码（`INVALID_REQUEST` /
  `INVALID_PARAMS` / `METHOD_NOT_FOUND` / `NOT_FOUND` / `CAPACITY` /
  `BUS_CLOSED`），不泄漏 traceback；外部消息触发工具权限时仍在 TUI
  弹窗由用户决策，bridge 无 `auto` 放行入口；control/validation 错误
  不会使 MCP server 崩溃。
- **验收**：五方法语义正确；同房间并发 submit 按 FIFO 顺序执行且相同
  `request_id` 不重复执行；官方 Python MCP SDK（`mcp>=1.27,<2`）经
  stdio 完成 initialize/list_tools/call_tool，stdout 无非协议输出；
  stdin EOF 后 bridge 进程 rc=0 干净退出；TUI 正常退出后无 socket、
  endpoint、MCP 或 agent 残留进程。
- **独立证据来源**：`tests/test_m3_bus.py`（FIFO、request_id 永久幂等、
  容量硬上限、close 兜底 cancelled）、`tests/test_m3_control.py`
  （五方法 roundtrip、0600/close 清理、stale 恢复与活跃不抢占、稳定
  错误码、start 失败无泄漏、AF_UNIX 超长路径可操作错误、Textual pilot
  外部命令实时可见、外部权限仍由 TUI 决策）、`tests/test_m3_mcp.py`（官方 SDK stdio
  list/call、注解、structuredContent、tool error 映射、干净退出）。
- **真实验收证据**：`scripts/e2e-m3-real.py` 已于 2026-07-26 通过：
  临时工作区内两次启动 TUI + 真实 `kimi acp`，由独立 MCP client 各
  提交一条无工具消息；第二轮恢复同一 session id，4 条 timeline 记录
  连续且 command_id 关联正确，退出后无 endpoint/socket/agent 残留。
  该脚本调用真实模型，不放进默认快速 gate。
- **里程碑**：M3。

### UC-CTRL-002 控制 socket 安全与生命周期

- **角色 / 触发**：TUI 启动/退出，或第二个进程试图占用同一房间的
  控制 socket。
- **前置条件**：socket/endpoint 固定在房间目录内，均为 0600；
  `endpoint.json` 经同目录临时文件 + `os.replace` + fsync 原子写，
  只登记 protocol version、PID、room_id、规范化 workdir、socket path。
- **主流程**：启动时若 socket 已存在，先实际连接验证——有监听者抛
  `ControlBusyError`（不抢占、不删除属主文件），确认无监听者才清理
  stale socket/endpoint；关闭时停止接收、取消全部 client handler
  （含等待中的 `command.wait`），只按 inode 删除自己创建的
  socket/endpoint，无 task/文件残留。
- **异常分支**：`start()` 中途失败（如 chmod 失败）不泄漏仍在监听的
  server，清理后可干净重试；macOS AF_UNIX 路径超约 104 字节时报
  可操作错误（提示缩短 `XDG_STATE_HOME`），不创建任何文件；连接验证
  遇非 stale 类 errno 一律 fail closed。
- **验收**：活跃 socket 任何情况下不被第二 server unlink/抢占；
  endpoint/socket 0600 与原子性成立；close 后文件与 task 无残留；
  client 发现路径不创建状态目录。
- **独立证据来源**：`tests/test_m3_control.py` 对应用例；
  `tests/test_m25.py` 的 lease 跨进程冲突用例（owner.lock 兜底）。
- **人工验收边界**：两个真实桌面 TUI 实例竞争同一房间的交互体验尚未
  验收。
- **里程碑**：M3。

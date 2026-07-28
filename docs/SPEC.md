# myagents 行为规格（SPEC）

<!-- harness:behaviour-evidence=canonical-source -->

> 作者：Bryant Yang　最近更新：2026-07-28
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
| M3 | 完成 | 内部 command bus、私有 Unix 控制 socket、MCP stdio 外部入口 |
| M3.1 | 完成 | 持久执行事件、heartbeat、权限/工具上下文、精确取消（七个 MCP 工具） |
| M4 | 完成 | Codex app-server 长连接、thread/turn 事件、取消、审批与 JSONL fallback |
| M4.2 | 完成 | 同一项目独立会话、默认房间兼容、TUI 安全切换与外部 selector |

## 1. 角色

- **用户**：在统一 TUI 点名 agent、批准/拒绝权限并验收结果。
- **worker agent**：通过 ACP 或 JSONL adapter 接收任务并流式返回事件。
- **host**：无显式 @ 时用一次调用直接回答或进行 worker 路由；也可被
  `@host` 点名做总结/仲裁。
- **Orchestrator**：唯一消息中心，维护共享 history、投递顺序与生命周期。

## 2. 关键行为用例

### UC-ROUTE-001 显式路由与并发扇出

- **角色 / 触发**：用户输入一个或多个已注册的 `@agent`。
- **前置条件**：agent 已在 `AGENT_SPECS` 注册。
- **主流程**：消息写入 history；显式 targets 去重；不同 agent 并发处理；回复
  回到共享时间线。
- **异常分支**：未知 mention 被忽略；单个 agent 失败编码为 error/history，
  不拖垮其他 target；所有 target 收尾后，Orchestrator 用 `DispatchOutcome`
  汇总失败，CommandBus 将本轮标为 `failed`。失败前已有 partial 时，时间线
  必须明确标注“调用失败”，不能伪装成正常答复。
- **验收**：显式 @ 绕过 host；多个 target 各执行一次；并发消息使用正确快照；
  任一 worker 失败时 command 失败，但其他 worker 仍完成。
- **独立证据来源**：`tests/test_basic.py` 的路由、fan-out、快照和失败回退测试；
  `tests/test_m3_bus.py` 的 fan-out 失败终态测试。
- **人工验收边界**：TUI 中多段流式回复的可读性与交错体验由用户验收。
- **里程碑**：M0。

### UC-ROUTE-002 host 明确委托

- **角色 / 触发**：用户未使用 `@`，但最新消息需要 worker 执行，例如“你构思
  一个小游戏，让 Kimi 实现”。
- **主流程**：host 在一次纯路由调用中选择 target，并为每个 target 生成完整、
  可执行的 `task`；Orchestrator 将该 task 作为一次性 assignment 注入 worker
  本轮 prompt，不写入共享 timeline。
- **异常分支**：host 返回旧格式或遗漏 task 时，Orchestrator 用原始用户请求
  生成执行型回退指令；worker 仍需自行完成构思、实现与验证，不能等待 host
  再发方案。路由阶段禁止工具、文件、命令、网络和 skill；底层若仍产生安全
  status/tool/permission 事件，实时进入 TUI 与执行日志，不得静默吞掉。
- **验收**：JSONL 与 stateful/ACP 两条投递路径均收到明确 assignment；非法
  target 的 task 被过滤；host 直接回答路径不产生 assignment。
- **独立证据来源**：`tests/test_basic.py` 的明确任务解析、stateful 透传、
  旧格式回退、路由 prompt 和 host 进度事件透传测试。
- **人工验收边界**：真实模型对开放式任务的完成质量仍由用户验收；自动化测试
  只证明委托没有在协议层丢失。
- **里程碑**：M4。

### UC-ACP-001 有状态增量上下文

- **角色 / 触发**：用户连续多次 `@` 同一个 ACP agent。
- **前置条件**：adapter 声明 `stateful_session=True`。
- **主流程**：首次仅 bootstrap 最近 `history_limit` 条；后续只发 cursor 后的新
  消息并过滤 agent 自己回复；成功后推进 cursor。
- **异常分支**：prompt 提交前或服务端明确拒绝的失败不推进 cursor；提交后
  静默超时等结果不确定失败先持久化 no-replay cursor，再公开失败。同 agent
  并发 dispatch 在 delivery lock 内串行；不同 agent 仍可并行。
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
  fail-closed；权限请求到结果发回期间暂停 agent inactivity timeout，用户等待
  超过该 timeout 也不会误判 agent 卡死。
- **独立证据来源**：`tests/fake_acp_server.py` + ACP/Phase 2 contract tests；
  `tests/test_acp.py::test_permission_wait_pauses_inactivity_timeout`；
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
- **异常分支**：不在等待人工权限时，prompt 连续 120 秒无任何 ACP 事件才自动
  cancel；该轮已提交，先建立 no-replay 边界再公开失败。cancel 不确认则连接
  作废并在下轮重建；断线使 pending request 立即失败。
- **验收**：人工权限等待不会触发 inactivity timeout；下一轮不与旧 prompt
  重叠；fake 子孙进程和 ACP server 均无残留。
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
  持有者 PID 提示）；CLI 将该冲突转换为无 traceback 的短提示，并给出关闭
  旧 TUI 或使用新 `--session` 的命令。进程异常退出由 OS 释放 flock，stale owner.lock
  不阻塞下次获取；用户消息 append 失败则 TUI 不显示该消息并显示持久化
  错误；`aclose()` 后新 dispatch 与排队中的 delivery 一律抛
  `OrchestratorClosedError`，不再写 timeline、不再 start/load/prompt。
- **验收**：重启后 seq 续接、历史按序恢复显示且 timeline 不重复 append；
  同进程/跨进程第二 writer 被拒；checkpoint/append 写失败不假提交。
- **独立证据来源**：`tests/test_storage.py`（timeline/state/lease/权限/
  fail loudly）、`tests/test_m25.py`（重启恢复、lease 冲突与跨进程、
  TUI 恢复显示、持久确认时序、close 排队保护）、`tests/test_basic.py`
  （CLI 冲突提示）。
- **人工验收边界**：真实桌面环境中两个 TUI 实例竞争同一房间的交互体验
  尚未验收。
- **里程碑**：M2.5。

### UC-SESSION-001 同一项目独立会话

- **角色 / 触发**：用户在空闲 TUI 按 `Ctrl+N`、精确输入 `/new`，或启动时
  传入 `--session NAME`。
- **前置条件**：当前没有 queued/running command 或权限等待；会话名合法且
  新建时尚不存在。
- **主流程**：房间身份由 `(规范化 workdir, session_name)` 决定；当前 App
  返回 `NewSessionRequest` 并按标准顺序关闭 control server、CommandBus、
  adapters 和 lease；同一进程随后构造新 App。新会话拥有独立 timeline、
  events、cursor 与原生 agent session。`--session NAME` 可恢复同名会话。
- **兼容分支**：`default` 沿用旧 `sha256(workdir)[:16]` room_id；旧
  `state.json` 缺少 `session_name` 时只在默认房间兼容读取。
- **异常分支**：非法名称、命名房间 state 不匹配、同名新建、活跃任务或权限
  等待全部明确拒绝；不删除、不覆盖旧会话，也不在同一 Orchestrator 上清空
  history/cursor。除精确 `/new` 外的普通文本（如 `hi`）不做本地语义猜测。
- **外部入口**：ControlClient 与 MCP bridge 使用同一可选 session selector；
  `room.get` 返回实际 `session_name`，default client 不误连命名会话。
- **验收**：默认 room_id 与现有状态兼容；命名会话互相隔离且同名可恢复；
  `Ctrl+N` 与精确 `/new` 只产生安全切换请求，`/new` 不持久化、不路由；
  顶层循环在旧 App 退出后重建目标会话。
- **自动化证据**：`tests/test_storage.py`（身份/隔离/名称与 state 校验）、
  `tests/test_m25.py`（Ctrl+N、同名/active 拒绝）、`tests/test_basic.py`
  （顶层重建循环/CLI）、`tests/test_m3_control.py` 与 `tests/test_m3_mcp.py`
  （命名会话发现与 bridge selector）。
- **独立兼容证据**：实现前已存在的默认房间
  `c249bb5584b575a2` 仍由规范化 workdir 的旧哈希得到；现有 timeline/state
  未迁移、未重写。
- **人工验收边界**：真实 TUI 中 Ctrl+N 后的视觉连续性、命名体验和真实
  Kimi/Codex 新 session 由用户验收；会话列表、删除和重命名不在本用例。
- **里程碑**：M4.2。

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
  agent 调用失败；prompt 提交前或明确拒绝的失败 cursor 不推进、下轮补发；
  prompt 已提交后的 inactivity timeout 属于结果不确定，先持久化 no-replay
  cursor，再返回失败。
- **验收**：load 成功后 prompt 不含旧内容；回退路径 bootstrap 有界；
  同一房间只有一个 writer 持有 session。
- **独立证据来源**：`tests/test_m25.py` 真实 `AcpAdapter` + fake ACP
  server 的 load 成功/失败/无 capability/重连/checkpoint 失败/prompt
  失败和 inactivity no-replay 用例；`tests/test_acp.py` 的
  `stream_prepared` restore 与 timeout 契约测试。
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
- **验收**：七方法语义正确；同房间并发 submit 按 FIFO 顺序执行且相同
  `request_id` 不重复执行；官方 Python MCP SDK（`mcp>=1.27,<2`）经
  stdio 完成 initialize/list_tools/call_tool，stdout 无非协议输出；
  stdin EOF 后 bridge 进程 rc=0 干净退出；TUI 正常退出后无 socket、
  endpoint、MCP 或 agent 残留进程。
- **独立证据来源**：`tests/test_m3_bus.py`（FIFO、request_id 永久幂等、
  容量硬上限、close 兜底 cancelled）、`tests/test_m3_control.py`
  （七方法 roundtrip、0600/close 清理、stale 恢复与活跃不抢占、稳定
  错误码、start 失败无泄漏、AF_UNIX 超长路径可操作错误、Textual pilot
  外部命令实时可见、外部权限仍由 TUI 决策）、`tests/test_m3_mcp.py`（官方 SDK stdio
  list/call、注解、structuredContent、tool error 映射、干净退出）。
- **真实验收证据**：`scripts/e2e-m3-real.py` 已于 2026-07-26 通过：
  临时工作区内两次启动 TUI + 真实 `kimi acp`，由独立 MCP client 各
  提交一条无工具消息；第二轮恢复同一 session id，4 条 timeline 记录
  连续且 command_id 关联正确，退出后无 endpoint/socket/agent 残留。
  该脚本调用真实模型，不放进默认快速 gate。
- **里程碑**：M3。

### UC-OBS-001 执行进度、权限上下文与重启证据

- **角色 / 触发**：TUI 或外部 MCP host 提交一个长任务。
- **主流程**：queued/running、ACP 阶段、工具、权限、partial、heartbeat
  与 terminal 事件写入独立 `events.jsonl`；heartbeat 显示当前阶段和累计
  静默时长，TUI 对同一 command 原位更新；工具事件按
  `(command_id, agent, tool_call_id)` 聚合，缺 ID 时使用脱敏标题作为可见
  identity；同一状态不重复转发或持久化，状态迁移与其他安全摘要实时显示。
  固定任务区显示 command 状态、耗时和每个 agent 的当前阶段。
- **安全分支**：thought 正文不显示；常见凭据字段隐藏；执行事件不进入
  agent 对话 history。
- **高频分支**：adapter 继承初始 tool title/command 并压缩重复 update；
  CommandBus 再做 producer-independent 防御性去重。重复 update 仍刷新
  activity 时钟，但 activity-only 事件不进入 UI/events，不会误触发
  heartbeat/inactivity；TUI 只在可见状态变化时
  重绘同一逻辑工具项，命令详情默认折叠、由 `/details` 切换，命令终态后清理
  内存索引。活跃工具使用 15 分钟独立 watchdog，普通分析仍使用 120 秒阈值。
- **重启分支**：最后事件非 terminal 的 command 显示为上次中断及最后状态。
- **完成语义**：`completed` 只表示本轮调用正常结束，不等同于用户任务验收；
  TUI 显示“本轮响应结束”，不显示“agent 完成”。fan-out 任一 worker
  失败时本轮是 `failed`，即使其他 worker 已经正常回复；固定任务区保留各
  agent 终态并把混合成功/失败显示为“部分完成”。
- **验收**：`events.read` 可分页读取；heartbeat 累计且界面不刷行；200 条相同
  tool update 在 ACP 层压成“创建/进行中/完成”三个状态，在防御性 bus
  fixture 中只转发并持久化一次，TUI 只占一个逻辑项且只重绘状态迁移；旧房间
  安全补建；损坏日志 fail loudly。
- **证据**：`tests/test_storage.py`、`tests/test_acp.py`、
  `tests/test_phase2.py`、`tests/test_basic.py`、`tests/test_tui_status.py`、
  `tests/test_m3_bus.py`、
  `tests/test_m3_control.py`、`tests/test_m3_mcp.py`。
- **真实回放证据**：2026-07-28 命名房间 `99a294ef32695cef` 的同一 command
  在约半分钟内产生 4,886 条完全相同的 `工具调用：in_progress`；最小 fake
  trace 在修复前稳定复现 ACP 200 条、CommandBus 200 条、TUI 51 行，修复后
  分层回归通过。历史事件保持 append-only，不回写清理。
- **里程碑**：M3.1。

### UC-CANCEL-001 精确取消

- **角色 / 触发**：用户按 `Ctrl+X`，或外部 host 调用 cancel。
- **主流程**：queued 直接取消；running 仅取消该 dispatch；terminal 幂等。
- **可见状态**：`cancel_requested` 仅显示“正在取消”，在 CommandBus 确认
  terminal 前保持运行态与工具状态。
- **异常分支**：取消确认超过 30 秒返回明确失败，不把未知状态报成成功。
- **验收**：取消后 CommandBus worker 继续执行下一条；无 ghost task。
- **证据**：`tests/test_m3_bus.py`、`tests/test_m3_control.py`、
  `tests/test_m3_mcp.py`。
- **里程碑**：M3.1。

### UC-CODEX-001 Codex 原生长连接

- **角色 / 触发**：用户连续点名 `@codex`，或多次发送无 mention 消息给
  Codex host。
- **前置条件**：本机 Codex CLI 支持 `codex app-server`；adapter 独占其进程。
- **主流程**：一次 initialize 后建立 thread；worker 的连续 turn 复用同一
  app-server PID/thread 并按 cursor 接收增量；host 复用暖进程、每次建立干净
  的 ephemeral thread，以免 transcript 快照在原生历史中重复，同时不把内部
  路由提示词写入 Codex 历史。
- **配置边界**：不发送 model、effort、config、collaboration mode、plugin 或
  MCP 覆盖，不修改 Codex 全局配置；仅传 cwd、既有 sandbox、approval policy，
  以及 host 专用的 `ephemeral: true`。worker 为
  `workspace-write + on-request`，越界操作进入统一权限 UI；host 为
  `read-only + never`。
- **事件分支**：agent message delta 流式进入正文；command/file/MCP item
  进入脱敏 tool/status；approval 进入统一权限 UI且无处理器默认拒绝；
  reasoning 正文不显示；`turn/completed` 后才产生 done。
- **取消/故障分支**：取消发 `turn/interrupt` 并等待 terminal；断线使 pending
  立即失败；initialize/thread prepare 失败可退到 JSONL，一旦发送
  `turn/start` 就禁止自动重放；已发送后的结果不确定、terminal 失败/中断和
  用户取消都先形成持久 no-replay 边界，再公开失败/取消。服务端明确拒绝或
  确认未发送的失败保持可重试；即使旧 thread 无法恢复，新 thread 也不
  bootstrap 已投递的旧输入。明确接受的 turn 在任何正文/工具/权限 event
  sink 回调前先持久化该边界。host 的 JSONL fallback 使用
  `codex exec --ephemeral`，故障路径也不写入 Codex 历史。
- **验收**：fake server 证明 worker 两轮同 PID/thread、host 两个 ephemeral
  thread 共用同一 PID、无 `jsonrpc` header、默认配置字段未被覆盖、取消不
  重叠、断线失败、close 无残留。
- **独立证据来源**：`tests/fake_codex_app_server.py` +
  `tests/test_codex_app_server.py`。
- **真实验收证据**：2026-07-27 在临时目录执行两组真实 Codex 探针，均未
  覆盖 model/effort/plugin/MCP：直接 adapter 两轮分别 18.0s/4.6s，
  同 PID/thread；真实 Orchestrator 连续两次 `@codex` 分别得到
  `SELF_ONE`/`SELF_TWO`，同 PID/thread；两组 `aclose()` 后对应 PID 均
  不存在。app-server 为实验接口，真实模型探针不进入普通快速 gate。
- **里程碑**：M4。

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

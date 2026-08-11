# myagents 行为规格（SPEC）

<!-- harness:behaviour-evidence=canonical-source -->

> 作者：Bryant Yang　最近更新：2026-08-11
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
| M3.1 | 完成 | 持久执行事件、heartbeat、权限/工具上下文、精确取消与 steering（八个 MCP 工具） |
| M4 | 完成 | Codex app-server 长连接、thread/turn 事件、取消、审批与 JSONL fallback |
| M4.2 | 完成 | 同一项目独立会话、默认房间兼容、TUI 安全切换与外部 selector |
| M4.3 | 完成 | macOS 剪贴板 PNG、会话私有附件、草稿引用与真实视觉验收 |
| M4.4 | 完成 | Kimi ACP-first、prepare-only 只读 JSONL fallback 与 no-replay |
| M4.5 | 完成 | OpenCode ACP-first、ask-by-default 权限收口与只读 JSONL fallback |
| M4.6 | 完成 | Qwen Code `qwen --acp` 接入、TUI 点名与 ACP-only 安全边界 |
| M4.7 | 完成 | 多项目会话目录、后台执行、资源 gate、未读通知与图片短引用 |
| M5.1 | 完成 | `/discuss` 指定成员、1–3 轮有界讨论与终局 moderator |
| M5 | 完成 | review → 单 writer 修改 → 独立复核、一次修复上限与阶段边界 steering |

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

### UC-DISCUSS-001 指定成员的有界多智能体讨论

- **角色 / 触发**：用户在 TUI 或 MCP 提交
  `/discuss @agent1 @agent2 [@agent3] [--rounds 1..3]
  [--moderator host|agent] -- 主题`；MCP/API 也可在首行参数后换行提供主题。
- **前置条件**：参与者是 `AGENT_SPECS` 中 2–3 个不同 worker；默认两轮、
  默认 moderator 为 `host`，主持人不能同时参会。
- **输入边界**：主题必须非空且不超过 3000 个字符；参数和主题在任何 timeline
  写入前完成确定性校验。
- **主流程**：整个讨论只占一个 CommandBus command 并写一条 user timeline；
  第一轮参与者并发独立提案，后续轮等待前轮全部收尾后并发交叉评议，最后
  moderator 单次仲裁。轮次目标由普通代码生成 assignment，不额外伪造 user
  消息、不递归 `dispatch`；全部回复共享同一 `command_id`。
- **上下文**：stateful agent 按现有 cursor 契约接收增量，所以下一轮可见其他
  参与者上一轮回复而不重发自己的原生 session 内容；JSONL agent 使用本轮开始
  时的有界 timeline 快照。
- **安全边界**：讨论 assignment 明确只产出观点，不读取/修改文件、不执行命令、
  不调用工具、Skill 或子 agent；transport 的 fail-closed 权限不因讨论放宽。
  最大调用数固定为 `3×3+1=10`，agent 无权动态加轮或拉人。
- **异常分支**：参与者失败不取消同轮其他人，但失败者退出后续轮次，避免重放
  不确定投递；存活者少于两个时跳过剩余交叉轮，moderator 仍总结已有证据。
  所有 worker/moderator 失败都保留在 `DispatchOutcome`，CommandBus 最终为
  `failed`。取消任一轮即取消整个 command，不再进入下一轮或主持总结。
- **验收**：parser 在 timeline 写入前拒绝缺主题、人数/轮数越界、重复/未知成员
  和主持人冲突；同轮并发、跨轮可见、唯一 user、终局主持及失败收口均有 fake
  contract；TUI 精确 `/discuss` 显示用法，带参数命令与 MCP 共用 CommandBus。
- **独立证据来源**：`tests/test_discussion.py`、
  `tests/test_tui_completion.py`、`tests/test_m3_bus.py` 既有 command 终态契约；
  ADR-0008 的授权真实模型回放不进入默认 gate。
- **真实验收证据**：2026-08-08 恢复命名房间
  `collab-smoke-20260808-7f3c`，经真实 MCP bridge 提交 Kimi/OpenCode 两轮
  `/discuss`，再由 Codex host 仲裁。command
  `ce688171-43e4-401d-81a5-ee1ff9cc5d8f` 为 completed；新增 timeline
  `seq=6..11` 恰含一条 user、两条 Kimi、两条 OpenCode 和一条 host，全部绑定
  同一 command id。两名 ACP worker 沿用原 session id，cursor 从 1 推进到 8；
  第二轮双方均明确回应对方首轮观点，证明跨轮共享生效。事件无 tool/permission，
  退出后无 endpoint/socket/agent 进程残留。
- **人工验收边界**：开放式讨论质量、不同模型观点的实际独立性和成本由用户验收；
  自动化只证明调度、上下文、边界与终态。
- **里程碑**：M5.1。

### UC-WORKFLOW-001 有界里程碑工作流与阶段边界 steering

- **状态**：已实现并通过自动化与授权真实模型验收；`/workflow` 可从 TUI/MCP
  提交，`/steer` 可从 TUI、control socket 或 MCP 提交。
- **角色 / 触发**：用户提交
  `/workflow --reviewer @agent|@host --implementer @worker
  [--verifier @agent|@host] -- 任务目标`。verifier 缺省为 reviewer；implementer
  不得兼任 review/verify。
- **工作区 fixed point**：只接受无 merge/rebase、HEAD/branch 可解析且
  index/tracked/untracked 全干净的 Git 工作区；校验在 timeline 前完成。启动
  HEAD OID 与覆盖 tracked diff、index、untracked 路径/内容的指纹构成 baseline。
  read-only 阶段前后指纹必须一致；implement/repair 后 branch、HEAD、index 不得
  改变，并生成 candidate 指纹供 verifier 锁定。外部 writer 在写阶段的归因无法
  自动证明，属于人工验收边界。
- **主流程**：一个 CommandBus command 和一条 user timeline 内依次执行
  review → implement → verify。verify 首次返回 `changes_requested` 时只允许
  原 implementer 修复一次，再由原 verifier 复核一次，最后 host 单次总结；
  最大六次模型调用，角色、阶段和终止条件均由普通代码决定。
- **阶段结果**：review/implement/verify/repair 以最后一行严格
  `MYAGENTS_WORKFLOW {...}` 信封报告有界状态。信封必须且只能包含必填的
  stage/status/findings，前缀只出现一次且位于最后非空行，UTF-8 最多 4096
  字节；findings 是最多 32 个不重复 id 的数组，单个 id 最多 64 字符。未知字段、
  字段缺失、类型错误、stage/status 不匹配、非法 JSON、blocked 或最终非 pass
  均 fail-closed；host 不能覆盖 verifier 终态。
- **写入与权限**：review/verify/final 使用 adapter `read_only` execution mode，
  implement/repair 使用 `workspace_write`；只有 implementer 是 writer，两个写
  阶段严格串行。ACP `read_only` 不 load/复用曾运行非只读轮次的 session，进入
  时重建进程与 fresh session，防止继承 `allow_always`。OpenCode 只读轮次在
  runtime 层 hard deny 未知/有副作用工具并只允许安全读取，避免权限 cancelled
  后零正文结束；切回普通轮次再次重建并恢复 TUI ask。Kimi/OpenCode implement
  若进入只读 JSONL fallback，阶段必须 blocked，不得以分析回复冒充已修改。
- **steering**：只有正在 review、implement 或 repair 且后面仍有验证阶段的
  workflow 可接收最多 5 条、单条 1000 字符且累计 4000 字符的补充指令；
  verify/reverify/final 已开始时拒绝。steering 作为 execution event 持久化，
  不进入 timeline，只注入尚未开始的阶段；不能改角色/阶段/权限、增加修复次数
  或修改已提交 prompt。立即停止仍使用 cancel。
- **失败/取消**：transport、持久化、host 或阶段信封失败均保留已完成证据并使
  command failed；post-submit 不确定失败不重试、不换 agent；取消后不进入任何
  后续阶段。进程重启只显示上次中断，不自动续跑写阶段。
- **独立证据来源**：`tests/test_workflow.py` 覆盖 parser/严格信封、固定状态机、
  Git index flags/tracked/untracked fixed point、read-only 漂移、主失败与 final 失败
  聚合、六阶段取消、Git 进程组回收、baseline events 持久化与 active owner 清理；
  `tests/test_acp.py` 覆盖跨轮 `allow_always` 授权隔离；Kimi/OpenCode/Codex
  adapter contract 覆盖 execution mode 与写阶段 fallback
  fail-closed；`tests/test_m3_bus.py` 覆盖 steering 先持久化后提交及落盘失败回滚；
  control/MCP/TUI completion/status tests 覆盖同一 owner 和阶段状态 UX。
- **真实验收证据**：2026-08-09 运行 `scripts/e2e-m5-real.py`，command
  `3e0c46d3-d658-49b8-8ed8-74ab9436ed2b` 在临时 Git repo 以 Kimi 为
  reviewer/verifier、Codex 为唯一 implementer，时间线依次为 user、Kimi、Codex、
  Kimi、host。baseline HEAD `57ce8875e1d4e4126e8afbf8d9a8e9af4c263639`
  和 main branch 未改变，index 为空；最终 diff 增加 `subtract` 与正/负两个
  unittest，完整 3 个 unittest 通过，workflow verdict 为 completed。
  同日以 `--verifier opencode` 运行同一探针，command
  `a9d65665-d712-4f7e-932b-8cb6ad80e576` 依次完成 Kimi review、Codex
  implement、OpenCode verify 和 host final；baseline
  `c9f7627e4d635d5e083746c14b282487db395662`、main、index 均未漂移，最终
  subtract diff 与 3 个 unittest 通过。此前零正文复现为 5/5；修复后同形状
  OpenCode 只读探针 5/5 返回合法 verify/pass 信封。
- **事实源**：ADR-0009、`workflow.py`、`workspace/git_adapter.py`。
- **里程碑**：M5。

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

### UC-ACP-003 Qwen Code ACP-only 接入

- **角色 / 触发**：用户在聊天室输入 `@qwen`，或把 Qwen 选为讨论/workflow
  中满足既有角色约束的 worker。
- **主流程**：`AGENT_SPECS` 以 `AgentSpec("qwen", "acp", AcpQwenAdapter)`
  注册；普通轮启动 `qwen --acp --approval-mode default`，workflow `read_only`
  启动 `qwen --acp --approval-mode plan`，profile 切换时重建进程、丢弃 resume id
  并 `session/new`。两者复用通用 ACP 增量 cursor、权限 UI、取消和进程组回收
  契约。TUI 的 `@` 补全、`/agents` 与启动状态动态展示 `@qwen(ACP)`，编排器
  不增加任何按 qwen 名称分支。
- **安全边界**：默认 `permission="deny"`，无处理器或非法 option 一律
  cancelled。普通轮显式覆盖用户 native TUI 可能保存的 auto/yolo mode；只读轮
  使用 Qwen 上游定义的 plan profile，在 runtime 层阻断文件修改和有副作用命令，
  不能只取消新的 ACP permission request。当前不启用 `stream-json` headless
  fallback：上游输入协议仍标记为未完成，且本项目尚无 Qwen fallback 的
  prepare-only/no-replay 与工具白名单证据；ACP prepare 失败直接失败，不跨协议重放。
- **验收**：红线强制 Qwen ACP-only 注册、deny 默认值和 default/plan 命令；fake
  contract 验证 profile 切换会 fresh 进程/session、无 fallback、TUI 补全/状态和
  普通 mention 路由；完整 Harness 不调用真实模型。
- **真实协议证据**：本地上游源码 0.21.8 的 CLI 配置与 ACP bridge 均以
  `qwen --acp` 作为一等入口。2026-08-09 本机安装版 0.21.7 已完成认证并接入
  LM Studio 的 `google/gemma-4-e4b`；生产 `AcpQwenAdapter` 在隔离临时目录建立
  session `031bddd9-5dbc-43eb-ac82-09b0abe8668b`，先产生
  `delivery_committed`，再流式返回 `QWEN_ACP_OK.`，以 `end_turn` 正常结束，
  `aclose()` 后无 Qwen ACP 子进程残留。独立 `--approval-mode plan` 写入探针建立
  session `a6b3b519-ac4c-49e2-9a48-d06c79deaad2`，模型发起的 `WriteFile` 在执行前
  以 failed 结束，目标文件未产生；切换 `default` 的同类探针也未写入。
- **人工验收边界**：真实 session、模型正文与正常回收已验收；真实权限 options、
  跨进程 `session/load` 恢复和取消时延尚未验收。默认 gate 不调用外部模型。
- **里程碑**：M4.6。

### UC-PERM-001 权限请求与选择

- **角色 / 触发**：Kimi/OpenCode ACP 在受控工具调用前发
  `session/request_permission`。
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
  2026-08-08 OpenCode 1.18.14 临时目录 probe 证明 runtime ask policy
  将无害 Bash 请求转为相同三类 options，默认 deny 后 `end_turn`。
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

### UC-SESSION-001 多项目会话与后台执行

- **角色 / 触发**：用户按 `Ctrl+N` 或精确输入 `/new` 新建会话；按 `Ctrl+O`
  或精确输入 `/sessions` 浏览、搜索和切换会话；启动时仍可传
  `--session NAME` 选择稳定 selector。
- **主流程**：房间身份仍由 `(规范化 workdir, session_name)` 决定；自动生成
  的 session_name 与可变展示标题分离。新会话初始标题为“新会话”，第一条
  用户消息经本地确定性清理后生成标题。SessionManager 为每个已加载会话持有
  独立 store/lease/orchestrator/bus/control/runtime；切换只替换当前可见历史和
  草稿，原会话 command、权限等待与持久化继续在后台运行。
- **目录与整理**：选择器默认列当前项目，Tab 切换全部项目并按 workdir 分组；
  搜索标题、最后用户消息、项目名和路径。标题可重命名且不改变 room_id。
  永久删除要求完整输入标题，且拒绝当前、运行中或等待权限的会话。
- **资源与通知**：最多三个会话实际 dispatch；第四个显示等待资源并可取消。
  后台完成/失败产生非阻塞通知和未读标记。后台空闲 runtime 十分钟后关闭，
  当前、pending 与权限等待 runtime 保留。草稿按 room_id 在进程内隔离。
- **兼容分支**：`default` 沿用旧 `sha256(workdir)[:16]` room_id；旧
  `state.json` 缺少 `session_name` 时只在默认房间兼容读取；缺展示标题时以
  session_name 展示。目录名与 state 身份必须一致，损坏项 fail loudly。
- **异常分支**：切换/新建不取消后台任务；删除不使用 glob 或用户标题拼路径；
  超过全局容量只排队，不偷开第四个执行槽。除精确本地命令外的普通文本不做
  本地语义猜测。
- **外部入口**：ControlClient 与 MCP bridge 使用同一可选 session selector；
  `room.get` 返回实际 `session_name`，default client 不误连命名会话。
- **验收**：默认 room_id 与旧历史兼容；新建/切换/搜索/重命名/确认删除可用；
  两个会话同时运行时事件、权限、草稿、时间线和未读不串房；全局容量、取消、
  空闲回收与关闭无残留。`/new`、`/sessions` 不持久化、不路由。
- **自动化证据**：`tests/test_storage.py`（身份/隔离/名称与 state 校验）、
  `tests/test_session_catalog.py`、`tests/test_session_manager.py`、
  `tests/test_session_tui.py`（目录、生命周期和 Textual pilot）、
  `tests/test_m25.py`（本地命令/no-replay）、`tests/test_basic.py`（CLI），以及
  M3 control/MCP 的命名 selector 回归。
- **真实验收证据**：2026-08-11 在 `/tmp` 隔离工作目录并发运行两个真实会话：
  Qwen ACP 房间 `2201750c4c38b770` / command
  `263614a2-3753-4ae2-b428-b58db844b73e` 返回 `SESSION_QWEN_OK`；OpenCode ACP
  房间 `fefb82a9f52ee98e` / command
  `a971f090-4221-4dda-a42f-2fb98b42efa2` 返回 `SESSION_OPENCODE_OK`。两条 command
  均 completed，切回 Qwen 后 OpenCode 会话保持未读，双方历史无 marker 串房；
  临时工作目录自动删除，退出后无 owned Qwen/OpenCode ACP 进程残留。
- **独立兼容证据**：实现前已存在的默认房间
  `c249bb5584b575a2` 仍由规范化 workdir 的旧哈希得到；现有 timeline/state
  未迁移、未重写。
- **人工验收边界**：真实终端中的窄窗口排版、桌面通知样式和十分钟真实等待
  仍由人工验收；自动化用注入时钟证明回收条件。
- **里程碑**：M4.7。

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

### UC-HYBRID-001 Kimi ACP-first 受限降级

- **角色 / 触发**：用户向 Kimi worker 派发任务，但新 ACP 连接在
  start、initialize 或 session prepare 阶段失败。
- **主流程**：`AcpKimiAdapter` 在同一 writer lock 内保持统一
  `AgentAdapter` interface；prepare 失败时先以
  `fallback:jsonl:kimi` 调用 checkpoint hook，再使用
  `kimi -p --output-format stream-json --agent-file <readonly-profile>` 执行
  一次降级轮，并公开 `prepare-only` fallback info 事件。
- **权限边界**：降级 profile 只暴露 `Read` / `Grep` / `Glob`，
  `subagents: []`；禁止写入、命令、网络、Skill、子 agent 和 MCP。
  JSONL 可返回分析或明确阻塞，不得伪装已完成变更。
- **禁止分支**：活跃 session 冲突、checkpoint 失败、prompt 明确拒绝
  以及 prompt 提交后的取消/超时/断线/不确定结果均不调用
  fallback。后一类先建立 no-replay cursor 再公开失败。
- **恢复**：下一轮不对 `fallback:jsonl:*` 伪 id 执行
  `session/load`，直接新建 ACP session；以 fresh/unrestored 语义
  归零 cursor 并有界 bootstrap。两种 transport 不并发写同一 session。
- **验收**：生产注册仍由 `AcpKimiAdapter` 构造，可见 transport
  为 `acp+jsonl`；prepare 失败正好调用一次受限 JSONL；
  prompt 拒绝、post-submit 静默与写入后断线均零 fallback 调用；首个可见
  ACP 输出前已提交内部 `delivery_committed`。
- **独立证据来源**：Kimi CLI 0.34.0 本机 `--help` 和官方 reference
  确认 `-p` / `stream-json` / `--agent-file`；`tests/fake_acp_server.py`
  作为独立 protocol fixture，`tests/test_kimi_hybrid.py` 验证工具白名单、
  prepare-only、伪 checkpoint 恢复、交付提交时序和 no-replay 反例。
- **人工验收边界**：真实 Kimi 模型的降级回复质量与 CLI 升级后
  schema 漂移需受限临时目录探针；默认 gate 不调用外部 agent。
- **里程碑**：M4.4。

### UC-HYBRID-002 OpenCode ACP-first 权限收口与受限降级

- **角色 / 触发**：用户向 OpenCode worker 派发任务；正常走 ACP，或新 ACP
  连接在 start、initialize、session prepare 阶段失败。
- **ACP 主流程**：生产由 `AcpOpenCodeAdapter` 构造，运行
  `opencode acp`，保持持久 session、增量 cursor、权限 UI、取消与恢复。
  OpenCode runtime 权限将未知工具、写入、命令、网络、Skill、子 agent、
  MCP 与外部目录收口为 ask；read/search/lsp/todo allow。无 TUI handler
  仍由通用 client cancelled。workflow `read_only` 使用独立 runtime policy：
  unknown/risky deny、read/search/lsp/todo allow；profile 前后切换重建 ACP
  进程和 fresh session，普通轮次恢复 ask。
- **降级流程**：prepare 失败时以 `fallback:jsonl:opencode` checkpoint，
  然后运行 `opencode --pure run --format json --agent
  myagents-readonly-fallback`。环境禁用项目配置、Claude 兼容层、自动升级，
  inline profile 与 runtime permission 双重限制为
  `read` / `glob` / `grep` / `list`。
- **禁止分支**：活跃 session 冲突、checkpoint 失败、prompt 明确拒绝以及
  prompt 提交后的取消、超时、断线或结果不确定均零 fallback；后一类先形成
  no-replay cursor。下一轮不 load fallback 伪 id，直接新建 ACP session。
- **验收**：生产 transport 为 `acp+jsonl`；真实 ACP v1 capability 包含
  loadSession、image、list/resume；无害 Bash 请求进入 ACP permission，默认
  deny 后正常结束；ACP `read_only` 的 Bash 在 runtime hard deny 后仍产出严格
  workflow 信封，切回普通轮恢复 TUI ask；只读 JSONL profile 无 `--auto`，
  写入/命令/网络工具均 deny。
- **独立证据来源**：本机 OpenCode 1.18.14 CLI/capability/permission wire
  probe；OpenCode 官方 Permissions/Agents/Config 文档；
  `tests/fake_acp_server.py` 独立协议 fixture；
  `tests/test_opencode_hybrid.py` 固定 registry、普通/只读 ACP policy、profile
  切换进程隔离、硬拒绝后继续输出、隔离 fallback、prepare-only 与 post-submit
  no-replay。
- **真实验收证据**：2026-08-08 在临时目录通过生产 adapter 完成 ACP
  `end_turn`、关闭重连后的 `session/load`（同 session id，输出
  `OPENCODE_ACP_ONE` / `OPENCODE_ACP_RESUMED`）、Bash 权限默认 deny，
  以及强制 prepare 失败后的真实只读 JSONL；写入探针文件未产生，结束后
  无残留 OpenCode 进程。ACP export 独立确认两轮 token 已持久化。
  2026-08-09 本机 OpenCode 1.18.15 额外复现 workflow 只读轮次因 Bash ask
  被 cancelled 而 5/5 零正文；改为 runtime hard deny 后，同形状探针 5/5
  产出合法 verify/pass，并完成 command
  `a9d65665-d712-4f7e-932b-8cb6ad80e576` 的真实多角色 workflow。
- **人工验收边界**：真实模型、session/load、权限 options 与 fallback 工具集
  已完成本版本受限复验；真实 cancel 时延、长会话 compaction，以及 OpenCode
  升级后的 `OPENCODE_PERMISSION` seam 仍需单独复验。默认 gate 不调用外部 agent。
- **里程碑**：M4.5。

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
- **验收**：八方法语义正确；同房间并发 submit 按 FIFO 顺序执行且相同
  `request_id` 不重复执行；官方 Python MCP SDK（`mcp>=1.27,<2`）经
  stdio 完成 initialize/list_tools/call_tool，stdout 无非协议输出；
  stdin EOF 后 bridge 进程 rc=0 干净退出；TUI 正常退出后无 socket、
  endpoint、MCP 或 agent 残留进程。
- **独立证据来源**：`tests/test_m3_bus.py`（FIFO、request_id 永久幂等、
  容量硬上限、close 兜底 cancelled）、`tests/test_m3_control.py`
  （八方法 roundtrip、0600/close 清理、stale 恢复与活跃不抢占、稳定
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

### UC-IMAGE-001 剪贴板图片附件

- **角色 / 触发**：macOS TUI 用户在草稿中按 `Ctrl+V` 且 Textual 文本剪贴板
  为空，或精确输入 `/paste-image`。
- **主流程**：系统读取 macOS 剪贴板的 PNG 表示，保存到当前命名房间的
  `attachments/img-NNNN.png`，再在光标位置插入 `[图片 N]`；不自动提交，
  用户可继续补充文字和 `@agent`。消息正常进入共享 timeline，被调用 agent
  收到图片读取提示；Kimi ACP 使用原生 `image` block，Codex app-server
  使用原生 `localImage`。无 mention 时仍按既有规则交给 host 决定。
- **安全边界**：附件目录为 0700、文件为 0600；文件必须是普通 PNG，单张
  不超过 20 MiB；不把二进制写入 timeline/events，也不写入目标工作区。
  只有解析后仍位于当前房间 `attachments/` 的路径才能升级为协议图片，
  防止手写标记读取任意本地文件。旧 `[图片附件：绝对路径]` 只在同一信任根
  内兼容读取。
- **异常分支**：非 macOS、剪贴板没有 PNG、读取超时、格式错误或大小超限时，
  显示可操作错误，不改变草稿、不提交消息、不留下不完整 PNG。
- **生命周期**：附件随房间保留；当前版本不提供预览、删除、跨机器传输或
  JPEG/HEIC 转换。
- **验收**：成功粘贴只改变草稿；路径位于当前 room；权限和格式正确；失败
  不残留文件。`Ctrl+V` 有文本时仍使用 Textual 原文本粘贴。
- **证据**：`tests/test_clipboard_image.py` 的注入式 macOS fixture；
  真实剪贴板脚本探针验证无 PNG 时返回稳定错误。自动测试不声称证明 agent
  的视觉理解质量。
- **真实验收证据**：2026-08-09 在房间 `e279f938f34e3475` 粘贴真实 macOS
  剪贴板 PNG；附件目录为 0700、文件为 0600。command
  `5415080a-1c55-4eba-b063-dd5b7203d877` 的 timeline `seq=5..7` 恰含一条
  user 和两条 agent 结果；Kimi 通过 ACP 原生 `image` 输入准确识别黑白飞碟
  画面。OpenCode 所选 `deepseek-v4-flash-free` 不支持视觉并明确拒绝，属于
  模型能力限制，不影响已验证的附件保存、fan-out 与 Kimi 视觉链路。
- **里程碑**：M4.3。

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

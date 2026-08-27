# myagents 行为规格（SPEC）

<!-- harness:behaviour-evidence=canonical-source -->

> 作者：Bryant Yang　最近更新：2026-08-27
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
| M4.8 | 完成 | 聊天主线降噪、每任务活动摘要卡与逐卡键盘展开 |
| M4.9 | 完成 | WorkBuddy ACP-only 接入、按需有界认证与读写 profile 隔离 |
| M4.10 | 完成 | Agent 被动就绪探测、原子派发门与 `/agents` 设置体验 |
| M4.11 | 完成 | Pi 原生 RPC、启动 attestation、逐次权限 bridge 与三 profile 隔离 |
| M4.12 | 完成 | DSH ACP-only adapter、标准 bundle + stock `myagents` profile、权限隔离与真实恢复验收 |
| M4.13 | 完成 | `/yolo` 会话级自动批准、allow-once 约束、持续风险提示与 read-only 硬边界 |
| M4.14 | 完成 | myagents 原生模型 provider/runtime、无工具 Host、LM Studio 与 OpenAI-compatible 接入 |
| M5.1 | 完成 | 自然语言或 `/discuss` 进入 1–3 轮有界讨论与终局 moderator |
| M5 | 完成 | review → 单 writer 修改 → 独立复核、一次修复上限与阶段边界 steering |
| M6 | 完成 | 自然语言指定会话级角色、跨任务持续、房间隔离与状态可见 |
| M7 | 完成 | 自然语言有序协作、2–4 步串行接力与失败/取消收口 |

## 1. 角色

- **用户**：在统一 TUI 点名 agent、批准/拒绝权限并验收结果。
- **worker agent**：通过 ACP、原生 RPC、app-server 或 JSONL adapter 接收任务并
  流式返回事件。
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

### UC-COLLAB-001 自然语言有序协作

- **角色 / 触发**：用户不用新命令，直接表达有依赖的多 agent 接力，例如
  “先让 Kimi 调研，再让 OpenCode 基于结果设计，最后让 Qwen 复核并交付”。
  明确多 mention 且含先后线索时，普通代码只决定是否进入计划提取；无 mention
  时复用既有 host 单次路由。`一起、分别、各自` 等无依赖表达仍是并发 fan-out。
- **计划合同**：host 只能返回直接回答、既有并行路由或 `CollaborationPlan` 三者
  之一。计划固定 2–4 步、至少两个不同且 ready 的 worker；每步只有固定 agent
  与非空 assignment。同一 worker 可在后续步骤再次出现，但 host 不得增员、漏掉
  显式 mention、改变权限或添加重试。模型输出未知 agent、空任务、单 agent 循环、
  缺步、超限或畸形 JSON 一律 fail-closed。
- **主流程**：整个计划只占一个 CommandBus command 和一条 user timeline。
  Orchestrator 用普通代码严格串行推进；每步回复按同一 command id 写回共享
  timeline，下一步从最新 timeline 读取前序真实结果。assignment 不伪装成 user
  消息。最后一步必须直接产生面向用户的最终交付，不自动追加 host 总结。
- **角色组合**：若同一句显式消息还明确设置/取消会话角色，host 在同一次提取中
  返回受 mention 闭集约束的 `role_changes`；Orchestrator 仍按既有先持久化、后
  更新内存和派发的原子入口处理，角色不能改变计划成员、步骤或权限。
- **失败 / 取消**：任一步失败使 command failed 并立即停止，后续 adapter 不得
  启动；取消当前 command 会取消当前步骤并阻止后续步骤。不得自动重试、换人、
  跳步或用成功的前序回复覆盖失败。调用上限为一次 host 识别加最多四个 worker。
- **权限与生命周期**：每一步沿用该 adapter 普通轮 execution mode、权限 UI、
  session/cursor、no-replay 与进程回收契约；协作不扩权、不跨协议重放，也不让
  agent 递归调用 Orchestrator。显式计划在 timeline 前原子要求全部参与者和 host
  ready；无 mention 时 host 只能从 ready worker 候选中选人。
- **验收**：`tests/test_collaboration.py` 使用纯内存 fake host/adapter 覆盖计划
  边界、host 三路解析、显式 mention 闭集、普通 fan-out 兼容、严格顺序、前序
  结果可见、唯一 user、统一 command id、同轮角色、失败和 CommandBus 取消。
  默认门禁不启动任何真实 agent。
- **人工验收边界**：真实模型能否稳定把开放式自然语言拆成高质量步骤、成本与
  最终内容质量由用户验收；自动化只证明计划边界、执行时序和安全终态。
- **事实源**：ADR-0013、`collaboration.py`、`host.py`、`orchestrator.py`。
- **里程碑**：M7。

### UC-ROLE-001 会话级自然语言角色

- **角色 / 触发**：用户在普通消息或 `/discuss` 主题中自然表达“让 Qwen 在这个
  会话担任产品研究员”“让 OpenCode 当反方”，或明确说“不再担任这个角色”；
  也可用精确本地命令 `/roles` 查看、`/roles clear` 清空当前会话全部角色。
- **主流程**：普通代码先固定实际 targets；host 仅在该闭集内提取 `set/clear`。
  无 mention 的消息复用既有 host 路由调用；显式 mention 和讨论只在存在角色线索
  时调用纯语义提取。合法变化先原子写入当前 room 的 `state.json.session_roles`，
  再更新内存并派发。Orchestrator 在每轮 assignment 前注入权威的角色状态：
  有角色时注入角色要求，无角色时明确不得沿用先前临时角色。
  `/roles` 只读取 Orchestrator 快照；`/roles clear` 复用同一原子状态入口，不写
  timeline、不路由、不调用模型。
- **生命周期**：角色在当前命名会话的后续命令中持续，切换/新建会话不继承，
  重开同一会话会恢复；明确取消后删除。不提供永久 Profile、全局角色库或跨会话
  自动继承。
- **安全分支**：模型输出只能引用固定 targets；角色只改变工作视角，不能改变
  speaker、adapter/session/cursor、runtime、工具权限、讨论参与者/轮数/moderator
  或 workflow 的固定职责和 execution mode。
- **异常分支**：未知 target、空值、畸形 JSON 与超限值不生效；提取失败保留已有
  角色并继续原任务。状态写失败则内存不更新、worker 不派发，错误向上传播。
  清除后的无角色声明必须进入后续 assignment，避免有状态 runtime 沿用旧角色。
  当前 room 存在 queued/running command 时 `/roles clear` 拒绝，避免阶段间漂移；
  未注册的 `/roles ...` 参数形式仍作为普通消息。
- **验收**：设置、跨任务沿用、自然语言取消、讨论跨轮注入、两个命名会话隔离、
  同会话重启恢复、损坏状态 fail loudly；活动卡和任务区显示
  `agent · 角色（本会话）`，后续 tool/done 更新不丢角色；查看/清空命令不进入
  timeline、不调用 host，清空失败不假提交。
- **独立证据来源**：`tests/test_session_roles.py`、`tests/test_discussion.py`、
  `tests/test_tui_completion.py`、`tests/test_tui_activity.py` 与
  `tests/test_tui_status.py`。
- **人工验收边界**：模型对模糊角色表述的提取质量由用户验收；永久角色模板、
  跨会话复制和可视化角色编辑器不在本里程碑。
- **里程碑**：M6。

### UC-DISCUSS-001 自然语言与精确命令的有界多智能体讨论

- **角色 / 触发**：用户可自然表达“@A @B 你们讨论两轮……”“让合适的两个
  agent 辩论并由 host 总结”，或在 TUI/MCP 提交
  `/discuss @agent1 @agent2 [@agent3] [--rounds 1..3]
  [--moderator host|agent] -- 主题`。显式自然语言 mention 固定参与者闭集；无
  mention 时 host 在现有单次路由中只能从 ready worker 选择。自然语言默认两轮、
  moderator 固定为 host；精确命令仍可选择未参会 moderator。
- **前置条件**：参与者是 `AGENT_SPECS` 中 2–3 个不同 worker；默认两轮、
  默认 moderator 为 `host`，主持人不能同时参会。
- **输入边界**：主题必须非空且不超过 3000 个字符；参数和主题在任何 timeline
  写入前完成确定性校验。自然语言中的“讨论、辩论、互相点评、交叉评议”是讨论
  候选；“一起分析、分别回答、各自建议”以及“不要/不用讨论”等明确否定表达
  保持单轮 fan-out；明确的跨 agent“先……再……”优先作为有序协作。自然语言
  轮数使用 `讨论三轮：主题`、`讨论三轮，主题`、`讨论三轮 关于主题` 等有边界
  写法；无边界的“三轮融资/一轮明月”等保留为主题并使用默认两轮。需要无歧义
  的机器输入时使用 `/discuss --rounds N`。
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
  和主持人冲突；自然语言显式点名与无点名 host 路由均复用同一状态机；同轮并发、
  跨轮可见、唯一 user、终局主持及失败收口均有 fake contract；TUI 会显示
  “已识别为讨论 · N 人 × N 轮 · host 总结”，精确 `/discuss` 仍显示用法。
- **独立证据来源**：`tests/test_discussion.py`、
  `tests/test_tui_completion.py`、`tests/test_m3_bus.py` 既有 command 终态契约；
  ADR-0008 的授权真实模型回放不进入默认 gate。
- **真实验收证据**：2026-08-08 恢复命名房间
  `collab-smoke-20260808-7f3c`，经真实 MCP bridge 提交 Kimi/OpenCode 两轮
  `/discuss`，再由当时的 Codex host 仲裁。command
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

### UC-PERM-002 显式自动批准模式

- **角色 / 触发**：用户信任当前 workspace 与输入，在活动会话输入 `/yolo`。
- **主流程**：只有当前 room 启用自动决策器；当 ACP、Pi RPC
  bridge 或 Codex app-server 产生权限请求时，从当次 options 中选择
  非空 `kind=allow_once`，不显示权限弹窗。再次输入 `/yolo` 关闭；切换会话
  时其他 room 保持各自的进程内状态。
- **可见性**：窗口标题与固定任务区持续显示 `YOLO` 危险模式；自动
  权限结果仍按 transport 镜像规则进入 events/活动卡。
- **安全边界**：默认仍逐次询问；模式不写 room state、退出后失效、不选 `allow_always`、
  不伪造 optionId。没有 `allow_once` 时拒绝。workflow `read_only`、各 adapter
  runtime/profile 硬拒绝和只读 JSONL fallback 不受影响。该模式不是 OS
  sandbox，不用于无人值守处理不受信输入。
- **验收**：Textual + fake ACP 验证 `/yolo` 不进 timeline、按会话隔离、
  无弹窗、allow-once、无 Future 残留和持续提示；现有 adapter 反例证明
  read-only 不能被上层 allow 结果突破。
- **里程碑**：M4.13。

### UC-HOST-001 原生模型驱动的 Host

- **角色 / 触发**：用户发送无 mention 消息、显式 `@host`，或有界讨论/
  workflow 进入 host moderator/final 阶段。
- **前置条件**：XDG 配置文件 `~/.config/myagents/config.toml` 的
  `[host.model].model_id` 已配置，且与 `base_url/models` 返回的完整 `id` 精确
  一致；当前 provider 为 `openai-compatible`。`MYAGENTS_MODEL_*` 可作当前进程
  临时覆盖。未配置、文件权限不是 0600 或 TOML 损坏时 host 为“未就绪”，在
  timeline 写入前拒绝并显示配置文件与 `/v1/models` 引导。
- **主流程**：`HostAgent` 仍是 moderator/supervisor 产品角色，底层由
  `NativeAgentRuntime` 驱动。runtime 通过中立 `ModelProvider/ModelEvent`
  契约流式调用模型、维护 room 内独立上下文和 session；首个 provider 使用
  OpenAI-compatible `/models` + `/chat/completions`，但通用 Orchestrator 不感知
  provider wire protocol。host 单次调用可以直接回答或输出经普通代码验证的
  route/discussion/collaboration JSON，终局主持继续复用既有有界状态机。
- **权限边界**：host 固定 `tool_policy=none`，请求不携带 `tools` 或
  `tool_choice`，不能读写文件、运行 shell、访问网络、调用 skill 或递归派发。
  `/yolo` 与 execution mode 不改变该 profile。需要执行动作时只能路由到已就绪
  worker；`MODERATOR` 是 UI 角色，不是 transport。
- **会话与失败**：每个 room 独占 runtime/context/writer lock。只有权威
  `finish_reason + [DONE]` 才更新模型上下文。提交后的取消、静默超时、断流或
  非权威终止形成 no-replay cursor 并重建 session；模型不存在在 POST 前拒绝。
  不自动降级或跨协议重放到任何第三方 Agent CLI。配置文件限制为 64 KiB 且
  必须是 0600；API key 在 repr、状态、错误和事件中脱敏。
- **验收**：fake HTTP server 覆盖流式正文、结构化路由、直接回答、discussion
  moderator、session 隔离、取消、超时、部分流 EOF、错误映射、未配置、模型
  不存在、配置文件/环境覆盖、权限/损坏 TOML、secret 不泄露和 host 无工具；
  显式 `@worker` 证明零 host HTTP 请求。
  TUI 显示 `MODERATOR · NATIVE MODEL` 与 `NATIVE-MODEL` readiness。真实 LM
  Studio 只做只读模型列表和一次有界最小回复，不修改用户配置。
- **独立证据来源**：ADR-0017、`tests/test_native_agent.py`、
  `tests/fake_openai_compatible_server.py`、`tests/test_phase2.py`、
  `tests/test_tui_completion.py`。
- **真实验收证据**：2026-08-27 只读获取本机 LM Studio 的 3 个模型 id，选择
  当前已加载的精确 id
  `qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive`，经生产 provider/runtime
  收到 `delivery_committed`、4 个 text chunk 和权威 done，合并正文严格为
  `NATIVE_HOST_OK`；未修改 LM Studio 或用户配置。
- **人工验收边界**：不同本地模型的路由 JSON 可靠性、长上下文质量、吞吐、成本
  与 provider 兼容范围不能由 fake contract 证明。
- **里程碑**：M4.14。

### UC-ACP-001 有状态增量上下文

- **角色 / 触发**：用户连续多次 `@` 同一个 stateful agent（ACP、Pi RPC 或
  app-server；用例编号为历史兼容保留 `ACP`）。
- **前置条件**：adapter 声明 `stateful_session=True`。
- **主流程**：首次仅 bootstrap 最近 `history_limit` 条；后续只发 cursor 后的新
  消息并过滤 agent 自己回复；成功后推进 cursor。
- **异常分支**：prompt 提交前或服务端明确拒绝的失败不推进 cursor；提交后
  静默超时等结果不确定失败先持久化 no-replay cursor，再公开失败。同 agent
  并发 dispatch 在 delivery lock 内串行；不同 agent 仍可并行。JSON-RPC
  `error.data` 只按固定字段/深度/字节预算提取并脱敏；上下文超限、余额或额度
  不足转为可操作提示，未知结构回退到有界 message。
- **验收**：不重复旧消息、不丢跨 agent 消息、不乱序、首次发送有界；fake
  provider error 证明 Qwen 类上下文超限和 Kimi 类余额不足不再显示为裸
  `-32603: Internal error`，且敏感字段不进入可见错误。
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
  契约。所有带 inactivity watchdog 的有状态 adapter 每轮非工具静默统一允许
  最长 300 秒，早期 plan/status 只会重置计时；仍由 adapter 有界看门狗与取消
  回收约束。TUI 的 `@` 补全、
  `/agents` 与启动状态动态展示 `@qwen(ACP)`，编排器
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

### UC-ACP-004 WorkBuddy ACP-only 接入

- **角色 / 触发**：用户在聊天室输入 `@workbuddy`，或把 WorkBuddy 选为满足
  既有约束的讨论/workflow worker。产品身份始终显示为 WorkBuddy，不把内部
  二进制名 `codebuddy`/`cbc` 暴露成另一个 agent。
- **主流程**：`AGENT_SPECS` 以
  `AgentSpec("workbuddy", "acp", AcpWorkBuddyAdapter)` 注册。adapter 依次查找
  `MYAGENTS_WORKBUDDY_CLI` 和 PATH 中的 `codebuddy`/`cbc`，只使用可独立运行的
  官方 CLI，不调用 WorkBuddy.app 包内私有二进制。使用
  `--acp --acp-transport stdio` 并固定官方中国区 `internal` 环境；先尝试直接
  `session/new` 复用既有登录态，只有服务端明确返回 `-32000 Authentication
  required` 时才发送标准 `authenticate(methodId)`。默认 method 为 `internal`，
  环境变量只可选择当次 initialize 确实公布的 method。收到登录通知时只在系统
  浏览器打开官方 HTTPS 地址，最多等待 300 秒。
- **安全边界**：默认 `permission="deny"`、无 JSONL fallback。普通轮固定
  `--permission-mode default`、subagent `dontAsk`、空 setting sources 与 strict
  空 MCP；workflow `read_only` 进一步改为 `dontAsk` 并把工具闭集限制为
  `Read,Glob,Grep`。profile 切换重建整个进程/session，禁止 load 旧 session，
  避免继承普通轮授权。server 未公布所选认证 method、登录 URL 非官方 HTTPS、
  浏览器无法打开、认证超时或 ACP prepare 失败均 fail-closed 并原子回收，不跨
  协议重放。
- **验收**：`tests/test_workbuddy_acp.py` 固定 ACP-only 注册、产品命名、CLI
  参数、普通/只读两次 fresh 进程/session、既有登录不重复认证、显式认证错误才
  打开官方 URL、恶意 URL 拒绝、认证超时、launcher 进程组回收及禁止 App 私有
  CLI fallback；`tests/test_phase2.py` 覆盖统一注册/增量路径，TUI 补全来自动态
  AgentSpec。完整 Harness 不调用真实 WorkBuddy。
- **真实协议证据**：2026-08-12 本机安装官方独立 CodeBuddy CLI 2.134.0。生产
  `AcpWorkBuddyAdapter` 在 `internal` 环境直接复用既有登录，default profile 建立
  session `fbcc8cb1-2ee9-440f-b202-3e81a3007f05`，返回
  `WORKBUDDY_PRODUCTION_OK` 并以 `end_turn` 完成。read_only profile 建立 session
  `066e9648-0f94-4508-a01f-63e70c2fe47f`，Write 与 Bash 均为 `Tool Not Found`；
  session `0e8460bd-bd12-4cea-ada8-6356d066faf2` 中 WebFetch 与 Agent/subagent
  同样为 `Tool Not Found`。临时工作区与项目目标文件均未产生，`aclose()` 后无
  `codebuddy` 进程残留。WorkBuddy.app 包内 CodeBuddy CLI 2.115.0 能完成
  initialize/session new，但真实 prompt 长时间无正文，因此被明确排除为 standalone
  fallback。
- **人工验收边界**：登录账户/计费由 WorkBuddy 管理；真实跨进程
  `session/load`、取消时延、图片与长期 session 稳定性尚未验收。国际版与私有化
  region profile 尚无独立安全证据，当前产品只启用已验收的中国区 `internal`。
- **里程碑**：M4.9。

### UC-ACP-005 DeepSeek Harness DSH ACP-only 接入

- **角色 / 触发**：用户输入 `@dsh`，或把 ready 的 DSH 选为普通任务、discussion、
  有序协作或 workflow worker。
- **主流程**：`AGENT_SPECS` 仅注册
  `AgentSpec("dsh", "acp", AcpDshAdapter, dsh_readiness_probe)`。安装入口由
  `MYAGENTS_DSH_CLI` 指向或 PATH 解析出的官方 `dsh` 提供；源码入口只由
  `MYAGENTS_DSH_SOURCE_ROOT` 定位已构建的官方 `apps/cli/lib/bin.js`。两种入口都固定
  启动 stock `dsh --profile myagents`，该 profile 的 exact bundle 顺序必须是
  `@deepseek-ai/dsh-base` → `@myagents/dsh-acp-host`。产品 bundle 的 canonical
  `package.json`、entry、`cordis.patch.yml`、ACP framing、session lifecycle、identity
  与权限桥全部由 `dsh_acp/plugin` 拥有；stock DSH checkout 不得有 myagents 产生的
  core、官方 ACP、示例或测试改动，也不得依赖未导出的 `src/*`。
  `DEFAULT` / `WORKSPACE_WRITE` 使用同一 stock profile 并覆盖
  `DSH_ACP_PROFILE=workspace-write`；`READ_ONLY` 覆盖
  `DSH_ACP_PROFILE=read-only`。跨组切换关闭旧 session/进程、禁止 load 旧 session，
  再以同一 stock profile 建立新进程与 session。
- **readiness 与状态**：probe 只读取环境、PATH、官方 CLI、
  `$DSH_HOME/profiles/myagents/package.json`、解析后的 bundle package manifest 与文件
  属性，不执行 CLI、pnpm/build 或真实模型，也不创建或修复 profile/state。它要求
  profile dependencies 包含 `@myagents/dsh-acp-host`、bundle 列表精确等于
  `["@deepseek-ai/dsh-base", "@myagents/dsh-acp-host"]`，解析后的 package 精确为
  `@myagents/dsh-acp-host@0.1.0`，并声明
  `dsh.bundle.patch=./cordis.patch.yml`；entry 与 patch 必须是 canonical、普通、可读，
  通过 no-follow 稳定读取，且 SHA-256 与 checked-in runtime contract 精确一致。
  `MYAGENTS_DSH_SOURCE_ROOT` 只定位官方已构建
  launcher；产品 host 组合由 profile 中的标准 bundle 提供，readiness 不审计整个
  源码树。`DSH_HOME/profiles` 与 `profiles/myagents` 必须是 profile home 内真实、
  非 symlink 的 canonical 目录；profile manifest 通过 no-follow、单链接、有界且
  读前后身份稳定的句柄读取。由于 stock DSH 在 bundle 后继续应用 home/profile
  `cordis.patch.yml`，这两层只能缺失，或是至多 64 KiB 且忽略空行/注释后唯一语义行
  精确为 `[]` 的 canonical 单链接普通文件；空文件、仅注释文件与任何有效 patch
  均拒绝。任一缺失、版本漂移、越界 symlink、后置 patch 或 runtime identity 漂移均为
  `invalid`。上述 profile 契约在 spawn 前及复用活跃进程的下一轮前重验。子进程显式收到绝对
  `DSH_ACP_PERSISTENCE_DIR`：尊重用户值，否则为
  `${XDG_STATE_HOME:-~/.local/state}/myagents/dsh-acp`，并固定派生 `sessions/`、
  `runtime-home/`、`attachment-home/` 与 `runtime-home/agents/`。state 与 stock checkout
  或 profile home/workspace/plugin/CLI 任一方向包含都 fail-closed；派生目录 symlink
  逃逸同样阻断。`DSH_HOME` 保留为 stock profile home；`DSH_AGENTS_HOME` 绑定
  `runtime-home/agents/`。profile home 的 settings/credentials 通过 no-follow 有界读取后
  复制到 `config-inputs/` 的 mode-0600 产品文件，host 只以显式 canonical path、
  `watch=false` 挂载副本，不接触原文件。入口定位不依赖 ambient cwd，但 transport 在
  initialize 前把子进程 cwd 规范绑定目标 workspace；活跃进程不得跨 cwd 复用。
- **stateful/lifecycle gate**：initialize 必须在任何 new/load/prompt 前同时广告
  `agentCapabilities.loadSession=true` 与对象形状的
  `sessionCapabilities.close`；`agentInfo` 还必须精确声明 name
  `dsh-myagents-acp`、精确 host version `0.1.0`，以及符合
  [ADR-0015 §2.2](adr/0015-dsh-acp-only-transport.md)
  的五个 literal `_meta` key：
  `deepseek.ai/dsh-myagents-profile` 字符串必须与当前进程一致，
  `deepseek.ai/dsh-myagents-policy-revision` 必须是 integer `1`，
  `deepseek.ai/dsh-myagents-read-only-tools` 必须是顺序精确的
  `["read", "glob", "grep"]`，`deepseek.ai/dsh-runtime-version` 必须为
  `0.1.1-rc.2`，`deepseek.ai/dsh-compatibility-revision` 必须是 integer `1`，否则
  进程回收并 block。恢复只用标准
  `session/load`；无 auth 的 load 历史通知直接丢弃，有 auth 时队列上限 64，
  不外泄到当前回复。cancel 有界等待原 prompt，reset/aclose 先有界 close session，
  最后按进程组回收。
  持久化固定 uncompressed/unpacked JSONL；load 在完整历史 materialize 前通过 public
  `list`/`locate` 做 no-follow 文件扫描，硬限制 4096 events / 16 MiB，并在 materialize
  与 resume 前复核 stat identity；无法证明时不广告或拒绝恢复。
- **权限与终局**：默认 deny、handler 结果必须属于当次 options；每次权限请求必须
  恰有不同 id 的 `allow_once` / `reject_once` 两项，`allow_always`、重复/缺失/新增项
  在进入 TUI 前 cancelled；read-only 即使注入 allow 也由 client cancelled。真正的安全边界还要求专用 DSH host 在 runtime
  证明 read/glob/grep 工具闭集、shadow/scoped/run-code hard deny，以及
  workspace-write 风险工具逐次 `allow_once/reject_once`。prompt 首个活动或响应先
  提交 no-replay cursor；外层取消、`stream.aclose()` 与 inactivity 等任何
  实际发出 `session/cancel` 的路径均保持 committed/no-replay，且只有精确
  `stopReason=cancelled` 才确认中断，否则连接重建。只有 `stopReason=end_turn` 产生 done，max-token、turn-limit、
  refusal、cancelled、缺失与未知终局都在 commit 后确定性失败。
- **无 fallback / 图片**：没有 headless、SDK JSON-RPC 或 JSONL fallback，prepare
  失败和提交后失败都不换协议。只有 initialize 同时广告 image 且真实
  provider/model 支持时发送可信房间 PNG；缺证据时图片能力不得宣称可用。
- **自动化证据**：`tests/test_dsh_acp.py` + `tests/fake_acp_server.py` 使用临时
  `DSH_HOME` 覆盖未安装、官方 CLI/source built launcher 缺失、profile 缺失、bundle
  顺序/依赖/name/version/entry/patch 漂移、canonical ownership、profile
  parent/profile/manifest/patch symlink、有效或空/仅注释的后置 patch、readiness 后
  patch 置换、probe 无副作用、
  绝对 argv/env 与子进程 cwd 绑定、
  注册/no fallback、双向
  profile 重建、identity/policy metadata、one-shot 权限闭集、load/no-replay、历史
  flood、materialize 前持久回放 4096 事件 / 16 MiB 上限、state/plugin/config symlink
  与自修改反例、严格 cancel/close/回收、
  lifecycle capability 缺失、动态 image gate 和非成功 terminal；R1/R2/R4 固定静态契约。默认 gate
  不启动真实 DSH。`dsh_acp/plugin/tests/` 是需显式
  `MYAGENTS_DSH_SOURCE_ROOT` 的 release contract 由
  `scripts/check-dsh-plugin.sh` 执行：必须在执行 checkout 内任何 build tool 前，以
  source-only preflight 把实际 DSH commit、official built CLI、ACP SDK、public package
  source/runtime exports 与当前平台 esbuild identity 对照 checked-in runtime contract；
  构建后再校验 built bundle entry/patch SHA-256，然后仅通过 stock DSH public exports 完成
  Oxlint、严格 TypeScript `noEmit` 和无缓存 Vitest；checkout 起始必须完全干净，
  且测试前后 HEAD、Git 状态和包含 ignored 文件内容 SHA-256 的完整 `lstat` 文件树
  不变。它不把 DSH 安装变成默认测试前提。
- **真实验收证据**：2026-08-26 在临时 `DSH_HOME` 与临时 workspace 中，先由官方
  `plugin --profile myagents add` 安装打包后的 `@myagents/dsh-acp-host@0.1.0`，再分别
  走 source launcher 解析和显式 `MYAGENTS_DSH_CLI` 解析。source 路径以两个独立进程
  完成 `new → prompt → close → load → prompt → close`，同一 session
  `7afca581-e99c-4a2f-8631-4efaec794fd8` 依次精确返回
  `DSH_FINAL_PROFILE_OK` 与 `DSH_FINAL_PROFILE_RESUME_OK`，第二轮
  `restored=true`。显式 CLI 路径的真实 workspace-write 轮产生一次绑定
  `allow_once/reject_once` 的 permission request；选择 reject 后零文件副作用，切换
  read-only 后建立 fresh session、风险工具不进入权限 UI 且仍为零副作用。release gate
  同时通过 12 个测试文件、151 项 host contract、Oxlint、严格 typecheck、两次可复现
  构建、无源码 tarball、官方 dump-config 与连续 vision 握手；前后 stock DSH HEAD、
  Git 状态及含 ignored 文件内容的完整文件树不变。用户 settings/credentials 的 inode、
  mode、size、mtime 与 SHA-256 前后相同，默认 `~/.dsh/profiles/myagents` 未创建，退出后
  无 DSH profile 进程残留。
- **人工验收边界**：旧 custom `tsx` host 证据仍为 **superseded**。当前未修改或安装
  用户级 DSH，因此独立发布包形态的全局 `dsh` 可执行文件仍需在实际安装时复跑同一
  release gate；真实主动 cancel 时延、长 session/compaction、max-token/refusal 压力
  与真实图片模型仍未覆盖。对应能力不得超出已获证范围，任何必须修改 DSH 本体才能
  通过的能力直接判为 no-go。
- **事实源**：ADR-0015、`dsh_acp/adapter.py`、`dsh_acp/plugin`、`acp/client.py`。
- **里程碑**：M4.12。

### UC-RPC-001 Pi 原生 RPC 权限桥接入

- **角色 / 触发**：用户在聊天室输入 `@pi`，或把 Pi 选为普通会话、discussion、
  自然语言有序协作或 workflow 中满足既有边界的 worker。
- **主流程**：`AGENT_SPECS` 以 `AgentSpec("pi", "rpc", PiRpcAdapter)` 注册。
  adapter 只启动 `pi --mode rpc`，持有一个 LF-delimited JSON request/event 进程与
  session；Pi 声明 stateful session，因此继续复用通用增量 cursor、delivery lock、
  room checkpoint、图片信任根、活动事件与统一回收。`agent_end` 后继续接收事件，
  只在 `agent_settled` 后结束本轮。
- **隔离与 attestation**：进程关闭自动发现的 extension、skill、prompt template、
  theme，并固定 `--offline` / `PI_OFFLINE=1` 阻止隐式工具下载与更新；不激活原生命名 built-in tools，只以绝对路径加载
  `pi_rpc/extensions/myagents_permission_bridge.ts`。第一条 prompt 前，client 必须
  精确核对一次性 nonce、`myagents.pi.policy/v1`、profile、规范化 workspace、
  policy/bridge hash、active tools 及每个 wrapper 的 source info；任一缺失、超时、
  重复或不匹配都在 timeline/workspace 副作用前 fail-closed 并回收进程。
  Pi 0.84.3 会在 RPC stdin reader 安装前 await 初始 `session_start` handler；bridge
  必须在 handler 内只启动 fire-and-forget attestation task 并立即返回，且用单调
  session generation 拒绝旧 session 的迟到 ACK/task 改写当前 ready 状态。
- **execution profile**：`DEFAULT` 和 `WORKSPACE_WRITE` 只暴露
  `myagents_read/grep/find/ls/edit/write/bash`；`READ_ONLY` 只暴露前四个读取
  active wrapper，并在 hook 内再次 hard-deny 风险工具。profile 切换关闭旧进程
  并建立新进程、新 session，不恢复跨 profile session。路径型 wrapper 先
  canonicalize；workspace 外读取（含 symlink 逃逸）逐次询权，写入则拒绝 workspace
  外、`.git`、hard-link 和非法新建目标；`edit/write/bash` 每次通过 extension UI 映射到共享 TUI，
  只接受与本次 call nonce 绑定的 `allow_once` / `reject_once`，不提供永久授权。
  adapter 还必须把 permission tool 精确绑定本进程已 attested 的 profile 工具闭集；
  permission handler/弹窗只能在 Orchestrator 已持久化本轮 no-replay cursor 后运行。
  权限 bridge 只把最多 4096 UTF-8 bytes 的有界 input 摘要交给共享
  PermissionScreen，由后者统一脱敏；截断时带 `_myagentsPreview`。超限 bash 不
  隐藏尾部并请求批准，而是直接阻断。完整 canonical args 由 `argsHash`、
  toolCallId、tool name、路径快照、60 秒 TTL 与 one-time permit 绑定，展示上限不得
  误拒绝合法的大文件输入；未知字段、畸形嵌套 schema 与无法安全截断的摘要均拒绝。
- **故障与重放边界**：无 permission handler、handler 异常、取消/超时、畸形或
  未绑定选择、未知工具和未知 interactive UI 均拒绝；未知非交互 lifecycle event
  只作为有界 `activity` 显示，不能扩权或完成本轮。prompt 成功响应前的事件只缓冲；成功
  后先发布 `delivery_committed` 再发布事件，新 session info 也只能在该边界后显示。
  本轮必须存在最终 assistant `message_end`，且 `stopReason` 只能属于
  `stop/length/toolUse/deferred` 显式成功闭集；缺失/未知 terminal、`error/aborted`，以及终局
  `tool_execution_end.isError + result.terminate=true` 必须记为提交后失败；中间错误
  后自动重试/后续 assistant 成功则正常完成。prompt 写入后无响应、提交后断线或
  abort 不确定结果按 no-replay 失败并作废当前连接。公开 client API 不接受任意 RPC dict，也不得
  发送 raw RPC `{ "type": "bash" }`。单帧、active prompt 事件累计字节/条数与
  extension UI 并发数都有硬上限；活跃重复 request id 或任一溢出关闭连接。Pi 没有
  ACP 或 headless JSON/JSONL fallback。
- **验收**：`tests/test_pi_rpc_client.py` + `tests/fake_pi_rpc_server.py` 固定 framing、
  stream、早到事件、permission checkpoint gate、最终失败、settled、abort、精确恢复和进程回收；
  `tests/test_pi_adapter.py` 固定注册、三 profile argv/tool 闭集、attestation、
  prepare 后重复 attestation 回收、profile 重建/关闭竞态、新 session 延迟落盘、
  单事件背压、取消 no-replay、长工具 watchdog、
  每轮 16 张且合计 20 MiB 的图片读取前预算与事件映射；
  `tests/test_pi_permission_bridge.py` 使用隔离 fixture 固定 wrapper 路径边界、逐次
  选择绑定、`session_start` 非阻塞与迟到 generation 失败反例。静态 R4 gate 阻断
  built-in/raw bash/fallback 回流。默认 Harness 不调用真实 Pi，不安装、卸载或
  修改用户 Pi 配置。
- **真实协议证据**：2026-08-25 本机安装版与本地源码均为 Pi 0.84.3；已核实
  `--mode rpc`、LF JSON stream/session/steer/follow_up/abort/image、extension
  pre-tool block 与 RPC extension UI 的协议形状；只读加载探针还证明当前 0.84.3
  导出可构造七个底层 tool definition。真实启动探针进一步确认：初始
  `session_start` 发生在 RPC stdin reader 安装前，在该 handler 内 await select 会
  形成约 15 秒 bootstrap deadlock。改为非阻塞 task 与 generation guard 后，真实
  0.84.3 约 0.4 秒完成 ACK、ready、`get_commands` 与 `get_state`。adapter 将
  受管目录内、与 native session id 绑定的未落盘路径视为新会话 reservation，
  以 0600、fsync 的 reserved/materialized sidecar 精确绑定 token/path/id/workspace/
  profile；跨进程只有精确 reserved 且文件未产生时才 fresh，materialized 后缺失
  fail-closed，并在 `agent_settled` 前强制核验实际文件/header/cwd。真实短对话返回
  `LIVE_PI_PONG` 并落盘 session；真实 implement 探针只经一次 `allow_once`
  创建临时 `result.txt=PI_WRITE_OK`，关闭后均无 Pi 进程残留。
- **人工验收边界**：获得明确授权后，在全新临时目录验证 attestation、越界读取
  拒绝、写入拒绝、一次写入允许后再次询权、abort 和关闭无残留。permission
  bridge 是应用层 capability boundary，不是 OS sandbox；批准 shell 后仍继承
  myagents 进程权限，故不支持不受信输入/敏感环境的无人值守执行。真实图片、长期
  session、跨进程恢复和取消时延仍待人工验收。路径快照不承诺抵抗同一用户下敌对
  并发进程在最终校验与底层 I/O 之间制造的竞态；该威胁需要外部 sandbox/容器。
- **事实源**：ADR-0014、`pi_rpc/client.py`、`pi_rpc/adapter.py`、
  `pi_rpc/extensions/myagents_permission_bridge.ts`。
- **里程碑**：M4.11。

### UC-AGENT-001 本机 Agent 就绪状态与设置入口

- **角色 / 触发**：用户启动 TUI、输入 `/agents` 或 `/agents rescan`，或提交
  指向一个或多个注册 agent 的任务。
- **主流程**：生产 TUI 为每个 `AgentSpec` 执行被动 readiness probe，只读取
  环境变量、当前进程 PATH、文件类型与可执行位。启动摘要区分注册数与 ready
  数；`@` 候选保留所有注册项但 ready 优先；`/agents` 显示状态、transport、
  原因和人工设置提示；`/agents rescan` 重新读取环境、同步所有已加载会话，并为
  新 ready 的目标惰性构造 adapter，不重启 TUI。新建会话继承同一 probe 配置。
- **原子边界**：显式多目标、discussion 全体与 moderator、workflow 全部固定
  角色及 final host 必须同时 ready，否则在 timeline/Git baseline/adapter 调用前
  整条拒绝。无 mention 消息要求 host ready；host 只看 ready worker 候选。
  TUI 在清空输入前调用同一资格门，因此错误后草稿与焦点保持。
- **安全边界**：probe 不得启动 CLI、联网、打开浏览器、读取登录态、执行包管理器
  或修改 shell/PATH；产品不自动安装、卸载、移动或替换 agent。`not_found` 只表述
  “当前进程 PATH 未检测到”，不得推断用户没有安装。WorkBuddy 候选继续受独立
  CLI canonical path 与 App bundle 拒绝规则约束。DSH 只接受官方 installed `dsh`，
  或 `MYAGENTS_DSH_SOURCE_ROOT` 中已构建的官方 CLI，并要求 stock `myagents` profile
  及 exact myagents bundle ready；CLI/profile/dependency/bundle/name/version/entry/patch
  任一缺失为 invalid，probe 不 build、不启动且不创建 persistence/profile 目录。
- **异常分支**：probe 异常、不可执行文件、无效显式路径或 adapter 构造失败记为
  `invalid` 并 fail-closed；状态错误不删除角色、cursor、session id 或历史事实。
  Pi 只被动解析 `pi` 可执行文件；probe 不以启动 RPC/attestation 代替 readiness。
- **验收**：`tests/test_agent_readiness.py` 使用 fake resolver、临时文件与符号链接
  覆盖零/部分/新增 CLI、原子门和 host 候选；`tests/test_tui_completion.py` 覆盖
  ready 排序、状态文案、rescan 及草稿保留。完整 Harness 不调用真实 agent。
- **人工验收边界**：安装器、包管理器选择、登录和 shell 配置仍由用户在产品外
  完成；myagents 只给出可操作提示并在用户要求时重新检测。
- **里程碑**：M4.10。

### UC-PERM-001 权限请求与选择

- **角色 / 触发**：ACP agent 在受控工具调用前发
  `session/request_permission`，或 Pi 的已 attested permission bridge 发出绑定工具
  call nonce 的 `extension_ui_request`。
- **前置条件**：TUI 已注入 agent-aware 异步权限处理器。
- **主流程**：弹窗显示来源 agent、工具标题和 options；用户选择；client 只接受
  本次 options 内非空 optionId。
- **异常分支**：无处理器、取消、异常、None、空或未知 optionId 全部 cancelled；
  Pi 的未知/复用 call nonce、非 `allow_once/reject_once` 选择同样拒绝。
- **验收**：等待用户时 read loop 不阻塞；合法 allow/reject 能回传；畸形结果
  fail-closed；权限请求到结果发回期间暂停 agent inactivity timeout，用户等待
  超过该 timeout 也不会误判 agent 卡死。
- **独立证据来源**：`tests/fake_acp_server.py` + ACP/Phase 2 contract tests；
  `tests/test_acp.py::test_permission_wait_pauses_inactivity_timeout`；
  2026-07-26 在临时目录用真实 Kimi Code CLI 0.29.1 + Textual TUI 验证实际
  `allow_once / allow_always / reject_once` options，选择 `allow_once` 后探针
  文件内容正确；严格 optionId 成员校验落地后再次真实复验通过。
  2026-08-08 OpenCode 1.18.14 临时目录 probe 证明 runtime ask policy
  将无害 Bash 请求转为相同三类 options，默认 deny 后 `end_turn`；Pi 权限形状由
  `tests/test_pi_adapter.py` 与 `tests/test_pi_permission_bridge.py` 的 fake bridge
  contract 固定；`tests/test_dsh_acp.py` 固定 DSH 两 profile 的 option 绑定与默认
  deny，默认 gate 不调用真实 Pi/DSH。
- **人工验收边界**：默认模式下任何真实工具写入必须由用户逐次授权；`/yolo`
  自动模式只能由用户在 TUI 显式开启，真实外部/工具测试仍需单独授权，自动化
  测试不能代替风险接受。
- **里程碑**：M2。

### UC-LIFE-001 取消与退出回收

- **角色 / 触发**：用户取消流、退出 TUI，或 agent 超时/断线。
- **前置条件**：子进程使用独立进程组，adapter 拥有 session。
- **主流程**：发送协议对应的 cancel/abort；等待原 prompt 结束；TUI 先收尾权限 Future，再
  `aclose`；SIGTERM 超时后 SIGKILL。
- **异常分支**：不在等待人工权限时，prompt 连续 300 秒无任何 transport 事件才自动
  cancel。该轮已提交，先建立 no-replay
  边界再公开失败。cancel 不确认则连接
  作废并在下轮重建；断线使 pending request 立即失败。
- **验收**：人工权限等待不会触发 inactivity timeout；下一轮不与旧 prompt
  重叠；fake 子孙进程和 ACP/Pi RPC/DSH server 均无残留。
- **独立证据来源**：接入 Harness 前已有的 basic/ACP/Phase 2 生命周期测试，
  `tests/test_dsh_acp.py` 的 cancel/close/profile reset/terminal fixture，以及
  `tests/test_pi_rpc_client.py` 的 abort/close/子孙进程 fixture；
  2026-07-26 两次真实 Kimi TUI E2E 退出后 `pgrep -fl '^kimi acp$'` 为空。
- **人工验收边界**：真实 Kimi cancel 响应时延和长任务中的不可逆工具副作用尚未
  验证。
- **里程碑**：M1–M4.12。

### UC-ROOM-001 持久房间与重启恢复

- **角色 / 触发**：用户在同一 workdir 重启 TUI，或第二个进程试图打开同一
  房间。
- **前置条件**：房间状态位于
  `${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/<room_id>`，不污染
  workdir。
- **主流程**：timeline 以单调 `seq` append-only 落盘；`state.json` 保存
  每个 stateful agent 的 seq cursor 与 opaque native `session_id`（原子写）；TUI
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
- **异常分支**：仅 load 返回标准 `-32002` / `-32601`，或具体 adapter
  已获证的精确 session-not-found 映射时回退新 session；agent 不声明
  loadSession 时可直接新建。其他 generic server/policy/quota/transport 错误
  fail-closed。实际 fresh session 将 cursor 归零并按 `history_limit`
  有界 bootstrap（不沿用旧 cursor 静默跳过 history），新 session id 落盘；
  checkpoint 写失败穿透
  dispatch（零 prompt、adapter reset、内存/磁盘不假提交），不伪装成
  agent 调用失败；post-submit no-replay checkpoint 落盘失败则停用
  Orchestrator，拒绝后续 dispatch；prompt 提交前的明确未发送失败
  cursor 不推进、下轮补发；
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

- **角色 / 触发**：用户向 Kimi worker 派发任务，但新 ACP 连接的本地
  executable 无法启动，或 initialize / `session/new` 明确返回标准
  method-not-found（`-32601`）。
- **主流程**：`AcpKimiAdapter` 在同一 writer lock 内保持统一
  `AgentAdapter` interface；prepare 失败时先以
  `fallback:jsonl:kimi` 调用 checkpoint hook，再使用
  `kimi -p --output-format stream-json --agent-file <readonly-profile>` 执行
  一次降级轮，并公开 `prepare-only` fallback info 事件。
- **权限边界**：降级 profile 只暴露 `Read` / `Grep` / `Glob`，
  `subagents: []`；禁止写入、命令、网络、Skill、子 agent 和 MCP。
  JSONL 可返回分析或明确阻塞，不得伪装已完成变更。
- **禁止分支**：`session/load`、认证/权限/配额/backend 拒绝、timeout、
  transport 错误、活跃 session 冲突、checkpoint 失败、prompt 明确拒绝以及
  prompt 提交后的取消/超时/断线/不确定结果均不调用 fallback。后一类先建立
  no-replay cursor 再公开失败。
- **恢复**：下一轮不对 `fallback:jsonl:*` 伪 id 执行
  `session/load`，直接新建 ACP session；以 fresh/unrestored 语义
  归零 cursor 并有界 bootstrap。两种 transport 不并发写同一 session。
- **验收**：生产注册仍由 `AcpKimiAdapter` 构造，可见 transport
  为 `acp+jsonl`；获准的 pre-session failure 正好调用一次受限 JSONL；
  load/auth/policy/quota/backend/timeout/transport、prompt 拒绝、post-submit
  静默与写入后断线均零 fallback 调用；首个可见 ACP 输出前已提交内部
  `delivery_committed`。
- **独立证据来源**：Kimi CLI 0.34.0 本机 `--help` 和官方 reference
  确认 `-p` / `stream-json` / `--agent-file`；`tests/fake_acp_server.py`
  作为独立 protocol fixture，`tests/test_kimi_hybrid.py` 验证工具白名单、
  prepare-only、伪 checkpoint 恢复、交付提交时序和 no-replay 反例。
- **人工验收边界**：真实 Kimi 模型的降级回复质量与 CLI 升级后
  schema 漂移需受限临时目录探针；默认 gate 不调用外部 agent。
- **里程碑**：M4.4。

### UC-HYBRID-002 OpenCode ACP-first 权限收口与受限降级

- **角色 / 触发**：用户向 OpenCode worker 派发任务；正常走 ACP，或新 ACP
  连接的本地 executable 无法启动，或 initialize / `session/new` 明确返回
  标准 method-not-found（`-32601`）。
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
- **禁止分支**：`session/load`、认证/权限/配额/backend 拒绝、timeout、
  transport 错误、活跃 session 冲突、checkpoint 失败、prompt 明确拒绝以及
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
  `BUS_CLOSED`），不泄漏 traceback；外部消息触发工具权限时仍由当前 TUI
  决策（默认弹窗），bridge 无模式开关或绕过入口；若 TUI 以 ADR-0016
  危险模式启动，外部任务同样遵守该进程级决策；control/validation 错误
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
- **正文呈现**：agent/host 回复中的常用 Markdown（强调、行内代码、标题、
  列表、引用和 fenced code block）转换成安全的终端 `Text` 样式；不解释 Rich
  markup。用户原文、system 状态与活动卡保持字面值，持久历史和流式重绘使用
  同一呈现路径，未闭合的 Markdown 在流式阶段保留为普通文本。
- **安全分支**：thought 正文不显示；常见凭据字段隐藏；执行事件不进入
  agent 对话 history。
- **高频分支**：adapter 继承初始 tool title/command 并压缩重复 update；
  CommandBus 再做 producer-independent 防御性去重。重复 update 仍刷新
  activity 时钟，但 activity-only 事件不进入 UI/events，不会误触发
  heartbeat/inactivity；TUI 只在可见状态变化时重绘同一 command 的活动卡，
  将阶段、最新 heartbeat、工具与权限合并，默认只显示终态、当前阶段和工具
  汇总。`Ctrl+G` 聚焦活动区并默认选中最近卡，`↑↓` 循环选择，`Enter` 逐卡
  展开脱敏明细，`Esc` 返回输入框；`/details` 切换当前选中卡，否则只切换
  最近一张卡。协议后续补发 tool ID 时迁移原逻辑项，
  不重复计数；fallback identity 来源显式传递，正式 ID 即使恰等于标题也不被
  误迁移。窗口裁掉旧工具后仍保留历史失败/拒绝/取消事实。每个 room 独立保留
  近期终态的可展开安全摘要及逐卡展开态，切走期间的后台
  更新继续进入该 room 的活动模型。每卡只保留最近 50 个工具明细、每 room
  只保留最近 100 张可展开终态卡，更早内容在当前视图冻结成折叠归档；完整事实仍从
  `events.jsonl` 读取；后台 runtime 经过 10 分钟 idle reap 后同步释放该 room
  的 UI 活动模型。ACP 与 Pi RPC 的活跃工具使用 15 分钟独立 watchdog；所有
  带 inactivity watchdog 的有状态 adapter 普通静默统一使用 300 秒阈值，
  Codex app-server 当前不切换独立工具预算。
- **重启分支**：最后事件非 terminal 的 command 显示为上次中断及最后状态。
- **完成语义**：`completed` 只表示本轮调用正常结束，不等同于用户任务验收；
  TUI 显示“本轮响应结束”，不显示“agent 完成”。fan-out 任一 worker
  失败时本轮是 `failed`，即使其他 worker 已经正常回复；固定任务区保留各
  agent 终态并把混合成功/失败显示为“部分完成”。
- **验收**：`events.read` 可分页读取；heartbeat 累计且界面不刷行；200 条相同
  tool update 在 ACP 层压成“创建/进行中/完成”三个状态，在防御性 bus
  fixture 中只转发并持久化一次；TUI 对一个 command 始终只占一张活动卡，
  agent Markdown 控制符不裸露且样式可见，用户/system/activity 文本不被解释；
  折叠态不泄露命令；`Ctrl+G`、`↑↓`、`Enter`、`Esc` 可完成逐卡键盘浏览，
  `/details` 只切换当前或最近卡；展开态可见最新 heartbeat、工具终态与脱敏命令，错误仍在
  聊天主线单独可见；多 agent 交错更新时焦点跟随最后真实活动，会话切走、后台
  完成再切回后卡片仍在；工具/终态窗口超限后有界归档；旧房间安全补建；损坏
  日志 fail loudly。
- **证据**：`tests/test_storage.py`、`tests/test_acp.py`、
  `tests/test_phase2.py`、`tests/test_basic.py`、`tests/test_tui_activity.py`、
  `tests/test_tui_status.py`、
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

### UC-CODEX-001 Codex worker 原生长连接

- **角色 / 触发**：用户连续点名 `@codex`，或编排器将 Codex 选为 worker。
- **前置条件**：本机 Codex CLI 支持 `codex app-server`；adapter 独占其进程。
- **主流程**：一次 initialize 后建立 thread；worker 的连续 turn 复用同一
  app-server PID/thread 并按 cursor 接收增量。历史 Codex host 的 ephemeral
  thread 能力仍由 adapter contract tests 保留，但生产 host 已由 ADR-0017 的
  native model runtime 取代。
- **配置边界**：不发送 model、effort、config、collaboration mode、plugin 或
  MCP 覆盖，不修改 Codex 全局配置；仅传 cwd、既有 sandbox、approval policy，
  worker 为 `workspace-write + on-request`，越界操作进入统一权限 UI。
- **事件分支**：agent message delta 流式进入正文；command/file/MCP item
  进入脱敏 tool/status；approval 进入统一权限 UI且无处理器默认拒绝；
  reasoning 正文不显示；`turn/completed` 后才产生 done。
- **取消/故障分支**：取消发 `turn/interrupt` 并等待 terminal；断线使 pending
  立即失败；initialize/thread prepare 失败可退到 JSONL，一旦发送
  `turn/start` 就禁止自动重放；已发送后的结果不确定、terminal 失败/中断和
  用户取消都先形成持久 no-replay 边界，再公开失败/取消。服务端明确拒绝或
  确认未发送的失败保持可重试；即使旧 thread 无法恢复，新 thread 也不
  bootstrap 已投递的旧输入。明确接受的 turn 在任何正文/工具/权限 event
  sink 回调前先持久化该边界。
- **验收**：fake server 证明 worker 两轮同 PID/thread、无 `jsonrpc` header、
  默认配置字段未被覆盖、取消不
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

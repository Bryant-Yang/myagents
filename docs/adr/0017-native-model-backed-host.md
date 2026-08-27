# ADR-0017：原生模型驱动的 myagents Host

- 状态：Accepted
- 日期：2026-08-27
- 里程碑：M4.14
- 作者：Bryant Yang
- 取代：[ADR-0004](0004-ephemeral-codex-host-threads.md) 的生产 host 决策

## 1. 背景

此前 `HostAgent` 是 moderator/supervisor 产品角色，但生产实现由一个只读
`CodexAppServerAdapter` 驱动。这使无 mention 路由、直接回答、讨论总结和
`@host` 都依赖 Codex CLI；“host 是什么”和“host 当前借用哪家 agent”也被
混在一起。用户只安装模型服务、没有任何第三方 coding-agent CLI 时，聊天室
无法独立工作。

模型 API 与 coding agent 协议也不是同一层抽象。Responses API、Chat
Completions 和 Anthropic Messages 是 provider wire protocol；ACP、app-server、
RPC 与 JSONL 是 agent/runtime protocol。把任一模型 API 写进 Orchestrator 会把
路由、会话、权限和厂商请求形状耦合在一起。

## 2. 决策

### 2.1 角色和分层

- `HostAgent` 继续表示 moderator/supervisor 产品角色；`MODERATOR` 只用于 UI
  描述，不是 transport。
- `ModelProvider` 是中立模型边界，只接收 `ModelMessage`，输出
  `ModelEvent(committed/text/activity/usage/done)`。它不知道 room、worker、
  workflow、权限或 TUI。
- `OpenAICompatibleProvider` 是首个 provider 实现，使用 `/models` 与
  `/chat/completions` SSE。未来 Responses API、Anthropic 或其他 provider 必须
  作为同级实现加入，不能在 Orchestrator 或 `NativeAgentRuntime` 事件循环中按
  provider/name 分支。
- `NativeAgentRuntime` 把 provider 映射为统一 `AgentAdapter`：单 writer、
  房间内会话上下文、流式事件、取消、静默超时、权威终态、session prepare 与
  no-replay 均由它负责。
- 生产 `HostAgent` 默认且仅由这个 myagents-owned runtime 驱动。Codex
  app-server 继续作为 `@codex` worker 的 transport，不再是 host 依赖。

### 2.2 配置和模型选择

配置只从当前进程环境读取：

- `MYAGENTS_MODEL_PROVIDER`：默认且当前唯一值 `openai-compatible`；
- `MYAGENTS_MODEL_BASE_URL`：默认 `http://127.0.0.1:1234/v1`；
- `MYAGENTS_MODEL_ID`：必填，必须与 `/v1/models` 返回的某个完整 `id` 精确相等；
- `MYAGENTS_MODEL_API_KEY`：可选，只进入 Authorization header，不进入 repr、
  readiness、timeline、event 或可见错误。

base URL 只接受无内嵌凭据、query 和 fragment 的 HTTP(S) URL。readiness 只被动
检查环境变量语法，不联网、不启动服务、不修改 LM Studio 或用户配置；模型是否
真实存在由 provider 首次请求前调用 `/models` 独立验证。缺失配置显示为未就绪，
不会崩溃或误报 ready。provider 名、base URL、model id 与 API key 分别限制为
64、2048、512 与 8192 字符；拒绝信息不回显凭据或无界配置值。

### 2.3 会话、取消和 no-replay

每个 room 的 Orchestrator 拥有独立 runtime、session id、消息上下文和 writer
lock，不跨房间共享。正常完成后才把 user/assistant 消息加入模型上下文。

provider 在开始 Chat Completions 投递时产生 `committed`；runtime 将其转换为
内部 `delivery_committed`，沿用 Orchestrator 的 checkpoint-before-visible-event
契约。提交后的静默超时、断流、无 `[DONE]`、无 `finish_reason` 或取消均不自动
重放：持久 cursor 先推进，runtime 丢弃不可信上下文并建立新 session。干净启动
可按现有 bootstrap 上限恢复有界 timeline；提交后不确定轮次的下一 session
禁止倒退 cursor。

无 mention 路由、角色提取与协作提取在 user record 提交时冻结本次
`committed.seq`。即使随后等待 host 单写锁，prompt 也只读取该 seq 及之前的
timeline，cursor 只单调推进而不把后来并发提交的消息混进本次语义判断。

默认连续无活动上限继续使用产品统一的 300 秒；测试可以注入更短值。`done`
必须同时具备 provider finish reason 和流终止标记，正文或 HTTP 200 不能代替
权威终态。

### 2.4 权限与能力

原生 host 永久 `tool_policy=none`：请求不发送 `tools` / `tool_choice`，system
与 moderator prompt 也明确禁止文件、shell、网络、skill 和子 agent。runtime
没有 permission handler 或进程启动 seam；`/yolo`、workflow execution mode
以及模型输出都不能扩大该 profile。

本阶段不提供原生 coding worker、写工具或 shell。需要执行动作时 host 只能在
已就绪 worker 闭集内路由；没有 worker 时只给出说明。图片仍应路由给具备视觉
能力的 worker，host provider 不自动获得本地文件访问。

### 2.5 错误和降级

不存在到 Kimi、Codex、Qwen 或其他 agent CLI 的自动 fallback。未配置、模型 id
不存在、HTTP 拒绝和提交前失败返回可操作错误；提交后的传输不确定映射为
`AgentDeliveryUncertainError`，取消映射为 `AgentDeliveryCancelledError`。
所有 provider 错误有长度上限并经过通用凭据脱敏。

Host 失败后 Orchestrator 现有确定性 worker 回退仍可用于路由可用性，但它只从
已就绪 worker 中选择，且不会把同一已提交 native model turn 跨协议重放。

## 3. 验收

1. 本地 fake OpenAI-compatible server 覆盖精确 `/models`、SSE 正文、结构化
   路由、直接回答、讨论 moderator、session 隔离与 context。
2. 取消、提交后静默超时、部分正文后 EOF、缺少权威终态均形成 no-replay
   失败；模型不存在在 POST 前拒绝。
3. HTTP 错误回显 Authorization 时 API key 不出现在异常；超长配置、模型清单
   和 SSE 均有界；所有请求都没有 `tools` 和 `tool_choice`。
4. 未配置 host 在 timeline 前阻断并提供设置引导；TUI 将 transport 显示为
   `NATIVE-MODEL`，不把 `MODERATOR` 当协议。
5. 显式 `@worker` 不触发 provider；原有 discussion、collaboration、workflow、
   会话角色和 worker adapter 测试全部回归；Host 锁竞争时前一 dispatch 的
   prompt 不得包含后一条 user record。
6. 真实 LM Studio 只允许读取 `/v1/models` 后选择一个精确 id，并进行一次有界
   最小对话；不修改 LM Studio、模型或用户配置。未运行时必须明确登记。

自动证据：`tests/test_native_agent.py`、
`tests/fake_openai_compatible_server.py`、`tests/test_phase2.py` 与
`tests/test_tui_completion.py`。

真实证据：2026-08-27 本机 LM Studio `/v1/models` 只读返回 3 个 id；选择当时
已加载的精确 id `qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive`，通过生产
`OpenAICompatibleProvider + NativeAgentRuntime` 完成一次有界短回复，事件为
`delivery_committed → 4×text → done`，正文严格为 `NATIVE_HOST_OK`。探针未修改
LM Studio、模型或用户配置。

## 4. 后果与明确非目标

- myagents 的基本主持能力不再依赖第三方 Agent CLI；worker 仍按各自 adapter
  独立安装和探测。
- 首版每个 room 的模型上下文驻留进程内；持久事实仍以 room timeline/cursor 为
  准，不新增第二套记忆数据库。
- Chat Completions 的兼容差异由 provider contract tests 承担；provider 协议
  漂移不能泄漏到通用编排器。
- 完整 coding 工具集、provider 热切换、模型管理 UI、成本预算和上下文压缩不在
  本阶段；需要时必须新增明确契约和权限反例。

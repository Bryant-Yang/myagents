# ADR-0006：Kimi ACP-first 与受限 JSONL 降级

- 状态：Accepted
- 日期：2026-08-08
- 里程碑：M4.4
- 作者：Bryant Yang

## 1. 背景

Kimi Code CLI 同时提供两种可编程入口：

- `kimi acp`：长驻 JSON-RPC/NDJSON 连接，支持 session、流式工具
  状态、权限请求、取消、图片与恢复。
- `kimi -p ... --output-format stream-json`：单次无 TUI 调用，启动和
  解析界面简单，但每轮新建进程，且 print mode 会自动处理
  工具权限，无法把写入/命令反向请求交给 myagents TUI。

只保留 ACP 会让 Kimi CLI 在启动、initialize 或 session/new 阶段
故障时整个 worker 不可用；直接恢复旧 `KimiAdapter` 生产注册又会
丢失权限、取消、恢复与 no-replay 保证。需要的不是平级二选一，
而是明确时机和权限边界的 hybrid transport policy。

## 2. 决策

### 2.1 对外 seam

Kimi 仍只向 Orchestrator 暴露一个 `AgentAdapter` interface：
`AcpKimiAdapter.stream_prepared()`。传输选择、降级 checkpoint 和一次性
JSONL 进程都收在 adapter implementation 内；Orchestrator 不出现
`if agent_name == "kimi"` 或任务关键词分支。

`AGENT_SPECS` 仍注册 `AcpKimiAdapter`，可见 transport 标识为
`acp+jsonl`。该标识是能力声明，不改变 stateful cursor 判定；
是否发增量上下文仍只看 adapter 的 `stateful_session` 能力。

### 2.2 唯一自动降级点

只有尚无活跃 ACP session，且本地 executable 无法启动，或
initialize / `session/new` 明确返回标准 method-not-found（`-32601`）时，
可以自动进入 JSONL。此时尚未建立 session、也未发送 `session/prompt`，
不存在重复工具副作用。`session/load` 的任何失败都不跨协议：只有标准或
具体 adapter 获证的 session-not-found 可在 ACP 内部回退 `session/new`。

以下情况一律禁止降级：

- 活跃 session 与持久化 session id 冲突；
- `session/load`、认证/权限/配额/backend 拒绝、timeout 或 transport 错误；
- Orchestrator `make_prompt` / checkpoint 失败；
- `session/prompt` 明确拒绝；
- prompt 发送后的断线、静默超时、取消、非成功终态或结果不确定。

后一类继续遵守 no-replay：先持久化已投递 cursor，再公开失败，
不把同一任务交给 JSONL 重做。

ACP client 只有在 `writer.write()` 前明确失败时才标记请求未发送；一旦 write
开始，`drain` 失败、断线与 prompt 的显式 remote error 都跨过 no-replay
边界，不能据此重投。session prepare 的显式 remote error 可确定失败，但是否
允许 fallback 仍只按上述精确 classifier。首个
`session/update` / 权限活动进入外部 sink 前，adapter 先发内部
`delivery_committed` 事件，让 Orchestrator 固化 no-replay cursor。

### 2.3 JSONL 权限与能力

自动 fallback 使用项目内置的显式 Kimi agent file，只允许：

- `Read`
- `Grep`
- `Glob`

`subagents: []`，不暴露 `Bash` / `Write` / `Edit` / `Skill` / `Agent`
或 MCP 工具。这是 Kimi 运行时执行的工具白名单，不是只靠 prompt
约束。JSONL 降级可返回问题定位、代码阅读或可操作的阻塞
说明，但不得声称已写文件、运行命令或完成部署。

不在本阶段提供可写 JSONL 模式。如果未来需要无人值守写入，
必须先新增独立的隔离工作区、显式用户 opt-in 和单独验收契约，
不得放宽当前 fallback profile。

### 2.4 checkpoint 与恢复

降级前仍在 adapter writer lock 内调用 `make_prompt`，使
Orchestrator 在任何 JSONL 输出前完成 checkpoint。降级轮使用
`fallback:jsonl:<agent>` 伪 session id 表示“本轮没有可恢复 ACP
session”。

下一轮不向 `session/load` 发送该伪 id，而是直接 `session/new`；
Orchestrator 看到 fresh/unrestored 后把 cursor 归零，按既有
`history_limit` 有界 bootstrap。JSONL 与新 ACP session 永不并发写入
同一 session。

### 2.5 Kimi Host 是显式只读能力，不是 fallback

生产 Host 默认仍可使用 ADR-0017 的 myagents 原生无工具 model runtime。同一固定
只读 profile 也可由 `AgentHostCapability` 显式创建为独立 Kimi Host；它是用户选择
的 Host transport，不是 native provider 或 worker ACP 失败后的自动 fallback。
它不复用 worker session，也不会在 prompt 提交后跨协议重放。

## 3. 验收

1. contract test 证明 JSONL 命令携带内置 agent file，且白名单
   没有写入、命令、Skill、子 agent 工具。
2. fake ACP initialize 明确返回 method-not-found 时，只调用一次 JSONL，
   用户可见 `prepare-only` fallback 事件；load/auth/policy/quota/backend/
   timeout/transport 反例均为零 fallback。
3. fallback checkpoint 的下一轮直接 session/new，不 load 伪 id。
4. prompt 明确拒绝、post-submit inactivity 与 prompt 写入后断线都不调用
   fallback；首个可见 ACP 输出前已产生 `delivery_committed`。
5. `AGENT_SPECS` 仍由 `AcpKimiAdapter` 工厂构造，可见 transport 为
   `acp+jsonl`；通用 Orchestrator/ACP 层无 agent-name 分支。
6. R4 负向探针证明直接注册 `KimiAdapter` 或放宽 fallback
   agent file 会被门禁拒绝；清理探针后全量 Harness 通过。
7. 真实 Kimi 验收只做受限临时目录探针：ACP 正常路径与
   tool-profile JSONL 输出可解析；不把真实模型测试放入默认 gate。

## 4. 后果

- Kimi 正常工作时继续获得 ACP 的持久 session、权限、取消、
  恢复和可观测性。
- ACP 尚未提交任务时的启动故障不再导致 Kimi 完全失联；
  降级是可见的只读能力，不伪装成完整 ACP 成功。
- 代码库需同时维护 Kimi ACP 和最小 JSONL schema；Kimi CLI 升级后
  需重跑 contract 与真实受限探针。
- JSONL 不能进行 TUI 权限交互，因此自动降级永久受工具白名单
  约束；任务需要写入时，只能返回可验证的分析和阻塞说明。

# ADR-0017：可切换 HostBackend 与原生模型 Runtime

- 状态：Accepted
- 日期：2026-08-27
- 里程碑：M4.14
- 作者：Bryant Yang
- 取代：[ADR-0004](0004-ephemeral-codex-host-threads.md) 的生产 host 决策

## 1. 背景

`HostAgent` 是 moderator/supervisor 产品角色，不是协议。Host 既应能在没有
第三方 coding-agent CLI 时直接调用模型，也应能按会话切换到一个完整 agent
作为推理引擎。模型 API 与 coding-agent transport 仍是两层：Responses、Chat
Completions、Anthropic Messages 属于 provider wire protocol；ACP、app-server、
RPC 与 JSONL 属于 agent/runtime protocol。

因此不能把某个模型 API 或 agent 名写进通用 Orchestrator，也不能把 Host
固化成单一 native runtime。

## 2. 决策

### 2.1 HostBackend 分层

- `HostAgent` 继续承载路由、直接回答、角色提取、讨论主持与总结语义。
- `HostBackendSelection` 是 room 级持久选择，只有 `model` 与 `agent` 两类；
  不保存凭据、runtime session id 或 provider 对象。
- model backend 由 `NativeAgentRuntime` 驱动；它只依赖中立
  `ModelProvider/ModelEvent`，负责流式输出、上下文、单 writer、取消、静默超时、
  权威终态与 no-replay。
- agent backend 只能来自具体 adapter 明确声明的 `AgentHostCapability`。声明同时
  给出真实 transport 与全新只读实例 factory；`AgentSpec` 不重复维护 Host 配置。
  当前七个生产 adapter 均已声明，通用层按 capability 查找，不按 agent 名分支。
- `@codex` worker 与 Codex Host 每次由不同 factory 构造，拥有不同 adapter、
  process/thread/session/writer；两者绝不复用。

内置默认选择仍是 `model/profile:default`，但不是唯一生产 Host。用户可在私有全局
配置中声明 `[host.backend]`，仅覆盖此后新建 room 的初始选择；已有 room 的持久
选择永远优先，不随全局默认漂移。

### 2.2 会话级命令与切换生命周期

本地命令不进入 timeline：

```text
/host
/host model <profile-or-exact-model-id>
/host agent <agent>
```

`/host` 显示类型、选择、实际模型/agent、transport 与 readiness。切换只在当前
room 生效并写入 `state.json.host_backend`；其他 room 不变。切换必须发生在 Host
空闲/阶段边界：TUI 有运行或排队任务时拒绝，Orchestrator 的 Host delivery lock
也做原子拒绝。

成功切换的顺序为：验证候选并构造惰性新实例 → 有界关闭旧 runtime → 原子持久化
新选择并将 `agents.host` 写为
`{cursor: 当前 timeline 边界, session_id: null}`，同时持久化不可回退的
`host_replay_floor` → 发布新实例。新 backend 的任何 fresh session 都只允许
从该 floor 之后取增量。该 floor 单调推进：切换时至少推进
到当前 timeline 边界，此后每个 `delivery_committed` 都与 Host cursor 原子推进，
所以重启或 session 恢复失败也不会重放已投递 turn。fresh session 不 bootstrap
floor 之前的历史；因此既不恢复
旧 provider/agent session，也不跨 backend 重放已投递或不确定 turn。关闭或持久化
失败时不伪装成功；候选被回收，原选择保持。旧 runtime 未能确认关闭时不创建第二
writer；持久化失败且旧 runtime 已关闭时才以 fresh runtime 恢复原选择。

恢复房间时若已选择的 backend 不可用，选择保持但 Host 标记未就绪并在 timeline
之前阻断；禁止偷偷切回默认模型或其他 agent。

### 2.3 模型 profile 与 provider capability

配置文件默认是 `$XDG_CONFIG_HOME/myagents/config.toml`，未设置时为
`~/.config/myagents/config.toml`。保留 `[host.model]` 作为兼容 default profile；
推荐使用命名 profile：

```toml
[host.backend]
kind = "agent"
target = "opencode"

[host.models.local]
provider = "openai-compatible"
base_url = "http://127.0.0.1:1234/v1"
model_id = "从 /v1/models 选择的精确 id"
models_discovery = true

[host.models.glm]
provider = "openai-compatible"
base_url = "由用户明确配置的服务地址"
model_id = "glm-5.3-flash"
api_key_env = "ZAI_API_KEY"
models_discovery = false
```

这里的 `glm-5.3-flash` 是用户指定、由实际服务首个 chat 请求接受或拒绝的精确
字符串；本文不据此声明任何厂商公开模型枚举。

模型发现是 `ModelProviderCapabilities.models_discovery`，不是所有
OpenAI-compatible provider 的强制能力：

- discovery=true 时，请求前调用 `/models` 并精确匹配完整 id；
- discovery=false 时，不伪造或请求模型清单，直接使用 profile 中的 exact
  `model_id`，由首次 chat 请求的 2xx/非 2xx 结果验证；服务端非 2xx 是可操作的
  pre-commit 拒绝，POST 后无响应则按不确定投递建立 no-replay 边界。

命名 profile 只保存 `api_key_env`，不保存 key。凭据只从当前进程环境解析并进入
Authorization header，不进入 selection、repr、readiness、timeline 或错误。
`[host.model]` 与命名 profile 都拒绝持久化 `api_key`；本地文件只保存
`api_key_env` 引用。`MYAGENTS_MODEL_API_KEY` 仅作为当前进程的临时覆盖，绝不
写入房间状态或时间线。

配置文件必须是非符号链接普通文件、权限精确 0600、最大 64 KiB。base URL 不得
内嵌凭据、query 或 fragment。readiness 只读本地配置/CLI 属性，不联网、不启动
服务、不修改用户配置。

### 2.4 权限

- model Host 永久 `tool_policy=none`，请求不携带 tools/tool_choice。
- agent Host 的每次 `stream/stream_prepared` 都由 `HostAgent` 强制
  `ExecutionMode.READ_ONLY`，不继承 worker/TUI permission handler。
- capability factory 必须返回与 `AgentSpec` 身份一致的全新 adapter，不能复用同名
  worker 的 process/session/writer。
- Codex Host factory 额外固定 `sandbox=read-only`、`approval_policy=never`、
  `fallback_jsonl=false`。
- OpenCode Host factory 固定 `fallback_jsonl=false`，并由既有 read-only execution
  profile 在 runtime 层 deny 未知/有副作用工具，只放行安全读取与本地只读索引。
- Kimi ACP 尚无独立获证的 runtime hard-deny profile，因此 Host capability 明确选择
  项目固定的 Read/Grep/Glob JSONL profile；这不是 worker ACP 失败后的跨协议重放。
- Qwen、CodeBuddy、DSH 与 Pi Host 复用各 adapter 已验收的只读 execution profile，
  但仍创建独立 runtime/session。
- `/yolo` 不能放宽任一 Host profile。未知 agent、无 Host capability、未就绪
  target 均明确 block。

### 2.5 错误与 no-replay

不存在跨 model/agent backend 的自动 fallback。候选不可用、模型 id 被拒绝、
配置损坏与旧 runtime 关闭失败均保留当前选择并显示可操作错误。

原生 provider 通过中立 `ModelDeliveryState` 显式公开当前请求的
not-attempted/attempted/committed/rejected 状态，而不是依赖具体实现的私有属性。
在 2xx headers 后产生 `committed`；随后取消、静默超时、断流、
缺少 `[DONE]` 或 `finish_reason` 均是不确定终态。POST 已尝试但 headers 丢失也
保守建立 no-replay。只有正常完成才把 user/assistant 加入 provider context。

## 3. 验收

自动验收覆盖：

1. native → agent Host → native，旧 runtime 有界关闭且每次 fresh；全部生产
   adapter 声明 capability，factory 都使用独立只读实例；
2. room 隔离与重启恢复；Host 写入跨 backend cursor 边界并清空 session，
   不碰 `@codex` worker；
3. 运行中拒绝、unknown/unready block、持久 unready 不自动 fallback；
4. Host/worker 双实例单 writer，agent Host 强制 read-only，`/yolo` 反例；
5. discovery 与 no-discovery provider、`glm-5.3-flash` 精确透传、非 2xx pre-commit；
6. `[host.backend]` 只初始化 fresh room，已有 room 不漂移；`/host` TUI 展示/切换
   不进 timeline，显式 `@worker` 不回归；
7. 原有取消、超时、断流、secret、配置权限、讨论和 workflow tests 全部回归。

主要证据：`tests/test_host_backend.py`、`tests/test_native_agent.py`、
`tests/fake_openai_compatible_server.py`、`tests/test_tui_completion.py`。

真实 LM Studio 证据包括 2026-08-27 的原始探针，以及 2026-08-29 对精确模型 ID
`google/gemma-4-e4b` 的只读 `/v1/models` 检查与一次有界最小对话：生产 runtime
收到 `delivery_committed`、2 个 text chunk 和权威 done，正文为 `OK.`。本次没有
调用真实 GLM，也没有读取或消耗远程凭据/额度。

## 4. 后果与非目标

- 基本主持能力仍可完全不依赖第三方 Agent CLI；需要时可显式选择受约束的完整
  agent Host。
- room timeline/cursor 仍是持久事实，不新增第二套记忆数据库。
- 新 provider 作为 `ModelProvider` 实现加入；新 agent Host 通过具体 adapter 的
  `AgentHostCapability` 加入，`AgentSpec` 只保留 worker 注册事实，均不得污染通用路由。
- 完整原生 coding 工具集、自动 backend fallback、模型别名猜测、自动迁移
  profile、成本预算和上下文压缩不在本阶段。

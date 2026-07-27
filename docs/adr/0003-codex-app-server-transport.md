# ADR-0003：Codex app-server 长连接传输

- 状态：Accepted
- 日期：2026-07-27
- 里程碑：M4
- 作者：Bryant Yang

## 1. 背景

M0 的 Codex adapter 每轮执行一次 `codex exec --json`。这种 JSONL 路径简单，
但每轮都会重新启动 CLI、初始化插件/MCP，并依靠编排器重复发送 transcript；
问候和短消息也承担完整冷启动成本。它同时无法把 app-server 的 turn、tool、
approval 与 interrupt 生命周期完整映射到统一事件。

Codex 官方提供实验性的 `codex app-server`：默认通过 stdio 交换逐行 JSON，
消息形状接近 JSON-RPC 2.0，但省略 `jsonrpc` 字段。客户端先完成一次
`initialize` / `initialized`，之后可在同一进程与 thread 上连续执行多个
`turn/start`，并通过通知接收流式结果。

app-server 是 Codex 专用协议，不是 ACP。M4 不把它包装成“通用 ACP”，而是在
统一 `AgentAdapter` 契约下与 Kimi ACP 并列。

## 2. 决策

### 2.1 传输优先级

- Kimi 继续优先 ACP。
- Codex 优先 `codex app-server`。
- `codex exec --json` 保留为兼容 fallback，不再是生产默认路径。
- “ACP-first”扩展为“可靠官方长连接协议优先”；各厂商协议差异留在 adapter，
  编排器只依赖 `stateful_session`、`stream_prepared`、权限和生命周期能力。

### 2.2 进程与会话所有权

- 一个 `CodexAppServerAdapter` 独占一个 app-server 进程和一个 thread。
- 同一 adapter 的 turn 严格串行；不同 adapter 可以并发。
- host 与 `@codex` worker 各自持有 adapter，不共享 thread，避免角色上下文和
  权限边界相互污染。
- 同一进程至少服务两个连续 turn；正常 turn 结束不关闭进程。
- `aclose()` 必须终止整个进程组并使全部 pending request 失败。

### 2.3 默认配置继承

app-server 启动命令不得添加 `--model`、`-c` 或 reasoning/plugin/MCP 覆盖；
`thread/start` 与 `turn/start` 也不得发送 `model`、`effort`、`config`、
`collaborationMode` 等覆盖字段。模型、推理强度、plugin 和 MCP 全部继承用户
现有 Codex 默认配置。

允许发送的运行时字段仅限协议必需数据、`cwd` 与已经存在的产品安全边界：
host 使用 `read-only`，worker 使用 `workspace-write`。这些 sandbox 值只作用于
该 thread，不写入 `~/.codex/config.toml`。

### 2.4 事件映射

- `item/agentMessage/delta` → `AgentEvent("text")`
- `item/started` / `item/completed` 的 command、file change、MCP/tool 项 →
  有界、脱敏的 `tool` / `status`
- app-server 反向 approval request → `permission`，无处理器默认拒绝
- `turn/completed` 的 completed / interrupted / failed →
  `done` / cancel confirmation / `error`
- reasoning 正文不进入 TUI 或持久事件，只允许安全阶段摘要

`turn/completed` 是本轮结束的唯一权威信号；`turn/start` response 只表示本轮
已经建立，不能提前视为完成。

### 2.5 取消与断线

- 取消活跃流时发送 `turn/interrupt(threadId, turnId)`，继续等待匹配的
  `turn/completed`。
- 有限时间未确认时关闭连接并在下一轮重建；writer lock 在确认停止或连接关闭
  前不释放，禁止新旧 turn 重叠。
- stdout EOF、非法协议帧或进程退出必须使 pending request 和活跃 turn 立即
  失败，不得留下“处理中”假状态。

### 2.6 fallback 语义

- initialize 或 thread prepare 阶段失败可以回退到 `codex exec --json`；
  此时尚未提交用户 turn，即使空 thread 已在服务端建立也没有工具副作用。
- 一旦发送 `turn/start` 就不自动回退：响应丢失时无法证明服务端是否已接受并
  开始执行。无论是否已收到流式事件或 approval，都诚实失败并作废连接，下一轮
  再重建，绝不冒险重复工具副作用。
- `turn/start` 已发送后的所有非成功退出——结果不确定、terminal failed /
  interrupted、用户取消——都先将本轮输入推进为持久 no-replay 边界，再公开
  错误、取消或写 timeline。服务端明确拒绝或确认未发送的失败不建立该边界，
  仍按普通失败重试。
- `turn/start` 明确接受时，adapter 先产生内部投递确认；Orchestrator 据此在
  正文、工具、权限等任何外部 event sink 回调之前持久化 cursor。这样即使
  `events.jsonl` 或 UI callback 失败，也不会把已开始执行的 turn 重新投递。
- no-replay 后续优先恢复原 thread；若恢复失败而新建 thread，也不自动
  bootstrap 已投递过的 Codex transcript。这里宁可损失旧上下文，也不重复
  潜在工具副作用。
- fallback 是可观测事件，不能静默伪装成 app-server 成功。

### 2.7 实验协议防漂移

- fake server contract tests 固定当前使用的最小 method/field 集。
- 生产实现只依赖生成 schema 中的稳定核心方法，不开启 experimental API。
- Codex CLI 升级后若 contract test 或真实探针失败，优先更新 adapter/schema
  证据；不得靠吞掉未知错误维持假成功。

## 3. 验收

1. fake app-server 验证 initialize、thread/start、两个连续 turn 共用 PID/thread。
2. 验证正文、tool、permission、completed/failed 映射。
3. 验证默认拒绝 approval、取消确认、断线 pending 失败和 close 无残留。
4. 请求抓包证明无 model/effort/config/plugin/MCP 覆盖。
5. 真实 Codex 临时目录 E2E 连续两轮共用一个 app-server PID/thread，退出后无
   残留；真实模型测试不进入默认快速 gate。
6. JSONL fallback 单独保留回归证据，且禁止对已发送 `turn/start` 自动重放。

## 4. 后果

- 短消息不再重复承担 Codex CLI、plugin 与 MCP 的进程冷启动成本。
- Codex 上下文由 thread 原生保持，编排器可复用已有 stateful 增量投递规则。
- 代码库同时维护 ACP、Codex app-server 和 JSONL fallback 三种 transport；
  统一点是 `AgentAdapter` 行为契约，不是底层 wire protocol。
- app-server 仍是实验接口，Codex 升级需要运行 contract tests 与真实探针。

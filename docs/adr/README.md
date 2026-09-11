# 架构决策记录（ADR）

本目录记录会改变协议、安全边界、持久化语义或进程所有权的决策。状态为
`Accepted` 的 ADR 是实现与评审的输入；修改其核心语义必须新增 ADR 取代，
不能只改代码。

| ADR | 状态 | 决策 |
| --- | --- | --- |
| [0001](0001-persistent-room-command-bus-mcp.md) | Accepted | M2.5 持久房间、单写者 command bus 与本机 MCP 入口 |
| [0002](0002-durable-execution-observability.md) | Accepted | M3.1 持久执行事件、心跳、权限上下文与精确取消 |
| [0003](0003-codex-app-server-transport.md) | Accepted | M4 Codex app-server 长连接、默认配置继承与安全 fallback |
| [0004](0004-ephemeral-codex-host-threads.md) | Superseded | M4.1 历史 Codex host ephemeral thread；生产 host 由 ADR-0017 取代 |
| [0005](0005-project-conversation-sessions.md) | Superseded | M4.2 独立命名会话身份；TUI 生命周期由 ADR-0010 取代 |
| [0006](0006-kimi-hybrid-transport-policy.md) | Accepted | M4.4 Kimi ACP-first、prepare-only 只读 JSONL fallback 与 no-replay |
| [0007](0007-opencode-hybrid-transport-policy.md) | Accepted | M4.5 OpenCode ACP-first、ask-by-default 权限收口与只读 JSONL fallback |
| [0008](0008-bounded-multi-agent-discussion.md) | Accepted | M5.1 `/discuss` 有界轮次、失败收口与终局主持 |
| [0009](0009-bounded-milestone-workflow-steering.md) | Accepted | M5 review → 修改 → 复核闭环与阶段边界 steering |
| [0010](0010-multi-session-tui-management.md) | Accepted | M4.7 会话目录、后台执行、资源 gate 与图片短引用 |
| [0011](0011-session-scoped-natural-language-roles.md) | Accepted | M6 自然语言指定会话级角色、持久恢复与安全边界 |
| [0012](0012-agent-readiness-and-setup-ux.md) | Accepted | M4.10 Agent 就绪探测、派发资格门与缺失安装体验 |
| [0013](0013-natural-language-sequential-collaboration.md) | Accepted | M7 自然语言有序协作计划、串行接力与失败收口 |
| [0014](0014-pi-rpc-permission-bridge.md) | Accepted | M4.11 Pi 原生 RPC、唯一权限 bridge、wrapper 工具闭集与三 profile 隔离 |
| [0015](0015-dsh-acp-only-transport.md) | Accepted | M4.12 标准 myagents bundle + stock `dsh --profile myagents`、不可变 DSH、两 execution safety profile、stateful lifecycle gate 与零 fallback |
| [0016](0016-explicit-auto-approve-mode.md) | Accepted | M4.13 `/yolo` 会话级自动批准、只选 allow-once、持续危险提示与 read-only 硬边界 |
| [0017](0017-native-model-backed-host.md) | Accepted | M4.14 会话级 HostBackend、原生模型 runtime 与独立只读 agent Host |
| [0018](0018-capability-bounded-runtime-interjection.md) | Accepted | Esc 精确取消、Alt+↑ 能力受限插话、Pi native steer 与 no-replay |
| [0019](0019-first-class-collaboration-plan-projection.md) | Accepted | 版本化协作计划事件、逐步骤交接视图与重启详情恢复 |
| [0020](0020-capability-bounded-context-lifecycle.md) | Accepted | capability-first 上下文预算、原生 Host 压缩与持久恢复 |
| [0021](0021-detachable-daemon-and-remote-companion.md) | Accepted | 单 owner 后台 daemon、可重新附着 TUI 与 loopback deny-only remote companion |
| [0022](0022-claude-code-stream-json-transport.md) | Accepted | M4.15 Claude Code headless stream-json 长连接、MCP 权限桥、两 profile 与零 fallback |

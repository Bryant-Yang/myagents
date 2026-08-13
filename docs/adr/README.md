# 架构决策记录（ADR）

本目录记录会改变协议、安全边界、持久化语义或进程所有权的决策。状态为
`Accepted` 的 ADR 是实现与评审的输入；修改其核心语义必须新增 ADR 取代，
不能只改代码。

| ADR | 状态 | 决策 |
| --- | --- | --- |
| [0001](0001-persistent-room-command-bus-mcp.md) | Accepted | M2.5 持久房间、单写者 command bus 与本机 MCP 入口 |
| [0002](0002-durable-execution-observability.md) | Accepted | M3.1 持久执行事件、心跳、权限上下文与精确取消 |
| [0003](0003-codex-app-server-transport.md) | Accepted | M4 Codex app-server 长连接、默认配置继承与安全 fallback |
| [0004](0004-ephemeral-codex-host-threads.md) | Accepted | M4.1 Codex host 使用不落盘的 ephemeral thread |
| [0005](0005-project-conversation-sessions.md) | Superseded | M4.2 独立命名会话身份；TUI 生命周期由 ADR-0010 取代 |
| [0006](0006-kimi-hybrid-transport-policy.md) | Accepted | M4.4 Kimi ACP-first、prepare-only 只读 JSONL fallback 与 no-replay |
| [0007](0007-opencode-hybrid-transport-policy.md) | Accepted | M4.5 OpenCode ACP-first、ask-by-default 权限收口与只读 JSONL fallback |
| [0008](0008-bounded-multi-agent-discussion.md) | Accepted | M5.1 `/discuss` 有界轮次、失败收口与终局主持 |
| [0009](0009-bounded-milestone-workflow-steering.md) | Accepted | M5 review → 修改 → 复核闭环与阶段边界 steering |
| [0010](0010-multi-session-tui-management.md) | Accepted | M4.7 会话目录、后台执行、资源 gate 与图片短引用 |
| [0011](0011-session-scoped-natural-language-roles.md) | Accepted | M6 自然语言指定会话级角色、持久恢复与安全边界 |
| [0012](0012-agent-readiness-and-setup-ux.md) | Accepted | M4.10 Agent 就绪探测、派发资格门与缺失安装体验 |
| [0013](0013-natural-language-sequential-collaboration.md) | Accepted | M7 自然语言有序协作计划、串行接力与失败收口 |

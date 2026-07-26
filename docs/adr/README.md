# 架构决策记录（ADR）

本目录记录会改变协议、安全边界、持久化语义或进程所有权的决策。状态为
`Accepted` 的 ADR 是实现与评审的输入；修改其核心语义必须新增 ADR 取代，
不能只改代码。

| ADR | 状态 | 决策 |
| --- | --- | --- |
| [0001](0001-persistent-room-command-bus-mcp.md) | Accepted | M2.5 持久房间、单写者 command bus 与本机 MCP 入口 |

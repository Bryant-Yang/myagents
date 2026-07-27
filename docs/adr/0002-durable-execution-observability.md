# ADR-0002：持久执行可观测性与精确取消

- 状态：Accepted
- 日期：2026-07-27
- Owner：Bryant Yang
- 里程碑：M3.1

## 1. 背景

M3 能提交命令并查询 queued/running/terminal，但 agent 在一轮内长时间分析、
调用工具或等待权限时，TUI 和外部 MCP host 看不到足够上下文。若进程重启，
内存中的流式状态也会消失，形成“后台悄悄做事、最后突然申请权限”的体验。

这不是自然语言路由问题，也不能靠为 `hi` 等输入建立关键词白名单解决。
Codex host 的模型、reasoning、plugin 与 MCP 配置继续继承用户默认配置。

## 2. 决策

### 2.1 执行事件与对话时间线分离

每个持久房间新增 `events.jsonl`。它是 append-only 执行日志，记录：

- queued / running / completed / failed / cancelled 生命周期；
- 安全的阶段 status 与静默 heartbeat；
- tool 标题和有界命令上下文；
- permission 请求及用户选择结果；
- partial 正文块。

事件字段固定为 `seq`、`command_id`、`agent`、`kind`、`text`、
`created_at`。事件绝不进入 agent history，避免把运行噪声反馈给模型。
M3 旧房间在 state/timeline 通过完整校验后补建空 `events.jsonl`；已有事件
文件损坏必须 fail loudly。

### 2.2 安全可见性

ACP `agent_thought_chunk` 只映射为“正在分析”等阶段状态，不显示 thought
正文。工具事件显示 title、status 与有界 command；常见 token、secret、
password、authorization 字段在权限 UI 中隐藏。权限弹窗必须显示工具上下文，
不再只有模糊标题。

CommandBus 在连续 10 秒没有 agent 事件时发 heartbeat，并持续记录静默时长。
ACP 连续 120 秒无协议活动仍按既有契约取消并重建不可信连接。

### 2.3 精确取消

CommandBus 为每个 active command 持有独立 dispatch task：

- queued command 可直接标记 cancelled，worker 取到后跳过；
- running command 只取消自己的 dispatch，不杀死 bus worker；
- terminal command 的 cancel 幂等返回原状态；
- TUI `Ctrl+X` 与 control `command.cancel` 复用同一原语。

取消后 worker 必须继续处理下一条命令。TUI 重启时，若某 command 的最后持久
事件不是 terminal，显示“上次任务已中断”及最后状态，不伪装为完成。

### 2.4 外部协议

私有 control socket 新增：

| method | 语义 |
| --- | --- |
| `events.read` | 按 `after_seq` / `limit` 读取持久执行事件 |
| `command.cancel` | 精确取消 queued/running 命令 |

MCP bridge 对应新增 `myagents_read_events` 与
`myagents_cancel_command`，总计七个工具。权限所有权仍在 TUI，bridge
不能授权工具。

## 3. 验收

1. ACP thought 正文不可见；阶段、工具标题和命令上下文可见。
2. 静默任务产生 heartbeat；事件经重启仍可读取。
3. 权限请求前 TUI 显示工具上下文，请求与结果均写入事件日志。
4. active/queued 可精确取消，bus worker 继续服务下一条命令。
5. TUI `Ctrl+X`、control 与 MCP 使用同一取消语义。
6. M3 旧房间可无损补建事件日志；损坏事件日志拒绝打开。
7. Codex host 不添加模型、reasoning、plugin、MCP 或自然语言关键词覆盖。

## 4. 后果

执行状态现在可被 TUI 与外部 agent 共同观察和干预，同时不会污染共享对话。
事件日志会随使用增长；压缩、保留周期与跨机器传输不属于 M3.1。

# ADR-0002：持久执行可观测性与精确取消

- 状态：Accepted
- 日期：2026-07-27
- 补充：2026-08-11（每任务活动摘要卡与逐卡键盘导航）
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

工具更新是状态流，不是聊天流：

- adapter 以 `tool_call_id` 为 identity，保存初始脱敏 title/command，并只输出
  可见状态迁移；缺 ID 时使用脱敏标题作为可见 identity；
- CommandBus 在刷新 activity 时钟后，对完全相同的 status/tool 做第二层去重，
  防止任一 producer 刷爆 UI 与 `events.jsonl`；
- adapter 对没有可见增量的重复工具 update 发内部 activity 事件；
  CommandBus 只刷新静默时钟，不向 UI 转发或持久化；
- TUI 在工具 identity 去重之上，再让同一 command 只占一张活动卡；折叠态只
  显示 command 终态、当前阶段与工具汇总。`Ctrl+G` 聚焦活动区并默认选中
  最近卡，`↑↓` 循环选择，`Enter` 独立展开或收起当前卡，`Esc` 返回输入框；
  `/details` 切换当前选中卡，否则只切换最近卡。展开内容包含阶段、heartbeat、
  权限、工具终态和脱敏命令；协议后续补发 tool ID 时只迁移显式标记为标题
  fallback 的原逻辑项，opaque 正式 ID 不用字符串启发式猜测；被明细窗口裁掉的
  工具若曾失败、拒绝或取消，卡片继续保留历史异常位；
- 活动模型及逐卡展开态按 room_id 隔离，非活动房间的事件也更新其模型；命令终态后近期卡
  冻结为可展开安全摘要。每卡最多保留最近 50 个工具明细，每 room 最多保留
  最近 100 张可展开终态卡，更早卡只留折叠归档；历史事件保持 append-only，
  是完整事实源。后台 runtime 被 idle reap 时一并释放其 UI feed。用户消息、
  agent 正文和失败仍留在聊天主线，不能被折叠卡隐藏。

CommandBus 在连续 10 秒没有 agent 事件时发 heartbeat，并持续记录累计静默
时长。heartbeat 不是 agent 活动，不得重置静默计时；TUI 在同一 command 活动
卡内原位更新，避免每 10 秒追加一条聊天记录。heartbeat 同时标明最近活动阶段
（host 或 worker）。

`completed` 是 CommandBus 的调用终态，只表示本轮 agent/host 调用正常结束，
不证明自然语言任务已经验收。TUI 对 adapter `done` 显示“本轮响应结束”，不得
显示“agent 完成”。
fan-out 中任一 worker 失败时，CommandBus 等其他 target 收尾后将 command
标为 `failed`；失败前已有 partial 时必须同时标注调用失败。
TUI 固定任务区独立保留每个 agent 的阶段和终态；总 command 失败但同时存在
成功与失败 agent 时显示“部分完成”，并在运行期显示累计耗时和取消入口。

有状态 transport 仅在不等待人工权限时应用 inactivity watchdog：普通静默统一
为 300 秒，早期 plan/status 只重置计时；ACP 与 Pi RPC 在工具已创建且尚未终止
时使用 15 分钟，Codex app-server 当前仍使用普通预算。独立工具预算用于工程
子代理和长命令，任何协议更新仍会重置计时。该轮已提交，因此 timeout 必须建立
no-replay cursor，不能自动补发。

### 2.3 精确取消

CommandBus 为每个 active command 持有独立 dispatch task：

- queued command 可直接标记 cancelled，worker 取到后跳过；
- running command 只取消自己的 dispatch，不杀死 bus worker；
- cancel_requested 只表示取消已发出，TUI 在 CommandBus 确认 terminal 前保持
  运行态和工具状态；
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
`myagents_cancel_command`，在 M3.1 当时总计七个工具；M5 后续增加
`myagents_steer_command`，当前总计八个。权限所有权仍在 TUI，bridge 不能
授权工具。

## 3. 验收

1. ACP thought 正文不可见；阶段、工具标题和命令上下文可见。
2. 静默任务产生累计 heartbeat；同一 command 在 TUI 只占一张活动卡；
   事件经重启仍可读取。
3. 权限请求前 TUI 显示工具上下文，请求与结果均写入事件日志。
4. active/queued 可精确取消，bus worker 继续服务下一条命令。
5. TUI `Ctrl+X`、control 与 MCP 使用同一取消语义。
6. M3 旧房间可无损补建事件日志；损坏事件日志拒绝打开。
7. Codex host 不添加模型、reasoning、plugin、MCP 或自然语言关键词覆盖。
8. `done/completed` 的用户文案不声称任务已验收。
9. 人工权限等待超过 inactivity timeout 不误取消；权限结束后恢复普通计时。
10. fan-out 任一 worker 失败时其他 target 仍收尾，command 终态为 `failed`。
11. 高频相同 tool update 不重复转发、持久化或重绘；真实状态迁移、工具标题、
    脱敏命令和终态仍完整可见。
12. 固定任务区显示每个 agent 的阶段；混合终态显示“部分完成”，terminal 后
    不再显示取消提示。
13. 活跃工具超过普通 300 秒阈值不会被误杀；独立工具 watchdog 到期仍按
    no-replay 失败处理。
14. 折叠时不显示工具命令；`Ctrl+G`、`↑↓`、`Enter`、`Esc` 可完成逐卡键盘
    浏览，`/details` 只切换当前或最近卡；展开后显示脱敏命令和最近过程，错误
    仍作为独立聊天正文可见。
15. 多 agent 交错更新时摘要焦点跟随最后活动者；切走会话、后台完成再切回后
    近期活动卡仍可展开；工具和终态卡超限后按固定窗口有界归档。

## 4. 后果

执行状态现在可被 TUI 与外部 agent 共同观察和干预，同时不会污染共享对话。
事件日志仍会随有信息增量的事件增长；历史压缩、保留周期与跨机器传输不属于
M3.1。旧版本已写入的重复记录不做破坏性回写。

# ACP 迁移设计（最小方案）

状态：**Phase 4 已完成**。`AgentAdapter` 是 TUI 与不同 coding agent 的
统一行为契约；wire protocol 按厂商能力选择：`acp/` 是通用 ACP runtime，
Kimi 走 `kimi acp`；Codex 走官方 `codex app-server`；OpenCode 与旧 Codex
adapter 保留 JSONL fallback。app-server 不是 ACP，二者只在 adapter 层统一。
Phase 2.5 落地了共享 history 持久化、ACP
session 映射与重启恢复、房间单写者 lease；Phase 3 落地了内部
command bus（`control/`）、私有 Unix 控制 socket 与 stdio MCP 外部入口
（`myagents_mcp.py`）——本机其他 agent 可以向运行中的同一房间注入
消息，单写者与 TUI 权限决策不变。多-agent 路由仍由
orchestrator 管——ACP 只负责传输，不参与"派给谁"的决策。

## 为什么迁移

私有 JSONL adapter（`kimi -p` 等无头模式）是"一次性命令"：每次调用新进程、
新会话，上下文靠 transcript 转发（token 线性膨胀），无法中断、无法恢复会话、
权限只能一把梭。ACP 是有状态协议，一次解决这四件事。

## 实测确认的协议形状（kimi acp，protocolVersion 1）

```
→ {"id":0,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{...}}}
← {"id":0,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true,
    "sessionCapabilities":{"list":{},"resume":{}},...},"agentInfo":{...}}}

→ {"id":1,"method":"session/new","params":{"cwd":"/tmp","mcpServers":[]}}
← {"id":1,"result":{"sessionId":"session_xxx"}}

→ {"id":2,"method":"session/prompt","params":{"sessionId":"...",
    "prompt":[{"type":"text","text":"..."}]}}
← {"method":"session/update","params":{"sessionId":"...","update":
    {"sessionUpdate":"agent_thought_chunk|agent_message_chunk|tool_call|...",
     "content":{"type":"text","text":"..."}}}}   （若干条，流式）
← {"id":2,"result":{"stopReason":"end_turn"}}

← {"id":N,"method":"session/request_permission","params":{"sessionId":"...",
    "toolCall":{...},"options":[{"optionId":"...","kind":"allow_once|reject_once",...}]}}
→ {"id":N,"result":{"outcome":{"outcome":"selected","optionId":"..."}}}
   或 {"outcome":{"outcome":"cancelled"}}

→ {"method":"session/cancel","params":{"sessionId":"..."}}   （通知，无响应）
```

## 架构（Phase 2 现状）

```
orchestrator.py（AgentAdapter 接口不变；AGENT_SPECS 注册表）
   │  AgentSpec(name, transport, factory)
   │    kimi     → acp   → AcpKimiAdapter（首个生产 ACP agent）
   │    codex    → app-server → CodexAppServerAdapter（原生 thread/turn）
   │    opencode → jsonl      → OpenCodeAdapter
   │                              CodexAdapter 作为安全 JSONL fallback
   │
   ├─ acp/adapter.py  AcpAdapter（通用：name + cmd 即一个 ACP agent；
   │     │            stateful_session = True 声明"会话在 agent 侧保持"）
   │     │  持有 session，串行化轮次（asyncio.Lock，aclose 同锁）
   │     ▼
   │   acp/client.py  AcpClient（协议层，与具体 agent 无关）
   │     JSON-RPC 2.0 / NDJSON / id 关联 / 反向请求 / killpg 清理
   │
   ├─ codex_app_server/  Codex 专用 JSONL-over-stdio client/adapter
   │                     （不是 ACP；无 jsonrpc header）
   └─ adapters/*_adapter.py  一次性 JSONL fallback
```

新增一个 ACP agent 不需要改编排器：写一行 `AgentSpec` 即可。
协议判断只看 `transport` / adapter 能力声明（`stateful_session`、
`set_permission_handler`、`aclose`），不散落 `if name == "..."`。

## 铁律：会话唯一持有者

一个 session 同一时刻只能有一个 writer。违反就会话损坏：

- `AcpAdapter` 实例是它 session 的唯一 writer（`_lock` 串行化轮次）
- 该 session id 不得交给普通 kimi TUI 或另一个进程并发使用
- `session/load` 恢复旧会话前，确认没有别的进程持有它

## 增量上下文契约（Phase 2 新增）

ACP session 是持久上下文，编排器**不再**每轮转发完整 transcript：

- 编排器为每个有状态 agent 维护一个 history cursor
  （`Orchestrator._cursors`），记录已交付到共享时间线的哪个位置。
- 每轮只发 cursor 之后的新消息：用户的新发言 + 其他 agent 的新回复。
  这是共享多-agent 时间线，不能只发孤立的最新一条。
- agent 自己的回复按 speaker 过滤、不重发——它们本来就在它的 ACP
  session 里，重发就是重复上下文。
- **首次 bootstrap 限界**：cursor=0 的首次派发只发最近 `history_limit`
  条（仍过滤自身），避免长聊天后第一次 @ 就无界发送全部 history；
  成功后 cursor 直接推进到本轮快照末尾，后续继续走纯增量。
- **delivery lock（P1 契约）**：读 cursor → 选增量 → stream 完整执行 →
  推进 cursor 是原子单元，在该 agent 的 delivery lock 内完成，prompt
  拿到锁之后才构造。同一 agent 的并发 dispatch 严格串行（不重复、
  顺序保持）；不同 agent 持不同的锁，并行扇出不受影响。
- **cursor 成功交付后推进**，且只推进到本轮构造时的快照末尾（本轮进行期间
  到达的新消息留给下一轮）。prompt 提交前或服务端明确拒绝的失败不推进，
  下一轮从旧 cursor 补发；prompt 已提交后的 inactivity timeout 等结果不确定
  失败必须先提交同一快照末尾的 no-replay cursor，再公开失败，避免工具任务
  被重复执行。
- 无状态 JSONL fallback 不受影响：仍用 dispatch 瞬间的完整 transcript 快照
  （最近 12 条，防并发串话）。

## 持久化与 session restore（Phase 2.5）

RoomStore（`storage/store.py`）把房间状态落盘到
`${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/<room_id>`：

- **timeline.jsonl**：append-only，记录单调 `seq`、speaker、text、UTC
  `created_at`、可选 `command_id`；文本/记录有明确大小上限；损坏记录、
  seq 回退、半初始化房间全部 fail loudly，绝不静默覆盖。
- **state.json**：schema version + 规范化 workdir + 每个 stateful agent
  的 cursor/session_id；同目录临时文件 + `os.replace` 原子写；agents
  entry 打开时全量校验（缺 cursor/session_id、负 cursor、空 session id
  都在构造阶段抛 `CorruptedStorageError`）。
- **seq cursor**：cursor 从 list 下标改为持久 timeline seq。`_messages_for`
  按 `m.seq > cursor` 选增量，cursor=0 才走 `history_limit` bootstrap；
  成功交付或提交后结果不确定的 no-replay 边界，都先 `set_agent_state`
  落盘再更新内存；明确未提交的失败保持旧 cursor。
- **checkpoint-before-prompt**：ACP 轮次走
  `stream_prepared(make_prompt, workdir, resume_session_id)`——prepare
  （start/load/new）与 prompt 在同一 writer lock 生命周期内。make_prompt
  在任何 event/prompt 前执行：`fresh and not restored`（新 session）选
  cursor=0，load 成功或复用活跃 session 保留 cursor；一次
  `set_agent_state(cursor, session_id)` 原子落盘后才构造 prompt。
  checkpoint 失败抛内部 `_CheckpointError` 穿透 dispatch（零 prompt，
  adapter 按契约 reset fresh session），绝不伪装成 agent 调用失败。
- **session restore**：重启后 `resume_session_id` 取自已持久化状态。
  load 成功（restored）保留 cursor 继续增量；load 失败或 agent 不声明
  loadSession 回退 session/new，cursor 归零有界 bootstrap，实际新
  session id 落盘。
- **持久确认时序**：用户消息 append 成功后才发 `committed` 事件（TUI
  此时才显示用户文本）；agent 的 `done` 不再由 adapter 直接转发，只在
  最终回复落盘成功后的成功轮补发——append 失败无 done。
- **owner lease**：persistent Orchestrator 构造末尾 `acquire_owner()`
  （owner.lock，`flock(LOCK_EX|LOCK_NB)`，0600，写入 PID 供冲突提示）；
  冲突抛 `RoomBusyError`。`aclose()` 先置 closed 标志（新 dispatch 与
  排队 delivery 抛 `OrchestratorClosedError`），关闭 adapters 后在
  finally 释放 lease；不删除 owner.lock，stale 文件不妨碍下次获取。
  只读 RoomStore 辅助实例不获取 lease。

## 权限策略（Phase 2：进入 TUI）

client 声明 `fs/terminal` 能力为 false（不代理文件/终端）。
`session/request_permission` 的决策链：

1. **TUI 已挂载**（默认路径）：`main.py` 把异步决策回调注入所有 ACP
   adapter（`Orchestrator.set_permission_handler`），回调签名
   `async (agent_name, params) -> outcome`——通用多 agent runtime 里
   弹窗必须显示来源 agent（名字在 adapter 注入时绑定，client 层保持
   params-only）。弹窗显示工具标题和 agent 提供的 options，用户选
   allow / reject / cancel（Esc）。
2. **无权限处理器**（非 TUI / 测试 / 脚本调用）：一律 cancelled——
   安全拒绝是默认，不是配置缺失的意外。
3. **auto 放行**：只能显式 opt-in（`AcpClient(..., permission="auto")`），
   优先 allow_once。绝不隐式恢复。
4. **fail-closed 校验**：决策器返回值不可信——None、畸形 dict、缺
   optionId 的 selected、空 optionId、不属于本次 options 的 ID、决策器
   抛异常，一律按 cancelled 回应（`_validate_outcome`），绝不向 agent
   发无效 outcome。

等待用户决策期间不阻塞 read loop（独立 task 应答），并暂停 adapter 的
agent inactivity timeout；权限结果发回后重新开始普通静默计时。等待仍可取消——
TUI 退出时所有挂起的权限 Future 按 cancelled 收尾（`on_unmount` →
`_cancel_pending_permissions` → `orch.aclose()`，顺序不能反：aclose
等的锁可能被等权限的 prompt 持有），不留挂起 Future 或 `kimi acp`
子进程。权限后台 task 的异常由 done callback 消费（连接断开导致应答
发不出去时不会留下 "Task exception was never retrieved"）。

## 取消契约（Phase 1 建立，Phase 2 不变）

流被取消时：先发 `session/cancel`，再**等待**原 prompt 以 cancelled
结束（默认 10s 有限超时）；超时说明连接不可信，关闭并标记必须重建
（下轮 stream 重新 start + session/new）。adapter 的锁只在确认停止或
连接关闭后释放——下一轮 prompt 绝不与仍在执行的上一轮重叠。

不在等待人工权限且没有活跃工具时，prompt 连续 120 秒无 ACP 通知或终止响应
会触发 inactivity cancel；工具已创建且尚未进入终态时改用独立 15 分钟
watchdog，避免工程子代理和长命令被普通分析阈值误杀。由于 prompt 已经提交，
这不是安全重试点：
Orchestrator 必须先持久化 no-replay cursor，再记录调用失败。

## 生命周期（Phase 2 新增）

TUI 退出时 `Orchestrator.aclose()` 统一关闭所有支持 `aclose()` 的
adapter（`asyncio.gather`，一个关不掉不耽误其他）。close 与进行中的
prompt/session 初始化共用 adapter 的同一把锁，不竞态杀进程。

## 可见状态（Phase 2 新增）

- TUI 启动行显示每个 agent 的传输协议：`@kimi(ACP) @opencode(JSONL)
  @codex(APP-SERVER)`。
- ACP session id 建立后通过 info 事件展示一次（每次建立一次，不刷屏）。
- `tool_call` 保存脱敏后的 title/command；后续 `tool_call_update` 按
  `toolCallId` 继承上下文，只在 title/status/command/kind 的可见指纹变化时
  产出事件。完全相同的高频 `in_progress` 仍被视为协议活动，但不进入 TUI
  或持久日志；TUI 将同一工具的状态迁移原位更新，命令详情默认折叠并通过
  `/details` 切换。
- 固定任务区显示 command 总状态、累计耗时及每个 agent 的阶段/终态；
  fan-out 中既有成功又有失败时显示“部分完成”，不抹掉成功 agent 的事实。

## 阶段计划

- [x] **Phase 1**：`acp/client.py` + `acp/adapter.py` + fake server 回归测试
  （initialize/new/list/load/prompt/update/cancel/权限默认 deny/auto opt-in/
  取消串行化/超时重建/close-during-prompt/initialize 失败回收）
- [x] **Phase 2**：通用 ACP runtime 接入统一 TUI（kimi 为首个验收 agent）：
  `AgentSpec` 注册表（transport = acp/jsonl）；ACP 增量上下文
  （history cursor）；权限请求弹到 TUI 让用户决策；TUI 退出统一
  `aclose()`；协议状态可见。JSONL 保留为 fallback。
- [x] **Phase 2.5**：共享 history 持久化（timeline/state + seq cursor）、
  ACP session 映射与重启恢复（load 保留 cursor / 回退 new 有界
  bootstrap、checkpoint-before-prompt）、房间单写者 owner lease、
  持久确认时序与 close 排队保护
- [x] **Phase 3**：内部 command bus + 受控 MCP 外部入口（已完成）：
  `control/command_bus.py` FIFO 单 worker（request_id 永久幂等、容量
  硬上限、close 兜底 cancelled）；`control/server.py` 私有 Unix 控制
  socket（0600、stale 验证后清理、活跃不抢占、稳定错误码）；
  `myagents_mcp.py` stdio MCP bridge（官方 SDK `mcp>=1.27,<2`，七个
  `myagents_*` 工具）只连运行中 TUI 的 socket，绝不实例化第二
  Orchestrator、不获取 lease、不绕过 TUI 权限。细节见
  [ADR-0001](adr/0001-persistent-room-command-bus-mcp.md)
- [x] **Phase 4**：Codex 官方 app-server 接入：长驻进程、thread/turn、
  流式 item、approval、interrupt、恢复和安全 JSONL fallback；host 使用
  ephemeral thread，避免内部路由污染 Codex 历史。详见
  [ADR-0003](adr/0003-codex-app-server-transport.md)与
  [ADR-0004](adr/0004-ephemeral-codex-host-threads.md)。Claude 尚未注册；
  后续按其可靠官方协议单独接入，不把厂商协议强行伪装成 ACP。
- [ ] **Phase 5**：里程碑工作流、review → 修改 → 复核闭环与 steering

A2A 不在当前阶段；Streamable HTTP、远程认证同样不在 M3（M3 是单机
单用户 stdio 集成，见 ADR-0001 §3）。只有出现跨机器、跨组织 agent
协作需求时再评估。

## 风险与注意

- **真实 `kimi acp` 已端到端验证**（Phase 2 收尾时实测）：真实二进制 +
  Textual TUI 的无工具回合正常；真实 Bash 权限请求弹窗正常，实际 options
  为 allow_once / allow_always / reject_once，选择 allow_once 后工具
  执行结果正确；退出后无残留进程。
- **M3 真实 E2E 已通过**（2026-07-26）：
  `scripts/e2e-m3-real.py` 用真实 `kimi acp` + 独立 MCP stdio client
  完成两轮 TUI 生命周期，第二轮 `session/load` 复用同一 session id，
  timeline 无重复，退出后无 endpoint/socket/agent 残留。
- **仍未覆盖**：cancel 响应时延在真实二进制上的表现（10s 有限超时
  契约只经 fake server 验证）；长会话的内存/token 增长与 compaction
  后的 restore 行为。
- **kimi acp 启动开销**：长驻进程只需一次握手，后续 prompt 无进程启动成本，
  比 JSONL 模式更快。
- **断线**：agent 进程 EOF 时所有 pending request 立即失败（带 stderr 尾段），
  由编排器的 `_run_one` 兜底成 error 事件。
- **增量补发的重复**：失败重试会把失败轮的增量再发一遍，agent 会在
  session 里看到重复的用户消息——可接受（丢上下文不可接受）。

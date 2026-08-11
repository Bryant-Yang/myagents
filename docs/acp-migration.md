# ACP 迁移设计（最小方案）

状态：**Phase 4.5、Phase 4.6、Phase 5.1 与 Phase 5 已完成**。
`AgentAdapter` 是 TUI 与不同 coding agent 的
统一行为契约；wire protocol 按厂商能力选择：`acp/` 是通用 ACP runtime，
Kimi/OpenCode 分别走 `kimi acp` / `opencode acp`，只在 ACP prepare
失败前使用各自受限 JSONL fallback；Qwen Code 走 `qwen --acp` 且保持
ACP-only；Codex 走官方 `codex app-server`，旧
Codex adapter 保留 JSONL fallback。app-server 不是 ACP，各协议只在
adapter 层统一。
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
    "promptCapabilities":{"image":true,"audio":false,"embeddedContext":true},
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

M4.3 图片附件沿用同一个 `session/prompt`：只有 Kimi 明确声明
`promptCapabilities.image=true` 时，客户端才在 text block 后追加
`{"type":"image","mimeType":"image/png","data":"<base64>"}`。图片只从当前
房间的私有 `attachments/` 信任根提取；聊天文本中的任意外部路径不会升级为
image block。新消息使用 `[图片 N]` 映射当前房间的 `img-NNNN.png`；旧绝对
路径引用只在同一信任根内兼容。二进制不进入共享 timeline。

M4.7 由 SessionManager 为每个已加载房间独立持有 Orchestrator/CommandBus 和
ACP/app-server runtime。切换可见会话不迁移或复用 native session writer；后台
权限请求携带 room_id，并在弹窗显示项目、会话、agent 与工具。空闲回收只关闭
非当前、无 command 且无权限等待的完整 runtime。

## 架构（Phase 2 现状）

```
orchestrator.py（AgentAdapter 接口不变；AGENT_SPECS 注册表）
   │  AgentSpec(name, transport, factory)
   │    kimi     → acp+jsonl → AcpKimiAdapter
   │                         └→ KimiAdapter（prepare-only，只读 profile）
   │    codex    → app-server → CodexAppServerAdapter（原生 thread/turn）
   │                         └→ CodexAdapter（安全 JSONL fallback）
   │    opencode → acp+jsonl → AcpOpenCodeAdapter
   │                         └→ OpenCodeAdapter（prepare-only，隔离只读配置）
   │    qwen     → acp       → AcpQwenAdapter（default/plan，无自动降级）
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
Kimi/OpenCode hybrid 的完整时机、权限与 checkpoint 契约见
[ADR-0006](adr/0006-kimi-hybrid-transport-policy.md)与
[ADR-0007](adr/0007-opencode-hybrid-transport-policy.md)。Qwen Code 的
`stream-json` 输入仍在上游文档中标记为未完成，且项目尚无独立只读 fallback
profile 的安全证据，因此生产只注册 ACP 路径，不做跨协议自动重放；普通 ACP
轮强制 `--approval-mode default`，workflow 只读轮强制 `plan`。

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
  被重复执行。ACP adapter 在首个 `session/update` / 权限活动进入外部 sink
  前发内部 `delivery_committed`，Orchestrator 据此先持久化 cursor；prompt
  写入后的断线即使没有收到 update，也按结果不确定处理。
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
agent inactivity timeout；权限结果发回后重新开始普通静默计时。OpenCode ACP
还通过 runtime permission policy 把默认偏宽的 unknown、edit、bash、task、
skill、network、MCP 与 external-directory 收口为 ask；read/search/lsp/todo
allow。其 workflow `read_only` profile 改为 unknown/risky=deny、只允许安全
读取；profile 切换关闭进程、禁止 load 并建立 fresh session，切回普通轮次恢复
ask。这样既隔离 `allow_always`，也避免 OpenCode 在 ask 被 cancelled 后直接
`end_turn` 且零正文。Qwen 普通 ACP 轮强制 approval `default`，避免继承 native
TUI 的 auto/yolo；只读轮用上游 plan mode 在 runtime 层阻断写入/有副作用命令，
profile 前后同样重建进程与 fresh session。等待仍可取消——TUI 退出时所有挂起的权限 Future 按 cancelled 收尾
（`on_unmount` →
`_cancel_pending_permissions` → `orch.aclose()`，顺序不能反：aclose
等的锁可能被等权限的 prompt 持有），不留挂起 Future 或 `kimi acp`
／`opencode acp` 子进程。权限后台 task 的异常由 done callback 消费（连接断开导致应答
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

- TUI 启动行显示每个 agent 的传输协议：`@kimi(ACP+JSONL) @opencode(ACP+JSONL)
  @qwen(ACP) @codex(APP-SERVER)`。
- ACP session id 建立后通过 info 事件展示一次（每次建立一次，不刷屏）。
- `tool_call` 保存脱敏后的 title/command；后续 `tool_call_update` 按
  `toolCallId` 继承上下文，只在 title/status/command/kind 的可见指纹变化时
  产出事件。完全相同的高频 `in_progress` 仍被视为协议活动，但不进入 TUI
  或持久日志；TUI 将同一 command 的阶段、heartbeat、工具与权限折叠为一张
  活动卡，命令详情默认隐藏并通过 `/details` 展开。
- 固定任务区显示 command 总状态、累计耗时及每个 agent 的阶段/终态；
  fan-out 中既有成功又有失败时显示“部分完成”，不抹掉成功 agent 的事实。

## 有界讨论（M5.1）

`/discuss` 不改变 ACP wire protocol，也不让 ACP agent 直接互发消息。它在
Orchestrator 内用普通代码执行 1–3 轮状态机：同轮不同 adapter 并发，跨轮等待
全部收尾，最后调用一次 moderator。整个讨论仍是一个 CommandBus command，
因此沿用同一个取消、事件、权限和失败终态。

每个 stateful 参与者继续使用原 delivery lock/cursor：第一轮成功后 cursor 只
推进到本轮 prompt 构造时的 timeline 末尾；其他参与者随后落盘的回复自然留给
下一轮增量。下一轮过滤自己的回复（原生 session 已有）并收到其他参与者发言，
不需要复制完整 transcript。参与者失败后退出后续轮次，不把 post-submit 不确定
结果当作可安全重试。完整工作流契约见
[ADR-0008](adr/0008-bounded-multi-agent-discussion.md)。

## 有界里程碑工作流（M5）

M5 不改变 ACP wire protocol，也不向 agent 暴露自主派发能力。`/workflow`
由普通代码严格推进 review → 单 writer implement → 独立 verify；首次复核要求
修改时最多允许同一 writer repair 一次并 reverify，最后由 host 汇总，最大六次
模型调用。全部阶段仍属于一个 CommandBus command 和一个 `command_id`。

adapter interface 已在 `stream` / `stream_prepared` 两条投递路径增加通用
execution mode：review/verify 映射为 `read_only`，implement/repair 映射为
`workspace_write`。具体 sandbox、权限和 fallback 行为仍由各 adapter 在现有
seam 内实现，Orchestrator 不按 agent 名分支。ACP 从普通轮次进入 `read_only`
时关闭旧进程、禁止 load 旧 session 并建立隔离 session，避免继承历史
`allow_always`；OpenCode 同时切换到 runtime deny-all + 安全读取白名单，退出
只读 mode 时再重建并恢复普通 ask；Qwen 切换到 `plan`，退出时恢复强制
`default`。Kimi/OpenCode 的只读 JSONL fallback 不能承担写阶段，触发时
workflow 必须 blocked。

运行中 steering 只在下一阶段边界注入尚未开始的 assignment，不并发写当前
ACP session、不修改已提交 prompt，也不能换人、加轮或扩大权限。完整设计见
[ADR-0009](adr/0009-bounded-milestone-workflow-steering.md)。生产 `/workflow`、
execution mode 与 TUI/control/MCP steering 均已实现并纳入 Harness。

workflow 还要求从干净 Git 工作区捕获 baseline HEAD/branch 和完整工作区指纹，
read-only 阶段前后不得漂移，写阶段不得改变 HEAD/branch/index。Git 探测由注入的
transport adapter 执行；Orchestrator/workflow 不直接启动子进程。外部 writer
恰好在 implement/repair 窗口写入时无法自动归因，必须作为人工验收边界披露。

## 阶段计划

- [x] **Phase 1**：`acp/client.py` + `acp/adapter.py` + fake server 回归测试
  （initialize/new/list/load/prompt/update/cancel/权限默认 deny/auto opt-in/
  取消串行化/超时重建/close-during-prompt/initialize 失败回收）
- [x] **Phase 2**：通用 ACP runtime 接入统一 TUI（kimi 为首个验收 agent）：
  `AgentSpec` 注册表（transport = acp/acp+jsonl/jsonl）；ACP 增量上下文
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
  `myagents_mcp.py` stdio MCP bridge（官方 SDK `mcp>=1.27,<2`，八个
  `myagents_*` 工具）只连运行中 TUI 的 socket，绝不实例化第二
  Orchestrator、不获取 lease、不绕过 TUI 权限。细节见
  [ADR-0001](adr/0001-persistent-room-command-bus-mcp.md)
- [x] **Phase 4**：Codex 官方 app-server 接入：长驻进程、thread/turn、
  流式 item、approval、interrupt、恢复和安全 JSONL fallback；host 使用
  ephemeral thread，避免内部路由污染 Codex 历史。详见
  [ADR-0003](adr/0003-codex-app-server-transport.md)与
  [ADR-0004](adr/0004-ephemeral-codex-host-threads.md)。Claude 尚未注册；
  后续按其可靠官方协议单独接入，不把厂商协议强行伪装成 ACP。
- [x] **Phase 4.4**：Kimi hybrid transport：保持 ACP-first，仅在新连接
  prepare 失败时进入内置只读 JSONL profile；伪 checkpoint 下轮新建
  ACP session，prompt 拒绝与 post-submit 失败禁止重放。见 ADR-0006。
- [x] **Phase 4.5**：OpenCode hybrid transport：`opencode acp` 成为生产
  主路径，风险/未知工具统一 ask；prepare-only fallback 使用 `--pure`、
  隔离配置和 deny-all 只读 agent。见 ADR-0007。
- [x] **Phase 5.1**：`/discuss` 指定 2–3 个 worker、1–3 轮有界讨论，
  同轮 fan-out、跨轮增量上下文、失败者退出与终局 moderator。见 ADR-0008。
- [x] **Phase 5**：里程碑 review → 单 writer 修改 → 独立复核、最多一次
  repair/reverify、阶段边界 steering、Git fixed point 与 TUI 阶段状态已完成；
  真实 Kimi review/verify + Codex implementer 临时仓库探针通过。见 ADR-0009。

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
- **M5 真实 E2E 已通过**（2026-08-09）：
  `scripts/e2e-m5-real.py` 在临时 Git repo 由 Kimi 只读 review/verify、Codex
  单 writer implement、host final；HEAD/branch/index 不变，最终 3 个 unittest
  通过；使用 `--verifier opencode` 的 Kimi review、Codex implement、OpenCode
  verify、host final 变体同样通过。OpenCode 1.18.15 同形状只读回复在 runtime
  hard deny 后连续 5/5 产出合法信封。真实模型探针不进入默认快速 gate。
- **仍未覆盖**：cancel 响应时延在真实二进制上的表现（10s 有限超时
  契约只经 fake server 验证）；长会话的内存/token 增长与 compaction
  后的 restore 行为。
- **kimi acp 启动开销**：长驻进程只需一次握手，后续 prompt 无进程启动成本，
  比 JSONL 模式更快。
- **JSONL 降级能力有意受限**：print mode 不能把写入权限交给
  TUI，因此 Kimi 自动 fallback 只提供 Read/Grep/Glob，OpenCode 只提供
  read/glob/grep/list；需要变更时应说明阻塞，不得伪装完成。
- **OpenCode 权限默认值**：上游默认允许大部分工具，生产 ACP 必须保留
  普通轮次 runtime ask policy，workflow 只读轮次必须保留 hard deny + 安全读取
  白名单；CLI 升级后用 `opencode debug agent build` 和真实 permission/workflow
  wire probe 复验规则顺序与零正文行为。
- **断线**：agent 进程 EOF 时所有 pending request 立即失败（带 stderr 尾段），
  由编排器的 `_run_one` 兜底成 error 事件。
- **图片能力漂移**：Kimi 不再声明 `promptCapabilities.image` 时退化为文本附件
  引用，不伪造协议能力；fake ACP contract 固定 text + image block 形状。
- **增量补发的重复**：失败重试会把失败轮的增量再发一遍，agent 会在
  session 里看到重复的用户消息——可接受（丢上下文不可接受）。

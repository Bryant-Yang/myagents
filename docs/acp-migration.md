# ACP 迁移设计（最小方案）

状态：**Phase 2 已完成**。ACP 不再是 Kimi 专用临时分支，而是
**TUI↔coding agent 的统一接入协议**：`acp/` 是与具体 agent 无关的通用
runtime，kimi 是首个验收 agent（命令 `kimi acp`），codex / opencode
继续走 JSONL fallback。多-agent 路由仍由 orchestrator 管——ACP 只负责
传输，不参与"派给谁"的决策。

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
   │    codex    → jsonl → CodexAdapter   ┐
   │    opencode → jsonl → OpenCodeAdapter ┘ JSONL fallback，永远保留
   │
   ├─ acp/adapter.py  AcpAdapter（通用：name + cmd 即一个 ACP agent；
   │     │            stateful_session = True 声明"会话在 agent 侧保持"）
   │     │  持有 session，串行化轮次（asyncio.Lock，aclose 同锁）
   │     ▼
   │   acp/client.py  AcpClient（协议层，与具体 agent 无关）
   │     JSON-RPC 2.0 / NDJSON / id 关联 / 反向请求 / killpg 清理
   │
   └─ adapters/*_adapter.py  私有 JSONL（fallback）
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
- **cursor 只在该轮成功交付后推进**，且只推进到本轮构造时的快照末尾
  （本轮进行期间到达的新消息留给下一轮）。失败不推进——下一轮从旧
  cursor 补发增量，不丢上下文；也不会退化成全量重发。
- JSONL fallback 不受影响：仍用 dispatch 瞬间的完整 transcript 快照
  （最近 12 条，防并发串话）。

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

等待用户决策期间不阻塞 read loop（独立 task 应答）；等待可取消——
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

## 生命周期（Phase 2 新增）

TUI 退出时 `Orchestrator.aclose()` 统一关闭所有支持 `aclose()` 的
adapter（`asyncio.gather`，一个关不掉不耽误其他）。close 与进行中的
prompt/session 初始化共用 adapter 的同一把锁，不竞态杀进程。

## 可见状态（Phase 2 新增）

- TUI 启动行显示每个 agent 的传输协议：`@kimi(ACP) @opencode(JSONL)
  @codex(JSONL)`。
- ACP session id 建立后通过 info 事件展示一次（每次建立一次，不刷屏）。

## 阶段计划

- [x] **Phase 1**：`acp/client.py` + `acp/adapter.py` + fake server 回归测试
  （initialize/new/list/load/prompt/update/cancel/权限默认 deny/auto opt-in/
  取消串行化/超时重建/close-during-prompt/initialize 失败回收）
- [x] **Phase 2**：通用 ACP runtime 接入统一 TUI（kimi 为首个验收 agent）：
  `AgentSpec` 注册表（transport = acp/jsonl）；ACP 增量上下文
  （history cursor）；权限请求弹到 TUI 让用户决策；TUI 退出统一
  `aclose()`；协议状态可见。JSONL 保留为 fallback。
- [ ] **Phase 2.5**：共享 history 持久化、ACP session 映射与重启恢复
- [ ] **Phase 3**：内部 command bus + 受控 MCP 外部入口，让 codex/skill
  向同一编排会话注入 review；Unix socket 可作为仅本机底层传输
- [ ] **Phase 4**：codex / claude 的 ACP 接入（有官方适配器后），
  JSONL fallback 逐步收缩为兜底
- [ ] **Phase 5**：里程碑工作流、review → 修改 → 复核闭环与 steering

A2A 不在当前阶段；只有出现跨机器、跨组织 agent 协作需求时再评估。

## 风险与注意

- **真实 `kimi acp` 已端到端验证**（Phase 2 收尾时实测）：真实二进制 +
  Textual TUI 的无工具回合正常；真实 Bash 权限请求弹窗正常，实际 options
  为 allow_once / allow_always / reject_once，选择 allow_once 后工具
  执行结果正确；退出后无残留进程。
- **仍未覆盖**：cancel 响应时延在真实二进制上的表现（10s 有限超时契约
  只经 fake server 验证）；长会话的内存/token 增长；session/load 恢复
  旧会话（协议层有方法，编排器尚未使用）。
- **kimi acp 启动开销**：长驻进程只需一次握手，后续 prompt 无进程启动成本，
  比 JSONL 模式更快。
- **断线**：agent 进程 EOF 时所有 pending request 立即失败（带 stderr 尾段），
  由编排器的 `_run_one` 兜底成 error 事件。
- **增量补发的重复**：失败重试会把失败轮的增量再发一遍，agent 会在
  session 里看到重复的用户消息——可接受（丢上下文不可接受）。

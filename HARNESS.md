# myagents 工程契约（HARNESS）

> 作者：Bryant Yang　最近更新：2026-07-27
> Harness Framework：2.1.0（来源 commit：
> `b3b8fd47ebc49b57abdc365f327688b33970d54c`）

这是 `myagents` 的工程事实源。产品行为与独立证据见
[`docs/SPEC.md`](docs/SPEC.md)，任务入口见
[`docs/workflow.md`](docs/workflow.md)，控制闭环见
[`docs/harness-controls.md`](docs/harness-controls.md)。

## 0. 项目使命

为不同 coding agent 提供一个统一、可验证的本地 TUI 编排面：路由和共享时间线
只有一个中心；ACP 负责有状态 agent 会话；adapter 隔离各 CLI 差异；权限默认
拒绝；取消或退出后不留下仍能修改工作区的子进程。

## 1. 北极星原则

| 原则 | 项目解释 |
| --- | --- |
| 一个编排中心 | agent 不直接互调，消息与 history 统一经过 Orchestrator。 |
| 原生长连接优先，JSONL fallback | 有官方可靠长连接时优先使用；Kimi 走 ACP，Codex 走 app-server，JSONL 只兼容回退。 |
| 协议通用、差异下沉 | 通用 runtime 不按 agent 名分支，具体差异进入 adapter/spec。 |
| 权限 fail-closed | 无处理器、异常或畸形选择一律拒绝；auto 必须显式授权。 |
| 生命周期负责到底 | 启动的进程组必须能 cancel、close 并被独立验证已回收。 |
| 行为证据优先 | fake contract tests 之外，关键真实协议边界保留人工/E2E 证据。 |

## 2. 技术与目录基线

| 类别 | 当前选择 |
| --- | --- |
| 语言 | Python 3.11+ |
| TUI | Textual `>=1.0` |
| ACP transport | NDJSON JSON-RPC 2.0 over stdio，protocolVersion 1 |
| Codex transport | app-server JSONL over stdio（JSON-RPC-like，无 `jsonrpc` header） |
| JSONL transport | 各 agent CLI 无头模式 |
| 持久化 | RoomStore：timeline.jsonl（对话）+ events.jsonl（执行）+ state.json（seq/cursor/session）+ owner.lock |
| 外部入口 | control/：CommandBus FIFO + 私有 Unix 控制 socket；myagents_mcp.py stdio MCP bridge（`mcp>=1.27,<2`） |
| 测试 | 直接运行的 Python test scripts + Textual pilot + fake ACP server + 官方 MCP SDK stdio client |
| 本地总门禁 | `bash scripts/check-harness.sh` |
| Git / CI | Git 已初始化；远程 CI 尚未配置，不得假称已有合并门禁 |

```text
main.py (Textual TUI)
    ↓
orchestrator.py (routing / history / delivery locks / lease)
    ↓
acp/             codex_app_server/  adapters/       storage/
ACP runtime      Codex runtime      JSONL fallback  RoomStore
    ↓                  ↓
coding agent subprocesses

control/ (CommandBus FIFO + 私有 Unix 控制 socket)
    ↑ 只连 socket，绝不创建 Orchestrator
myagents_mcp.py (stdio MCP bridge，mcp>=1.27,<2)
```

`tests/fake_acp_server.py` 是协议 fixture，不是生产 transport。

## 3. 分层与依赖

- `main.py` 只负责 UI、权限交互和生命周期入口，不直接启动 agent 进程。
- `orchestrator.py` 只面向 adapter 能力和 `AgentSpec`，不解析厂商协议。
- `acp/client.py` 负责通用 ACP framing、request/response、反向权限请求和进程回收。
- `acp/adapter.py` 把 ACP session 转成 `AgentEvent`。
- `codex_app_server/client.py` 独占 Codex app-server 进程，负责 initialize、
  request/response、反向 approval、通知和进程回收。
- `codex_app_server/adapter.py` 把 Codex thread/turn 映射成 stateful
  `AgentEvent`；不得把 app-server 冒充 ACP。
- `adapters/` 包含具体 JSONL CLI 参数/解析与共享子进程工具。
- `host.py` 包装一个 adapter 做路由/主持，不拥有 transport 实现。
- `storage/store.py` 负责 timeline/events/state 持久化与 owner lease，不做路由
  或协议判断；只读辅助实例不得获取 lease。
- `control/command_bus.py` 是 TUI 进程内唯一命令入口（FIFO 单 worker），
  不拥有 Orchestrator；`control/server.py` 只做 socket 序列化，
  `control/client.py` 只做发现与连接验证。
- `myagents_mcp.py` 是 stdio MCP bridge：只连运行中 TUI 的控制 socket，
  绝不实例化 Orchestrator、不获取 lease、不直写 timeline。

跨层协议变化必须同步实现、调用方、fake fixture、tests 和
[`docs/acp-migration.md`](docs/acp-migration.md)。

## 4. 核心契约

### 4.1 路由与上下文

- 显式 `@agent` 永远优先；无 `@` 时 host 单次调用直接回答或返回 worker
  路由，本地代码不维护自然语言关键词白名单。
- JSONL adapter 收 dispatch 时刻的有界 transcript 快照。
- stateful ACP adapter 在每-agent delivery lock 内读取 cursor、构造增量、
  完成 stream 后推进 cursor；失败不得推进。
- 首次 ACP bootstrap 最多发送 `history_limit` 条共享历史。

### 4.2 权限

- `AcpClient`、`AcpAdapter`、`AcpKimiAdapter` 默认权限都是 `deny`。
- TUI 异步决定权限并显示来源 agent。
- `selected.optionId` 必须非空且属于本次 `params.options`；否则 cancelled。
- `auto` 只允许明确授权的测试、运维或一次性外部调用使用。

### 4.3 生命周期

- 每个 ACP adapter 是其 session 的唯一 writer。
- 同一 ACP agent 的 prompt 串行；不同 agent 可以并发 fan-out。
- prompt 连续 120 秒无任何 ACP 通知或终止响应时自动取消本轮。
- cancel 后必须等待原 prompt 停止；超时则关闭并重建连接。
- 子进程使用独立进程组；退出时 SIGTERM，超时再 SIGKILL。
- TUI unmount 先取消权限 Future，再调用 `Orchestrator.aclose()`。

### 4.4 持久化与恢复（M2.5）

- RoomStore timeline 记录带单调 `seq`；cursor 是 seq 而非 list 下标，
  只在成功交付后先落盘再更新内存，失败不推进、不假提交。
- cursor/session_id 的 checkpoint 在 ACP prompt 前一次性原子落盘
  （`stream_prepared` 的 make_prompt hook）；checkpoint 失败穿透 dispatch，
  绝不伪装成 agent 调用失败。
- `session/load` 成功保留 cursor 继续增量；load 失败或 capability 不支持
  回退新 session，cursor 归零并按 `history_limit` 有界 bootstrap。
- 持久确认时序：用户消息 append 成功后才发 committed 事件并在 TUI 显示；
  agent 的 done 只在最终回复落盘成功后的成功轮发出。
- 房间单写者 lease：persistent Orchestrator 构造末尾获取 owner.lock
  （flock 非阻塞），冲突抛 `RoomBusyError`；`aclose()` 无论 adapter
  关闭结果如何都释放 lease；closed 后新 dispatch 与排队 delivery 一律
  拒绝（`OrchestratorClosedError`）。
- timeline/state/lease 损坏或不一致全部 fail loudly，不静默覆盖或
  bootstrap 成默认值。

### 4.5 控制层与 MCP 外部入口（M3）

- TUI 输入与外部控制统一经 CommandBus FIFO（单 worker 串行执行）；
  `request_id` 在 bus 生命周期内永久幂等；命令记录有容量硬上限；
  `wait` 有界（≤30s）且超时不取消任务；`aclose()` 把 active/queued
  命令兜底置 cancelled，不残留 task，不关闭 Orchestrator。
- 控制 socket 是私有本机 transport（每行一个 JSON request/response，
  请求 128KiB / 响应 32MiB 上限），不对外宣称为 MCP；socket/endpoint
  0600，endpoint 原子写；stale 文件只在实际连接验证无监听者后清理，
  活跃房间抛 `ControlBusyError`，绝不抢占或误删属主文件；未知 method、
  非法字段与超限返回稳定错误码，不泄漏 traceback。
- `ControlClient` 纯发现：不构造 RoomStore、不创建/删除状态；lstat
  拒绝 symlink、强制 0600、校验 room_id/workdir/socket_path 匹配后，
  每次调用仍实际连接验证。
- MCP bridge 七个 `myagents_*` 工具只翻译到上述 socket 协议；
  `ControlClientError` 一律转为可操作 tool error，不使 server 崩溃；
  stdout 只输出 MCP 帧；stdin EOF 后干净退出；权限请求仍由 TUI 决策，
  bridge 无 `auto` 入口。
- 退出顺序：先取消权限 Future，再停 control server（停止接收、删除
  endpoint/socket），随后 bus 收尾，最后关闭 Orchestrator 并释放 lease。

### 4.6 执行可观测性与精确取消（M3.1）

- `events.jsonl` 与对话 timeline 分离，记录生命周期、status、tool、
  permission、partial 与 terminal 事件；绝不进入 agent history。
- ACP thought 正文不可见，只映射安全阶段；工具与权限上下文有界展示，
  常见凭据字段隐藏。
- CommandBus 静默 10 秒发 heartbeat；active/queued 可精确取消，
  terminal cancel 幂等，取消不得杀死 worker。
- TUI `Ctrl+X`、control `command.cancel`、MCP
  `myagents_cancel_command` 共用一个取消原语；外部可通过
  `myagents_read_events` 读取持久进度。
- 重启后最后事件非 terminal 的命令必须显示为“已中断”，不得伪装完成。
- Codex host 继承用户默认配置，不覆盖 model、reasoning、plugin 或 MCP。

### 4.7 Codex app-server（M4）

- 一个 adapter 独占一个 app-server 进程与 thread，同 adapter turn 串行；
  Codex worker 连续轮次复用 thread，host 复用暖进程但每次建立干净 thread。
- 启动与请求不得覆盖 model、effort、config、collaboration mode、plugin 或
  MCP；只传协议必需字段、cwd 与既有 host/worker sandbox 安全边界，且不写
  `~/.codex/config.toml`。
- `turn/completed` 是完成权威信号；取消发送 `turn/interrupt` 并等 terminal，
  未确认则关闭重建，下一轮不得与旧 turn 重叠。
- approval 无处理器默认 decline；tool/command 元数据必须有界脱敏，reasoning
  正文不可显示。
- 仅在 initialize/thread prepare（尚未发送用户 turn）失败时允许 JSONL
  fallback；一旦发送 `turn/start` 就禁止自动重放，避免响应丢失时重复工具副作用。
- `turn/start` 已发送后的结果不确定、terminal 失败/中断或用户取消，都先把
  本轮输入持久推进为 no-replay 边界，再公开错误/取消；服务端明确拒绝或确认
  未发送的失败保持普通可重试语义。旧 thread 无法恢复而新建 thread 时不得
  自动 bootstrap 已投递过的 Codex transcript。
- `turn/start` 明确接受后，no-replay cursor 必须早于正文、工具、权限等任何
  外部 event sink 回调落盘，避免执行事件写失败重新打开重投窗口。
- app-server 是 Codex 专用实验协议；最小 wire contract 由 fake tests 固定，
  CLI 升级后必须重跑 contract 与真实探针。

## 5. 测试策略

| 层级 | 证据 |
| --- | --- |
| 路由/编排 | `tests/test_basic.py` |
| ACP 协议与取消 | `tests/test_acp.py` + `tests/fake_acp_server.py` |
| TUI/增量/权限/回收 | `tests/test_phase2.py` |
| RoomStore 持久化 | `tests/test_storage.py` |
| M2.5 恢复/lease/时序 | `tests/test_m25.py` |
| M3 command bus | `tests/test_m3_bus.py` |
| M3 控制 socket 安全/协议 | `tests/test_m3_control.py` |
| M3 MCP stdio | `tests/test_m3_mcp.py`（官方 SDK client） |
| M4 Codex app-server | `tests/test_codex_app_server.py` + `tests/fake_codex_app_server.py` |
| 真实协议边界 | `docs/SPEC.md` 登记的 Kimi ACP + Textual 人工 E2E |

普通测试禁止调用真实 Kimi/Codex/OpenCode。真实 agent 验收必须由用户明确授权，
在临时目录运行，并在结束后检查没有残留进程。

## 6. 质量门禁

当前本地门禁顺序：

```text
harness 文档引用 → redlines → py_compile → basic → ACP → Phase 2
→ storage → M2.5 → M3 bus → M3 control → M3 MCP stdio → M4 app-server
```

运行：

```bash
bash scripts/check-harness.sh
```

真实 Kimi + MCP 端到端验收（ADR-0001 §5）是发布前手工证据，不放进
上述默认快速 gate；执行前必须获得用户明确授权。

项目当前只有本地 gate，尚无远程 CI，不能保证每次合并都会自动触发。远程
branch protection / required checks 需要单独配置后才能宣称生效。

## 7. 推理型与人工边界

以下重要但不能伪装成 grep 红线：

- 新抽象是否真的比现有 `AgentSpec + AgentAdapter` 更简单；
- 某 agent 的 ACP 适配器是否成熟到可替换 JSONL fallback；
- 外部入口传输已定为私有 Unix socket + stdio MCP（ADR-0001）；是否引入
  Streamable HTTP、远程认证或 A2A 仍需人工决策，M3 不做；
- 真实工具调用的权限风险是否可接受；
- TUI 的可用性、长会话 token/内存表现和真实 cancel 时延；
- 真实 Kimi `session/load` 与 MCP 端到端恢复已由
  `scripts/e2e-m3-real.py` 验收；它调用真实模型，不进默认快速 gate；
- 长会话 compaction 后的 restore、真实 cancel 时延仍需人工验收。

具体 Owner、Sensor 与处置见 [`docs/harness-controls.md`](docs/harness-controls.md)。

## 8. 红线（4 条，违反必驳回）

| # | 红线 | 守门 |
| --- | --- | --- |
| R1 | 生产构造不得显式使用 `permission="auto"`，权限默认必须为 `deny` | `bash scripts/check-redlines.sh` 的 AST permission gate |
| R2 | 通用 orchestration/ACP 层不得按具体 agent 名做条件分支 | `bash scripts/check-redlines.sh` 的 AST name-branch gate |
| R3 | UI、orchestrator、host 不得直接启动 shell/子进程 | `bash scripts/check-redlines.sh` 的 AST process-boundary gate |
| R4 | Kimi 生产注册不得从 ACP 回退到旧 JSONL `KimiAdapter` | `bash scripts/check-redlines.sh` 的 registry/import gate |

红线变更必须同步本文、`AGENTS.md`、`docs/workflow.md`、enforcement 与
`docs/harness-controls.md`，并重新做正向和负向验证。

## 9. 决策与演进

- 当前 ACP 架构事实源：[`docs/acp-migration.md`](docs/acp-migration.md)。
- M2.5/M3 持久化与外部入口事实源：
  [`docs/adr/0001-persistent-room-command-bus-mcp.md`](docs/adr/0001-persistent-room-command-bus-mcp.md)。
- M3.1 执行可观测性事实源：
  [`docs/adr/0002-durable-execution-observability.md`](docs/adr/0002-durable-execution-observability.md)。
- M4 Codex app-server 事实源：
  [`docs/adr/0003-codex-app-server-transport.md`](docs/adr/0003-codex-app-server-transport.md)。
- 当前路线图：[`README.md`](README.md)“路线图”。
- 重大协议/安全边界改变先形成可评审设计记录，再修改本契约。
- Steering 只在同类失败至少两次或已有趋势证据时建立；单次失败只修当前问题。

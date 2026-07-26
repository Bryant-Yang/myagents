# myagents

ACP-first 的本地多 agent 终端编排器：在一个 Textual TUI 中点名 Kimi、
Codex、OpenCode 等 coding agent，共享时间线、流式接收回复，并统一处理权限、
上下文和进程生命周期。

> 当前状态：M2.5 与 M3 已完成——共享 timeline 持久化、ACP session
> 恢复、房间单写者 lease、内部 command bus、本机控制 socket 与 MCP
> stdio 外部入口均已落地并通过本地 gate。Kimi 使用真实 ACP 长驻会话；
> Codex/OpenCode 使用 JSONL 无头模式作为 fallback。真实 Kimi + MCP
> 端到端验收是发布前手工证据，见“当前限制”。

## 为什么做这个项目

不同 coding agent 的 CLI 参数、事件格式、会话与权限模型各不相同。`myagents`
用一层轻量编排把这些差异收敛到统一接口：

- 用户只面对一个 TUI 和一条共享时间线；
- 显式 `@agent` 决定任务交给谁，无 `@` 时由 host 做语义路由；
- ACP agent 保持原生 session，编排器只发送增量上下文；
- JSONL agent 仍可通过 adapter 接入；
- agent 之间不直接互调，路由、权限和生命周期由中心统一管理。

这个仓库同时保留可运行实现、协议实验和中文学习文档。

## 当前能力

- `@kimi`：通过 `kimi acp` 使用持久 ACP session。
- `@codex`、`@opencode`：通过各自 JSONL 无头模式运行。
- `@host`：由只读 Codex adapter 扮演主持人，负责总结和仲裁。
- 无显式 mention：host 输出结构化路由决策，再由 Orchestrator 派发。
- 多 agent fan-out：同一消息可同时点名多个 agent，并发执行。
- ACP 增量上下文：每个 stateful agent 独立维护 history cursor。
- 并发顺序保证：同一 ACP agent 严格串行，不同 agent 保持并行。
- 持久时间线：房间 timeline/state 落盘（单调 seq、UTC 时间戳），TUI 重启
  后恢复显示历史。
- ACP session 恢复：重启后优先 `session/load` 续接旧 session，保留已持久化
  cursor；load 失败或不支持时回退新 session 并有界 bootstrap。
- 原子 checkpoint：cursor/session_id 在 prompt 前一次性落盘，失败不伪装
  成功；用户消息持久确认后才在 TUI 显示。
- 房间单写者 lease：owner.lock（flock）保证同一房间同一时刻只有一个
  写入进程。
- 内部 command bus：TUI 输入与外部控制统一的 FIFO 入口；`request_id`
  幂等去重；单 worker 串行执行，命令状态可查、可有界等待。
- 本机控制 socket：TUI 私有的 Unix socket JSONL 协议（**不是 MCP**）；
  socket/endpoint 0600，活跃房间不被第二个 server 抢占，stale 文件
  只在确认无监听者后清理。
- MCP stdio bridge：五个 `myagents_*` 工具把本机其他 agent 的 review
  注入同一房间；bridge 不创建第二 Orchestrator、不直写 timeline、
  不绕过 TUI 权限。
- 权限弹窗：显示来源 agent、工具标题和 agent 提供的 options。
- 权限 fail-closed：无处理器、异常或非法 option 一律拒绝。
- 完整进程回收：取消、超时和 TUI 退出都会清理 agent 进程组。

## 架构

```text
┌────────────────────────────────────────────┐
│ main.py                                    │
│ Textual TUI / shared timeline / permission │
└──────────────────────┬─────────────────────┘
                       │
┌──────────────────────▼─────────────────────┐
│ orchestrator.py                            │
│ routing / AgentSpec / history / fan-out    │
└─────────────┬─────────────────┬────────────┘
              │                 │
┌─────────────▼──────────┐  ┌───▼────────────────────┐
│ acp/                   │  │ adapters/              │
│ stateful ACP runtime   │  │ JSONL CLI fallbacks    │
│ Kimi production path  │  │ Codex / OpenCode       │
└────────────────────────┘  └────────────────────────┘
┌────────────────────────┐  ┌────────────────────────┐
│ storage/               │  │ control/               │
│ RoomStore：timeline +  │  │ CommandBus（FIFO）+    │
│ state + owner lease    │  │ 私有 Unix 控制 socket  │
└────────────────────────┘  └───────────▲────────────┘
                                        │ 只连 socket
                            ┌───────────┴────────────┐
                            │ myagents_mcp.py        │
                            │ stdio MCP bridge       │
                            └────────────────────────┘
```

核心原则是 **Hub-and-Spoke**：所有消息先进入 Orchestrator，worker agent
之间不直接通信。ACP 只负责“如何驱动 agent”，不参与“任务应该派给谁”的决策。

## 环境要求

- macOS 或 Linux
- Python 3.11+
- 需要使用的 agent CLI 已安装并完成登录
  - [Kimi Code CLI](https://www.kimi.com/code)
  - [OpenAI Codex CLI](https://developers.openai.com/codex/cli)
  - [OpenCode](https://opencode.ai/)

只使用某一个 agent 时，不要求安装其他 worker CLI；但无 mention 路由和
`@host` 当前依赖 Codex CLI。

## 快速开始

```bash
git clone https://github.com/Bryant-Yang/myagents.git
cd myagents

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

也可以指定 agent 的工作目录：

```bash
.venv/bin/python main.py /path/to/project
```

每个工作目录对应一个持久房间：timeline、agent cursor/session 映射和
owner lease 存放在 `${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/<room_id>`，
不会写进目标工作区。同一房间同一时刻只允许一个 TUI 实例写入。

## 使用方式

```text
@kimi 解释这个模块，并给出最小修改方案
@codex review 当前实现，只报告可复现问题
@kimi @opencode 分别提出一个方案
@host 总结上面两个方案的分歧
```

路由规则：

1. 显式 `@agent` 永远优先。
2. 同一条消息中的多个有效 mention 会并发派发。
3. 不带 mention 时，host 根据最近对话选择 1–2 个 target，或由自己回答。
4. 单个 agent 失败会写入时间线，不会中断其他 agent。

## Transport 与上下文

| Agent | 生产 transport | 上下文策略 | 当前状态 |
| --- | --- | --- | --- |
| Kimi | ACP (`kimi acp`) | 持久 session + 增量 history + session 恢复 | 已验证（restore 路径为 fake 证据） |
| Codex | JSONL (`codex exec --json`) | 有界 transcript 快照 | fallback |
| OpenCode | JSONL (`opencode run --format json`) | 有界 transcript 快照 | fallback |
| Claude | 未接入 | 预留 AgentSpec/adapter 扩展点 | 规划中 |

ACP agent 首次接入只收到最近 `history_limit` 条共享记录；后续只收到 cursor
之后的新消息，并过滤它自己的回复。失败时 cursor 不推进，下一轮会补发。
cursor 是持久化 timeline 的单调 seq：重启后优先 `session/load` 续接旧
session 并保留 cursor；load 失败或 agent 不支持时回退新 session，cursor
归零并按 `history_limit` 有界 bootstrap。

## MCP 外部入口（M3）

本机其他 coding agent（Codex skill、Claude 等 MCP host）可以通过 MCP
stdio bridge 向**正在运行的** TUI 房间提交消息、读取时间线。前提：

- 依赖已安装：`requirements.txt` 固定 `mcp>=1.27,<2`（官方 Python SDK
  稳定 v1）；
- TUI 必须先运行：`.venv/bin/python main.py /path/to/project`；
- bridge 只做发现与连接，**绝不自动拉起 TUI，也绝不创建第二个
  Orchestrator**。

MCP host 的 stdio 配置示例（`--workdir` 必填，必须与目标 TUI 的
workdir 一致）：

```json
{
  "mcpServers": {
    "myagents": {
      "command": "/abs/path/myagents/.venv/bin/python",
      "args": [
        "/abs/path/myagents/myagents_mcp.py",
        "--workdir", "/path/to/project"
      ]
    }
  }
}
```

五个工具：

| MCP tool | 语义 | 注解 |
| --- | --- | --- |
| `myagents_get_room` | 房间、workdir、PID、agent transport | read-only、idempotent、closed-world |
| `myagents_read_timeline` | 按 `after_seq`/`limit`（≤200）读持久时间线 | read-only、idempotent、closed-world |
| `myagents_send_message` | 提交外部用户消息，立即返回 `command_id` | write、non-idempotent（带 `request_id` 去重）、open-world |
| `myagents_get_command` | 查 queued/running/completed/failed/cancelled | read-only、idempotent、closed-world |
| `myagents_wait_command` | 有界等待（≤30s）状态变化，超时不取消任务 | read-only、idempotent、closed-world |

最短 send → wait → read 流程：

```text
myagents_send_message(message="@kimi 总结这个模块", request_id="r1")
  → {"command_id": "..."}                    # 立即返回；同 request_id 重试幂等
myagents_wait_command(command_id=..., timeout=30)
  → {"status": "completed", ...}             # timed_out=true 也不取消任务
myagents_read_timeline(after_seq=0, limit=50)
  → {"items": [...], "has_more": ..., "next_after_seq": ...}
```

边界（与 [ADR-0001](docs/adr/0001-persistent-room-command-bus-mcp.md)
一致）：

- TUI 房间目录里的 `control.sock` 是**私有本机 transport**，不对外宣称
  为 MCP；MCP 只存在于 bridge 的 stdio 一侧；
- 外部消息走与 TUI 输入完全相同的 CommandBus FIFO 和
  `Orchestrator.dispatch`，单写者约束不变；
- 外部消息触发工具权限时仍在 TUI 弹窗由用户决策，bridge 没有 `auto`
  放行入口；
- TUI 未运行、endpoint stale 或房间不匹配时，工具返回可操作错误
  （含启动命令），不创建任何状态。

## 权限与安全

Kimi ACP 的权限请求会进入 TUI 弹窗。默认策略是 `deny`：

- `auto` 只能由明确授权的 client invocation 显式开启，并在该 client
  生命周期内持续生效；
- `selected.optionId` 必须属于本次 ACP 请求提供的 options；
- 关闭 TUI 时，等待中的权限请求按 cancelled 收尾；
- 一个 ACP session 同一时刻只能有一个 writer；
- 一个房间同一时刻只有一个写入进程：owner.lock（flock）冲突时第二个
  TUI 实例启动即失败，不会抢占或静默共用状态。

Codex/OpenCode 的 JSONL 无头模式可能直接执行工具。建议始终让 agent 在 Git
仓库或隔离 worktree 中工作，并在交付前检查 diff。

## 状态目录、恢复与排障

每个 workdir 的房间状态位于
`${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/<room_id>`
（目录 0700、文件 0600、`state.json`/`endpoint.json` 原子写），绝不写进
目标工作区。TUI 重启后恢复时间线显示、agent seq cursor 和 ACP session
映射（优先 `session/load`，失败回退新 session + 有界 bootstrap）。

常见排障：

- **"未发现活跃 TUI endpoint / 无法连接活跃 TUI"**：目标 TUI 没在运行。
  按错误提示启动：`.venv/bin/python main.py <workdir>`。bridge/MCP 不会
  自动拉起 TUI。
- **stale endpoint/socket**：TUI 正常退出会删除 `control.sock` 和
  `endpoint.json`；进程被 kill 留下的 stale 文件，下次启动时先实际
  连接验证无监听者，确认 stale 后自动清理恢复。
- **"房间已有活跃 control server"**：另一个 TUI 正在占用该房间，不会
  被抢占；owner.lock（flock）冲突同样使第二个 TUI 启动即失败并提示
  持有者 PID。
- **"control socket 路径过长"**：macOS AF_UNIX 路径上限约 104 字节。
  用更短的 `XDG_STATE_HOME`（如 `XDG_STATE_HOME=/tmp/mya-state`）重启
  TUI。
- **"endpoint 与目标房间不匹配 / 权限不是 0600"**：fail closed，说明
  状态目录里的文件不属于该房间的有效 TUI；确认 workdir 无误后重启
  TUI。

## 开发与验证

创建虚拟环境后运行完整本地门禁：

```bash
bash scripts/check-harness.sh
```

它依次检查：

```text
Harness markers/links
→ architecture redlines
→ py_compile
→ basic tests
→ ACP contract tests
→ Phase 2 TUI/integration tests
→ storage tests
→ M2.5 persistence/restore tests
→ M3 command bus tests
→ M3 control socket tests
→ M3 MCP stdio tests
```

也可以单独运行：

```bash
.venv/bin/python tests/test_basic.py
.venv/bin/python tests/test_acp.py
.venv/bin/python tests/test_phase2.py
.venv/bin/python tests/test_storage.py
.venv/bin/python tests/test_m25.py
.venv/bin/python tests/test_m3_bus.py
.venv/bin/python tests/test_m3_control.py
.venv/bin/python tests/test_m3_mcp.py
```

普通测试全部使用 fake adapter/fake ACP server，不会调用真实外部 agent。
真实 Kimi + MCP 端到端验收（见“当前限制”）是发布前手工证据，不在
默认 gate 内。

## 项目结构

```text
myagents/
├── main.py                    # Textual TUI
├── myagents_mcp.py            # stdio MCP bridge（只连控制 socket）
├── orchestrator.py            # 路由、history、并发投递、lease 生命周期
├── host.py                    # supervisor / host
├── acp/                       # 通用 ACP client 与 adapter
├── adapters/                  # JSONL adapter 与进程工具
├── control/                   # CommandBus + 私有 Unix 控制 socket server/client
├── storage/                   # RoomStore：timeline/state/owner lease
├── tests/                     # basic / ACP / Phase 2 / storage / M2.5 / M3 tests
├── docs/
│   ├── SPEC.md                # 关键行为与独立证据
│   ├── acp-migration.md       # ACP 契约和迁移状态
│   ├── adr/                   # 架构决策记录（ADR-0001 持久房间/command bus/MCP）
│   ├── concepts.md            # agent 编排概念词汇表
│   └── workflow.md            # 按任务选择上下文
├── AGENTS.md                  # agent 入场契约
├── HARNESS.md                 # 工程红线与完成标准
└── scripts/check-harness.sh   # 本地总门禁
```

## 文档入口

- [AGENTS.md](AGENTS.md)：coding agent 进入项目时先读。
- [HARNESS.md](HARNESS.md)：架构、安全红线和质量门禁。
- [docs/SPEC.md](docs/SPEC.md)：关键行为及验收证据。
- [docs/acp-migration.md](docs/acp-migration.md)：ACP 消息、权限、取消和生命周期。
- [docs/concepts.md](docs/concepts.md)：相关协议与编排模式。
- [docs/knowledge-map.html](docs/knowledge-map.html)：可交互知识地图。

## 路线图

- [x] M0：统一 TUI、显式路由、host、JSONL adapters。
- [x] M1：通用 ACP client/adapter、fake server contract tests。
- [x] M2：Kimi ACP、增量上下文、权限 UI、统一回收、真实 TUI E2E。
- [x] M2.5：history 持久化与 session 映射/恢复、房间单写者 lease。
- [x] M3：内部 command bus、私有 Unix 控制 socket、MCP stdio 外部入口。
- [ ] M4：Codex/Claude 等 agent 的 ACP 接入，JSONL 逐步退为兜底。
- [ ] M5：里程碑工作流、review → 修改 → 复核闭环与 steering。
- [ ] Later：只有出现跨机器、跨组织 agent 协作需求时再评估 A2A。

## 当前限制

- 真实 Kimi cancel 时延和长会话 token/内存增长（含 compaction 表现）尚未压测。
- M3 真实 E2E 已于 2026-07-26 通过
  [`scripts/e2e-m3-real.py`](scripts/e2e-m3-real.py)：两次独立
  TUI/ACP/MCP 生命周期复用同一 Kimi session，timeline 无重复，退出后无
  endpoint/socket/agent 残留。该脚本调用真实模型，不放进默认快速 gate。
- 独立 ACP client 写入已有 Kimi session 不会让已打开的 native Kimi TUI
  实时刷新；一个前端应独占该 session。

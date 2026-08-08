# myagents

原生长连接优先的本地多 agent 终端编排器：在一个 Textual TUI 中点名 Kimi、
Codex、OpenCode 等 coding agent，共享时间线、流式接收回复，并统一处理权限、
上下文和进程生命周期。

> 当前状态：M2.5、M3、M3.1、M4、M4.2、M4.4、M4.5 与 M5.1 已完成；M4.3 图片粘贴实现已完成，
> 等待真实截图与 agent 视觉结果人工验收。共享 timeline 与执行 events 持久化、ACP session
> 恢复、房间单写者 lease、内部 command bus、本机控制 socket 与 MCP
> stdio 外部入口、执行心跳、精确取消、Codex app-server 长连接与同项目独立会话均已落地。
> Kimi/OpenCode 使用 ACP-first + prepare-only 只读 JSONL fallback，Codex 使用官方
> app-server；JSONL 不会在已提交任务后跨协议重放。真实 Kimi + MCP
> 端到端验收是发布前手工证据，见“当前限制”。

## 为什么做这个项目

不同 coding agent 的 CLI 参数、事件格式、会话与权限模型各不相同。`myagents`
用一层轻量编排把这些差异收敛到统一接口：

- 用户只面对一个 TUI 和一条共享时间线；
- 显式 `@agent` 决定任务交给谁，无 `@` 时由 host 做语义路由；
- 有状态 agent 保持原生 session/thread，编排器只发送增量上下文；
- JSONL agent 仍可通过 adapter 接入；
- agent 之间不直接互调，路由、权限和生命周期由中心统一管理。

这个仓库同时保留可运行实现、协议实验和中文学习文档。

## 当前能力

- `@kimi`：正常通过 `kimi acp` 使用持久 ACP session；仅在
  ACP 启动/建 session 失败且尚未提交 prompt 时，进入只读 JSONL 降级。
- `@codex`：通过 `codex app-server` 复用长驻进程与原生 thread。
- `@opencode`：正常通过 `opencode acp` 使用持久 ACP session；未知及
  有副作用工具进入 TUI 权限，只有 prepare 失败才进入隔离只读 JSONL。
- `@host`：由只读 Codex adapter 扮演主持人，负责总结和仲裁。
- 无显式 mention：host 用一次调用决定“直接回答”或输出结构化 worker
  路由；直接回答时不再发起第二次 host 调用。
- 多 agent fan-out：同一消息可同时点名多个 agent，并发执行。
- 有界讨论：`/discuss` 在一个 command 内安排 2–3 个指定 worker 做 1–3 轮
  独立提案与交叉评议，再由 `host` 或另一个未参会 worker 最终仲裁。
- ACP 增量上下文：每个 stateful agent 独立维护 history cursor。
- 并发顺序保证：同一 ACP agent 严格串行，不同 agent 保持并行。
- 持久时间线：房间 timeline/state 落盘（单调 seq、UTC 时间戳），TUI 重启
  后恢复显示历史。
- 独立会话：同一工作目录可通过 `Ctrl+N` 或精确输入 `/new` 新建完全隔离的
  timeline、events、cursor 与原生 agent session；默认会话继续兼容已有历史。
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
- MCP stdio bridge：七个 `myagents_*` 工具把本机其他 agent 的 review
  注入同一房间；bridge 不创建第二 Orchestrator、不直写 timeline、
  不绕过 TUI 权限。
- 可观测执行：独立 events 日志、累计且原位更新的静默 heartbeat、工具/权限
  上下文与重启中断提示；固定任务区持续显示总状态、耗时和各 agent 阶段，
  部分 agent 成功、部分失败时明确显示“部分完成”。
- 工具状态聚合：同一工具的高频 `in_progress` 只保留一次，标题与命令上下文
  延续到终态；TUI 原位更新为“进行中/已完成/失败”，命令详情默认折叠，
  输入 `/details` 切换显示，执行日志仍完整记录状态迁移。
- 明确委托：host 路由同时给每个 worker 生成完整 task，消解“你/让 Kimi”
  等角色关系，并直接注入本轮 prompt，不再只显示路由理由。
- 权限弹窗：显示来源 agent、工具标题、命令上下文和 agent 提供的 options。
- 精确取消：TUI `Ctrl+X` 或 MCP 只取消当前 command，房间继续工作。
- 权限 fail-closed：无处理器、异常或非法 option 一律拒绝。
- 流式回复合并：ACP token/chunk 持续更新同一条 TUI 记录，不再一词一行。
- ACP 卡死回收：普通分析连续 120 秒无协议事件才取消；已进入工具生命周期后
  使用独立 15 分钟无活动上限，避免工程子代理或长命令被普通静默阈值误杀。
  必要时重建连接，并将已提交轮次标为 no-replay，避免重复执行。
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
│ acp/                   │  │ codex_app_server/      │
│ Kimi/OpenCode ACP      │  │ Codex native runtime   │
└────────────────────────┘  └────────────────────────┘
              │                 │
              └────────┬────────┘
                       │ adapters/ JSONL fallback
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
之间不直接通信。ACP/app-server 只负责“如何驱动 agent”，不参与“任务应该
派给谁”的决策。

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

按名称恢复同一项目中的独立会话：

```bash
.venv/bin/python main.py --session game-review /path/to/project
```

每个 `(工作目录, 会话名)` 对应一个持久房间：对话 timeline、执行 events、
图片 attachments、agent cursor/session 映射和
owner lease 存放在 `${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/<room_id>`，
不会写进目标工作区。未指定名称时使用兼容旧历史的 `default` 会话；同一房间
同一时刻只允许一个 TUI 实例写入。

## 使用方式

```text
@kimi 解释这个模块，并给出最小修改方案
@codex review 当前实现，只报告可复现问题
@kimi @opencode 分别提出一个方案
@host 总结上面两个方案的分歧
/discuss @kimi @opencode --rounds 2 --moderator host -- 讨论新增 adapter 的协议选择
```

`/discuss` 默认两轮、默认由 `host` 主持。参与者必须是 2–3 个不同 worker，
主持人不能同时参会；轮次由 Orchestrator 的普通代码推进，agent 不能自行加轮或
拉人。讨论模式只要求文字观点，不用于并发修改代码。MCP/API 也可把主题写在
首行参数后的下一行。完整契约见
[ADR-0008](docs/adr/0008-bounded-multi-agent-discussion.md)。

空闲时按 `Ctrl+N` 或精确输入 `/new` 可新建会话：输入名称，或留空自动命名。
`/new` 是本地命令，不会写入时间线，也不会发送给 host 或 worker。已有名称
不会被覆盖；恢复已有会话请退出后使用 `--session NAME`。任务正在排队、运行
或等待权限时，必须先完成或按 `Ctrl+X` 取消后再切换。

粘贴 macOS 剪贴板中的截图或图片：

1. 快捷方式：先写说明和可选的 `@agent`，再按 `Ctrl+V`；Textual
   文本剪贴板为空时会尝试粘贴系统图片。
2. 稳定方式：单独输入 `/paste-image` 并按 Enter，再围绕插入的图片引用
   补充说明和可选的 `@agent`。
3. 确认图片引用已经出现在草稿中，再按 Enter 与文字一起发送。

图片必须能由 macOS 剪贴板提供 PNG 表示，单张不超过 20 MiB。文件保存到当前
房间的私有 `attachments/` 目录（目录 0700、文件 0600），不会写入项目工作区；
TUI 目前显示附件文件名而不是终端内预览。Kimi ACP 与 Codex app-server 会
使用各自的原生图片输入发送可信附件，而不是要求 agent 越界读取该绝对路径。

路由规则：

1. 显式 `@agent` 永远优先。
2. 同一条消息中的多个有效 mention 会并发派发。
3. 不带 mention 时，host 在一次调用中直接回答，或根据最近对话选择
   1–2 个 worker；自然语言内容不由本地关键词白名单判断。
4. 单个 agent 失败会写入时间线，不会中断其他 agent；fan-out 全部收尾后，
   只要任一 worker 失败，该 command 终态就是 `failed`。
5. `/discuss` 同轮并发、跨轮串行；失败参与者不自动重试，主持人仍总结已有
   证据，但不能把失败 command 洗成 completed。

## Transport 与上下文

| Agent | 生产 transport | 上下文策略 | 当前状态 |
| --- | --- | --- | --- |
| Kimi | ACP + 只读 JSONL (`kimi acp` → `kimi -p`) | 持久 session + 增量 history；只有 prepare 失败才降级 | ACP 已验证；hybrid contract 已验收 |
| Codex | app-server (`codex app-server`) | 持久 thread + 增量 history + thread 恢复 | 已接入；JSONL fallback |
| OpenCode | ACP + 隔离只读 JSONL (`opencode acp` → `opencode run`) | 持久 session + 增量 history；风险工具 ask；只有 prepare 失败才降级 | ACP/permission 已验证；hybrid contract 已验收 |
| Claude | 未接入 | 预留 AgentSpec/adapter 扩展点 | 规划中 |

有状态 agent 首次接入只收到最近 `history_limit` 条共享记录；后续只收到 cursor
之后的新消息，并过滤它自己的回复。提交前明确失败时 cursor 不推进、下轮补发；
提交后静默超时等结果不确定失败会先建立 no-replay cursor，防止工具任务被重复
执行。
cursor 是持久化 timeline 的单调 seq：重启后优先 `session/load` 续接旧
session 并保留 cursor；load 失败或 agent 不支持时回退新 session，cursor
归零并按 `history_limit` 有界 bootstrap。

Kimi 的 JSONL 降级是明确的受限模式：内置 agent profile 只允许
`Read` / `Grep` / `Glob`，禁止写入、命令、Skill、子 agent 和 MCP。
降级轮可返回定位与阻塞说明，不伪装成已完成的文件变更。
细节见 [ADR-0006](docs/adr/0006-kimi-hybrid-transport-policy.md)。

OpenCode 正常 ACP 路径额外把默认偏宽的权限收口为 unknown/risky=ask，
由同一 TUI 决策。JSONL 降级使用 `--pure`、禁用项目配置/Claude 兼容层/
自动升级，并通过 inline agent 与 runtime permission 双重限制为
`read` / `glob` / `grep` / `list`。细节见
[ADR-0007](docs/adr/0007-opencode-hybrid-transport-policy.md)。

## MCP 外部入口（M3）

本机其他 coding agent（Codex skill、Claude 等 MCP host）可以通过 MCP
stdio bridge 向**正在运行的** TUI 房间提交消息、读取时间线和执行进度，
也可精确取消命令。前提：

- 依赖已安装：`requirements.txt` 固定 `mcp>=1.27,<2`（官方 Python SDK
  稳定 v1）；
- TUI 必须先运行：`.venv/bin/python main.py /path/to/project`；
- bridge 只做发现与连接，**绝不自动拉起 TUI，也绝不创建第二个
  Orchestrator**。

MCP host 的 stdio 配置示例（`--workdir` 必填，必须与目标 TUI 的
workdir 一致；命名会话还必须传同名 `--session`）：

```json
{
  "mcpServers": {
    "myagents": {
      "command": "/abs/path/myagents/.venv/bin/python",
      "args": [
        "/abs/path/myagents/myagents_mcp.py",
        "--workdir", "/path/to/project",
        "--session", "game-review"
      ]
    }
  }
}
```

七个工具：

| MCP tool | 语义 | 注解 |
| --- | --- | --- |
| `myagents_get_room` | 房间、workdir、PID、agent transport | read-only、idempotent、closed-world |
| `myagents_read_timeline` | 按 `after_seq`/`limit`（≤200）读持久时间线 | read-only、idempotent、closed-world |
| `myagents_read_events` | 读生命周期、工具、权限、心跳和 terminal 事件 | read-only、idempotent、closed-world |
| `myagents_send_message` | 提交外部用户消息，立即返回 `command_id` | write、non-idempotent（带 `request_id` 去重）、open-world |
| `myagents_get_command` | 查 queued/running/completed/failed/cancelled | read-only、idempotent、closed-world |
| `myagents_wait_command` | 有界等待（≤30s）状态变化，超时不取消任务 | read-only、idempotent、closed-world |
| `myagents_cancel_command` | 精确取消 queued/running 命令；terminal 幂等 | write、idempotent、closed-world |

最短 send → wait → read 流程：

```text
myagents_send_message(message="@kimi 总结这个模块", request_id="r1")
  → {"command_id": "..."}                    # 立即返回；同 request_id 重试幂等
myagents_wait_command(command_id=..., timeout=30)
  → {"status": "completed", ...}             # timed_out=true 也不取消任务
myagents_read_events(after_seq=0, limit=50)
  → {"items": [...]}                         # 长任务阶段、工具、权限与心跳
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

Kimi/OpenCode ACP 的权限请求会进入 TUI 弹窗。默认策略是 `deny`；
OpenCode adapter 还会把上游默认偏宽的未知及风险工具收口为 `ask`：

- `auto` 只能由明确授权的 client invocation 显式开启，并在该 client
  生命周期内持续生效；
- `selected.optionId` 必须属于本次 ACP 请求提供的 options；
- 关闭 TUI 时，等待中的权限请求按 cancelled 收尾；
- 一个 ACP session 同一时刻只能有一个 writer；
- 一个房间同一时刻只有一个写入进程：owner.lock（flock）冲突时第二个
  TUI 实例启动即失败，不会抢占或静默共用状态。

Codex worker 使用 `workspace-write + on-request`：超出沙箱的 Git 元数据、
本地 socket 等操作必须进入同一 TUI 权限弹窗；只读 host 使用
`read-only + never`，不会为路由申请写权限。Kimi/OpenCode 自动 JSONL
fallback 都由 runtime 白名单限制为只读；正常 ACP 写入仍需在 TUI 明确批准，
并建议在 Git 仓库或隔离 worktree 中工作、交付前检查 diff。

## 状态目录、恢复与排障

每个 `(workdir, session_name)` 的房间状态位于
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
  持有者 PID，但不会打印 Python traceback。关闭旧 TUI，或使用
  `uv run main.py --session <新名称> <workdir>` 打开独立会话。
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
→ M4 Codex app-server contract tests
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
.venv/bin/python tests/test_codex_app_server.py
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
├── storage/                   # RoomStore：timeline/events/state/owner lease
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
- [docs/adr/0002-durable-execution-observability.md](docs/adr/0002-durable-execution-observability.md)：执行事件、心跳与取消决策。
- [docs/adr/0003-codex-app-server-transport.md](docs/adr/0003-codex-app-server-transport.md)：Codex 长连接、默认配置继承和 fallback。
- [docs/adr/0006-kimi-hybrid-transport-policy.md](docs/adr/0006-kimi-hybrid-transport-policy.md)：Kimi ACP-first 与只读降级。
- [docs/adr/0007-opencode-hybrid-transport-policy.md](docs/adr/0007-opencode-hybrid-transport-policy.md)：OpenCode ACP 权限收口与只读降级。
- [docs/adr/0008-bounded-multi-agent-discussion.md](docs/adr/0008-bounded-multi-agent-discussion.md)：有界讨论状态机与失败收口。
- [docs/concepts.md](docs/concepts.md)：相关协议与编排模式。
- [docs/knowledge-map.html](docs/knowledge-map.html)：可交互知识地图。

## 路线图

- [x] M0：统一 TUI、显式路由、host、JSONL adapters。
- [x] M1：通用 ACP client/adapter、fake server contract tests。
- [x] M2：Kimi ACP、增量上下文、权限 UI、统一回收、真实 TUI E2E。
- [x] M2.5：history 持久化与 session 映射/恢复、房间单写者 lease。
- [x] M3：内部 command bus、私有 Unix 控制 socket、MCP stdio 外部入口。
- [x] M3.1：持久执行可观测性、heartbeat、权限上下文与精确取消。
- [x] M4：Codex 官方 app-server 长连接接入，JSONL 退为安全兜底。
- [x] M4.2：同一项目独立会话、默认历史兼容、TUI 安全切换与外部 selector。
- [ ] M4.3：实现已完成；等待 macOS 真实截图与 agent 视觉结果人工验收。
- [x] M4.4：Kimi ACP-first + prepare-only 只读 JSONL fallback。
- [x] M4.5：OpenCode ACP-first + ask-by-default 权限 + 隔离只读 JSONL fallback。
- [x] M5.1：`/discuss` 指定成员、1–3 轮有界讨论与终局 moderator。
- [ ] M5：里程碑工作流、review → 修改 → 复核闭环与 steering。
- [ ] Later：只有出现跨机器、跨组织 agent 协作需求时再评估 A2A。

## 当前限制

- M4 真实 Codex 两轮探针已于 2026-07-27 通过：直接 adapter 冷/热两轮约
  18.0s/4.6s，真实 Orchestrator 连续两次 `@codex` 也复用同一
  app-server PID/thread；退出后无残留。app-server 是实验接口，Codex CLI
  升级后仍需重跑 contract 与真实探针。
- 真实 Kimi cancel 时延和长会话 token/内存增长（含 compaction 表现）尚未压测。
- Kimi JSONL fallback 只能读取/分析，无法代替 ACP 完成写入任务；
  真实 fallback 回复质量与 CLI 升级后 schema 漂移仍需受限探针。
- OpenCode fallback 同样只能读取/分析；`OPENCODE_PERMISSION` 与配置合并
  seam 属于 CLI 版本边界。1.18.14 的 ACP 回复、`session/load`、Bash deny、
  真实只读 fallback 和无残留进程已于 2026-08-08 通过；升级后必须重跑
  capability/permission/profile 探针。
- M3 真实 E2E 已于 2026-07-26 通过
  [`scripts/e2e-m3-real.py`](scripts/e2e-m3-real.py)：两次独立
  TUI/ACP/MCP 生命周期复用同一 Kimi session，timeline 无重复，退出后无
  endpoint/socket/agent 残留。该脚本调用真实模型，不放进默认快速 gate。
- M5.1 `/discuss` 已于 2026-08-08 在同一命名房间恢复原 Kimi/OpenCode
  session，通过 MCP 完成两轮交叉讨论和一次 Codex host 仲裁；6 条新增
  timeline 连续、第二轮能回应对方首轮、无工具/权限事件且退出无残留。
- 独立 ACP client 写入已有 Kimi session 不会让已打开的 native Kimi TUI
  实时刷新；一个前端应独占该 session。
- 当前只支持新建会话和用 `--session NAME` 恢复；TUI 内的会话列表、删除和
  重命名尚未实现。

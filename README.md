# myagents

ACP-first 的本地多 agent 终端编排器：在一个 Textual TUI 中点名 Kimi、
Codex、OpenCode 等 coding agent，共享时间线、流式接收回复，并统一处理权限、
上下文和进程生命周期。

> 当前状态：ACP Phase 2 已完成。Kimi 使用真实 ACP 长驻会话；
> Codex/OpenCode 使用 JSONL 无头模式作为 fallback。

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
| Kimi | ACP (`kimi acp`) | 持久 session + 增量 history | 已验证 |
| Codex | JSONL (`codex exec --json`) | 有界 transcript 快照 | fallback |
| OpenCode | JSONL (`opencode run --format json`) | 有界 transcript 快照 | fallback |
| Claude | 未接入 | 预留 AgentSpec/adapter 扩展点 | 规划中 |

ACP agent 首次接入只收到最近 `history_limit` 条共享记录；后续只收到 cursor
之后的新消息，并过滤它自己的回复。失败时 cursor 不推进，下一轮会补发。

## 权限与安全

Kimi ACP 的权限请求会进入 TUI 弹窗。默认策略是 `deny`：

- `auto` 只能由明确授权的 client invocation 显式开启，并在该 client
  生命周期内持续生效；
- `selected.optionId` 必须属于本次 ACP 请求提供的 options；
- 关闭 TUI 时，等待中的权限请求按 cancelled 收尾；
- 一个 ACP session 同一时刻只能有一个 writer。

Codex/OpenCode 的 JSONL 无头模式可能直接执行工具。建议始终让 agent 在 Git
仓库或隔离 worktree 中工作，并在交付前检查 diff。

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
```

也可以单独运行：

```bash
.venv/bin/python tests/test_basic.py
.venv/bin/python tests/test_acp.py
.venv/bin/python tests/test_phase2.py
```

普通测试全部使用 fake adapter/fake ACP server，不会调用真实外部 agent。

## 项目结构

```text
myagents/
├── main.py                    # Textual TUI
├── orchestrator.py            # 路由、history、并发投递
├── host.py                    # supervisor / host
├── acp/                       # 通用 ACP client 与 adapter
├── adapters/                  # JSONL adapter 与进程工具
├── tests/                     # basic / ACP / Phase 2 tests
├── docs/
│   ├── SPEC.md                # 关键行为与独立证据
│   ├── acp-migration.md       # ACP 契约和迁移状态
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
- [ ] M2.5：history 持久化与 session 映射/恢复。
- [ ] M3：内部 command bus + 受控 MCP 外部入口；Unix socket 可作为本地传输。
- [ ] M4：Codex/Claude 等 agent 的 ACP 接入，JSONL 逐步退为兜底。
- [ ] M5：里程碑工作流、review → 修改 → 复核闭环与 steering。
- [ ] Later：只有出现跨机器、跨组织 agent 协作需求时再评估 A2A。

## 当前限制

- 共享 history 尚未持久化，TUI 重启后不会恢复房间状态。
- 编排器尚未使用 `session/load` 恢复旧 ACP session。
- 真实 Kimi cancel 时延和长会话 token/内存增长尚未压测。
- 独立 ACP client 写入已有 Kimi session 不会让已打开的 native Kimi TUI
  实时刷新；一个前端应独占该 session。

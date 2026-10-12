# myagents

原生长连接优先的本地多 agent 终端编排器：在一个 Textual TUI 中点名 Kimi、Codex、
OpenCode、Qwen Code、CodeBuddy、DeepSeek Harness（DSH）、Pi、Claude Code 等 coding
agent，共享同一条时间线，流式接收回复，并统一处理权限、上下文和进程生命周期。

所有 agent 之间不直接通信，路由、权限与生命周期由中心统一管理；每个有状态 agent 保持
自己的原生 session/thread，编排器只发送增量上下文。Kimi / OpenCode 走 ACP-first，Qwen /
CodeBuddy / DSH 走 ACP-only，Pi 走官方 RPC，Codex 走官方 app-server，Claude Code 走
headless stream-json，Host 默认用内置无工具模型 runtime。

## 快速开始

```bash
git clone https://github.com/Bryant-Yang/myagents.git
cd myagents

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python - <<'PY'
import json, urllib.request
print(*(item["id"] for item in json.load(
    urllib.request.urlopen("http://127.0.0.1:1234/v1/models"))["data"]), sep="\n")
PY
case "${XDG_CONFIG_HOME:-}" in
  /*) myagents_config_home="$XDG_CONFIG_HOME" ;;
  *) myagents_config_home="$HOME/.config" ;;
esac
install -d -m 700 "$myagents_config_home/myagents"
$EDITOR "$myagents_config_home/myagents/config.toml"
chmod 600 "$myagents_config_home/myagents/config.toml"
.venv/bin/python main.py /path/to/project
```

配置文件在 `~/.config/myagents/config.toml`（或 `$XDG_CONFIG_HOME/myagents/`），必须是
权限 0600 的普通文件：

```toml
[host.model]
provider = "openai-compatible"
base_url = "http://127.0.0.1:1234/v1"
model_id = "把这里替换为 /v1/models 返回的精确 id"
```

需要哪些 agent 就装哪些 CLI（Kimi、Codex、OpenCode、Qwen、Pi、Claude Code、CodeBuddy、
DSH；Python 3.11+，Qwen 还需 Node.js 22+）。myagents 不会自动安装或修改它们，装好后用
`/agents` 查看检测结果、`/agents rescan` 重扫、`/agents disable|enable <name>` 开关。

可选：`uv tool install --editable /absolute/path/to/myagents` 后可在任意目录直接运行
`myagents`；长任务用 `myagents daemon start` 持有、`myagents attach` 反复附着，退出
attach 不会取消任务。

## 能力

| 点名 | 用法与权限 |
| --- | --- |
| `@kimi` / `@opencode` / `@qwen` / `@codebuddy` / `@dsh` | 持久 ACP session；写操作进 TUI 权限弹窗，只读轮切换到只读 profile；Kimi/OpenCode 在建 session 失败时有只读降级，Qwen/CodeBuddy/DSH 无降级 |
| `@codex` | 官方 app-server 长驻进程 + 原生 thread |
| `@pi` | 官方 RPC；风险工具逐次 `allow_once`，不激活其原生内置工具 |
| `@claude` | 官方 headless stream-json；工具审批经 MCP 权限桥桥回 TUI，只读轮用 `--restricted` 闭集 |
| `@host` | 意图识别、路由、直接回答、讨论主持与总结；默认用内置无工具模型 runtime，也可切到任一 host-safe agent，始终只读 |

- **路由与协作**：显式 `@agent` 优先；多个点名默认并发；“先让 A 调研，再让 B 设计，最后
  让 C 复核”进入 2–4 步有序接力（后一步能看到前一步的真实回复）；“你们讨论两轮”进入
  2–3 人、1–3 轮有界讨论；`/workflow` 走固定的 review → implement → verify 里程碑流程，
  运行中可用 `/steer` 追加约束。
- **会话**：`Ctrl+N` 新建隔离会话，`Ctrl+O` 在多个项目/会话间搜索切换，后台完成标未读；
  对话、执行事件与 agent session 映射全部落盘，重启后恢复历史并续接原 session。
- **上下文**：每个 agent 只收自己没见过的新消息；`/context` 查看各目标的字符预算与
  checkpoint，`/compact` 手动压缩，原生 Host 在安全回合边界自动压缩；完整时间线永不因
  压缩被改写。
- **权限**：默认 deny，弹窗显示来源 agent、工具和脱敏参数，按风险分级；`/yolo` 可对当前
  会话自动批准 `allow_once`（不持久化、不放宽只读边界）；`Esc` 精确取消当前命令，Enter
  排队、`Alt+↑` 尝试向运行中的任务插话。
- **可观测**：`Ctrl+G` 打开任务活动区，查看每个任务的阶段、工具、权限与输出统计；
  回复中的 Markdown 渲染成终端样式，流式原位合并；`/sessions`、`/roles`（会话级自然语言
  角色）、`/paste-image`（粘贴剪贴板截图）等命令开箱即用。
- **外部集成**：`myagents remote` 提供只监听本机的网页伴侣（Bearer token），可查看、
  提交、取消任务；MCP bridge 提供 `myagents_send_message`、`myagents_read_timeline`、
  `myagents_read_events`、`myagents_get_command`、`myagents_wait_command`、
  `myagents_cancel_command`、`myagents_steer_command`、`myagents_get_room` 八个工具，
  让本机其他 agent 向正在运行的房间提交任务和读取进度（不会自动拉起第二个编排器）。

## 架构

```text
┌────────────────────────────────────────────┐
│ main.py / runtime_daemon.py                 │
│ 前台 TUI owner 或单 room daemon owner       │
└──────────────────────┬─────────────────────┘
                       │
┌──────────────────────▼─────────────────────┐
│ orchestrator.py                            │
│ routing / AgentSpec / history / fan-out    │
└─────────┬──────────────┬────────────────┬──────────┘
          │              │                │
┌─────────▼───────┐ ┌────▼──────────┐ ┌───▼────────────────┐
│ acp/            │ │ pi_rpc/       │ │ codex_app_server/  │
│ Kimi/OpenCode/  │ │ Pi RPC +      │ │ Codex native       │
│ Qwen/CodeBuddy/ │ │ permission    │ │ runtime            │
│ DSH             │ │ bridge        │ │                    │
└─────────────────┘ └───────────────┘ └────────────────────┘
┌────────────────────────────────────────────────────────────┐
│ claude_code/  Claude headless stream-json + MCP 权限桥      │
└────────────────────────────────────────────────────────────┘
          │              │                │
          └──────────────┴────────┬───────┘
                                  │ adapters/ JSONL fallback
┌────────────────────────┐  ┌────────────────────────┐
│ storage/               │  │ control/               │
│ RoomStore：timeline +  │  │ CommandBus（FIFO）+    │
│ state + owner lease    │  │ 私有 Unix 控制 socket  │
└────────────────────────┘  └───────────▲────────────┘
                                        │ 只连 socket
                     ┌──────┴────────┬────────────────────┐
                     │ myagents_mcp  │ attach / remote    │
                     │ stdio bridge  │ ControlClient only │
                     └───────────────┴────────────────────┘
```

- 同一个 agent 的任务严格串行，不同 agent 并行执行。
- 每个 `(工作目录, 会话名)` 是一个独立房间，状态存放在
  `${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/`，绝不写进你的项目目录。
- 同一房间同一时刻只允许一个写入进程，第二个实例启动即失败，不会静默共用状态。
- 取消、超时和退出都会完整回收 agent 进程组；已提交的任务不会被重复执行。
- 所有 agent 的写操作都需要在 TUI 明确批准（`/yolo` 除外）；建议在 Git 仓库或隔离
  worktree 中工作，交付前检查 diff。

## 文档

- [AGENTS.md](AGENTS.md) / [HARNESS.md](HARNESS.md)：贡献者与 agent 协作契约、工程红线。
- [docs/SPEC.md](docs/SPEC.md)：关键行为与验收证据；[docs/adr/README.md](docs/adr/README.md)：架构决策索引。
- [docs/acp-migration.md](docs/acp-migration.md)、[docs/concepts.md](docs/concepts.md)、
  [docs/workflow.md](docs/workflow.md)、[docs/knowledge-map.html](docs/knowledge-map.html)。
- 本地开发入口：`bash scripts/check-harness.sh`（红线 + 全部契约测试）。

## 路线图

- [x] M0–M8：统一 TUI、各 agent 原生长连接接入、持久化与恢复、command bus、MCP 入口、
  执行可观测、有界讨论与 workflow、会话级角色、有序协作、上下文压缩、后台 daemon 与
  remote companion。
- [ ] Later：只有出现跨机器、跨组织 agent 协作需求时再评估 A2A。

## 当前限制

- 各 agent 的权限模型和降级策略不同，具体边界见
  [docs/adr/README.md](docs/adr/README.md)；Pi 的权限 bridge 与只读降级都不是 OS
  sandbox，不要用它无人值守处理不受信输入。
- Kimi / OpenCode 的只读降级只能分析不能写入；daemon 首版一次只持有一个房间；remote 网页
  只能拒绝权限、不能批准。
- 长会话内存/成本、同一本地模型的并发吞吐尚未压测。完整的真实验收记录与未覆盖项见
  [docs/SPEC.md](docs/SPEC.md)。

# myagents

在一个终端里驱动多个 coding agent：点名分派任务、并行获取回复，或安排调研、实现与复核接力。

myagents 是基于 Python + Textual 的本地编排器，支持 Kimi、Codex、OpenCode、Qwen Code、
CodeBuddy、DeepSeek Harness（DSH）、Pi 和 Claude Code。所有 agent 共享一条时间线，
各自保留原生会话；消息路由、工具审批与进程回收由 myagents 统一管理。

## 快速开始

需要 **Python 3.11+**。按需安装并配置要使用的 agent CLI；Qwen Code 还需 Node.js 22+。
myagents 只检测可用性，不会自动安装或修改这些 CLI。

### 1. 安装

```bash
git clone https://github.com/Bryant-Yang/myagents.git
cd myagents
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 配置 Host

Host 负责意图识别、路由与主持，默认使用内置的无工具模型 runtime。以下示例需要先在
`http://127.0.0.1:1234/v1` 启动 OpenAI-compatible 模型服务；用此命令查看模型的精确 id：

```bash
curl -fsS http://127.0.0.1:1234/v1/models | .venv/bin/python -m json.tool
```

创建配置文件（默认 `~/.config/myagents/config.toml`，支持绝对路径的 `XDG_CONFIG_HOME`）：

```bash
case "${XDG_CONFIG_HOME:-}" in
  /*) myagents_config_home="$XDG_CONFIG_HOME" ;;
  *) myagents_config_home="$HOME/.config" ;;
esac
install -d -m 700 "$myagents_config_home/myagents"
${EDITOR:-vi} "$myagents_config_home/myagents/config.toml"
chmod 600 "$myagents_config_home/myagents/config.toml"
```

填入以下内容，替换 `model_id`。配置必须是权限 `0600` 的普通文件，不能是符号链接。

```toml
[host.model]
provider = "openai-compatible"
base_url = "http://127.0.0.1:1234/v1"
model_id = "替换为 /v1/models 返回的精确 id"
```

其他模型服务、凭据与 agent Host 配置见 [Host 配置说明](docs/adr/0017-native-model-backed-host.md)。

### 3. 启动

```bash
.venv/bin/python main.py /path/to/project
```

进入 TUI 后，用 `/agents` 查看可用 agent，`/agents rescan` 重新检测，
`/agents disable|enable <name>` 开关。也可用 `uv tool install --editable /absolute/path/to/myagents`
安装命令行入口，再从任意目录运行 `myagents /path/to/project`。

## 怎么使用

显式 `@agent` 优先；没有点名时由 Host 直接回答或分派任务。

| 场景 | 输入示例 | 执行方式 |
| --- | --- | --- |
| 单独提问 | `@codex 解释这个模块的入口` | 指定 agent 流式回复 |
| 并行分析 | `@kimi @codex 分别分析这个方案的风险` | 多个 agent 同时执行 |
| 有序接力 | `先让 @kimi 调研，再让 @codex 实现，最后让 @opencode 复核` | 2–4 步串行，后一步读取前序真实回复，失败或取消即停 |
| 有界讨论 | `@kimi @codex 讨论两轮这个方案` | 2–3 个 worker、1–3 轮，最后由 Host 主持总结 |

里程碑流程固定为 review → implement → verify，只读复核、单写者，最多一次修复与复验：

```text
/workflow --reviewer @kimi --implementer @codex --verifier @opencode -- 修复登录异常
```

- **会话与上下文**：`Ctrl+N` 新建隔离会话，`Ctrl+O` 搜索切换；历史、执行事件与会话映射落盘。
  每个 agent 只接收未读的增量消息；`/context` 查看预算与 checkpoint，`/compact [@agent]`
  手动压缩，模型 Host 在安全边界自动压缩，完整时间线保留。
- **提交与取消**：Enter 排队，`Esc` 取消当前命令；`Alt+↑` 尝试把最早排队的命令插入当前
  Pi/Codex 任务，受 adapter 能力限制；workflow 用 `/steer` 在阶段边界追加约束。
- **状态与辅助命令**：`Ctrl+G` 查看任务阶段、工具、权限和输出统计；`/sessions` 查看会话，
  `/roles` 查看会话级角色，`/paste-image` 粘贴剪贴板截图。回复支持流式 Markdown 渲染。
- **后台与外部入口**：`myagents daemon start` 后可用 `myagents attach` 反复附着，退出 attach
  不会取消任务。`myagents remote` 提供本机网页伴侣；[MCP bridge](docs/adr/0001-persistent-room-command-bus-mcp.md)
  允许其他本机 agent 提交、读取、等待、取消与 steering，统一连接正在运行的房间。

## Agent 接入

优先使用官方有状态长连接。协议差异由 adapter 处理，agent 之间不直接通信。

| 点名 | Transport | 权限与降级边界 |
| --- | --- | --- |
| `@kimi` / `@opencode` | ACP-first | 仅在获证的 session 建立前失败时允许只读 JSONL 降级，提交后不跨协议重放 |
| `@qwen` / `@codebuddy` / `@dsh` | ACP-only | 固定安全 profile，无自动降级；CodeBuddy 使用独立官方 CLI |
| `@codex` | 官方 app-server | 长驻进程与原生 thread |
| `@pi` | 官方 RPC | 固定权限 bridge 与工具闭集，风险工具逐次 `allow_once`，不激活原生内置工具 |
| `@claude` | 官方 headless stream-json | MCP 权限桥接回 TUI，只读轮使用 `--restricted` 工具闭集 |
| `@host` | 默认内置模型 runtime | 无工具；可显式切换已就绪的 host-safe agent，独立会话且始终只读 |

## 安全与架构

工具权限默认拒绝，风险操作在 TUI 中逐次审批，弹窗展示来源 agent、工具与脱敏参数。
`/yolo` 仅在当前会话自动批准 `allow_once`，退出后失效，也不能放宽只读边界。
建议在 Git 仓库或隔离 worktree 中运行，交付前检查 diff。

```text
前台 TUI / 后台 daemon（房间唯一 owner）
  ├─ Orchestrator：路由、增量上下文、任务调度
  │    └─ adapter → ACP / app-server / RPC / stream-json → agent 进程
  ├─ RoomStore：时间线、执行事件、会话状态、owner lease
  └─ CommandBus：FIFO 队列 + 私有 Unix socket
       └─ ControlClient ← attach / remote / MCP bridge
```

- 每个 `(工作目录, 会话名)` 是独立房间，同一房间只允许一个 owner；第二个实例启动即失败。
- 同一 agent 的任务串行，不同 agent 可并行；已确认投递或结果不确定的任务不会自动重放。
- 状态存放在 `${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/`，不写入项目目录；
  取消、超时与退出按 transport 契约停止任务并回收进程组。

## 当前限制

- 权限 bridge 与只读降级不是 OS sandbox；各 agent 的权限与恢复边界见 [架构决策](docs/adr/README.md)。
- Kimi/OpenCode 的降级只能分析；daemon 一次持有一个房间；remote 仅监听 loopback，
  使用 Bearer token，可查看、提交、取消任务与拒绝权限，不能批准权限。
- 长会话内存、成本与同一本地模型的并发吞吐尚未压测；真实验收记录与未覆盖项见 [SPEC](docs/SPEC.md)。

## 文档与开发

| 文档 | 内容 |
| --- | --- |
| [AGENTS.md](AGENTS.md) · [HARNESS.md](HARNESS.md) | 协作契约与工程红线 |
| [SPEC](docs/SPEC.md) · [ADR 索引](docs/adr/README.md) | 行为验收、独立证据与架构决策 |
| [核心概念](docs/concepts.md) · [工作流](docs/workflow.md) | 项目术语与按任务选读入口 |
| [协议接入](docs/acp-migration.md) · [知识地图](docs/knowledge-map.html) | Transport 契约与模块关系 |

本地验证：`bash scripts/check-harness.sh`（红线检查与全部契约测试）。

## 路线图

- [x] M0–M8：统一 TUI、原生长连接、持久化与恢复、MCP、任务可观测、讨论与 workflow、
  会话角色、有序协作、上下文压缩、daemon 与 remote companion。
- [ ] 出现跨机器、跨组织协作需求后，再评估 A2A。

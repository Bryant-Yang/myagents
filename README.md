# myagents

原生长连接优先的本地多 agent 终端编排器：在一个 Textual TUI 中点名 Kimi、
Codex、OpenCode、Qwen Code、CodeBuddy、DeepSeek Harness（DSH）、Pi 等 coding agent，共享时间线、流式接收回复，并统一处理权限、
上下文和进程生命周期。

> 当前状态：M2.5、M3、M3.1、M4、M4.2、M4.3、M4.4、M4.5、M4.6、M4.7、M4.8、M4.9、M4.10、M4.11、M4.12、M4.13、M4.14、M5.1、M5、M6 与 M7 已完成。
> 共享 timeline 与执行 events 持久化、ACP session
> 恢复、房间单写者 lease、内部 command bus、本机控制 socket 与 MCP
> stdio 外部入口、执行心跳、精确取消、Codex app-server 长连接与同项目独立会话均已落地。
> Kimi/OpenCode 使用 ACP-first + prepare-only 只读 JSONL fallback，Qwen Code /
> CodeBuddy/DSH 使用 ACP-only，Pi 使用官方 RPC + 权限 bridge，Codex worker 使用官方 app-server；host 使用 myagents 原生模型 runtime；JSONL 不会在已提交任务后跨协议重放。真实 Kimi + MCP
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
- `@qwen`：通过 `qwen --acp` 使用持久 ACP session；普通轮强制 approval
  `default`，workflow 只读轮强制 `plan`，避免继承 native TUI 的 auto/yolo。
  当前不启用 headless JSONL fallback，避免在独立降级 profile 尚未验证前扩大权限面。
- `@codebuddy`：通过独立 CodeBuddy CLI 的官方 ACP stdio 使用持久 session。
  普通轮固定 `default` 权限模式；workflow 只读轮强制 `dontAsk` 并把工具闭集
  收口为 `Read,Glob,Grep`，profile 切换时重建进程/session。已有登录态会直接
  复用；只有 CLI 明确返回需要认证时才在系统浏览器打开官方登录页，认证等待
  有限超时；不提供 JSONL fallback。
  它不是 WorkBuddy App，也不继承该 App 的连接器或授权；`@workbuddy` 不再注册。
- `@dsh`：通过 stock `dsh --profile myagents` 加载 myagents 标准 bundle，使用持久
  ACP session。普通/写轮固定 `DSH_ACP_PROFILE=workspace-write`，workflow 只读轮固定
  `DSH_ACP_PROFILE=read-only`；切换时重建进程/session。
  首轮前强制要求 load/close capability，只有 `end_turn` 记为成功；默认 deny，
  不提供 headless、SDK RPC 或 JSONL fallback。当前自动化只证明 myagents fake
  contract，真实 DSH profile/权限/回收验收见 ADR-0015。
- `@pi`：通过 `pi --mode rpc` 使用持久 session。myagents 启动的 Pi 进程关闭
  自动联网下载/更新与自动发现，不激活原生命名 built-in tools，只加载固定 permission bridge 与唯一命名 wrapper
  工具；启动 attestation 通过后才允许 prompt。普通/写 profile 的 edit、write、
  bash 每次只接受 TUI 的 `allow_once`，只读 profile 不激活风险工具且 hook 再次
  hard-deny；授权弹窗只会在本轮 no-replay checkpoint 已持久化后出现。图片每轮
  最多 16 张、合计 20 MiB；不提供
  ACP 或 JSON/JSONL fallback。
- `@host`：负责意图识别、路由、直接回答、讨论主持和总结。默认由 myagents
  自有的无工具 native model runtime 驱动，也可按会话显式切换到注册为 host-safe
  的完整 agent；agent Host 独立于同名 worker 且始终只读，`/yolo` 不扩权。
- Agent 就绪中心：启动时只读取当前进程的 PATH、环境变量和可执行文件属性，
  不启动或安装 agent。`/agents` 显示注册项的可用状态和设置提示，
  `/agents rescan` 在用户修复 PATH 或安装后被动重扫；缺失目标会在时间线写入前
  整条拒绝并保留输入草稿。`/agents disable kimi` 可全局禁用一个 worker，
  `/agents enable kimi` 无需重装即可恢复；禁用不删除登录态、会话或历史。
- 无显式 mention：host 用一次调用决定“直接回答”或输出结构化 worker
  路由；直接回答时不再发起第二次 host 调用。
- 多 agent fan-out：同一消息可同时点名多个 agent，并发执行。
- 自然语言有序协作：直接说“先让 A 调研，再让 B 基于结果设计，最后让 C
  复核并交付”，host 会生成 2–4 步固定计划；Orchestrator 严格串行执行，后一步
  能看到前序真实回复。无需 `/task`，中间失败或取消后不再启动后续步骤。
- 有界讨论：可直接说“@A @B 你们讨论两轮……”，或使用 `/discuss` 精确指定
  参与者、轮数和 moderator；两种入口都复用同一个 2–3 人、1–3 轮状态机。
- 有界工作流：`/workflow` 固定 reviewer、唯一 implementer 和 verifier，依次
  review → implement → verify；复核不通过时最多一次 repair/reverify，最后由
  host 如实汇总。Git baseline/candidate、execution mode 和失败终态由普通代码
  锁定，运行中可用有界 `/steer` 补充尚未开始阶段的约束。
- ACP 增量上下文：每个 stateful agent 独立维护 history cursor。
- 并发顺序保证：同一 ACP agent 严格串行，不同 agent 保持并行。
- 持久时间线：房间 timeline/state 落盘（单调 seq、UTC 时间戳），TUI 重启
  后恢复显示历史。
- 独立会话：同一工作目录可通过 `Ctrl+N` 或精确输入 `/new` 新建完全隔离的
  timeline、events、cursor 与原生 agent session；默认会话继续兼容已有历史。
- 会话工作台：`Ctrl+O` 或 `/sessions` 搜索当前/全部项目会话，任务运行时也能
  切换；每个会话保留独立草稿、状态与未读标记，并支持重命名和确认后永久删除。
- ACP session 恢复：重启后优先 `session/load` 续接旧 session，保留已持久化
  cursor；仅标准 resource/method-not-found 或具体 adapter 已获证的精确映射
  可回退新 session，不支持 load 时直接新建；其他错误 fail-closed。
- 原子 checkpoint：cursor/session_id 在 prompt 前一次性落盘，失败不伪装
  成功；用户消息持久确认后才在 TUI 显示。
- 房间单写者 lease：owner.lock（flock）保证同一房间同一时刻只有一个
  写入进程。
- 内部 command bus：TUI 输入与外部控制统一的 FIFO 入口；`request_id`
  幂等去重；单 worker 串行执行，命令状态可查、可有界等待。
- 本机控制 socket：TUI 私有的 Unix socket JSONL 协议（**不是 MCP**）；
  socket/endpoint 0600，活跃房间不被第二个 server 抢占，stale 文件
  只在确认无监听者后清理。
- MCP stdio bridge：八个 `myagents_*` 工具把本机其他 agent 的 review
  注入同一房间；bridge 不创建第二 Orchestrator、不直写 timeline、
  不绕过 TUI 权限。
- 可观测执行：独立 events 日志、累计且原位更新的静默 heartbeat、工具/权限
  上下文与重启中断提示；固定任务区持续显示总状态、耗时和各 agent 阶段，
  部分 agent 成功、部分失败时明确显示“部分完成”。固定区首行同时显示当前
  Host backend、就绪 Agent 数，以及其他会话的运行中/未读计数。
- 执行活动卡：同一任务的路由、阶段、heartbeat、工具与权限过程合并成一张
  原位更新的摘要卡，并放在聊天主线之外的独立任务活动区；默认只显示终态、
  当前阶段和工具汇总。按 `Ctrl+G` 进入
  活动区，使用 `↑↓` 选择、`Enter` 独立展开或收起当前卡、`Esc` 返回输入框；
  `/details` 可直接切换当前选中卡，否则切换最近一张卡。展开后按需显示持久
  过程概览、阶段、工具/权限、插话/取消与输出统计；回答正文仍只在聊天主线，
  无工具的直接回答也会明确说明。重启后首次 `/details` 可恢复最近任务，不会
  在启动时刷满历史活动。用户消息、agent 回复
  和失败仍保持在聊天主线上，
  会话切走和后台完成不会丢卡；近期明细有界保留，独立执行日志继续完整记录
  状态迁移。
- 聊天正文呈现：agent/host 回复中的加粗、斜体、行内代码、标题、列表、引用与
  fenced code block 会转换成安全的终端样式，不再显示 Markdown 控制符；用户
  输入、系统状态和活动卡保持原文，流式回复仍原位合并。
- 明确委托：host 路由同时给每个 worker 生成完整 task，消解“你/让 Kimi”
  等角色关系，并直接注入本轮 prompt，不再只显示路由理由。
- 多行输入：`Shift+Enter` 换行，输入框按内容在 3–8 行间增长；固定提示会根据
  空闲或运行状态切换“发送”与“排队/插话/取消”语义。
- 默认权限弹窗：显示会话、来源 agent、工具标题和经过脱敏的人类可读字段；
  `allow_once`、长期允许、拒绝与“取消整个任务”使用明确的风险文案和层级。
- 错误不丢失：最新错误固定显示在输入区上方，同时仍保留在聊天证据中；下一次
  消息被 CommandBus 成功接受后清除固定提示。
- 精确取消：正常输入态按 `Esc`（`Ctrl+X` 仍兼容）或通过 MCP，只取消当前
  command，房间继续工作；补全、会话弹窗和活动导航先关闭自身。
- 运行中插话：先按 Enter 把输入加入 FIFO，再按 `Alt+↑` 提升最早的排队输入。
  workflow 在下一阶段边界采纳；普通任务只有唯一活动且 adapter 有已验收能力时
  接受（当前 Pi 走官方 native `steer`，Codex 走 app-server `turn/steer`）。输入框
  里尚未提交的草稿不参与；ACP/native host、多目标或无活动目标明确拒绝，原队列
  顺序保持不变。
- 权限 fail-closed：无处理器、异常或非法 option 一律拒绝。
- 流式回复合并：ACP token/chunk 持续更新同一条 TUI 记录，不再一词一行。
- 有状态 agent 卡死回收：普通分析连续 300 秒无协议事件才取消；ACP 与 Pi RPC
  在进入已跟踪的工具生命周期后使用独立 15 分钟无活动上限，避免工程子代理
  或长命令被普通静默阈值误杀。Codex app-server 当前仍使用统一普通预算。
  统一预算避免长 prompt prefill 被误判为卡死，任意协议活动都会重新计时。
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
└─────────┬──────────────┬────────────────┬──────────┘
          │              │                │
┌─────────▼───────┐ ┌────▼──────────┐ ┌───▼────────────────┐
│ acp/            │ │ pi_rpc/       │ │ codex_app_server/  │
│ Kimi/OpenCode/  │ │ Pi RPC +      │ │ Codex native       │
│ Qwen/CodeBuddy/ │ │ permission    │ │ runtime            │
│ DSH             │ │ bridge        │ │                    │
└─────────────────┘ └───────────────┘ └────────────────────┘
          │              │                │
          └──────────────┴────────┬───────┘
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
之间不直接通信。ACP/Pi RPC/app-server 只负责“如何驱动 agent”，不参与“任务应该
派给谁”的决策。

## 环境要求

- macOS 或 Linux
- Qwen Code 可选：Node.js 22+，且 `qwen` 命令可从 PATH 解析。若使用本地源码，
  先在源码仓库执行 `npm install && npm run build`，再按其贡献指南将
  `packages/cli` 链接为 `qwen` 命令。
- Python 3.11+
- 需要使用的 agent CLI 已安装并完成登录
  - [Kimi Code CLI](https://www.kimi.com/code)
  - [OpenAI Codex CLI](https://developers.openai.com/codex/cli)
  - [OpenCode](https://opencode.ai/)
  - [Qwen Code](https://github.com/QwenLM/qwen-code)
  - Pi Coding Agent：`pi` 命令必须能从当前进程 PATH 解析，并支持
    `pi --mode rpc`。myagents 只给子进程传入隔离参数，不修改用户已有 Pi 配置。
  - [CodeBuddy Code CLI](https://www.codebuddy.cn/docs/cli/installation)：安装可独立
    运行的官方 CLI，例如 `npm install -g @tencent-ai/codebuddy-code`。项目不会
    调用 WorkBuddy.app 包内私有二进制；若 CLI 不在 PATH，用
    `MYAGENTS_CODEBUDDY_CLI=/absolute/path/to/codebuddy` 指定。
  - DeepSeek Harness：安装官方 `dsh` CLI，或准备一个已安装依赖且已构建官方 CLI
    的 stock DSH 源码树。myagents 自己拥有 `dsh_acp/plugin` 中的标准
    `@myagents/dsh-acp-host` bundle、ACP server 与产品 host；不会复制或修改 DSH 的
    core、官方 ACP 包、示例和测试。readiness 也不运行 pnpm/build/CLI 或真实 agent。

先把 canonical source 打成标准 tarball，再用官方 plugin 命令把 bundle 安装进固定
profile。打包只写指定输出目录，不修改 DSH checkout 或用户 profile：

```bash
mkdir -p /tmp/myagents-dsh-package
MYAGENTS_DSH_SOURCE_ROOT=/absolute/path/to/deepseek-harness \
  bash scripts/package-dsh-plugin.sh /tmp/myagents-dsh-package
```

安装后的
`$DSH_HOME/profiles/myagents/package.json` 必须按顺序只列出
`@deepseek-ai/dsh-base` 与 `@myagents/dsh-acp-host` 两个 bundle：

```bash
DSH_HOME=/absolute/profile-home \
  /absolute/path/to/dsh plugin --profile myagents add \
  /tmp/myagents-dsh-package/myagents-dsh-acp-host-0.1.0.tgz --offline
```

源码运行时把 `/absolute/path/to/dsh` 换成
`node /absolute/path/to/deepseek-harness/apps/cli/lib/bin.js`。不要直接安装
`dsh_acp/plugin` 源码目录；发布入口固定为 tarball 内的 `lib/index.js`。

随后二选一配置启动入口：

```bash
export MYAGENTS_DSH_CLI=/absolute/path/to/dsh
# 或：stock 源码树必须已有依赖和已构建的 apps/cli/lib/bin.js
export MYAGENTS_DSH_SOURCE_ROOT=/absolute/path/to/deepseek-harness
```

DSH 状态默认保存到
`${XDG_STATE_HOME:-~/.local/state}/myagents/dsh-acp`，不污染工作区；可用绝对
`DSH_ACP_PERSISTENCE_DIR` 覆盖。其下固定为 `sessions/`、`runtime-home/` 和
`attachment-home/`。子进程的 `DSH_HOME` 保留为上述 stock profile home；只有
`DSH_AGENTS_HOME` 绑定产品派生状态。状态目录不能与 DSH checkout、canonical
plugin/CLI、profile home 或目标 workspace 任一方向重叠；目标 workspace 也不能包含
执行入口或原始 settings/credentials。adapter 从 profile home 对
`settings.yaml` / `.credentials.yaml` 做 no-follow 有界读取，再原子复制到
`config-inputs/` 的 mode-0600 产品文件；host 只挂载副本，不修改用户原文件。

源码模式把 DSH checkout 当作不可变依赖。若某项能力只能通过修改 DSH 本体或从
未导出的包内 `src/*` 深层导入才能实现，该能力会被阻断，而不会在 DSH 仓库打补丁。
readiness 纯读取官方 CLI、`profiles/myagents/package.json`、exact bundle 顺序、
依赖解析后的 `@myagents/dsh-acp-host@0.1.0` manifest、
`dsh.bundle.patch=./cordis.patch.yml`、entry 与 patch 文件，并以 no-follow 稳定读取和
checked-in SHA-256 contract 拒绝同名同版本的产物漂移；不会执行它们。
`$DSH_HOME/profiles` 与 `profiles/myagents` 必须是 profile home 内真实、非 symlink
的 canonical 目录，manifest 通过 no-follow、有界、单链接稳定快照读取。stock DSH
会在 bundle 后继续应用 `$DSH_HOME/cordis.patch.yml` 与
`$DSH_HOME/profiles/myagents/cordis.patch.yml`；两者只能缺失，或忽略空行/注释后
唯一语义行精确为 `[]`（文件至多 64 KiB）。空文件、仅注释文件、任何有效 patch
或 symlink/hardlink 都会使 DSH fail-closed；spawn 前和复用进程的下一轮前会重验。
`MYAGENTS_DSH_SOURCE_ROOT` 只定位已构建官方 `apps/cli/lib/bin.js`；产品 host 组合由
profile 中的标准 bundle 加载，readiness 不审计整个源码树。启动后的 DSH 子进程 cwd 固定为
本轮目标 workspace，不能用同一活跃 session 跨目录工作。

DSH 持久化固定为 uncompressed、unpacked JSONL。`session/load` 在 DSH materialize
历史前通过公开 `list`/`locate` seam 做 no-follow 文件预检，硬限制 4096 事件 / 16 MiB，
并在 materialize 与 resume 前复核同一文件身份；无法证明边界时不广告恢复能力。

标准 bundle/host 的 release contract 可对已安装依赖的 DSH checkout 与临时
`DSH_HOME` profile 运行；默认 Harness 不要求本机存在 DSH：

```bash
MYAGENTS_DSH_SOURCE_ROOT=/absolute/path/to/deepseek-harness \
  bash scripts/check-dsh-plugin.sh
```

该 release gate 要求 DSH checkout 起始即完全干净，以临时 profile 验证 exact
base → host bundle、entry/patch、permission 与 lifecycle contract；前后比较 HEAD、
Git 状态以及包含 ignored 文件内容 SHA-256 的完整文件树。它只读 DSH，不会向该仓库
安装、生成或写入文件。完整文件树比较只用于证明验收未修改不可变 checkout，不是
readiness 的兼容性 fingerprint；运行时不会扫描或哈希整个 DSH 源码树。

CodeBuddy 当前固定使用官方文档中的中国区环境 `internal`。连接优先复用 CLI
已有登录态；仅当 `session/new` 明确返回 `Authentication required` 时，才使用
`MYAGENTS_CODEBUDDY_AUTH_METHOD`（默认 `internal`）发起认证，并且该 method 必须
由本次 `initialize.authMethods` 公布。其他区域 profile 尚未独立验收。

只使用某一个 agent 时，不要求安装其他 worker CLI。无 mention 路由和
`@host` 只需要配置一个模型 API；LM Studio 可直接提供首版支持的
OpenAI-compatible 接口。

myagents 不会自动安装、卸载或修改这些 CLI。聊天室启动后可输入 `/agents`
查看“当前进程检测到的状态”；完成外部安装或 PATH 调整后输入
`/agents rescan` 即可，无需重启聊天室。

不再使用某个 Agent 时，可在聊天室执行：

```text
/agents disable kimi
/agents enable kimi
```

开关保存在私有的 `~/.config/myagents/config.toml` 中，对之后启动的聊天室也生效。
当前进程的所有已加载会话立即同步；其他已经运行的 myagents 进程执行
`/agents rescan` 后同步。被禁用项仍出现在候选和 `/agents` 状态中，但不能参与
显式派发、Host 路由、讨论、协作、workflow 或充当 Agent Host。正在执行的任务
不会被强制中断。

## 源码级全局命令

使用 uv 的 editable tool 安装后，可以在任意目录运行 `myagents`，同时始终加载
当前 checkout 中的源码，不复制另一份代码，也不修改 shell 配置：

```bash
uv tool install --editable /absolute/path/to/myagents
myagents --help
```

直接修改 Python 源码后，下一次启动立即生效。只有 `pyproject.toml` 中的依赖或
命令入口发生变化时，才需要刷新 tool 环境：

```bash
uv tool install --editable --force /absolute/path/to/myagents
```

不带工作目录参数时，启动命令所在的当前目录就是 agent workspace；也可以显式
指定项目和命名会话：

```bash
myagents /absolute/path/to/project
myagents --session review /absolute/path/to/project
```

卸载全局入口不会删除源码或会话状态：

```bash
uv tool uninstall myagents
```

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
.venv/bin/python main.py
```

配置文件内容（兼容的默认 profile）：

```toml
[host.model]
provider = "openai-compatible"
base_url = "http://127.0.0.1:1234/v1"
model_id = "把这里替换为 /v1/models 返回的精确 id"
```

默认路径遵守 XDG：`$XDG_CONFIG_HOME/myagents/config.toml`，否则使用
`~/.config/myagents/config.toml`；相对的 `XDG_CONFIG_HOME` 无效并回退该默认
路径。文件必须为普通文件且权限精确为 0600，不接受符号链接。

也可以配置多个命名 profile；远程凭据只引用环境变量名：

```toml
[host.models.local]
provider = "openai-compatible"
base_url = "http://127.0.0.1:1234/v1"
model_id = "从 /v1/models 返回的精确 id"
models_discovery = true

[host.models.glm]
provider = "openai-compatible"
base_url = "由你明确配置的服务地址"
model_id = "glm-5.3-flash"
api_key_env = "ZAI_API_KEY"
models_discovery = false
```

`models_discovery=true` 会先用 `/models` 精确校验；不提供模型列表的服务设为
`false`，由首次 chat 请求接受或拒绝 exact `model_id`。myagents 不猜别名、不
伪造模型列表，也不自动 fallback。`glm-5.3-flash` 在此是用户指定的精确 ID，
不是本文对厂商模型枚举的声明。`MYAGENTS_MODEL_*` 仅用于当前进程临时覆盖。

Host 可在当前会话中查看和切换；命令不进入聊天时间线：

```text
/host
/host model local
/host model glm
/host agent codex
```

切换只允许在当前会话无运行/排队任务时执行并持久恢复。Codex Host 与
`@codex` worker 使用独立进程/session/writer，Host 永远只读；`/yolo` 不扩权。
选择不可用时保留选择并显示错误，不会偷偷换回其他 backend。`/agents` 也会显示
当前 Host transport/readiness。

也可以指定 agent 的工作目录：

```bash
.venv/bin/python main.py /path/to/project
```

按名称恢复同一项目中的独立会话：

```bash
.venv/bin/python main.py --session game-review /path/to/project
```

对明确信任的 workspace，可在聊天室输入下面的命令，为当前会话自动批准每个
`allow_once` 权限请求：

```text
/yolo
```

再次输入 `/yolo` 即关闭。该模式按会话隔离，退出后不记忆，不选择
`allow_always`，也不放宽 workflow
的 review/verify/final 只读边界。它不是 OS sandbox，不应用于无人
值守处理不受信输入。

每个 `(工作目录, 会话名)` 对应一个持久房间：对话 timeline、执行 events、
图片 attachments、agent cursor/session 映射和
owner lease 存放在 `${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/<room_id>`，
不会写进目标工作区。未指定名称时使用兼容旧历史的 `default` 会话；同一房间
同一时刻只允许一个 TUI 实例写入。

### 会话级自然语言角色

无需维护永久角色库，直接在聊天中指定即可：

```text
@qwen 接下来你担任产品研究员，先核对事实并列出未知项
@opencode 你在本会话担任反方审查者，重点寻找反例
@qwen 继续分析下一项
@qwen 不再担任这个角色，恢复普通助手
/roles
/roles clear
```

角色会在当前命名会话的后续任务中持续生效，并在活动卡和任务状态中显示为
`qwen · 产品研究员（本会话）`。切换到其他会话不会继承；关闭程序后重开同一
会话则会恢复。角色只是工作视角，不能改变实际 agent、工具权限、runtime、讨论
成员/轮数或 workflow 固定职责。`/roles` 查看当前会话全部角色，
`/roles clear` 在没有运行/排队任务时原子清空；这两个命令都不会进入对话或调用
模型。完整契约见
[ADR-0011](docs/adr/0011-session-scoped-natural-language-roles.md)。

## 使用方式

```text
@kimi 解释这个模块，并给出最小修改方案
@codex review 当前实现，只报告可复现问题
@kimi @opencode 分别提出一个方案
先让 @kimi 调研现状，再让 @opencode 基于结果提出方案，最后让 @qwen 复核并交付
@qwen 检查当前模块并给出最小修复
@qwen 接下来担任产品研究员，汇总现有证据并列出未知项
@host 总结上面两个方案的分歧
@kimi @opencode 你们讨论两轮为什么这个方案有风险
/discuss @kimi @opencode --rounds 2 --moderator host -- 讨论新增 adapter 的协议选择
/workflow --reviewer @kimi --implementer @codex -- 给解析器补边界测试并验收
/steer -- 额外覆盖空输入，保持现有公开 API
```

前一任务运行时，Enter 先按 FIFO 排队；`Alt+↑` 把队首等待输入提升为尝试影响
当前运行的插话。有多条时只取最早一条，其余继续排队；输入框草稿不参与。
插话不创建新 command，也不进入共享对话历史。完整边界见
[ADR-0018](docs/adr/0018-capability-bounded-runtime-interjection.md)。

自然语言协作与普通多点名是两种不同语义：`一起、分别、各自` 保持并发 fan-out；
明确的“先…再/然后…最后…”进入有序接力。未点名时 host 也可在 ready worker
闭集内识别有序计划；显式点名时参与者闭集不可增删。计划固定为 2–4 步且至少
两个不同 worker，同一 worker 可在后续再次修订。整个接力只有一个 command 和
一条 user timeline，失败/取消即停，不自动重试、换人、跳步或追加 host 总结。
最后一步直接产生面向用户的交付。完整契约见
[ADR-0013](docs/adr/0013-natural-language-sequential-collaboration.md)。

自然语言中的“你们讨论、辩论、互相点评、交叉评议”会进入有界讨论；显式点名
固定 2–3 个参与者，未点名时 host 只能从 ready worker 中选择。默认两轮并由
`host` 总结，用户可明确说一至三轮。`一起分析、分别回答、各自给建议` 以及
`不要/不用讨论` 等否定表达仍是一次 fan-out；明确“先 A 再 B”优先进入有序
协作。需要精确自定义 moderator 时继续使用
`/discuss`。自然语言轮数建议写成“讨论三轮：主题”；`三轮融资/一轮明月` 等
无边界复合词会保留为主题并使用默认两轮。两种入口的轮次都由普通代码推进，
agent 不能自行加轮或拉人；讨论
只要求文字观点，不用于并发修改代码。完整契约见
[ADR-0008](docs/adr/0008-bounded-multi-agent-discussion.md)。

`/workflow` 只接受干净 Git 工作区；reviewer/verifier 默认只读，只有固定的
implementer 可写。未指定 `--verifier` 时由 reviewer 复核。`/steer` 只作用于
当前活动 workflow，且只在 review/implement/repair 阶段接受；TUI 固定任务区会
显示当前 workflow 阶段、审/写/验角色、各角色“进行中/等待”状态和 steering
是否仍可用。完整契约见
[ADR-0009](docs/adr/0009-bounded-milestone-workflow-steering.md)。

按 `Ctrl+N` 或精确输入 `/new` 会直接创建“新会话”；第一条消息会自动生成本地
标题。按 `Ctrl+O` 或输入 `/sessions` 打开会话选择器：默认当前项目，Tab 查看
全部项目，输入文字搜索，Enter 切换，F2 重命名，Ctrl+D 永久删除。切换不会
取消正在运行的任务；后台完成/失败会通知并标记未读。当前或运行中的会话不能
删除，删除其他会话必须完整输入标题确认。

粘贴 macOS 剪贴板中的截图或图片：

1. 快捷方式：先写说明和可选的 `@agent`，再按 `Ctrl+V`；Textual
   文本剪贴板为空时会尝试粘贴系统图片。
2. 稳定方式：单独输入 `/paste-image` 并按 Enter，再围绕插入的图片引用
   补充说明和可选的 `@agent`。
3. 确认图片引用已经出现在草稿中，再按 Enter 与文字一起发送。

图片必须能由 macOS 剪贴板提供 PNG 表示，单张不超过 20 MiB。文件保存到当前
房间的私有 `attachments/` 目录（目录 0700、文件 0600），不会写入项目工作区；
草稿只显示 `[图片 1]` 这类短引用。Kimi ACP 与 Codex app-server 会使用各自的
原生图片输入发送可信附件；旧绝对路径引用仍只在原房间信任根内兼容。

路由规则：

1. 显式 `@agent` 永远优先。
2. 同一条消息中的多个有效 mention 默认并发；明确讨论意图时进入有界讨论，
   明确跨 agent 先后关系时进入有序协作。
3. 不带 mention 时，host 在一次调用中直接回答，或根据最近对话选择
   1–2 个 worker；自然语言内容不由本地关键词白名单判断。
4. 单个 agent 失败会写入时间线，不会中断其他 agent；fan-out 全部收尾后，
   只要任一 worker 失败，该 command 终态就是 `failed`。
5. 自然语言讨论与 `/discuss` 复用同一状态机：同轮并发、跨轮串行；失败参与者
   不自动重试，主持人仍总结已有证据，但不能把失败 command 洗成 completed。
6. `/workflow` 固定阶段串行推进，任何阶段、Git 漂移或最终汇总失败都会保留
   独立失败证据，host 不能把失败洗成 completed。

## Transport 与上下文

| Agent | 生产 transport | 上下文策略 | 当前状态 |
| --- | --- | --- | --- |
| Kimi | ACP + 只读 JSONL (`kimi acp` → `kimi -p`) | 持久 session + 增量 history；只有 prepare 失败才降级 | ACP 已验证；hybrid contract 已验收 |
| Codex | app-server (`codex app-server`) | 持久 thread + 增量 history + thread 恢复 | 已接入；JSONL fallback |
| OpenCode | ACP + 隔离只读 JSONL (`opencode acp` → `opencode run`) | 持久 session + 增量 history；风险工具 ask；只有 prepare 失败才降级 | ACP/permission 已验证；hybrid contract 已验收 |
| Qwen Code | ACP (`qwen --acp`) | 持久 session + 增量 history；普通轮 default、只读轮 plan | ACP-only 已验证 |
| CodeBuddy | ACP (`codebuddy --acp --acp-transport stdio`) | 持久 session + 增量 history；default/受限只读 fresh profile | ACP-only 已验证 |
| DSH | ACP（stock `dsh --profile myagents` + `@myagents/dsh-acp-host` bundle） | 持久 session + 增量 history；workspace-write/read-only fresh execution profile；load/close hard gate | ACP-only fake/release contract 与临时 profile 核心真实验收已通过 |
| Pi | RPC (`pi --mode rpc`) | 持久 session + 增量 history；唯一 permission bridge、三 profile fresh session | RPC-only；fake contract 已验收 |
| Claude | 未接入 | 预留 AgentSpec/adapter 扩展点 | 规划中 |

有状态 agent 首次接入只收到最近 `history_limit` 条共享记录；后续只收到 cursor
之后的新消息，并过滤它自己的回复。提交前明确失败时 cursor 不推进、下轮补发；
提交后静默超时等结果不确定失败会先建立 no-replay cursor，防止工具任务被重复
执行。
cursor 是持久化 timeline 的单调 seq：重启后优先使用各 transport 的精确原生
恢复能力续接旧 session 并保留 cursor；恢复失败或 agent 不支持时只按对应
adapter 已冻结的安全契约处理。新 session 的 cursor 归零并按 `history_limit`
有界 bootstrap，不能把失败恢复伪装成命中。

Kimi 的 JSONL 降级是明确的受限模式：内置 agent profile 只允许
`Read` / `Grep` / `Glob`，禁止写入、命令、Skill、子 agent 和 MCP。
降级轮可返回定位与阻塞说明，不伪装成已完成的文件变更。
细节见 [ADR-0006](docs/adr/0006-kimi-hybrid-transport-policy.md)。

OpenCode 正常 ACP 路径额外把默认偏宽的权限收口为 unknown/risky=ask，
由同一 TUI 决策。JSONL 降级使用 `--pure`、禁用项目配置/Claude 兼容层/
自动升级，并通过 inline agent 与 runtime permission 双重限制为
`read` / `glob` / `grep` / `list`。细节见
[ADR-0007](docs/adr/0007-opencode-hybrid-transport-policy.md)。

DSH 只接受官方安装版 `dsh`，或源码树中已构建的官方 CLI；两者都固定启动
`--profile myagents` 并从该 profile 加载 myagents 标准 bundle，readiness 纯被动。
两种 execution safety profile、
load/close、no-replay、cancel/close、仅 `end_turn` 成功与零 fallback 的完整边界见
[ADR-0015](docs/adr/0015-dsh-acp-only-transport.md)。DSH 专用 host 的工具守卫和
真实模型能力没有进入默认 fake gate，缺证据的能力保持 blocked。

Pi 不暴露原生命名 built-in tools 或 raw RPC bash。启动时关闭自动发现，只加载
`pi_rpc/extensions/myagents_permission_bridge.ts`，并核验 nonce/profile/workspace/
policy/tool source attestation；普通/写 profile 的风险工具逐次询权，只读 profile
只有四个读取 wrapper。profile 切换重建进程和新 session，提交后失败 no-replay，
没有 JSON/JSONL fallback。细节见
[ADR-0014](docs/adr/0014-pi-rpc-permission-bridge.md)。该 bridge 不是 OS sandbox；
批准 shell 后仍继承本机进程权限，不适合无人值守处理不受信输入。

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

八个工具：

| MCP tool | 语义 | 注解 |
| --- | --- | --- |
| `myagents_get_room` | 房间、workdir、PID、agent transport | read-only、idempotent、closed-world |
| `myagents_read_timeline` | 按 `after_seq`/`limit`（≤200）读持久时间线 | read-only、idempotent、closed-world |
| `myagents_read_events` | 读生命周期、工具、权限、心跳和 terminal 事件 | read-only、idempotent、closed-world |
| `myagents_send_message` | 提交外部用户消息，立即返回 `command_id` | write、non-idempotent（带 `request_id` 去重）、open-world |
| `myagents_get_command` | 查 queued/running/completed/failed/cancelled | read-only、idempotent、closed-world |
| `myagents_wait_command` | 有界等待（≤30s）状态变化，超时不取消任务 | read-only、idempotent、closed-world |
| `myagents_cancel_command` | 精确取消 queued/running 命令；terminal 幂等 | write、idempotent、closed-world |
| `myagents_steer_command` | 给活动 workflow 追加下一阶段生效的有界指令 | write、non-idempotent、closed-world |

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
- 外部消息触发工具权限时仍由所属会话的当前 TUI 决策；bridge 没有开关或绕过
  入口。默认弹窗，只有该会话已由用户显式输入 `/yolo` 才自动批准；
- TUI 未运行、endpoint stale 或房间不匹配时，工具返回可操作错误
  （含启动命令），不创建任何状态。

## 权限与安全

Kimi/OpenCode ACP 的权限请求会进入 TUI 弹窗。默认策略是 `deny`；
OpenCode adapter 还会把上游默认偏宽的未知及风险工具收口为 `ask`：

- `auto` 只能由明确授权的 client invocation 显式开启，并在该 client
  生命周期内持续生效；
- 生产 TUI 的 `/yolo` 默认关闭；开启后只自动选择当前会话当次 options
  中的 `allow_once`，窗口标题和固定任务区持续显示危险状态；
- `selected.optionId` 必须属于本次 ACP 请求提供的 options；
- 关闭 TUI 时，等待中的权限请求按 cancelled 收尾；
- 一个 ACP session 同一时刻只能有一个 writer；
- 一个房间同一时刻只有一个写入进程：owner.lock（flock）冲突时第二个
  TUI 实例启动即失败，不会抢占或静默共用状态。

Codex worker 使用 `workspace-write + on-request`：超出沙箱的 Git 元数据、
本地 socket 等操作必须进入同一 TUI 权限决策器（默认弹窗）；只读 host 使用
`read-only + never`，不会为路由申请写权限。Kimi/OpenCode 自动 JSONL
fallback 都由 runtime 白名单限制为只读；正常 ACP 写入仍需在 TUI 明确批准，
并建议在 Git 仓库或隔离 worktree 中工作、交付前检查 diff。

## 状态目录、恢复与排障

每个 `(workdir, session_name)` 的房间状态位于
`${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/<room_id>`
（目录 0700、文件 0600、`state.json`/`endpoint.json` 原子写），绝不写进
目标工作区。TUI 重启后恢复时间线显示、agent seq cursor 和 ACP session
映射（优先 `session/load`；仅确定的 session-not-found 回退新
session + 有界 bootstrap，其他错误 fail-closed）。

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
→ DSH ACP-only contract tests
→ Pi RPC/client/adapter/permission bridge contract tests
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
.venv/bin/python tests/test_dsh_acp.py
.venv/bin/python tests/test_pi_rpc_client.py
.venv/bin/python tests/test_pi_adapter.py
.venv/bin/python tests/test_pi_permission_bridge.py
.venv/bin/python tests/test_phase2.py
.venv/bin/python tests/test_storage.py
.venv/bin/python tests/test_m25.py
.venv/bin/python tests/test_m3_bus.py
.venv/bin/python tests/test_m3_control.py
.venv/bin/python tests/test_m3_mcp.py
.venv/bin/python tests/test_codex_app_server.py
```

普通测试全部使用 fake adapter/fake ACP/Pi RPC server，不会调用真实外部 agent，
也不会 build 或启动真实 DSH。
真实 Kimi + MCP 端到端验收（见“当前限制”）是发布前手工证据，不在
默认 gate 内。

## 项目结构

```text
myagents/
├── main.py                    # Textual TUI
├── myagents_mcp.py            # stdio MCP bridge（只连控制 socket）
├── orchestrator.py            # 路由、history、并发投递、lease 生命周期
├── host.py                    # supervisor / host
├── native_agent/              # 中立 model provider + 原生无工具 agent runtime
├── acp/                       # 通用 ACP client 与 adapter
├── pi_rpc/                    # Pi 原生 RPC client/adapter + 固定权限 bridge
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
- [docs/adr/0009-bounded-milestone-workflow-steering.md](docs/adr/0009-bounded-milestone-workflow-steering.md)：有界里程碑 workflow 与 steering。
- [docs/adr/0010-multi-session-tui-management.md](docs/adr/0010-multi-session-tui-management.md)：会话目录、后台任务、资源上限与图片短引用。
- [docs/adr/0011-session-scoped-natural-language-roles.md](docs/adr/0011-session-scoped-natural-language-roles.md)：自然语言指定、会话生命周期与安全边界。
- [docs/adr/0013-natural-language-sequential-collaboration.md](docs/adr/0013-natural-language-sequential-collaboration.md)：自然语言固定计划、串行接力与失败收口。
- [docs/adr/0014-pi-rpc-permission-bridge.md](docs/adr/0014-pi-rpc-permission-bridge.md)：Pi 原生 RPC、启动 attestation、逐次权限 bridge 与三 profile。
- [docs/adr/0015-dsh-acp-only-transport.md](docs/adr/0015-dsh-acp-only-transport.md)：DSH 专用 ACP 入口、两 profile、stateful lifecycle gate 与零 fallback。
- [docs/adr/0016-explicit-auto-approve-mode.md](docs/adr/0016-explicit-auto-approve-mode.md)：`/yolo` 会话级自动批准、持续危险提示与只读硬边界。
- [docs/adr/0017-native-model-backed-host.md](docs/adr/0017-native-model-backed-host.md)：会话级 HostBackend、原生模型 provider/runtime 与只读 agent Host。
- [docs/adr/0018-capability-bounded-runtime-interjection.md](docs/adr/0018-capability-bounded-runtime-interjection.md)：Esc 精确取消、Alt+↑ 能力受限插话，以及 Pi/Codex 原生同轮 steer。
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
- [x] M4.3：macOS 真实截图、私有附件与 Kimi ACP 原生视觉输入已验收。
- [x] M4.4：Kimi ACP-first + prepare-only 只读 JSONL fallback。
- [x] M4.5：OpenCode ACP-first + ask-by-default 权限 + 隔离只读 JSONL fallback。
- [x] M4.6：Qwen Code ACP-only、default/plan profile 与 TUI 点名接入。
- [x] M4.7：多项目会话目录、后台执行、资源 gate、未读通知与图片短引用。
- [x] M4.11：Pi RPC-only、唯一权限 bridge、wrapper 工具闭集、三 profile 与 no-replay。
- [x] M4.12：DSH ACP-only adapter、标准 bundle、stock
  `--profile myagents` 被动探测、两 execution profile 与核心真实恢复/权限验收。
- [x] M4.13：显式 `/yolo` 会话级自动批准、只选 allow-once、持续危险提示与
  workflow read-only 硬边界。
- [x] M4.14：myagents 原生模型 provider/runtime、LM Studio
  OpenAI-compatible 首版、无工具 host 与 no-replay 生命周期。
- [x] M5.1：自然语言或 `/discuss` 进入 1–3 轮有界讨论与终局 moderator。
- [x] M5：干净 Git fixed point、review → 单 writer 修改 → 独立复核、最多
  一次 repair/reverify、阶段边界 steering 与 TUI 阶段状态。
- [x] M6：自然语言指定会话级角色、跨任务持续、房间隔离与状态可见。
- [x] M7：自然语言 2–4 步有序协作、前序结果接力、失败/取消即停。
- [ ] Later：只有出现跨机器、跨组织 agent 协作需求时再评估 A2A。

## 当前限制

- M4.14 原生 host 已于 2026-08-27 通过本机 LM Studio 真实最小探针：从
  `/v1/models` 选择当前已加载的精确 id
  `qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive`，生产 provider/runtime
  流式返回 `NATIVE_HOST_OK` 和权威 done；未修改 LM Studio 或用户配置。不同
  OpenAI-compatible 服务的兼容差异、长上下文质量与成本仍需分别验收。
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
- Pi 0.84.3 已在临时目录完成真实 attestation、固定短回复/session 落盘、一次
  `allow_once` 写入和无残留回收；默认 Harness 仍只跑 fake server/bridge contract。
  真实路径逃逸反例、profile 重建、abort 时延、长期 session 和图片仍待人工验收。permission bridge 不是 OS
  sandbox；批准 shell 后仍继承 myagents 的本机权限，不承诺无人值守处理不受信
  输入或敏感环境。
- 2026-08-26 已以标准 bundle + stock `dsh --profile myagents`、临时
  `DSH_HOME` 与临时 workspace 复跑最终双进程恢复：session
  `7afca581-e99c-4a2f-8631-4efaec794fd8` 第二轮 `restored=true`，精确返回
  `DSH_FINAL_PROFILE_OK` / `DSH_FINAL_PROFILE_RESUME_OK`；逐次 reject、read-only
  fresh session、配置/默认 profile/checkout 不变及退出无残留也通过。旧 custom
  `tsx` 证据继续 **superseded**；独立发布包形态的 installed CLI、真实主动 cancel
  时延、长 session/compaction、压力终局与真实图片模型仍是人工边界。
- M4.3 图片链路已于 2026-08-09 在房间 `e279f938f34e3475` 用真实 macOS
  剪贴板 PNG 验收：附件目录/文件权限为 0700/0600，command
  `5415080a-1c55-4eba-b063-dd5b7203d877` 的 timeline `seq=5..7` 完整，
  Kimi 经 ACP 原生图片输入准确识别飞碟画面。OpenCode 当前选择的
  `deepseek-v4-flash-free` 不支持视觉并明确拒绝，不视为附件链路失败。
- M3 真实 E2E 已于 2026-07-26 通过
  [`scripts/e2e-m3-real.py`](scripts/e2e-m3-real.py)：两次独立
  TUI/ACP/MCP 生命周期复用同一 Kimi session，timeline 无重复，退出后无
  endpoint/socket/agent 残留。该脚本调用真实模型，不放进默认快速 gate。
- M5.1 `/discuss` 的历史实测已于 2026-08-08 在同一命名房间恢复原
  Kimi/OpenCode session，通过 MCP 完成两轮交叉讨论和一次当时的 Codex host
  仲裁；6 条新增
  timeline 连续、第二轮能回应对方首轮、无工具/权限事件且退出无残留。
- M5 真实探针已于 2026-08-09 通过
  [`scripts/e2e-m5-real.py`](scripts/e2e-m5-real.py)：Kimi 只读 review/verify，
  Codex 作为唯一 writer 在临时 Git repo 增加 `subtract` 与两个测试，host 最终
  汇总；时间线恰为 user/Kimi/Codex/Kimi/host，HEAD、branch、index 保持 baseline，
  最终 3 个 unittest 全部通过。真实模型探针不进入默认快速 gate。
- M4.7 双会话真实探针已于 2026-08-11 在 `/tmp` 通过：Qwen ACP 与 OpenCode
  ACP 在两个隔离房间并发返回 `SESSION_QWEN_OK` / `SESSION_OPENCODE_OK`，两条
  command 均 completed；切换后后台会话正确标未读，历史不串房，临时目录删除且
  退出后无 owned ACP 子进程残留。具体 room/command id 见 SPEC UC-SESSION-001。
- 独立 ACP client 写入已有 Kimi session 不会让已打开的 native Kimi TUI
  实时刷新；一个前端应独占该 session。
- 同一 LM Studio 后端并发启动两个 Qwen ACP 的一次探针中，一路正常返回、一路
  `end_turn` 但零正文；跨 Qwen/OpenCode 的双会话探针稳定通过。当前不对同一
  本地模型的并发吞吐作质量承诺，零正文仍会诚实记录而不会伪造成回答。

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
| 原生长连接优先，JSONL fallback | 有官方可靠长连接时优先使用；Kimi/OpenCode/Qwen Code 走 ACP，Codex 走 app-server；Qwen 在只读降级契约获证前保持 ACP-only。 |
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
| JSONL transport | 各 agent CLI 无头模式；Kimi/OpenCode 自动降级只允许各自内置只读 profile；Qwen 不自动降级 |
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
- `acp/adapter.py` 把 ACP session 转成 `AgentEvent`，并将可选的
  prepare-only JSONL 降级收在同一 adapter seam 内。
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
- host 路由结果包含每个 target 的一次性明确任务；Orchestrator 将任务直接
  加入该 worker 本轮 prompt，不写入共享 timeline。任务必须消解“你/让某人”
  等角色关系；旧格式或缺失任务使用原始用户请求生成执行型回退指令，不能只把
  `reason` 展示给用户后丢失委托语义。
- host 路由是纯分类与任务改写步骤，prompt 明确禁止调用工具、文件、命令、
  网络或 skill；保持 Codex 默认配置继承，不用配置覆盖换取速度。底层若仍
  产生安全的 status/tool/permission 事件，必须透传到执行日志与 TUI，不能
  在 `decide()` 内静默吞掉。
- JSONL adapter 收 dispatch 时刻的有界 transcript 快照。
- stateful ACP adapter 在每-agent delivery lock 内读取 cursor、构造增量、
  完成 stream 后推进 cursor。prompt 提交前或上游明确拒绝的失败不推进、
  允许下轮补发；prompt 已提交后的静默超时属于结果不确定，必须先持久化
  no-replay cursor 再公开失败，不能自动重放。
- 首次 ACP bootstrap 最多发送 `history_limit` 条共享历史。
- `/discuss` 是 Orchestrator 内的确定性有界状态机：2–3 个显式 worker、
  1–3 轮、一个终局 moderator。同轮复用 fan-out，跨轮等待全部收尾；内部
  assignment 不写成 user timeline，不递归 `dispatch`，agent 不决定下一轮。
- 讨论参与者失败后退出后续轮次，避免把不确定投递当作安全重试；moderator
  仍总结已有证据，但 `DispatchOutcome` 保留失败，CommandBus 不得报 completed。

### 4.2 权限

- `AcpClient`、`AcpAdapter`、`AcpKimiAdapter`、`AcpOpenCodeAdapter`、`AcpQwenAdapter`
  默认权限都是 `deny`。OpenCode ACP 普通轮次把未知及有副作用工具收口为 ask；
  `read_only` 轮次改用 runtime deny-all + 安全读取白名单，并在 profile 切换时
  重建进程/session，避免取消权限导致零正文或跨 mode 继承授权。Qwen ACP
  普通轮强制 `--approval-mode default`，`read_only` 强制 `plan`；不能继承用户
  native TUI 的 auto/yolo mode，也不能只依赖 ACP permission cancelled。
- TUI 异步决定权限并显示来源 agent。
- `session/request_permission` 到权限结果发回前属于人工等待，不计入 ACP
  inactivity timeout；read loop、取消和关闭仍保持可响应。
- `selected.optionId` 必须非空且属于本次 `params.options`；否则 cancelled。
- `auto` 只允许明确授权的测试、运维或一次性外部调用使用。

### 4.3 生命周期

- 每个 ACP adapter 是其 session 的唯一 writer。
- 同一 ACP agent 的 prompt 串行；不同 agent 可以并发 fan-out。
- 不在等待人工权限且没有活跃工具时，prompt 连续 120 秒无任何 ACP 通知或
  终止响应才自动取消；活跃工具使用独立 15 分钟 watchdog。两类超时都是提交后
  结果不确定，按 no-replay 失败处理。
- cancel 后必须等待原 prompt 停止；超时则关闭并重建连接。
- 子进程使用独立进程组；退出时 SIGTERM，超时再 SIGKILL。
- TUI unmount 先取消权限 Future，再调用 `Orchestrator.aclose()`。
- worker JSONL fallback 只允许在新 ACP 连接的 start/initialize/
  session prepare 失败时启动；活跃 session 冲突、checkpoint 失败、
  prompt 拒绝和 post-submit 失败均禁止跨协议重放。
- Kimi fallback agent file 只允许 `Read` / `Grep` / `Glob`；OpenCode
  fallback 只允许 `read` / `glob` / `grep` / `list`，并禁用项目配置、
  Claude 兼容层、plugin 和自动升级。两者均禁止写入、命令、网络、Skill、
  子 agent 和 MCP；权限必须由各 CLI runtime 执行，不能退化为 prompt-only。

### 4.4 持久化与恢复（M2.5）

- 房间身份由 `(规范化 workdir, session_name)` 决定；`default` 保持历史
  room_id 兼容。命名会话的 timeline/events/cursor/原生 agent session 完全
  隔离；`Ctrl+N` 和精确输入 `/new` 是同一个本地新会话动作，命令不得写入
  timeline、不得交给 host/worker。TUI 内由 SessionManager 为每个已加载房间
  独立持有 lease、Orchestrator、CommandBus 和 ControlServer；切换只改变可见
  会话，后台任务与权限等待继续运行（ADR-0010）。
- `Ctrl+O` 或精确 `/sessions` 打开当前/全部项目会话目录；标题与稳定
  session_name/room_id 分离。运行中/当前会话不得永久删除，删除必须逐字确认
  展示标题。最多三个会话实际 dispatch，第四个保持可取消的等待资源状态；
  后台空闲 runtime 十分钟后按标准关闭顺序回收。
- RoomStore timeline 记录带单调 `seq`；cursor 是 seq 而非 list 下标，
  成功交付后先落盘再更新内存；明确未提交的失败不推进，已提交但结果不确定的
  失败建立 no-replay 边界，不假提交成功，也不重复执行。
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
  拒绝 symlink、强制 0600、按 workdir/session_name 计算 room_id 并校验
  room_id/workdir/socket_path 匹配后，
  每次调用仍实际连接验证。
- MCP bridge 八个 `myagents_*` 工具只翻译到上述 socket 协议；
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
- 工具生命周期按 `(command_id, agent, tool_call_id)` 合并；协议缺少 ID 时
  使用脱敏标题作为可见 identity。adapter 继承初始工具标题并只产出状态迁移，
  CommandBus 对完全相同的 status/tool 做防御性去重，但重复协议活动仍刷新
  静默计时。TUI 再按 command 把阶段、heartbeat、工具与权限合并为一张活动卡，
  默认只显示终态、当前阶段与工具汇总；活动卡逐张维护展开态，`Ctrl+G` 进入
  活动区后由 `↑↓` 选择、`Enter` 展开或收起、`Esc` 返回输入框；`/details`
  切换当前选中卡，否则只切换最近一张卡。展开后显示各工具的
  “进行中/已完成/失败”和脱敏命令。协议后续补发 tool ID 时必须迁移同一逻辑项，
  不能重复计数；identity 是否来自标题 fallback 必须显式传递，不能靠字符串值
  猜测，同名但 ID 不同的工具不得误合并。被窗口裁掉的工具若曾失败、拒绝或
  取消，摘要必须继续保留该异常事实。每卡工具明细与可展开终态卡
  必须有上限，超限只归档折叠摘要；后台 runtime 被 idle reap 时同步释放该 room
  的 UI feed，完整事实仍以 `events.jsonl` 为准；
  `events.jsonl` 只持久化有信息增量的工具状态，不能被高频
  `in_progress` 刷爆。
- CommandBus 静默 10 秒发 heartbeat，静默时长保持累计且 heartbeat 自身不
  重置活动时钟；TUI 在同一 command 活动卡内原位更新 heartbeat，而不是追加
  聊天行。用户消息、agent 正文和失败保持主线可见，活动折叠不得隐藏失败事实。
  活动模型按 room_id 隔离；切换会话和后台完成后切回，近期终态卡仍可展开。
  `completed` 只表示本轮调用正常结束，TUI 使用“本轮响应结束”，不声称用户
  任务已经验收。fan-out 要等待全部 target 收尾；任一 worker 失败时 command
  终态为 `failed`，即使其他 worker 已正常回复，失败 worker 的 partial 也必须
  明确标注调用失败。固定任务区必须保留各 agent 阶段和终态；混合成功/失败
  显示“部分完成”。活动详情默认折叠，仅由上述显式操作逐卡切换；展开态按
  room_id 隔离并在会话切换后恢复。active/queued 可精确取消，
  terminal cancel 幂等，取消不得杀死 worker。
- TUI `Ctrl+X`、control `command.cancel`、MCP
  `myagents_cancel_command` 共用一个取消原语；外部可通过
  `myagents_read_events` 读取持久进度。
- 重启后最后事件非 terminal 的命令必须显示为“已中断”，不得伪装完成。
- Codex host 继承用户默认配置，不覆盖 model、reasoning、plugin 或 MCP。

### 4.7 Codex app-server（M4）

- 一个 adapter 独占一个 app-server 进程与当前 thread，同 adapter turn 串行；
  Codex worker 连续轮次复用持久 thread，host 复用暖进程但每次建立干净的
  ephemeral thread，内部路由不得写入 Codex 历史。
- 启动与请求不得覆盖 model、effort、config、collaboration mode、plugin 或
  MCP；只传协议必需字段、cwd、既有 host/worker sandbox 与 approval-policy
  安全边界，以及 host 专用的 `ephemeral: true`，且不写
  `~/.codex/config.toml`。
- `turn/completed` 是完成权威信号；取消发送 `turn/interrupt` 并等 terminal，
  未确认则关闭重建，下一轮不得与旧 turn 重叠。
- worker thread 使用 `workspace-write + on-request`，越界操作进入统一权限
  UI；host thread 使用 `read-only + never`。approval 无处理器默认 decline；
  tool/command 元数据必须有界脱敏，reasoning 正文不可显示。
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

### 4.8 剪贴板图片附件（M4.3）

- TUI 只从 macOS 系统剪贴板读取 PNG；文本粘贴保持 Textual 原行为。
- 图片写入当前房间的 `attachments/`，目录 0700、文件 0600；不得写入目标
  工作区、timeline 或 events，单张上限 20 MiB。
- 粘贴只在光标处插入附件引用，不自动提交。可信房间附件通过 Kimi ACP
  `image` block 和 Codex app-server `localImage` 原生发送；文本引用仍保留
  在共享 timeline。手写的房间外路径不得升级成协议图片；host 遇图片附件
  必须路由给 worker。
- 新附件使用 `img-NNNN.png` 和 `[图片 N]` 短引用；旧
  `[图片附件：绝对路径]` 仅在同一房间信任根内兼容读取。
- 失败不得改变草稿或残留不完整文件。终端内图片预览、转换、删除与跨机器同步
  不在本阶段。

## 5. 测试策略

| 层级 | 证据 |
| --- | --- |
| 路由/编排 | `tests/test_basic.py` |
| ACP 协议与取消 | `tests/test_acp.py` + `tests/fake_acp_server.py` |
| Kimi hybrid transport | `tests/test_kimi_hybrid.py` + `tests/fake_acp_server.py` |
| OpenCode hybrid transport | `tests/test_opencode_hybrid.py` + `tests/fake_acp_server.py` |
| Qwen Code ACP-only 注册 | `tests/test_phase2.py` + `tests/fake_acp_server.py` |
| TUI/增量/权限/回收 | `tests/test_phase2.py` |
| 多会话目录/生命周期/TUI | `tests/test_session_catalog.py` + `tests/test_session_manager.py` + `tests/test_session_tui.py` |
| RoomStore 持久化 | `tests/test_storage.py` |
| M2.5 恢复/lease/时序 | `tests/test_m25.py` |
| M3 command bus | `tests/test_m3_bus.py` |
| M3 控制 socket 安全/协议 | `tests/test_m3_control.py` |
| M3 MCP stdio | `tests/test_m3_mcp.py`（官方 SDK client） |
| M4 Codex app-server | `tests/test_codex_app_server.py` + `tests/fake_codex_app_server.py` |
| M4.3 剪贴板图片 | `tests/test_clipboard_image.py` + macOS 人工截图验收 |
| 真实协议边界 | `docs/SPEC.md` 登记的 Kimi/OpenCode ACP + Textual/受限临时目录 E2E；Qwen 上游源码与本机启动证据 |

普通测试禁止调用真实 Kimi/Codex/OpenCode/Qwen。真实 agent 验收必须由用户明确授权，
在临时目录运行，并在结束后检查没有残留进程。

## 6. 质量门禁

当前本地门禁顺序：

```text
harness 文档引用 → redlines → py_compile → basic → ACP → Phase 2
→ Kimi hybrid
→ OpenCode hybrid
→ storage → M2.5 → M3 bus → M3 control → M3 MCP stdio → M4 app-server
→ M4.3 clipboard image
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
- 某 agent 的 ACP 适配器是否成熟到作为 JSONL fallback 的主路径；
- 外部入口传输已定为私有 Unix socket + stdio MCP（ADR-0001）；是否引入
  Streamable HTTP、远程认证或 A2A 仍需人工决策，M3 不做；
- 真实工具调用的权限风险是否可接受；
- TUI 的可用性、长会话 token/内存表现和真实 cancel 时延；
- 真实 Kimi `session/load` 与 MCP 端到端恢复已由
  `scripts/e2e-m3-real.py` 验收；它调用真实模型，不进默认快速 gate；
- OpenCode 1.18.14 的 ACP 正常回合、重连 `session/load`、Bash 权限 deny、
  prepare 失败只读 JSONL 与无残留进程已于 2026-08-08 受限验收；
- OpenCode 1.18.15 的 workflow 只读 runtime deny 已于 2026-08-09 完成 5/5
  同形状回复与 Kimi → Codex → OpenCode → host 真实临时仓库验收；
- Qwen Code 0.21.7 已于 2026-08-09 通过生产 adapter 建立真实 ACP session，
  经本机 LM Studio `google/gemma-4-e4b` 返回流式正文并 `end_turn`，关闭后无
  Qwen ACP 子进程残留；独立 plan profile 写入探针在 runtime 层失败且未产生
  文件，default profile 同类探针也未写入；真实权限 options、跨进程恢复与取消
  时延仍待人工验收；
- `/discuss` 已于 2026-08-08 在同一持久房间恢复原 Kimi/OpenCode session，
  经 MCP 完成两轮交叉讨论和 Codex host 仲裁；单 user、连续 timeline、跨轮
  引用、无工具事件及退出回收均已核对。真实模型不进默认 gate；
- 长会话 compaction 后的 restore、真实 cancel 时延仍需人工验收。

具体 Owner、Sensor 与处置见 [`docs/harness-controls.md`](docs/harness-controls.md)。

## 8. 红线（6 条，违反必驳回）

| # | 红线 | 守门 |
| --- | --- | --- |
| R1 | 生产构造不得显式使用 `permission="auto"`，权限默认必须为 `deny` | `bash scripts/check-redlines.sh` 的 AST permission gate |
| R2 | 通用 orchestration/ACP 层不得按具体 agent 名做条件分支 | `bash scripts/check-redlines.sh` 的 AST name-branch gate |
| R3 | UI、orchestrator、host 不得直接启动 shell/子进程 | `bash scripts/check-redlines.sh` 的 AST process-boundary gate |
| R4 | Kimi/OpenCode 必须受限 ACP-first；Qwen 必须 ACP-only 且固定 default/plan runtime profile；OpenCode 普通轮风险工具 ask、只读轮 runtime deny；JSONL 仅获证的 prepare-only 只读降级 | `bash scripts/check-redlines.sh` 的 registry/policy/profile gate |
| R5 | `/discuss` 必须保持 2–3 人、1–3 轮、终局主持且不得递归 dispatch | `bash scripts/check-redlines.sh` 的 discussion bounds/AST gate |
| R6 | `/workflow` 必须保持固定角色/阶段、单 writer、最多一次 repair/reverify、read-only 复核和有界 steering | `bash scripts/check-redlines.sh` 的 workflow bounds/mode/AST gate |

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
- M4.4 Kimi hybrid transport 事实源：
  [`docs/adr/0006-kimi-hybrid-transport-policy.md`](docs/adr/0006-kimi-hybrid-transport-policy.md)。
- M4.5 OpenCode hybrid transport 事实源：
  [`docs/adr/0007-opencode-hybrid-transport-policy.md`](docs/adr/0007-opencode-hybrid-transport-policy.md)。
- M5.1 有界多智能体讨论事实源：
  [`docs/adr/0008-bounded-multi-agent-discussion.md`](docs/adr/0008-bounded-multi-agent-discussion.md)。
- M5 有界里程碑工作流事实源：
  [`docs/adr/0009-bounded-milestone-workflow-steering.md`](docs/adr/0009-bounded-milestone-workflow-steering.md)。
- 当前路线图：[`README.md`](README.md)“路线图”。
- 重大协议/安全边界改变先形成可评审设计记录，再修改本契约。
- Steering 只在同类失败至少两次或已有趋势证据时建立；单次失败只修当前问题。

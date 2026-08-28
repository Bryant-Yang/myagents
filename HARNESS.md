# myagents 工程契约（HARNESS）

> 作者：Bryant Yang　最近更新：2026-08-27
> Harness Framework：2.1.0（来源 commit：
> `b3b8fd47ebc49b57abdc365f327688b33970d54c`）

这是 `myagents` 的工程事实源。产品行为与独立证据见
[`docs/SPEC.md`](docs/SPEC.md)，任务入口见
[`docs/workflow.md`](docs/workflow.md)，控制闭环见
[`docs/harness-controls.md`](docs/harness-controls.md)。

## 0. 项目使命

为不同 coding agent 提供一个统一、可验证的本地 TUI 编排面：路由和共享时间线
只有一个中心；ACP、原生 RPC 与 app-server 负责有状态 agent 会话；adapter 隔离各 CLI 差异；权限默认
拒绝；myagents 原生 model runtime 让 host 不依赖第三方 Agent CLI；取消或退出后
不留下仍能修改工作区的子进程。

## 1. 北极星原则

| 原则 | 项目解释 |
| --- | --- |
| 一个编排中心 | agent 不直接互调，消息与 history 统一经过 Orchestrator。 |
| 原生长连接优先，JSONL fallback | 有官方可靠长连接时优先使用；Kimi/OpenCode/Qwen Code/CodeBuddy/DSH 走 ACP，Codex 走 app-server，Pi 走原生 RPC；Qwen/CodeBuddy/DSH/Pi 不自动降级。 |
| 协议通用、差异下沉 | 通用 runtime 不按 agent 名分支，具体差异进入 adapter/spec。 |
| 注册不等于就绪 | 生产 TUI 被动探测当前进程可见的 CLI；缺失目标在副作用前原子拒绝，不自动安装或修改用户环境。 |
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
| Pi transport | `pi --mode rpc`，LF-delimited JSON request/event stream；独立 `pi_rpc/` runtime，不是 ACP |
| JSONL transport | 各 agent CLI 无头模式；Kimi/OpenCode 自动降级只允许各自内置只读 profile；Qwen/CodeBuddy/DSH/Pi 不自动降级 |
| 持久化 | RoomStore：timeline.jsonl（对话）+ events.jsonl（执行）+ state.json（seq/cursor/session）+ owner.lock |
| 外部入口 | control/：CommandBus FIFO + 私有 Unix 控制 socket；myagents_mcp.py stdio MCP bridge（`mcp>=1.27,<2`） |
| 测试 | 直接运行的 Python test scripts + Textual pilot + fake ACP/Pi RPC server + 官方 MCP SDK stdio client |
| 本地总门禁 | `bash scripts/check-harness.sh` |
| Git / CI | Git 已初始化；远程 CI 尚未配置，不得假称已有合并门禁 |

```text
main.py (Textual TUI)
    ↓
orchestrator.py (routing / history / delivery locks / lease)
    ↓
acp/             pi_rpc/          codex_app_server/  adapters/       storage/
ACP runtime      Pi RPC runtime   Codex runtime      JSONL fallback  RoomStore
    ↓                  ↓                 ↓
coding agent subprocesses

control/ (CommandBus FIFO + 私有 Unix 控制 socket)
    ↑ 只连 socket，绝不创建 Orchestrator
myagents_mcp.py (stdio MCP bridge，mcp>=1.27,<2)
```

`tests/fake_acp_server.py` 与 `tests/fake_pi_rpc_server.py` 是协议 fixture，不是生产
transport。

## 3. 分层与依赖

- `main.py` 只负责 UI、权限交互和生命周期入口，不直接启动 agent 进程。
- `agent_readiness.py` 只读取环境/PATH/文件属性，集中提供 agent 状态快照、重扫
  与资格门；不得启动 CLI、联网、读取登录态或执行安装器。
- `orchestrator.py` 只面向 adapter 能力和 `AgentSpec`，不解析厂商协议。
- `acp/client.py` 负责通用 ACP framing、request/response、反向权限请求和进程回收。
- `acp/adapter.py` 把 ACP session 转成 `AgentEvent`，并将可选的
  prepare-only JSONL 降级收在同一 adapter seam 内。DSH 的入口解析、被动
  readiness、stock `myagents` profile 命令、绝对状态目录和 load/close hard gate 只存在于
  `AcpDshAdapter`，不进入通用 Orchestrator。
- `pi_rpc/client.py` 独占 `pi --mode rpc` 进程，负责 LF framing、request/event、
  permission bridge UI、attestation、官方 steer/abort、session 与进程组回收；
  公开 API 不接受任意 raw RPC dict。
- `pi_rpc/adapter.py` 把 Pi session/stream 映射成 stateful `AgentEvent`；不得把
  Pi RPC 冒充 ACP，也不得提供 JSON/JSONL fallback。
- `pi_rpc/extensions/myagents_permission_bridge.ts` 是 Pi 唯一显式加载的权限组件，
  注册唯一命名 wrapper 工具闭集；协议/权限策略不得散入 Orchestrator。
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

- `AgentSpec` 注册能力不等于本机 ready。生产 TUI 启动及新 runtime 创建时执行
  被动 probe；显式多目标、discussion、workflow 和无 mention host 都必须在
  timeline/workspace 副作用前通过统一资格门。host 路由候选只包含 ready worker。
- `/agents` 与 `/agents rescan` 是本地状态命令；rescan 只重新读取当前进程环境并
  同步所有已加载 room，不安装、卸载、移动或启动真实 agent。TUI 的资格错误
  必须保留原草稿和原光标位置。
- 显式 `@agent` 永远优先；无 `@` 时 host 单次调用直接回答或返回 worker
  路由，本地代码不维护自然语言关键词白名单。
- host 路由结果包含每个 target 的一次性明确任务；Orchestrator 将任务直接
  加入该 worker 本轮 prompt，不写入共享 timeline。任务必须消解“你/让某人”
  等角色关系；旧格式或缺失任务使用原始用户请求生成执行型回退指令，不能只把
  `reason` 展示给用户后丢失委托语义。
- 会话级角色只保存为当前 room 的 `实际 agent -> label + instructions`；由 host
  在固定 targets 闭集内从自然语言提取，instructions 只注入对应 worker prompt，
  不写共享 timeline。角色不得创建 agent、改变 runtime/权限、扩充讨论成员/轮次
  或替换 workflow 固定职责；状态写失败时不得更新内存或继续派发。精确本地命令
  `/roles` 与 `/roles clear` 分别只查看/原子清空当前 room，不进 timeline、不调用
  模型；有 queued/running command 时不得跨阶段清空。
  每轮 assignment 必须注入当前权威角色状态；角色已清除时显式声明无角色，
  不得依赖有状态 runtime 自行忘记旧角色。
- host 路由是纯分类与任务改写步骤，prompt 明确禁止调用工具、文件、命令、
  网络或 skill；生产 host 由显式配置的原生 model provider 驱动，runtime
  固定无工具且 `/yolo` 不得扩权。底层若仍产生安全的 status 事件，必须
  透传到执行日志与 TUI，不能在语义调用中静默吞掉。
- JSONL adapter 收 dispatch 时刻的有界 transcript 快照。
- stateful ACP adapter 在每-agent delivery lock 内读取 cursor、构造增量、
  完成 stream 后推进 cursor。prompt 提交前或上游明确拒绝的失败不推进、
  允许下轮补发；prompt 已提交后的静默超时属于结果不确定，必须先持久化
  no-replay cursor 再公开失败，不能自动重放。
- 首次 ACP bootstrap 最多发送 `history_limit` 条共享历史。
- 自然语言讨论与 `/discuss` 复用 Orchestrator 内同一个确定性有界状态机：
  2–3 个 worker、1–3 轮、一个终局 moderator。同轮复用 fan-out，跨轮等待全部
  收尾；内部 assignment 不写成 user timeline，不递归 `dispatch`，agent 不决定
  下一轮。显式自然语言只使用 mention 闭集；未点名时 host 只能从 ready worker
  中选择。`一起分析/分别回答/各自建议` 仍是单轮 fan-out，明确跨 agent 先后
  关系优先进入有序协作。
- 讨论参与者失败后退出后续轮次，避免把不确定投递当作安全重试；moderator
  仍总结已有证据，但 `DispatchOutcome` 保留失败，CommandBus 不得报 completed。
- 自然语言有序协作由 `CollaborationPlan` 与 Orchestrator 的确定性串行状态机
  执行：2–4 步、至少两个 ready worker、一个 command 和一条 user timeline。
  host 只在 ready worker 或显式 mention 闭集内做一次语义提取；普通
  `一起/分别/各自` 继续 fan-out。后一步读取前序真实回复，中间失败/取消后不得
  启动后续步骤；禁止递归 dispatch、动态扩员、自动重试、换人、跳步或追加
  host 伪造成功总结。计划不改变 adapter 权限、execution mode 或 no-replay。
  Orchestrator 同时发出版本化、脱敏且有界的 plan projection event，供固定任务区、
  活动卡与重启详情共享；projection 绝不反向驱动调度，同一 agent 的不同步骤按
  step index 独立保存。完整合同见 ADR-0019。

### 4.2 权限

- `AcpClient`、`AcpAdapter`、`AcpKimiAdapter`、`AcpOpenCodeAdapter`、`AcpQwenAdapter`、`AcpCodeBuddyAdapter`、`AcpDshAdapter`
  默认权限都是 `deny`。OpenCode ACP 普通轮次把未知及有副作用工具收口为 ask；
  `read_only` 轮次改用 runtime deny-all + 安全读取白名单，并在 profile 切换时
  重建进程/session，避免取消权限导致零正文或跨 mode 继承授权。Qwen ACP
  普通轮强制 `--approval-mode default`，`read_only` 强制 `plan`；不能继承用户
  native TUI 的 auto/yolo mode，也不能只依赖 ACP permission cancelled。
- CodeBuddy 普通轮强制 `--permission-mode default`；`read_only` 强制
  `dontAsk`、禁用 subagent 自动询问，并把内置工具闭集固定为
  `Read,Glob,Grep`。两种 profile 都忽略用户/项目/local settings 与外部 MCP，
  profile 切换必须重建进程/session。只使用可独立运行的官方 CLI，固定中国区
  `internal` 环境，不回退 WorkBuddy.app 包内私有二进制。已有登录态直接复用；
  只有 session prepare 明确返回认证错误才接受 initialize 公布的 method 并按需
  认证。登录 URL 仅允许官方 HTTPS 域名，认证等待有界，失败原子回收。
  该 adapter 仅代表独立 CodeBuddy CLI；WorkBuddy App、私有 owner runtime、
  connector-proxy 与 App 连接器授权均不在接入范围内。
- DSH 只启动 stock `dsh --profile myagents`；普通与 `workspace_write` 轮固定
  `DSH_ACP_PROFILE=workspace-write`，`read_only` 固定
  `DSH_ACP_PROFILE=read-only`，跨 execution safety profile 重建进程和新 session。
  专用 host 必须在 runtime
  证明 read/glob/grep 闭集与风险工具逐次审批；myagents 无 handler/畸形选择时
  deny。子进程显式使用固定 persistence/sessions/runtime-home/attachment-home/agents/
  config-inputs 拓扑，用户配置只经 no-follow 有界快照进入 mode-0600 产品副本；
  `DSH_HOME` 只指向包含 `profiles/myagents` 的 stock profile home，
  `DSH_AGENTS_HOME` 绑定产品派生状态。安装入口是官方 `dsh`；源码入口只以
  `MYAGENTS_DSH_SOURCE_ROOT` 定位已构建官方 `apps/cli/lib/bin.js`，产品 host 组合由
  profile 中的标准 bundle 加载。profile home/state/workspace/source/plugin/CLI/config
  不得重叠或 symlink 逃逸；readiness 必须被动核验官方 CLI identity、profile
  dependencies、exact bundle 顺序 `@deepseek-ai/dsh-base` →
  `@myagents/dsh-acp-host`，以及解析后的 host `0.1.0`、
  `dsh.bundle.patch=./cordis.patch.yml`、entry/patch 与 runtime `0.1.1-rc.2`，缺一即
  invalid。readiness 不执行 pnpm/build/CLI/真实 agent。
  产品专属 Python adapter/readiness、TypeScript ACP server 与 host composition 的
  canonical source 均归 `dsh_acp/`；标准 bundle 的 canonical package、entry 与
  `cordis.patch.yml` 均归 `dsh_acp/plugin`。DSH checkout 是不可变运行依赖，只允许消费其
  公开 agent/AgentSetup/ToolRuntime/持久化 seam；不得修改 core、官方 ACP 包、示例或
  测试，也不得从未导出的 `src/*` 深层导入。缺 profile 守卫证据或只能靠 DSH 补丁
  才能成立的能力必须 block。
  持久化固定为 uncompressed/unpacked JSONL；load 必须在 materialize 前通过 public
  list/locate 做 no-follow 4096 events / 16 MiB 扫描及跨阶段 stat identity 复核。
- `PiRpcAdapter` 的 `permission_handler` 默认为 `None`（deny）；`PiRpcClient` 对
  handler 的畸形/异常结果回复 cancelled，对缺 id/method 的畸形 wire request
  关闭连接。Pi 进程关闭自动发现并固定 `--offline` / `PI_OFFLINE=1`，
  且不激活原生命名 built-in tools；只显式加载固定
  `myagents_permission_bridge.ts`，并在第一条 prompt 前精确核验 nonce、policy
  version、profile、workspace、bridge source 与 active wrapper tools。
  `DEFAULT` / `WORKSPACE_WRITE` 只暴露七个 `myagents_*` wrapper，
  `edit/write/bash` 每次只接受共享 UI 返回的 `allow_once`；`READ_ONLY` 只激活
  `read/grep/find/ls` 四个 wrapper，并在 hook 内再次 hard-deny 风险工具。
  permission handler 必须等 no-replay cursor 持久化后才能运行；profile 切换必须
  重建进程和新 session。
  attestation、权限 handler、请求绑定或 option 校验任一失败都拒绝，不允许
  `allow_always`。permission preview 的最终 stable JSON 必须不超过 4096 UTF-8
  bytes；未知 schema 或无法安全截断的内容拒绝。active prompt 事件同时受条数与
  64 MiB 累计字节预算约束，adapter 不得用无界中间队列绕过预算；extension UI
  受 32 个并发和活跃 id 唯一约束。
- TUI 异步决定权限并显示来源 agent。
- `session/request_permission` 到权限结果发回前属于人工等待，不计入 ACP
  inactivity timeout；read loop、取消和关闭仍保持可响应。
- `selected.optionId` 必须非空且属于本次 `params.options`；否则 cancelled。
- `auto` 只允许明确授权的测试、运维或一次性外部调用使用。
- 生产 TUI 只允许用户在当前会话显式输入 `/yolo` 开启自动批准；
  默认关闭、按 room 隔离、退出后不记忆。该决策器只能选择当次 options 中的非空
  `allow_once`，不得选 `allow_always`或伪造 optionId；标题与固定任务区
  必须持续显示危险模式。`read_only` runtime/profile 与只读 JSONL
  fallback 不得被放宽。完整决策见
  [`ADR-0016`](docs/adr/0016-explicit-auto-approve-mode.md)。
- ACP JSON-RPC `error.data` 必须在通用 client 层按固定字段、深度与 UTF-8
  字节预算提取并脱敏；上下文超限、余额/额度不足等已知 provider 原因要转为
  可操作提示。不得在 Orchestrator 按 agent 名解析错误，也不得把未知结构、
  traceback 或凭据原样写入 timeline。

### 4.3 生命周期

- 每个 stateful adapter 是其原生 session 的唯一 writer。
- 同一 stateful agent 的 prompt 串行；不同 agent 可以并发 fan-out。
- 不在等待人工权限且没有活跃工具时，prompt 连续 300 秒无任何 transport 事件或
  终止响应才自动取消；早期 plan/status 更新只重置计时，不缩短后续 provider
  prefill 的统一预算。ACP 与 Pi RPC 对已跟踪的活跃工具使用独立 15 分钟
  watchdog；Codex app-server 当前仍使用统一普通预算。
  这些超时都是提交后结果不确定，按 no-replay 失败处理。
- cancel 后必须等待原 prompt 停止；超时则关闭并重建连接。
- ACP 只有 `stopReason=end_turn` 能产生成功 done；max-token、turn-limit、refusal、
  cancelled、缺失和未知 terminal 都在 no-replay cursor 提交后确定性失败。
- agent 主进程使用独立进程组；退出时 SIGTERM，超时再 SIGKILL。Pi Bash 另用
  detached group，正常关闭依赖 Pi 的 signal handler 回收；外部直接 SIGKILL 时不
  宣称 detached 子孙必然回收。
- TUI unmount 先取消权限 Future，再调用 `Orchestrator.aclose()`。
- worker JSONL fallback 只允许在新 ACP 连接尚未建立 session 时，因本地
  executable 无法启动，或 initialize / session/new 明确返回标准
  method-not-found（`-32601`）而启动；session/load、认证/权限/配额/backend
  拒绝、timeout、transport 不确定、活跃 session 冲突、checkpoint 失败、
  prompt 拒绝和 post-submit 失败均禁止跨协议重放。
- Kimi fallback agent file 只允许 `Read` / `Grep` / `Glob`；OpenCode
  fallback 只允许 `read` / `glob` / `grep` / `list`，并禁用项目配置、
  Claude 兼容层、plugin 和自动升级。两者均禁止写入、命令、网络、Skill、
  子 agent 和 MCP；权限必须由各 CLI runtime 执行，不能退化为 prompt-only。
- Pi 不存在 fallback。prompt 成功响应后发生的断线、超时或 abort 不确定结果必须
  建立 no-replay 边界；早到事件先缓冲，提交确认后先发布 `delivery_committed`。
  `agent_end` 不作为终局，必须等 `agent_settled`；最终 assistant terminal 必须存在
  且 stop reason 属于显式成功闭集，缺失/未知 terminal、error/abort 或权限终止工具
  不得记为成功，自动重试后的最终成功可正常收口。reserved session path 另以私有、
  fsync 的 reserved/materialized marker 绑定 checkpoint；只有精确 reserved 且文件尚未
  产生时可 fresh，materialized 后缺失必须 fail-closed。取消超时则回收进程组。生产
  client 不得发送 RPC `type: "bash"`，命令只能走逐次权限 wrapper。
- DSH 不存在 fallback。产品 ACP server 完全归标准
  `@myagents/dsh-acp-host` bundle，不得委托给 stock `dsh-acp-demo` 或要求 DSH checkout
  携带产品补丁。首条 session 操作前必须同时看到 `loadSession=true` 与
  `sessionCapabilities.close`；load 历史通知在无 auth prepare 中直接丢弃，有 auth
  时只进入 64 条有界队列。reset/aclose 先有界 close session，再回收进程组。

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
  绝不伪装成 agent 调用失败；prompt 已提交后的 no-replay
  checkpoint 落盘失败必须停用 Orchestrator，禁止后续重放。
- `session/load` 成功保留 cursor 继续增量；仅标准 `-32002` / `-32601`
  或具体 adapter 已获证的精确 session-not-found 映射可回退新
  session，capability 不支持时可直接新建；其他 remote/transport 错误
  fail-closed，不得用 `session/new` 绕过。
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

- `events.jsonl` 与对话 timeline 分离，记录生命周期、status、tool、permission、
  partial、plan 与 terminal 事件；绝不进入 agent history。plan payload 是
  versioned strict JSON，只保存有界 assignment preview 与确定性步骤迁移。
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
  持久房间展开 `/details` 时按 command 异步投影 `events.jsonl`：显示完整统计、
  生命周期/阶段、工具、权限、控制事件和输出片段统计，不重复 partial 正文，也不
  显示 thought 正文；直接回答必须有“未调用工具或请求权限”的明确空状态。存储层
  最多返回 200 条代表性首尾事件，UI 最多显示 80 条并优先保留关键证据，截断必须
  显示省略量；凭据继续脱敏。重启后内存活动为空时仅按需恢复最近任务，不在启动
  时回灌全部活动；运行中快照到达 terminal 后必须刷新，不能伪装成完整终态记录；
  `events.jsonl` 只持久化有信息增量的工具状态，不能被高频
  `in_progress` 刷爆。
- CommandBus 静默 10 秒发 heartbeat，静默时长保持累计且 heartbeat 自身不
  重置活动时钟；TUI 在同一 command 活动卡内原位更新 heartbeat，而不是追加
  聊天行。用户消息、agent 正文和失败保持主线可见，活动折叠不得隐藏失败事实。
  同一 room 连续提交时，已入 FIFO 但尚未 committed 的输入必须以有序“待发送”
  摘要立即可见；出队 committed 后移除摘要并只保留一条正式 user 消息，提前取消
  则标成“未发送”。该摘要只属于 TUI 执行模型，不得提前进入 agent history。
  活动模型按 room_id 隔离；切换会话和后台完成后切回，近期终态卡仍可展开。
  `completed` 只表示本轮调用正常结束，TUI 使用“本轮响应结束”，不声称用户
  任务已经验收。fan-out 要等待全部 target 收尾；任一 worker 失败时 command
  终态为 `failed`，即使其他 worker 已正常回复，失败 worker 的 partial 也必须
  明确标注调用失败。固定任务区必须保留各 agent 阶段和终态；混合成功/失败
  显示“部分完成”。活动详情默认折叠，仅由上述显式操作逐卡切换；展开态按
  room_id 隔离并在会话切换后恢复。active/queued 可精确取消，
  terminal cancel 幂等，取消不得杀死 worker。
- 固定任务区的运行时长显示为“已用”，到达 terminal 后冻结为可读的
  “响应耗时”；活动卡只在 terminal 后显示同一冻结读数，避免为逐秒计时重绘
  整个 `RichLog`。时长以 CommandBus 的 `created_at` / `finished_at` 为权威，
  终态计时不得继续增长；缺少历史边界时显示“未知”，不得伪造为 0 秒。秒、分、
  小时按紧凑中文单位呈现，不得再使用无标签、易被误认作时钟的 `MM:SS`。
- TUI `Esc` / `Ctrl+X`、control `command.cancel`、MCP
  `myagents_cancel_command` 共用一个取消原语；外部可通过
  `myagents_read_events` 读取持久进度。
- TUI `Alt+↑` 只提升同 room FIFO 中最早的 queued command，composer 草稿不参与；
  其余排队项保持原顺序。workflow 继续走 ADR-0009 阶段边界；普通轮只有 adapter
  明确提供已验收 `interject()` 才允许，当前为 Pi 官方 RPC `steer` 与 Codex
  app-server `turn/steer`。意图必须先落 execution event，再写 transport；结果
  不确定时原 queued command 必须 terminal 且不得重放。多目标、ACP/native model
  等未获证路径保留原队列并 fail-closed。
- 重启后最后事件非 terminal 的命令必须显示为“已中断”，不得伪装完成。
- model Host 的 provider/model 只来自 ADR-0017 的 XDG 私有配置文件与显式环境
  临时覆盖；凭据不进入 readiness、事件或错误。agent Host 只来自
  `AgentSpec.host_factory`，独立于同名 worker 并固定 read-only。

### 4.7 Codex app-server（M4）

- 一个 adapter 独占一个 app-server 进程与当前 thread，同 adapter turn 串行；
  Codex worker 连续轮次复用持久 thread。Host 选择 Codex 时必须由独立的
  host-safe factory 创建 read-only app-server，不能复用 worker thread。
- 启动与请求不得覆盖 model、effort、config、collaboration mode、plugin 或
  MCP；只传协议必需字段、cwd、worker sandbox 与 approval-policy，且不写
  `~/.codex/config.toml`。
- `turn/completed` 是完成权威信号；取消发送 `turn/interrupt` 并等 terminal，
  未确认则关闭重建，下一轮不得与旧 turn 重叠。
- worker thread 使用 `workspace-write + on-request`，越界操作进入统一权限
  UI。approval 无处理器默认 decline；
  tool/command 元数据必须有界脱敏，reasoning 正文不可显示。
- 仅在 initialize/thread prepare（尚未发送用户 turn）失败时允许 JSONL
  fallback；一旦发送 `turn/start` 就禁止自动重放，避免响应丢失时重复工具副作用。
- `turn/start` 已发送后的结果不确定、terminal 失败/中断或用户取消，都先把
  本轮输入持久推进为 no-replay 边界，再公开错误/取消；服务端明确拒绝或确认
  未发送的失败保持普通可重试语义。旧 thread 无法恢复而新建 thread 时不得
  自动 bootstrap 已投递过的 Codex transcript。
- `turn/start` 明确接受后，no-replay cursor 必须早于正文、工具、权限等任何
  外部 event sink 回调落盘，避免执行事件写失败重新打开重投窗口。
- 普通 worker 的运行中插话仅在上述 cursor 已落盘后开放；使用同一 client 发送
  `turn/steer(threadId, expectedTurnId, input)`，必须命中原活动 turn，不创建第二
  turn/thread/writer。明确拒绝保留队首；写后取消、断线或响应不可信按 uncertain
  作废连接，源 queued command 终止且不得作为普通 turn 重投。
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

### 4.9 可切换 HostBackend 与原生模型 Runtime（M4.14）

- `HostAgent` 是 moderator/supervisor 产品角色，不是 transport；room 可显式
  选择直接模型或声明了 host-safe factory 的完整 agent，默认是原生模型。
- `HostBackendSelection` 只持久化 kind/target/reference；切换只在 Host 空闲或
  阶段边界执行，有界关闭旧 runtime 后创建 fresh session，不跨 backend 重放。
  持久选择不可用时必须阻断，不得自动 fallback。
- `ModelProvider/ModelEvent` 保持 provider-neutral；OpenAI-compatible
  Chat Completions 只是首个实现。模型发现是 provider capability：支持者用
  `/models` 精确验证，不支持者用配置的 exact id 由首个 chat 请求验证。
  provider 选择和 wire schema 不得进入 Orchestrator。
- 原生 host 配置默认来自 XDG 私有文件 `~/.config/myagents/config.toml` 的
  `[host.model]` 兼容 default 与 `[host.models.<name>]` 命名 profile；命名 profile
  只引用 `api_key_env`。readiness 只读文件/环境语法与 0600 权限，不联网。
  API key 只进入 header，并在 repr/错误/事件中脱敏。
- model Host 永久 `tool_policy=none`；agent Host 必须由独立 factory 构造并
  强制 `READ_ONLY`，不复用同名 worker session/writer。`/yolo` 不得扩权。
- `/host`、`/host model <profile-or-id>`、`/host agent <agent>` 是不进 timeline
  的 room 本地命令；只有明确注册 host capability 的 agent 可被选择。
- runtime 每 room 隔离上下文并保持单 writer；提交后的取消、静默超时、断流和
  非权威终态必须建立 no-replay 边界并重建 session，不跨 provider/agent 重放。
- 缺配置、模型不存在或 provider 拒绝必须给出可操作错误；不存在到第三方 Agent
  CLI 的隐式 fallback。完整契约和验收见 ADR-0017。

## 5. 测试策略

| 层级 | 证据 |
| --- | --- |
| 路由/编排 | `tests/test_basic.py` |
| 原生模型 provider/runtime/host | `tests/test_native_agent.py` + `tests/fake_openai_compatible_server.py` |
| ACP 协议与取消 | `tests/test_acp.py` + `tests/fake_acp_server.py` |
| Kimi hybrid transport | `tests/test_kimi_hybrid.py` + `tests/fake_acp_server.py` |
| OpenCode hybrid transport | `tests/test_opencode_hybrid.py` + `tests/fake_acp_server.py` |
| Qwen Code ACP-only 注册 | `tests/test_phase2.py` + `tests/fake_acp_server.py` |
| CodeBuddy ACP-only 注册/认证/profile | `tests/test_codebuddy_acp.py` + `tests/fake_acp_server.py` |
| DSH ACP-only 官方 CLI/profile/bundle readiness、execution profile、恢复/图片/终局 | `tests/test_dsh_acp.py` + `tests/fake_acp_server.py`（临时 `DSH_HOME`，不调用真实 DSH） |
| Pi RPC/attestation/权限 bridge/profile | `tests/test_pi_rpc_client.py` + `tests/test_pi_adapter.py` + `tests/test_pi_permission_bridge.py` + `tests/fake_pi_rpc_server.py`（只调用 fixture，不调用真实 Pi） |
| Agent 被动就绪探测/全局开关/原子资格门/TUI | `tests/test_agent_readiness.py` + `tests/test_tui_completion.py`（仅 fake resolver/临时私有配置） |
| 源码级全局命令打包 | `tests/test_packaging.py` + 临时目录 `uv build` 人工验收 |
| 会话级自然语言角色 | `tests/test_session_roles.py` + `tests/test_discussion.py` + `tests/test_tui_completion.py` + TUI 纯状态模型 |
| 自然语言有界讨论 | `tests/test_discussion.py`（显式 mention、host 路由、边界与同一状态机） |
| 自然语言有序协作与步骤投影 | `tests/test_collaboration.py` + `tests/test_tui_status.py` + `tests/test_tui_activity.py` + `tests/test_storage.py`（fake host/adapter、持久 plan event 与重启详情） |
| TUI/增量/权限/回收 | `tests/test_phase2.py` |
| 多会话目录/生命周期/TUI | `tests/test_session_catalog.py` + `tests/test_session_manager.py` + `tests/test_session_tui.py` |
| RoomStore 持久化 | `tests/test_storage.py` |
| M2.5 恢复/lease/时序 | `tests/test_m25.py` |
| M3 command bus | `tests/test_m3_bus.py` |
| M3 控制 socket 安全/协议 | `tests/test_m3_control.py` |
| M3 MCP stdio | `tests/test_m3_mcp.py`（官方 SDK client） |
| M4 Codex app-server | `tests/test_codex_app_server.py` + `tests/fake_codex_app_server.py` |
| M4.3 剪贴板图片 | `tests/test_clipboard_image.py` + macOS 人工截图验收 |
| 真实协议边界 | `docs/SPEC.md` 登记的 Kimi/OpenCode/CodeBuddy ACP + Textual/受限临时目录 E2E；Qwen 上游源码与本机启动证据；Pi 0.84.3 临时目录握手/短回复/逐次授权写入 E2E；DSH 仅登记 ADR-0015 真实验收清单，尚未宣称 E2E |

普通测试禁止调用真实 Kimi/Codex/OpenCode/Qwen/CodeBuddy/DSH/Pi 或模型服务。真实 agent/provider 验收必须由用户明确授权，
在临时目录运行，并在结束后检查没有残留进程。

## 6. 质量门禁

当前本地门禁顺序：

```text
harness 文档引用 → redlines → py_compile → readiness → basic → 会话角色 → ACP → Phase 2
→ Kimi hybrid
→ OpenCode hybrid
→ CodeBuddy ACP
→ DSH ACP
→ Pi RPC + permission bridge
→ native model host → storage → M2.5 → M3 bus → M3 control → M3 MCP stdio → M4 app-server
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
- 原生 host 已于 2026-08-27 通过本机 LM Studio `/v1/models` + 单轮
  `NATIVE_HOST_OK` 生产 runtime 探针；不同 provider 的兼容性、长上下文与成本
  仍属人工边界；
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
- 官方独立 CodeBuddy CLI 2.134.0 已于 2026-08-12 完成生产 adapter
  真实验收：中国区 `internal` 环境直接复用既有登录，default profile 建立 session
  `fbcc8cb1-2ee9-440f-b202-3e81a3007f05`，返回当时验收提示词要求的
  `WORKBUDDY_PRODUCTION_OK` 并
  `end_turn`；read_only profile 的 Write、Bash、WebFetch 与 Agent/subagent 负向
  探针均为 `Tool Not Found`，两个受检目录无新增文件，关闭后无 `codebuddy`
  进程残留。WorkBuddy.app 包内 CodeBuddy CLI 2.115.0 虽能握手，但正文路径会
  挂起，故明确不作为独立 CLI fallback；真实跨进程恢复、取消时延、图片与长期
  session 稳定性仍待人工验收；
- 2026-08-26 的 custom `tsx` source host 曾在隔离临时目录完成受限
  `new/load/prompt/close` 探针，但该启动路径已被标准 bundle + stock
  `dsh --profile myagents` 契约取代，**不计入最终发布验收**。官方安装入口、源码已构建
  CLI、临时 `DSH_HOME` profile 安装/解析、execution profile、permission、cwd/未知
  id、主动 cancel、图片、终局压力与无残留证据均须按 ADR-0015 重新记录；
- Pi 0.84.3 的本机 CLI 与源码已核实官方 `--mode rpc`、LF JSON request/event、
  session/stream/abort/image 与 extension 前置工具阻断能力；临时目录真实探针已
  完成 attestation、固定短回复/session 落盘、一次 `allow_once` 写入及无残留回收。
  默认 gate 仍只使用 fake RPC/extension fixture；真实路径逃逸、profile 重建、
  图片、abort 时延和长期 session 尚待人工验收。permission bridge 不是 OS
  sandbox，不支持不受信输入的无人值守执行；
- `/discuss` 的历史实测已于 2026-08-08 在同一持久房间恢复原
  Kimi/OpenCode session，经 MCP 完成两轮交叉讨论和当时的 Codex host 仲裁；
  单 user、连续 timeline、跨轮
  引用、无工具事件及退出回收均已核对。真实模型不进默认 gate；
- 长会话 compaction 后的 restore、真实 cancel 时延仍需人工验收。

具体 Owner、Sensor 与处置见 [`docs/harness-controls.md`](docs/harness-controls.md)。

## 8. 红线（6 条，违反必驳回）

| # | 红线 | 守门 |
| --- | --- | --- |
| R1 | 生产构造不得显式使用 `permission="auto"`，权限默认必须为 `deny` | `bash scripts/check-redlines.sh` 的 AST permission gate |
| R2 | 通用 orchestration/transport 层不得按具体 agent 名做条件分支 | `bash scripts/check-redlines.sh` 的 AST name-branch gate |
| R3 | UI、orchestrator、host 不得直接启动 shell/子进程 | `bash scripts/check-redlines.sh` 的 AST process-boundary gate |
| R4 | Kimi/OpenCode 必须受限 ACP-first；Qwen/CodeBuddy/DSH 必须 ACP-only 且固定 runtime profile；DSH 还必须以 myagents 标准 bundle + stock `dsh --profile myagents` 实现专用 ACP server、把 stock DSH 作为不可变依赖、使用被动 profile/bundle readiness、load+close hard gate、绝对状态目录、仅 end_turn 成功和零 fallback；Pi 必须是原生 RPC-only + 固定 bridge/wrapper/tool/profile attestation，禁止 raw RPC bash 和 fallback；OpenCode 普通轮风险工具 ask、只读轮 runtime deny；CodeBuddy 只用独立 CLI、固定已验收 region、按需有界认证且登录 URL fail-closed；JSONL 仅获证的 prepare-only 只读降级 | `bash scripts/check-redlines.sh` 的 registry/policy/profile/bridge gate |
| R5 | 自然语言讨论与 `/discuss` 必须共用 2–3 人、1–3 轮、终局主持状态机；自然语言协作必须保持 2–4 步、至少两个 worker、严格串行且失败/取消即停；两者均不得递归 dispatch/动态扩员；会话角色不得改变参与者、步骤/轮数、权限或 runtime | `bash scripts/check-redlines.sh` 的 discussion/collaboration bounds 与 AST gate + `tests/test_discussion.py` + `tests/test_session_roles.py` + `tests/test_collaboration.py` |
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
- M4.11 Pi RPC 与权限 bridge 事实源：
  [`docs/adr/0014-pi-rpc-permission-bridge.md`](docs/adr/0014-pi-rpc-permission-bridge.md)。
- M4.12 DSH ACP-only transport 事实源：
  [`docs/adr/0015-dsh-acp-only-transport.md`](docs/adr/0015-dsh-acp-only-transport.md)。
- M5.1 有界多智能体讨论事实源：
  [`docs/adr/0008-bounded-multi-agent-discussion.md`](docs/adr/0008-bounded-multi-agent-discussion.md)。
- M5 有界里程碑工作流事实源：
  [`docs/adr/0009-bounded-milestone-workflow-steering.md`](docs/adr/0009-bounded-milestone-workflow-steering.md)。
- M4.10 Agent 就绪与 setup UX 事实源：
  [`docs/adr/0012-agent-readiness-and-setup-ux.md`](docs/adr/0012-agent-readiness-and-setup-ux.md)。
- M7 自然语言有序协作事实源：
  [`docs/adr/0013-natural-language-sequential-collaboration.md`](docs/adr/0013-natural-language-sequential-collaboration.md)；
  M7.1 可见计划投影见
  [`docs/adr/0019-first-class-collaboration-plan-projection.md`](docs/adr/0019-first-class-collaboration-plan-projection.md)。
- 当前路线图：[`README.md`](README.md)“路线图”。
- 重大协议/安全边界改变先形成可评审设计记录，再修改本契约。
- Steering 只在同类失败至少两次或已有趋势证据时建立；单次失败只修当前问题。

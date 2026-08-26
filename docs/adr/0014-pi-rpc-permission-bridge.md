# ADR-0014：Pi RPC 权限桥与隔离 profile

- 状态：Accepted
- 日期：2026-08-25
- Owner：Bryant Yang
- 里程碑：M4.11

## 1. 背景与已核实事实

本机安装版与本地源码中的 Pi 均为 `0.84.3`。Pi 的官方持续集成入口是
`pi --mode rpc`：stdin/stdout 使用 LF 分隔 JSON，请求与事件支持持续 session、
流式 text/thinking/tool 事件、图片、`steer`、`follow_up`、`abort` 和 session
操作。`--mode json` 是一次性输出，不适合作为聊天室的有状态 transport；Pi
当前也没有 ACP 入口，因此不能把它冒充 ACP adapter。

Pi RPC 原生工具直接继承启动进程的操作系统权限。它没有与 ACP
`session/request_permission` 等价的逐次权限请求，也没有内置 workspace sandbox。
此外，RPC 控制面存在独立的 `type: "bash"` 命令；它不受模型工具白名单约束。
仅给内置工具传 `--tools`，或只在 prompt 中要求“只读”，都不足以成为 myagents
的生产安全边界。

Pi extension 在工具实际执行前提供可阻断的 `tool_call` hook，并可在 RPC 模式下
通过 `extension_ui_request` 与 host 交互。启动参数可以关闭自动发现的 extension、
skill、prompt template 与 theme，同时显式加载一个指定 extension，并通过
`--tools` 限定实际 active tool 集合。这些能力足以建立 myagents 自己拥有、可验证
且 fail-closed 的权限桥。

## 2. 决策

### 2.1 transport 与注册

- 生产只注册 `AgentSpec("pi", "rpc", PiRpcAdapter, ...)`，由独立的
  `pi_rpc/` client/adapter 持有进程、协议、session 与事件映射。
- 只启动官方 `pi --mode rpc`；不使用一次性 `--mode json`、headless JSONL 或
  ACP，不提供跨协议 fallback。
- 通用 Orchestrator 只读取 `AgentSpec` 与 `AgentAdapter` 能力，不增加
  `if agent_name == "pi"` 分支。Pi 可以像其他 ready worker 一样参加普通会话、
  fan-out、discussion、自然语言协作与有界 workflow。

### 2.2 唯一显式权限 extension 与工具闭集

Pi 进程必须同时满足以下启动约束：

1. 固定 `--offline` 与 `PI_OFFLINE=1`，禁止 stock tool manager 隐式下载、写盘或更新；
2. 关闭自动发现的 extensions、skills、prompt templates 与 themes；不把任何
   原生命名 built-in tool 放入 active tool 集合；
3. 只用绝对路径显式加载
   `pi_rpc/extensions/myagents_permission_bridge.ts`；
4. `--tools` 只列出本项目命名空间内的 wrapper tools：
   `myagents_read`、`myagents_grep`、`myagents_find`、`myagents_ls`、
   `myagents_edit`、`myagents_write`、`myagents_bash`；
5. wrapper 自己执行规范化路径检查与操作，不能把同名 Pi built-in tool 重新暴露
   给模型；未知工具、未知 action 和畸形输入一律阻断。

关闭自动发现不是为了覆盖用户的 Pi 配置，而是只隔离由 myagents 启动的子进程；
本项目不安装、删除或修改用户现有 Pi extension、skill 和配置。

### 2.3 三种 execution profile

Pi 与其他 agent 使用同一组 `ExecutionMode` 语义，但安全性由 runtime profile
实现，不依赖 prompt：

| profile | 可见 wrapper 闭集 | 权限行为 |
| --- | --- | --- |
| `DEFAULT` | 七个 wrapper 全部 | workspace 内读取直接执行；外部读取与 `edit`、`write`、`bash` 每次进入共享 TUI 权限决策器（默认弹窗） |
| `READ_ONLY` | `read`、`grep`、`find`、`ls` 四个 active wrapper | 写入与命令不在 active tool 闭集中且 hook 再次 hard-deny；外部读取仍逐次询权 |
| `WORKSPACE_WRITE` | 七个 wrapper 全部 | 与普通轮使用同一逐次 `allow_once`；不产生永久授权 |

所有路径型 wrapper 都 canonicalize 目标，拒绝 NUL、`~`、file URL 和附件语法等
歧义形式。workspace 内读取可直接执行；workspace 外读取（含 symlink 逃逸）必须
显示 canonical 目标并逐次批准。`edit/write` 只允许 workspace 内、非 `.git`、
非 hard-link 目标，创建新文件时校验最近的既存父目录。`bash` 不能靠字符串可靠
限制路径，因此每次都必须人工批准。人工选择只有 `allow_once` 与
`reject_once`；无 handler、超时、取消、异常、未知选项或不属于本次请求的选择
都等价于 reject。

权限 envelope 的 `input` 最多携带 4096 UTF-8 bytes 的有界展示摘要；截断时
写入 `_myagentsPreview` 元数据，不能复制大文件正文。脱敏由共享 PermissionScreen
统一完成，不把 transport preview 冒充已脱敏内容；超限 bash 不能安全隐藏尾部，
因此直接 block 而不是截断后请求批准。`argsHash`
必须覆盖完整 canonical args；bridge 将 hash、tool name、
toolCallId、一次性 call nonce、路径快照和 60 秒 TTL 绑定到 permit，执行前再次对完整
参数与路径快照校验并立即消费 permit。展示上限不能变成拒绝合法大文件写入的隐式
能力上限。

### 2.4 启动 attestation

transport 为每次启动生成不可复用的 nonce，并把 profile、规范化 workspace、
bridge 路径/摘要与期望工具闭集绑定到本次进程。bridge 在接受 prompt 前返回带
`MYAGENTS_PI_ATTEST_V1:` 前缀的 attestation；client 必须精确核对 nonce、policy
version、profile、workspace、bridge source 和 active tool 闭集，再在本次 select
请求中返回精确值 `ack:<nonce>` 完成握手；不存在可复用的全局 ACK。

Pi 0.84.3 的初始 `session_start` handler 会在 RPC stdin reader 安装前被 await。
因此 bridge 不得在该 handler 内直接 await attestation select，否则 host 尚不能
读取并应答 `extension_ui_request`，启动会形成确定性死锁。handler 必须只安排
fire-and-forget attestation task 并立即返回；task 以单调 session generation 绑定，
只有仍属于当前 generation 的成功 ACK 才能把 bridge 标为 ready。旧 session 的
迟到 ACK、reject、异常或 task completion 都不得改变新 session 状态。

attestation 缺失、超时、重复、字段未知、bridge 或工具集合不匹配时，adapter
保持 unavailable 并回收进程；不得降级、继续 prompt 或仅记录警告。检查的是
实际可调用工具与来源，不假设 Pi 内部 extension 总数恰好为一。

### 2.5 session、交付和生命周期

- 每个 adapter 是其 Pi session 的唯一 writer；session id 对 myagents 仅作为
  opaque checkpoint。
- Pi 0.84 会先返回受管目录内的 reserved `sessionFile`，直到第一条 assistant
  message 才创建文件。新会话只允许路径父目录、文件名和 native session id 全部
  绑定的未落盘 reservation；`agent_settled` 前必须重新要求单链接普通文件并核验
  header id/cwd。恢复路径则从一开始就必须存在并通过相同校验。
- adapter 在 checkpoint 旁原子持久化 0600 sidecar marker，字段闭集精确绑定完整
  token、session path/native id、canonical workspace 与 profile；reserved/materialized
  写入都执行临时文件 fsync、原子替换和父目录 fsync。跨进程恢复时，只有 marker
  仍为精确 reserved 且 session 文件尚未产生，才可在保留 Orchestrator no-replay
  cursor 的前提下建立 fresh native session；已 materialized 后文件缺失、marker
  缺失/损坏/越界、header 或 cwd 不匹配全部 fail-closed。
- profile 变化必须关闭旧进程并建立新进程、新 session，不继承旧 profile 的
  工具集合、权限或 session 授权；关闭与 profile 切换竞态时，关闭标记必须在
  旧进程回收后再次检查，禁止 `aclose()` 返回后重启 Pi。
- prompt 的成功响应是提交边界；提交后断线、超时、abort 结果不明均按
  no-replay 处理。prompt 帧已经 drain 但接受响应丢失时，也必须判为不确定交付并
  作废当前连接，不能复用仍可能执行的进程。事件可在成功响应前到达，但必须先
  缓冲；确认提交后先发布 `delivery_committed`，再发布缓冲事件。任何 permission
  handler/弹窗还必须等 consumer 恢复该内部事件——即 Orchestrator 已原子持久化
  no-replay cursor——之后才可运行，不能让工具批准抢在 checkpoint 前发生。
- 新 session 的可见 info 同样延后到 `delivery_committed` 之后，避免在 reservation
  checkpoint 与 prompt 提交之间引入可失败的 UI 窗口。
- `agent_end` 不是可靠终局；只有 `agent_settled`、session 已持久化，且本轮至少
  存在一个最终 assistant `message_end`，其 `stopReason` 属于显式成功闭集
  `stop/length/toolUse/deferred`，并且不存在
  `isError + result.terminate=true` 的终局工具阻断时才成功。自动重试期间的中间
  error 或后续已恢复的普通 tool error 不得提前失败；缺失/未知 stopReason 与
  `error/aborted` 一律作为提交后不确定失败。取消先发 `abort`，有界等待
  settle；失败则关闭并回收整个独立进程组。
- 无活跃工具时使用普通 inactivity timeout；工具已开始但尚未结束时使用独立的
  15 分钟 watchdog，避免正常 build/bash 被 2 分钟聊天静默阈值误杀。
- client 的公开 API 不接受任意 RPC dict，也绝不发送 RPC 控制命令
  `{ "type": "bash" }`。模型命令只能走 `myagents_bash` 权限 wrapper。
- 单帧上限 32 MiB；active prompt 同时受 4096 帧与 64 MiB 累计事件预算约束，
  消费后释放额度，任一溢出即关闭连接。extension UI 最多 32 个并发请求，活跃
  request id 必须唯一；重复或超限同样按协议错误 fail-closed。adapter 不得用无界
  中间队列提前抽空 client 的受预算事件队列；每次最多预取一个原生事件，权限镜像
  使用独立有界队列。

## 3. 能力范围

满足 attestation 后，Pi 可承担普通对话、discussion、review、verify 与 implement；
具体是否写入仍由当前 execution profile 和每次权限选择决定。图片只从现有房间
附件信任根转成 RPC image input，任意聊天路径不升级为图片；每轮最多 16 张、
读取前合计最多 20 MiB，超额在继续读取或发送前拒绝。

以下能力明确 block：

- 无 bridge 或 attestation 不通过时的任何 prompt；
- 未经逐次批准的写入或 shell；
- raw RPC command passthrough、RPC `type: "bash"` 和未知 wrapper；
- headless JSONL/ACP fallback，以及提交后的自动重放；
- profile 间复用进程或 session；
- 自动发现的第三方 extension/skill/MCP/subagent 通过 Pi 进程扩权。

## 4. 操作系统边界

权限桥是应用层 capability boundary，不是 OS sandbox。用户批准
`myagents_bash` 后，命令继承 myagents 进程的系统权限，恶意 shell 仍可能访问
workspace 外资源。因此本决策支持“用户在场、逐次审批”的本地工作，不宣称可将
不受信仓库或敏感凭据环境交给 Pi 无人值守执行。需要该保证时必须另加外部 sandbox
并形成新的 ADR；不能用更多 prompt 或路径正则替代。

路径 canonicalization、单链接检查与 permit 前后快照可以阻断正常的 symlink/hard-link
逃逸和批准后替换，但不把同一用户下的敌对并发进程纳入隔离边界；这类进程仍可能在
最终检查与底层 I/O 之间竞态改名。需要抵抗该威胁时同样必须使用外部 sandbox/容器，
不能宣称应用层 bridge 已提供不可竞态的文件系统边界。

Pi 0.84.3 的 Bash 子进程使用 detached process group，正常 SIGTERM/SIGHUP 依赖 Pi
自身 signal handler 调用 `killTrackedDetachedChildren()` 回收。fake fixture 按该拓扑
验收正常关闭；若 Pi 被外部直接 SIGKILL、内核崩溃或 signal handler 未运行，detached
子进程仍可能残留，这属于 OS sandbox/监督器之外的人工诊断边界，不能把父进程
`killpg` 测试描述成绝对保证。

## 5. 验收

默认 Harness 只使用 fake RPC server/extension fixture，不调用真实 Pi：

1. 注册与 argv 精确固定 `pi/rpc/PiRpcAdapter`、隔离开关、唯一 bridge 绝对路径、
   wrapper 闭集和三 profile；不出现 JSONL fallback。
2. attestation 的正常、缺失、超时、nonce/profile/workspace/source/tools 不匹配均
   有正反例；初始 `session_start` handler 非阻塞、迟到 task 受 generation guard
   隔离，prepare 后迟到的重复 attestation 立即作废并回收当前 generation，失败
   发生在第一条 prompt 前并回收进程。
3. fake extension 证明 `edit/write/bash` 每次只接受本次 `allow_once`；权限 handler
   必须发生在 durable `delivery_committed` 之后；无 handler、
   cancel、异常、畸形或未知选择拒绝，`READ_ONLY` 不激活风险工具且 hook hard-deny；
   大 write/edit 只展示摘要但委托完整参数，超限 bash 直接阻断。
4. workspace 边界覆盖外部读取逐次询权、写入越界拒绝、`~`/file URL/NUL 等歧义
   路径、symlink、hard link、`.git` 和不存在目标的既存父目录；错误路径没有副作用。
5. fake RPC 覆盖 stream、超过 1 MiB 的真实图片回显形状、16 张/20 MiB 读取前预算、
   new 延迟落盘/精确 restore、profile 重建、单事件背压、早到事件缓冲、长工具 watchdog、
   最终 assistant error、权限终止 tool、自动重试恢复、`agent_end`/`agent_settled`、
   abort、no-replay、关闭/profile 竞态，以及 Pi graceful signal handler 对 detached
   子进程的回收。
6. 静态红线阻断 raw RPC bash API、内置工具复活、bridge/工具闭集放宽及 Pi
   JSONL fallback。

真实 Pi 验收不进入默认 gate。2026-08-25 已在新建临时目录验证约 0.4 秒启动握手、
固定短回复/session 落盘，以及一次 `allow_once` 后由 `myagents_write` 创建精确内容；
两次关闭均无残留进程，且未安装/卸载 Pi 或修改用户配置。越界读取/写入拒绝与重询
由 fixture 固定；真实图片、长期 session、跨进程恢复、取消时延和 OS sandbox 仍
属于人工边界。

## 6. 不选择的方案

- **只读接入**：不能满足与现有 agent 一致的普通/implement 体验；安全问题应由
  逐次权限桥解决，而不是永久砍掉能力。
- **直接使用 Pi built-in tools + prompt 约束**：无法证明 workspace 边界，且
  raw RPC bash 绕开模型工具白名单。
- **只依赖 extension hook 包住 built-in tools**：可拦截但难以独立证明 active
  tool source；唯一命名 wrapper 闭集提供更清晰的可验证边界。
- **`--mode json` fallback**：丢失持续 session/事件/取消语义，并会引入跨协议
  不确定重放。
- **把 Pi 当 ACP**：wire protocol 与 session/权限模型均不同，会污染通用 ACP
  runtime。

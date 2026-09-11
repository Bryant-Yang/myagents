# ADR-0022：Claude Code headless stream-json transport 与 MCP 权限桥

- 状态：Accepted
- 日期：2026-09-10
- Owner：Bryant Yang
- 里程碑：M4.15

## 1. 背景与已核实事实

Claude Code 是市占最高的 coding agent，但在 myagents 的传输表中长期处于
"规划中"。接入前核实了官方集成面（文档域 code.claude.com，本机 CLI
2.1.267，2026-09-09）：

- **没有官方 ACP**。官方文档不存在 ACP 页面，编程化集成的官方推荐路径只有
  CLI headless 与 Claude Agent SDK。按 Pi RPC 先例（ADR-0014），厂商协议
  必须封装在原生 adapter 内，不引入社区 ACP 适配层。
- **长连接多轮**：`claude -p --input-format stream-json
  --output-format stream-json` 是常驻进程；每轮从 stdin 接受一条 user
  NDJSON 帧，stdout 输出事件帧并以**恰好一条 `result` 帧**结束本轮。
  `--include-partial-messages` 提供 `stream_event`（`text_delta` 等）。
  **启动是输入驱动的**：2026-09-10 真实探针证实，收到第一条 stdin 消息
  之前进程完全静默（无 init、无 stderr）；首条消息后按 `system/init` →
  `system/status` → replay 回执 → 事件流 → `result` 的顺序输出。启动
  失败（非法 flag、`--resume` 未命中）因此在首个 turn 才浮出。
- **交付确认**：`--replay-user-messages` 把 stdin 收到的 user 消息回显到
  stdout 作为确认。这是官方文档定义的接受信号，可充当 no-replay 边界。
- **交互式权限回调**：`--permission-prompt-tool` 指向一个经
  `--mcp-config` 注册的 MCP 工具，返回契约是
  `{"behavior":"allow","updatedInput":...}` / `{"behavior":"deny","message":...}`。
  `--strict-mcp-config` 保证只有这一份 MCP 配置生效。
- **执行安全开关**：`--restricted`（≥2.1.248）移除代码执行工具与
  WebFetch、把文件工具限制在工作目录、拒绝 `bypassPermissions`；
  `--permission-prompts none`（≥2.1.259）让一切本应弹提示的调用直接拒绝。
- **session**：`--session-id <uuid>` 可为新会话预生成 ID；`--resume <id>`
  显式恢复（`-p` 会话不在 picker 中但可显式 resume）；resume 未命中的
  精确错误是 stderr 句子 `No conversation found with session ID: <id>`；
  transcript 是内部格式，任何版本可能变化，**不得解析**。
- **打断**：SIGINT 结束当前轮（进程存活）；SIGTERM 以退出码 143 终止且
  该轮不完成。`control_request` 双向控制帧是 SDK 内部协议，**官方文档
  未公开线格式**。
- **图片**：只有流式输入模式支持把 base64 image content block 直接附在
  user 消息里；单发管道模式明确不支持。
- **result 用量**：`result.subtype` 取值 `success` 与
  `error_max_turns` / `error_during_execution` / `error_max_budget_usd`；
  `total_cost_usd` 是客户端估算值。

## 2. 决策

### 2.1 transport 与注册

- `AgentSpec("claude", "stream-json", ClaudeCodeAdapter,
  claude_readiness_probe, display_name="Claude Code")`。`stream-json` 是
  新的 transport 标签，仅用于展示与红线 registry；通用层不出现
  `if name == "claude"`（R2 把 `"claude"` 列入 worker 名集合）。
- readiness 纯被动：`MYAGENTS_CLAUDE_CLI` 显式路径（校验普通文件 + 可执行）
  → PATH 解析 `claude`；不执行 CLI、不读登录态、不联网。
- 包结构 `claude_code/`：`client.py`（进程 + NDJSON 帧协议 + 单写者）、
  `adapter.py`（profile/事件映射/生命周期）、`permission_server.py`
  （stdio MCP 权限桥）。

### 2.2 两 profile 与 argv 闭集

跨 execution profile 必须重建进程；`DEFAULT` 与 `WORKSPACE_WRITE` 共用
普通 profile（写权限由 Claude Code 的 permission-mode default 加 TUI
逐次弹窗收口）。

- 普通/写轮 argv 闭集：
  `claude -p --input-format stream-json --output-format stream-json
  --verbose --include-partial-messages --setting-sources ""
  --strict-mcp-config --mcp-config <0600 配置文件路径>
  --permission-prompt-tool mcp__myagents-claude-permission__request_permission
  --permission-mode default
  (--session-id|--resume) <uuid> --replay-user-messages`
- 只读轮 argv 闭集：
  同上前 8 项，另加 `--restricted --tools "Read,Glob,Grep"
  --permission-prompts none --no-session-persistence`，**不注入**权限桥、
  无 session flag。
- 环境硬化（固定四项）：`DISABLE_AUTOUPDATER=1`、
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`、`DISABLE_TELEMETRY=1`、
  `DISABLE_ERROR_REPORTING=1`。不设置 `CLAUDE_CONFIG_DIR`（复用既有登录，
  对照 CodeBuddy 的"已有登录态直接复用"）。
- `--setting-sources ""` 关闭全部 settings 文件加载：用户 settings 里的
  `permissions.allow` 规则若生效，会绕过 myagents 的 TUI 弹窗决策，必须
  排除。项目 CLAUDE.md 自动发现保留（与其他 worker 可读仓库文档一致，
  且 CLAUDE.md 不能改变权限）。
- v1 不透传 `--model` / `--max-turns` / `--max-budget-usd`。

### 2.3 权限桥

- `permission_server.py` 是 stdio MCP server，由 claude 按
  `--mcp-config` 指向的 0600 配置文件（写在 myagents 私有 state 目录，
  避免 token 出现在 argv 上被 `ps` 读取）中的 `env` 注入
  `MYAGENTS_CLAUDE_PERMISSION_SOCKET`（Unix socket 路径，同目录）与
  `MYAGENTS_CLAUDE_PERMISSION_TOKEN`（每进程一代的 token）后自行拉起。
- adapter 监听该 socket（0600）；每个请求独立连接，逐条校验
  version/token（常数时间比较）/toolName/input 大小闭集，任一不符直接
  deny。
- 合法请求映射为标准 myagents 权限 params（`allow_once`/`reject_once`
  两个 option），走共享 TUI 弹窗；outcome 逐一校验，allow 回写
  `{"behavior":"allow","updatedInput":<原 input>}`，reject/cancelled/
  异常/超时/断连一律回写 deny。
- 权限弹窗只允许在 `delivery_committed`（no-replay cursor 已持久化）之后
  展示与授权（checkpoint gate，照 Pi 模式）。
- **普通轮启动必须确认权限桥已连接**：`system/init` 的 `mcp_servers`
  必须包含 `myagents-claude-permission` 且 `mcp_server_errors` 为空，否则
  fail-closed 拒绝启动；绝不以无桥模式跑可写轮。只读轮反向校验
  `mcp_servers` 为空。

### 2.4 session、交付和生命周期

- myagents 自持 session checkpoint（不解析 Claude transcript）：
  `claude:v1:` + base64url JSON `{version, uuid, profile, workspace}`。
  fresh 轮生成 `uuid4` 并以 `--session-id` 传给 CLI；重启后以 `--resume`
  续接。
- resume 失败处理：仅 stderr 精确出现 `No conversation found with
  session ID:` 时允许回退 fresh session 一次（cursor 增量仍生效，无历史
  重放）；fresh 启动失败与其他一切错误 fail-closed。
- **交付确认**：写入 user 帧后，client 在回执等待中先消费 `system/init`
  （实测先于回执），再等 `--replay-user-messages` 回执；二者到齐才产出
  `delivery_committed`（先于一切可见事件）。init 校验在 `delivery_committed`
  之前执行：普通轮 `mcp_servers` 缺桥或 `mcp_server_errors` 非空即
  fail-closed，且该进程不复用；**回执消费之后的任何失败（含 init 校验
  失败与 CLI 合成的 API 错误 assistant 帧）必须以
  `AgentDeliveryUncertainError` 收口**，否则 orchestrator 不推进
  no-replay cursor，已交付轮可被重投。CLI 合成帧以
  `message.model == "<synthetic>"` 识别，错误原文经脱敏附在失败证据里。
  写入失败按
  may_have_written 区分
  pre-submit 失败与 `AgentDeliveryUncertainError`；resume 未命中在读取
  stdin 之前发生（本轮必然未执行），允许 `stream_prepared` 层一次性回退
  fresh session。
- **终态**：`result.subtype == "success"` 是唯一成功（对齐 DSH 仅
  `end_turn` 规则）；`error_*` subtype 与 result session 不一致一律
  `AgentDeliveryUncertainError`。
- **取消**：`os.kill(pid, SIGINT)`（文档语义：结束当前轮；只对 CLI 进程，
  不打进程组以免波及权限桥）→ 有界等待 result；未确认则回收整个进程组。
  committed 未 settled 的轮次结束前必须重建进程（单写者/无重叠最终保证）。
- 看护：无事件 300s / 活跃工具 900s 双阈值；NDJSON 帧上限 32MiB（回执
  内嵌 base64 图片）；`start_new_session=True` + stderr `BoundedLog` 排空
  + `killpg` SIGTERM→SIGKILL。
- 只读轮使用一次性进程与临时会话，但 checkpoint 原样保留，使后续普通轮
  仍能 `--resume` 原 durable session（与 Pi 的跨 profile 互斥不同，这里
  只读轮不写入任何持久状态，因此不破坏普通轮上下文）。
- Host capability：`AgentHostCapability("stream-json", lambda:
  cls(host_read_only=True))`，host 专用构造器硬编码只读闭集。

### 2.5 图片

复用 `clipboard_image` 的 TrustedImage 链路（附件根校验、PNG 容器校验、
16 张 / 20MiB 上限），在流式输入的 user 帧中以 base64 image content block
发送。

## 3. 能力范围（明确 block 的情况）

以下能力 v1 明确不做，出现在代码或红线中即驳回：

- **运行中插话（interject）**：bare CLI 没有文档化的 mid-turn steer 线
  格式；`ClaudeCodeAdapter` 不得声明 `interject`。orchestrator 按
  ADR-0018 能力缺失 fail-closed 拒绝。
- **认证流**：不自动打开浏览器、不轮询登录态；未登录时诚实失败并给出
  setup hint（readiness 只查 CLI 存在）。
- **裸 control 协议**：任何未文档化的 stdin 控制帧。
- **transcript 解析**：`~/.claude/projects/**` 是内部格式。
- **`--bare` 模式**：永不读取 OAuth 凭据，会破坏订阅登录用户。
- **`--model` / `--max-turns` / `--max-budget-usd` 透传**：留待后续 ADR。

## 4. 操作系统边界

MCP 权限桥是应用层决策通道，**不是 OS sandbox**：批准 Bash/Edit 后，
Claude 子进程仍继承 myagents 的本机进程权限；复用用户既有登录态也意味着
继承该账号的 API 配额与计费。`/yolo` 的会话级 allow-once 语义与 workflow
只读轮硬边界照常适用于本 adapter；无人值守处理不受信输入依旧不被支持。

## 5. 自动化验收

默认 gate 的证据入口：

- `tests/fake_claude_stream.py` + `tests/test_claude_stream_client.py`：
  init 握手、回执先于事件、回执丢失→uncertain、回执前事件→协议错误、
  init 前退出、resume-miss 精确映射、SIGINT 取消保进程、close killpg、
  单写者、出/入帧上限、图片 base64 块与数量预算。
- `tests/test_claude_adapter.py`（进程内 FakeClaudeClient）：注册形状
  （stateful、replay=False、host capability）、普通轮事件序列与 argv/env
  闭集、同 profile 复用、只读轮闭集且不破坏 durable checkpoint、跨
  profile 重建、resume token 工作区/profile 绑定、resume-miss 一次回退、
  权限桥缺失/错误 fail-closed、uncertain/取消后重建、看护超时、仅
  success 终态、权限 outcome 校验（交付前拒绝、畸形 outcome 拒绝、允许
  路径回写）、socket 全链路 token 校验、受信图片转发、aclose 回收、Host
  构造器强制只读、探针三态。
- `tests/test_claude_permission_server.py`：缺凭据启动即败、畸形参数
  不触碰 socket 直接 deny、socket 应答归一化、MCP 工具调用全链路。
- `scripts/check-redlines.sh` Claude 专段：受控文件存在、常量闭集
  （transport/桥名/env 名/只读工具集/成功 subtype/硬化 env）、`_command`
  argv 闭集与禁用项、session flag 存在、v1 无 `interject`、token 常数
  时间比较、client 无 control_request/start_new_session/SIGINT/killpg、
  桥默认 deny 且不启动子进程。

以上全部使用 fake 进程/内存 fake，不调用真实 Claude CLI。

## 6. 真实验收清单（人工，不进默认 gate）

在本机已登录 Claude Code 的环境验证并记录到 SPEC。

**2026-09-10/11 已执行（本机 CLI 2.1.267，探针
`scripts/e2e-m415-claude-real.py`，全部通过）：**

- [x] 冷/热两轮对话：同一 CLI 进程复用（冷轮 6.7s，热轮同 pid）、流式正文、
  `delivery_committed` 先于全部可见事件。
- [x] 一次权限 allow 与一次 deny（经真实 MCP 桥）：deny 后文件保持
  `ORIGINAL`；一次 `allow_once` 后文件改写为 `ALLOWED`。
- [x] 只读轮：进程重建、argv 含 `--restricted` 闭集、读取成功、写入被
  工具闭集自动拒绝。
- [x] 重启 `--resume`：新进程续接同一 checkpoint 并记得此前交付内容。
- [x] 提交后取消：no-replay 生效，进程组回收、无残留。
- [x] 图片：受信 PNG 经 base64 内容块被正确识别（绿色 → "green"）。
- [ ] TUI 内 Esc 取消与 `Ctrl+V` 截图交互路径（探针以 adapter 层等价覆盖
  取消与图片；TUI 键盘路径待人工补验）。
- [ ] 未登录负例：失败证据诚实呈现，不拉起浏览器。
- [ ] 版本升级后重跑：`system/init` 的 `mcp_servers` 条目形状、
  `--permission-prompts`/`--restricted` 可用性、replay 回执行为。

**实测发现并已修复的契约偏差（真实探针价值记录）：**

1. **输入驱动启动**：`-p --input-format stream-json` 在首条 stdin 消息前
   完全静默，`system/init` 并非开机即发（§1 已更正）。client 的
   `start()` 不再等待 init；init 与 replay 回执都在首个 turn 内消费，
   init 校验移到 `delivery_committed` 之前。
2. **pre-echo 杂音帧**：init 与回执之间存在 `system/status` 等帧，回执
   等待必须容忍。
3. **`--permission-prompt-tool` 必须显式指向
   `mcp__<server>__request_permission`**：缺失时 `-p` 对权限请求静默
   拒绝、桥不工作。
4. **MCP 调用拆包**：claude 调用权限工具的 arguments 是
   `{tool_name, input, tool_use_id}`，桥必须经验证路径拆包转发；直接
   转发会把整个 arguments 当 `updatedInput` 回写，被模型侧 schema 校验
   拒绝（表现为"权限层配置故障"）。
5. claude 写文件带尾换行，验收比较按内容语义处理。
6. **认证在 settings.json 的 env 块**：`--setting-sources ""` 会把代理
   认证一起屏蔽，CLI 对 API 错误会**本地合成一条 assistant 消息
   （`model == "<synthetic>"`，无流式 delta、无错误标志字段）并仍返回
   success result**。adapter 现将认证键挑拣进 0600 `--settings` 副本，
   并把 synthetic assistant 帧如实判为 uncertain（附错误原文）。

## 7. 不选择的方案

- **社区 Zed ACP 适配器**：非官方面，维护状态不受厂商承诺，与仓库
  "可靠官方长连接优先"原则冲突。
- **`--bare` 模式**：官方文档明示其从不读取 OAuth/keychain，仅接受
  `ANTHROPIC_API_KEY`，会破坏订阅登录用户的开箱可用性。
- **手写未文档化的 stdin 控制协议**（SDK 内部 `control_request`）：无
  兼容承诺，违背仓库"官方面 only"红线。
- **全量加载用户 settings**：settings 级 `permissions.allow` 与 hooks
  会绕过/干扰 TUI 权限决策，违背 R1 fail-closed。
- **`CLAUDE_CONFIG_DIR` 隔离目录**：登录态与 trust 状态留在原目录，隔离
  即丢失，首版复用既有登录（对照 CodeBuddy）。

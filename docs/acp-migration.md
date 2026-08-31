# ACP 迁移设计（最小方案）

状态：**Phase 4.5、Phase 4.6、Phase 4.9、Phase 4.11、Phase 4.12、Phase 5.1 与 Phase 5 已完成**。
`AgentAdapter` 是 TUI 与不同 coding agent 的
统一行为契约；wire protocol 按厂商能力选择：`acp/` 是通用 ACP runtime，
Kimi/OpenCode 分别走 `kimi acp` / `opencode acp`，只在 ACP prepare
失败前使用各自受限 JSONL fallback；Qwen Code 走 `qwen --acp`、CodeBuddy
走官方独立 CodeBuddy CLI `--acp --acp-transport stdio`，两者保持 ACP-only；
DeepSeek Harness（DSH）只走专用 `dsh-myagents-acp` host，保持 ACP-only；
Pi 走官方 `pi --mode rpc` 与 myagents 权限 bridge，保持 RPC-only；Codex 走官方
`codex app-server`，旧
Codex adapter 保留 JSONL fallback。app-server 不是 ACP，各协议只在
adapter 层统一。Pi RPC 与 app-server 都不是 ACP。
Phase 2.5 落地了共享 history 持久化、ACP
session 映射与重启恢复、房间单写者 lease；Phase 3 落地了内部
command bus（`control/`）、私有 Unix 控制 socket 与 stdio MCP 外部入口
（`myagents_mcp.py`）——本机其他 agent 可以向运行中的同一房间注入
消息，单写者与 TUI 权限决策不变。多-agent 路由仍由
orchestrator 管——具体协议只负责传输，不参与"派给谁"的决策。

## 为什么迁移

私有 JSONL adapter（`kimi -p` 等无头模式）是"一次性命令"：每次调用新进程、
新会话，上下文靠 transcript 转发（token 线性膨胀），无法中断、无法恢复会话、
权限只能一把梭。ACP 是有状态协议，一次解决这四件事。

Pi 同样提供官方有状态入口，但 wire contract 不同：`pi --mode rpc` 使用 LF
分隔 JSON request/event，支持 session、stream、图片、steer/follow_up 与 abort；
`--mode json` 仍是一次性输出。Pi 没有 ACP 权限请求和内置 sandbox，因此必须由
独立 `pi_rpc/` adapter 与显式 permission bridge 建立安全边界，不能塞进通用 ACP
runtime。完整决策见 [ADR-0014](adr/0014-pi-rpc-permission-bridge.md)。

## 实测确认的协议形状（kimi acp，protocolVersion 1）

```
→ {"id":0,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{...}}}
← {"id":0,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true,
    "promptCapabilities":{"image":true,"audio":false,"embeddedContext":true},
    "sessionCapabilities":{"list":{},"resume":{}},...},"agentInfo":{...}}}

→ {"id":1,"method":"session/new","params":{"cwd":"/tmp","mcpServers":[]}}
← {"id":1,"result":{"sessionId":"session_xxx"}}

→ {"id":2,"method":"session/prompt","params":{"sessionId":"...",
    "prompt":[{"type":"text","text":"..."}]}}
← {"method":"session/update","params":{"sessionId":"...","update":
    {"sessionUpdate":"agent_thought_chunk|agent_message_chunk|tool_call|...",
     "content":{"type":"text","text":"..."}}}}   （若干条，流式）
← {"id":2,"result":{"stopReason":"end_turn"}}

← {"id":N,"method":"session/request_permission","params":{"sessionId":"...",
    "toolCall":{...},"options":[{"optionId":"...","kind":"allow_once|reject_once",...}]}}
→ {"id":N,"result":{"outcome":{"outcome":"selected","optionId":"..."}}}
   或 {"outcome":{"outcome":"cancelled"}}

→ {"method":"session/cancel","params":{"sessionId":"..."}}   （通知，无响应）
```

M4.3 图片附件沿用同一个 `session/prompt`：只有 Kimi 明确声明
`promptCapabilities.image=true` 时，客户端才在 text block 后追加
`{"type":"image","mimeType":"image/png","data":"<base64>"}`。图片只从当前
房间的私有 `attachments/` 信任根提取；聊天文本中的任意外部路径不会升级为
image block。新消息使用 `[图片 N]` 映射当前房间的 `img-NNNN.png`；旧绝对
路径引用只在同一信任根内兼容。二进制不进入共享 timeline。

M4.7 由 SessionManager 为每个已加载房间独立持有 Orchestrator/CommandBus 和
ACP/Pi RPC/app-server runtime。切换可见会话不迁移或复用 native session writer；后台
权限请求携带 room_id，并在弹窗显示项目、会话、agent 与工具。空闲回收只关闭
非当前、无 command 且无权限等待的完整 runtime。

## 架构（Phase 2 现状）

```
orchestrator.py（AgentAdapter 接口不变；AGENT_SPECS 注册表）
   │  AgentSpec(name, transport, factory)
   │    kimi     → acp+jsonl → AcpKimiAdapter
   │                         └→ KimiAdapter（prepare-only，只读 profile）
   │    codex    → app-server → CodexAppServerAdapter（原生 thread/turn）
   │                         └→ CodexAdapter（安全 JSONL fallback）
   │    opencode → acp+jsonl → AcpOpenCodeAdapter
   │                         └→ OpenCodeAdapter（prepare-only，隔离只读配置）
   │    qwen     → acp       → AcpQwenAdapter（default/plan，无自动降级）
   │    codebuddy→ acp       → AcpCodeBuddyAdapter（default/受限只读，无自动降级）
   │    dsh      → acp       → AcpDshAdapter（workspace-write/read-only，无自动降级）
   │    pi       → rpc       → PiRpcAdapter（唯一 permission bridge，三 profile，无降级）
   │
   ├─ acp/adapter.py  AcpAdapter（通用：name + cmd 即一个 ACP agent；
   │     │            stateful_session = True 声明"会话在 agent 侧保持"）
   │     │  持有 session，串行化轮次（asyncio.Lock，aclose 同锁）
   │     ▼
   │   acp/client.py  AcpClient（协议层，与具体 agent 无关）
   │     JSON-RPC 2.0 / NDJSON / id 关联 / 反向请求 / killpg 清理
   │
   ├─ codex_app_server/  Codex 专用 JSONL-over-stdio client/adapter
   │                     （不是 ACP；无 jsonrpc header）
   ├─ pi_rpc/  Pi 专用 LF-JSON client/adapter + 唯一显式 permission bridge
   │           （不是 ACP；关闭自动发现/原生命名 tools，启动先 attestation）
   └─ adapters/*_adapter.py  一次性 JSONL fallback
```

新增一个 agent 不需要改编排器：写一行 `AgentSpec` 并在具体 adapter 隔离协议即可。会话中的自然语言
角色不创建新 agent 或 runtime，只在实际 agent 的 assignment 前注入受限工作视角，
具体约束见 [ADR-0011](adr/0011-session-scoped-natural-language-roles.md)。
协议判断只看 `transport` / adapter 能力声明（`stateful_session`、
`set_permission_handler`、`aclose`），不散落 `if name == "..."`。
Kimi/OpenCode hybrid 的完整时机、权限与 checkpoint 契约见
[ADR-0006](adr/0006-kimi-hybrid-transport-policy.md)与
[ADR-0007](adr/0007-opencode-hybrid-transport-policy.md)。Qwen Code 的
`stream-json` 输入仍在上游文档中标记为未完成，且项目尚无独立只读 fallback
profile 的安全证据，因此生产只注册 ACP 路径，不做跨协议自动重放；普通 ACP
轮强制 `--approval-mode default`，workflow 只读轮强制 `plan`。
CodeBuddy 同样保持 ACP-only。adapter 优先解析显式
`MYAGENTS_CODEBUDDY_CLI`、PATH 中的 `codebuddy`/`cbc`，只接受可独立运行的
官方 CLI，不使用 WorkBuddy.app 包内私有二进制；产品身份始终显示为
`codebuddy`，不再注册 `workbuddy`，也不声称接入 WorkBuddy App。普通轮固定
`--permission-mode default`，workflow 只读轮使用 `dontAsk`、
`--subagent-permission-mode dontAsk` 与 `Read,Glob,Grep` 工具闭集，并用空 setting
sources + strict 空 MCP 配置阻断用户/项目配置扩权；profile 切换会重建进程和
fresh session。进程环境固定为已验收的中国区 `internal`；session prepare 优先
复用已有登录，只有服务端明确返回 `-32000 Authentication required` 才进入有界
浏览器认证。
DSH 同样保持 ACP-only，完整决策见
[ADR-0015](adr/0015-dsh-acp-only-transport.md)。安装入口接受
`MYAGENTS_DSH_CLI` 指向或 PATH 解析出的官方 `dsh`；源码入口只接受
`MYAGENTS_DSH_SOURCE_ROOT` 并定位该树已构建的官方 `apps/cli/lib/bin.js`。两者都固定
启动 `--profile myagents`。产品 `@myagents/dsh-acp-host` 是
`dsh_acp/plugin` 拥有的标准 bundle，声明 `dsh.bundle.patch=./cordis.patch.yml`，自己
实现 ACP server 与产品 host，只消费 stock DSH 的公开 agent/AgentSetup/
ToolRuntime/持久化服务；不委托 stock `dsh-acp-demo`，不深层导入未公开 `src/*`，也
不复制或修改 DSH 的 core、官方 ACP 包、示例与测试。readiness 不调用
pnpm/build/CLI/真实 agent，也不创建或修复 profile/state；它被动要求
`profiles/myagents` 的依赖与 bundle 顺序精确为 base → host，并读取解析后 package 的
name/version/entry/patch。`DSH_HOME/profiles` 与 `profiles/myagents` 必须是 profile
home 内真实、非 symlink 的 canonical 目录，profile manifest 以 no-follow、单链接、
有界且身份稳定的句柄读取。stock DSH 在 bundle 后继续应用 home/profile 两层
`cordis.patch.yml`，因此两者只能缺失，或是至多 64 KiB 且忽略空行/注释后唯一语义行
精确为 `[]` 的 canonical 单链接普通文件；空文件、仅注释文件、有效 patch 与任何
symlink/hardlink/越界都 fail-closed，并在 spawn 前和活跃进程下一轮前重验。
readiness 不用 Git 或完整 runtime-tree fingerprint 审计 DSH checkout；release gate
的完整树前后比较只证明验收未修改不可变依赖。普通/写轮覆盖
`DSH_ACP_PROFILE=workspace-write`，只读轮覆盖
`DSH_ACP_PROFILE=read-only`，跨 execution safety profile 重建进程和 fresh session。
子进程状态固定为绝对 `DSH_ACP_PERSISTENCE_DIR`（默认遵循
`XDG_STATE_HOME`），其下固定 sessions/runtime-home/attachment-home/agents 拓扑，
`DSH_HOME` 保留为 stock profile home，`DSH_AGENTS_HOME` 只指向产品状态。profile
home 的 settings/credentials 经 no-follow 有界读取后复制为 `config-inputs/` 的私有
产品文件。profile home、source、state、workspace、plugin/CLI 与原始配置不得重叠。
`MYAGENTS_DSH_SOURCE_ROOT` 只定位官方 launcher；产品 host 组合由 profile 中的
标准 bundle 提供。首个
session 操作前必须看到 load/close capability，且无
headless、SDK RPC 或 JSONL fallback。initialize 还必须通过专用 host identity gate：
name 为 `dsh-myagents-acp`、version 精确为 `0.1.1`，metadata 必须遵守
[ADR-0015 §2.2](adr/0015-dsh-acp-only-transport.md)
的五个 literal key/schema：`deepseek.ai/dsh-myagents-profile` 是与当前
`DSH_ACP_PROFILE` 一致的
profile string，`deepseek.ai/dsh-myagents-policy-revision` 是 integer `1`，
`deepseek.ai/dsh-myagents-read-only-tools` 是顺序精确的
`["read", "glob", "grep"]`，runtime version 为 `0.1.2-alpha.2`，compatibility
revision 为 integer `2`。权限 options 每次都必须是不同 id 的
`allow_once` / `reject_once` 两项；任何 `allow_always` 或结构漂移直接 cancelled。
源码 launcher 的 argv 定位不依赖 ambient cwd，但 transport 必须在 initialize 前把 DSH
子进程 cwd 绑定为目标 workspace；同一活跃进程跨 cwd 使用必须 fail-closed。
外层取消、`stream.aclose()`、inactivity 等任何实际发出
`session/cancel` 的路径都形成 committed/no-replay，且只有精确
`stopReason=cancelled` 可复用连接。
持久化固定为 uncompressed、unpacked JSONL；load 在完整历史 materialize 前通过
公开 persistence `list`/`locate` 做 no-follow 文件扫描，限制 4096 events / 16 MiB，
并在 materialize/resume 前复核 stat identity，无法证明时不广告或拒绝 load。
Pi 保持 RPC-only：`PiRpcAdapter` 只显式加载固定 bridge，关闭自动发现且不激活
原生命名 built-in tools，按 `DEFAULT` / `READ_ONLY` / `WORKSPACE_WRITE` 启动
精确 wrapper 闭集。
第一条 prompt 前必须完成 nonce/profile/workspace/policy/source/tool-set attestation；
profile 切换重建进程并建立 fresh session。写入与 shell 每次只接受与本次 call
绑定的 `allow_once`，且 client 没有 raw RPC passthrough/`type: "bash"` 入口。

## 铁律：会话唯一持有者

一个 session 同一时刻只能有一个 writer。违反就会话损坏：

- 每个 stateful adapter 实例是它 native session 的唯一 writer（adapter lock 串行化轮次）
- session id/checkpoint 不得交给对应 native TUI 或另一个进程并发使用
- 恢复旧会话前必须确认没有别的进程持有它；Pi 还必须精确核验 session 文件和 cwd

## 增量上下文契约（Phase 2 新增）

stateful native session 是持久上下文，编排器**不再**每轮转发完整 transcript：

- 编排器为每个有状态 agent 维护一个 history cursor
  （`Orchestrator._cursors`），记录已交付到共享时间线的哪个位置。
- 每轮只发 cursor 之后的新消息：用户的新发言 + 其他 agent 的新回复。
  这是共享多-agent 时间线，不能只发孤立的最新一条。
- agent 自己的回复按 speaker 过滤、不重发——它们本来就在它的 ACP
  session 里，重发就是重复上下文。
- **首次 bootstrap 限界**：cursor=0 的首次派发只发最近 `history_limit`
  条（仍过滤自身），避免长聊天后第一次 @ 就无界发送全部 history；
  成功后 cursor 直接推进到本轮快照末尾，后续继续走纯增量。
- **delivery lock（P1 契约）**：读 cursor → 选增量 → stream 完整执行 →
  推进 cursor 是原子单元，在该 agent 的 delivery lock 内完成，prompt
  拿到锁之后才构造。同一 agent 的并发 dispatch 严格串行（不重复、
  顺序保持）；不同 agent 持不同的锁，并行扇出不受影响。
- **cursor 成功交付后推进**，且只推进到本轮构造时的快照末尾（本轮进行期间
  到达的新消息留给下一轮）。prompt 提交前或服务端明确拒绝的失败不推进，
  下一轮从旧 cursor 补发；prompt 已提交后的 inactivity timeout 等结果不确定
  失败必须先提交同一快照末尾的 no-replay cursor，再公开失败，避免工具任务
  被重复执行。ACP adapter 在首个 `session/update` / 权限活动进入外部 sink
  前发内部 `delivery_committed`，Orchestrator 据此先持久化 cursor；prompt
  写入后的断线即使没有收到 update，也按结果不确定处理。
- 无状态 JSONL fallback 不受影响：仍用 dispatch 瞬间的完整 transcript 快照
  （最近 12 条，防并发串话）。

## 上下文生命周期（M7.2）

`history_limit` 与 cursor 是传输减量，不是 compaction。ADR-0020 在既有单写者与
no-replay 契约上增加可选、provider-neutral capability：

- adapter 只有显式实现 `context_snapshot` / `compact_context` 才可被压缩；通用层
  不按 ACP/RPC/app-server 或 agent 名猜测私有命令。首版仅 myagents 原生直接模型
  Host 获证，第三方 stateful adapter 保持 transport-managed；
- 自动压缩在同一 target delivery lock 内、真正 `session/prompt` / `turn/start` /
  model chat 之前执行；摘要请求不是用户 prompt，不得推进 cursor 或制造
  `delivery_committed`。拒绝、空摘要、断流、未知终态与取消保持旧 context；
- 成功摘要绑定当前 cursor 写入私有 `ContextCheckpoint`。timeline 不删除不改写；
  checkpoint 写失败使 room fail-closed，避免 live runtime 与恢复事实分叉；
- fresh runtime 可把旧摘要作为背景注入一次，并从 checkpoint boundary 后继续增量。
  若前一用户 prompt 已进入不确定终态，当前 no-replay cursor 优先，不能为了补齐
  checkpoint 后内容而重放；
- execution profile 或 HostBackend 切换不复用 checkpoint，也不借压缩绕过各 adapter
  的 fresh session、权限和回收契约。

因此 `/context` 对 ACP/RPC/app-server 只展示已证明的所有权状态；`/compact` 对未
声明 capability 的 transport 明确拒绝，不调用 `session/new` 或 reset 伪造压缩。

## 持久化与 session restore（Phase 2.5）

RoomStore（`storage/store.py`）把房间状态落盘到
`${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/<room_id>`：

- **timeline.jsonl**：append-only，记录单调 `seq`、speaker、text、UTC
  `created_at`、可选 `command_id`；文本/记录有明确大小上限；损坏记录、
  seq 回退、半初始化房间全部 fail loudly，绝不静默覆盖。
- **state.json**：schema version + 规范化 workdir + 每个 stateful agent
  的 cursor/session_id；同目录临时文件 + `os.replace` 原子写；agents
  entry 打开时全量校验（缺 cursor/session_id、负 cursor、空 session id
  都在构造阶段抛 `CorruptedStorageError`）。
- **seq cursor**：cursor 从 list 下标改为持久 timeline seq。`_messages_for`
  按 `m.seq > cursor` 选增量，cursor=0 才走 `history_limit` bootstrap；
  成功交付或提交后结果不确定的 no-replay 边界，都先 `set_agent_state`
  落盘再更新内存；明确未提交的失败保持旧 cursor。
- **checkpoint-before-prompt**：ACP 轮次走
  `stream_prepared(make_prompt, workdir, resume_session_id)`——prepare
  （start/load/new）与 prompt 在同一 writer lock 生命周期内。make_prompt
  在任何 event/prompt 前执行：`fresh and not restored`（新 session）选
  cursor=0，load 成功或复用活跃 session 保留 cursor；一次
  `set_agent_state(cursor, session_id)` 原子落盘后才构造 prompt。
  checkpoint 失败抛内部 `_CheckpointError` 穿透 dispatch（零 prompt，
  adapter 按契约 reset fresh session），绝不伪装成 agent 调用失败。
- **session restore**：重启后 `resume_session_id` 取自已持久化状态。
  load 成功（restored）保留 cursor 继续增量；仅标准 `-32002`
  Resource not found / `-32601` Method not found，或具体 adapter
  已获证的精确 session-not-found 映射可回退 session/new。agent 不声明
  loadSession 时可直接新建；其他 generic server/policy/quota/transport
  错误全部 fail-closed。fresh session 才将 cursor 归零有界 bootstrap
  并落盘实际 session id。
- **DSH hard gate**：DSH 不接受“不声明 load 就 new”的通用降级；initialize 必须
  同时声明 `loadSession=true` 与对象形状的 `sessionCapabilities.close`，否则在
  new/load/prompt 前 block。load 回放的历史通知在无认证 prepare 中直接丢弃，
  有认证时只进入 64 条有界队列，溢出关闭连接，历史不外泄为当前轮输出。
- **持久确认时序**：用户消息 append 成功后才发 `committed` 事件（TUI
  此时才显示用户文本）；agent 的 `done` 不再由 adapter 直接转发，只在
  最终回复落盘成功后的成功轮补发——append 失败无 done。
- **owner lease**：persistent Orchestrator 构造末尾 `acquire_owner()`
  （owner.lock，`flock(LOCK_EX|LOCK_NB)`，0600，写入 PID 供冲突提示）；
  冲突抛 `RoomBusyError`。`aclose()` 先置 closed 标志（新 dispatch 与
  排队 delivery 抛 `OrchestratorClosedError`），关闭 adapters 后在
  finally 释放 lease；不删除 owner.lock，stale 文件不妨碍下次获取。
  只读 RoomStore 辅助实例不获取 lease。

## 权限策略（Phase 2：进入 TUI）

client 声明 `fs/terminal` 能力为 false（不代理文件/终端）。
`session/request_permission` 的决策链：

1. **TUI 已挂载**（默认询问路径）：`main.py` 把异步决策回调注入所有 ACP
   adapter（`Orchestrator.set_permission_handler`），回调签名
   `async (agent_name, params) -> outcome`——通用多 agent runtime 里
   弹窗必须显示来源 agent（名字在 adapter 注入时绑定，client 层保持
   params-only）。弹窗显示工具标题和 agent 提供的 options，用户可选
   allow / reject / cancel；生产 TUI 的 `Esc` 直接取消当前 command，单次权限
   取消仍可使用弹窗按钮或 reject option。
2. **无权限处理器**（非 TUI / 测试 / 脚本调用）：一律 cancelled——
   安全拒绝是默认，不是配置缺失的意外。
3. **auto 放行**：只能显式 opt-in（`AcpClient(..., permission="auto")`），
   优先 allow_once。绝不隐式恢复。
4. **fail-closed 校验**：决策器返回值不可信——None、畸形 dict、缺
   optionId 的 selected、空 optionId、不属于本次 options 的 ID、决策器
   抛异常，一律按 cancelled 回应（`_validate_outcome`），绝不向 agent
   发无效 outcome。

生产 TUI 另提供显式 `/yolo` 会话级开关。它不修改 client/adapter 的 deny
默认值，而是只让当前 room 的 TUI 决策器选择本次 options 中的非空
`allow_once`；没有该选项仍 cancelled。再次输入关闭，状态只保存在当前进程，
不写 room state；切换会话时各 room 独立，退出后全部失效，
并且不改变 workflow `read_only`、runtime hard-deny 或只读 JSONL fallback。见
[ADR-0016](adr/0016-explicit-auto-approve-mode.md)。

JSON-RPC error object 的 `data` 是 provider 具体原因的重要载体。通用
`AcpClient` 保留 `code` / `message` 作为协议事实，同时只从 `data` 的固定
`error/details/message/reason/hint/description` 字段、最多三层中提取正文，
统一做凭据脱敏与 900 UTF-8 bytes 上限。上下文超限显示请求量、模型上限和
调整 Context Length 的建议；余额/额度不足提示充值或切换 agent。未知结构仍
回退到有界 `message`，不得把整个 data、traceback 或 secret 写进 timeline。

等待用户决策期间不阻塞 read loop（独立 task 应答），并暂停 adapter 的
agent inactivity timeout；权限结果发回后重新开始普通静默计时。OpenCode ACP
还通过 runtime permission policy 把默认偏宽的 unknown、edit、bash、task、
skill、network、MCP 与 external-directory 收口为 ask；read/search/lsp/todo
allow。其 workflow `read_only` profile 改为 unknown/risky=deny、只允许安全
读取；profile 切换关闭进程、禁止 load 并建立 fresh session，切回普通轮次恢复
ask。这样既隔离 `allow_always`，也避免 OpenCode 在 ask 被 cancelled 后直接
`end_turn` 且零正文。Qwen 普通 ACP 轮强制 approval `default`，避免继承 native
TUI 的 auto/yolo；只读轮用上游 plan mode 在 runtime 层阻断写入/有副作用命令，
profile 前后同样重建进程与 fresh session。CodeBuddy 先尝试 `session/new` 复用
CLI 既有登录态；只在明确的认证错误后发送标准 ACP `authenticate(methodId)`。
大型本地模型可能在系统提示、工具 schema 和长上下文的 prefill 期间不产生
transport event。ACP、Pi RPC 与 Codex app-server 等有状态 adapter 因此共享
300 秒普通静默看门狗；早期 plan/status 只重置计时，取消/close 仍可立即打断。
默认 method 是 `internal`，可由环境变量覆盖，但必须属于 server 当次公布的集合。私有
`_codebuddy.ai/authUrl` 通知只允许打开官方 HTTPS 域名，认证等待最多 300 秒，
失败时整条连接原子回收。

DSH 的普通/写 profile 只能由专用 host 在风险工具执行前产生标准 permission
request；只读 profile 必须在 runtime 把工具闭集收紧为 read/glob/grep，并阻断
write/shell/network/process/subagent/MCP/scoped/run-code/shadow。myagents 侧仍只接受
当次 options 内的选择，默认 deny；只读轮即使上层 handler 试图 allow 也 cancelled。
在专用 host 的守卫与插件顺序获得独立证据前，对应 profile 必须 block。

Pi 不使用 ACP `session/request_permission`。固定 extension 以
`MYAGENTS_PI_ATTEST_V1:` 完成启动 attestation，并以
`MYAGENTS_PI_PERMISSION_V1:` 把风险 wrapper 的 tool call 映射成同一个 TUI
权限 handler。普通/写 profile 激活七个唯一命名 wrapper，只读 profile 只激活
四个读取 wrapper并在 hook 内再次 hard-deny 风险工具；未知工具与非法路径在
extension 内执行前阻断。权限选项绑定
一次性 call nonce，仅支持 `allow_once` / `reject_once`。无 handler、超时、取消、
异常、未知/复用 nonce 或 attestation 不匹配全部拒绝。该 bridge 是应用层权限
边界，不提供 OS sandbox；用户批准 bash 后仍由本机进程权限执行。权限 UI 的
input 只携带最多 4096 UTF-8 bytes 的展示摘要，再由共享 PermissionScreen 脱敏；
完整 canonical args 只以 `argsHash` + tool/call nonce + one-time permit + 路径
快照绑定，避免大 write/edit 因展示上限被误拒绝。超限 bash 因无法安全隐藏尾部而
直接 block，不做截断审批。最终 stable JSON 仍必须不超过 4096 bytes；未知 schema
或无法安全截断的内容 fail-closed。

Pi 的 permission request 还受 adapter 侧 checkpoint gate 约束：即使 extension UI
request 与 prompt success frame 并发到达，也必须先把内部 `delivery_committed`
交给 Orchestrator，并等其同步持久化 no-replay cursor 后，才允许调用共享权限
handler、显示弹窗或返回 `allow_once`。profile 工具闭集不匹配的 request 在弹窗前
直接 cancelled。

Pi 0.84.3 在安装 RPC stdin reader 前会 await 初始 `session_start` handler，故
attestation 不能在 handler 内同步等待 UI select。bridge 必须立即返回并用后台 task
完成握手；session generation guard 保证旧 task 的迟到 ACK/异常不能改变新 session
的 ready 状态。第一条 prompt 仍由 adapter 侧 attestation gate 阻断，不能把
fire-and-forget 误解成先运行模型再补验权。

myagents 启动 Pi 时同时固定 `--offline` 与 `PI_OFFLINE=1`，避免 `grep/find` 在
缺少 `rg/fd` 时通过 stock tool manager 隐式下载、写盘或更新。Pi 预留但尚未落盘的
session path 由私有、fsync 的 reserved/materialized sidecar 精确绑定完整 checkpoint、
native id、workspace 与 profile；只有精确 reserved 且文件仍不存在时可保留 no-replay
cursor 并 fresh，materialized 后文件或 marker 缺失一律 fail-closed。

等待仍可取消——TUI 退出时所有挂起的权限 Future 按 cancelled 收尾
（`on_unmount` →
`_cancel_pending_permissions` → `orch.aclose()`，顺序不能反：aclose
等的锁可能被等权限的 prompt 持有），不留挂起 Future、`kimi acp`
／`opencode acp` 或 `pi --mode rpc` 子进程。权限后台 task 的异常由 done callback 消费（连接断开导致应答
发不出去时不会留下 "Task exception was never retrieved"）。

## 取消契约（Phase 1 建立，Phase 2 不变）

流被取消时：先发 `session/cancel`，再**等待**原 prompt 以 cancelled
结束（默认 10s 有限超时）；超时说明连接不可信，关闭并标记必须重建
（下轮 stream 重新 start + session/new）。adapter 的锁只在确认停止或
连接关闭后释放——下一轮 prompt 绝不与仍在执行的上一轮重叠。

通用 ACP 成功终局只接受 `stopReason=end_turn`。`max_tokens`、
`max_turn_requests`、`refusal`、`cancelled`、缺失或未知值都在
`delivery_committed`/no-replay cursor 已提交后抛确定性错误，不能 yield done。

Pi 对应动作是 `abort` 并等待 `agent_settled`；`agent_end` 后仍可能发生 retry、
compaction 或 continuation，不能提前释放 writer lock。abort/settle 超时则关闭整个
进程组。prompt 成功 response 前到达的事件先缓冲，成功后先发布
`delivery_committed`；prompt 已写入但无 response 或提交后断线均为结果不确定，
严格 no-replay，并作废可能仍在执行的连接。Pi 不跨协议重试。
Pi 必须看到最终 assistant `message_end`，且 stop reason 属于
`stop/length/toolUse/deferred` 才可成功；缺失/未知 terminal 或 provider error/abort，
以及权限拒绝造成的
`tool_execution_end.result.terminate=true` 也属于已提交失败，而不是成功的空回复。
自动重试后的最后一条 assistant 成功可覆盖中间 error。

普通 ACP 和 native model runtime 当前没有已验收的运行中插话原语，不能为
`Alt+↑` 建立第二 prompt/writer。Alt+↑ 只提升同 room FIFO 最早 queued command，
composer 草稿不参与。Pi 仅在原 prompt 的 durable `delivery_committed` 之后，由
同一 client 发送官方 `steer`；Codex app-server 同样只在 durable commit 之后，由
同一 client 向原 `threadId` / `expectedTurnId` 发送官方 `turn/steer`。两者都先写
`interjection_requested`；响应丢失记 uncertain，源 queued command 不得作为普通
命令重投。workflow 即使使用支持 native steer 的 stage 也继续受 ADR-0009 阶段边界约束。完整设计见
[ADR-0018](adr/0018-capability-bounded-runtime-interjection.md)。

不在等待人工权限且没有活跃工具时，prompt 连续 300 秒无 transport 事件或终止响应
会触发 inactivity cancel；
ACP 与 Pi RPC 在工具已创建且尚未进入终态时改用独立 15 分钟 watchdog，避免
工程子代理和长命令被普通分析阈值误杀；Codex app-server 当前仍使用普通预算。
由于 prompt 已经提交，
这不是安全重试点：
Orchestrator 必须先持久化 no-replay cursor，再记录调用失败。

## 生命周期（Phase 2 新增）

TUI 退出时 `Orchestrator.aclose()` 统一关闭所有支持 `aclose()` 的
adapter（`asyncio.gather`，一个关不掉不耽误其他）。close 与进行中的
prompt/session 初始化共用 adapter 的同一把锁，不竞态杀进程。
DSH 广告的活跃 session 在 profile reset 与 `aclose()` 时先有界
`session/close`，再统一回收进程组；close 失败不阻止最终回收。

## 可见状态（Phase 2 新增）

- TUI 启动行显示每个 agent 的传输协议：`@kimi(ACP+JSONL) @opencode(ACP+JSONL)
  @qwen(ACP) @codebuddy(ACP) @dsh(ACP) @pi(RPC) @codex(APP-SERVER)`。
- native session id 建立后通过 info 事件展示一次（每次建立一次，不刷屏）。Pi 的
  session info 只能在 `delivery_committed` 已被消费后显示；其可信图片每轮最多
  16 张、读取前合计最多 20 MiB。Pi attestation 只显示可操作状态，不泄漏 nonce
  或内部完整策略载荷。
- `tool_call` 保存脱敏后的 title/command；后续 `tool_call_update` 按
  `toolCallId` 继承上下文，只在 title/status/command/kind 的可见指纹变化时
  产出事件。完全相同的高频 `in_progress` 仍被视为协议活动，但不进入 TUI
  或持久日志；TUI 将同一 command 的阶段、heartbeat、工具与权限折叠为一张
  活动卡，命令详情默认隐藏并通过 `/details` 展开。
- 固定任务区显示 command 总状态、累计耗时及每个 agent 的阶段/终态；
  fan-out 中既有成功又有失败时显示“部分完成”，不抹掉成功 agent 的事实。

## 有界讨论（M5.1）

`/discuss` 不改变任何 agent wire protocol，也不让 agent 直接互发消息。它在
Orchestrator 内用普通代码执行 1–3 轮状态机：同轮不同 adapter 并发，跨轮等待
全部收尾，最后调用一次 moderator。整个讨论仍是一个 CommandBus command，
因此沿用同一个取消、事件、权限和失败终态。

每个 stateful 参与者继续使用原 delivery lock/cursor：第一轮成功后 cursor 只
推进到本轮 prompt 构造时的 timeline 末尾；其他参与者随后落盘的回复自然留给
下一轮增量。下一轮过滤自己的回复（原生 session 已有）并收到其他参与者发言，
不需要复制完整 transcript。参与者失败后退出后续轮次，不把 post-submit 不确定
结果当作可安全重试。完整工作流契约见
[ADR-0008](adr/0008-bounded-multi-agent-discussion.md)。

## 有界里程碑工作流（M5）

M5 不改变任何 wire protocol，也不向 agent 暴露自主派发能力。`/workflow`
由普通代码严格推进 review → 单 writer implement → 独立 verify；首次复核要求
修改时最多允许同一 writer repair 一次并 reverify，最后由 host 汇总，最大六次
模型调用。全部阶段仍属于一个 CommandBus command 和一个 `command_id`。

adapter interface 已在 `stream` / `stream_prepared` 两条投递路径增加通用
execution mode：review/verify 映射为 `read_only`，implement/repair 映射为
`workspace_write`。具体 sandbox、权限和 fallback 行为仍由各 adapter 在现有
seam 内实现，Orchestrator 不按 agent 名分支。ACP 从普通轮次进入 `read_only`
时关闭旧进程、禁止 load 旧 session 并建立隔离 session，避免继承历史
`allow_always`；OpenCode 同时切换到 runtime deny-all + 安全读取白名单，退出
只读 mode 时再重建并恢复普通 ask；Qwen 切换到 `plan`，退出时恢复强制
`default`；Pi 切换到四个读取 wrapper，退出时重建 fresh process/session 并恢复
七个 wrapper，写工具仍逐次 `allow_once`；DSH 切换到 `read-only` 专用 host
profile，退出时恢复 `workspace-write`，双向都建立 fresh process/session。
Kimi/OpenCode 的只读 JSONL fallback 不能承担写阶段，触发时
workflow 必须 blocked。

运行中 steering 只在下一阶段边界注入尚未开始的 assignment，不并发写当前
native session、不修改已提交 prompt，也不能换人、加轮或扩大权限。完整设计见
[ADR-0009](adr/0009-bounded-milestone-workflow-steering.md)。生产 `/workflow`、
execution mode 与 TUI/control/MCP steering 均已实现并纳入 Harness。

workflow 还要求从干净 Git 工作区捕获 baseline HEAD/branch 和完整工作区指纹，
read-only 阶段前后不得漂移，写阶段不得改变 HEAD/branch/index。Git 探测由注入的
transport adapter 执行；Orchestrator/workflow 不直接启动子进程。外部 writer
恰好在 implement/repair 窗口写入时无法自动归因，必须作为人工验收边界披露。

## 阶段计划

- [x] **Phase 1**：`acp/client.py` + `acp/adapter.py` + fake server 回归测试
  （initialize/new/list/load/prompt/update/cancel/权限默认 deny/auto opt-in/
  取消串行化/超时重建/close-during-prompt/initialize 失败回收）
- [x] **Phase 2**：通用 ACP runtime 接入统一 TUI（kimi 为首个验收 agent）：
  `AgentSpec` 注册表（transport = acp/acp+jsonl/jsonl）；ACP 增量上下文
  （history cursor）；权限请求弹到 TUI 让用户决策；TUI 退出统一
  `aclose()`；协议状态可见。JSONL 保留为 fallback。
- [x] **Phase 2.5**：共享 history 持久化（timeline/state + seq cursor）、
  ACP session 映射与重启恢复（load 保留 cursor / 回退 new 有界
  bootstrap、checkpoint-before-prompt）、房间单写者 owner lease、
  持久确认时序与 close 排队保护
- [x] **Phase 3**：内部 command bus + 受控 MCP 外部入口（已完成）：
  `control/command_bus.py` FIFO 单 worker（request_id 永久幂等、容量
  硬上限、close 兜底 cancelled）；`control/server.py` 私有 Unix 控制
  socket（0600、stale 验证后清理、活跃不抢占、稳定错误码）；
  `myagents_mcp.py` stdio MCP bridge（官方 SDK `mcp>=1.27,<2`，八个
  `myagents_*` 工具）只连运行中 TUI 的 socket，绝不实例化第二
  Orchestrator、不获取 lease、不绕过 TUI 权限。细节见
  [ADR-0001](adr/0001-persistent-room-command-bus-mcp.md)
- [x] **Phase 4**：Codex 官方 app-server worker 接入：长驻进程、thread/turn、
  流式 item、approval、interrupt、恢复和安全 JSONL fallback；ADR-0017 允许
  通过独立 read-only factory 将 Codex 显式选为 Host，但不复用 worker。详见
  [ADR-0003](adr/0003-codex-app-server-transport.md)与
  [ADR-0004](adr/0004-ephemeral-codex-host-threads.md)。Claude 尚未注册；
  后续按其可靠官方协议单独接入，不把厂商协议强行伪装成 ACP。
- [x] **Phase 4.14**：会话级 HostBackend 支持原生模型 runtime 与 adapter 声明的
  独立只读 agent Host；`AgentHostCapability` 让同一安全适配自动服务 worker/Host，
  无需在注册表重复 factory。首个 OpenAI-compatible provider 覆盖 LM Studio，保持
  session/cancel/timeout/no-replay 与零自动 fallback。见 ADR-0017。
- [x] **Phase 4.4**：Kimi hybrid transport：保持 ACP-first，仅在新连接
  prepare 失败时进入内置只读 JSONL profile；伪 checkpoint 下轮新建
  ACP session，prompt 拒绝与 post-submit 失败禁止重放。见 ADR-0006。
- [x] **Phase 4.5**：OpenCode hybrid transport：`opencode acp` 成为生产
  主路径，风险/未知工具统一 ask；prepare-only fallback 使用 `--pure`、
  隔离配置和 deny-all 只读 agent。见 ADR-0007。
- [x] **Phase 4.11**：Pi 原生 RPC-only：`pi --mode rpc` 由独立 client/adapter
  持有；唯一显式 permission bridge、唯一命名 wrapper 工具闭集、启动
  attestation、三 profile fresh session、逐次 `allow_once` 与 no-replay 纳入
  fake contract 和静态 R4 gate。见 ADR-0014。
- [x] **Phase 4.12**：DSH ACP-only 生命周期、permission、load/close/replay 上限、
  精确 identity、仅 end_turn 成功与零 fallback 已通过 fake/release contract；标准
  `@myagents/dsh-acp-host` bundle + stock `dsh --profile myagents` 已在临时
  `DSH_HOME` 完成核心真实恢复、逐次权限、read-only fresh session 与无残留验收。见
  ADR-0015。
- [x] **Phase 5.1**：`/discuss` 指定 2–3 个 worker、1–3 轮有界讨论，
  同轮 fan-out、跨轮增量上下文、失败者退出与终局 moderator。见 ADR-0008。
- [x] **Phase 5**：里程碑 review → 单 writer 修改 → 独立复核、最多一次
  repair/reverify、阶段边界 steering、Git fixed point 与 TUI 阶段状态已完成；
  真实 Kimi review/verify + Codex implementer 临时仓库探针通过。见 ADR-0009。
- [x] **Phase 7.2**：provider-neutral `ContextPolicy`、adapter capability、原生
  Host 自动/手动摘要、私有 checkpoint 与 fresh 恢复已完成；第三方 transport
  未获证前保持只读状态与明确拒绝。见 ADR-0020。

A2A 不在当前阶段；Streamable HTTP、远程认证同样不在 M3（M3 是单机
单用户 stdio 集成，见 ADR-0001 §3）。只有出现跨机器、跨组织 agent
协作需求时再评估。

## 风险与注意

- **真实 `kimi acp` 已端到端验证**（Phase 2 收尾时实测）：真实二进制 +
  Textual TUI 的无工具回合正常；真实 Bash 权限请求弹窗正常，实际 options
  为 allow_once / allow_always / reject_once，选择 allow_once 后工具
  执行结果正确；退出后无残留进程。
- **M3 真实 E2E 已通过**（2026-07-26）：
  `scripts/e2e-m3-real.py` 用真实 `kimi acp` + 独立 MCP stdio client
  完成两轮 TUI 生命周期，第二轮 `session/load` 复用同一 session id，
  timeline 无重复，退出后无 endpoint/socket/agent 残留。
- **M5 真实 E2E 已通过**（2026-08-09）：
  `scripts/e2e-m5-real.py` 在临时 Git repo 由 Kimi 只读 review/verify、Codex
  单 writer implement、host final；HEAD/branch/index 不变，最终 3 个 unittest
  通过；使用 `--verifier opencode` 的 Kimi review、Codex implement、OpenCode
  verify、host final 变体同样通过。OpenCode 1.18.15 同形状只读回复在 runtime
  hard deny 后连续 5/5 产出合法信封。真实模型探针不进入默认快速 gate。
- **仍未覆盖**：cancel 响应时延在真实二进制上的表现（10s 有限超时
  契约只经 fake server 验证）；长会话的内存/token 增长与 compaction
  后的 restore 行为。
- **kimi acp 启动开销**：长驻进程只需一次握手，后续 prompt 无进程启动成本，
  比 JSONL 模式更快。
- **JSONL 降级能力有意受限**：print mode 不能把写入权限交给
  TUI，因此 Kimi 自动 fallback 只提供 Read/Grep/Glob，OpenCode 只提供
  read/glob/grep/list；需要变更时应说明阻塞，不得伪装完成。
- **OpenCode 权限默认值**：上游默认允许大部分工具，生产 ACP 必须保留
  普通轮次 runtime ask policy，workflow 只读轮次必须保留 hard deny + 安全读取
  白名单；CLI 升级后用 `opencode debug agent build` 和真实 permission/workflow
  wire probe 复验规则顺序与零正文行为。
- **断线**：agent 进程 EOF 时所有 pending request 立即失败（带 stderr 尾段），
  由编排器的 `_run_one` 兜底成 error 事件。
- **图片能力漂移**：Kimi 不再声明 `promptCapabilities.image` 时退化为文本附件
  引用，不伪造协议能力；fake ACP contract 固定 text + image block 形状。
- **Pi 权限边界**：Pi 0.84.3 没有内置 permission prompt/sandbox；生产必须关闭
  自动发现且不激活原生命名 built-in tools，只信任已 attested 的项目 bridge/wrapper。任何
  attestation 漂移、raw RPC passthrough 或 JSON/JSONL fallback 都是阻断项。
  bridge 仍不是 OS sandbox；用户批准 bash 后的系统权限风险由人工决定。
- **DSH 标准路径已完成核心真实验收**（2026-08-26）：临时 `DSH_HOME` 经官方
  `plugin add` 安装标准 bundle，source launcher 的两个独立进程完成同 session
  `new/load` 两轮恢复；显式 CLI 解析完成真实逐次 reject、workspace-write 零副作用与
  read-only fresh session 阻断。release gate 的 151 项 host contract、完整 checkout
  不变性及退出无残留均通过。默认 gate 仍不调用真实 DSH；真实主动 cancel 时延、长
  session/compaction、压力终局与真实图片模型仍是人工边界。旧 custom `tsx` 证据继续
  superseded；DSH pre-release 版本/包布局变化后必须重跑 ADR-0015 的 fake、release 与
  真实清单。
- **增量补发的重复**：失败重试会把失败轮的增量再发一遍，agent 会在
  session 里看到重复的用户消息——可接受（丢上下文不可接受）。

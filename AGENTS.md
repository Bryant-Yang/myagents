# AGENTS.md — myagents Agent 协作契约

<!-- harness:controls-read-policy=on-demand -->

> 作者：Bryant Yang　最近更新：2026-08-28
>
> 本文是所有编码 agent 的项目级入场入口，也是唯一的协作规则源。详细工程契约见
> [`HARNESS.md`](HARNESS.md)，按任务选读规则见
> [`docs/workflow.md`](docs/workflow.md)。

## 1. 项目定位

`myagents` 是一个 Python + Textual 的本地多 agent 终端编排器。它以
hub-and-spoke 方式维护统一时间线，以 ACP 作为有状态 coding agent 的优先接入
协议；厂商只有其他可靠官方长连接时使用独立 adapter（Pi RPC、Codex
app-server），同时仅为已获证路径保留 JSONL 兼容回退。

使命：让不同 coding agent 在同一 TUI 中被安全、可恢复、可验证地驱动；协议差异
下沉到 adapter/runtime，路由、权限与生命周期由编排器统一管理。

## 2. 入场顺序

1. 先在 [`docs/workflow.md`](docs/workflow.md) 找到当前任务场景。
2. 阅读 [`HARNESS.md`](HARNESS.md) §8 对应红线。
3. 按场景读取 [`docs/SPEC.md`](docs/SPEC.md)、
   [`docs/acp-migration.md`](docs/acp-migration.md) 或其他具体文档。
4. 只有涉及软约束、人工边界或重复失败时，才读取
   [`docs/harness-controls.md`](docs/harness-controls.md)。

## 3. 不可破的硬约束

以下编号与 [`HARNESS.md`](HARNESS.md) §8、`scripts/check-redlines.sh`
严格对齐：

- **R1 权限默认 fail-closed**：生产代码不得显式构造
  `permission="auto"`；自动放行只能由明确授权的外部调用，或用户在当前
  TUI 会话显式输入 `/yolo` 后临时 opt-in。`/yolo` 不得持久化或突破
  read-only runtime/profile。
- **R2 通用层不得按 agent 名分支**：`orchestrator.py` 与通用 transport runtime
  不得出现 `if agent_name == "kimi"` 一类协议分支；差异必须进入
  `AgentSpec` 或具体 adapter。
- **R3 进程只能由 transport 层启动**：`main.py`、`orchestrator.py`、
  `host.py` 不得直接创建 shell/子进程。
- **R4 agent 的生产 transport 保持受约束**：Kimi/OpenCode 生产注册必须分别由
  `AcpKimiAdapter` / `AcpOpenCodeAdapter` 构造；JSONL 只能作 ACP
  prepare 失败前的只读 fallback，并使用各自项目内置工具白名单。
  禁止直接注册旧 JSONL adapter、放宽写入/命令工具或在 prompt
  提交后跨协议重放；OpenCode 普通轮次的未知及有副作用工具必须进入 ask，
  workflow 只读轮次必须在 runtime 层 deny 并只放行安全读取。Qwen Code
  必须由 `AcpQwenAdapter` 注册为 ACP-only：普通轮强制 approval `default`，
  workflow 只读轮强制 `plan` 并在 profile 切换时重建进程/session；在只读
  fallback 安全契约得到独立证据前不得自动降级到 headless JSONL。
  CodeBuddy 必须由 `AcpCodeBuddyAdapter` 注册为 ACP-only：普通轮固定
  `default`，workflow 只读轮固定 `dontAsk` + `Read,Glob,Grep` 工具闭集，
  profile 切换时重建进程/session；只允许可独立运行的官方 CLI，不得借用 App
  包内私有二进制，运行环境固定为已验收的中国区 `internal`。已有登录态直接
  复用，只有明确的 `Authentication required` 才按需认证；认证只能使用 server
  公布的 ACP method，登录 URL 必须是官方 HTTPS 地址且等待有界，在独立
  fallback 安全契约获证前不得自动降级。该注册只代表独立 CodeBuddy CLI；不得
  声称它是 WorkBuddy App，也不得读取或复用 App 的私有 runtime、连接器或授权。
  DeepSeek Harness（DSH）必须以
  `AgentSpec("dsh", "acp", AcpDshAdapter, ...)` 注册为 ACP-only：安装模式只接受
  `MYAGENTS_DSH_CLI` 指向或 PATH 解析出的官方 `dsh`，源码模式只用
  `MYAGENTS_DSH_SOURCE_ROOT` 定位已构建的官方 `apps/cli/lib/bin.js`；两者都固定启动
  stock `dsh --profile myagents`。产品专属 `@myagents/dsh-acp-host` 必须作为标准
  bundle 存在于 `dsh_acp/plugin`，声明 `dsh.bundle.patch=./cordis.patch.yml`；profile
  必须被动证明 exact bundle 顺序为 `@deepseek-ai/dsh-base` →
  `@myagents/dsh-acp-host`，且依赖解析后的插件 name/version/entry/patch 精确匹配。
  DSH checkout 是不可变依赖：不得复制产品源码到其中，也不得修改其 core、官方 ACP
  包、示例或测试；只能调用 DSH 已公开的 agent/AgentSetup/ToolRuntime/持久化 seam。
  readiness 只能被动读取官方 CLI、profile manifest、bundle manifest、解析后的插件
  版本与文件属性，禁止调用 pnpm/build/CLI/真实 agent；`MYAGENTS_DSH_SOURCE_ROOT`
  不能再承担产品 host 或环境注入职责。profile home、固定 product state、workspace、
  source、plugin 与配置输入不得重叠。普通与 workspace-write 轮通过
  `DSH_ACP_PROFILE=workspace-write` 固定执行安全 profile，只读轮固定
  `DSH_ACP_PROFILE=read-only`；跨 execution safety profile 必须重建
  进程/session；首轮前必须确认 load/close capability。DSH 默认 deny、无
  headless/SDK RPC/JSONL fallback，状态目录必须为绝对路径且遵循
  `XDG_STATE_HOME`。host/runtime/compatibility identity 必须精确匹配 checked-in contract；durable
  replay 必须有事件数与字节上限。只有 `end_turn` 是成功
  terminal，其他、缺失或未知 stop reason 均不得产生 done。
  Pi 必须以 `AgentSpec("pi", "rpc", PiRpcAdapter, ...)` 走官方
  `pi --mode rpc`，不得冒充 ACP 或回退一次性 JSON/JSONL。Pi 子进程必须关闭
  自动发现并强制 offline，不激活任何原生命名 built-in tool，只显式加载项目固定的 permission
  bridge，并只暴露
  `myagents_*` wrapper 工具闭集；第一条 prompt 前必须完成 nonce/profile/workspace/
  bridge/tool-set attestation。`DEFAULT`、`READ_ONLY`、`WORKSPACE_WRITE` 切换必须
  重建进程和新 session；写入与 shell 只允许逐次 `allow_once`，无 handler、畸形
  选择、profile 越界工具或 attestation 失败一律拒绝。permission handler 只能在
  no-replay cursor 已持久化后运行；只有明确成功闭集内的 assistant terminal 才能
  完成本轮，缺失/未知 terminal、error/abort 与权限终止工具必须记为失败。生产 client 不得暴露任意 RPC passthrough 或
  发送 raw RPC `type: "bash"`；Pi 没有跨协议 fallback，提交后严格 no-replay。
  运行中插话只允许把同 room FIFO 中最早的 queued command 提升到当前 running
  command；composer 未提交草稿不得参与。原 prompt 的 `delivery_committed` cursor
  已持久化后，才可通过同一 Pi client 发送官方 `steer`，或通过同一 Codex
  app-server client 向原 `threadId` / `expectedTurnId` 发送官方 `turn/steer`；意图
  必须先落 execution event，写后结果不确定不得重投或再执行原 queued command。
  其他 adapter 没有已验收 capability 时必须 fail-closed，workflow 不得借 native
  steer 绕过阶段边界。
- **R5 多智能体协作必须有界**：自然语言讨论与 `/discuss` 只允许 2–3 个
  已注册 worker、1–3 轮和一个终局 moderator，并复用同一确定性状态机。
  自然语言有序协作只允许 2–4 个串行步骤、至少
  两个 ready worker；host 只能在 ready/显式 mention 闭集内给出固定计划。
  两类调度均由普通代码推进，禁止 agent 自主递归派发、动态扩员、自动重试或
  形成无界对话。会话级自然语言角色只能绑定已固定的实际 agent，不得改变
  参与者、moderator、步骤/轮数、工具权限或 runtime。
- **R6 里程碑 workflow 必须固定且单写者**：`/workflow` 固定 review →
  implement → verify，最多一次同 writer repair/reverify 和一次 host final；
  review/verify/final 必须 read-only，steering 只允许在阶段边界按冻结上限追加，
  禁止递归派发、换角色、扩权限或无界修复。

## 4. 工作要求

- 改协议、权限、并发或进程生命周期前，先读
  [`docs/acp-migration.md`](docs/acp-migration.md)；改持久化、恢复或
  lease 前，先读
  [`docs/adr/0001-persistent-room-command-bus-mcp.md`](docs/adr/0001-persistent-room-command-bus-mcp.md)。
- 改 agent 可用性、安装探测或 setup 引导前，先读
  [`docs/adr/0012-agent-readiness-and-setup-ux.md`](docs/adr/0012-agent-readiness-and-setup-ux.md)；
  probe 只能被动读取环境/PATH/文件属性，不得安装、卸载或启动真实 agent。
- 新增 agent 时实现统一 `AgentAdapter`，在 `AGENT_SPECS` 注册；不要把
  name-specific 逻辑散进编排器。
- `HostAgent` 是 moderator/supervisor 产品角色；room 默认使用 ADR-0017 的
  myagents 原生无工具 model runtime，也可显式选择由
  `AgentSpec.host_factory/host_probe` 声明的 host-safe agent backend。model
  provider 只能在 `native_agent/` 内选择，agent Host 必须使用独立 adapter/
  session/writer 并强制 `READ_ONLY`；通用 Orchestrator 不感知具体 provider wire
  protocol，也不得按 agent 名分支。`/yolo` 不得突破 Host profile，未就绪选择
  必须阻断且不得自动 fallback。
- 权限处理器返回值必须绑定本次 `params.options` 校验；异常、空值或未知
  `optionId` 一律 cancelled。
- ACP session 同一时刻只有一个 writer；不要让独立 native TUI 与 ACP client
  并发写同一 session。
- Kimi/OpenCode hybrid transport 分别遵守
  [`ADR-0006`](docs/adr/0006-kimi-hybrid-transport-policy.md) 和
  [`ADR-0007`](docs/adr/0007-opencode-hybrid-transport-policy.md)；
  JSONL checkpoint 不是可恢复 ACP session，下一轮必须新建会话。
- Pi 原生 RPC 接入遵守
  [`ADR-0014`](docs/adr/0014-pi-rpc-permission-bridge.md)：唯一显式权限 bridge、
  wrapper tool 闭集、启动 attestation、三 profile 隔离和 raw RPC bash 禁令必须
  同时成立；应用层权限桥不宣称提供 OS sandbox。
- 运行中插话遵守
  [`ADR-0018`](docs/adr/0018-capability-bounded-runtime-interjection.md)：
  最早 queued command、唯一活动 delivery、显式 adapter capability、持久化先于
  协议写入和 no-replay 必须同时成立；`Esc` 取消不得破坏补全/modal/活动导航的
  优先关闭语义。
- 上下文治理遵守
  [`ADR-0020`](docs/adr/0020-capability-bounded-context-lifecycle.md)：完整 timeline
  永不因压缩改写；通用层只消费显式 capability，未获证 transport 必须拒绝；
  自动压缩只在安全阶段边界与用户 prompt 前执行，checkpoint 写失败使房间
  fail-closed，fresh 恢复不得跨 backend/profile 或越过 no-replay cursor。
- DSH ACP-only 接入遵守
  [`ADR-0015`](docs/adr/0015-dsh-acp-only-transport.md)：官方 profile 入口、标准
  myagents bundle、被动 readiness、两 execution safety profile、load/close hard gate、
  no-replay、状态目录与零 fallback 必须同时成立；
  fake contract 通过不能替代 DSH runtime 工具守卫与真实回收验收。
- 有界讨论遵守
  [`ADR-0008`](docs/adr/0008-bounded-multi-agent-discussion.md)：同轮并发、
  跨轮串行，一条 command 只有一条 user 记录；参与者失败不自动重试，最终
  moderator 不能掩盖失败终态；自然语言识别不得改变这些边界。
- 有序协作的可见计划遵守
  [`ADR-0019`](docs/adr/0019-first-class-collaboration-plan-projection.md)：
  `CollaborationPlan` 仍是唯一调度权威；版本化 plan event 只做有界、脱敏的
  步骤投影，不得反向驱动执行、解析状态文案或进入 agent history。
- 自然语言有序协作遵守
  [`ADR-0013`](docs/adr/0013-natural-language-sequential-collaboration.md)：
  不新增 `/task`，固定计划严格串行，后一步读取前序真实回复，失败/取消即停。
- 测试必须使用本地 fake server/fixture；普通自动化测试不得调用真实外部 agent。
- 只修改任务直接需要的文件，不顺手重构；保留用户已有改动。

## 5. 完成标准

提交或交付前运行：

```bash
bash scripts/check-harness.sh
```

任何失败都必须如实报告，不得删除测试、降低红线或绕过权限制造假绿。关键行为还需
满足 [`docs/SPEC.md`](docs/SPEC.md) 中的独立证据和人工验收边界。

仓库已初始化 Git。未经用户明确要求不得提交或推送；操作时遵循
[`docs/git-commit-conventions.md`](docs/git-commit-conventions.md)，暂存必须
显式列文件，禁止 `git add .` / `git add -A`。

## 6. 多工具入口

`AGENTS.md` 是唯一规则源。只有实际启用不原生读取本文的工具时，才添加对应的
`CLAUDE.md` / `GEMINI.md` 薄指针；指针不得复制规则。

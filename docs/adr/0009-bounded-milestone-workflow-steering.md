# ADR-0009：有界里程碑工作流与阶段边界 steering

- 状态：Accepted
- 日期：2026-08-09
- 里程碑：M5
- 作者：Bryant Yang

## 1. 背景

现有显式 mention 适合一次派发，`/discuss` 适合只读讨论，但用户仍需手工推动
“先 review、再让一个 agent 修改、再由另一个 agent 复核”的工程闭环。若让模型
自行决定下一阶段、换人或反复修复，会重新引入无界调用、并发写工作区、权限扩大
和 post-submit 重放风险。

M5 只解决单机、单房间内一个可观察、可取消的里程碑工作流。本文中的 runtime
steering 指用户在运行中补充任务约束；它与 `docs/harness-controls.md` 中记录重复
工程失败的 Harness Steering 不是同一概念。

## 2. 决策

### 2.1 显式命令与角色

```text
/workflow --reviewer @agent|@host --implementer @worker
          [--verifier @agent|@host] -- 任务目标
/steer -- 给当前活动 workflow 的补充指令
```

- `reviewer` 和 `implementer` 必填且各出现一次；`verifier` 缺省时等于
  `reviewer`。参数顺序不影响语义。
- `implementer` 必须是 `AGENT_SPECS` 中的 worker；`reviewer` / `verifier`
  可以是 worker 或 `host`。实现者不得兼任 review 或 verify；reviewer 与
  verifier 可以相同，也可以是两个不同 agent。
- 角色在 command 开始前固定，运行中不得动态换人、扩员或新增阶段。任务目标
  必须非空且不超过 3000 字符；非法输入在写入 timeline 前拒绝。
- `/workflow` 通过现有 TUI 输入或 `myagents_send_message` 提交，不新增第二个
  Orchestrator。TUI `/steer` 指向当前活动 workflow；外部入口使用显式
  `command_id` 调用新的 `command.steer` / `myagents_steer_command`。

### 2.2 Git fixed point 与工作区漂移

- workflow 当前只接受 Git 工作区。开始 command 前、写入 timeline 前，普通代码
  通过注入的 workspace inspector 要求：不存在 merge/rebase 等未完成操作，
  HEAD/branch 可解析，index、tracked worktree 与 untracked 集合全部干净；否则
  稳定拒绝并提示用户先自行收口。
- inspector 返回 repo root、启动 branch/ref、baseline HEAD OID 和工作区指纹。
  baseline 进入 execution metadata，并明确注入 reviewer/verifier；复核对象是
  相对该 OID 的完整 tracked diff 与新增 untracked 文件，而不是 agent 自述。
- reviewer、verifier、reverifier 和 final 前后工作区指纹必须完全一致；任何
  read-only 阶段漂移立即 failed。implement/repair 允许工作区内容变化，但阶段
  结束时 branch、HEAD 和 index 必须仍等于 baseline：禁止 commit、checkout、
  reset、merge/rebase 和 staging。满足后生成新的 candidate 指纹供下一复核阶段
  锁定。
- 指纹必须覆盖 HEAD/ref、index diff、tracked worktree diff，以及 untracked
  路径与内容摘要；采集失败、路径逃逸或超过实现的有界读取上限一律 blocked，
  不退化为只看 `git status`。
- myagents 只能保证自己不并发启动第二个 writer。外部编辑器或其他进程若恰好在
  implement/repair 窗口写入，无法可靠归因；这是明确的人工验收边界。用户在
  workflow 期间不得使用其他 writer，read-only 阶段的外部漂移会被指纹检测。

### 2.3 确定性状态机

一条 `/workflow` 是一个 CommandBus command，只写一条真实 user timeline：

```text
review → implement → verify
                    ├─ pass → host final
                    └─ changes_requested → repair → reverify → host final
```

- `review` 只分析当前目标、代码和证据，给实现者一份带稳定 finding id 的报告。
- `implement` 由唯一 writer 完成修改和必要验证。
- `verify` 由独立于 implementer 的角色复核目标、review findings、最终 diff 和
  验证结果。
- 首次 verify 为 `changes_requested` 时，只允许同一 implementer 修复一次，再由
  同一 verifier 复核一次；不再自动循环。最后由 `host` 单次汇总实际阶段、证据、
  未解决 finding 和验收结论。
- 普通代码固定阶段、角色、修复次数和终止条件；模型只负责语义 review、实现、
  复核与总结。内部阶段 assignment 不伪装成 user 消息、不递归 `dispatch`、
  不创建嵌套 CommandBus command。
- 最大模型调用数固定为 `review 1 + implement 1 + verify 1 + repair 1 +
  reverify 1 + host 1 = 6`。review/implement 提前失败时跳过无意义后续阶段，
  但非取消场景仍可让 host 汇总已有证据。

### 2.4 结构化阶段结果

review、implement、verify 和 repair 的回复都保留可读正文，并以最后一条非空行
提供严格结果信封：

```text
MYAGENTS_WORKFLOW {"stage":"verify","status":"pass","findings":[]}
```

- JSON object 必须且只能包含 `stage`、`status`、`findings` 三个字段，三者均
  必填；未知字段、字段缺失或类型错误均非法。结果前缀在整份回复中只能出现一次，
  且该行必须是最后一条非空行。
- `stage` 必须与当前阶段一致。
- review 状态只允许 `ready|blocked`；implement/repair 只允许
  `completed|blocked`；verify/reverify 只允许
  `pass|changes_requested|blocked`。
- 结果信封 UTF-8 编码不超过 4096 字节；`findings` 必须是数组，最多 32 个
  不重复 id，每个 id 最多 64 个字符并只允许字母、数字、点、下划线和连字符。
  空数组合法。实现者和 verifier 可以在正文解释语义，但普通代码只依据合法
  信封推进状态机。
- 缺失、重复、过长、非法 JSON、错误 stage 或未知 status 一律 fail-closed，
  不让 host 猜测流程状态。host 的最终总结不能把 verifier 的失败或
  `changes_requested` 改写成通过。

### 2.5 单一 writer 与 adapter execution mode

workflow 在现有 `AgentAdapter` seam 上增加通用 execution mode，并在普通
`stream` 与有状态 `stream_prepared` 两条投递路径显式传递：

- review、verify、reverify 和 final 使用 `read_only`；
- implement、repair 使用 `workspace_write`；
- 只有 implementer 以 writer 身份进入工作区，且两个写阶段严格串行。

execution mode 是 adapter interface 的通用语义，不由 Orchestrator 按 agent 名
分支。Codex adapter 把 `read_only` 映射到 app-server sandbox；ACP adapter 在
read-only 阶段取消所有权限升级，并保留具体 runtime 的 write/command/network
ask/deny 策略。为防普通轮次的 `allow_always` 或 session 级授权泄漏，ACP
`read_only` 只能复用同为只读的 session；从其他 mode 进入时关闭旧 ACP 进程、
禁止 load 旧 session 并新建隔离 session。无头 fallback 只能使用现有只读
profile。任何 adapter 不能证明 read-only 时，该角色阶段直接 blocked。

Kimi/OpenCode implement 阶段若因 ACP prepare 失败进入只读 JSONL fallback，
workflow 必须把阶段标为 blocked；只读分析回复不能伪装成已修改工作区。所有阶段
继续遵守 permission fail-closed、单 session 单 writer、delivery lock、cancel 和
post-submit no-replay 契约。

### 2.6 阶段边界 steering

- `/steer` 只接受当前正在 review、implement 或 repair，且后面仍有可验证阶段
  的 workflow。queued、verify/reverify/final 或 terminal 状态都拒绝，避免接受
  一条已经没有写阶段可以落实的指令。每条指令最多 1000 字符，每个 command
  最多 5 条且累计不超过 4000 字符。
- 已接受 steering 作为 `agent=user, kind=steering` 的 execution event 持久化，
  不写入共享 timeline、不推进 ACP cursor；workflow 将累计 steering 明确注入
  所有尚未开始的阶段 assignment。
- steering 只在阶段边界生效，不修改已经提交的 prompt、不打断当前 agent、
  不跨 session 并发写入。需要立即停止时仍使用 `Ctrl+X` / command cancel。
- steering 只能补充目标、验收标准或实现约束；不能更换角色、增加修复次数、
  跳过 verify、扩大 sandbox/权限、授权外部副作用或覆盖 no-replay。
- queued、verify/reverify/final、terminal、非 workflow 或超过限制的 steering
  返回稳定错误，不静默排队到下一条普通 command。

### 2.7 完成、失败、取消与恢复

- 只有最终 verifier/reverifier 返回 `pass`，host final 正常结束且所有必需阶段
  无 transport/持久化失败时，CommandBus 才标记 `completed`。
- `blocked`、最终 `changes_requested`、非法结果信封、只读 fallback 承担写阶段、
  任一 adapter/host 失败都进入 `DispatchOutcome.failures`，command 为
  `failed`；已有成功阶段和证据继续保留。
- 参与阶段发生 post-submit 不确定失败时不自动重试或换 agent；沿用现有
  no-replay cursor 后收口。取消当前阶段即取消整个 workflow，不再 repair、
  reverify 或 final。
- 当前 CommandBus 不恢复进程重启前的 active command；workflow 同样在重启后
  显示为上次中断。timeline、execution events 和已接受 steering 只用于审计，
  不自动续跑或重放写阶段。

### 2.8 深模块与 seam

新增 `workflow.py` 作为深模块：内部拥有命令解析、阶段状态机、结果信封校验、
steering 限界和阶段 prompt；调用方只使用小 interface：创建/运行一个固定
`WorkflowRequest`，以及向活动实例提交一条 steering。

Orchestrator 只负责把 `/workflow` 委托给该模块、提供复用现有 `_run_one` 投递
能力的 stage runner，并按 `command_id` 暂存活动实例；它不展开角色条件或解析
结果信封。CommandBus 增加 steer 转发和状态校验，control socket/MCP/TUI 只做
同一 interface 的 transport adapter，不复制工作流逻辑。

Git 状态与指纹通过 `WorkspaceInspector` interface 注入 workflow；生产使用位于
transport 层的 Git adapter，测试使用内存 fake。`workflow.py`、Orchestrator、
CommandBus 和 TUI 不直接启动 `git` 子进程，继续满足 R3。

## 3. 不选择的方案

- **让 host 自由选择角色或决定是否继续修复**：成本和写入次数不可预测，无法
  在提交前证明谁会修改工作区。
- **reviewer 与 implementer 并发工作**：review 结果无法约束实现，并会破坏唯一
  writer 和可复核的 fixed point。
- **把 steering 当成一条普通排队消息**：CommandBus FIFO 会在 workflow 完成后
  才执行，已经失去运行中指导意义。
- **向正在执行的 ACP session 直接插话**：会产生第二 writer 或并发 prompt，
  破坏 session、取消和 no-replay 语义。
- **用 host 自然语言判断 pass/fail**：无法稳定驱动普通代码状态机，也可能把
  verifier 的失败洗成完成。
- **无限 review/repair 循环**：无法限制成本，重复写阶段也扩大副作用和重放风险。

## 4. 验收

1. parser 在 timeline 前拒绝缺角色、未知/重复参数、实现者兼任 review/verify、
   超长/空目标、非 Git 或脏工作区；非精确 `/workflow` 文本不误判。
2. fake stage runner 证明严格的 review → implement → verify 顺序、最多一次
   repair/reverify、唯一 user timeline、同一 command id 和最大六次调用。
3. 结果信封缺失/非法、review/implement blocked、最终 verify 非 pass、host 失败
   都使 command failed；host 不能覆盖 verifier 终态。
4. adapter contract 证明 read-only 阶段无法写文件或批准权限，implement/repair
   才能写；Kimi/OpenCode 只读 fallback 承担 implement 时确定性 blocked。
5. workspace inspector fake 证明 baseline/final diff 固定、read-only 漂移失败、
   implement/repair 后 HEAD/branch/index 漂移失败，candidate 指纹进入 verifier。
6. steering 在阶段边界生效、持久化为 execution event 且不进入 timeline；限额、
   太晚、非 workflow、改角色/权限等反例稳定拒绝。
7. cancel 在每个阶段都不进入后续阶段；post-submit 不确定失败零自动重试；关闭后
   无 adapter、权限 Future、socket 或 workflow task 残留。
8. TUI `/workflow`、`/steer` 帮助与 control/MCP steer 使用同一个 CommandBus/
   Orchestrator owner；外部入口不创建第二 Orchestrator、不绕过 TUI 权限。
9. 自动化全部使用 fake adapter/fixture。实现完成后另做一次授权真实工作区验收，
   固定 reviewer/implementer/verifier，核对 diff、阶段事件、最终 verdict 和退出
   清理；真实模型不进入默认 gate。

## 5. 后果与实现顺序

用户能用一条命令安排可插话、可观察、失败诚实的工程闭环，同时始终只有一个
writer。代价是 adapter interface 需要 execution mode，CommandBus/control/MCP
需要一个 steer 动作，TUI 需要活动 workflow 状态与本地命令。

实现顺序固定为：workspace inspector + `workflow.py` 纯 parser/状态机及 fake
contract → adapter execution mode 与负向写入测试 → Orchestrator/CommandBus
集成 → TUI/control/MCP steering → Harness 红线与真实验收。ADR Accepted 只表示
语义冻结；README/SPEC 只有在上述证据全部完成后才能把 M5 标记为完成，当前
实现与验收状态记录如下。

## 6. 实现与验收状态

2026-08-09 已按上述顺序完成生产实现。`tests/test_workflow.py`、adapter hybrid/
app-server contracts、CommandBus/control/MCP/TUI tests 覆盖 fixed point、execution
mode、严格信封、steering 原子提交、失败聚合、六阶段取消与进程回收；完整
`bash scripts/check-harness.sh` 通过。

授权真实探针 `scripts/e2e-m5-real.py` 由 Kimi 执行只读 review/verify、Codex
作为唯一 writer、host 最终汇总；HEAD/branch/index 不变，最终 3 个 unittest
通过。README/SPEC 因此可将 M5 标记为完成。

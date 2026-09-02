# myagents Harness Controls

<!-- harness:control-map=minimum -->
<!-- harness:behaviour-evidence=spec-reference-only -->
<!-- harness:steering-trigger=min-occurrences-2 -->

> Framework version：2.1.0
> 项目实例版本：2026-07-26（首次 Git 提交前）

本文把重要目标映射为 Guide、Sensor/人工确认、失败处置和 Owner。关键 Behaviour
证据只在 [`SPEC.md`](SPEC.md) 维护，本文只引用。

## 1. 覆盖边界

| 调节目标 | 当前覆盖 | 证据索引 | 明确不保证 |
| --- | --- | --- | --- |
| Maintainability | 部分：项目入口、场景索引、文档引用、Python 语法和测试 gate | `AGENTS.md`、`workflow.md`、`scripts/check-harness.sh` | 没有 formatter、lint、静态类型检查和复杂度阈值 |
| Architecture Fitness | 较强：transport 边界、ACP-first/Pi RPC-only、DSH ACP-only 生命周期 gate、权限默认值、Pi bridge attestation、会话角色边界、有界讨论、单写者 workflow 与 remote 单 owner 有确定性检查 | HARNESS R1–R7、`test_session_roles.py`、`test_phase2.py`、`test_dsh_acp.py`、Pi RPC tests、`test_discussion.py`、`test_workflow.py`、`test_runtime_daemon.py`、`test_remote_control.py` | 不证明新抽象必要，也不覆盖生产性能或 OS sandbox |
| Behaviour | 关键 Phase 2 + M2.5（持久化/恢复/lease）+ M3（command bus/控制 socket/MCP stdio）路径有 fake contract tests 与真实 Kimi E2E 双证据 | [`SPEC.md#关键行为用例`](SPEC.md#关键行为用例) | 不保证所有 CLI 版本、长会话 compaction 或真实 cancel 时延 |

## 2. Control Map

| ID | 目标 | Guide（行动前） | Sensor / 人工确认（行动后） | 失败处置 | Owner |
| --- | --- | --- | --- | --- | --- |
| C1 | 通用 agent 接入边界 | HARNESS §3；`acp-migration.md` 架构 | R2–R4；`test_phase2.py` AgentSpec/并发用例 | 阻断并把差异下沉至 adapter/spec | Bryant Yang |
| C2 | 权限 fail-closed 且人工等待不误超时 | HARNESS §4.2；`acp-migration.md` 权限策略；ADR-0014 | R1；ACP/Phase 2 权限测试；`test_acp.py` permission-wait/inactivity；Pi adapter/bridge 权限请求绑定；SPEC UC-PERM-001 | 阻断；恢复 deny/合法 option 或 Pi call nonce 校验与人工等待暂停计时后复验 | Bryant Yang |
| C3 | 增量上下文不重发、不乱序（seq cursor） | HARNESS §4.1、§4.4；`acp-migration.md` 增量契约与持久化契约 | `test_phase2.py` cursor/bootstrap/delivery-lock 用例；`test_m25.py` seq 增量与 timeout no-replay 用例 | 区分明确未提交的可重试失败与提交后不确定的 no-replay；修复后重跑并发回归 | Bryant Yang |
| C4 | 取消与退出不留进程 | HARNESS §4.3；`acp-migration.md` 取消/生命周期 | basic/ACP/Phase 2 与 Pi RPC abort/close 回收测试；SPEC UC-LIFE-001 人工边界 | 阻断交付；清理进程并定位锁/进程组问题 | Bryant Yang |
| C5 | Harness 入口与引用不漂移 | `AGENTS.md`；`workflow.md` | `scripts/check-harness.sh` 文档/marker/link Sensor | 修正文档或引用；不得复制多份规则 | Bryant Yang |
| C6 | 关键行为证据可信 | `SPEC.md` | fake fixture + 既有 tests + 授权的真实 E2E/人工验收 | 报告证据缺口，不以新生成测试代替验收 | Bryant Yang |
| C7 | 持久化、多会话 runtime、session restore 与单写者 lease | HARNESS §4.4；`acp-migration.md`；ADR-0001、ADR-0005、ADR-0010 | storage/M2.5 与 `test_session_catalog.py`、`test_session_manager.py`、`test_session_tui.py`；SPEC UC-ROOM-001/UC-SESSION-001/UC-ACP-002 | fail loudly 不假提交；本地命令不入 timeline；事件/权限按 room_id 归属；只回收合格的后台空闲 runtime | Bryant Yang |
| C8 | 外部入口保持单写者、权限不绕过且失败终态真实 | HARNESS §4.5–4.6；ADR-0001 §2.4–2.6 | `test_m3_bus.py`（含 fan-out worker failure）、`test_m3_control.py`、`test_m3_mcp.py`；SPEC UC-CTRL-001/UC-CTRL-002 | 阻断；bridge 不得创建 Orchestrator/获取 lease/绕过 TUI 权限；worker 失败不得报 completed；修复后重跑 M3 回归 | Bryant Yang |
| C9 | 执行状态有界、可读且不刷屏 | HARNESS §4.6；ADR-0002；`acp-migration.md` 可见状态 | `test_acp.py` tool-spam/long-tool watchdog、`test_m3_bus.py` 防御性去重、`test_basic.py` 工具折叠、`test_tui_status.py` 分 agent 状态；SPEC UC-OBS-001 | 阻断交付；在 adapter/bus/UI 正确边界恢复状态迁移、去重和索引清理，不删除 append-only 历史 | Bryant Yang |
| C10 | 图片附件私有、有界且不污染工作区 | HARNESS §4.8；SPEC UC-IMAGE-001 | `test_clipboard_image.py` 权限/格式/大小/草稿 fixture；真实截图由用户人工验收 | 阻断交付；删除不完整附件，恢复 0700/0600、20 MiB 和不自动提交边界 | Bryant Yang |
| C11 | ACP hybrid 降级不绕过权限、不跨协议重放；Qwen 保持 ACP-only + default/plan profile | ADR-0006/0007/0009；HARNESS §4.2–4.3；SPEC UC-HYBRID-001/002 与 UC-ACP-003 | R4 registry/profile/policy gate；Kimi/OpenCode hybrid tests；Qwen command-profile/fresh-session contract；受限真实临时目录探针 | 阻断交付；恢复具体 ACP adapter、权限 policy、已获证只读 profile 和 prompt 前唯一 fallback 点；Qwen 不得继承 auto/yolo 或静默接入未验证 JSONL | Bryant Yang |
| C12 | 指定成员讨论有界、跨轮上下文正确且失败不假绿 | ADR-0008；HARNESS §4.1；SPEC UC-DISCUSS-001 | R5 bounds/AST gate；`test_discussion.py` parser/并发/失败 contract；授权真实 MCP 回放 | 阻断；恢复 2–3 人、1–3 轮、非递归状态机和失败汇总后复验 | Bryant Yang |
| C13 | 里程碑 workflow 保持 Git fixed point、单 writer、固定复核与阶段边界 steering | ADR-0009；SPEC UC-WORKFLOW-001 | R6 bounds/mode/AST gate；`test_workflow.py`；hybrid/app-server/control/MCP/TUI contract；授权真实验收 | 阻断；恢复 fixed point、read-only 复核、一次 repair 和 steering 上限后复验 | Bryant Yang |
| C14 | 自然语言角色保持会话隔离且不改变编排安全边界 | ADR-0011；HARNESS §4.1；SPEC UC-ROLE-001 | `test_session_roles.py`；`test_discussion.py`；`test_tui_completion.py`；TUI activity/status tests | 阻断；恢复固定 target 闭集、room 级原子状态与 assignment-only 注入；查看/清空不得进入 timeline 或跨运行中 command 边界，确认不改权限/runtime/讨论边界/workflow | Bryant Yang |
| C15 | Pi RPC 只通过已 attested 的唯一权限 bridge 与 wrapper 闭集执行 | ADR-0014；HARNESS §3、§4.2–4.3；SPEC UC-RPC-001 | R4 registry/bridge/profile/raw-bash gate；`test_pi_rpc_client.py`、`test_pi_adapter.py`、`test_pi_permission_bridge.py` + fake server；授权临时目录反例 | 阻断；恢复 RPC-only、隔离 flags、bridge/hash/nonce/tool source 精确核验、三 profile fresh session 与逐次 allow_once；不得用 fallback、prompt-only 或 raw RPC command 绕过 | Bryant Yang |
| C16 | DSH 只通过 stock `dsh --profile myagents` 加载 myagents 标准 bundle，以完整 stateful 生命周期和两 execution safety profile 执行；stock DSH 保持不可变 | ADR-0015；HARNESS §3、§4.2–4.3；SPEC UC-ACP-005 | R4 registry/official-launcher/profile/exact-bundle/name-version/entry+patch SHA/capability/state/no-fallback gate；临时 `DSH_HOME` 的 `test_dsh_acp.py` + fake server；`scripts/check-dsh-plugin.sh` 校验 pinned source/runtime contract 且证明 DSH HEAD/Git/完整文件树 invariant；2026-08-26 核心真实 session/load、reject 与 read-only 清单已执行，主动 cancel 时延、长会话/压力与真实图片仍属人工边界 | 阻断；恢复标准 bundle canonical ownership、stock DSH 零改动、被动 profile/bundle 解析、load/close hard gate、绝对状态目录、execution profile fresh session、仅 end_turn 成功与零 fallback；缺 runtime 工具守卫证据或需 DSH 补丁时不得宣称生产安全 | Bryant Yang |
| C17 | 运行中插话只提升 FIFO 最早 queued command、只走显式已验收 capability，且持久化先于协议写入 | ADR-0018；ADR-0009；SPEC UC-INTERJECT-001 | `test_m3_bus.py`、`test_tui_completion.py`、`test_phase2.py`、Pi RPC 与 Codex app-server client/adapter fake contract；R2/R4/R6 | 阻断；恢复队首选择、其余 FIFO、唯一活动 delivery、能力检查、Pi/Codex commit gate、requested no-replay event、uncertain 源命令 terminal 和不支持 transport 的 fail-closed 行为 | Bryant Yang |
| C18 | daemon 是唯一 room owner；attach/remote 不创建 owner 且远程权限 deny-only | ADR-0021；HARNESS §4.11；SPEC UC-DAEMON-001 | R7 client-layer/route/bind gate；`test_runtime_daemon.py`；`test_remote_control.py`；`test_m3_control.py` | 阻断；移除客户端 owner/import/passthrough，恢复 loopback + Bearer + Host allowlist、精确 option 与 deny-only 权限后复验 | Bryant Yang |

## 3. 约束等级

| ID | 等级 | 判据 | 处置 |
| --- | --- | --- | --- |
| R1–R7 | Deterministic Gate | AST/注册表/有界常量检查低误报且能定位文件 | 违反必拦，修复后复验 |
| S1 | Inferential Review Criterion | 新抽象、跨层职责、重复逻辑需要语义判断 | review 提供证据与替代方案，不假装机械事实 |
| S2 | Inferential Review Criterion | fake server 是否仍代表真实 ACP/Pi RPC 边界 | 比较真实 wire/options/extension UI；必要时更新 fixture |
| A1 | Human Acceptance Decision | 真实工具调用、auto 权限和外部系统写入风险 | 只有用户明确授权才执行 |
| A2 | Human Acceptance Decision | 公网账户、多用户 ACL、A2A/Streamable HTTP MCP；M8 只冻结 loopback remote companion | Owner 决定后再冻结协议 |

## 4. Behaviour Evidence 引用

| 用例 / 风险 | SPEC 证据位置 | Control |
| --- | --- | --- |
| 显式路由与 fan-out | [`SPEC.md#uc-route-001-显式路由与并发扇出`](SPEC.md#uc-route-001-显式路由与并发扇出) | C1 |
| 会话级自然语言角色 | [`SPEC.md#uc-role-001-会话级自然语言角色`](SPEC.md#uc-role-001-会话级自然语言角色) | C1、C3、C7、C14 |
| 指定成员有界讨论 | [`SPEC.md#uc-discuss-001-指定成员的有界多智能体讨论`](SPEC.md#uc-discuss-001-指定成员的有界多智能体讨论) | C12、C3、C8 |
| ACP 增量上下文 | [`SPEC.md#uc-acp-001-有状态增量上下文`](SPEC.md#uc-acp-001-有状态增量上下文) | C1、C3 |
| Kimi hybrid 受限降级 | [`SPEC.md#uc-hybrid-001-kimi-acp-first-受限降级`](SPEC.md#uc-hybrid-001-kimi-acp-first-受限降级) | C1、C2、C3、C11 |
| OpenCode hybrid 权限与受限降级 | [`SPEC.md#uc-hybrid-002-opencode-acp-first-权限收口与受限降级`](SPEC.md#uc-hybrid-002-opencode-acp-first-权限收口与受限降级) | C1、C2、C3、C11 |
| Qwen Code ACP-only 接入 | [`SPEC.md#uc-acp-003-qwen-code-acp-only-接入`](SPEC.md#uc-acp-003-qwen-code-acp-only-接入) | C1、C2、C3、C11 |
| DSH ACP-only 接入 | [`SPEC.md#uc-acp-005-deepseek-harness-dsh-acp-only-接入`](SPEC.md#uc-acp-005-deepseek-harness-dsh-acp-only-接入) | C1、C2、C3、C4、C6、C7、C16 |
| Pi 原生 RPC 权限桥接入 | [`SPEC.md#uc-rpc-001-pi-原生-rpc-权限桥接入`](SPEC.md#uc-rpc-001-pi-原生-rpc-权限桥接入) | C1、C2、C3、C4、C6、C15 |
| 权限决策 | [`SPEC.md#uc-perm-001-权限请求与选择`](SPEC.md#uc-perm-001-权限请求与选择) | C2、C6 |
| 生命周期 | [`SPEC.md#uc-life-001-取消与退出回收`](SPEC.md#uc-life-001-取消与退出回收) | C4、C6 |
| 持久房间与重启恢复 | [`SPEC.md#uc-room-001-持久房间与重启恢复`](SPEC.md#uc-room-001-持久房间与重启恢复) | C7、C6 |
| 同一项目独立会话 | [`SPEC.md#uc-session-001-同一项目独立会话`](SPEC.md#uc-session-001-同一项目独立会话) | C7、C4、C6 |
| ACP session 恢复 | [`SPEC.md#uc-acp-002-acp-session-恢复与原子-checkpoint`](SPEC.md#uc-acp-002-acp-session-恢复与原子-checkpoint) | C7、C3、C6 |
| 外部命令注入（bus + MCP） | [`SPEC.md#uc-ctrl-001-外部命令注入command-bus--mcp-stdio`](SPEC.md#uc-ctrl-001-外部命令注入command-bus--mcp-stdio) | C8、C6 |
| 控制 socket 安全与生命周期 | [`SPEC.md#uc-ctrl-002-控制-socket-安全与生命周期`](SPEC.md#uc-ctrl-002-控制-socket-安全与生命周期) | C8、C4 |
| 有界执行可见性 | [`SPEC.md#uc-obs-001-执行进度权限上下文与重启证据`](SPEC.md#uc-obs-001-执行进度权限上下文与重启证据) | C9、C6 |
| 运行中插话与 Esc 取消 | [`SPEC.md#uc-interject-001-能力受限的运行中插话`](SPEC.md#uc-interject-001-能力受限的运行中插话) | C4、C6、C15、C17 |
| 后台继续、重新附着与远程伴侣 | [`SPEC.md#uc-daemon-001-后台继续重新附着与远程伴侣`](SPEC.md#uc-daemon-001-后台继续重新附着与远程伴侣) | C4、C7、C8、C18 |

## 5. 反馈生命周期

| 阶段 | 必跑 Sensor | 失败后 |
| --- | --- | --- |
| Agent 本地循环 | `check-redlines.sh` + 相关 test script | 当前 agent 自修并复验 |
| 完整交付 | `check-harness.sh`（含 storage/M2.5/M3 回归） | 阻断“完成”声明 |
| 人工 Review | SPEC 证据边界、真实权限/协议风险；长会话 compaction 与真实 cancel 时延仍是证据缺口 | 修改或由 Owner 明确接受 |
| 真实 agent 验收 | `scripts/e2e-m3-real.py`、ADR-0006/0007 临时目录探针、ADR-0008 真实讨论回放、ADR-0014 Pi 权限反例与 ADR-0015 临时 `DSH_HOME` 官方 CLI/profile/bundle/lifecycle 清单；真实模型不进默认快速 gate；结束后检查进程 | 立即停止外部写入并报告 |

## 6. Steering

同类 Harness 失败至少出现两次或形成趋势证据后才建 Steering 记录。当前首次接入
没有达到触发条件，不建立空台账。

## 7. Baseline 与模板升级

- **Legacy baseline**：R1–R7 当前均为零命中，不需要债务 baseline。生成方式是
  `bash scripts/check-redlines.sh`；若未来接入时已有历史债，必须先保存稳定、
  去行号的命中集合，再用 `comm -13` 只拦新增。
- **模板来源**：Harness Framework `2.1.0`，来源 commit
  `b3b8fd47ebc49b57abdc365f327688b33970d54c`。
- **本地差异**：小型 Python 项目；不引入重型 CI/commitlint/lint 骨架；使用 AST
  Sensor 检查项目特有协议和进程边界；真实 Behaviour 证据记录 Phase 2 E2E。
- **升级策略**：人工比较上游模板，只合并适用的方法变化；红线变化必须重新做
  负向探针。

# myagents 工作流

> 作者：Bryant Yang　最近更新：2026-08-27
>
> 每次任务先在本表定位场景，再按需加载文档。顶层契约是
> [`../HARNESS.md`](../HARNESS.md)。

## 1. 典型场景

| # | 场景 | 先读哪份 | 必守红线 | 产出落点 | DoD |
| --- | --- | --- | --- | --- | --- |
| 1 | 改路由、history、fan-out 或有界讨论 | [`SPEC.md`](SPEC.md) 路由/讨论/增量用例；[`adr/0008-bounded-multi-agent-discussion.md`](adr/0008-bounded-multi-agent-discussion.md)；`../HARNESS.md` §4.1 | R2、R3、R5 | `discussion.py`、`orchestrator.py`、`host.py`、对应测试 | 快照与并发顺序测试通过；讨论有界、不递归；无 name-specific 协议分支 |
| 2 | 改 ACP/RPC、权限、取消、session 或 hybrid fallback | [`acp-migration.md`](acp-migration.md)、[`adr/0006-kimi-hybrid-transport-policy.md`](adr/0006-kimi-hybrid-transport-policy.md)、[`adr/0007-opencode-hybrid-transport-policy.md`](adr/0007-opencode-hybrid-transport-policy.md)、[`adr/0014-pi-rpc-permission-bridge.md`](adr/0014-pi-rpc-permission-bridge.md)、[`adr/0015-dsh-acp-only-transport.md`](adr/0015-dsh-acp-only-transport.md)、[`adr/0016-explicit-auto-approve-mode.md`](adr/0016-explicit-auto-approve-mode.md)；`../HARNESS.md` §4.2–4.3 | R1–R4 | `acp/` / `pi_rpc/`、具体 adapter/profile/bridge、fake server、protocol tests 与文档 | fail-closed、attestation、串行化、no-replay、回收测试通过；高风险变化有真实/人工验收计划 |
| 3 | 新增或迁移 agent | `../HARNESS.md` §1–§3；`acp-migration.md`；有独立权限模型时读取对应 ADR | R2–R4 | 具体 adapter + `AGENT_SPECS` + tests + README | adapter 接口统一；transport 状态可见；fallback 只按获证契约开放 |
| 4 | 改 TUI 或权限交互 | [`SPEC.md`](SPEC.md) 权限用例；[`adr/0016-explicit-auto-approve-mode.md`](adr/0016-explicit-auto-approve-mode.md)；`../HARNESS.md` §4.2–4.3 | R1、R3 | `main.py` + Textual pilot tests | UI 不阻塞；退出无 Future/进程残留；来源 agent 与当前权限模式可见 |
| 5 | 只做 review / 文档 / Harness | 本文件；相关契约；必要时 [`harness-controls.md`](harness-controls.md) | 所有受影响红线 | 对应文档、Sensor 或 review 结论 | 引用无悬空；红线 gate 与相关测试通过 |
| 6 | 改持久化、恢复、会话身份/切换、lease、command bus、可观测性或 MCP 入口 | [`adr/0001-persistent-room-command-bus-mcp.md`](adr/0001-persistent-room-command-bus-mcp.md)、[`adr/0002-durable-execution-observability.md`](adr/0002-durable-execution-observability.md)、[`adr/0005-project-conversation-sessions.md`](adr/0005-project-conversation-sessions.md)、[`adr/0010-multi-session-tui-management.md`](adr/0010-multi-session-tui-management.md)；[`SPEC.md`](SPEC.md) 房间/恢复/会话/控制/可观测用例 | R1–R4、单写者、执行事件不进 history | `storage/`、`control/`、`session_catalog.py`、`session_manager.py`、ACP adapter、MCP bridge、TUI、对应测试 | storage/M2.5/M3 与 session catalog/manager/TUI 测试通过；会话隔离且 default 兼容；后台事件/权限按 room_id 归属；取消不杀其他会话；MCP 不创建第二 Orchestrator、不获取 lease、不绕过 TUI 权限 |
| 7 | 改 Codex app-server、thread、approval 或 fallback | [`adr/0003-codex-app-server-transport.md`](adr/0003-codex-app-server-transport.md)；[`adr/0004-ephemeral-codex-host-threads.md`](adr/0004-ephemeral-codex-host-threads.md) 仅作历史/adapter 能力；[`SPEC.md`](SPEC.md) Codex 用例 | R1–R4、默认配置继承、已发送 `turn/start` 不重放 | `codex_app_server/`、fake server、M4 tests、注册表 | worker 两轮同 PID/thread；取消确认；断线失败；close 无残留；真实 E2E 单独登记 |
| 8 | 实现里程碑 workflow 或运行中 steering | [`adr/0009-bounded-milestone-workflow-steering.md`](adr/0009-bounded-milestone-workflow-steering.md)；[`SPEC.md`](SPEC.md) UC-WORKFLOW-001；`../HARNESS.md` §4 | R1–R4、R6、干净 Git fixed point、单 writer、固定阶段/角色/修复上限、read-only review/verify | workspace inspector、`workflow.py`、adapter execution mode、Orchestrator/CommandBus、control/MCP/TUI、对应 fake tests | baseline/candidate 指纹固定且漂移 fail-closed；最多六次调用；阶段结果 fail-closed；steering 只在阶段边界生效；写入只由 implementer 串行发生；取消/no-replay/回收契约通过 |
| 9 | 新增或修改会话级自然语言角色 | [`adr/0011-session-scoped-natural-language-roles.md`](adr/0011-session-scoped-natural-language-roles.md)；`../HARNESS.md` §4.1 | R1–R6、固定 target 闭集、room 级原子状态、角色不扩权 | `session_roles.py`、host/Orchestrator、RoomStore、TUI、对应 tests | 设置/取消、跨任务持续、讨论跨轮、命名会话隔离与重启恢复；写失败不假提交；活动区明确标注本会话；`/roles` 查看/清空不进 timeline，运行中不跨阶段清空 |
| 10 | 改 agent 安装探测、全局开关、可用性或 setup 引导 | [`adr/0012-agent-readiness-and-setup-ux.md`](adr/0012-agent-readiness-and-setup-ux.md)；`../HARNESS.md` §4 | R2、R3、R5、R6 | `agent_readiness.py`、`AgentSpec`、Orchestrator/TUI、fake tests | 启动 probe 无进程/网络/安装副作用；未就绪/已禁用在 timeline 前原子拒绝；host 只看 ready worker；开关保留 agent 本体与状态；rescan 无需重启 |
| 11 | 改自然语言有序协作或步骤接力 | [`adr/0013-natural-language-sequential-collaboration.md`](adr/0013-natural-language-sequential-collaboration.md)；[`SPEC.md`](SPEC.md) UC-COLLAB-001；`../HARNESS.md` §4.1 | R1–R5 | `collaboration.py`、host/Orchestrator、TUI 状态、fake tests | 2–4 步、至少两个 worker、严格串行、前序产物可见、失败/取消即停、无递归/扩员 |
| 12 | 改 HostBackend、原生模型 provider/runtime 或 agent Host | [`adr/0017-native-model-backed-host.md`](adr/0017-native-model-backed-host.md)；[`SPEC.md`](SPEC.md) UC-HOST-001；`../HARNESS.md` §4.9 | R1–R6、provider-neutral、model tool-less、agent Host 独立只读、no-replay、无 fallback | `host_backend.py`、`native_agent/`、`host.py`、`AgentSpec`、RoomStore、TUI、fake tests | room 级 model/agent 切换与恢复；provider discovery capability；fresh session；同名 worker 隔离；运行中拒绝；权限/no-fallback/显式 @ 反例通过 |
| 13 | 改 Esc 取消、运行中插话或 transport steering capability | [`adr/0018-capability-bounded-runtime-interjection.md`](adr/0018-capability-bounded-runtime-interjection.md)；[`adr/0009-bounded-milestone-workflow-steering.md`](adr/0009-bounded-milestone-workflow-steering.md)；[`SPEC.md`](SPEC.md) UC-INTERJECT-001；`../HARNESS.md` §4.3、§4.6 | R2–R6、单 writer、能力显式、no-replay、workflow bounds | adapter capability、Orchestrator active delivery、CommandBus execution events、TUI、fake transport tests | Esc 精确取消且 overlay 优先；Alt+↑ 成功清空/失败保留；Pi 同 session native steer；ACP/多目标 fail-closed；requested 在协议写入前持久化 |

不在表内且会改变协议、安全或外部接口的任务，先向用户确认范围。

## 2. 红线速记

| § | 速记 |
| --- | --- |
| R1 | 权限默认 deny，生产不得显式 auto |
| R2 | 通用层不按 agent 名分支 |
| R3 | 子进程只在 transport 层启动 |
| R4 | Kimi/OpenCode 保持受限 hybrid；Qwen Code/CodeBuddy/DSH 保持 ACP-only；DSH 以 stock `dsh --profile myagents` 加载 myagents 标准 bundle、stock DSH 不可变，并保持被动 profile/bundle readiness、load+close gate、两 execution safety profile、零 fallback；Pi 保持 RPC-only + 固定 bridge/wrapper/attestation；所有 runtime profile 受约束 |
| R5 | 自然语言讨论与 `/discuss` 固定 2–3 人、1–3 轮，不由 agent 自主续轮 |
| R6 | `/workflow` 固定角色/阶段、单 writer、一次 repair 上限和有界 steering |

## 3. 上下文按需载入

| 目的 | 必读 | 按需 |
| --- | --- | --- |
| 理解产品与路线 | `README.md`、`docs/SPEC.md` | `docs/concepts.md` |
| 改 ACP/Pi RPC 协议或 hybrid fallback | `docs/acp-migration.md`、对应 ADR-0006/0007/0014/0015、HARNESS §4 | `docs/harness-controls.md` 对应 Control |
| 改编排器 | HARNESS §3–§4、SPEC 对应用例 | `host.py` 与 adapter 调用方 |
| 新增 agent | `AGENTS.md`、HARNESS §1–§3 | 对应 CLI 官方协议文档 |
| 改会话级自然语言角色 | ADR-0011、HARNESS §4.1 | RoomStore 与 host/Orchestrator/TUI 调用方 |
| 处理重复失败 | `docs/harness-controls.md` Steering 规则 | 相关 Guide/Sensor 证据 |

## 4. Self-correction

1. 先读 gate 输出中的红线编号和修复方向。
2. 回到本表对应场景及 Guide 修复当前变更。
3. 重跑同一 Sensor；不得删测试、降权限校验或改 gate 制造假绿。
4. 同类失败至少出现两次或形成趋势证据时，才向 Owner 提议更新 Harness。

## 5. 交付前检查

```bash
bash scripts/check-harness.sh
```

并确认：

- 任务范围外文件未被修改；
- 关键 Behaviour 的证据与 [`SPEC.md`](SPEC.md) 仍一致；
- 真实 agent 测试若未获授权，明确写“未运行”，不能用 fake test 冒充；
- 当前无 Git 时不声称已提交、已推送或已有 CI 保护。

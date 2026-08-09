# AGENTS.md — myagents Agent 协作契约

<!-- harness:controls-read-policy=on-demand -->

> 作者：Bryant Yang　最近更新：2026-07-26
>
> 本文是所有编码 agent 的项目级入场入口，也是唯一的协作规则源。详细工程契约见
> [`HARNESS.md`](HARNESS.md)，按任务选读规则见
> [`docs/workflow.md`](docs/workflow.md)。

## 1. 项目定位

`myagents` 是一个 Python + Textual 的本地多 agent 终端编排器。它以
hub-and-spoke 方式维护统一时间线，以 ACP 作为有状态 coding agent 的优先接入
协议，同时保留 JSONL adapter 作为兼容回退。

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
  `permission="auto"`；自动放行只能由明确授权的外部调用临时 opt-in。
- **R2 通用层不得按 agent 名分支**：`orchestrator.py` 与通用 ACP runtime
  不得出现 `if agent_name == "kimi"` 一类协议分支；差异必须进入
  `AgentSpec` 或具体 adapter。
- **R3 进程只能由 transport 层启动**：`main.py`、`orchestrator.py`、
  `host.py` 不得直接创建 shell/子进程。
- **R4 Kimi/OpenCode 生产路径保持 ACP-first**：生产注册必须分别由
  `AcpKimiAdapter` / `AcpOpenCodeAdapter` 构造；JSONL 只能作 ACP
  prepare 失败前的只读 fallback，并使用各自项目内置工具白名单。
  禁止直接注册旧 JSONL adapter、放宽写入/命令工具或在 prompt
  提交后跨协议重放；OpenCode 未知及有副作用工具必须进入 ask。
- **R5 多智能体讨论必须显式且有界**：`/discuss` 只允许 2–3 个已注册
  worker、1–3 轮和一个终局 moderator；轮次由普通代码推进，禁止 agent
  自主递归派发、动态扩员或形成无界对话。
- **R6 里程碑 workflow 必须固定且单写者**：`/workflow` 固定 review →
  implement → verify，最多一次同 writer repair/reverify 和一次 host final；
  review/verify/final 必须 read-only，steering 只允许在阶段边界按冻结上限追加，
  禁止递归派发、换角色、扩权限或无界修复。

## 4. 工作要求

- 改协议、权限、并发或进程生命周期前，先读
  [`docs/acp-migration.md`](docs/acp-migration.md)；改持久化、恢复或
  lease 前，先读
  [`docs/adr/0001-persistent-room-command-bus-mcp.md`](docs/adr/0001-persistent-room-command-bus-mcp.md)。
- 新增 agent 时实现统一 `AgentAdapter`，在 `AGENT_SPECS` 注册；不要把
  name-specific 逻辑散进编排器。
- 权限处理器返回值必须绑定本次 `params.options` 校验；异常、空值或未知
  `optionId` 一律 cancelled。
- ACP session 同一时刻只有一个 writer；不要让独立 native TUI 与 ACP client
  并发写同一 session。
- Kimi/OpenCode hybrid transport 分别遵守
  [`ADR-0006`](docs/adr/0006-kimi-hybrid-transport-policy.md) 和
  [`ADR-0007`](docs/adr/0007-opencode-hybrid-transport-policy.md)；
  JSONL checkpoint 不是可恢复 ACP session，下一轮必须新建会话。
- 有界讨论遵守
  [`ADR-0008`](docs/adr/0008-bounded-multi-agent-discussion.md)：同轮并发、
  跨轮串行，一条 command 只有一条 user 记录；参与者失败不自动重试，最终
  moderator 不能掩盖失败终态。
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

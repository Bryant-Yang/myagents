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
- **R4 Kimi 生产路径保持 ACP-first**：生产注册表不得重新导入或注册旧
  `KimiAdapter` JSONL 实现。调整此决策前必须先更新协议设计与验收契约。

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

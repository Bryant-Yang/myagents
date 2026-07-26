# myagents 工程契约（HARNESS）

> 作者：Bryant Yang　最近更新：2026-07-26
> Harness Framework：2.1.0（来源 commit：
> `b3b8fd47ebc49b57abdc365f327688b33970d54c`）

这是 `myagents` 的工程事实源。产品行为与独立证据见
[`docs/SPEC.md`](docs/SPEC.md)，任务入口见
[`docs/workflow.md`](docs/workflow.md)，控制闭环见
[`docs/harness-controls.md`](docs/harness-controls.md)。

## 0. 项目使命

为不同 coding agent 提供一个统一、可验证的本地 TUI 编排面：路由和共享时间线
只有一个中心；ACP 负责有状态 agent 会话；adapter 隔离各 CLI 差异；权限默认
拒绝；取消或退出后不留下仍能修改工作区的子进程。

## 1. 北极星原则

| 原则 | 项目解释 |
| --- | --- |
| 一个编排中心 | agent 不直接互调，消息与 history 统一经过 Orchestrator。 |
| ACP-first，JSONL fallback | 有官方/可靠 ACP 时优先有状态协议；JSONL 只做兼容回退。 |
| 协议通用、差异下沉 | 通用 runtime 不按 agent 名分支，具体差异进入 adapter/spec。 |
| 权限 fail-closed | 无处理器、异常或畸形选择一律拒绝；auto 必须显式授权。 |
| 生命周期负责到底 | 启动的进程组必须能 cancel、close 并被独立验证已回收。 |
| 行为证据优先 | fake contract tests 之外，关键真实协议边界保留人工/E2E 证据。 |

## 2. 技术与目录基线

| 类别 | 当前选择 |
| --- | --- |
| 语言 | Python 3.11+ |
| TUI | Textual `>=1.0` |
| ACP transport | NDJSON JSON-RPC 2.0 over stdio，protocolVersion 1 |
| JSONL transport | 各 agent CLI 无头模式 |
| 测试 | 直接运行的 Python test scripts + Textual pilot + fake ACP server |
| 本地总门禁 | `bash scripts/check-harness.sh` |
| Git / CI | Git 已初始化；远程 CI 尚未配置，不得假称已有合并门禁 |

```text
main.py (Textual TUI)
    ↓
orchestrator.py (routing / history / delivery locks)
    ↓
acp/             adapters/             host.py
ACP runtime      JSONL fallbacks       supervisor wrapper
    ↓                  ↓
coding agent subprocesses
```

`tests/fake_acp_server.py` 是协议 fixture，不是生产 transport。

## 3. 分层与依赖

- `main.py` 只负责 UI、权限交互和生命周期入口，不直接启动 agent 进程。
- `orchestrator.py` 只面向 adapter 能力和 `AgentSpec`，不解析厂商协议。
- `acp/client.py` 负责通用 ACP framing、request/response、反向权限请求和进程回收。
- `acp/adapter.py` 把 ACP session 转成 `AgentEvent`。
- `adapters/` 包含具体 JSONL CLI 参数/解析与共享子进程工具。
- `host.py` 包装一个 adapter 做路由/主持，不拥有 transport 实现。

跨层协议变化必须同步实现、调用方、fake fixture、tests 和
[`docs/acp-migration.md`](docs/acp-migration.md)。

## 4. 核心契约

### 4.1 路由与上下文

- 显式 `@agent` 永远优先；无 `@` 才调用 host 语义路由。
- JSONL adapter 收 dispatch 时刻的有界 transcript 快照。
- stateful ACP adapter 在每-agent delivery lock 内读取 cursor、构造增量、
  完成 stream 后推进 cursor；失败不得推进。
- 首次 ACP bootstrap 最多发送 `history_limit` 条共享历史。

### 4.2 权限

- `AcpClient`、`AcpAdapter`、`AcpKimiAdapter` 默认权限都是 `deny`。
- TUI 异步决定权限并显示来源 agent。
- `selected.optionId` 必须非空且属于本次 `params.options`；否则 cancelled。
- `auto` 只允许明确授权的测试、运维或一次性外部调用使用。

### 4.3 生命周期

- 每个 ACP adapter 是其 session 的唯一 writer。
- 同一 ACP agent 的 prompt 串行；不同 agent 可以并发 fan-out。
- cancel 后必须等待原 prompt 停止；超时则关闭并重建连接。
- 子进程使用独立进程组；退出时 SIGTERM，超时再 SIGKILL。
- TUI unmount 先取消权限 Future，再调用 `Orchestrator.aclose()`。

## 5. 测试策略

| 层级 | 证据 |
| --- | --- |
| 路由/编排 | `tests/test_basic.py` |
| ACP 协议与取消 | `tests/test_acp.py` + `tests/fake_acp_server.py` |
| TUI/增量/权限/回收 | `tests/test_phase2.py` |
| 真实协议边界 | `docs/SPEC.md` 登记的 Kimi ACP + Textual 人工 E2E |

普通测试禁止调用真实 Kimi/Codex/OpenCode。真实 agent 验收必须由用户明确授权，
在临时目录运行，并在结束后检查没有残留进程。

## 6. 质量门禁

当前本地门禁顺序：

```text
harness 文档引用 → redlines → py_compile → basic → ACP → Phase 2
```

运行：

```bash
bash scripts/check-harness.sh
```

项目当前只有本地 gate，尚无远程 CI，不能保证每次合并都会自动触发。远程
branch protection / required checks 需要单独配置后才能宣称生效。

## 7. 推理型与人工边界

以下重要但不能伪装成 grep 红线：

- 新抽象是否真的比现有 `AgentSpec + AgentAdapter` 更简单；
- 某 agent 的 ACP 适配器是否成熟到可替换 JSONL fallback；
- 是否应该把外部入口实现为 MCP、Unix socket 或其他传输；
- 真实工具调用的权限风险是否可接受；
- TUI 的可用性、长会话 token/内存表现和真实 cancel 时延。

具体 Owner、Sensor 与处置见 [`docs/harness-controls.md`](docs/harness-controls.md)。

## 8. 红线（4 条，违反必驳回）

| # | 红线 | 守门 |
| --- | --- | --- |
| R1 | 生产构造不得显式使用 `permission="auto"`，权限默认必须为 `deny` | `bash scripts/check-redlines.sh` 的 AST permission gate |
| R2 | 通用 orchestration/ACP 层不得按具体 agent 名做条件分支 | `bash scripts/check-redlines.sh` 的 AST name-branch gate |
| R3 | UI、orchestrator、host 不得直接启动 shell/子进程 | `bash scripts/check-redlines.sh` 的 AST process-boundary gate |
| R4 | Kimi 生产注册不得从 ACP 回退到旧 JSONL `KimiAdapter` | `bash scripts/check-redlines.sh` 的 registry/import gate |

红线变更必须同步本文、`AGENTS.md`、`docs/workflow.md`、enforcement 与
`docs/harness-controls.md`，并重新做正向和负向验证。

## 9. 决策与演进

- 当前 ACP 架构事实源：[`docs/acp-migration.md`](docs/acp-migration.md)。
- 当前路线图：[`README.md`](README.md)“路线图”。
- 重大协议/安全边界改变先形成可评审设计记录，再修改本契约。
- Steering 只在同类失败至少两次或已有趋势证据时建立；单次失败只修当前问题。

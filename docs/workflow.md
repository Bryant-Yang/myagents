# myagents 工作流

> 作者：Bryant Yang　最近更新：2026-07-26
>
> 每次任务先在本表定位场景，再按需加载文档。顶层契约是
> [`../HARNESS.md`](../HARNESS.md)。

## 1. 典型场景

| # | 场景 | 先读哪份 | 必守红线 | 产出落点 | DoD |
| --- | --- | --- | --- | --- | --- |
| 1 | 改路由、history 或 fan-out | [`SPEC.md`](SPEC.md) 路由/增量用例；`../HARNESS.md` §4.1 | R2、R3 | `orchestrator.py`、`host.py`、对应测试 | 快照与并发顺序测试通过；无 name-specific 协议分支 |
| 2 | 改 ACP、权限、取消或 session | [`acp-migration.md`](acp-migration.md)；`../HARNESS.md` §4.2–4.3 | R1–R4 | `acp/`、fake server、ACP/Phase 2 测试、协议文档 | fail-closed、串行化、回收测试通过；高风险变化有真实/人工验收计划 |
| 3 | 新增或迁移 agent | `../HARNESS.md` §1–§3；`acp-migration.md` | R2–R4 | 具体 adapter + `AGENT_SPECS` + tests + README | adapter 接口统一；transport 状态可见；JSONL fallback 无回归 |
| 4 | 改 TUI 或权限交互 | [`SPEC.md`](SPEC.md) 权限用例；`../HARNESS.md` §4.2–4.3 | R1、R3 | `main.py` + Textual pilot tests | UI 不阻塞；退出无 Future/进程残留；来源 agent 可见 |
| 5 | 只做 review / 文档 / Harness | 本文件；相关契约；必要时 [`harness-controls.md`](harness-controls.md) | 所有受影响红线 | 对应文档、Sensor 或 review 结论 | 引用无悬空；红线 gate 与相关测试通过 |

不在表内且会改变协议、安全或外部接口的任务，先向用户确认范围。

## 2. 红线速记

| § | 速记 |
| --- | --- |
| R1 | 权限默认 deny，生产不得显式 auto |
| R2 | 通用层不按 agent 名分支 |
| R3 | 子进程只在 transport 层启动 |
| R4 | Kimi 生产路径保持 ACP-first |

## 3. 上下文按需载入

| 目的 | 必读 | 按需 |
| --- | --- | --- |
| 理解产品与路线 | `README.md`、`docs/SPEC.md` | `docs/concepts.md` |
| 改 ACP 协议 | `docs/acp-migration.md`、HARNESS §4 | `docs/harness-controls.md` 对应 Control |
| 改编排器 | HARNESS §3–§4、SPEC 对应用例 | `host.py` 与 adapter 调用方 |
| 新增 agent | `AGENTS.md`、HARNESS §1–§3 | 对应 CLI 官方协议文档 |
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

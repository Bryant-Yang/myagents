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
| Architecture Fitness | 较强：transport 边界、通用 runtime、ACP-first 和权限默认值有确定性检查 | HARNESS R1–R4、`test_phase2.py` | 不证明新抽象必要，也不覆盖生产性能 |
| Behaviour | 关键 Phase 2 路径有 fake contract tests 与真实 Kimi E2E 双证据 | [`SPEC.md#关键行为用例`](SPEC.md#关键行为用例) | 不保证所有 CLI 版本、长会话或真实 cancel 时延 |

## 2. Control Map

| ID | 目标 | Guide（行动前） | Sensor / 人工确认（行动后） | 失败处置 | Owner |
| --- | --- | --- | --- | --- | --- |
| C1 | 通用 agent 接入边界 | HARNESS §3；`acp-migration.md` 架构 | R2–R4；`test_phase2.py` AgentSpec/并发用例 | 阻断并把差异下沉至 adapter/spec | Bryant Yang |
| C2 | 权限 fail-closed | HARNESS §4.2；`acp-migration.md` 权限策略 | R1；ACP/Phase 2 权限测试；SPEC UC-PERM-001 | 阻断；恢复 deny/合法 option 校验后复验 | Bryant Yang |
| C3 | 增量上下文不重发、不乱序 | HARNESS §4.1；`acp-migration.md` 增量契约 | `test_phase2.py` cursor/bootstrap/delivery-lock 用例 | 不推进 cursor；修复后重跑并发回归 | Bryant Yang |
| C4 | 取消与退出不留进程 | HARNESS §4.3；`acp-migration.md` 取消/生命周期 | basic/ACP/Phase 2 回收测试；SPEC UC-LIFE-001 人工边界 | 阻断交付；清理进程并定位锁/进程组问题 | Bryant Yang |
| C5 | Harness 入口与引用不漂移 | `AGENTS.md`；`workflow.md` | `scripts/check-harness.sh` 文档/marker/link Sensor | 修正文档或引用；不得复制多份规则 | Bryant Yang |
| C6 | 关键行为证据可信 | `SPEC.md` | fake fixture + 既有 tests + 授权的真实 E2E/人工验收 | 报告证据缺口，不以新生成测试代替验收 | Bryant Yang |

## 3. 约束等级

| ID | 等级 | 判据 | 处置 |
| --- | --- | --- | --- |
| R1–R4 | Deterministic Gate | AST/注册表检查低误报且能定位文件 | 违反必拦，修复后复验 |
| S1 | Inferential Review Criterion | 新抽象、跨层职责、重复逻辑需要语义判断 | review 提供证据与替代方案，不假装机械事实 |
| S2 | Inferential Review Criterion | fake server 是否仍代表真实 ACP 边界 | 比较真实 wire/options；必要时更新 fixture |
| A1 | Human Acceptance Decision | 真实工具调用、auto 权限和外部系统写入风险 | 只有用户明确授权才执行 |
| A2 | Human Acceptance Decision | MCP/Unix socket/A2A 路线及兼容成本 | Owner 决定后再冻结协议 |

## 4. Behaviour Evidence 引用

| 用例 / 风险 | SPEC 证据位置 | Control |
| --- | --- | --- |
| 显式路由与 fan-out | [`SPEC.md#uc-route-001-显式路由与并发扇出`](SPEC.md#uc-route-001-显式路由与并发扇出) | C1 |
| ACP 增量上下文 | [`SPEC.md#uc-acp-001-有状态增量上下文`](SPEC.md#uc-acp-001-有状态增量上下文) | C1、C3 |
| 权限决策 | [`SPEC.md#uc-perm-001-权限请求与选择`](SPEC.md#uc-perm-001-权限请求与选择) | C2、C6 |
| 生命周期 | [`SPEC.md#uc-life-001-取消与退出回收`](SPEC.md#uc-life-001-取消与退出回收) | C4、C6 |

## 5. 反馈生命周期

| 阶段 | 必跑 Sensor | 失败后 |
| --- | --- | --- |
| Agent 本地循环 | `check-redlines.sh` + 相关 test script | 当前 agent 自修并复验 |
| 完整交付 | `check-harness.sh` | 阻断“完成”声明 |
| 人工 Review | SPEC 证据边界、真实权限/协议风险 | 修改或由 Owner 明确接受 |
| 真实 agent 验收 | 临时目录、显式授权、退出后进程检查 | 立即停止外部写入并报告 |

## 6. Steering

同类 Harness 失败至少出现两次或形成趋势证据后才建 Steering 记录。当前首次接入
没有达到触发条件，不建立空台账。

## 7. Baseline 与模板升级

- **Legacy baseline**：R1–R4 当前均为零命中，不需要债务 baseline。生成方式是
  `bash scripts/check-redlines.sh`；若未来接入时已有历史债，必须先保存稳定、
  去行号的命中集合，再用 `comm -13` 只拦新增。
- **模板来源**：Harness Framework `2.1.0`，来源 commit
  `b3b8fd47ebc49b57abdc365f327688b33970d54c`。
- **本地差异**：小型 Python 项目；不引入重型 CI/commitlint/lint 骨架；使用 AST
  Sensor 检查项目特有协议和进程边界；真实 Behaviour 证据记录 Phase 2 E2E。
- **升级策略**：人工比较上游模板，只合并适用的方法变化；红线变化必须重新做
  负向探针。

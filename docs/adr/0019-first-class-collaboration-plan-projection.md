# ADR-0019：一等协作计划与步骤交接投影

- 状态：Accepted
- 日期：2026-08-28
- Owner：Bryant Yang
- 里程碑：M7.1

## 1. 背景

ADR-0013 已把自然语言协作限制为一个 2–4 步、严格串行、失败即停的
`CollaborationPlan`。但实时 UI 仍主要把事件压缩成“每个 agent 当前状态”：

- 同一 agent 在第 1、3 步重复出现时，两步会折叠成一行；
- assignment、当前步骤和交接关系只存在于编排器内存；
- `events.jsonl` 只保留自然语言阶段文本，重启后的 `/details` 无法可靠恢复计划；
- UI 若解析“协作第 2/3 步”等文案，会把展示逻辑绑定到不稳定文本。

因此需要一个结构化 seam，但不能引入第二套调度器、修改 R5 上限，或把执行权
交给 Agent 框架。

## 2. 决策

### 2.1 调度事实与展示投影分离

`CollaborationPlan` 继续是唯一执行权威，Orchestrator 仍用普通代码串行推进。
新增版本化 `CollaborationPlanEvent`，只投影已冻结的计划和确定性步骤迁移：

- `created`：固定 2–4 个 `agent + assignment preview`；
- `step`：固定 `step / total / agent / state`；
- state 只允许 `running / completed / failed / cancelled / skipped`。

计划事件不能触发 adapter、改变下一步、重试、扩员或恢复执行。任何调度逻辑读取
UI 投影都属于违约。

### 2.2 持久化与安全

`events.jsonl` 保持 ADR-0002 的六字段 schema，只新增 `kind=plan`；`text` 是带
`version=1` 的严格 JSON payload。这样旧房间不迁移，旧 reader 遇到新 kind 时按
既有严格 kind 合同升级，而不引入可选字段歧义。

assignment 只保存单行、有界、常见凭据已隐藏的 240 字预览；完整用户请求仍在
timeline，完整内部 assignment 不复制到执行日志。计划事件不进入 agent history，
也不显示 chain-of-thought。

按 command 有界读取详情时，最多额外固定保留 16 条 plan 事件，并继续遵守调用方
给定的总 limit；正常 2–4 步计划最多产生 9 条事件。损坏 payload 不执行、不影响
其余详情可用性，原始 append-only 记录仍保留作为诊断证据。

### 2.3 TUI

固定任务区与活动卡消费同一个纯 `CollaborationPlanProgress` 投影：

- 每一步按序号独立展示，重复 agent 不合并；
- 折叠活动卡显示当前 `步骤 x/n + agent + assignment preview`；
- 展开态显示等待、进行中、已结束、失败、取消和未执行；
- 从第 2 步开始显示前序 agent 到当前 agent 的交接；
- `/details` 从 plan 事件重建相同视图，不展示原始 JSON；
- 计划之外的 host、工具、权限、错误和控制事件继续沿用 ADR-0002。

### 2.4 不选择的方案

- 不引入 LangGraph、LlamaIndex Workflow 或 smolagents 作为编排层；
- 不由 UI 解析阶段中文文案；
- 不把 plan meta 加入 timeline 或 agent context；
- 不为展示增加重试、动态 handoff、并行写入或 Agent 自主下一跳；
- 不新增通用任意 JSON metadata 字段，避免扩大执行日志攻击面。

## 3. 验收

1. created/step payload 版本、字段、步骤数、状态和凭据隐藏均有纯模型测试。
2. 成功协作持久顺序为 created → 每步 running/completed。
3. 失败和取消分别产生 failed/cancelled，未启动步骤产生 skipped；不新增调用。
4. 重复 agent 的不同步骤在固定任务区和活动卡中独立展示。
5. 折叠态显示当前步骤，展开态显示 assignment 与交接，原始 JSON 不可见。
6. 重启详情从 `events.jsonl` 恢复计划；高频状态超过读取窗口时仍固定保留计划。
7. malformed plan event 不崩溃、不成为调度输入；完整 Harness 继续通过。

## 4. 后果

myagents 的协作从“按 agent 展示调用状态”提升为“按冻结步骤展示可验证接力”，
而调度、安全和协议模型保持不变。后续 workflow 若需要统一计划 UI，应新增第二个
真实 producer 后再抽象通用 execution-plan seam，不能提前让 CollaborationPlan
迁就尚不存在的接口。

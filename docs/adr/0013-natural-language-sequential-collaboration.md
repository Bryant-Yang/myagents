# ADR-0013：自然语言有序协作计划

- 状态：Accepted
- 日期：2026-08-12
- Owner：Bryant Yang
- 里程碑：M7

## 1. 背景

现有显式多 mention 只做同轮并行 fan-out，`/discuss` 负责有界观点交流，
`/workflow` 固定服务于 review → implement → verify。用户仍无法用一句自然语言
可靠表达“先让 A 调研，再让 B 基于结果设计，最后让 C 复核并交付”。

不能把执行顺序、参与者和停止条件交给 agent 自主决定，否则前后依赖、取消、失败
终态和调用上限都无法验证；也不应强迫用户学习一个新的 `/task` 命令。

## 2. 决策

### 2.1 自然语言是唯一新增入口

- 无 mention 消息继续只调用一次 host。host 只能返回三种结果之一：直接回答、
  既有并行路由、或一个 `CollaborationPlan`。
- `@` 明确点名两个以上 worker 且文本表达明确先后关系时，普通代码只负责识别
  “需要语义提取”的候选形状；host 在固定 mention 闭集内提取有序计划，不得增员
  或漏掉已点名参与者。
- “一起、分别、各自”等没有先后关系的表达保持既有 fan-out；不新增 `/task`。

### 2.2 CollaborationPlan 合同

- 一个计划有 2–4 个顺序步骤，至少包含两个不同、已注册且 ready 的 worker。
- 每步只有 `agent` 与非空 `assignment`；agent 可以在后续步骤再次出现，以支持
  “起草 → 复核 → 原作者修订”，但总步骤仍受上限约束。
- host 只做语义理解和任务改写。步骤校验、顺序推进、停止条件和事件状态由普通
  代码实现；模型输出未知 agent、缺步、超限、空任务或畸形 JSON 一律拒绝。
- 最后一步必须被 host 写成面向用户的最终交付；系统不自动追加 host 总结。

### 2.3 执行与上下文

- 整个计划是一个 CommandBus command，只有一条真实 user timeline 记录。
- 步骤严格串行。每步开始时从当前共享 timeline 构造上下文，因此后续 worker
  能看到此前步骤的真实回复；内部 assignment 不伪装成 user 消息。
- 每步回复照常进入 timeline，并共享原 command id。事件携带计划阶段、总步骤和
  当前 agent，现有活动卡和固定任务区据此展示进度。
- 权限、adapter execution mode、session/cursor、no-replay 与进程生命周期沿用
  现有契约；协作计划不扩权，也不创建 agent 之间的直接调用。

### 2.4 失败与取消

- 任一步失败立即停止，后续步骤不调用；失败进入 `DispatchOutcome`，CommandBus
  终态为 failed。不得自动重试、换人、跳步或用成功的前序回复掩盖失败。
- 取消当前 command 会取消当前步骤，之后不再启动任何步骤。
- 最大模型调用数固定为：一次 host 识别 + 四个 worker 步骤；显式 mention 的
  计划提取同样最多一次 host 调用。

## 3. 不选择的方案

- **新增 `/task` 作为主要入口**：增加产品概念和语法负担；自然语言已经足以表达。
- **agent 自主决定下一位参与者**：会动态扩员并形成无界调用。
- **把所有人一次 fan-out 后要求自行参考**：同轮 agent 看不到彼此本轮产物。
- **每步创建独立 command**：会伪造多条用户任务，破坏整体取消和失败终态。
- **默认并发写文件**：共享 workdir 存在竞态；本阶段仅提供严格串行接力。

## 4. 验收

1. fake host 从自然语言识别有序计划，同时保持直接回答与并行路由兼容。
2. 计划解析拒绝未知/未就绪 agent、1/5 步、空任务和单一参与者循环。
3. fake worker 证明严格顺序、后一步看见前一步回复、唯一 user 记录、统一
   command id，并由最后一步产生最终交付。
4. 中间步骤失败或取消后不启动后续 agent，CommandBus 终态诚实。
5. 明确多 mention 的有序表达固定参与者闭集；普通 fan-out 不额外触发计划。
6. 所有自动化只使用 fake adapter，不调用真实 agent；完整 Harness 通过。

## 5. 后果

myagents 从“并排调用多个 agent”扩展为“让多个 agent 可靠接力完成一个任务”，
且保持聊天式自然入口。代价是 host 路由结果增加一个受限形状、Orchestrator 增加
一个有界顺序状态机；复杂度集中在 `collaboration.py`，不下沉到各 transport。

# ADR-0011：会话级自然语言临时角色

- 状态：Accepted
- 日期：2026-08-12
- Owner：Bryant Yang
- 里程碑：M6

## 1. 背景

用户需要在聊天室里直接表达“让 Qwen 当产品研究员、让 OpenCode 当反方审查者”，
并让分工在当前会话的后续任务中保持，而不是先维护一套永久角色。全局 Profile 会
引入配置 CRUD、跨会话角色库和独立记忆系统；这些都不是当前协作主线。

原始自然语言会进入 worker prompt，但编排器不知道角色，因此不能在活动区显示，
不能保证讨论每一轮稳定提醒角色，也不能在上游 session 重建后继续注入。

## 2. 决策

### 2.1 生命周期与存储

- 角色属于一个现有命名会话（room），表示 `实际 agent -> label + instructions`。
- 角色在该会话后续命令中持续生效；切换其他会话不带过去，新建或删除会话自然
  隔离。重启恢复同一会话时一并恢复。
- 不新增全局 Profile、角色文件、角色模板 CRUD、向量记忆、跨会话检索或自动继承。
- 角色复用已有 `state.json` 原子状态，作为可选 `session_roles` 字段；旧房间缺失
  该字段等同于空映射，不升级 schema、不迁移 timeline。
- 原始用户消息仍是唯一对话事实并照常写 timeline。角色状态不伪造成新的 user
  消息，不改变 RoomStore 的 agent cursor/session entry。

### 2.2 语义提取与确定性边界

- 自然语言理解交给 host 模型；普通代码先固定候选 targets，再只接受这些 target
  的 `set` 或 `clear` 变化。模型不能增加参与者、轮次、权限或工具。
- 无显式 mention 时，host 在既有单次路由判断中同时返回可选角色变化，不增加
  一次模型调用。
- 有显式 mention 或 `/discuss` 时，仅在文本包含“作为、担任、扮演、角色、
  不再、取消、act as、serve as”等明确线索时调用 host 的纯提取路径；没有线索
  时零额外调用。
- label 折叠空白后为 1–40 字符，instructions 为 1–1000 字符。未知 target、
  非字符串、空值和超限值丢弃；同一 target 同时 set/clear 时 clear 优先。
- 畸形输出或提取失败不阻断原任务：原文仍完整交给 worker，已有会话角色不变。
- 提取 prompt 明确禁止工具、文件、命令、网络和 skill，只接受 JSON object。

### 2.3 原子更新、投递与显示

- 用户消息先按既有契约持久化；提取成功后，角色变化必须原子写入 `state.json`，
  写失败不更新内存、不派发 worker，并向 CommandBus 传播失败。
- Orchestrator 在每轮 assignment 前注入权威的当前会话角色状态：有角色时注入
  label/instructions，无角色时明确要求不得沿用先前临时角色。这使得清空在
  有状态 ACP/app-server session 的下一轮也生效。角色只是工作视角，提示中
  明确不能改变权限、安全策略、参与者、讨论轮次或 workflow 阶段。
- `/discuss` 每一轮都注入参与者角色；workflow 仍由固定 review/implement/verify
  assignment 和 execution mode 主导，角色不能替换 workflow 职责。
- Orchestrator 用 status event 的 `session_role` meta 公布 label。固定任务区和
  活动卡显示 `agent · 角色（本会话）`，后续 status/tool/done 更新必须保留。
- TUI 精确命令 `/roles` 只读取当前 room 的角色快照；`/roles clear` 通过
  Orchestrator 原子清空当前 room 的全部角色。两者都不写 timeline、不调用 host
  或 worker。存在 queued/running command 时拒绝清空，避免跨阶段改变正在执行的
  assignment；写盘失败时保留内存和磁盘角色并显示错误。
- timeline speaker、adapter 名、权限弹窗来源、cursor key 和原生 session id
  始终是实际 agent 名，角色不能伪装成新 agent。

## 3. 不选择的方案

- **永久 TOML Profile**：需要独立角色库与迁移系统，超出当前需求。
- **只依赖 agent 自己记住一句话**：TUI 不可见，且上游新建 session 后会漂移。
- **正则直接抽取完整中文角色**：难覆盖自然语言变化；正则只决定是否调用语义
  提取，角色内容由模型理解。
- **每条消息都调用提取器**：无角色消息不应增加延迟和成本。
- **把角色作为 speaker 或新 user 消息写 timeline**：污染共享事实并制造不存在
  的 agent 身份。

## 4. 验收

1. `@qwen 接下来你担任产品研究员……` 原子建立角色；当前及后续 Qwen prompt
   都获得角色前缀，活动区显示 `qwen · 产品研究员（本会话）`。
2. `@qwen 不再担任该角色……` 清除角色；后续 prompt 显式注入无角色状态，
   不再沿用旧角色，活动区不再显示角色。
3. 无 mention 的“让 Qwen 担任研究员”由既有 host 路由调用同时返回 target、
   task 和角色变化；不发生第二次模型调用。
4. `/discuss` 只接受固定参与者/主持人的角色变化，每轮稳定注入；模型输出不能
   改变参与者、轮数或 moderator。
5. 两个命名会话的角色互不相见；关闭并重开同一房间后恢复；旧房间保持空角色
   兼容。角色写失败时内存/磁盘均不假提交。
6. 未知 target、超限/畸形值和提取异常均不扩员、不改已有角色、不阻断原任务；
   无角色线索时不调用提取器。
7. 权限、runtime、session/cursor 和 R1–R6 不变；完整 Harness 通过。
8. `/roles` 展示实际 agent、label 与 instructions；`/roles clear` 空闲时原子清空，
   运行中拒绝。两个精确本地命令均不进入 timeline、不调用模型；其他
   `/roles ...` 文本仍按普通消息处理。

## 5. 后果

用户能在自然对话中安排本会话的长期分工，无需管理角色实体或额外记忆系统。代价
是显式点名且带角色线索的消息会多一次 host 语义提取；角色会随房间状态持久化，
但不会跨会话复用或被全局搜索。

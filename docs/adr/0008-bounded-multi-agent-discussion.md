# ADR-0008：有界多智能体讨论工作流

- 状态：Accepted
- 日期：2026-08-08
- 最近更新：2026-08-13
- 里程碑：M5.1
- 作者：Bryant Yang

## 1. 背景

现有显式多 mention 能让多个 worker 同轮并发回答，后续 `@host` 也能总结，
但用户必须手工推动每一轮。若把“还要不要继续、下一轮找谁”交给 LLM 自主决定，
会失去调用次数、失败终态、权限边界和取消语义的确定性，还可能形成 agent 相互
触发的无界循环。

本功能只解决本机聊天室内的有界讨论，不引入 A2A、后台自治任务或 agent 直接
互发消息。

## 2. 决策

### 2.1 触发与精确命令

普通消息可直接表达讨论意图：

```text
@agent1 @agent2 你们讨论两轮：主题
让合适的两个 agent 辩论这个主题，并由 host 总结
```

- 显式点名时，mention 顺序固定参与者闭集，不允许 host 替换或扩员。
- 未点名时，host 只能在当前 ready worker 闭集内返回一次结构化讨论计划。
- “讨论、辩论、互相点评、交叉评议”等明确交互线索才进入讨论；普通“一起
  分析、分别回答、各自建议”以及“不要/不用讨论”等明确否定表达仍是单轮
  fan-out。
- 明确跨 agent 的“先……再……”优先进入自然语言有序协作，不被后文的讨论词
  或并行词降级。
- 自然语言讨论默认两轮，moderator 固定为 `host`；人数、轮数、参与者和主题
  必须在写 timeline 前完成确定性校验。
- 自然语言显式轮数需要在“轮”后使用句末、空格、标点或“关于/这个”等主题
  边界，例如“讨论三轮：主题”；“三轮融资/一轮明月”等无边界复合词保留为
  主题并使用默认两轮。无歧义的机器输入使用 `/discuss --rounds N`。

需要精确选择轮数或未参会 moderator 时，仍可使用：

```text
/discuss @agent1 @agent2 [@agent3] [--rounds 1..3] [--moderator host|agent] -- 讨论主题
```

- 参与者必须是 `AGENT_SPECS` 中 2–3 个不同 worker；`host` 只能做主持人。
- 默认两轮、默认主持人为 `host`；主持人不能同时是参与者。
- 主题最多 3000 个字符，避免一次有界轮次被超大重复 prompt 放大。
- TUI 使用 `--` 分隔单行主题；MCP/API 也可在首行参数后换行提供多行主题。
  无效参数在写入 timeline 前拒绝；其他未知 slash 文本仍沿用普通消息语义。

### 2.2 确定性状态机

自然语言讨论与 `/discuss` 复用同一个确定性状态机。一场讨论是一个 CommandBus
command，只有一条真实 user timeline 记录：

```text
round 1：参与者并发独立提案
round 2..N：仍存活参与者并发交叉评议/收敛
final：moderator 单次仲裁，终止
```

轮次、目标、退出条件和主持人调用都由普通代码决定。agent 只生成本轮文字，
不能新增参与者、递归调用 `dispatch` 或启动下一轮。同轮使用现有 fan-out；跨轮
等待前一轮全部收尾，因此 stateful cursor 会把其他参与者的新回复作为增量交给
下一轮，同时过滤 agent 自己已经保存在原生 session 的回复。

内部 assignment 不伪装成 user 消息，也不额外写入 timeline；所有回复和唯一
user 记录共享同一个 `command_id`。

### 2.3 安全与失败

- 讨论 prompt 明确为只读文本活动，不修改文件、不执行命令、不调用工具、Skill
  或子 agent；现有 transport 权限仍保持 fail-closed，不能由讨论模式放宽。
- 任一参与者失败时，其他同轮参与者继续完成；失败者退出后续轮次，避免把一次
  不确定投递当成安全重试。
- 少于两个存活参与者时停止后续交叉轮，但仍让 moderator 对已有回复和失败占位
  做一次总结。
- 任一参与者或 moderator 失败都会进入 `DispatchOutcome.failures`，最终 command
  为 `failed`；主持人成功不能掩盖 worker 失败。
- 取消沿用 CommandBus 的单 command 取消：取消当前 round 的 gather 后不再进入
  后续 round 或 moderator，adapter 继续遵守原有 cancel/no-replay 契约。

## 3. 不选择的方案

- **agent 自主互相 @**：轮次和成本无上限，且会绕过 CommandBus 的单命令取消。
- **每轮提交一条内部 command**：会伪造用户消息、破坏 request_id 幂等与整体
  失败终态。
- **让 host 每轮动态选择参与者**：把确定性路由重新交给模型，无法保证指定成员
  真正参加或调用次数有界。
- **并发修改代码**：多个 agent 在同一 workdir 写入会产生竞态；本命令只负责
  讨论，实施仍应作为后续单独任务明确派发。

## 4. 验收

1. parser 覆盖合法命令、自然语言显式点名、未知/重复 agent、主持人冲突、缺失
   主题以及轮数/人数上下界；普通 fan-out 和有序协作文本不误判。
2. host 路由只能从 ready 闭集返回 2–3 个参与者、1–3 轮、host moderator；未知
   成员、畸形 JSON、布尔轮数和自定义 moderator 全部 fail-closed。
3. fake agents 证明同轮并发、跨轮可见其他参与者回复、只写一条 user 记录、
   全部记录共享一个 `command_id`，且最后只有一次 moderator 仲裁。
4. 参与者失败后不进入下一轮，moderator 仍总结，CommandBus 最终标记 failed。
5. R5 gate 固定人数/轮数硬上限并拒绝 `_dispatch_discussion` 递归 dispatch；
   负向探针能失败，恢复后通过。
6. TUI 对自然语言识别显示人数、轮数和 moderator；`/discuss` 候选和精确命令
   帮助仍可用。两种入口经同一 CommandBus，MCP 无需增加新方法即可提交。
7. 授权的真实模型用例通过 MCP bridge 执行 Kimi/OpenCode 两轮与 host 仲裁，
   timeline、原生 session、终态和进程清理均独立核对。真实模型不进默认 gate。

实际结果（2026-08-08）：在既有房间 `ccb04c39ae92bc88` 恢复原 Kimi/OpenCode
session 后提交 command `ce688171-43e4-401d-81a5-ee1ff9cc5d8f`。37 秒内完成
两轮 participant + 一轮 host；新增 `seq=6..11` 连续且 speaker 数量为
`user×1 / kimi×2 / opencode×2 / host×1`，所有记录共用 command id。两名
worker 的 session id 未变化、cursor 从 1 推进到 8；第二轮回复分别引用对方
首轮观点。事件没有 tool/permission，退出后控制文件与三个 agent 进程均无残留。

## 5. 后果

用户可以用一句自然语言或一条精确命令安排指定成员完成可恢复、可观察、可取消的
短讨论。最大模型调用
次数固定为 `3 participants × 3 rounds + 1 moderator = 10`。当前不提供投票、
动态增减成员、并发写代码或跨机器讨论；这些能力需要新的安全和状态契约。

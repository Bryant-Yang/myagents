# Agent 编排概念词汇表

按"你在这个项目里会在哪里碰到它"组织。每条一句话定义 + 为什么重要。

## 基础层：怎么驱动一个 agent

**无头模式（headless mode）**
agent CLI 的非交互用法：一条命令进去，结果打到 stdout，不带人机界面。
例：`kimi -p`、`claude -p`、`codex exec`、`opencode run`。
→ 这是一切程序化编排的前提；不能无头调用的 agent 只能靠 tmux/expect 硬糊。

**JSONL 事件流（streaming events）**
无头模式的结构化输出：stdout 每行一个 JSON，表示一个事件（正文片段、
工具调用、结束、token 用量）。逐行解析就能实时展示进度。
→ 对比"等全部跑完拿文本"：用户会对着空白终端怀疑人生。

**Adapter 模式**
为每家 CLI 包一层，对外暴露统一接口（本项目：`stream(prompt, workdir)`
→ `AgentEvent` 流）。编排器只依赖接口，不依赖任何具体 CLI。
→ 新增 agent 的成本 = 一个文件；换掉某家不伤筋动骨。

**Session resume（会话恢复）**
每次无头调用默认开新会话；带上上次返回的 session id 可延续上下文
（kimi `-S`、opencode `--session`、claude `--resume`、codex `exec resume`）。
→ 省 token、保持 agent 的"记忆"，但各家会话语义不一致，管理成本高。

## 编排层：怎么让多个 agent 协作

**Orchestrator（编排器）**
决定"谁该收到什么上下文、什么时候跑、结果贴回哪里"的中心组件。
本项目的 `orchestrator.py` 就是最小形态。

**Hub-and-Spoke（中心辐射）**
agent 之间从不直接通信，全部由编排器中转。
→  alternative 是 peer-to-peer（agent 互相调），灵活但容易死循环、
上下文碎片化。初学先把 hub-and-spoke 做扎实。

**Supervisor（中心协调者 / 主持人）**
中心节点本身也是一个 agent：用户没点名时，由它读懂消息、决定派给谁，
还能总结讨论、仲裁分歧。本项目的 `host.py` 就是最小实现。
→ 对应 AutoGen GroupChat Manager、LangGraph supervisor pattern。代价是
每条无 @ 消息多一次 LLM 调用，所以"显式 @ 优先于主持人判断"是保命的
兜底规则。

**Transcript 转发**
把聊天历史塞进 prompt 发给 agent，让它"看到"之前的对话。
→ 最朴素的上下文共享。缺点：token 线性膨胀，超长要截断/摘要。

**Fan-out / Fan-in（扇出/扇入）**
一条消息同时派给多个 agent（fan-out），各自独立执行，结果陆续收回
（fan-in）。本项目用 `asyncio.gather` 实现。
→ 对比"级联（pipeline）"：A 的输出作为 B 的输入，顺序执行。两种拓扑
解决不同问题：并发比对用扇出，分工接力用级联。本项目的自然语言有序协作
会把 host 提取的 2–4 步固定计划交给 `collaboration.py` / Orchestrator 串行推进，
而不是让 agent 自己决定下一个参与者。

**Steering（运行中插话）**
agent 还在干活时追加新指令（"方向变了，别改那个文件"）。
→ 需要 agent 侧有注入消息的通道（pi 的 `getSteeringMessages`、
ACP 的 session/update），比"杀掉重跑"高级，MVP 之后再做。

**Subagent（子代理）**
一个 agent 内部分裂出的临时 worker：主 agent 把子任务连上下文一起
委托出去，收回结论。你在用的 kimi 的 Agent 工具、claude 的 Task 工具都是。
→ 与"多 agent 编排"是同构问题，只是一个在进程内、一个跨进程。

## 协议层：agent 之间的标准接口

**MCP（Model Context Protocol）**
Anthropic 主导的协议：把**工具/数据源**标准化地暴露给 agent
（"我给你三个工具：读邮件、查日历、发消息"）。agent 是 MCP client。
→ 解决的是"agent 用什么工具"，粒度细。

**ACP（Agent Client Protocol）**
Zed 主导的协议：把 **agent 本身**标准化成服务（"我给你一个会话，
可以 prompt、可以中断、会流式回报进展"）。编辑器/编排器是 client。
kimi（`kimi acp`）、OpenCode（`opencode acp`）和 Qwen Code
（`qwen --acp`）均有原生入口；Codex 在本项目使用官方 app-server。
→ 解决的是"怎么驱动另一个 agent"，正好是本项目的整合方向：
多个具体 adapter 共享同一个 ACP client/runtime。

**A2A（Agent2Agent，Google）**
agent 对 agent 的任务委托协议，面向"企业内部跨系统 agent 协作"。
→ 知道名字即可，做终端工具时用不上。

## 安全与验收

**Sandbox / 权限模式**
限制 agent 能动什么：codex 有 `--sandbox read-only|workspace-write`，
claude 有 `--allowedTools`，kimi/opencode 无头模式默认放开。
→ 评审类任务给只读，干活才给写权限。

**Worktree 隔离**
`git worktree add` 给每个 agent 一个独立工作目录+分支，并行改代码
不互踩，完事再合并。
→ 多 agent 同时改同一仓库的标准解法。

**git 验收纪律**
agent 跑完用 `git diff` 验收。多条纪律（借自 pi 的 AGENTS.md）：
只提交自己改的文件、`git add` 用显式路径、禁 `git add -A` / `reset --hard`。

## 同赛道的现有项目（值得读源码）

**claude-squad**（smtg-ai/claude-squad）
在 tmux 里同时管理多个终端 agent（claude code、aider、codex…），
每个 agent 一个独立 worktree，面板式切换查看。
→ 思路是"每 agent 一个真终端 + 工作区隔离"，和本项目"无头调用 +
单时间线"是两种流派：前者保真度高（agent 的完整交互能力都在），
后者可编程性强（消息可路由、可复制、可持久化）。

**Vibe Kanban**（BloopAI/vibe-kanban）
看板式多 agent 任务编排：每张卡片是一个任务，指派给某个 agent
（支持 claude code、codex、gemini、opencode 等），agent 在独立
worktree 干活，你在 diff 视图里验收、合并。
→ 是"任务管理"视角的编排：重点不在聊天而在工单流转与代码验收。

**pi**（earendil-works/pi，本目录 ../pi）
自扩展的 coding agent + 多 provider 抽象 + TUI 库 + 多实例 RPC 编排器
（`packages/server`）。接口设计和子进程协议是本项目的直接参照。

## 模型侧常识（编排时绕不开）

**上下文窗口（context window）/ token**
agent 每次调用能"看到"的文本总量有限，按 token 计费。transcript 转发
方案下对话越长每次调用越贵——所以 session resume 和上下文裁剪
（compaction）是必修课。

**Compaction（上下文压缩）**
对话逼近窗口上限时，把旧历史总结成摘要替换原文，保住近期细节。
各家 CLI 内部都在做；编排层转发 transcript 时同样需要（本项目二期）。

**Agentic loop（代理循环）**
agent 的基本工作方式：模型输出 → 调工具 → 拿结果 → 再输出……直到
给出最终答复。无头模式把这一整个循环跑完，JSONL 事件流就是循环的轨迹。

"""Orchestrator：整个系统的大脑。

关键概念（学习要点）：
- **Hub-and-spoke（中心辐射）**：agent 之间从不直接对话。所有消息先进
  共享时间线（history），@谁就把上下文打包发给谁，回复再贴回时间线。
  这样不会有"两个 agent 互相@死循环"，也只有一个地方需要管上下文。
- **两种传输，两种上下文策略**（AgentSpec.transport）：
  - JSONL adapter：无头调用每次是全新会话，agent 看不到聊天记录，
    把最近 N 条对话记录塞进 prompt 一起发（transcript 快照）。
  - ACP adapter（`stateful_session`）：session 在 agent 侧保持，编排器
    只发**增量**——每个 agent 一个 history cursor，记录已交付到哪儿；
    每轮只发 cursor 之后的新消息（跳过 agent 自己的回复，那些本来就在
    它的 ACP session 里；首次 bootstrap 限发最近 N 条）。读 cursor →
    选增量 → stream → 推进 cursor 在每-agent delivery lock 内原子完成，
    同一 agent 的并发 dispatch 严格串行；cursor 只在成功交付后推进，
    失败不推进，下一轮补发增量，不丢上下文。
- **并发扇出（fan-out）**：一条消息 @多个 agent 时并行派发，互不等待。
- **Supervisor（中心协调者）**：注册表里的 host 是一个由 LLM 扮演的
  主持人。显式 @ 永远优先；用户没点名时，由 host 做 LLM 路由决定
  派发给谁（见 host.py）。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Callable

from acp.adapter import AcpKimiAdapter, AgentPermissionHandler
from adapters.base import AgentAdapter, AgentEvent
from adapters.codex_adapter import CodexAdapter
from adapters.opencode_adapter import OpenCodeAdapter
from host import MODERATOR_TEMPLATE, HostAgent

HOST_NAME = "host"

_MENTION_RE = re.compile(r"@(\w+)")


@dataclass(frozen=True)
class AgentSpec:
    """一个工人 agent 的注册信息：名字 + 传输协议 + adapter 工厂。

    transport:
      - "acp"   有状态会话（ACP 协议），编排器发增量上下文
      - "jsonl" 无头一次性调用，编排器发完整 transcript 快照

    新增 agent = 在这里加一行（写一个 adapter 类）。协议判断只看
    transport / adapter 能力声明，不散落 `if name == ...`。
    """

    name: str
    transport: str  # "acp" | "jsonl"
    factory: Callable[[], AgentAdapter]


# 工人 agent 注册表：ACP-first，JSONL 保留为 fallback。
# Kimi 是首个生产 ACP agent（命令 ["kimi", "acp"]）；
# Codex/OpenCode 继续走 JSONL adapter。
AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec("kimi", "acp", AcpKimiAdapter),
    AgentSpec("opencode", "jsonl", OpenCodeAdapter),
    AgentSpec("codex", "jsonl", CodexAdapter),
)
AGENTS: dict[str, AgentSpec] = {spec.name: spec for spec in AGENT_SPECS}

# 发给 JSONL agent 的 prompt 模板：身份 + 对话记录 + 工作目录约定
_PROMPT_TEMPLATE = """\
你在一个名叫 myagents 的多 agent 聊天室里，身份是 "{name}"。
房间里有一个人类用户，可能还有其他 AI agent。
以下是最近的对话记录（格式 [发言者] 内容）：

{transcript}

请作为 {name}，针对用户最新的消息给出回复或执行其中的任务。
要求：直接输出内容，不要自我介绍，不要复述上面的记录。
如需读写文件、运行命令，都在当前目录内进行。
"""

# 发给 ACP agent 的 prompt 模板：session 原生记忆完整上下文，
# 只补"你上次被派发之后"的新消息（含其他 agent 的发言）
_ACP_PROMPT_TEMPLATE = """\
你在一个名叫 myagents 的多 agent 聊天室里，身份是 "{name}"。
房间里有一个人类用户，可能还有其他 AI agent。你的会话保持着完整上下文，
以下是自上次派发给你之后的新消息（格式 [发言者] 内容）：

{transcript}

请作为 {name}，针对最新的消息给出回复或执行其中的任务。
要求：直接输出内容，不要自我介绍，不要复述上面的记录。
如需读写文件、运行命令，都在当前目录内进行。
"""


@dataclass
class Message:
    speaker: str  # "user" 或 agent 名
    text: str


# on_event(agent_name, event) —— UI 层传进来的回调
EventCallback = Callable[[str, AgentEvent], None]


class Orchestrator:
    def __init__(self, workdir: str, history_limit: int = 12,
                 specs: tuple[AgentSpec, ...] = AGENT_SPECS) -> None:
        self.workdir = workdir
        self.history_limit = history_limit
        self.specs = specs
        self.adapters: dict[str, AgentAdapter] = {
            spec.name: spec.factory() for spec in specs
        }
        # 主持人也注册进 adapters：@host 时和工人走同一条派发路径，
        # 只是 _build_prompt 会给它主持人角色的 prompt。
        # 主持人由 codex 扮演，用 read-only 沙箱：总结/仲裁/路由只需要看，不需要写。
        self.host = HostAgent(adapter=CodexAdapter(sandbox="read-only"),
                              workers=[spec.name for spec in specs])
        self.adapters[HOST_NAME] = self.host
        self.history: list[Message] = []
        # 每个有状态（ACP）agent 的 history cursor：已交付到 history 的哪个
        # 位置（下标，左闭右开）。只增不减；只在成功交付后推进。
        self._cursors: dict[str, int] = {}
        # 每-agent delivery lock：读 cursor → 选增量 → stream 完整执行 →
        # 推进 cursor 是一个原子单元。没有它，两个针对同一 agent 的并发
        # dispatch 会都按旧 cursor 构造 prompt，重复/乱序投递。
        self._delivery_locks: dict[str, asyncio.Lock] = {}

    # ---- 注册信息 / 生命周期 ----

    def set_permission_handler(self, handler: AgentPermissionHandler | None) -> None:
        """把 TUI 的权限决策器注入所有支持它的 adapter（ACP）。
        handler 签名：async (agent_name, params) -> outcome。
        未注入时 ACP 一律 deny——这是安全契约，不是默认值偷懒。"""
        for adapter in self.adapters.values():
            setter = getattr(adapter, "set_permission_handler", None)
            if setter is not None:
                setter(handler)

    async def aclose(self) -> None:
        """统一关闭所有支持 aclose() 的 adapter（ACP 长驻进程在此回收）。
        adapter 内部用同一把锁保证 close 不与进行中的 prompt 竞态。"""
        await asyncio.gather(
            *(adapter.aclose() for adapter in self.adapters.values()
              if hasattr(adapter, "aclose")),
            return_exceptions=True,  # 一个关不掉不耽误其他的回收
        )

    # ---- 路由 ----

    def parse_mentions(self, text: str) -> list[str]:
        """从消息里提取 @agent，按出现顺序去重，忽略未注册的名字。"""
        seen: list[str] = []
        for name in _MENTION_RE.findall(text):
            if name in self.adapters and name not in seen:
                seen.append(name)
        return seen

    # ---- 上下文组装 ----

    def _delivery_lock(self, name: str) -> asyncio.Lock:
        lock = self._delivery_locks.get(name)
        if lock is None:
            lock = self._delivery_locks[name] = asyncio.Lock()
        return lock

    @staticmethod
    def _format(messages: list[Message]) -> str:
        if not messages:
            return "（暂无记录）"
        return "\n".join(f"[{m.speaker}] {m.text[:2000]}" for m in messages)

    def _messages_for(self, name: str) -> tuple[list[Message], int]:
        """有状态 agent 本轮的增量消息 + 交付目标位置（快照末尾下标）。

        必须在 delivery lock 内调用：cursor 读取、增量选择、推进是一个
        原子单元，锁外读到的 cursor 可能已被并发轮次推进。

        - 增量：cursor 之后的新消息，跳过 agent 自己的回复（那些已经在
          它的 ACP session 里，重发就是重复上下文）。
        - bootstrap 限界：cursor=0 的首次派发不发全部历史，只发最近
          history_limit 条，避免长聊天后第一次 @ 就无界发送。
        """
        snapshot = list(self.history)
        cursor = self._cursors.get(name, 0)
        start = min(max(cursor, len(snapshot) - self.history_limit), len(snapshot))
        msgs = [m for m in snapshot[start:] if m.speaker != name]
        if not msgs:
            # 没有新消息也要给出触发点（快照窗口里可能只剩自己的发言）
            msgs = [m for m in snapshot if m.speaker != name][-1:]
        return msgs, len(snapshot)

    def _build_prompt(self, agent_name: str, messages: list[Message]) -> str:
        transcript = self._format(messages)
        if agent_name == HOST_NAME:
            template = MODERATOR_TEMPLATE
        elif getattr(self.adapters[agent_name], "stateful_session", False):
            template = _ACP_PROMPT_TEMPLATE
        else:
            template = _PROMPT_TEMPLATE
        return template.format(name=agent_name, transcript=transcript)

    # ---- 派发 ----

    async def dispatch(self, user_text: str, on_event: EventCallback) -> None:
        """处理一条用户消息：记录 → 路由 → 并发派发 → 收回回复。

        路由规则：**显式 @ 永远优先**；用户没点名时交给 host 做 LLM 路由
        （它可能派给工人，也可能自己回答）。
        """
        self.history.append(Message("user", user_text))
        # 快照（在第一个 await 之前）：等待 host 路由或 agent 启动期间，
        # 用户可能又提交了新消息进 history。JSONL agent 和 host 路由的
        # prompt 必须用快照拼接，否则并发消息会串话（agent 执行错任务）。
        # ACP agent 不走这个快照——它的增量起点（cursor）在 delivery lock
        # 内、拿到锁之后才读取，见 _run_one。
        snapshot = list(self.history)
        snapshot_text = self._format(snapshot[-self.history_limit:])
        targets = self.parse_mentions(user_text)
        if not targets:
            # decide 不在 _run_one 的异常保护里，自己兜底。契约：
            # 路由失败 → error 事件 + 确定性回退到第一个工人（可用性
            # 最高的路径）。绝不回退到 host adapter 本身——路由失败
            # 大概率就是它挂了，再调一次只会伪造出"无文本回复"。
            try:
                targets, reason = await self.host.decide(snapshot_text, self.workdir)
            except Exception as exc:
                on_event(HOST_NAME, AgentEvent("error", f"host 路由失败：{exc}"))
                targets = [self.specs[0].name]
                reason = f"路由失败，回退到 {targets[0]}"
            on_event(HOST_NAME, AgentEvent(
                "info", f"路由 → {'、'.join(targets)}（{reason}）"))
        await asyncio.gather(
            *(self._run_one(name, on_event, snapshot) for name in targets)
        )

    async def _run_one(self, name: str, on_event: EventCallback,
                       snapshot: list[Message]) -> None:
        adapter = self.adapters[name]
        if getattr(adapter, "stateful_session", False):
            # P1 契约：读 cursor → 选增量 → stream 完整执行 → 推进 cursor
            # 必须在该 agent 的 delivery lock 内完成，prompt 拿到锁之后才
            # 构造——否则并发的两个 dispatch 都按旧 cursor 构造 prompt，
            # 第二轮会重复/乱序投递。不同 agent 持不同的锁，并行扇出不受影响。
            async with self._delivery_lock(name):
                messages, delivered_upto = self._messages_for(name)
                failed = await self._deliver(name, adapter, messages, on_event)
                if failed is None:
                    # 成功交付后才推进 cursor，且只推进到本轮构造时的快照
                    # 末尾：1) 失败不推进——下轮从旧 cursor 补发，不丢上下文；
                    # 2) 本轮进行期间到达的新消息没发出去，必须留给下一轮；
                    # 3) 自己的回复靠 speaker 过滤跳过（见 _messages_for）。
                    self._cursors[name] = max(
                        self._cursors.get(name, 0), delivered_upto)
        else:
            # 无状态（JSONL）agent：dispatch 瞬间的 transcript 快照
            await self._deliver(name, adapter,
                                snapshot[-self.history_limit:], on_event)

    async def _deliver(self, name: str, adapter: AgentAdapter,
                       messages: list[Message],
                       on_event: EventCallback) -> str | None:
        """执行一轮并把结果写回 history；返回失败信息（None = 成功）。"""
        prompt = self._build_prompt(name, messages)
        parts: list[str] = []
        failed: str | None = None
        try:
            async for ev in adapter.stream(prompt, self.workdir):
                on_event(name, ev)
                if ev.kind == "text":
                    parts.append(ev.text)
        except Exception as exc:  # agent 崩溃不拖垮整个聊天室
            failed = str(exc)
            on_event(name, AgentEvent("error", failed))
        # history 必须诚实：有回复记回复，失败了记失败，
        # "无文本回复"只留给调用成功但真没说话的情况。
        reply = "".join(parts).strip()
        if reply:
            self.history.append(Message(name, reply))
        elif failed is not None:
            self.history.append(Message(name, f"（调用失败：{failed[:120]}）"))
        else:
            self.history.append(Message(name, "（无文本回复）"))
        return failed

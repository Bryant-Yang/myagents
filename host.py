"""HostAgent：聊天室的中心协调者（supervisor）。

关键概念（学习要点）：
- **Supervisor 模式**：工人 agent（kimi/opencode）只负责干活和回答，
  主持人负责"理解消息 → 决定谁来处理"。这是 AutoGen GroupChat Manager、
  LangGraph supervisor pattern 的最小实现。
- **LLM 路由**：不带 @ 的消息交给主持人判断。它只输出一行 JSON：
  {"targets": ["kimi"], "reason": "..."}，orchestrator 按指令派发。
  **显式 @ 永远优先**——主持人的判断只在用户没点名时生效。
- **代价**：每条无 @ 消息多一次 LLM 调用（延迟 + token），且 LLM 判断
  不如规则确定，所以路由指令必须可解析、可校验、有兜底（解析失败就自己答）。
- 主持人本身也是一个普通 adapter（默认 kimi），只是 prompt 角色不同。
  它也注册进 AGENTS，@host 可以直接叫它出来总结、仲裁、回答元问题。
"""

from __future__ import annotations

import json
import re
from typing import AsyncIterator

from adapters.base import AgentAdapter, AgentEvent
from adapters.kimi_adapter import KimiAdapter

# 路由指令 prompt：要求严格输出一行 JSON
_ROUTE_TEMPLATE = """\
你是多 agent 聊天室的主持人。聊天室里有人类用户、你自己（host），
以及这些干活的 agent：{workers}。

对话记录（格式 [发言者] 内容）：

{transcript}

请判断用户最新的消息应该由谁来处理。只输出一行 JSON，不要输出任何其他文字：
{{"targets": ["{first_worker}"], "reason": "一句话理由"}}

规则：
- targets 从 [{choices}] 中选 1~2 个，reason 用中文一句话
- 需要写代码、改文件、跑命令、调研具体技术问题 → 选干活的 agent
- 总结对话、比较/仲裁几个 agent 的回答、一般性讨论、闲聊 → 选 "host" 自己答
- 拿不准 → 选 "host"
"""

# 主持人作为参会者发言时的 prompt（@host 或路由结果是 host 时用）
MODERATOR_TEMPLATE = """\
你在一个名叫 myagents 的多 agent 聊天室里，身份是主持人 "host"。
你掌握全部对话记录，职责是：总结讨论、仲裁不同 agent 之间的分歧、
回答"该找谁/怎么办"这类元问题，以及处理一般性讨论。
对话记录（格式 [发言者] 内容）：

{transcript}

请以主持人身份回应用户最新的消息。直接输出内容，不要自我介绍，不要复述记录。
如需读写文件、运行命令，都在当前目录内进行。
"""

_JSON_RE = re.compile(r"\{.*\}", re.S)


class HostAgent:
    """supervisor：包一个普通 adapter，多一个 decide() 路由能力。"""

    name = "host"

    def __init__(self, adapter: AgentAdapter | None = None,
                 workers: list[str] | None = None) -> None:
        self.adapter = adapter or KimiAdapter()
        self.workers = workers or []

    @property
    def session_id(self) -> str | None:  # 满足 AgentAdapter 协议
        return self.adapter.session_id

    async def decide(self, transcript: str, workdir: str) -> tuple[list[str], str]:
        """LLM 路由：返回 (targets, reason)。解析失败兜底为自己回答。"""
        choices = self.workers + ["host"]
        prompt = _ROUTE_TEMPLATE.format(
            workers="、".join(self.workers),
            first_worker=self.workers[0] if self.workers else "host",
            choices=", ".join(choices),
            transcript=transcript,
        )
        buf: list[str] = []
        async for ev in self.adapter.stream(prompt, workdir):
            if ev.kind == "text":
                buf.append(ev.text)
        return self._parse("".join(buf), choices)

    def _parse(self, raw: str, choices: list[str]) -> tuple[list[str], str]:
        m = _JSON_RE.search(raw)
        if m:
            try:
                data = json.loads(m.group(0))
                # 校验 + 按序去重：LLM 可能返回 ["kimi", "kimi"]，
                # 不去重会在同一工作目录并发跑两遍相同任务
                targets: list[str] = []
                for t in data.get("targets", []):
                    if t in choices and t not in targets:
                        targets.append(t)
                reason = str(data.get("reason", ""))[:80]
                if targets:
                    return targets[:2], reason or "主持人判断"
            except (json.JSONDecodeError, AttributeError):
                pass
        return ["host"], "路由解析失败，主持人自己回答"

    async def stream(self, prompt: str, workdir: str) -> AsyncIterator[AgentEvent]:
        """让 host 也能像普通 agent 一样被 dispatch（@host 时走这条路）。"""
        async for ev in self.adapter.stream(prompt, workdir):
            yield ev

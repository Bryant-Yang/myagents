"""HostAgent：聊天室的中心协调者（supervisor）。

关键概念（学习要点）：
- **Supervisor 模式**：工人 agent（kimi/opencode）只负责干活和回答，
  主持人负责"理解消息 → 决定谁来处理"。这是 AutoGen GroupChat Manager、
  LangGraph supervisor pattern 的最小实现。
- **单次判断或回答**：不带 @ 的消息交给主持人处理。需要 worker 时输出
  路由 JSON；主持人能处理时直接给最终回答。
  **显式 @ 永远优先**——主持人的判断只在用户没点名时生效。
- **代价**：需要 worker 的无 @ 消息仍比显式 @ 多一次 LLM 路由调用；
  主持人直接回答的消息只调用一次。路由 JSON 必须可解析、可校验、有兜底。
- 主持人本身也是一个普通 adapter（默认 kimi），只是 prompt 角色不同。
  它也注册进 AGENTS，@host 可以直接叫它出来总结、仲裁、回答元问题。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from adapters.base import AgentAdapter, AgentEvent, ExecutionMode
from adapters.kimi_adapter import KimiAdapter
from session_roles import SessionRoleChanges

# host 一次调用完成二选一：需要 worker 才输出路由 JSON；能自己处理就直接回答。
# 这样闲聊、问候、总结等不再经历“先路由给 host，再调用 host 回答”的双重延迟。
_ROUTE_TEMPLATE = """\
你是多 agent 聊天室的主持人。聊天室里有人类用户、你自己（host），
以及这些干活的 agent：{workers}。

对话记录（格式 [发言者] 内容）：

{transcript}

请直接处理用户最新的消息，只能二选一：
1. 需要写代码、改文件、跑命令、查看图片附件、调研具体技术问题，或明确要求某个
   agent 在当前会话担任/取消角色：只输出一行 JSON，
   不要输出其他文字：
   {{"targets": ["{first_worker}"], "reason": "一句话理由",
     "tasks": {{"{first_worker}": "直接交给该 agent 的完整、可执行任务"}},
     "role_changes": {{"set": {{}}, "clear": []}}}}
   targets 从 [{choices}] 中选 1~2 个。
   tasks 必须为每个 target 提供一条完整指令，直接对该 agent 说话，并消解
   原消息中的“你”“让某人做”等角色关系。任务同时包含构思、实现、验证时，
   把这些步骤全部写进指令；除非存在真实阻塞，不要让 agent 再向用户确认方案。
   仅当用户明确要求某个 target 在当前会话中担任或取消角色时，填写
   role_changes：set 的值必须包含简短 label 和忠实复述的 instructions；clear
   只列需要取消角色的 target。不得推测、扩展或改变 targets。
2. 你自己可以处理（包括问候、闲聊、一般讨论、总结、比较和仲裁）：
   直接输出给用户的最终回答，不要输出 JSON，不要自我介绍或复述记录。

这是纯路由与任务改写步骤：禁止调用工具、命令、文件、网络或 skill。
拿不准时直接回答。
"""

_ROLE_EXTRACTION_TEMPLATE = """\
你只负责从用户原文中提取当前聊天室会话的角色变化。
候选 agent：{choices}
用户原文：
{text}

只输出一行 JSON，不要输出其他文字：
{{"set": {{"agent": {{"label": "简短角色名", "instructions": "忠实复述职责"}}}},
  "clear": ["agent"]}}

规则：
- 只能使用候选 agent；不得新增参与者、任务、权限、工具或轮次。
- 只有用户明确指定角色时才 set；只有明确取消角色时才 clear；不要自行推断。
- label 最多 40 字，instructions 最多 1000 字，均为单行自然语言。
- 这是纯语义提取：禁止调用工具、命令、文件、网络或 skill。
- 没有有效变化时输出 {{"set": {{}}, "clear": []}}。
"""

# 主持人被 `@host` 显式点名时使用的 prompt。
MODERATOR_TEMPLATE = """\
你在一个名叫 myagents 的多 agent 聊天室里，身份是主持人 "host"。
你掌握全部对话记录，职责是：总结讨论、仲裁不同 agent 之间的分歧、
回答"该找谁/怎么办"这类元问题，以及处理一般性讨论。
对话记录（格式 [发言者] 内容）：

{transcript}

{assignment}
请以主持人身份回应用户最新的消息。直接输出内容，不要自我介绍，不要复述记录。
如需读写文件、运行命令，都在当前目录内进行。
"""

_JSON_RE = re.compile(r"\{.*\}", re.S)


@dataclass(frozen=True)
class HostDecision:
    """host 单次调用的结果：派发给 workers，或直接给出最终回答。"""

    targets: list[str]
    reason: str
    answer: str | None = None
    tasks: dict[str, str] = field(default_factory=dict)
    role_changes: SessionRoleChanges = field(
        default_factory=SessionRoleChanges.empty)


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

    async def decide(
            self, transcript: str, workdir: str,
            on_event: Callable[[AgentEvent], None] | None = None,
    ) -> HostDecision:
        """一次 LLM 调用返回路由/回答，并公开安全的非正文进度事件。"""
        choices = self.workers
        prompt = self._build_route_prompt(transcript, choices)
        buf: list[str] = []
        async for ev in self.adapter.stream(prompt, workdir):
            if ev.kind == "text":
                buf.append(ev.text)
            elif ev.kind not in {"done", "delivery_committed"}:
                if on_event is not None:
                    on_event(ev)
        return self._parse("".join(buf), choices)

    async def extract_session_roles(
        self,
        text: str,
        choices: list[str],
        workdir: str,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> SessionRoleChanges:
        """在固定候选闭集内提取角色变化，不参与路由。"""
        prompt = _ROLE_EXTRACTION_TEMPLATE.format(
            choices="、".join(choices),
            text=text[:12000],
        )
        parts: list[str] = []
        async for event in self.adapter.stream(prompt, workdir):
            if event.kind == "text":
                parts.append(event.text)
            elif event.kind not in {"done", "delivery_committed"}:
                if on_event is not None:
                    on_event(event)
        return SessionRoleChanges.from_model_output(
            "".join(parts), choices)

    def _build_route_prompt(
            self, transcript: str, choices: list[str] | None = None) -> str:
        """构造纯路由 prompt；保留为可直接测试的协议边界。"""
        selected = self.workers if choices is None else choices
        return _ROUTE_TEMPLATE.format(
            workers="、".join(self.workers),
            first_worker=selected[0] if selected else "host",
            choices=", ".join(selected),
            transcript=transcript,
        )

    def _parse(self, raw: str, choices: list[str]) -> HostDecision:
        """有效路由 JSON 才派发；其余非空输出就是 host 的最终回答。"""
        raw = raw.strip()
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
                    raw_tasks = data.get("tasks", {})
                    tasks: dict[str, str] = {}
                    if isinstance(raw_tasks, dict):
                        for target in targets[:2]:
                            task = raw_tasks.get(target)
                            if isinstance(task, str) and task.strip():
                                tasks[target] = task.strip()[:4000]
                    role_changes = SessionRoleChanges.from_payload(
                        data.get("role_changes", {}), targets[:2])
                    return HostDecision(
                        targets[:2], reason or "主持人判断",
                        tasks=tasks,
                        role_changes=role_changes)
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
        if raw:
            return HostDecision([], "host 直接回答", raw)
        # 空输出没有可展示答案；确定性回退到首个 worker 保持可用性。
        targets = choices[:1]
        return HostDecision(targets, "host 无输出，回退到 worker")

    async def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        """让 host 也能像普通 agent 一样被 dispatch（@host 时走这条路）。"""
        if execution_mode is ExecutionMode.DEFAULT:
            stream = self.adapter.stream(prompt, workdir)
        else:
            stream = self.adapter.stream(
                prompt, workdir, execution_mode=execution_mode)
        async for ev in stream:
            yield ev

    def set_permission_handler(self, handler) -> None:
        """把通用权限处理器转发给底层 adapter，并保留 host 身份。"""
        setter = getattr(self.adapter, "set_permission_handler", None)
        if setter is not None:
            if handler is None:
                setter(None)
            else:
                setter(lambda _agent_name, params: handler(self.name, params))

    def set_attachment_root(self, root) -> None:
        """把当前房间附件信任根转发给底层 transport。"""
        setter = getattr(self.adapter, "set_attachment_root", None)
        if setter is not None:
            setter(root)

    async def aclose(self) -> None:
        """回收底层长驻 transport；无生命周期能力的旧 adapter 无操作。"""
        closer = getattr(self.adapter, "aclose", None)
        if closer is not None:
            await closer()

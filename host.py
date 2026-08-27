"""HostAgent：聊天室的中心协调者（supervisor）。

关键概念（学习要点）：
- **Supervisor 模式**：工人 agent（kimi/opencode）只负责干活和回答，
  主持人负责"理解消息 → 决定谁来处理"。这是 AutoGen GroupChat Manager、
  LangGraph supervisor pattern 的最小实现。
- **单次判断或回答**：不带 @ 的消息交给主持人处理。需要 worker 时输出
  并行路由或有序协作 JSON；主持人能处理时直接给最终回答。
  **显式 @ 永远优先**——主持人的判断只在用户没点名时生效。
- **代价**：需要 worker 的无 @ 消息仍比显式 @ 多一次 LLM 路由调用；
  主持人直接回答的消息只调用一次。路由 JSON 必须可解析、可校验、有兜底。
- 主持人本身也是一个普通 adapter（默认 myagents 原生模型 runtime），只是
  prompt 角色不同。
  它也注册进 AGENTS，@host 可以直接叫它出来总结、仲裁、回答元问题。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from adapters.base import AgentAdapter, AgentEvent, ExecutionMode
from collaboration import (
    CollaborationPlan,
    CollaborationValidationError,
    parse_collaboration_payload,
)
from discussion import (
    DiscussionRequest,
    DiscussionValidationError,
    parse_discussion_payload,
)
from native_agent import NativeSessionPreparation, create_native_host_runtime
from session_roles import SessionRoleChanges

# host 一次调用完成四选一：讨论、并行路由、有序协作，或直接回答。
# 这样闲聊、问候、总结等不再经历“先路由给 host，再调用 host 回答”的双重延迟。
_ROUTE_TEMPLATE = """\
你是多 agent 聊天室的主持人。聊天室里有人类用户、你自己（host），
以及这些干活的 agent：{workers}。

对话记录（格式 [发言者] 内容）：

{transcript}

请直接处理用户最新的消息，只能选择以下四种结果之一：
1. 用户明确要求多个 agent 互相讨论、辩论、交叉评议或达成共识：只输出一行
   JSON：
   {{"discussion": {{"participants": ["{first_worker}", "另一候选 agent"],
     "rounds": 2, "moderator": "host", "topic": "忠实提取的讨论主题"}},
     "reason": "一句话理由"}}
   participants 只能从 [{choices}] 选择 2~3 个不同 agent；rounds 只能是 1~3，
   用户没明确说轮数时必须为 2；moderator 必须是 host。只有“互相讨论/辩论/
   交叉评议”等需要读取对方观点的请求才使用此模式；“分别回答/各自分析”不是讨论。
2. 用户要求多个 agent 按先后依赖接力完成同一件事：只输出一行 JSON：
   {{"collaboration": {{"steps": [
     {{"agent": "{first_worker}", "assignment": "该步骤的完整任务"}},
     {{"agent": "另一候选 agent", "assignment": "基于前序结果完成最终交付"}}
   ]}}, "reason": "一句话理由",
   "role_changes": {{"set": {{}}, "clear": []}}}}
   steps 必须有 2~4 步、至少两个不同 agent，agent 只能来自 [{choices}]；可以让
   同一 agent 在后续步骤再次出现。最后一步必须直接产出面向用户的最终交付。
   仅当用户同时明确设置/取消会话角色时填写 role_changes，不得自行推断。
3. 需要写代码、改文件、跑命令、查看图片附件、调研具体技术问题，或明确要求某个
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
4. 你自己可以处理（包括问候、闲聊、一般讨论、总结、比较和仲裁）：
   直接输出给用户的最终回答，不要输出 JSON，不要自我介绍或复述记录。

这是纯路由与任务改写步骤：禁止调用工具、命令、文件、网络或 skill。
拿不准时直接回答。
"""

_NO_WORKER_ROUTE_TEMPLATE = """\
你是多 agent 聊天室的主持人 host。当前没有可派发的 worker。

对话记录（格式 [发言者] 内容）：

{transcript}

请直接回答用户，不要输出路由 JSON。如果请求必须由 worker 执行，请简洁说明
当前没有就绪 worker，并请用户使用 /agents 查看状态、修复后执行
/agents rescan。禁止调用工具、命令、文件、网络或 skill。
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

_COLLABORATION_EXTRACTION_TEMPLATE = """\
你只负责把用户明确表达的多 agent 先后接力转换成有界执行计划。
候选 agent（固定闭集）：{choices}
用户原文：
{text}

只输出一行 JSON，不要输出其他文字：
{{"steps": [
  {{"agent": "候选 agent", "assignment": "该步骤完整、可执行的任务"}},
  {{"agent": "候选 agent", "assignment": "基于前序结果完成最终交付"}}
], "role_changes": {{"set": {{}}, "clear": []}}}}

规则：
- 只能使用全部候选 agent，不能遗漏、增员或改名；允许某个候选在后续再次出现。
- 只能有 2~4 步，严格忠实于用户要求的先后关系。
- 每步 assignment 必须独立完整；最后一步必须面向用户产生最终交付。
- 仅当原文同时明确设置或取消会话角色时填写 role_changes；不得自行推断。
- 不得添加重试、动态路由、权限、工具或用户未要求的工作。
- 这是纯语义提取：禁止调用工具、命令、文件、网络或 skill。
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
你没有工具，禁止读写文件、运行命令、访问网络或调用 skill；需要执行这些动作时，
只能基于已有记录提出建议或明确指出应交给 worker，不得声称已经执行。
"""

_JSON_RE = re.compile(r"\{.*\}", re.S)
_COLLABORATION_MARKER_RE = re.compile(
    r"(?:\{|,)\s*[\"']?collaboration[\"']?\s*(?::|\}|\{|$)",
    re.IGNORECASE,
)
_DISCUSSION_MARKER_RE = re.compile(
    r"(?:\{|,)\s*[\"']?discussion[\"']?\s*(?::|\}|\{|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HostDecision:
    """host 单次调用的结果：并行派发、有序协作或直接回答。"""

    targets: list[str]
    reason: str
    answer: str | None = None
    tasks: dict[str, str] = field(default_factory=dict)
    role_changes: SessionRoleChanges = field(
        default_factory=SessionRoleChanges.empty)
    collaboration: CollaborationPlan | None = None
    discussion: DiscussionRequest | None = None


@dataclass(frozen=True)
class HostCollaboration:
    """One explicit-message extraction: ordered plan plus optional roles."""

    plan: CollaborationPlan
    role_changes: SessionRoleChanges = field(
        default_factory=SessionRoleChanges.empty)


class HostAgent:
    """Product-level supervisor over a tool-less native agent runtime."""

    name = "host"

    def __init__(self, adapter: AgentAdapter | None = None,
                 workers: list[str] | None = None) -> None:
        self.adapter = adapter or create_native_host_runtime()
        self.workers = workers or []

    @property
    def session_id(self) -> str | None:  # 满足 AgentAdapter 协议
        return self.adapter.session_id

    @property
    def stateful_session(self) -> bool:
        return bool(
            getattr(self.adapter, "stateful_session", False)
            and callable(getattr(self.adapter, "stream_prepared", None))
        )

    @property
    def prepared_semantic_session(self) -> bool:
        """Whether Orchestrator must use the durable semantic-call seam."""
        return self.stateful_session

    @property
    def replay_history_on_fresh_session(self) -> bool:
        return bool(getattr(
            self.adapter, "replay_history_on_fresh_session", True))

    async def decide(
            self, transcript: str, workdir: str,
            on_event: Callable[[AgentEvent], None] | None = None,
            *,
            choices: list[str] | None = None,
    ) -> HostDecision:
        """一次 LLM 调用返回路由/回答，并公开安全的非正文进度事件。"""
        selected = self.workers if choices is None else choices
        prompt = self._build_route_prompt(transcript, selected)
        buf: list[str] = []
        async for ev in self._direct_semantic_stream(prompt, workdir):
            if ev.kind == "text":
                buf.append(ev.text)
            elif ev.kind not in {"done", "delivery_committed"}:
                if on_event is not None:
                    on_event(ev)
        return self._parse("".join(buf), selected)

    def build_route_prompt(
        self,
        transcript: str,
        choices: list[str],
    ) -> str:
        """Build a route prompt for Orchestrator's prepared delivery seam."""
        return self._build_route_prompt(transcript, choices)

    def parse_decision(self, raw: str, choices: list[str]) -> HostDecision:
        """Validate one captured model response without invoking a provider."""
        return self._parse(raw, choices)

    async def extract_session_roles(
        self,
        text: str,
        choices: list[str],
        workdir: str,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> SessionRoleChanges:
        """在固定候选闭集内提取角色变化，不参与路由。"""
        prompt = self.build_session_roles_prompt(text, choices)
        parts: list[str] = []
        async for event in self._direct_semantic_stream(prompt, workdir):
            if event.kind == "text":
                parts.append(event.text)
            elif event.kind not in {"done", "delivery_committed"}:
                if on_event is not None:
                    on_event(event)
        return self.parse_session_roles("".join(parts), choices)

    @staticmethod
    def build_session_roles_prompt(text: str, choices: list[str]) -> str:
        return _ROLE_EXTRACTION_TEMPLATE.format(
            choices="、".join(choices),
            text=text[:12000],
        )

    @staticmethod
    def parse_session_roles(
        raw: str,
        choices: list[str],
    ) -> SessionRoleChanges:
        return SessionRoleChanges.from_model_output(raw, choices)

    async def extract_collaboration(
        self,
        text: str,
        choices: list[str],
        workdir: str,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> HostCollaboration:
        """Extract plan and roles in one call inside the mention closure."""
        prompt = self.build_collaboration_prompt(text, choices)
        parts: list[str] = []
        async for event in self._direct_semantic_stream(prompt, workdir):
            if event.kind == "text":
                parts.append(event.text)
            elif event.kind not in {"done", "delivery_committed"}:
                if on_event is not None:
                    on_event(event)
        return self.parse_collaboration("".join(parts), choices)

    @staticmethod
    def build_collaboration_prompt(text: str, choices: list[str]) -> str:
        return _COLLABORATION_EXTRACTION_TEMPLATE.format(
            choices="、".join(choices),
            text=text[:12000],
        )

    @staticmethod
    def parse_collaboration(
        raw: str,
        choices: list[str],
    ) -> HostCollaboration:
        raw = raw.strip()
        match = _JSON_RE.search(raw)
        if match is None:
            raise CollaborationValidationError("host 未返回协作计划 JSON")
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise CollaborationValidationError(
                "host 返回的协作计划不是合法 JSON") from exc
        plan = parse_collaboration_payload(
            payload,
            choices,
            require_all=choices,
        )
        role_changes = SessionRoleChanges.from_payload(
            payload.get("role_changes", {})
            if isinstance(payload, dict) else {},
            choices,
        )
        return HostCollaboration(plan, role_changes)

    def _direct_semantic_stream(
        self,
        prompt: str,
        workdir: str,
    ) -> AsyncIterator[AgentEvent]:
        """Compatibility path restricted to stateless injected adapters.

        Production stateful hosts must be invoked by Orchestrator through
        ``stream_prepared`` so cursor/checkpoint and no-replay stay atomic.
        """
        if self.stateful_session:
            raise RuntimeError(
                "有状态 host 语义调用必须通过 prepared delivery seam")
        return self.adapter.stream(prompt, workdir)

    async def extract_collaboration_plan(
        self,
        text: str,
        choices: list[str],
        workdir: str,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> CollaborationPlan:
        """Compatibility facade for callers that only need the plan."""
        result = await self.extract_collaboration(
            text, choices, workdir, on_event)
        return result.plan

    def _build_route_prompt(
            self, transcript: str, choices: list[str] | None = None) -> str:
        """构造纯路由 prompt；保留为可直接测试的协议边界。"""
        selected = self.workers if choices is None else choices
        if not selected:
            return _NO_WORKER_ROUTE_TEMPLATE.format(transcript=transcript)
        return _ROUTE_TEMPLATE.format(
            workers="、".join(selected),
            first_worker=selected[0],
            choices=", ".join(selected),
            transcript=transcript,
        )

    def _parse(self, raw: str, choices: list[str]) -> HostDecision:
        """有效路由 JSON 才派发；其余非空输出就是 host 的最终回答。"""
        raw = raw.strip()
        collaboration_marker = _COLLABORATION_MARKER_RE.search(raw) is not None
        discussion_marker = _DISCUSSION_MARKER_RE.search(raw) is not None
        m = _JSON_RE.search(raw)
        if m:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, dict) and "discussion" in data:
                    request = parse_discussion_payload(
                        data["discussion"], choices)
                    reason = str(data.get("reason", ""))[:80]
                    return HostDecision(
                        [],
                        reason or "主持人识别为有界讨论",
                        discussion=request,
                    )
                if isinstance(data, dict) and "collaboration" in data:
                    plan = parse_collaboration_payload(
                        data["collaboration"], choices)
                    reason = str(data.get("reason", ""))[:80]
                    role_changes = SessionRoleChanges.from_payload(
                        data.get("role_changes", {}),
                        list(plan.participants),
                    )
                    return HostDecision(
                        [], reason or "主持人识别为有序协作",
                        role_changes=role_changes,
                        collaboration=plan,
                    )
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
            except DiscussionValidationError:
                raise
            except (json.JSONDecodeError, AttributeError, TypeError) as exc:
                if discussion_marker:
                    raise DiscussionValidationError(
                        "host 返回的讨论意图不是合法 JSON") from exc
                if collaboration_marker:
                    raise CollaborationValidationError(
                        "host 返回的协作计划不是合法 JSON") from exc
        if collaboration_marker:
            raise CollaborationValidationError(
                "host 返回的协作计划不是合法 JSON")
        if discussion_marker:
            raise DiscussionValidationError(
                "host 返回的讨论意图不是合法 JSON")
        if raw:
            return HostDecision([], "host 直接回答", raw)
        # 空输出没有可展示答案；确定性回退到首个 worker 保持可用性。
        targets = choices[:1]
        return HostDecision(targets, "host 无输出，回退到 worker")

    def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        """让 host 也能像普通 agent 一样被 dispatch（@host 时走这条路）。"""
        if execution_mode is ExecutionMode.DEFAULT:
            return self.adapter.stream(prompt, workdir)
        return self.adapter.stream(
            prompt, workdir, execution_mode=execution_mode)

    def stream_prepared(
        self,
        make_prompt: Callable[[NativeSessionPreparation], str],
        workdir: str,
        resume_session_id: str | None = None,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        """Forward the stateful prepare/checkpoint contract to the runtime."""
        prepared = getattr(self.adapter, "stream_prepared", None)
        if prepared is None:
            raise RuntimeError("host 底层 adapter 不支持 stateful prepare")
        if execution_mode is ExecutionMode.DEFAULT:
            return prepared(make_prompt, workdir, resume_session_id)
        return prepared(
            make_prompt,
            workdir,
            resume_session_id,
            execution_mode=execution_mode,
        )

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

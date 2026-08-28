"""Bounded ordered collaboration as a small deterministic state contract.

The host may understand natural language, but it cannot decide execution policy.
This module owns the validated plan shape and the conservative trigger used for
explicit multi-mention messages.  It contains no adapter or process behavior.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from adapters.base import redact_sensitive_text


MIN_COLLABORATION_STEPS = 2
MAX_COLLABORATION_STEPS = 4
MAX_COLLABORATION_ASSIGNMENT_CHARS = 4000
MAX_COLLABORATION_ASSIGNMENT_PREVIEW_CHARS = 240
MAX_COLLABORATION_AGENT_CHARS = 64
COLLABORATION_PLAN_EVENT_VERSION = 1

_PLAN_TRANSITION_STATES = frozenset({
    "running", "completed", "failed", "cancelled", "skipped",
})
_PLAN_STATE_MARKS = {
    "queued": "○",
    "running": "›",
    "completed": "✓",
    "failed": "×",
    "cancelled": "×",
    "skipped": "–",
}
_PLAN_STATE_LABELS = {
    "queued": "等待",
    "running": "进行中",
    "completed": "已结束",
    "failed": "失败",
    "cancelled": "已取消",
    "skipped": "未执行",
}

_ORDERED_CUE_RE = re.compile(
    r"(?:先|首先).{0,240}?(?:再|然后|接着|随后|最后)"
    r"|@\w+.{0,240}?(?:然后|接着|随后|最后).{0,240}?@\w+"
    r"|\bfirst\b.{0,240}?\b(?:then|next|finally)\b",
    re.IGNORECASE | re.DOTALL,
)
_CROSS_AGENT_ORDER_RE = re.compile(
    r"(?:先|首先)(?:让|由)?\s*@\w+.{0,240}?"
    r"(?:再|然后|接着|随后|最后)(?:让|由)?\s*@\w+"
    r"|\bfirst\b\s+@\w+.{0,240}?"
    r"\b(?:then|next|finally)\b\s+@\w+",
    re.IGNORECASE | re.DOTALL,
)
_PARALLEL_CUE_RE = re.compile(
    r"一起|分别|各自|同时|并行|\btogether\b|\bseparately\b|"
    r"\bin[ -]?parallel\b|\beach\b",
    re.IGNORECASE,
)


class CollaborationValidationError(ValueError):
    """A model-produced collaboration plan violates the bounded contract."""


class CollaborationPlanEventError(ValueError):
    """A persisted or live collaboration plan event is malformed."""


@dataclass(frozen=True)
class CollaborationStep:
    agent: str
    assignment: str


@dataclass(frozen=True)
class CollaborationPlan:
    steps: tuple[CollaborationStep, ...]

    @property
    def participants(self) -> tuple[str, ...]:
        """Distinct participants in first-appearance order."""
        return tuple(dict.fromkeys(step.agent for step in self.steps))


@dataclass(frozen=True)
class CollaborationPlanEvent:
    """Versioned projection event for one bounded collaboration plan.

    This is the small seam shared by the orchestrator, CommandBus persistence
    and TUI.  The event never drives scheduling; ``CollaborationPlan`` remains
    the execution authority.  It only projects the already-frozen plan and its
    deterministic step transitions for user-visible state and restart detail.
    """

    event: str
    steps: tuple[CollaborationStep, ...] = ()
    step: int | None = None
    total: int | None = None
    agent: str = ""
    state: str = ""

    @classmethod
    def created(cls, plan: CollaborationPlan) -> "CollaborationPlanEvent":
        previews = tuple(
            CollaborationStep(
                step.agent,
                redact_sensitive_text(
                    " ".join(step.assignment.split()),
                    limit=MAX_COLLABORATION_ASSIGNMENT_PREVIEW_CHARS,
                ),
            )
            for step in plan.steps
        )
        return cls("created", steps=previews)

    @classmethod
    def transition(
        cls,
        plan: CollaborationPlan,
        step: int,
        state: str,
    ) -> "CollaborationPlanEvent":
        if not 1 <= step <= len(plan.steps):
            raise CollaborationPlanEventError("协作步骤编号越界")
        if state not in _PLAN_TRANSITION_STATES:
            raise CollaborationPlanEventError("协作步骤状态非法")
        selected = plan.steps[step - 1]
        return cls(
            "step",
            step=step,
            total=len(plan.steps),
            agent=selected.agent,
            state=state,
        )

    def encode(self) -> str:
        if self.event == "created":
            payload: dict[str, object] = {
                "version": COLLABORATION_PLAN_EVENT_VERSION,
                "event": "created",
                "steps": [
                    {"agent": step.agent, "assignment": step.assignment}
                    for step in self.steps
                ],
            }
        elif self.event == "step":
            payload = {
                "version": COLLABORATION_PLAN_EVENT_VERSION,
                "event": "step",
                "step": self.step,
                "total": self.total,
                "agent": self.agent,
                "state": self.state,
            }
        else:
            raise CollaborationPlanEventError("未知协作计划事件")
        return json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        )

    @classmethod
    def decode(cls, text: str) -> "CollaborationPlanEvent":
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CollaborationPlanEventError("协作计划事件不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise CollaborationPlanEventError("协作计划事件必须是 JSON object")
        if payload.get("version") != COLLABORATION_PLAN_EVENT_VERSION:
            raise CollaborationPlanEventError("协作计划事件版本不受支持")

        event = payload.get("event")
        if event == "created":
            if set(payload) != {"version", "event", "steps"}:
                raise CollaborationPlanEventError("协作计划创建事件字段非法")
            raw_steps = payload.get("steps")
            if not isinstance(raw_steps, list) or not (
                MIN_COLLABORATION_STEPS
                <= len(raw_steps)
                <= MAX_COLLABORATION_STEPS
            ):
                raise CollaborationPlanEventError("协作计划创建事件步骤数非法")
            steps: list[CollaborationStep] = []
            for raw_step in raw_steps:
                if not isinstance(raw_step, dict) or set(raw_step) != {
                    "agent", "assignment",
                }:
                    raise CollaborationPlanEventError("协作计划步骤字段非法")
                agent = raw_step.get("agent")
                assignment = raw_step.get("assignment")
                if (
                    not isinstance(agent, str)
                    or not agent.strip()
                    or len(agent) > MAX_COLLABORATION_AGENT_CHARS
                    or not isinstance(assignment, str)
                    or not assignment.strip()
                    or len(assignment) > MAX_COLLABORATION_ASSIGNMENT_PREVIEW_CHARS
                ):
                    raise CollaborationPlanEventError("协作计划步骤内容非法")
                steps.append(CollaborationStep(agent.strip(), assignment.strip()))
            if len({step.agent for step in steps}) < 2:
                raise CollaborationPlanEventError("协作计划至少需要两个 agent")
            return cls("created", steps=tuple(steps))

        if event == "step":
            if set(payload) != {
                "version", "event", "step", "total", "agent", "state",
            }:
                raise CollaborationPlanEventError("协作步骤事件字段非法")
            step = payload.get("step")
            total = payload.get("total")
            agent = payload.get("agent")
            state = payload.get("state")
            if (
                not isinstance(step, int)
                or isinstance(step, bool)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or not MIN_COLLABORATION_STEPS <= total <= MAX_COLLABORATION_STEPS
                or not 1 <= step <= total
                or not isinstance(agent, str)
                or not agent.strip()
                or len(agent) > MAX_COLLABORATION_AGENT_CHARS
                or state not in _PLAN_TRANSITION_STATES
            ):
                raise CollaborationPlanEventError("协作步骤事件内容非法")
            return cls(
                "step",
                step=step,
                total=total,
                agent=agent.strip(),
                state=str(state),
            )

        raise CollaborationPlanEventError("未知协作计划事件")


@dataclass(frozen=True)
class CollaborationPlanProgressStep:
    agent: str
    assignment: str
    state: str = "queued"

    @property
    def mark(self) -> str:
        return _PLAN_STATE_MARKS.get(self.state, "·")

    @property
    def state_label(self) -> str:
        return _PLAN_STATE_LABELS.get(self.state, self.state)


@dataclass(frozen=True)
class CollaborationPlanProgress:
    """Pure, deterministic projection of plan events for any UI surface."""

    steps: tuple[CollaborationPlanProgressStep, ...] = ()

    def apply(self, event: CollaborationPlanEvent) -> "CollaborationPlanProgress":
        if event.event == "created":
            created = CollaborationPlanProgress(tuple(
                CollaborationPlanProgressStep(
                    step.agent,
                    redact_sensitive_text(step.assignment, limit=240),
                )
                for step in event.steps
            ))
            # created 是同一 command 的身份锚点；重复或迟到快照不能把已推进
            # 的实时状态重置为 queued，也不能用另一份计划替换当前投影。
            return self if self.steps else created
        if event.event != "step" or event.step is None or event.total is None:
            return self
        if len(self.steps) != event.total:
            return self
        index = event.step - 1
        current = self.steps[index]
        if current.agent != event.agent:
            return self
        terminal = {"completed", "failed", "cancelled", "skipped"}
        if current.state in terminal:
            return self
        prior_states = tuple(step.state for step in self.steps[:index])
        later_states = tuple(step.state for step in self.steps[index + 1:])
        if event.state == "running" and not (
            current.state == "queued"
            and all(state == "completed" for state in prior_states)
            and all(state == "queued" for state in later_states)
        ):
            return self
        if event.state in {"completed", "failed", "cancelled"} \
                and current.state != "running":
            return self
        if event.state == "skipped" and not (
            current.state == "queued"
            and any(
                state in {"failed", "cancelled", "skipped"}
                for state in prior_states
            )
        ):
            return self
        updated = CollaborationPlanProgressStep(
            current.agent, current.assignment, event.state)
        return CollaborationPlanProgress(
            self.steps[:index] + (updated,) + self.steps[index + 1:]
        )

    @property
    def current(self) -> tuple[int, CollaborationPlanProgressStep] | None:
        if not self.steps:
            return None
        for state in ("running", "queued", "failed", "cancelled"):
            for index, step in enumerate(self.steps, start=1):
                if step.state == state:
                    return index, step
        return len(self.steps), self.steps[-1]


def has_ordered_collaboration_cue(
    text: str,
    targets: Sequence[str],
) -> bool:
    """Conservatively select explicit multi-mention text for host extraction.

    This does not infer a plan.  It only prevents ordinary parallel fan-out
    from paying an extra host call unless the user expressed a clear sequence.
    """
    distinct_targets = tuple(dict.fromkeys(targets))
    if len(distinct_targets) < 2:
        return False
    if _CROSS_AGENT_ORDER_RE.search(text):
        return True
    if _PARALLEL_CUE_RE.search(text):
        return False
    return bool(_ORDERED_CUE_RE.search(text))


def parse_collaboration_payload(
    payload: object,
    allowed_agents: Sequence[str],
    *,
    require_all: Sequence[str] = (),
) -> CollaborationPlan:
    """Validate an untrusted model payload into a bounded immutable plan.

    ``allowed_agents`` is an authority boundary, not a hint.  ``require_all``
    additionally freezes an explicit mention set: every named worker must be
    used and no worker outside that set can enter the plan.
    """
    if not isinstance(payload, Mapping):
        raise CollaborationValidationError("协作计划必须是 JSON object")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise CollaborationValidationError("协作计划 steps 必须是 JSON array")
    if not MIN_COLLABORATION_STEPS <= len(raw_steps) <= MAX_COLLABORATION_STEPS:
        raise CollaborationValidationError(
            f"协作计划必须包含 {MIN_COLLABORATION_STEPS}–"
            f"{MAX_COLLABORATION_STEPS} 步")

    allowed = set(allowed_agents)
    steps: list[CollaborationStep] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, Mapping):
            raise CollaborationValidationError(f"协作第 {index} 步必须是 object")
        agent = raw_step.get("agent")
        assignment = raw_step.get("assignment")
        if not isinstance(agent, str) or agent not in allowed:
            raise CollaborationValidationError(
                f"协作第 {index} 步包含未知或未就绪 agent")
        if not isinstance(assignment, str) or not assignment.strip():
            raise CollaborationValidationError(
                f"协作第 {index} 步 assignment 不能为空")
        assignment = assignment.strip()
        if len(assignment) > MAX_COLLABORATION_ASSIGNMENT_CHARS:
            raise CollaborationValidationError(
                f"协作第 {index} 步 assignment 不能超过 "
                f"{MAX_COLLABORATION_ASSIGNMENT_CHARS} 字")
        steps.append(CollaborationStep(agent, assignment))

    plan = CollaborationPlan(tuple(steps))
    if len(plan.participants) < 2:
        raise CollaborationValidationError("协作计划至少需要两个不同 agent")
    if require_all and set(plan.participants) != set(require_all):
        raise CollaborationValidationError("协作计划必须使用全部已点名 agent，且不得扩员")
    return plan

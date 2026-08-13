"""Bounded ordered collaboration as a small deterministic state contract.

The host may understand natural language, but it cannot decide execution policy.
This module owns the validated plan shape and the conservative trigger used for
explicit multi-mention messages.  It contains no adapter or process behavior.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence


MIN_COLLABORATION_STEPS = 2
MAX_COLLABORATION_STEPS = 4
MAX_COLLABORATION_ASSIGNMENT_CHARS = 4000

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

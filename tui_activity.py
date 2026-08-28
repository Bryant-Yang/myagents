"""TUI 执行活动摘要的纯状态模型。

协议事件仍由 CommandBus 持久化；本模块只把同一 command 的高频可见状态
压缩成一张可展开卡片，不参与路由、权限或生命周期判断。
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from adapters.base import redact_sensitive_text
from collaboration import (
    CollaborationPlanEvent,
    CollaborationPlanEventError,
    CollaborationPlanProgress,
)
from tui_status import format_response_duration


_COMMAND_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "cancelling": "取消中",
    "completed": "已结束",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}

_TOOL_LABELS = {
    "pending": "等待中",
    "queued": "等待中",
    "in_progress": "进行中",
    "running": "进行中",
    "completed": "已完成",
    "succeeded": "已完成",
    "success": "已完成",
    "failed": "失败",
    "error": "失败",
    "declined": "已拒绝",
    "cancelled": "已取消",
    "canceled": "已取消",
}

_NOTE_LABELS = {
    "heartbeat": "等待",
    "permission": "权限",
    "info": "信息",
    "workflow": "流程",
    "steering": "补充",
    "cancel": "控制",
    "collaboration": "协作",
}

_MAX_NOTES = 12
_MAX_REQUEST_PREVIEW_CHARS = 120
_DEFAULT_MAX_TERMINAL_CARDS = 100
_DEFAULT_MAX_TOOLS_PER_CARD = 50
_DEFAULT_MAX_DETAIL_ROWS = 80
_TERMINAL_STATES = {
    "completed", "failed", "cancelled", "interrupted", "skipped"}

_DETAIL_KIND_LABELS = {
    "queued": "排队",
    "running": "开始",
    "status": "阶段",
    "tool": "工具",
    "permission": "权限",
    "steering": "阶段补充",
    "interjection_requested": "插话",
    "interjection_accepted": "插话",
    "interjection_failed": "插话失败",
    "interjection_uncertain": "插话状态不确定",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "取消",
    "plan": "协作计划",
}
_DETAIL_IMPORTANT_KINDS = frozenset({
    "plan", "tool", "permission", "steering",
    "interjection_requested", "interjection_accepted",
    "interjection_failed", "interjection_uncertain",
})
_PERSISTED_TERMINAL_KINDS = frozenset({
    "completed", "failed", "cancelled"})
_LIFECYCLE_KINDS = frozenset({
    "queued", "running", "completed", "failed", "cancelled"})
_CONTROL_KINDS = frozenset({
    "steering", "interjection_requested", "interjection_accepted",
    "interjection_failed", "interjection_uncertain", "cancelled",
})


@dataclass(frozen=True)
class ActivityDetailEvent:
    """持久过程事件的 UI-safe 中立投影输入。"""

    seq: int
    agent: str
    kind: str
    text: str
    created_at: str


@dataclass(frozen=True)
class _AgentActivity:
    state: str
    phase: str
    session_role: str = ""


@dataclass(frozen=True)
class _ToolActivity:
    agent: str
    title: str
    status: str
    detail: str
    identity_is_fallback: bool


@dataclass
class _ActivityCard:
    command_id: str
    command_state: str = "running"
    pending_request: str = ""
    elapsed_seconds: float | None = None
    error: str = ""
    agents: OrderedDict[str, _AgentActivity] = field(
        default_factory=OrderedDict
    )
    tools: OrderedDict[tuple[str, str], _ToolActivity] = field(
        default_factory=OrderedDict
    )
    tools_truncated: bool = False
    historical_tool_exception: str = ""
    notes: OrderedDict[tuple[str, str], str] = field(
        default_factory=OrderedDict
    )
    plan: CollaborationPlanProgress = field(
        default_factory=CollaborationPlanProgress)
    detail_state: str = "idle"
    detail_events: tuple[ActivityDetailEvent, ...] = ()
    detail_total_count: int = 0
    detail_omitted_count: int = 0
    detail_kind_counts: tuple[tuple[str, int], ...] = ()
    detail_agents: tuple[str, ...] = ()
    detail_partial_char_count: int = 0
    detail_error: str = ""
    detail_generation: int = 0

    def status_label(self) -> str:
        label = _COMMAND_LABELS.get(self.command_state, self.command_state)
        if self.command_state == "running" and self.agents:
            states = {activity.state for activity in self.agents.values()}
            if states and states <= {"completed"}:
                return "响应已结束"
            if "failed" in states:
                return "失败"
        return label


class ActivityFeed:
    """按 command 聚合可见执行活动，所有更新均可幂等比较。"""

    def __init__(
        self,
        *,
        max_terminal_cards: int = _DEFAULT_MAX_TERMINAL_CARDS,
        max_tools_per_card: int = _DEFAULT_MAX_TOOLS_PER_CARD,
        max_detail_rows: int = _DEFAULT_MAX_DETAIL_ROWS,
    ) -> None:
        if (max_terminal_cards < 1 or max_tools_per_card < 1
                or max_detail_rows < 1):
            raise ValueError("活动卡、工具与详情行上限必须为正数")
        self._cards: OrderedDict[str, _ActivityCard] = OrderedDict()
        self._max_terminal_cards = max_terminal_cards
        self._max_tools_per_card = max_tools_per_card
        self._max_detail_rows = max_detail_rows
        self._evicted: deque[tuple[str, str]] = deque(
            maxlen=max_terminal_cards)

    def clear(self) -> None:
        self._cards.clear()
        self._evicted.clear()

    def begin(self, command_id: str, state: str = "running") -> bool:
        if command_id in self._cards:
            return False
        self._cards[command_id] = self._new_card(command_id, state)
        return True

    def _new_card(
        self, command_id: str, state: str = "running"
    ) -> _ActivityCard:
        return _ActivityCard(
            command_id=command_id,
            command_state=state,
        )

    def _ensure_card(self, command_id: str) -> _ActivityCard:
        card = self._cards.get(command_id)
        if card is None:
            card = self._new_card(command_id)
            self._cards[command_id] = card
        return card

    def has(self, command_id: str) -> bool:
        return command_id in self._cards

    def record_pending_request(self, command_id: str, text: str) -> bool:
        """记录尚未进入 timeline 的用户输入，仅用于当前 room 的排队提示。"""
        card = self._ensure_card(command_id)
        compact = " ".join(text.split())
        if len(compact) > _MAX_REQUEST_PREVIEW_CHARS:
            compact = f"{compact[:_MAX_REQUEST_PREVIEW_CHARS - 1]}…"
        if card.pending_request == compact:
            return False
        card.pending_request = compact
        return True

    def mark_request_committed(self, command_id: str) -> bool:
        """正文已进入 timeline 后移除临时摘要，避免同一输入重复显示。"""
        return self.clear_pending_request(command_id)

    def clear_pending_request(self, command_id: str) -> bool:
        """清除未提交摘要；持久化失败时不得把原文留在活动卡。"""
        card = self._cards.get(command_id)
        if card is None or not card.pending_request:
            return False
        card.pending_request = ""
        return True

    def command_ids(self) -> tuple[str, ...]:
        return tuple(self._cards)

    def has_terminal_persisted_details(self, command_id: str) -> bool:
        card = self._cards.get(command_id)
        return bool(
            card is not None
            and card.detail_state == "ready"
            and card.detail_events
            and card.detail_events[-1].kind in _PERSISTED_TERMINAL_KINDS
        )

    def start_details_loading(self, command_id: str) -> int:
        card = self._ensure_card(command_id)
        card.detail_generation += 1
        card.detail_state = "loading"
        card.detail_error = ""
        return card.detail_generation

    def set_details_error(
        self,
        command_id: str,
        error: str,
        *,
        generation: int | None = None,
    ) -> bool:
        card = self._ensure_card(command_id)
        if (
            generation is not None
            and generation != card.detail_generation
        ) or card.detail_state == "ready":
            return False
        safe_error = redact_sensitive_text(" ".join(error.split()), limit=400)
        changed = (
            card.detail_state != "error"
            or card.detail_error != safe_error
        )
        card.detail_state = "error"
        card.detail_error = safe_error
        return changed

    def set_persisted_details(
        self,
        command_id: str,
        events: tuple[ActivityDetailEvent, ...],
        *,
        total_count: int,
        omitted_count: int,
        kind_counts: Mapping[str, int] | None = None,
        agents: tuple[str, ...] | None = None,
        partial_char_count: int | None = None,
        generation: int | None = None,
    ) -> bool:
        """提交一次持久详情快照；正文只计数，不进入详情文本。"""
        card = self._ensure_card(command_id)
        normalized = tuple(events)
        if generation is not None and generation != card.detail_generation:
            return False
        incoming_last_kind = normalized[-1].kind if normalized else ""
        if (
            card.command_state in {"completed", "failed", "cancelled"}
            and incoming_last_kind not in _PERSISTED_TERMINAL_KINDS
        ):
            return False
        if (
            card.detail_state == "ready"
            and card.detail_events
            and normalized
            and normalized[-1].seq < card.detail_events[-1].seq
        ):
            return False
        resolved_counts: dict[str, int] = {}
        if kind_counts is None:
            for event in normalized:
                resolved_counts[event.kind] = (
                    resolved_counts.get(event.kind, 0) + 1)
        else:
            resolved_counts = {
                str(kind): max(0, int(count))
                for kind, count in kind_counts.items()
            }
        normalized_counts = tuple(sorted(resolved_counts.items()))
        normalized_agents = (
            tuple(dict.fromkeys(str(agent) for agent in agents))
            if agents is not None
            else tuple(dict.fromkeys(event.agent for event in normalized))
        )
        resolved_partial_chars = (
            sum(len(event.text) for event in normalized
                if event.kind == "partial")
            if partial_char_count is None
            else max(0, int(partial_char_count))
        )
        plan_changed = self._restore_plan_from_details(card, normalized)
        changed = (
            card.detail_state != "ready"
            or card.detail_events != normalized
            or card.detail_total_count != total_count
            or card.detail_omitted_count != omitted_count
            or card.detail_kind_counts != normalized_counts
            or card.detail_agents != normalized_agents
            or card.detail_partial_char_count != resolved_partial_chars
            or plan_changed
        )
        card.detail_state = "ready"
        card.detail_events = normalized
        card.detail_total_count = max(0, int(total_count))
        card.detail_omitted_count = max(0, int(omitted_count))
        card.detail_kind_counts = normalized_counts
        card.detail_agents = normalized_agents
        card.detail_partial_char_count = resolved_partial_chars
        card.detail_error = ""
        return changed

    def record_plan_event(
        self,
        command_id: str,
        event: CollaborationPlanEvent | str,
    ) -> bool:
        """Project one validated collaboration event into a card.

        Scheduling never reads this state.  Malformed persisted input is
        ignored here because RoomStore already preserves the raw evidence and
        the UI must remain usable while showing other command details.
        """
        card = self._ensure_card(command_id)
        try:
            decoded = (
                CollaborationPlanEvent.decode(event)
                if isinstance(event, str) else event
            )
        except CollaborationPlanEventError:
            return False
        return self._apply_plan_event(card, decoded)

    @staticmethod
    def _apply_plan_event(
        card: _ActivityCard,
        event: CollaborationPlanEvent,
    ) -> bool:
        updated = card.plan.apply(event)
        if updated == card.plan:
            return False
        card.plan = updated
        return True

    def _restore_plan_from_details(
        self,
        card: _ActivityCard,
        events: tuple[ActivityDetailEvent, ...],
    ) -> bool:
        plan_events: list[CollaborationPlanEvent] = []
        for detail in events:
            if detail.kind != "plan":
                continue
            try:
                plan_events.append(CollaborationPlanEvent.decode(detail.text))
            except CollaborationPlanEventError:
                continue
        created_index = next(
            (
                index for index, event in enumerate(plan_events)
                if event.event == "created"
            ),
            None,
        )
        if created_index is None:
            return False
        previous = card.plan
        created = plan_events[created_index]
        restored = CollaborationPlanProgress().apply(created)
        if previous.steps:
            # 详情读取与实时事件并发：持久快照可能早于屏幕上已收到的状态。
            # 只在计划身份一致时把后续迁移单调合入，绝不先 reset 造成倒退。
            if tuple(
                (step.agent, step.assignment) for step in restored.steps
            ) != tuple(
                (step.agent, step.assignment) for step in previous.steps
            ):
                return False
            restored = previous
        for event in plan_events[created_index + 1:]:
            restored = restored.apply(event)
        card.plan = restored
        return restored != previous

    def record_status(
        self,
        command_id: str,
        agent: str,
        phase: str,
        *,
        state: str = "running",
        heartbeat: bool = False,
        session_role: str | None = None,
    ) -> bool:
        card = self._ensure_card(command_id)
        agent = redact_sensitive_text(
            " ".join(str(agent).split()), limit=60) or "agent"
        phase = redact_sensitive_text(
            " ".join(str(phase).split()), limit=500)
        changed = False
        current = card.agents.get(agent)
        # heartbeat 只刷新等待证据，不覆盖更有意义的实际阶段。
        if not heartbeat or current is None:
            role = (
                _clean_session_role(session_role)
                if session_role is not None
                else (current.session_role if current is not None else "")
            )
            updated = _AgentActivity(state, phase, role)
            if current != updated:
                card.agents[agent] = updated
                card.agents.move_to_end(agent)
                changed = True
        note_key = ("heartbeat" if heartbeat else "status", agent)
        if card.notes.get(note_key) != phase:
            card.notes[note_key] = phase
            card.notes.move_to_end(note_key)
            while len(card.notes) > _MAX_NOTES:
                card.notes.popitem(last=False)
            changed = True
        return changed

    def record_note(
        self,
        command_id: str,
        agent: str,
        category: str,
        text: str,
        *,
        state: str | None = None,
    ) -> bool:
        card = self._ensure_card(command_id)
        agent = redact_sensitive_text(
            " ".join(str(agent).split()), limit=60) or "agent"
        category = redact_sensitive_text(
            " ".join(str(category).split()), limit=60) or "info"
        text = redact_sensitive_text(str(text), limit=1200)
        changed = False
        key = (category, agent)
        if card.notes.get(key) != text:
            card.notes[key] = text
            card.notes.move_to_end(key)
            while len(card.notes) > _MAX_NOTES:
                card.notes.popitem(last=False)
            changed = True
        if state is not None:
            current = card.agents.get(agent)
            updated = _AgentActivity(
                state,
                text,
                current.session_role if current is not None else "",
            )
            if current != updated:
                card.agents[agent] = updated
                card.agents.move_to_end(agent)
                changed = True
        return changed

    def record_tool(
        self,
        command_id: str,
        agent: str,
        identity: str,
        title: str,
        *,
        status: str = "",
        detail: str = "",
        identity_is_fallback: bool = False,
    ) -> bool:
        card = self._ensure_card(command_id)
        agent = redact_sensitive_text(
            " ".join(str(agent).split()), limit=60) or "agent"
        identity = redact_sensitive_text(str(identity), limit=120)
        title = redact_sensitive_text(str(title), limit=300)
        detail = redact_sensitive_text(str(detail), limit=2000)
        key = (agent, identity)
        inherited: _ToolActivity | None = None
        # 有些协议先只给 title，后续 update 才补 toolCallId；迁移 identity，
        # 避免同一个工具在摘要里变成两项。
        if key not in card.tools:
            for old_key, old_tool in tuple(card.tools.items()):
                if (
                    old_tool.identity_is_fallback
                    and old_tool.agent == agent
                    and old_tool.title == title
                ):
                    inherited = card.tools.pop(old_key)
                    break
        if key not in card.tools and len(card.tools) >= self._max_tools_per_card:
            card.tools.popitem(last=False)
            card.tools_truncated = True
        previous = card.tools.get(key) or inherited
        updated = _ToolActivity(
            agent,
            title,
            status,
            detail or (previous.detail if previous is not None else ""),
            identity_is_fallback,
        )
        changed = card.tools.get(key) != updated
        if changed:
            card.tools[key] = updated
            card.tools.move_to_end(key)
        status_label = _TOOL_LABELS.get(
            status.lower(), status or "已记录")
        if status_label in {"失败", "已拒绝", "已取消"}:
            priority = {"已取消": 1, "已拒绝": 2, "失败": 3}
            if priority[status_label] > priority.get(
                    card.historical_tool_exception, 0):
                card.historical_tool_exception = status_label
                changed = True
        current_agent = card.agents.get(agent)
        agent_activity = _AgentActivity(
            "running",
            f"工具：{title}",
            current_agent.session_role if current_agent is not None else "",
        )
        if card.agents.get(agent) != agent_activity:
            card.agents[agent] = agent_activity
            changed = True
        if changed:
            card.agents.move_to_end(agent)
        return changed

    def set_command_state(
        self,
        command_id: str,
        state: str,
        error: str | None = None,
        *,
        elapsed_seconds: float | None = None,
    ) -> bool:
        card = self._cards.get(command_id)
        if card is None:
            return False
        cleared_pending = state == "failed" and bool(card.pending_request)
        if cleared_pending:
            card.pending_request = ""
        details_became_stale = (
            state in _TERMINAL_STATES
            and card.detail_state == "ready"
            and (
                not card.detail_events
                or card.detail_events[-1].kind
                not in _PERSISTED_TERMINAL_KINDS
            )
        )
        if details_became_stale:
            # 运行中读取的快照不能在任务结束后伪装成完整过程。先回到最新
            # 内存摘要；用户再次展开或切回会话时会重新读取 terminal 快照。
            card.detail_state = "idle"
        normalized_error = (
            redact_sensitive_text(str(error), limit=500) if error else ""
        )
        changed = (
            card.command_state != state
            or card.error != normalized_error
            or cleared_pending
            or details_became_stale
        )
        if elapsed_seconds is not None and card.elapsed_seconds is None:
            normalized_elapsed = max(0.0, float(elapsed_seconds))
            card.elapsed_seconds = normalized_elapsed
            changed = True
        if not changed:
            return False
        card.command_state = state
        card.error = normalized_error
        if state in _TERMINAL_STATES:
            self._prune_terminal_cards()
        return True

    def take_evicted(self) -> tuple[tuple[str, str], ...]:
        """返回刚归档的终态卡及其折叠快照；调用后清空队列。"""
        evicted = tuple(self._evicted)
        self._evicted.clear()
        return evicted

    def _prune_terminal_cards(self) -> None:
        terminal_ids = [
            command_id for command_id, card in self._cards.items()
            if card.command_state in _TERMINAL_STATES
        ]
        while len(terminal_ids) > self._max_terminal_cards:
            command_id = terminal_ids.pop(0)
            snapshot = self.render(command_id, expanded=False).replace(
                "/details 展开", "活动已归档"
            )
            self._cards.pop(command_id)
            self._evicted.append((command_id, snapshot))

    def render(self, command_id: str, *, expanded: bool) -> str:
        card = self._cards[command_id]
        tool_count = len(card.tools)
        persisted_tool_events = (
            dict(card.detail_kind_counts).get("tool", 0)
            if card.detail_state == "ready" else 0
        )
        if card.pending_request:
            prefix = (
                "待发送" if card.command_state == "queued" else "未发送"
            )
            focus = f"{prefix}：{card.pending_request}"
        elif card.plan.current is not None:
            step_index, step = card.plan.current
            assignment = step.assignment.replace("\n", " ")
            if len(assignment) > 40:
                assignment = f"{assignment[:39]}…"
            focus = (
                f"步骤 {step_index}/{len(card.plan.steps)} · "
                f"{_agent_label(card, step.agent)}：{assignment}"
            )
        elif card.agents:
            latest_name = next(reversed(card.agents))
            latest = card.agents[latest_name]
            phase = latest.phase.replace("\n", " ")
            if len(phase) > 40:
                phase = f"{phase[:39]}…"
            focus = f"{_agent_label(card, latest_name)}：{phase}"
        else:
            focus = "准备中"
        if card.tools:
            tool_states = {
                _TOOL_LABELS.get(tool.status.lower(), tool.status or "已记录")
                for tool in card.tools.values()
            }
            exceptional = tool_states & {"失败", "已拒绝", "已取消"}
            if card.historical_tool_exception:
                exceptional.add(card.historical_tool_exception)
            if exceptional:
                tool_state = next(
                    label for label in ("失败", "已拒绝", "已取消")
                    if label in exceptional
                )
            elif tool_states <= {"已完成"}:
                tool_state = "已完成"
            elif tool_states <= {"已记录", "等待中"}:
                tool_state = "已记录"
            else:
                tool_state = "进行中"
            count = f"≥{tool_count}" if card.tools_truncated else str(tool_count)
            tool_summary = f"{count} 个工具（{tool_state}）"
        else:
            tool_summary = (
                f"{persisted_tool_events} 条工具事件"
                if persisted_tool_events else "0 个工具"
            )
        timing = ""
        if card.command_state in _TERMINAL_STATES:
            duration = (
                format_response_duration(card.elapsed_seconds)
                if card.elapsed_seconds is not None else "未知"
            )
            timing = f" · 响应耗时 {duration}"
        status = card.status_label()
        if card.command_state == "queued":
            queued = [
                queued_id
                for queued_id, queued_card in self._cards.items()
                if queued_card.command_state == "queued"
            ]
            status = f"排队 {queued.index(command_id) + 1}"
        header = (
            f"任务 {command_id[:8]} · {status}{timing} · {focus}"
            f" · {tool_summary} · /details "
            f"{'收起' if expanded else '展开'}"
        )
        if not expanded:
            return header

        rows: list[str] = [header]
        rows.extend(self._render_plan(card))
        if card.detail_state == "ready":
            rows.extend(self._render_persisted_details(card))
            if card.error:
                rows.append(f"  失败原因 · {card.error}")
            return "\n".join(rows)

        for (category, agent), note in card.notes.items():
            marker = _NOTE_LABELS.get(category, "阶段")
            rows.append(
                f"  {_agent_label(card, agent)} · {marker} · {note}")
        for tool in card.tools.values():
            status = _TOOL_LABELS.get(
                tool.status.lower(), tool.status or "已记录")
            rows.append(
                f"  {_agent_label(card, tool.agent)} · "
                f"{tool.title} · {status}")
            if tool.detail:
                detail = tool.detail.replace("\n", "\n    ")
                rows.append(f"    {detail}")
        if card.historical_tool_exception:
            rows.append(
                "  历史工具异常 · "
                f"{card.historical_tool_exception}（完整过程见 events.jsonl）"
            )
        if card.tools_truncated:
            rows.append(
                f"  … · 仅保留最近 {self._max_tools_per_card} 个工具；"
                "完整过程见 events.jsonl"
            )
        if card.error:
            rows.append(f"  失败原因 · {card.error}")
        if card.detail_state == "loading":
            rows.append("  过程 · 正在读取持久记录…")
        elif card.detail_state == "error":
            rows.append(f"  详情加载失败 · {card.detail_error}")
        elif (
            not card.notes
            and not card.tools
            and not card.error
            and not card.plan.steps
        ):
            rows.append("  过程 · 暂无可显示的阶段、工具或权限事件")
        return "\n".join(rows)

    @staticmethod
    def _render_plan(card: _ActivityCard) -> list[str]:
        if not card.plan.steps or card.plan.current is None:
            return []
        current_index, _current = card.plan.current
        rows = [
            f"  协作计划 · {current_index}/{len(card.plan.steps)}"
        ]
        for index, step in enumerate(card.plan.steps, start=1):
            assignment = step.assignment.replace("\n", " ")
            rows.append(
                f"  {step.mark} {index}  {_agent_label(card, step.agent)}"
                f" · {step.state_label}"
                f" · {assignment}"
            )
        if current_index > 1:
            previous = card.plan.steps[current_index - 2]
            current = card.plan.steps[current_index - 1]
            rows.append(f"  交接 · {previous.agent} → {current.agent}")
        return rows

    def _render_persisted_details(self, card: _ActivityCard) -> list[str]:
        events = card.detail_events
        agents = tuple(
            _detail_actor(agent) for agent in card.detail_agents
            if agent not in {"system", "user"}
        )
        agent_summary = "、".join(agents) if agents else "system"
        kind_counts = dict(card.detail_kind_counts)
        tool_count = kind_counts.get("tool", 0)
        permission_count = kind_counts.get("permission", 0)
        lifecycle_count = sum(
            kind_counts.get(kind, 0) for kind in _LIFECYCLE_KINDS)
        status_count = kind_counts.get("status", 0)
        control_count = sum(
            kind_counts.get(kind, 0) for kind in _CONTROL_KINDS)
        plan_count = kind_counts.get("plan", 0)
        rows = [
            f"  过程概览 · {card.detail_total_count} 条事件 · {agent_summary}"
            f" · 已读取 {len(events)} 条代表记录",
            f"  完整统计 · 生命周期 {lifecycle_count} · 阶段 {status_count}"
            f" · 工具 {tool_count} · 权限 {permission_count}"
            f" · 控制 {control_count} · 计划 {plan_count}",
        ]

        partial_count = kind_counts.get("partial", 0)
        raw_process_events = tuple(
            event for event in events
            if event.kind not in {"partial", "plan"}
        )
        process_events = self._compact_heartbeats(raw_process_events)
        compacted_heartbeats = len(raw_process_events) - len(process_events)
        visible_events, projected_omitted = self._bound_detail_events(
            process_events)
        for event in visible_events:
            label = _DETAIL_KIND_LABELS.get(event.kind, "事件")
            text = redact_sensitive_text(
                " ↳ ".join(event.text.splitlines()), limit=500)
            rows.append(
                f"  {_detail_time(event.created_at)} · "
                f"{_detail_actor(event.agent)}"
                f" · {label} · {text or '已记录'}"
            )

        if partial_count:
            rows.append(
                f"  输出 · {partial_count} 个片段 / "
                f"{card.detail_partial_char_count} 字"
                "（正文见聊天主线）"
            )
        if tool_count == 0 and permission_count == 0:
            rows.append("  过程 · 本轮未调用工具或请求权限")
        if card.detail_omitted_count:
            rows.append(
                f"  … · 持久日志读取省略 {card.detail_omitted_count} 条；"
                "已保留首尾证据"
            )
        if compacted_heartbeats:
            rows.append(
                f"  … · 已合并 {compacted_heartbeats} 条重复心跳"
            )
        if projected_omitted:
            rows.append(
                f"  … · 过程视图再省略 {projected_omitted} 条高频事件"
            )
        if not events:
            rows.append("  过程 · 本轮没有持久过程事件")
        return rows

    @staticmethod
    def _compact_heartbeats(
        events: tuple[ActivityDetailEvent, ...],
    ) -> tuple[ActivityDetailEvent, ...]:
        compacted: list[ActivityDetailEvent] = []
        pending_heartbeat: ActivityDetailEvent | None = None
        for event in events:
            is_heartbeat = (
                event.kind == "status"
                and "仍在运行（已等待" in event.text
            )
            if is_heartbeat:
                pending_heartbeat = event
                continue
            if pending_heartbeat is not None:
                compacted.append(pending_heartbeat)
                pending_heartbeat = None
            compacted.append(event)
        if pending_heartbeat is not None:
            compacted.append(pending_heartbeat)
        return tuple(compacted)

    def _bound_detail_events(
        self,
        events: tuple[ActivityDetailEvent, ...],
    ) -> tuple[tuple[ActivityDetailEvent, ...], int]:
        if len(events) <= self._max_detail_rows:
            return events, 0

        selected: dict[int, ActivityDetailEvent] = {}
        selected[events[0].seq] = events[0]
        selected[events[-1].seq] = events[-1]
        for event in events:
            if event.kind in _DETAIL_IMPORTANT_KINDS:
                selected[event.seq] = event
                if len(selected) >= self._max_detail_rows:
                    break
        if len(selected) < self._max_detail_rows:
            for event in reversed(events):
                selected[event.seq] = event
                if len(selected) >= self._max_detail_rows:
                    break
        bounded = tuple(sorted(selected.values(), key=lambda event: event.seq))
        return bounded, len(events) - len(bounded)


def _clean_session_role(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return redact_sensitive_text(
        " ".join(value.split()), limit=40)


def _agent_label(card: _ActivityCard, agent: str) -> str:
    activity = card.agents.get(agent)
    if activity is None or not activity.session_role:
        return agent
    return f"{agent} · {activity.session_role}（本会话）"


def _detail_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return "--:--:--"


def _detail_actor(value: str) -> str:
    compact = " ".join(str(value).split())
    return redact_sensitive_text(compact, limit=60) or "system"

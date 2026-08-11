"""TUI 执行活动摘要的纯状态模型。

协议事件仍由 CommandBus 持久化；本模块只把同一 command 的高频可见状态
压缩成一张可展开卡片，不参与路由、权限或生命周期判断。
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field


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
}

_MAX_NOTES = 12
_DEFAULT_MAX_TERMINAL_CARDS = 100
_DEFAULT_MAX_TOOLS_PER_CARD = 50
_TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}


@dataclass(frozen=True)
class _AgentActivity:
    state: str
    phase: str


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
    ) -> None:
        if max_terminal_cards < 1 or max_tools_per_card < 1:
            raise ValueError("活动卡与工具上限必须为正数")
        self._cards: OrderedDict[str, _ActivityCard] = OrderedDict()
        self._max_terminal_cards = max_terminal_cards
        self._max_tools_per_card = max_tools_per_card
        self._evicted: deque[tuple[str, str]] = deque(
            maxlen=max_terminal_cards)

    def clear(self) -> None:
        self._cards.clear()
        self._evicted.clear()

    def begin(self, command_id: str, state: str = "running") -> bool:
        if command_id in self._cards:
            return False
        self._cards[command_id] = _ActivityCard(command_id, state)
        return True

    def has(self, command_id: str) -> bool:
        return command_id in self._cards

    def command_ids(self) -> tuple[str, ...]:
        return tuple(self._cards)

    def record_status(
        self,
        command_id: str,
        agent: str,
        phase: str,
        *,
        state: str = "running",
        heartbeat: bool = False,
    ) -> bool:
        card = self._cards.setdefault(command_id, _ActivityCard(command_id))
        changed = False
        current = card.agents.get(agent)
        # heartbeat 只刷新等待证据，不覆盖更有意义的实际阶段。
        if not heartbeat or current is None:
            updated = _AgentActivity(state, phase)
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
        card = self._cards.setdefault(command_id, _ActivityCard(command_id))
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
            updated = _AgentActivity(state, text)
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
        card = self._cards.setdefault(command_id, _ActivityCard(command_id))
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
        agent_activity = _AgentActivity("running", f"工具：{title}")
        if card.agents.get(agent) != agent_activity:
            card.agents[agent] = agent_activity
            changed = True
        if changed:
            card.agents.move_to_end(agent)
        return changed

    def set_command_state(
        self, command_id: str, state: str, error: str | None = None
    ) -> bool:
        card = self._cards.get(command_id)
        if card is None:
            return False
        normalized_error = error or ""
        if card.command_state == state and card.error == normalized_error:
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
        if card.agents:
            latest_name = next(reversed(card.agents))
            latest = card.agents[latest_name]
            phase = latest.phase.replace("\n", " ")
            if len(phase) > 40:
                phase = f"{phase[:39]}…"
            focus = f"{latest_name}：{phase}"
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
            tool_summary = "0 个工具"
        header = (
            f"任务 {command_id[:8]} · {card.status_label()} · {focus}"
            f" · {tool_summary} · /details "
            f"{'收起' if expanded else '展开'}"
        )
        if not expanded:
            return header

        rows: list[str] = [header]
        for (category, agent), note in card.notes.items():
            marker = _NOTE_LABELS.get(category, "阶段")
            rows.append(f"  {agent} · {marker} · {note}")
        for tool in card.tools.values():
            status = _TOOL_LABELS.get(
                tool.status.lower(), tool.status or "已记录")
            rows.append(f"  {tool.agent} · {tool.title} · {status}")
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
        return "\n".join(rows)

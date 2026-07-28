"""TUI 固定任务状态区的纯数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


_COMMAND_LABELS = {
    "queued": "排队",
    "running": "运行中",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}
_AGENT_LABELS = {
    "queued": "排队",
    "running": "进行中",
    "waiting_permission": "等待权限",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}
_TERMINAL_AGENT_STATES = {
    "completed", "failed", "cancelled", "interrupted",
}


@dataclass
class AgentProgress:
    state: str = "queued"
    phase: str = "等待派发"


@dataclass
class TaskProgress:
    """一条命令及其各 agent 的可见生命周期。"""

    command_id: str
    status: str = "queued"
    error: str | None = None
    agents: dict[str, AgentProgress] = field(default_factory=dict)

    def set_command(self, status: str, error: str | None = None) -> None:
        self.status = status
        if error:
            self.error = error[:500]

    def set_agent(self, name: str, state: str, phase: str = "") -> None:
        current = self.agents.get(name)
        # 迟到的普通状态或 done 不得把已经记录的失败洗掉。
        if current is not None and current.state == "failed" \
                and state != "failed":
            return
        if current is not None and current.state in _TERMINAL_AGENT_STATES \
                and state in {"queued", "running", "waiting_permission"}:
            return
        clean_phase = " ".join(str(phase).split())[:120]
        if not clean_phase and current is not None:
            clean_phase = current.phase
        self.agents[name] = AgentProgress(
            state=state,
            phase=clean_phase or _AGENT_LABELS.get(state, state),
        )

    @property
    def overall_label(self) -> str:
        worker_states = {
            item.state for name, item in self.agents.items()
            if name not in {"host", "system"}
        }
        states = worker_states or {
            item.state for item in self.agents.values()
        }
        if self.status == "failed" \
                and "completed" in states \
                and bool(states & {"failed", "cancelled"}):
            return "部分完成"
        return _COMMAND_LABELS.get(self.status, self.status)

    def render(self, elapsed_seconds: float) -> str:
        seconds = max(0, int(elapsed_seconds))
        duration = f"{seconds // 60:02d}:{seconds % 60:02d}"
        line = (
            f"任务 {self.command_id[:8]}  {self.overall_label}  {duration}"
        )
        if self.status in {"queued", "running"}:
            line += "  Ctrl+X 取消"
        details = [line]
        for name, item in self.agents.items():
            label = _AGENT_LABELS.get(item.state, item.state)
            details.append(f"{name} {label} · {item.phase}")
        return "\n".join(details)

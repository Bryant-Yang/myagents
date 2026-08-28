"""TUI 固定任务状态区的纯数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from adapters.base import redact_sensitive_text


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
    "skipped": "未执行",
}
_TERMINAL_AGENT_STATES = {
    "completed", "failed", "cancelled", "interrupted", "skipped",
}
_TERMINAL_COMMAND_STATES = {
    "completed", "failed", "cancelled", "interrupted",
}


def format_response_duration(elapsed_seconds: float) -> str:
    """把响应耗时压成短而明确的中文读数。"""
    elapsed = max(0.0, float(elapsed_seconds))
    if elapsed < 10:
        value = f"{elapsed:.1f}".rstrip("0").rstrip(".")
        return f"{value}秒"
    seconds = int(elapsed)
    if seconds < 60:
        return f"{seconds}秒"
    minutes, remaining = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{remaining:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes:02d}分{remaining:02d}秒"


def command_elapsed_seconds(
    created_at: str | None,
    finished_at: str | None = None,
    *,
    now: datetime | None = None,
    allow_open_interval: bool = True,
) -> float | None:
    """从 CommandBus 权威 UTC 时间计算总响应耗时。"""
    if not created_at:
        return None
    if not finished_at and not allow_open_interval:
        return None
    try:
        started = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        finished = (
            datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            if finished_at else (now or datetime.now(timezone.utc))
        )
    except (AttributeError, TypeError, ValueError):
        return None
    if started.tzinfo is None or finished.tzinfo is None:
        return None
    return max(0.0, (finished - started).total_seconds())


@dataclass
class AgentProgress:
    state: str = "queued"
    phase: str = "等待派发"
    session_role: str = ""


@dataclass
class TaskProgress:
    """一条命令及其各 agent 的可见生命周期。"""

    command_id: str
    status: str = "queued"
    error: str | None = None
    agents: dict[str, AgentProgress] = field(default_factory=dict)
    workflow_stage: str | None = None
    workflow_roles: dict[str, str] = field(default_factory=dict)
    steering_available: bool | None = None
    elapsed_seconds: float | None = None

    def set_command(
        self,
        status: str,
        error: str | None = None,
        *,
        elapsed_seconds: float | None = None,
    ) -> None:
        self.status = status
        if error:
            self.error = redact_sensitive_text(str(error), limit=500)
        if elapsed_seconds is not None:
            self.elapsed_seconds = max(0.0, float(elapsed_seconds))
        elif status in _TERMINAL_COMMAND_STATES:
            # 没有权威终点时不能把最后一次 running 读数冒充成终态耗时。
            self.elapsed_seconds = None

    def set_agent(
        self,
        name: str,
        state: str,
        phase: str = "",
        *,
        session_role: str | None = None,
        allow_reentry: bool = False,
    ) -> None:
        clean_name = redact_sensitive_text(
            " ".join(str(name).split()), limit=60) or "agent"
        current = self.agents.get(clean_name)
        # 迟到的普通状态或 done 不得把已经记录的失败洗掉。
        if current is not None and current.state == "failed" \
                and state != "failed":
            return
        if not allow_reentry \
                and current is not None \
                and current.state in _TERMINAL_AGENT_STATES \
                and state in {"queued", "running", "waiting_permission"}:
            return
        clean_phase = redact_sensitive_text(
            " ".join(str(phase).split()), limit=120)
        if not clean_phase and current is not None:
            clean_phase = current.phase
        self.agents[clean_name] = AgentProgress(
            state=state,
            phase=clean_phase or _AGENT_LABELS.get(state, state),
            session_role=(
                _clean_session_role(session_role)
                if session_role is not None
                else (current.session_role if current is not None else "")
            ),
        )

    def set_workflow(
        self,
        stage: object,
        roles: object,
        steering_available: object,
    ) -> None:
        if isinstance(stage, str) and stage:
            self.workflow_stage = stage[:32]
        if isinstance(roles, dict):
            clean = {
                str(key): str(value)
                for key, value in roles.items()
                if key in {"reviewer", "implementer", "verifier"}
            }
            if clean:
                self.workflow_roles = clean
        if isinstance(steering_available, bool):
            self.steering_available = steering_available

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

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_COMMAND_STATES

    def render(self, elapsed_seconds: float | None = None) -> str:
        elapsed = self.elapsed_seconds if elapsed_seconds is None else max(
            0.0, float(elapsed_seconds))
        duration = (
            format_response_duration(elapsed)
            if elapsed is not None else "未知"
        )
        timing_label = "响应耗时" if self.is_terminal else "已用"
        line = (
            f"任务 {self.command_id[:8]}  {self.overall_label}"
            f"  ·  {timing_label} {duration}"
        )
        if self.status in {"queued", "running"}:
            line += "  Ctrl+X 取消"
        details = [line]
        if self.workflow_stage is not None:
            roles = self.workflow_roles
            role_line = " → ".join(filter(None, (
                f"审 {roles.get('reviewer')}" if roles.get("reviewer") else "",
                f"写 {roles.get('implementer')}" if roles.get("implementer") else "",
                f"验 {roles.get('verifier')}" if roles.get("verifier") else "",
            )))
            steering = (
                "可追加 /steer"
                if self.steering_available else "steering 已关闭"
            )
            suffix = f" · {role_line}" if role_line else ""
            details.append(
                f"workflow {self.workflow_stage}{suffix} · {steering}")
        for name, item in self.agents.items():
            label = _AGENT_LABELS.get(item.state, item.state)
            agent_label = (
                f"{name} · {item.session_role}（本会话）"
                if item.session_role else name
            )
            details.append(f"{agent_label} {label} · {item.phase}")
        return "\n".join(details)


def _clean_session_role(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return redact_sensitive_text(
        " ".join(value.split()), limit=40)

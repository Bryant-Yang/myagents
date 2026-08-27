"""Agent 注册与本机就绪状态之间的深模块。

probe 只能观察当前进程的环境和文件系统，不能启动 CLI、联网或安装软件。
调用方只使用 snapshot/require/refresh，不解释各厂商命令路径。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping


_DETAIL_LIMIT = 500


class ReadinessState(str, Enum):
    READY = "ready"
    NOT_FOUND = "not_found"
    INVALID = "invalid"

    @property
    def label(self) -> str:
        return {
            ReadinessState.READY: "可用",
            ReadinessState.NOT_FOUND: "未检测到 CLI",
            ReadinessState.INVALID: "配置无效",
        }[self]


@dataclass(frozen=True)
class AgentReadiness:
    name: str
    state: ReadinessState
    detail: str
    setup_hint: str
    executable: str | None = None

    @property
    def ready(self) -> bool:
        return self.state is ReadinessState.READY


ReadinessProbe = Callable[[], AgentReadiness]
ExecutableResolver = Callable[[str], str | None]


class AgentUnavailableError(ValueError):
    """一条命令引用了当前未就绪的 agent；调用方不得部分派发。"""

    def __init__(
        self,
        statuses: Iterable[AgentReadiness],
        *,
        purpose: str,
    ) -> None:
        items = tuple(statuses)
        self.names = tuple(item.name for item in items)
        detail = "；".join(
            f"@{item.name}：{item.detail}；{item.setup_hint}"
            for item in items
        )
        super().__init__(
            f"{purpose}未开始，以下 agent 当前未就绪：{detail}。"
            "修复后执行 /agents rescan"
        )


class AgentReadinessRegistry:
    """缓存一组被动 probe，并提供原子资格检查。"""

    def __init__(self, probes: Mapping[str, ReadinessProbe]) -> None:
        self._probes = dict(probes)
        self._statuses = {
            name: AgentReadiness(
                name,
                ReadinessState.NOT_FOUND,
                "尚未执行本机就绪探测",
                "执行 /agents rescan",
            )
            for name in self._probes
        }

    def snapshot(self) -> tuple[AgentReadiness, ...]:
        return tuple(self._statuses[name] for name in self._probes)

    def refresh(self) -> tuple[AgentReadiness, ...]:
        updated: dict[str, AgentReadiness] = {}
        for name, probe in self._probes.items():
            try:
                status = probe()
                if status.name != name:
                    raise ValueError(
                        f"probe 返回 agent {status.name!r}，预期 {name!r}")
                if not isinstance(status.state, ReadinessState):
                    raise TypeError("probe 返回了未知 readiness state")
            except Exception as exc:
                previous = self._statuses[name]
                status = AgentReadiness(
                    name,
                    ReadinessState.INVALID,
                    f"就绪探测失败：{exc}"[:_DETAIL_LIMIT],
                    previous.setup_hint,
                )
            updated[name] = status
        self._statuses = updated
        return self.snapshot()

    def require(self, names: Iterable[str], *, purpose: str) -> None:
        missing: list[AgentReadiness] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            status = self._statuses.get(name)
            if status is None:
                status = AgentReadiness(
                    name,
                    ReadinessState.INVALID,
                    "未注册 readiness probe",
                    "从 /agents 中选择已注册 agent",
                )
            if not status.ready:
                missing.append(status)
        if missing:
            raise AgentUnavailableError(missing, purpose=purpose)

    def mark_invalid(
        self,
        name: str,
        detail: str,
        *,
        setup_hint: str | None = None,
    ) -> None:
        previous = self._statuses[name]
        self._statuses[name] = AgentReadiness(
            name,
            ReadinessState.INVALID,
            detail[:_DETAIL_LIMIT],
            setup_hint or previous.setup_hint,
        )

    def set_status(self, status: AgentReadiness) -> None:
        """Replace one registered status after a backend-specific probe."""
        if status.name not in self._probes:
            raise KeyError(f"未注册 readiness probe：{status.name!r}")
        if not isinstance(status.state, ReadinessState):
            raise TypeError("status 包含未知 readiness state")
        self._statuses[status.name] = status


def executable_probe(
    name: str,
    candidates: tuple[str, ...],
    setup_hint: str,
    *,
    resolver: ExecutableResolver | None = None,
) -> ReadinessProbe:
    """构造纯 PATH/文件 probe；不执行候选命令。"""
    if not candidates:
        raise ValueError("candidates 不能为空")
    find = resolver or shutil.which

    def probe() -> AgentReadiness:
        for command in candidates:
            candidate = find(command)
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                return AgentReadiness(
                    name,
                    ReadinessState.INVALID,
                    f"命令 {command} 的路径无效：{exc}"[:_DETAIL_LIMIT],
                    setup_hint,
                )
            if not resolved.is_file() or not os.access(resolved, os.X_OK):
                return AgentReadiness(
                    name,
                    ReadinessState.INVALID,
                    f"命令 {command} 不可执行：{resolved}"[:_DETAIL_LIMIT],
                    setup_hint,
                    str(resolved),
                )
            return AgentReadiness(
                name,
                ReadinessState.READY,
                f"已检测到 CLI：{resolved}"[:_DETAIL_LIMIT],
                setup_hint,
                str(resolved),
            )
        rendered = " / ".join(candidates)
        return AgentReadiness(
            name,
            ReadinessState.NOT_FOUND,
            f"当前进程 PATH 未检测到 {rendered}",
            setup_hint,
        )

    return probe

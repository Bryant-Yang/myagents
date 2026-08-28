"""Provider-neutral context lifecycle contracts.

The orchestrator only consumes the capability shapes defined here.  A
transport that cannot prove a safe compaction path remains observable but is
not treated as compactable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


DEFAULT_CONTEXT_TRIGGER_CHARACTERS = 48_000
DEFAULT_CONTEXT_RETAIN_MESSAGES = 4
DEFAULT_CONTEXT_SUMMARY_CHARACTERS = 4_000
DEFAULT_CONTEXT_SOURCE_CHARACTERS = 64_000
MAX_CONTEXT_SUMMARY_CHARACTERS = 8_000


class ContextLifecycleError(RuntimeError):
    """Context inspection or compaction could not complete safely."""


class ContextCommandValidationError(ValueError):
    """A local context command is malformed."""


@dataclass(frozen=True)
class ContextPolicy:
    """One room's bounded automatic/manual compaction policy."""

    auto_compact: bool = True
    trigger_characters: int = DEFAULT_CONTEXT_TRIGGER_CHARACTERS
    retain_messages: int = DEFAULT_CONTEXT_RETAIN_MESSAGES
    summary_characters: int = DEFAULT_CONTEXT_SUMMARY_CHARACTERS
    source_characters: int = DEFAULT_CONTEXT_SOURCE_CHARACTERS

    def __post_init__(self) -> None:
        if not isinstance(self.auto_compact, bool):
            raise ValueError("auto_compact 必须是 bool")
        for label, value in (
            ("trigger_characters", self.trigger_characters),
            ("retain_messages", self.retain_messages),
            ("summary_characters", self.summary_characters),
            ("source_characters", self.source_characters),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{label} 必须是正整数")
        if self.retain_messages < 2 or self.retain_messages % 2:
            raise ValueError("retain_messages 必须是至少 2 的偶数")
        if self.summary_characters > MAX_CONTEXT_SUMMARY_CHARACTERS:
            raise ValueError(
                f"summary_characters 不能超过 {MAX_CONTEXT_SUMMARY_CHARACTERS}")
        if self.source_characters <= self.summary_characters:
            raise ValueError("source_characters 必须大于 summary_characters")


@dataclass(frozen=True)
class AdapterContextSnapshot:
    """A transport's bounded, non-secret context counters."""

    strategy: str
    compactable: bool
    message_count: int
    character_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, str) or not self.strategy.strip():
            raise ValueError("context strategy 不能为空")
        if not isinstance(self.compactable, bool):
            raise ValueError("compactable 必须是 bool")
        for label, value in (
            ("message_count", self.message_count),
            ("character_count", self.character_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{label} 必须是非负整数")


@dataclass(frozen=True)
class ContextCompactionResult:
    """A completed adapter compaction; summary content stays private."""

    changed: bool
    summary: str
    before_messages: int
    after_messages: int
    source_messages: int
    retained_messages: int
    before_characters: int
    after_characters: int

    def __post_init__(self) -> None:
        if not isinstance(self.changed, bool):
            raise ValueError("changed 必须是 bool")
        if not isinstance(self.summary, str):
            raise ValueError("context summary 必须是字符串")
        if self.changed and not self.summary.strip():
            raise ValueError("有效压缩必须返回非空摘要")
        if len(self.summary) > MAX_CONTEXT_SUMMARY_CHARACTERS:
            raise ValueError("context summary 超过持久化上限")
        for label, value in (
            ("before_messages", self.before_messages),
            ("after_messages", self.after_messages),
            ("source_messages", self.source_messages),
            ("retained_messages", self.retained_messages),
            ("before_characters", self.before_characters),
            ("after_characters", self.after_characters),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{label} 必须是非负整数")
        if self.changed and (
            self.after_messages > self.before_messages
            or self.after_characters >= self.before_characters
        ):
            raise ValueError("context compaction 未实际缩小上下文")
        if self.changed and self.source_messages != self.before_messages:
            raise ValueError("context summary 必须覆盖完整 checkpoint 边界")
        if self.retained_messages > self.after_messages:
            raise ValueError("retained_messages 超出压缩后消息数")


@dataclass(frozen=True)
class ContextCheckpoint:
    """Durable summary covering one agent's timeline through a cursor."""

    boundary_seq: int
    summary: str
    generation: int
    created_at: str
    source_messages: int
    retained_messages: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.boundary_seq, int)
            or isinstance(self.boundary_seq, bool)
            or self.boundary_seq < 0
        ):
            raise ValueError("context checkpoint boundary_seq 非法")
        if (
            not isinstance(self.summary, str)
            or not self.summary.strip()
            or len(self.summary) > MAX_CONTEXT_SUMMARY_CHARACTERS
        ):
            raise ValueError("context checkpoint summary 非法")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation <= 0
        ):
            raise ValueError("context checkpoint generation 非法")
        if not _is_utc_iso(self.created_at):
            raise ValueError("context checkpoint created_at 非法")
        for label, value in (
            ("source_messages", self.source_messages),
            ("retained_messages", self.retained_messages),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"context checkpoint {label} 非法")

    def to_state(self) -> dict:
        return {
            "boundary_seq": self.boundary_seq,
            "summary": self.summary,
            "generation": self.generation,
            "created_at": self.created_at,
            "source_messages": self.source_messages,
            "retained_messages": self.retained_messages,
        }

    @classmethod
    def from_state(cls, value: object) -> "ContextCheckpoint":
        if not isinstance(value, dict):
            raise ValueError("context checkpoint 不是 object")
        expected = {
            "boundary_seq", "summary", "generation", "created_at",
            "source_messages", "retained_messages",
        }
        if set(value) != expected:
            raise ValueError("context checkpoint 字段不完整或含未知字段")
        return cls(
            boundary_seq=value["boundary_seq"],
            summary=value["summary"],
            generation=value["generation"],
            created_at=value["created_at"],
            source_messages=value["source_messages"],
            retained_messages=value["retained_messages"],
        )

    @classmethod
    def create(
        cls,
        *,
        boundary_seq: int,
        summary: str,
        generation: int,
        source_messages: int,
        retained_messages: int,
    ) -> "ContextCheckpoint":
        return cls(
            boundary_seq=boundary_seq,
            summary=summary,
            generation=generation,
            created_at=datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"),
            source_messages=source_messages,
            retained_messages=retained_messages,
        )


@dataclass(frozen=True)
class ContextStatus:
    """User-facing status without summary content or credentials."""

    name: str
    strategy: str
    compactable: bool
    state: str
    detail: str
    message_count: int | None = None
    character_count: int | None = None
    checkpoint_generation: int | None = None
    checkpoint_boundary: int | None = None


@dataclass(frozen=True)
class ContextCommand:
    action: str
    target: str | None = None


def parse_context_command(text: str) -> ContextCommand | None:
    """Parse exact local context commands; unknown slash text stays a message."""
    stripped = text.strip()
    if stripped == "/context":
        return ContextCommand("show")
    if not stripped.startswith("/compact"):
        return None
    parts = stripped.split()
    if parts[0] != "/compact":
        return None
    if len(parts) == 1:
        return ContextCommand("compact", "host")
    if len(parts) != 2:
        raise ContextCommandValidationError(
            "用法：/compact [@agent]；省略目标时压缩 @host")
    if not parts[1].startswith("@"):
        raise ContextCommandValidationError(
            "用法：/compact [@agent]；显式目标必须以 @ 开头")
    target = parts[1][1:].strip()
    if not target or not target.replace("-", "").replace("_", "").isalnum():
        raise ContextCommandValidationError(
            "用法：/compact [@agent]；目标必须是一个已注册 agent")
    return ContextCommand("compact", target)


def _is_utc_iso(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z") or "T" not in value:
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() == timezone.utc.utcoffset(None)

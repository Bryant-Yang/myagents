"""跨项目会话目录。

目录不构造 Orchestrator，也不启动 agent。发现只读；重命名和永久删除会对唯一
room 目标获取短期 lease 并复核身份，不能与活跃 owner 并发写。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from storage.store import (
    DEFAULT_SESSION_NAME,
    CorruptedStorageError,
    TimelineRecord,
    default_state_root,
    normalize_workdir,
    normalize_session_name,
    normalize_session_title,
    room_id_for,
    RoomStore,
    RoomLease,
)
from host_backend import HostBackendSelection

_ROOM_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_MENTION_RE = re.compile(r"(?<!\w)@\w+")
_SHORT_IMAGE_RE = re.compile(r"\[图片\s+\d+\]")
_LEGACY_IMAGE_RE = re.compile(r"\[图片附件：[^\]\r\n]+\]")


class SessionCatalogError(Exception):
    """会话目录无法安全完成操作。"""


@dataclass(frozen=True)
class SessionSummary:
    room_id: str
    session_name: str
    title: str
    workdir: str
    project_name: str
    message_count: int
    last_user_message: str
    last_active_at: str | None
    attachment_count: int


class SessionCatalog:
    """从私有状态根发现并摘要会话，不触碰目标工作区。"""

    def __init__(
        self,
        state_root: str | Path | None = None,
        *,
        default_host_backend: HostBackendSelection | None = None,
    ) -> None:
        self.state_root = (
            Path(state_root).expanduser().resolve()
            if state_root is not None
            else default_state_root()
        )
        self.default_host_backend = (
            default_host_backend or HostBackendSelection.default()
        ).validated()
        self.rooms_root = self.state_root / "rooms"

    def create_session(self, workdir: str | Path) -> SessionSummary:
        """创建稳定 selector 与可变标题分离的新会话。"""
        normalized = normalize_workdir(workdir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        while True:
            session_name = f"chat-{stamp}-{uuid.uuid4().hex[:8]}"
            room_id = room_id_for(normalized, session_name)
            if not (self.rooms_root / room_id).exists():
                break
        store = RoomStore(
            normalized,
            state_root=self.state_root,
            session_name=session_name,
            default_host_backend=self.default_host_backend,
        )
        store.set_session_title("新会话", pending=True)
        return self._read_summary(store.room_dir)

    def get_session(self, room_id: str) -> SessionSummary:
        return self._read_summary(self._room_for_id(room_id))

    def rename_session(self, room_id: str, title: str) -> SessionSummary:
        """在独占 inactive room lease 下修改展示标题。"""
        room_dir = self._room_for_id(room_id)
        before = self._read_summary(room_dir)
        lease = RoomLease(room_dir, before.workdir)
        try:
            store = RoomStore(
                before.workdir,
                state_root=self.state_root,
                session_name=before.session_name,
            )
            store.set_session_title(title, pending=False)
            return self._read_summary(room_dir)
        finally:
            lease.release()

    def delete_session(self, room_id: str, *, confirmation: str) -> None:
        """永久删除一个经身份校验且未被其他 owner 持有的房间。"""
        room_dir = self._room_for_id(room_id)
        before = self._read_summary(room_dir)
        lease = RoomLease(room_dir, before.workdir)
        try:
            current = self._read_summary(room_dir)
            if confirmation != current.title:
                raise ValueError("请完整输入会话标题以确认永久删除")
            if current.room_id != room_id or room_dir.name != room_id:
                raise SessionCatalogError("删除前会话身份发生变化")
            shutil.rmtree(room_dir)
            parent_fd = os.open(self.rooms_root, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            lease.release()

    def _room_for_id(self, room_id: str) -> Path:
        if not isinstance(room_id, str) or _ROOM_ID_RE.fullmatch(room_id) is None:
            raise SessionCatalogError("room_id 必须是 16 位小写十六进制")
        room_dir = self.rooms_root / room_id
        if room_dir.is_symlink() or not room_dir.is_dir():
            raise SessionCatalogError(f"会话不存在：{room_id}")
        if room_dir.resolve().parent != self.rooms_root.resolve():
            raise SessionCatalogError("会话目录逃逸状态根")
        return room_dir
    def list_sessions(
        self,
        *,
        current_workdir: str | Path,
        include_all: bool = False,
        query: str = "",
    ) -> tuple[SessionSummary, ...]:
        current = normalize_workdir(current_workdir)
        if not self.rooms_root.is_dir():
            return ()
        found: list[SessionSummary] = []
        needle = " ".join(query.split()).casefold()
        for room_dir in self.rooms_root.iterdir():
            if not room_dir.is_dir() or room_dir.is_symlink():
                continue
            if _ROOM_ID_RE.fullmatch(room_dir.name) is None:
                continue
            state_path = room_dir / "state.json"
            timeline_path = room_dir / "timeline.jsonl"
            if not state_path.is_file() or not timeline_path.is_file():
                raise SessionCatalogError(
                    f"会话目录不完整：{room_dir.name}"
                )
            summary = self._read_summary(room_dir)
            searchable = "\n".join((
                summary.title,
                summary.last_user_message,
                summary.project_name,
                summary.workdir,
            )).casefold()
            if (include_all or summary.workdir == current) \
                    and (not needle or needle in searchable):
                found.append(summary)
        found.sort(
            key=lambda item: (item.last_active_at or "", item.room_id),
            reverse=True,
        )
        return tuple(found)

    def _read_summary(self, room_dir: Path) -> SessionSummary:
        try:
            data = json.loads((room_dir / "state.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionCatalogError(
                f"会话状态无法读取：{room_dir.name}（{exc}）") from exc
        if not isinstance(data, dict):
            raise SessionCatalogError(f"会话状态不是 object：{room_dir.name}")
        try:
            workdir = normalize_workdir(data["workdir"])
            session_name = normalize_session_name(
                data.get("session_name", DEFAULT_SESSION_NAME)
            )
        except (KeyError, ValueError, OSError) as exc:
            raise SessionCatalogError(
                f"会话身份非法：{room_dir.name}（{exc}）") from exc
        expected = room_id_for(workdir, session_name)
        if room_dir.name != expected or data.get("room_id") != expected:
            raise SessionCatalogError(
                f"会话 room_id 不匹配：目录={room_dir.name!r}，预期={expected!r}"
            )

        messages = 0
        last_user = ""
        last_active: str | None = None
        try:
            with (room_dir / "timeline.jsonl").open("r", encoding="utf-8") as file:
                for line_no, raw in enumerate(file, start=1):
                    if not raw.strip():
                        continue
                    record = TimelineRecord.from_dict(
                        json.loads(raw), line_no=line_no
                    )
                    messages += 1
                    last_active = record.created_at
                    if record.speaker == "user":
                        last_user = record.text
        except (OSError, json.JSONDecodeError, CorruptedStorageError) as exc:
            raise SessionCatalogError(
                f"会话时间线无法读取：{room_dir.name}（{exc}）") from exc

        raw_title = data.get("session_title", session_name)
        try:
            title = normalize_session_title(raw_title)
        except ValueError as exc:
            raise SessionCatalogError(
                f"会话标题非法：{room_dir.name}（{exc}）"
            ) from exc
        pending = data.get("session_title_pending", False)
        if not isinstance(pending, bool):
            raise SessionCatalogError(
                f"会话标题 pending 标记非法：{room_dir.name}"
            )
        updated_at = data.get("session_updated_at")
        if updated_at is not None:
            if not isinstance(updated_at, str):
                raise SessionCatalogError(
                    f"会话更新时间非法：{room_dir.name}"
                )
            try:
                parsed_updated_at = datetime.fromisoformat(
                    updated_at.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise SessionCatalogError(
                    f"会话更新时间非法：{room_dir.name}"
                ) from exc
            if parsed_updated_at.tzinfo is None:
                raise SessionCatalogError(
                    f"会话更新时间缺少时区：{room_dir.name}"
                )
            last_active = max(last_active or updated_at, updated_at)
        attachments = room_dir / "attachments"
        attachment_count = (
            sum(1 for path in attachments.iterdir()
                if path.is_file() and not path.is_symlink())
            if attachments.is_dir() and not attachments.is_symlink()
            else 0
        )
        return SessionSummary(
            room_id=expected,
            session_name=session_name,
            title=title,
            workdir=workdir,
            project_name=Path(workdir).name,
            message_count=messages,
            last_user_message=last_user,
            last_active_at=last_active,
            attachment_count=attachment_count,
        )


def derive_session_title(message: str, *, limit: int = 40) -> str:
    """从第一条用户消息确定性生成展示标题，不调用模型。"""
    text = _MENTION_RE.sub(" ", message)
    text = _SHORT_IMAGE_RE.sub(" ", text)
    text = _LEGACY_IMAGE_RE.sub(" ", text)
    text = " ".join(text.split())
    if not text:
        return "新会话"
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"

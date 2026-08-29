"""房间持久化存储（ADR-0001 §2.1、§2.2）。

关键概念：
- **房间身份**：规范化绝对 workdir 与 session_name 决定稳定 `room_id`
  （SHA-256 截断，不包含可逆路径）；default 会话保持旧 workdir-only 哈希。
- **状态位置**：默认 `${XDG_STATE_HOME:-~/.local/state}/myagents/rooms/<room_id>`，
  测试可注入临时 `state_root`。状态绝不写进目标 workdir。
- **fail loudly**：schema 不支持、timeline/state 损坏、workdir 不匹配都抛
  异常，绝不静默覆盖旧数据；写失败不伪装成功（seq 只在 fsync 后推进，
  内存可见的 agent 状态只在原子写成功后提交）。
- **执行可观测性**：events.jsonl 独立记录命令生命周期、工具、权限和心跳，
  不进入 agent 对话上下文；M3 旧房间在合法 state/timeline 校验后安全补建。
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import threading
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping

from session_roles import (
    SessionRole,
    SessionRoleValidationError,
    normalize_session_roles,
)
from host_backend import (
    HostBackendSelection,
    HostBackendValidationError,
)
from context_lifecycle import ContextCheckpoint

APP_NAME = "myagents"
STATE_SCHEMA_VERSION = 1
DEFAULT_SESSION_NAME = "default"
MAX_SESSION_NAME_CHARS = 64
MAX_SESSION_TITLE_CHARS = 80

DEFAULT_READ_LIMIT = 50
MAX_READ_LIMIT = 200
MAX_TEXT_BYTES = 64 * 1024      # 单条消息文本上限（UTF-8 字节）
MAX_RECORD_BYTES = 128 * 1024   # 单条 JSONL 记录上限（UTF-8 字节）
MAX_PINNED_PLAN_EVENTS = 16
EXECUTION_EVENT_KINDS = frozenset({
    "queued", "running", "status", "tool", "permission", "partial", "plan",
    "steering", "interjection_requested", "interjection_accepted",
    "interjection_failed", "interjection_uncertain",
    "completed", "failed", "cancelled",
})

ROOM_DIR_MODE = 0o700
STATE_FILE_MODE = 0o600


class StorageError(Exception):
    """持久化层异常的公共基类。"""


class SchemaVersionError(StorageError):
    """state.json 的 schema version 不被本版本代码支持。"""


class CorruptedStorageError(StorageError):
    """timeline.jsonl / state.json 内容损坏或不完整。绝不自动修复或覆盖。"""


class WorkdirMismatchError(StorageError):
    """房间目录里记录的 workdir 与本次打开的规范化 workdir 不一致。"""


class RoomBusyError(StorageError):
    """房间已被另一个进程持有（单写者 lease 冲突）。"""


class LimitExceededError(StorageError, ValueError):
    """文本或单条记录超过明确大小上限。"""


def normalize_workdir(workdir: str | Path) -> str:
    """规范化 workdir：展开 ~、解析符号链接、转为绝对路径。

    必须是已存在的目录——给不存在的路径建房间只会制造悬空状态。
    """
    resolved = Path(workdir).expanduser().resolve()
    if not resolved.is_dir():
        raise StorageError(f"workdir 不存在或不是目录：{workdir}")
    return str(resolved)


def normalize_session_name(session_name: str) -> str:
    """规范会话名；名称只进 state/hash，不直接成为文件路径。"""
    if not isinstance(session_name, str):
        raise ValueError("session_name 必须是字符串")
    name = session_name.strip()
    if not name:
        raise ValueError("session_name 不能为空")
    if len(name) > MAX_SESSION_NAME_CHARS:
        raise ValueError(
            f"session_name 不能超过 {MAX_SESSION_NAME_CHARS} 个字符")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("session_name 不能是 .、.. 或包含路径分隔符")
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise ValueError("session_name 不能包含控制或不可见格式字符")
    return name


def normalize_session_title(title: str) -> str:
    """规范可变展示标题；标题不参与路径或 room_id 计算。"""
    if not isinstance(title, str):
        raise ValueError("session_title 必须是字符串")
    value = " ".join(title.split())
    if not value:
        raise ValueError("session_title 不能为空")
    if len(value) > MAX_SESSION_TITLE_CHARS:
        raise ValueError(
            f"session_title 不能超过 {MAX_SESSION_TITLE_CHARS} 个字符")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("session_title 不能包含控制或不可见格式字符")
    return value


def room_id_for(
        normalized_workdir: str,
        session_name: str = DEFAULT_SESSION_NAME) -> str:
    """由 workdir + 会话名派生稳定 room_id；default 保持旧哈希兼容。"""
    name = normalize_session_name(session_name)
    if name == DEFAULT_SESSION_NAME:
        material = normalized_workdir
    else:
        material = f"{normalized_workdir}\0myagents-session\0{name}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def default_state_root() -> Path:
    """默认状态根目录：${XDG_STATE_HOME:-~/.local/state}/myagents。

    返回展开并 resolve 后的绝对路径——相对 XDG_STATE_HOME 也必须先
    resolve，否则 RoomStore 的 workdir 重叠判断会被相对路径绕过。
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return (base / APP_NAME).resolve()


def _is_utc_iso(value: str) -> bool:
    """只接受形如 2026-07-26T08:00:00.123Z 的 UTC ISO-8601 时间戳。"""
    if not value.endswith("Z") or "T" not in value:
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None \
        and parsed.utcoffset() == timezone.utc.utcoffset(None)


@dataclass(frozen=True)
class TimelineRecord:
    """一条时间线记录：单调 seq、speaker、text、UTC created_at、可选 command_id。"""

    seq: int
    speaker: str
    text: str
    created_at: str
    command_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "speaker": self.speaker,
            "text": self.text,
            "created_at": self.created_at,
            "command_id": self.command_id,
        }

    @staticmethod
    def from_dict(data: object, *, line_no: int) -> "TimelineRecord":
        if not isinstance(data, dict):
            raise CorruptedStorageError(
                f"timeline 第 {line_no} 行不是 JSON object")
        try:
            seq = data["seq"]
            speaker = data["speaker"]
            text = data["text"]
            created_at = data["created_at"]
            command_id = data.get("command_id")
        except KeyError as exc:
            raise CorruptedStorageError(
                f"timeline 第 {line_no} 行缺少字段 {exc}") from exc
        if (not isinstance(seq, int) or isinstance(seq, bool)
                or not isinstance(speaker, str) or not isinstance(text, str)
                or not isinstance(created_at, str)
                or (command_id is not None and not isinstance(command_id, str))):
            raise CorruptedStorageError(
                f"timeline 第 {line_no} 行字段类型非法")
        # 语义校验：重开/读取时必须拒绝写入路径放不进来的值，
        # 否则损坏记录会被当成合法历史继续服务。
        if seq <= 0:
            raise CorruptedStorageError(
                f"timeline 第 {line_no} 行 seq 非法：{seq}")
        if not speaker:
            raise CorruptedStorageError(
                f"timeline 第 {line_no} 行 speaker 为空")
        if not _is_utc_iso(created_at):
            raise CorruptedStorageError(
                f"timeline 第 {line_no} 行 created_at 非 UTC-Z 格式："
                f"{created_at!r}")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise CorruptedStorageError(
                f"timeline 第 {line_no} 行 text 超过上限 {MAX_TEXT_BYTES} 字节")
        return TimelineRecord(seq=seq, speaker=speaker, text=text,
                              created_at=created_at, command_id=command_id)


@dataclass(frozen=True)
class ExecutionEventRecord:
    """不进入对话上下文的执行事件。"""

    seq: int
    command_id: str
    agent: str
    kind: str
    text: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "command_id": self.command_id,
            "agent": self.agent,
            "kind": self.kind,
            "text": self.text,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: object, *, line_no: int) -> "ExecutionEventRecord":
        if not isinstance(data, dict):
            raise CorruptedStorageError(
                f"events 第 {line_no} 行不是 JSON object")
        expected = {
            "seq", "command_id", "agent", "kind", "text", "created_at"}
        if set(data) != expected:
            raise CorruptedStorageError(
                f"events 第 {line_no} 行字段不完整或含未知字段")
        seq = data["seq"]
        command_id = data["command_id"]
        agent = data["agent"]
        kind = data["kind"]
        text = data["text"]
        created_at = data["created_at"]
        if (not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0
                or not isinstance(command_id, str) or not command_id
                or not isinstance(agent, str) or not agent
                or kind not in EXECUTION_EVENT_KINDS
                or not isinstance(text, str)
                or not isinstance(created_at, str)
                or not _is_utc_iso(created_at)):
            raise CorruptedStorageError(
                f"events 第 {line_no} 行字段非法")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise CorruptedStorageError(
                f"events 第 {line_no} 行 text 超过上限 {MAX_TEXT_BYTES} 字节")
        return ExecutionEventRecord(
            seq=seq, command_id=command_id, agent=agent, kind=kind,
            text=text, created_at=created_at)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RoomLease:
    """房间单写者 lease：room_dir/owner.lock 上的进程级 flock。

    - Unix/macOS：fcntl.flock(LOCK_EX|LOCK_NB)，获取失败抛 RoomBusyError；
    - lock 文件 0600，写入当前 PID（flush+fsync）供冲突错误提示；
    - 绝不删除 owner.lock——删除已锁 inode 会产生双锁竞态；进程异常
      退出由 OS 释放 flock，stale 文件不妨碍下次获取；
    - release() 幂等；支持 context manager；__del__ best effort。
    """

    def __init__(self, room_dir: Path, workdir: str) -> None:
        self._path = room_dir / "owner.lock"
        self._fd: int | None = None
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, STATE_FILE_MODE)
        try:
            # 已存在文件也强制规范权限；open/chmod 失败保持原异常
            # （不是 busy），fd 不泄露
            os.chmod(self._path, STATE_FILE_MODE)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # 只有 EAGAIN/EWOULDBLOCK 才是锁冲突（busy）：
                # 读旧 PID 供提示后抛 RoomBusyError
                pid_hint = ""
                with contextlib.suppress(OSError):
                    pid_hint = self._path.read_text(
                        encoding="utf-8").strip()
                raise RoomBusyError(
                    f"房间已被其他进程持有：{room_dir}（workdir={workdir!r}"
                    + (f"，owner PID={pid_hint}" if pid_hint else "") + ")"
                ) from None
        except BaseException:
            os.close(fd)
            raise
        # flock 已成功：PID 写入任一步失败（含 KeyboardInterrupt/
        # Cancelled）必须立即 unlock+close，原样传播，不依赖 __del__
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.fsync(fd)
        except BaseException:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            raise
        self._fd = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        """幂等：unlock + close。不删除 lock 文件（见类 docstring）。"""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "RoomLease":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.release()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class RoomStore:
    """房间存储：对话 timeline、执行 events 与原子 state。"""

    def __init__(self, workdir: str | Path,
                 state_root: str | Path | None = None, *,
                 session_name: str = DEFAULT_SESSION_NAME,
                 default_host_backend: HostBackendSelection | None = None,
                 ) -> None:
        self.workdir = normalize_workdir(workdir)
        self.session_name = normalize_session_name(session_name)
        self.default_host_backend = (
            default_host_backend or HostBackendSelection.default()
        ).validated()
        self.room_name = Path(self.workdir).name
        self.state_root = (Path(state_root).expanduser().resolve()
                           if state_root is not None else default_state_root())
        workdir_path = Path(self.workdir)
        if self.state_root == workdir_path \
                or self.state_root.is_relative_to(workdir_path):
            raise StorageError(
                f"state_root 不能等于 workdir 或位于其子目录内："
                f"{self.state_root}（workdir={self.workdir}）")
        self.room_id = room_id_for(self.workdir, self.session_name)
        self.room_dir = self.state_root / "rooms" / self.room_id
        self.timeline_path = self.room_dir / "timeline.jsonl"
        self.events_path = self.room_dir / "events.jsonl"
        self.state_path = self.room_dir / "state.json"
        self._state: dict = {}
        self._next_seq = 1
        self._next_event_seq = 1
        self._events_lock = threading.RLock()
        self._open()

    @classmethod
    def session_exists(
            cls, workdir: str | Path, session_name: str,
            state_root: str | Path | None = None) -> bool:
        """只检查命名房间路径是否存在，不创建状态或获取 lease。"""
        normalized_workdir = normalize_workdir(workdir)
        normalized_session = normalize_session_name(session_name)
        root = (Path(state_root).expanduser().resolve()
                if state_root is not None else default_state_root())
        room_id = room_id_for(normalized_workdir, normalized_session)
        return (root / "rooms" / room_id).exists()

    # ---- 打开 / 初始化 ----

    def _open(self) -> None:
        if self.room_dir.exists():
            if not self.room_dir.is_dir():
                raise CorruptedStorageError(
                    f"房间路径存在但不是目录：{self.room_dir}")
            entries = list(self.room_dir.iterdir())
            if entries and not self.state_path.is_file():
                # 有内容却没有 state.json：半初始化或被破坏，绝不覆盖
                raise CorruptedStorageError(
                    f"房间目录存在但缺少 state.json：{self.room_dir}")
            if entries:
                self._load_state()
                self._next_seq = self._scan_timeline()
                # M3 房间没有 events.jsonl：先验证原状态/时间线，再补建空文件。
                if not self.events_path.exists():
                    self._create_empty_events()
                self._next_event_seq = self._scan_events()
                # 内容校验通过后才动权限：目录和状态文件都强制规范值
                os.chmod(self.room_dir, ROOM_DIR_MODE)
                os.chmod(self.state_path, STATE_FILE_MODE)
                os.chmod(self.timeline_path, STATE_FILE_MODE)
                os.chmod(self.events_path, STATE_FILE_MODE)
                return
            # 空目录：视为未初始化，走新建流程
        self._init_fresh()

    def _init_fresh(self) -> None:
        self.room_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.room_dir, ROOM_DIR_MODE)
        state = {
            "schema": STATE_SCHEMA_VERSION,
            "room_id": self.room_id,
            "room_name": self.room_name,
            "workdir": self.workdir,
            "session_name": self.session_name,
            "agents": {},
            "session_roles": {},
            "host_backend": self.default_host_backend.to_state(),
            "host_replay_floor": 0,
            "context_checkpoints": {},
        }
        self._write_state(state)
        self._state = state
        # 创建空 append-only 文件（0600），让权限从第一天就正确
        fd = os.open(self.timeline_path,
                     os.O_CREAT | os.O_APPEND | os.O_WRONLY, STATE_FILE_MODE)
        os.close(fd)
        os.chmod(self.timeline_path, STATE_FILE_MODE)
        self._create_empty_events()
        _fsync_dir(self.room_dir)

    def _create_empty_events(self) -> None:
        fd = os.open(self.events_path,
                     os.O_CREAT | os.O_APPEND | os.O_WRONLY, STATE_FILE_MODE)
        os.close(fd)
        os.chmod(self.events_path, STATE_FILE_MODE)
        _fsync_dir(self.room_dir)

    def _load_state(self) -> None:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise CorruptedStorageError(
                f"state.json 无法解析：{self.state_path}（{exc}）") from exc
        if not isinstance(data, dict):
            raise CorruptedStorageError("state.json 不是 JSON object")
        schema = data.get("schema")
        if schema != STATE_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"不支持的 state schema：{schema!r}"
                f"（本版本支持 {STATE_SCHEMA_VERSION}）")
        if data.get("room_id") != self.room_id:
            raise CorruptedStorageError(
                f"state.json 的 room_id 与目录不符：{data.get('room_id')!r}")
        if data.get("workdir") != self.workdir:
            raise WorkdirMismatchError(
                f"房间记录的 workdir 是 {data.get('workdir')!r}，"
                f"与本次打开的 {self.workdir!r} 不一致")
        recorded_session = data.get(
            "session_name", DEFAULT_SESSION_NAME)
        if recorded_session != self.session_name:
            raise CorruptedStorageError(
                f"房间记录的 session_name 是 {recorded_session!r}，"
                f"与本次打开的 {self.session_name!r} 不一致")
        if not isinstance(data.get("agents"), dict):
            raise CorruptedStorageError("state.json 缺少合法的 agents 映射")
        try:
            normalize_session_roles(data.get("session_roles", {}))
        except SessionRoleValidationError as exc:
            raise CorruptedStorageError(
                f"state.json 的 session_roles 非法：{exc}") from exc
        try:
            HostBackendSelection.from_state(data.get("host_backend"))
        except HostBackendValidationError as exc:
            raise CorruptedStorageError(
                f"state.json 的 host_backend 非法：{exc}") from exc
        replay_floor = data.get("host_replay_floor", 0)
        if (not isinstance(replay_floor, int) or isinstance(replay_floor, bool)
                or replay_floor < 0):
            raise CorruptedStorageError(
                "state.json 的 host_replay_floor 必须是非负整数")
        checkpoints = data.get("context_checkpoints", {})
        if not isinstance(checkpoints, dict):
            raise CorruptedStorageError(
                "state.json 的 context_checkpoints 必须是 object")
        for agent_name, checkpoint in checkpoints.items():
            if not isinstance(agent_name, str) or not agent_name:
                raise CorruptedStorageError(
                    "state.json 的 context checkpoint agent 名非法")
            try:
                ContextCheckpoint.from_state(checkpoint)
            except ValueError as exc:
                raise CorruptedStorageError(
                    f"agent {agent_name!r} 的 context checkpoint 非法：{exc}"
                ) from exc
        if "session_title" in data:
            try:
                normalize_session_title(data["session_title"])
            except ValueError as exc:
                raise CorruptedStorageError(
                    f"state.json 的 session_title 非法：{exc}"
                ) from exc
        if not isinstance(data.get("session_title_pending", False), bool):
            raise CorruptedStorageError(
                "state.json 的 session_title_pending 非 bool"
            )
        updated_at = data.get("session_updated_at")
        if updated_at is not None and (
            not isinstance(updated_at, str) or not _is_utc_iso(updated_at)
        ):
            raise CorruptedStorageError(
                "state.json 的 session_updated_at 非法"
            )
        # 打开时全量校验 agents entry：损坏状态在构造阶段立即 fail loudly，
        # 而不是等该 agent 首次访问才发现
        for agent_name, entry in data["agents"].items():
            self._validate_agent_entry(agent_name, entry)
        self._state = data

    def _scan_timeline(self) -> int:
        """校验整条 timeline 并返回下一个 seq。任何损坏都 fail loudly。"""
        last_seq = 0
        for record in self._load_timeline():
            if record.seq <= last_seq:
                raise CorruptedStorageError(
                    f"timeline seq 非严格递增：{record.seq} 出现在 {last_seq} 之后")
            last_seq = record.seq
        return last_seq + 1

    def _load_timeline(self) -> list[TimelineRecord]:
        if not self.timeline_path.is_file():
            raise CorruptedStorageError(
                f"缺少 timeline.jsonl：{self.timeline_path}")
        records: list[TimelineRecord] = []
        with open(self.timeline_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
                    raise CorruptedStorageError(
                        f"timeline 第 {line_no} 行超过单条记录上限 "
                        f"{MAX_RECORD_BYTES} 字节")
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CorruptedStorageError(
                        f"timeline 第 {line_no} 行不是合法 JSON：{exc}") from exc
                records.append(TimelineRecord.from_dict(data, line_no=line_no))
        return records

    def _scan_events(self) -> int:
        last_seq = 0
        for record in self._load_events():
            if record.seq <= last_seq:
                raise CorruptedStorageError(
                    f"events seq 非严格递增：{record.seq} 出现在 {last_seq} 之后")
            last_seq = record.seq
        return last_seq + 1

    def _load_events(self) -> list[ExecutionEventRecord]:
        return list(self._iter_event_snapshot())

    def _iter_event_snapshot(self) -> Iterator[ExecutionEventRecord]:
        """流式读取一个完整前缀；只在取得 fd/size 时短暂阻塞 append。

        events 文件只追加不回写。锁内取得的文件大小因此定义了稳定前缀，后续
        解析无需继续持锁，也不会把 Textual event loop 的同步事件写入卡在长扫描
        后面。每次最多读取一条记录大小，避免为详情全量载入文件。
        """
        with self._events_lock:
            if not self.events_path.is_file():
                raise CorruptedStorageError(
                    f"缺少 events.jsonl：{self.events_path}")
            fd = os.open(self.events_path, os.O_RDONLY)
            try:
                snapshot_size = os.fstat(fd).st_size
            except BaseException:
                os.close(fd)
                raise
        try:
            with os.fdopen(fd, "rb") as stream:
                remaining = snapshot_size
                line_no = 0
                while remaining:
                    line_no += 1
                    raw = stream.readline(
                        min(remaining, MAX_RECORD_BYTES + 1))
                    if not raw:
                        raise CorruptedStorageError(
                            "events.jsonl 在读取快照期间意外截断")
                    remaining -= len(raw)
                    if len(raw) > MAX_RECORD_BYTES:
                        raise CorruptedStorageError(
                            f"events 第 {line_no} 行超过单条记录上限 "
                            f"{MAX_RECORD_BYTES} 字节")
                    if not raw.endswith(b"\n"):
                        raise CorruptedStorageError(
                            f"events 第 {line_no} 行缺少换行终止符")
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise CorruptedStorageError(
                            f"events 第 {line_no} 行不是合法 JSON：{exc}"
                        ) from exc
                    yield ExecutionEventRecord.from_dict(
                        data, line_no=line_no)
        except BaseException:
            # fdopen 未取得所有权前出错时仍需关闭；正常 with 退出后 EBADF 忽略。
            with contextlib.suppress(OSError):
                os.close(fd)
            raise

    # ---- 单写者 lease ----

    def acquire_owner(self) -> RoomLease:
        """获取本房间的进程级单写者 lease；已被持有时抛 RoomBusyError。

        只读辅助实例不要调用——lease 只属于负责写入的 owner
        （TUI 的 Orchestrator）；外部工具（如 MCP）应走 command bus。
        """
        return RoomLease(self.room_dir, self.workdir)

    # ---- state.json：同目录临时文件 + os.replace 原子写 ----

    def _write_state(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.room_dir,
                                   prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, STATE_FILE_MODE)
            os.replace(tmp, self.state_path)
            # rename 本身的持久性：fsync 目录项，崩溃后临时文件不会复活成
            # state.json、新名字也不会丢失
            _fsync_dir(self.room_dir)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- agent cursor / session 映射（ADR-0001 §2.2） ----

    @staticmethod
    def _validate_agent_entry(name: str, entry: object) -> dict:
        """校验一条持久化 agent 状态；返回规范化 {"cursor", "session_id"}。

        任一违反都抛 CorruptedStorageError，绝不静默 bootstrap 成默认值
        （那会丢 cursor、重复投递）：
        - 必须是 object，且显式包含 cursor 与 session_id 两个字段；
        - cursor 必须 int、非 bool、>= 0；
        - session_id 必须 None 或非空 str。
        """
        if not isinstance(entry, dict):
            raise CorruptedStorageError(
                f"agent {name!r} 的持久化状态不是 object：{entry!r}")
        if "cursor" not in entry or "session_id" not in entry:
            raise CorruptedStorageError(
                f"agent {name!r} 的持久化状态缺少 cursor/session_id 字段")
        cursor = entry["cursor"]
        session_id = entry["session_id"]
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise CorruptedStorageError(
                f"agent {name!r} 的持久化 cursor 非法：{cursor!r}")
        if session_id is not None \
                and (not isinstance(session_id, str) or not session_id):
            raise CorruptedStorageError(
                f"agent {name!r} 的持久化 session_id 非法：{session_id!r}")
        return {"cursor": cursor, "session_id": session_id}

    def get_agent_state(self, name: str) -> dict:
        """返回 {"cursor": int, "session_id": str | None}；未记录时为默认值。

        key 存在但内容损坏时抛 CorruptedStorageError——损坏状态绝不静默
        重置成默认值（那会丢 cursor、重复投递）。
        """
        if name not in self._state["agents"]:
            return {"cursor": 0, "session_id": None}
        return self._validate_agent_entry(name, self._state["agents"][name])

    @property
    def session_title(self) -> str:
        raw = self._state.get("session_title", self.session_name)
        if not isinstance(raw, str):
            raise CorruptedStorageError("state.json 的 session_title 非字符串")
        try:
            return normalize_session_title(raw)
        except ValueError as exc:
            raise CorruptedStorageError(
                f"state.json 的 session_title 非法：{exc}") from exc

    @property
    def session_title_pending(self) -> bool:
        value = self._state.get("session_title_pending", False)
        if not isinstance(value, bool):
            raise CorruptedStorageError(
                "state.json 的 session_title_pending 非 bool")
        return value

    def set_session_title(self, title: str, *, pending: bool = False) -> None:
        """原子更新展示标题；不改变稳定 session_name/room_id。"""
        normalized = normalize_session_title(title)
        if not isinstance(pending, bool):
            raise ValueError("pending 必须是 bool")
        new_state = {
            **self._state,
            "session_title": normalized,
            "session_title_pending": pending,
            "session_updated_at": _utc_now_iso(),
        }
        self._write_state(new_state)
        self._state = new_state

    def get_session_roles(self) -> dict[str, SessionRole]:
        """返回当前房间角色快照；旧房间缺字段时为空。"""
        try:
            return normalize_session_roles(
                self._state.get("session_roles", {}))
        except SessionRoleValidationError as exc:
            raise CorruptedStorageError(
                f"state.json 的 session_roles 非法：{exc}") from exc

    def set_session_roles(
        self,
        roles: Mapping[str, SessionRole],
    ) -> None:
        """原子替换会话角色；写失败时内存和磁盘都保持旧值。"""
        normalized = normalize_session_roles(dict(roles))
        new_state = {
            **self._state,
            "session_roles": {
                name: role.to_state() for name, role in normalized.items()
            },
        }
        self._write_state(new_state)
        self._state = new_state

    def get_host_backend(self) -> HostBackendSelection:
        """Return the room selection; legacy rooms default to native model."""
        try:
            return HostBackendSelection.from_state(
                self._state.get("host_backend"))
        except HostBackendValidationError as exc:
            raise CorruptedStorageError(
                f"state.json 的 host_backend 非法：{exc}") from exc

    def get_host_replay_floor(self) -> int:
        """Lowest timeline cursor any fresh Host runtime may replay from."""
        return int(self._state.get("host_replay_floor", 0))

    def set_host_backend(
        self,
        selection: HostBackendSelection,
        *,
        cursor: int,
    ) -> None:
        """Persist a fresh backend at a durable cross-backend replay boundary."""
        selected = selection.validated()
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValueError("host backend cursor 必须是非负整数")
        host_state = {"cursor": cursor, "session_id": None}
        new_state = {
            **self._state,
            "host_backend": selected.to_state(),
            "host_replay_floor": cursor,
            "agents": {**self._state["agents"], "host": host_state},
            "context_checkpoints": {
                name: value
                for name, value in self._state.get(
                    "context_checkpoints", {}).items()
                if name != "host"
            },
        }
        self._write_state(new_state)
        self._state = new_state

    def commit_host_cursor(self, cursor: int) -> None:
        """Atomically advance Host cursor and its durable no-replay floor."""
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValueError("host cursor 必须是非负整数")
        current = self.get_agent_state("host")
        committed = max(current["cursor"], cursor)
        host_state = {
            "cursor": committed,
            "session_id": current["session_id"],
        }
        new_state = {
            **self._state,
            "host_replay_floor": max(
                self.get_host_replay_floor(), committed),
            "agents": {**self._state["agents"], "host": host_state},
        }
        self._write_state(new_state)
        self._state = new_state

    def set_agent_state(self, name: str, *, cursor: int | None = None,
                        session_id: str | None = None) -> None:
        """持久化某 stateful agent 的 cursor / ACP session_id（原子写）。

        copy-on-write：先构造新 state 再落盘，只有原子写成功后才提交到
        内存可见状态。写盘失败时内存和磁盘都保持旧值，绝不伪装成功。
        """
        new_entry = self.get_agent_state(name)
        if cursor is not None:
            if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
                raise ValueError(f"cursor 必须是非负 int：{cursor!r}")
            new_entry["cursor"] = cursor
        if session_id is not None:
            new_entry["session_id"] = session_id
        new_state = {**self._state,
                     "agents": {**self._state["agents"], name: new_entry}}
        self._write_state(new_state)
        self._state = new_state

    # ---- context lifecycle checkpoints（ADR-0020） ----

    def get_context_checkpoints(self) -> dict[str, ContextCheckpoint]:
        """Return a validated copy; legacy rooms default to no checkpoints."""
        raw = self._state.get("context_checkpoints", {})
        if not isinstance(raw, dict):
            raise CorruptedStorageError(
                "state.json 的 context_checkpoints 必须是 object")
        result: dict[str, ContextCheckpoint] = {}
        for name, value in raw.items():
            try:
                result[name] = ContextCheckpoint.from_state(value)
            except ValueError as exc:
                raise CorruptedStorageError(
                    f"agent {name!r} 的 context checkpoint 非法：{exc}"
                ) from exc
        return result

    def set_context_checkpoint(
        self,
        name: str,
        checkpoint: ContextCheckpoint,
    ) -> None:
        """Atomically persist one private summary checkpoint."""
        if not isinstance(name, str) or not name:
            raise ValueError("context checkpoint agent 名不能为空")
        if not isinstance(checkpoint, ContextCheckpoint):
            raise ValueError("checkpoint 必须是 ContextCheckpoint")
        current = self.get_context_checkpoints()
        previous = current.get(name)
        if previous is not None:
            if checkpoint.generation <= previous.generation:
                raise ValueError("context checkpoint generation 必须单调递增")
            if checkpoint.boundary_seq < previous.boundary_seq:
                raise ValueError("context checkpoint boundary 不得倒退")
        new_state = {
            **self._state,
            "context_checkpoints": {
                **self._state.get("context_checkpoints", {}),
                name: checkpoint.to_state(),
            },
        }
        self._write_state(new_state)
        self._state = new_state

    # ---- timeline ----

    def append(self, speaker: str, text: str,
               command_id: str | None = None) -> TimelineRecord:
        """append 一条记录并 flush + fsync；seq 只在写盘成功后推进。"""
        if not speaker:
            raise ValueError("speaker 不能为空")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise LimitExceededError(
                f"文本超过上限 {MAX_TEXT_BYTES} 字节："
                f"{len(text.encode('utf-8'))}")
        record = TimelineRecord(seq=self._next_seq, speaker=speaker, text=text,
                                created_at=_utc_now_iso(), command_id=command_id)
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
            raise LimitExceededError(
                f"单条记录超过上限 {MAX_RECORD_BYTES} 字节")
        fd = os.open(self.timeline_path, os.O_APPEND | os.O_WRONLY)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            # 写失败不伪装成功：seq 不推进，下次 append 重用同一 seq
            raise
        self._next_seq += 1
        return record

    def read(self, after_seq: int = 0,
             limit: int = DEFAULT_READ_LIMIT) -> dict:
        """有界读取：after_seq 之后最多 limit 条（默认 50，最大 200）。

        返回 {"items": [TimelineRecord], "has_more": bool,
        "next_after_seq": int}。
        """
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) \
                or after_seq < 0:
            raise ValueError(f"after_seq 必须是非负 int：{after_seq!r}")
        limit = max(1, min(int(limit), MAX_READ_LIMIT))
        records = [r for r in self._load_timeline() if r.seq > after_seq]
        items = records[:limit]
        return {
            "items": items,
            "has_more": len(records) > len(items),
            "next_after_seq": items[-1].seq if items else after_seq,
        }

    # ---- execution events（不进入对话 history）----

    def append_event(self, *, command_id: str, agent: str,
                     kind: str, text: str) -> ExecutionEventRecord:
        if not command_id:
            raise ValueError("command_id 不能为空")
        if not agent:
            raise ValueError("agent 不能为空")
        if kind not in EXECUTION_EVENT_KINDS:
            raise ValueError(f"未知执行事件 kind：{kind!r}")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise LimitExceededError(
                f"执行事件文本超过上限 {MAX_TEXT_BYTES} 字节")
        with self._events_lock:
            record = ExecutionEventRecord(
                seq=self._next_event_seq, command_id=command_id,
                agent=agent, kind=kind, text=text,
                created_at=_utc_now_iso())
            line = json.dumps(record.to_dict(), ensure_ascii=False)
            if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
                raise LimitExceededError(
                    f"执行事件记录超过上限 {MAX_RECORD_BYTES} 字节")
            fd = os.open(self.events_path, os.O_APPEND | os.O_WRONLY)
            try:
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                raise
            self._next_event_seq += 1
            return record

    def read_events(self, after_seq: int = 0,
                    limit: int = DEFAULT_READ_LIMIT) -> dict:
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) \
                or after_seq < 0:
            raise ValueError(f"after_seq 必须是非负 int：{after_seq!r}")
        limit = max(1, min(int(limit), MAX_READ_LIMIT))
        records = [r for r in self._load_events() if r.seq > after_seq]
        items = records[:limit]
        return {
            "items": items,
            "has_more": len(records) > len(items),
            "next_after_seq": items[-1].seq if items else after_seq,
        }

    def read_command_events(
        self,
        command_id: str,
        limit: int = MAX_READ_LIMIT,
    ) -> dict:
        """有界读取一个 command 的过程事件，并保留首尾生命周期证据。

        详情视图按需调用该接口。事件总量超过上限时返回代表性的头尾窗口，
        不会因只取前 ``limit`` 条而把 terminal 结果藏掉。
        """
        if not isinstance(command_id, str) or not command_id:
            raise ValueError("command_id 不能为空")
        bounded_limit = max(1, min(int(limit), MAX_READ_LIMIT))
        return self._command_events_page(
            self._iter_event_snapshot(), command_id, bounded_limit)

    def read_latest_command_events(
        self,
        limit: int = MAX_READ_LIMIT,
    ) -> dict | None:
        """单次扫描返回最近 command 的详情；空日志返回 ``None``。"""
        bounded_limit = max(1, min(int(limit), MAX_READ_LIMIT))
        command_id: str | None = None
        for record in self._iter_event_snapshot():
            command_id = record.command_id
        if command_id is None:
            return None
        return self._command_events_page(
            self._iter_event_snapshot(), command_id, bounded_limit)

    @staticmethod
    def _command_events_page(
        all_records: Iterable[ExecutionEventRecord],
        command_id: str,
        bounded_limit: int,
    ) -> dict:
        head_count = 0 if bounded_limit == 1 else max(
            1, bounded_limit // 3)
        tail_count = bounded_limit - head_count
        head: list[ExecutionEventRecord] = []
        tail: deque[ExecutionEventRecord] = deque(maxlen=tail_count)
        first_plan: ExecutionEventRecord | None = None
        recent_plan: deque[ExecutionEventRecord] = deque(
            maxlen=MAX_PINNED_PLAN_EVENTS - 1)
        total_count = 0
        kind_counts: dict[str, int] = {}
        agents: dict[str, None] = {}
        partial_char_count = 0
        for record in all_records:
            if record.command_id != command_id:
                continue
            total_count += 1
            kind_counts[record.kind] = kind_counts.get(record.kind, 0) + 1
            agents.setdefault(record.agent, None)
            if record.kind == "partial":
                partial_char_count += len(record.text)
            if record.kind == "plan":
                if first_plan is None:
                    first_plan = record
                else:
                    recent_plan.append(record)
            if len(head) < head_count:
                head.append(record)
            else:
                tail.append(record)
        candidates = {
            record.seq: record
            for record in (
                head
                + ([first_plan] if first_plan is not None else [])
                + list(recent_plan)
                + list(tail)
            )
        }
        if len(candidates) <= bounded_limit:
            items = sorted(candidates.values(), key=lambda record: record.seq)
        else:
            # 详情优先保留首尾事实和版本化计划；极小 limit 下仍严格有界。
            ordered = sorted(candidates.values(), key=lambda record: record.seq)
            selected: dict[int, ExecutionEventRecord] = {}
            if bounded_limit == 1:
                selected[ordered[-1].seq] = ordered[-1]
            else:
                selected[ordered[0].seq] = ordered[0]
                selected[ordered[-1].seq] = ordered[-1]
                for record in ordered:
                    if len(selected) >= bounded_limit:
                        break
                    if record.kind == "plan":
                        selected[record.seq] = record
                if len(selected) < bounded_limit:
                    for record in reversed(ordered):
                        selected[record.seq] = record
                        if len(selected) >= bounded_limit:
                            break
            items = sorted(selected.values(), key=lambda record: record.seq)
        return {
            "command_id": command_id,
            "items": items,
            "total_count": total_count,
            "omitted_count": total_count - len(items),
            "kind_counts": kind_counts,
            "agents": tuple(agents),
            "partial_char_count": partial_char_count,
        }

    def latest_execution_events(self) -> list[ExecutionEventRecord]:
        """每个 command 的最后一条事件，按事件 seq 排序。"""
        latest: dict[str, ExecutionEventRecord] = {}
        for record in self._iter_event_snapshot():
            latest[record.command_id] = record
        return sorted(latest.values(), key=lambda record: record.seq)

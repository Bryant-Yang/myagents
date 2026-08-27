"""Pi native RPC -> AgentAdapter bridge.

Pi RPC is LF-delimited JSON over stdio, not ACP.  This adapter keeps that
vendor protocol in a deep module while exposing the same stateful session,
permission, execution-mode, cancellation, and no-replay seams used by the
orchestrator.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Sequence

from adapters.base import (
    DEFAULT_AGENT_INACTIVITY_TIMEOUT,
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    AgentEvent,
    ExecutionMode,
    redact_sensitive_text,
)
from clipboard_image import MAX_CLIPBOARD_IMAGE_BYTES, prompt_images
from storage.store import default_state_root

from .client import PiRpcClient


PI_POLICY_VERSION = "myagents.pi.policy/v1"
PI_ATTEST_PREFIX = "MYAGENTS_PI_ATTEST_V1:"
PI_PERMISSION_PREFIX = "MYAGENTS_PI_PERMISSION_V1:"
PI_READY_STATUS_KEY = "myagents.pi.policy"
PI_POLICY_COMMAND = "myagents-policy-v1"

PI_READ_ONLY_TOOLS = (
    "myagents_read",
    "myagents_grep",
    "myagents_find",
    "myagents_ls",
)
PI_MUTATING_TOOLS = (
    "myagents_edit",
    "myagents_write",
    "myagents_bash",
)
PI_ALL_TOOLS = (*PI_READ_ONLY_TOOLS, *PI_MUTATING_TOOLS)

_POLICY_NONCE_ENV = "MYAGENTS_PI_POLICY_NONCE"
_POLICY_HASH_ENV = "MYAGENTS_PI_POLICY_HASH"
_POLICY_PATH_ENV = "MYAGENTS_PI_POLICY_PATH"
_PROFILE_ENV = "MYAGENTS_PI_PROFILE"
_WORKSPACE_ENV = "MYAGENTS_PI_WORKSPACE"
_SESSION_ROOT_ENV = "MYAGENTS_PI_SESSION_ROOT"
_SESSION_TOKEN_PREFIX = "pi:v1:"
_SESSION_MARKER_VERSION = "myagents.pi.session-marker/v1"
_MAX_ENVELOPE_BYTES = 16 * 1024
_MAX_SESSION_HEADER_BYTES = 128 * 1024
_MAX_SESSION_MARKER_BYTES = 32 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_MAX_PROMPT_IMAGES = 16
_PERMISSION_EVENT_QUEUE_LIMIT = 256
_PI_TERMINAL_SUCCESS_STOP_REASONS = frozenset({
    "stop", "length", "toolUse", "deferred",
})
_PI_TERMINAL_FAILURE_STOP_REASONS = frozenset({"error", "aborted"})


class PiRpcError(RuntimeError):
    """Pi transport, policy attestation, or session contract failed."""


AgentPermissionHandler = Callable[[str, dict], Awaitable[dict]]
PiClientFactory = Callable[..., PiRpcClient]


@dataclass(frozen=True)
class PiSessionPreparation:
    """Compatibility shape consumed by Orchestrator.stream_prepared."""

    session_id: str
    restored: bool
    load_failed: bool
    fresh: bool


def _b64decode_json(encoded: str) -> dict:
    if not encoded or len(encoded) > _MAX_ENVELOPE_BYTES * 2:
        raise PiRpcError("Pi policy envelope 长度非法")
    padding = "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        if len(raw) > _MAX_ENVELOPE_BYTES:
            raise PiRpcError("Pi policy envelope 超过大小上限")
        value = json.loads(raw.decode("utf-8"))
    except PiRpcError:
        raise
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise PiRpcError("Pi policy envelope 不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise PiRpcError("Pi policy envelope 顶层必须是对象")
    return value


def _b64encode_json(value: dict) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _validate_permission_outcome(outcome: object, params: dict) -> dict:
    if not isinstance(outcome, dict):
        return {"outcome": "cancelled"}
    if outcome.get("outcome") == "cancelled":
        return {"outcome": "cancelled"}
    if outcome.get("outcome") != "selected":
        return {"outcome": "cancelled"}
    option_id = outcome.get("optionId")
    valid_ids = {
        item.get("optionId")
        for item in params.get("options", [])
        if isinstance(item, dict)
    }
    if isinstance(option_id, str) and option_id and option_id in valid_ids:
        return {"outcome": "selected", "optionId": option_id}
    return {"outcome": "cancelled"}


class PiRpcAdapter:
    """Stateful adapter for ``pi --mode rpc`` with a fail-closed policy bridge."""

    name = "pi"
    stateful_session = True
    # A missing native session must not cause the orchestrator to replay old
    # tool-bearing assignments into a fresh Pi process.
    replay_history_on_fresh_session = False

    def __init__(
        self,
        cmd: Sequence[str] = ("pi",),
        *,
        client_factory: PiClientFactory = PiRpcClient,
        state_root: Path | None = None,
        bridge_path: Path | None = None,
        permission_handler: AgentPermissionHandler | None = None,
        startup_timeout: float = 10.0,
        cancel_timeout: float = 10.0,
        inactivity_timeout: float = DEFAULT_AGENT_INACTIVITY_TIMEOUT,
        tool_inactivity_timeout: float = 900.0,
        request_timeout: float = 30.0,
    ) -> None:
        if not cmd:
            raise ValueError("Pi command 不能为空")
        if min(
            startup_timeout,
            cancel_timeout,
            inactivity_timeout,
            tool_inactivity_timeout,
            request_timeout,
        ) <= 0:
            raise ValueError("Pi timeout 必须大于 0")
        self.session_id: str | None = None
        self._base_cmd = list(cmd)
        self._client_factory = client_factory
        configured_root = (
            state_root
            if state_root is not None
            else Path(os.environ.get(
                _SESSION_ROOT_ENV,
                str(default_state_root() / "pi-rpc"),
            ))
        )
        self._state_root = Path(configured_root).expanduser().resolve()
        self._bridge_path = (
            Path(bridge_path)
            if bridge_path is not None
            else Path(__file__).parent / "extensions" /
            "myagents_permission_bridge.ts"
        ).expanduser().resolve()
        self._permission_handler = permission_handler
        self._attachment_root: Path | None = None
        self._startup_timeout = startup_timeout
        self._cancel_timeout = cancel_timeout
        self._inactivity_timeout = inactivity_timeout
        self._tool_inactivity_timeout = tool_inactivity_timeout
        self._request_timeout = request_timeout
        self._client: PiRpcClient | None = None
        self._started = False
        self._closed = False
        self._active_mode: ExecutionMode | None = None
        self._active_workdir: str | None = None
        self._active_session_file: Path | None = None
        self._active_session_dir: Path | None = None
        self._active_native_session_id: str | None = None
        self._lock = asyncio.Lock()
        self._process_nonce = ""
        self._policy_hash = ""
        self._expected_tools: tuple[str, ...] = ()
        self._pending_mode: ExecutionMode | None = None
        self._pending_workdir: str | None = None
        self._attestation_event: asyncio.Event | None = None
        self._ready_event: asyncio.Event | None = None
        self._attestation_error: str | None = None
        self._policy_generation = 0
        self._active_event_queue: asyncio.Queue[AgentEvent] | None = None
        self._pending_permissions: set[str] = set()
        self._prompt_permission_gate: asyncio.Event | None = None

    @property
    def pid(self) -> int | None:
        return None if self._client is None else self._client.pid

    def set_permission_handler(
        self,
        handler: AgentPermissionHandler | None,
    ) -> None:
        self._permission_handler = handler

    def set_attachment_root(self, root: Path | None) -> None:
        self._attachment_root = None if root is None else Path(root).absolute()

    @staticmethod
    def _tools_for(mode: ExecutionMode) -> tuple[str, ...]:
        if mode is ExecutionMode.READ_ONLY:
            return PI_READ_ONLY_TOOLS
        return PI_ALL_TOOLS

    def _ensure_bridge(self) -> str:
        try:
            info = self._bridge_path.lstat()
            resolved = self._bridge_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PiRpcError(
                f"Pi permission bridge 不存在：{self._bridge_path}"
            ) from exc
        if self._bridge_path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise PiRpcError("Pi permission bridge 必须是非符号链接普通文件")
        if resolved != self._bridge_path:
            raise PiRpcError("Pi permission bridge 路径不稳定")
        return hashlib.sha256(resolved.read_bytes()).hexdigest()

    def _session_dir(
        self,
        workdir: str,
        mode: ExecutionMode,
    ) -> Path:
        workspace_key = hashlib.sha256(
            workdir.encode("utf-8")
        ).hexdigest()[:24]
        target = self._state_root / workspace_key / mode.value
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(target, 0o700)
        return target.resolve()

    def _decode_resume_token(
        self,
        token: str,
        *,
        workdir: str,
        mode: ExecutionMode,
        session_dir: Path,
    ) -> Path | None:
        if not token.startswith(_SESSION_TOKEN_PREFIX):
            raise PiRpcError("Pi session checkpoint 格式不受支持")
        payload = _b64decode_json(token[len(_SESSION_TOKEN_PREFIX):])
        expected_workspace = hashlib.sha256(
            workdir.encode("utf-8")
        ).hexdigest()
        if payload.get("workspace") != expected_workspace:
            raise PiRpcError("Pi session checkpoint 属于其他工作区")
        if payload.get("profile") != mode.value:
            # Execution profiles never reuse one another's process/session.
            return None
        raw_path = payload.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise PiRpcError("Pi session checkpoint 缺少路径")
        native_session_id = payload.get("nativeSessionId")
        if (not isinstance(native_session_id, str)
                or _SESSION_ID_RE.fullmatch(native_session_id) is None):
            raise PiRpcError("Pi session checkpoint 缺少 native session id")
        path = Path(raw_path).expanduser()
        try:
            parent = path.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PiRpcError("Pi session checkpoint 父目录无效") from exc
        if parent != session_dir:
            raise PiRpcError("Pi session checkpoint 文件越界")
        resolved = parent / path.name
        if not resolved.name.endswith(f"_{native_session_id}.jsonl"):
            raise PiRpcError("Pi session checkpoint 文件名与 id 不绑定")
        marker_state = self._read_session_marker(
            self._session_marker_path(
                session_dir, resolved, native_session_id),
            token=token,
            session_file=resolved,
            native_session_id=native_session_id,
            workdir=workdir,
            mode=mode,
        )
        try:
            info = resolved.lstat()
        except FileNotFoundError as exc:
            if marker_state == "reserved":
                # Pi cannot execute a model tool before an assistant message has
                # materialized this file.  The orchestrator also preserves its
                # no-replay cursor, so this exact unfinished reservation may be
                # replaced by a fresh native session without replaying writes.
                return None
            raise PiRpcError(
                "Pi 已持久化 session checkpoint 文件缺失") from exc
        except OSError as exc:
            raise PiRpcError("Pi session checkpoint 文件无法读取") from exc
        if (resolved.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1):
            raise PiRpcError("Pi session checkpoint 必须是单链接普通文件")
        self._validate_session_header(
            resolved,
            workdir,
            expected_session_id=native_session_id,
        )
        if marker_state == "reserved":
            self._write_session_marker(
                self._session_marker_path(
                    session_dir, resolved, native_session_id),
                token=token,
                session_file=resolved,
                native_session_id=native_session_id,
                workdir=workdir,
                mode=mode,
                state="materialized",
                expected_previous="reserved",
            )
        return resolved

    @staticmethod
    def _session_marker_path(
        session_dir: Path,
        session_file: Path,
        native_session_id: str,
    ) -> Path:
        marker_key = hashlib.sha256(
            f"{session_file}\0{native_session_id}".encode("utf-8")
        ).hexdigest()[:32]
        return session_dir / f".myagents-pi-{marker_key}.checkpoint"

    @staticmethod
    def _session_marker_payload(
        *,
        token: str,
        session_file: Path,
        native_session_id: str,
        workdir: str,
        mode: ExecutionMode,
        state: str,
    ) -> dict:
        return {
            "version": _SESSION_MARKER_VERSION,
            "state": state,
            "token": token,
            "path": str(session_file),
            "nativeSessionId": native_session_id,
            "workspace": workdir,
            "profile": mode.value,
        }

    def _read_session_marker(
        self,
        marker: Path,
        *,
        token: str,
        session_file: Path,
        native_session_id: str,
        workdir: str,
        mode: ExecutionMode,
    ) -> str:
        try:
            before = marker.lstat()
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(marker, flags)
            try:
                opened = os.fstat(fd)
                if (before.st_dev != opened.st_dev
                        or before.st_ino != opened.st_ino):
                    raise PiRpcError("Pi session marker 在读取时发生变化")
                chunks = bytearray()
                while len(chunks) <= _MAX_SESSION_MARKER_BYTES:
                    chunk = os.read(
                        fd,
                        min(
                            64 * 1024,
                            _MAX_SESSION_MARKER_BYTES + 1 - len(chunks),
                        ),
                    )
                    if not chunk:
                        break
                    chunks.extend(chunk)
                raw = bytes(chunks)
            finally:
                os.close(fd)
        except PiRpcError:
            raise
        except OSError as exc:
            raise PiRpcError("Pi session reservation marker 缺失或不可读") from exc
        if (marker.is_symlink() or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600):
            raise PiRpcError("Pi session marker 必须是私有单链接普通文件")
        if len(raw) > _MAX_SESSION_MARKER_BYTES or not raw.endswith(b"\n"):
            raise PiRpcError("Pi session marker 过大或不完整")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PiRpcError("Pi session marker 不是合法 JSON") from exc
        if not isinstance(value, dict) or set(value) != {
            "version", "state", "token", "path", "nativeSessionId",
            "workspace", "profile",
        }:
            raise PiRpcError("Pi session marker 字段闭集不匹配")
        state = value.get("state")
        if state not in {"reserved", "materialized"}:
            raise PiRpcError("Pi session marker 状态非法")
        expected = self._session_marker_payload(
            token=token,
            session_file=session_file,
            native_session_id=native_session_id,
            workdir=workdir,
            mode=mode,
            state=state,
        )
        if value != expected:
            raise PiRpcError("Pi session marker 与 checkpoint 不绑定")
        return state

    def _write_session_marker(
        self,
        marker: Path,
        *,
        token: str,
        session_file: Path,
        native_session_id: str,
        workdir: str,
        mode: ExecutionMode,
        state: str,
        expected_previous: str | None,
    ) -> None:
        if expected_previous is None:
            if marker.exists() or marker.is_symlink():
                raise PiRpcError("Pi session reservation marker 已存在")
        else:
            actual = self._read_session_marker(
                marker,
                token=token,
                session_file=session_file,
                native_session_id=native_session_id,
                workdir=workdir,
                mode=mode,
            )
            if actual != expected_previous:
                raise PiRpcError("Pi session marker 状态转换不匹配")
        payload = self._session_marker_payload(
            token=token,
            session_file=session_file,
            native_session_id=native_session_id,
            workdir=workdir,
            mode=mode,
            state=state,
        )
        raw = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8") + b"\n"
        if len(raw) > _MAX_SESSION_MARKER_BYTES:
            raise PiRpcError("Pi session marker 超过大小上限")
        temporary = marker.with_name(
            f".{marker.name}.tmp-{secrets.token_hex(12)}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        try:
            fd = os.open(temporary, flags, 0o600)
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, marker)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
            directory_fd = os.open(marker.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise

    @staticmethod
    def _validate_session_header(
        path: Path,
        workdir: str,
        expected_session_id: str | None = None,
    ) -> None:
        try:
            with path.open("rb") as handle:
                raw = handle.readline(_MAX_SESSION_HEADER_BYTES + 1)
            if len(raw) > _MAX_SESSION_HEADER_BYTES or not raw.endswith(b"\n"):
                raise PiRpcError("Pi session header 过大或不完整")
            header = json.loads(raw.decode("utf-8"))
        except PiRpcError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PiRpcError("Pi session header 损坏") from exc
        if not isinstance(header, dict) or header.get("type") != "session":
            raise PiRpcError("Pi session header 类型非法")
        if (expected_session_id is not None
                and header.get("id") != expected_session_id):
            raise PiRpcError("Pi session header id 与运行时不一致")
        cwd = header.get("cwd")
        if not isinstance(cwd, str):
            raise PiRpcError("Pi session header 缺少 cwd")
        try:
            canonical = str(Path(cwd).expanduser().resolve(strict=True))
        except (OSError, RuntimeError) as exc:
            raise PiRpcError("Pi session header cwd 无效") from exc
        if canonical != workdir:
            raise PiRpcError("Pi session 与当前工作区不一致")

    @staticmethod
    def _session_token(
        session_file: Path,
        *,
        workdir: str,
        mode: ExecutionMode,
        native_session_id: str,
    ) -> str:
        return _SESSION_TOKEN_PREFIX + _b64encode_json({
            "nativeSessionId": native_session_id,
            "path": str(session_file),
            "profile": mode.value,
            "workspace": hashlib.sha256(
                workdir.encode("utf-8")
            ).hexdigest(),
        })

    def _command(
        self,
        *,
        mode: ExecutionMode,
        session_dir: Path,
        resume_file: Path | None,
    ) -> list[str]:
        command = [
            *self._base_cmd,
            "--mode", "rpc",
            "--offline",
            "--no-approve",
            "--no-extensions",
            "--extension", str(self._bridge_path),
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-builtin-tools",
            "--tools", ",".join(self._tools_for(mode)),
            "--session-dir", str(session_dir),
        ]
        if resume_file is not None:
            command.extend(("--session", str(resume_file)))
        return command

    async def _prepare_locked(
        self,
        workdir: str,
        resume_session_id: str | None,
        mode: ExecutionMode,
    ) -> PiSessionPreparation:
        if self._closed:
            raise PiRpcError("Pi adapter 已关闭")
        canonical_workdir = str(Path(workdir).expanduser().resolve(strict=True))
        if not Path(canonical_workdir).is_dir():
            raise PiRpcError("Pi workdir 不是目录")

        if self._started and self._attestation_error is not None:
            await self._reset_locked()
            resume_session_id = None

        if self._started:
            if self._active_mode is mode and self._active_workdir == canonical_workdir:
                assert self.session_id is not None
                if (resume_session_id is not None
                        and resume_session_id != self.session_id):
                    raise PiRpcError("Pi 活跃 session 与 checkpoint 不一致")
                return PiSessionPreparation(
                    self.session_id,
                    restored=resume_session_id is not None,
                    load_failed=False,
                    fresh=False,
                )
            await self._reset_locked()
            if self._closed:
                raise PiRpcError("Pi adapter 已关闭")
            # A profile/workspace boundary always starts a new native session.
            resume_session_id = None

        self._policy_hash = self._ensure_bridge()
        self._process_nonce = secrets.token_urlsafe(24)
        self._expected_tools = self._tools_for(mode)
        self._pending_mode = mode
        self._pending_workdir = canonical_workdir
        self._attestation_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._attestation_error = None
        self._policy_generation += 1
        policy_generation = self._policy_generation
        session_dir = self._session_dir(canonical_workdir, mode)
        resume_file = None
        if resume_session_id is not None:
            resume_file = self._decode_resume_token(
                resume_session_id,
                workdir=canonical_workdir,
                mode=mode,
                session_dir=session_dir,
            )
        command = self._command(
            mode=mode,
            session_dir=session_dir,
            resume_file=resume_file,
        )
        env = {
            "PI_OFFLINE": "1",
            _POLICY_NONCE_ENV: self._process_nonce,
            _POLICY_HASH_ENV: self._policy_hash,
            _POLICY_PATH_ENV: str(self._bridge_path),
            _PROFILE_ENV: mode.value,
            _WORKSPACE_ENV: canonical_workdir,
        }
        async def handle_extension_ui(request: dict) -> dict | None:
            return await self._handle_extension_ui(
                request,
                policy_generation,
            )

        client = self._client_factory(
            command,
            cwd=canonical_workdir,
            env_overrides=env,
            extension_ui_handler=handle_extension_ui,
            request_timeout=self._request_timeout,
        )
        self._client = client
        try:
            await client.start()
            assert self._attestation_event is not None
            assert self._ready_event is not None
            await asyncio.wait_for(
                self._attestation_event.wait(),
                timeout=self._startup_timeout,
            )
            if self._attestation_error is not None:
                raise PiRpcError(
                    f"Pi bridge attestation 失败：{self._attestation_error}")
            await asyncio.wait_for(
                self._ready_event.wait(),
                timeout=self._startup_timeout,
            )
            if self._attestation_error is not None:
                raise PiRpcError(
                    f"Pi bridge attestation 失败：{self._attestation_error}")
            commands = await asyncio.wait_for(
                client.get_commands(),
                timeout=self._startup_timeout,
            )
            self._validate_policy_command(commands)
            state = await asyncio.wait_for(
                client.get_state(),
                timeout=self._startup_timeout,
            )
            session_file, native_session_id = (
                self._validated_state_session_file(
                    state,
                    session_dir,
                    canonical_workdir,
                    allow_missing=resume_file is None,
                )
            )
        except BaseException as exc:
            await self._reset_locked()
            if isinstance(exc, (asyncio.CancelledError, PiRpcError)):
                raise
            if isinstance(exc, TimeoutError):
                raise PiRpcError("Pi permission bridge 握手超时") from exc
            raise PiRpcError(f"Pi RPC 启动失败：{exc}") from exc

        token = self._session_token(
            session_file,
            workdir=canonical_workdir,
            mode=mode,
            native_session_id=native_session_id,
        )
        if resume_file is None:
            try:
                self._write_session_marker(
                    self._session_marker_path(
                        session_dir, session_file, native_session_id),
                    token=token,
                    session_file=session_file,
                    native_session_id=native_session_id,
                    workdir=canonical_workdir,
                    mode=mode,
                    state=(
                        "materialized" if session_file.exists() else "reserved"
                    ),
                    expected_previous=None,
                )
            except BaseException:
                await self._reset_locked()
                raise
        self.session_id = token
        self._active_mode = mode
        self._active_workdir = canonical_workdir
        self._active_session_file = session_file
        self._active_session_dir = session_dir
        self._active_native_session_id = native_session_id
        self._started = True
        return PiSessionPreparation(
            token,
            restored=resume_file is not None,
            load_failed=False,
            fresh=True,
        )

    def _validate_policy_command(self, commands: object) -> None:
        if not isinstance(commands, list):
            raise PiRpcError("Pi get_commands 返回值非法")
        matches = [
            item for item in commands
            if isinstance(item, dict) and item.get("name") == PI_POLICY_COMMAND
        ]
        if len(matches) != 1:
            raise PiRpcError("Pi policy command 缺失或重复")
        item = matches[0]
        source = item.get("sourceInfo")
        if not isinstance(source, dict):
            raise PiRpcError("Pi policy command 缺少 sourceInfo")
        try:
            path = Path(str(source.get("path", ""))).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PiRpcError("Pi policy command 来源路径无效") from exc
        if path != self._bridge_path:
            raise PiRpcError("Pi policy command 不是由固定 bridge 注册")

    def _validated_state_session_file(
        self,
        state: object,
        session_dir: Path,
        workdir: str,
        *,
        allow_missing: bool = False,
    ) -> tuple[Path, str]:
        if not isinstance(state, dict):
            raise PiRpcError("Pi get_state 返回值非法")
        session_id = state.get("sessionId")
        if (not isinstance(session_id, str)
                or _SESSION_ID_RE.fullmatch(session_id) is None):
            raise PiRpcError("Pi get_state 未返回合法 sessionId")
        raw_path = state.get("sessionFile")
        if not isinstance(raw_path, str) or not raw_path:
            raise PiRpcError("Pi get_state 未返回持久 sessionFile")
        path = Path(raw_path)
        if not path.is_absolute():
            raise PiRpcError("Pi sessionFile 必须是绝对路径")
        try:
            parent = path.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PiRpcError("Pi sessionFile 父目录无效") from exc
        if parent != session_dir:
            raise PiRpcError("Pi sessionFile 越出受管目录")
        canonical = parent / path.name
        if not canonical.name.endswith(f"_{session_id}.jsonl"):
            raise PiRpcError("Pi sessionFile 名称与 sessionId 不绑定")
        try:
            info = canonical.lstat()
        except FileNotFoundError:
            if allow_missing:
                # Pi 0.84 deliberately defers writing a new session file until
                # an assistant message exists.  The reserved path is safe to
                # checkpoint only after exact parent/name/id confinement; a
                # successful turn must materialize and validate it below.
                return canonical, session_id
            raise PiRpcError("Pi sessionFile 尚未持久化")
        except OSError as exc:
            raise PiRpcError("Pi sessionFile 无法读取") from exc
        if (canonical.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1):
            raise PiRpcError("Pi sessionFile 必须是单链接普通文件")
        try:
            resolved = canonical.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PiRpcError("Pi sessionFile 解析失败") from exc
        if resolved.parent != session_dir:
            raise PiRpcError("Pi sessionFile 解析后越出受管目录")
        self._validate_session_header(
            resolved, workdir, expected_session_id=session_id)
        return resolved, session_id

    def _validate_active_session_checkpoint(self) -> None:
        path = self._active_session_file
        session_dir = self._active_session_dir
        workdir = self._active_workdir
        session_id = self._active_native_session_id
        if (path is None or session_dir is None or workdir is None
                or session_id is None):
            raise PiRpcError("Pi active session checkpoint 缺失")
        validated, actual_session_id = self._validated_state_session_file(
            {"sessionFile": str(path), "sessionId": session_id},
            session_dir,
            workdir,
            allow_missing=False,
        )
        if validated != path or actual_session_id != session_id:
            raise PiRpcError("Pi active session checkpoint 发生变化")
        token = self.session_id
        mode = self._active_mode
        if token is None or mode is None:
            raise PiRpcError("Pi active session marker 绑定缺失")
        marker = self._session_marker_path(session_dir, path, session_id)
        marker_state = self._read_session_marker(
            marker,
            token=token,
            session_file=path,
            native_session_id=session_id,
            workdir=workdir,
            mode=mode,
        )
        if marker_state == "reserved":
            self._write_session_marker(
                marker,
                token=token,
                session_file=path,
                native_session_id=session_id,
                workdir=workdir,
                mode=mode,
                state="materialized",
                expected_previous="reserved",
            )

    async def _handle_extension_ui(
        self,
        request: dict,
        policy_generation: int,
    ) -> dict | None:
        method = request.get("method")
        if policy_generation != self._policy_generation:
            if method in {"select", "confirm", "input", "editor"}:
                return {
                    "type": "extension_ui_response",
                    "id": request.get("id"),
                    "cancelled": True,
                }
            return None
        if method == "setStatus":
            self._handle_ready_status(request)
            if self._attestation_error is not None:
                await self._reclaim_policy_generation(policy_generation)
            return None
        if method != "select":
            # Unknown interactive UI cannot broaden capability.
            if method in {"confirm", "input", "editor"}:
                return {
                    "type": "extension_ui_response",
                    "id": request.get("id"),
                    "cancelled": True,
                }
            return None
        title = request.get("title")
        if isinstance(title, str) and title.startswith(PI_ATTEST_PREFIX):
            response = self._handle_attestation(request, title)
            if self._attestation_error is not None:
                await self._reclaim_policy_generation(policy_generation)
            return response
        if isinstance(title, str) and title.startswith(PI_PERMISSION_PREFIX):
            return await self._handle_permission(request, title)
        return {
            "type": "extension_ui_response",
            "id": request.get("id"),
            "cancelled": True,
        }

    async def _reclaim_policy_generation(
        self,
        policy_generation: int,
    ) -> None:
        """Poison and reclaim only the process that failed attestation."""
        if policy_generation != self._policy_generation:
            return
        client = self._client
        if client is not None:
            await client.close()

    def _handle_attestation(self, request: dict, title: str) -> dict:
        request_id = request.get("id")
        try:
            if self._attestation_event is None or self._attestation_event.is_set():
                raise PiRpcError("重复或非预期 attestation")
            if not isinstance(request_id, str) or not request_id:
                raise PiRpcError("attestation request id 非法")
            payload = _b64decode_json(title[len(PI_ATTEST_PREFIX):])
            if set(payload) != {
                "version", "nonce", "policyHash", "profile", "workspace",
                "activeTools", "tools",
            }:
                raise PiRpcError("attestation 字段闭集不匹配")
            nonce = self._process_nonce
            expected_options = [f"ack:{nonce}", f"deny:{nonce}"]
            if request.get("options") != expected_options:
                raise PiRpcError("attestation options 不匹配")
            expected_scalars = {
                "version": PI_POLICY_VERSION,
                "nonce": nonce,
                "policyHash": self._policy_hash,
                "profile": (
                    self._pending_mode.value
                    if self._pending_mode is not None else None
                ),
                "workspace": self._pending_workdir,
            }
            for key, expected in expected_scalars.items():
                if payload.get(key) != expected:
                    raise PiRpcError(f"attestation {key} 不匹配")
            workspace = payload.get("workspace")
            if not isinstance(workspace, str):
                raise PiRpcError("attestation workspace 缺失")
            canonical_workspace = str(Path(workspace).resolve(strict=True))
            client_cwd = str(Path(
                self._client.cwd if self._client is not None else workspace
            ).resolve(strict=True))
            if canonical_workspace != client_cwd:
                raise PiRpcError("attestation workspace 不匹配")
            active_tools = payload.get("activeTools")
            if active_tools != list(self._expected_tools):
                raise PiRpcError("attestation activeTools 不是固定闭集")
            tools = payload.get("tools")
            if not isinstance(tools, list) or len(tools) != len(self._expected_tools):
                raise PiRpcError("attestation tool sources 数量不匹配")
            by_name = {}
            for item in tools:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise PiRpcError("attestation tool source 结构非法")
                if item["name"] in by_name:
                    raise PiRpcError("attestation tool source 重复")
                by_name[item["name"]] = item
            if set(by_name) != set(self._expected_tools):
                raise PiRpcError("attestation tool source 闭集不匹配")
            for name in self._expected_tools:
                source = by_name[name].get("sourceInfo")
                if not isinstance(source, dict):
                    raise PiRpcError("attestation tool 缺少 sourceInfo")
                source_path = Path(str(source.get("path", ""))).resolve(strict=True)
                if source_path != self._bridge_path:
                    raise PiRpcError("attestation tool 来源不是固定 bridge")
        except Exception as exc:
            self._attestation_error = str(exc)
            if self._attestation_event is not None:
                self._attestation_event.set()
            return {
                "type": "extension_ui_response",
                "id": request_id,
                "cancelled": True,
            }
        self._attestation_event.set()
        return {
            "type": "extension_ui_response",
            "id": request_id,
            "value": f"ack:{self._process_nonce}",
        }

    def _handle_ready_status(self, request: dict) -> None:
        if request.get("statusKey") != PI_READY_STATUS_KEY:
            return
        expected = f"ready:{self._process_nonce}:{self._policy_hash}"
        if request.get("statusText") != expected:
            self._attestation_error = "ready status 不匹配"
        if self._ready_event is not None:
            self._ready_event.set()

    async def _handle_permission(self, request: dict, title: str) -> dict:
        request_id = request.get("id")
        cancelled = {
            "type": "extension_ui_response",
            "id": request_id,
            "cancelled": True,
        }
        try:
            if not isinstance(request_id, str) or not request_id:
                raise PiRpcError("permission request id 非法")
            payload = _b64decode_json(title[len(PI_PERMISSION_PREFIX):])
            if set(payload) != {
                "version", "processNonce", "callNonce", "toolCallId",
                "toolName", "argsHash", "input",
            }:
                raise PiRpcError("permission 字段闭集不匹配")
            if payload.get("version") != PI_POLICY_VERSION:
                raise PiRpcError("permission version 不匹配")
            if payload.get("processNonce") != self._process_nonce:
                raise PiRpcError("permission process nonce 不匹配")
            call_nonce = payload.get("callNonce")
            tool_call_id = payload.get("toolCallId")
            tool_name = payload.get("toolName")
            args_hash = payload.get("argsHash")
            raw_input = payload.get("input")
            if not isinstance(call_nonce, str) or not call_nonce:
                raise PiRpcError("permission call nonce 非法")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise PiRpcError("permission toolCallId 非法")
            if tool_name not in self._expected_tools:
                raise PiRpcError("permission toolName 不在当前 profile 闭集")
            if not isinstance(args_hash, str) or _SHA256_RE.fullmatch(args_hash) is None:
                raise PiRpcError("permission argsHash 非法")
            if not isinstance(raw_input, dict):
                raise PiRpcError("permission input 非法")
            if len(json.dumps(raw_input, ensure_ascii=False)) > 8 * 1024:
                raise PiRpcError("permission input 超过显示上限")
            allow_id = f"allow_once:{call_nonce}"
            reject_id = f"reject_once:{call_nonce}"
            if request.get("options") != [allow_id, reject_id]:
                raise PiRpcError("permission options 不匹配")
        except Exception:
            return cancelled

        permission_gate = self._prompt_permission_gate
        permission_process_nonce = self._process_nonce
        if permission_gate is None:
            return cancelled
        try:
            await asyncio.wait_for(
                permission_gate.wait(),
                timeout=self._request_timeout,
            )
        except (TimeoutError, asyncio.CancelledError):
            return cancelled
        if (self._prompt_permission_gate is not permission_gate
                or self._process_nonce != permission_process_nonce
                or payload.get("processNonce") != permission_process_nonce):
            return cancelled

        display_name = str(tool_name).removeprefix("myagents_")
        params = {
            "toolCall": {
                "toolCallId": tool_call_id,
                "title": display_name,
                "rawInput": raw_input,
            },
            "options": [
                {
                    "optionId": allow_id,
                    "name": "允许一次",
                    "kind": "allow_once",
                },
                {
                    "optionId": reject_id,
                    "name": "拒绝",
                    "kind": "reject_once",
                },
            ],
            "_myagents_mirrors_permission_events": True,
        }
        self._pending_permissions.add(request_id)
        if not self._queue_permission_event(
            f"等待权限：{display_name}",
            tool_call_id,
        ):
            self._pending_permissions.discard(request_id)
            return cancelled
        try:
            if self._permission_handler is None:
                outcome = {"outcome": "cancelled"}
            else:
                outcome = self._permission_handler(self.name, params)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
            selected = _validate_permission_outcome(outcome, params)
        except BaseException:
            selected = {"outcome": "cancelled"}
        finally:
            self._pending_permissions.discard(request_id)

        if selected.get("outcome") == "selected":
            option_id = selected["optionId"]
            if option_id == allow_id:
                queued = self._queue_permission_event(
                    f"权限已允许一次：{display_name}", tool_call_id)
            else:
                queued = self._queue_permission_event(
                    f"权限已拒绝：{display_name}", tool_call_id)
            if not queued:
                return cancelled
            return {
                "type": "extension_ui_response",
                "id": request_id,
                "value": option_id,
            }
        self._queue_permission_event(
            f"权限已取消或拒绝：{display_name}", tool_call_id)
        return cancelled

    def _queue_permission_event(self, text: str, tool_call_id: str) -> bool:
        queue = self._active_event_queue
        if queue is None:
            return False
        try:
            queue.put_nowait(AgentEvent(
                "permission",
                text,
                meta={"tool_call_id": tool_call_id},
            ))
        except asyncio.QueueFull:
            return False
        return True

    def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        return self.stream_prepared(
            lambda _prep: prompt,
            workdir,
            execution_mode=execution_mode,
        )

    async def stream_prepared(
        self,
        make_prompt: Callable[[PiSessionPreparation], str],
        workdir: str,
        resume_session_id: str | None = None,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        async with self._lock:
            prep = await self._prepare_locked(
                workdir,
                resume_session_id,
                execution_mode,
            )
            try:
                prompt = make_prompt(prep)
            except BaseException:
                if prep.fresh:
                    await self._reset_locked()
                raise
            session_info = None
            if prep.fresh:
                verb = "已恢复" if prep.restored else "已建立"
                session_info = AgentEvent(
                    "info",
                    f"Pi RPC session {verb}",
                    meta={"sessionId": prep.session_id, "transport": "rpc"},
                )
            inner = self._prompt_locked(prompt, prep.session_id)
            try:
                async for event in inner:
                    yield event
                    if event.kind == "delivery_committed":
                        permission_gate = self._prompt_permission_gate
                        if permission_gate is not None:
                            # Resuming after this internal event means the
                            # Orchestrator has durably advanced its no-replay
                            # cursor.  Only now may a waiting tool approval be
                            # shown or granted.
                            permission_gate.set()
                        if session_info is not None:
                            # A new Pi session path is only a reservation
                            # before the first prompt.  Do not expose fallible
                            # UI work in the checkpoint-to-submit window.
                            yield session_info
                            session_info = None
            finally:
                with contextlib.suppress(RuntimeError):
                    await inner.aclose()

    async def _prompt_locked(
        self,
        prompt: str,
        session_id: str,
    ) -> AsyncIterator[AgentEvent]:
        if self._client is None:
            raise PiRpcError("Pi RPC client 尚未启动")
        if self._attestation_error is not None:
            detail = self._attestation_error
            await self._reset_locked()
            raise PiRpcError(
                f"Pi bridge attestation 已失效：{detail}")
        images = prompt_images(
            prompt,
            self._attachment_root,
            max_total_bytes=MAX_CLIPBOARD_IMAGE_BYTES,
            max_images=_MAX_PROMPT_IMAGES,
        )
        if self._prompt_permission_gate is not None:
            raise PiRpcError("Pi permission checkpoint gate 已被占用")
        permission_gate = asyncio.Event()
        self._prompt_permission_gate = permission_gate
        permission_events: asyncio.Queue[AgentEvent] = asyncio.Queue(
            maxsize=_PERMISSION_EVENT_QUEUE_LIMIT,
        )
        self._active_event_queue = permission_events
        raw_stream = self._client.prompt(prompt, images)
        raw_task = asyncio.create_task(anext(raw_stream))
        permission_task = asyncio.create_task(permission_events.get())
        committed = False
        delivery_maybe_sent = False
        settled = False
        buffered: list[AgentEvent] = []
        seen_thinking = False
        active_tool_ids: set[str] = set()
        last_assistant_end: tuple[str, str | None] | None = None
        terminal_tool_failure: str | None = None
        try:
            while not settled:
                timeout = None
                if not self._pending_permissions:
                    timeout = (
                        self._tool_inactivity_timeout
                        if active_tool_ids
                        else self._inactivity_timeout
                    )
                done, _pending = await asyncio.wait(
                    {raw_task, permission_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    assert timeout is not None
                    phase = "活跃工具" if active_tool_ids else "RPC"
                    raise AgentDeliveryUncertainError(
                        f"Pi {phase} 连续 {timeout:g} 秒无活动，"
                        "本轮结果不确定"
                    )
                if permission_task in done:
                    permission_event = permission_task.result()
                    permission_task = asyncio.create_task(
                        permission_events.get())
                    if committed:
                        yield permission_event
                    else:
                        buffered.append(permission_event)
                    if raw_task not in done:
                        continue
                try:
                    payload = raw_task.result()
                except StopAsyncIteration as exc:
                    raise AgentDeliveryUncertainError(
                        "Pi RPC 在 agent_settled 前结束") from exc
                raw_task = asyncio.create_task(anext(raw_stream))
                if not isinstance(payload, dict):
                    raise PiRpcError("Pi RPC event 不是对象")
                raw_type = payload.get("type")
                if raw_type == "delivery_committed":
                    if committed:
                        raise PiRpcError("Pi RPC 重复 delivery_committed")
                    committed = True
                    yield AgentEvent("delivery_committed", meta={
                        "sessionId": session_id,
                        "transport": "rpc",
                    })
                    for event in buffered:
                        yield event
                    buffered.clear()
                    continue
                if not committed:
                    # Client promises to buffer vendor events until prompt
                    # acceptance.  Reject a broken client rather than expose a
                    # pre-checkpoint side effect to the outer event sink.
                    raise PiRpcError(
                        "Pi RPC 在 delivery_committed 前泄露了事件")
                if raw_type == "message_update":
                    delta = payload.get("assistantMessageEvent")
                    if not isinstance(delta, dict):
                        continue
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        text = delta.get("delta")
                        if isinstance(text, str) and text:
                            yield AgentEvent("text", text)
                    elif (isinstance(delta_type, str)
                          and delta_type.startswith("thinking")
                          and not seen_thinking):
                        seen_thinking = True
                        yield AgentEvent("status", "Pi 正在分析…")
                elif raw_type == "tool_execution_start":
                    tool_call_id = payload.get("toolCallId")
                    if isinstance(tool_call_id, str) and tool_call_id:
                        active_tool_ids.add(tool_call_id)
                    event = self._tool_event(payload, "in_progress")
                    if event is not None:
                        yield event
                elif raw_type == "tool_execution_update":
                    tool_call_id = payload.get("toolCallId")
                    if isinstance(tool_call_id, str) and tool_call_id:
                        active_tool_ids.add(tool_call_id)
                    event = self._tool_event(payload, "in_progress")
                    if event is not None:
                        yield event
                elif raw_type == "tool_execution_end":
                    tool_call_id = payload.get("toolCallId")
                    if isinstance(tool_call_id, str):
                        active_tool_ids.discard(tool_call_id)
                    status = "failed" if payload.get("isError") else "completed"
                    result = payload.get("result")
                    if (payload.get("isError") is True
                            and isinstance(result, dict)
                            and result.get("terminate") is True):
                        tool_name = payload.get("toolName")
                        terminal_tool_failure = (
                            tool_name.removeprefix("myagents_")
                            if isinstance(tool_name, str)
                            else "unknown"
                        )
                    event = self._tool_event(payload, status)
                    if event is not None:
                        yield event
                elif raw_type == "message_end":
                    message = payload.get("message")
                    if (isinstance(message, dict)
                            and message.get("role") == "assistant"):
                        # A later assistant response proves that a prior
                        # recoverable/mixed tool batch continued normally.
                        terminal_tool_failure = None
                        stop_reason = message.get("stopReason")
                        error_message = message.get("errorMessage")
                        if not isinstance(stop_reason, str):
                            stop_reason = "invalid"
                        if not isinstance(error_message, str):
                            error_message = None
                        last_assistant_end = (stop_reason, error_message)
                    yield AgentEvent("activity", meta={"phase": raw_type})
                elif raw_type in {
                    "agent_start", "turn_start", "turn_end", "message_start",
                    "agent_end", "queue_update",
                }:
                    yield AgentEvent("activity", meta={"phase": raw_type})
                elif raw_type in {
                    "auto_retry_start", "compaction_start",
                    "summarization_retry_attempt_start",
                }:
                    yield AgentEvent(
                        "status",
                        "Pi 正在重试或整理上下文…",
                        meta={"phase": raw_type},
                    )
                elif raw_type == "extension_error":
                    detail = redact_sensitive_text(
                        str(payload.get("error") or "Pi extension error"),
                        limit=2000,
                    )
                    raise AgentDeliveryUncertainError(detail)
                elif raw_type == "agent_settled":
                    self._validate_active_session_checkpoint()
                    if last_assistant_end is None:
                        raise AgentDeliveryUncertainError(
                            "Pi 请求已提交但缺少 assistant message_end 终态")
                    stop_reason, error_message = last_assistant_end
                    if stop_reason in _PI_TERMINAL_FAILURE_STOP_REASONS:
                        detail = error_message or (
                            "响应被中止"
                            if stop_reason == "aborted"
                            else "模型执行失败"
                        )
                        raise AgentDeliveryUncertainError(
                            "Pi 请求已提交但执行失败："
                            + redact_sensitive_text(detail, limit=2000)
                        )
                    if stop_reason not in _PI_TERMINAL_SUCCESS_STOP_REASONS:
                        raise AgentDeliveryUncertainError(
                            "Pi 请求已提交但 assistant stopReason 非法："
                            + redact_sensitive_text(
                                stop_reason,
                                limit=200,
                            )
                        )
                    if terminal_tool_failure is not None:
                        raise AgentDeliveryUncertainError(
                            "Pi 请求已提交但工具被权限策略终止："
                            + redact_sensitive_text(
                                terminal_tool_failure,
                                limit=200,
                            )
                        )
                    settled = True
                    yield AgentEvent("done", meta={
                        "sessionId": session_id,
                        "piPid": self.pid,
                    })
                else:
                    yield AgentEvent("activity", meta={"phase": raw_type})
        except AgentDeliveryCancelledError:
            delivery_maybe_sent = True
            raise
        except asyncio.CancelledError as exc:
            delivery_maybe_sent = True
            # Once the native prompt iterator is running, cancellation can race with stdin
            # drain before the synthetic delivery_committed event reaches
            # this adapter.  Treat the boundary conservatively: a cancelled
            # user task must never cause an automatic replay of a Pi turn that
            # may already be executing.
            raise AgentDeliveryCancelledError(
                "Pi prompt 发送后被取消，禁止自动重投"
            ) from exc
        except AgentDeliveryUncertainError:
            delivery_maybe_sent = True
            raise
        except BaseException as exc:
            if committed:
                raise AgentDeliveryUncertainError(
                    f"Pi prompt 已提交但未成功完成：{exc}"
                ) from exc
            raise
        finally:
            self._active_event_queue = None
            if self._prompt_permission_gate is permission_gate:
                self._prompt_permission_gate = None
            for task in (raw_task, permission_task):
                if not task.done():
                    task.cancel()
            for task in (raw_task, permission_task):
                with contextlib.suppress(BaseException):
                    await task
            with contextlib.suppress(BaseException):
                await raw_stream.aclose()
            if (committed or delivery_maybe_sent) and not settled:
                # A prompt without a trustworthy terminal boundary must never
                # share a process with the next turn.  Client-side generator
                # cancellation already attempts ``abort``; process rebuild is
                # the final single-writer/no-overlap guarantee.
                await self._reset_locked()

    @staticmethod
    def _tool_event(raw: dict, status: str) -> AgentEvent | None:
        tool_name = raw.get("toolName")
        if not isinstance(tool_name, str):
            return None
        title = tool_name.removeprefix("myagents_")
        args = raw.get("args")
        command = ""
        if isinstance(args, dict) and isinstance(args.get("command"), str):
            command = redact_sensitive_text(args["command"])
        return AgentEvent(
            "tool",
            title,
            meta={
                "tool_call_id": raw.get("toolCallId"),
                "status": status,
                "command": command,
            },
        )

    async def _reset_locked(self) -> None:
        # Invalidate callbacks before awaiting process teardown so a late task
        # from the old client cannot poison or authorize the next generation.
        self._policy_generation += 1
        client = self._client
        self._client = None
        permission_gate = self._prompt_permission_gate
        self._prompt_permission_gate = None
        if permission_gate is not None:
            permission_gate.set()
        if client is not None:
            with contextlib.suppress(BaseException):
                await client.close()
        self._started = False
        self.session_id = None
        self._active_mode = None
        self._active_workdir = None
        self._active_session_file = None
        self._active_session_dir = None
        self._active_native_session_id = None
        self._active_event_queue = None
        self._pending_mode = None
        self._pending_workdir = None
        self._attestation_event = None
        self._ready_event = None
        self._attestation_error = None
        self._process_nonce = ""
        self._policy_hash = ""
        self._expected_tools = ()
        self._pending_permissions.clear()

    async def aclose(self) -> None:
        self._closed = True
        client = self._client
        if client is not None:
            with contextlib.suppress(BaseException):
                await client.close()
        acquired = False
        try:
            await asyncio.wait_for(
                self._lock.acquire(), timeout=min(self._cancel_timeout, 1.0))
            acquired = True
            await self._reset_locked()
        except TimeoutError:
            pass
        finally:
            if acquired:
                self._lock.release()

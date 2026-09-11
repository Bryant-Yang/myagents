"""Strict async client for Claude Code's headless stream-json mode.

Claude Code does not speak ACP.  The official programmatic surface is
``claude -p --input-format stream-json --output-format stream-json``: a
long-lived process that accepts one user NDJSON frame per turn on stdin and
emits event frames plus exactly one ``result`` frame per turn on stdout.
This client keeps that vendor protocol behind a deliberately small public
surface.  It never sends undocumented control frames over stdin — the only
cancellation primitives are the documented POSIX signals (SIGINT ends the
current turn, SIGTERM terminates).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator, Mapping
from typing import Any

from adapters.base import (
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    BoundedLog,
    redact_sensitive_text,
)
from clipboard_image import MAX_CLIPBOARD_IMAGE_BYTES, TrustedImage


# Claude replays the full user message back on stdout, including base64
# image data.  One allowed 20 MiB PNG expands to about 26.7 MiB, so the
# transport needs a larger but still finite frame ceiling.
_DEFAULT_FRAME_LIMIT = 32 * 1024 * 1024
_MAX_PROMPT_IMAGE_BYTES = MAX_CLIPBOARD_IMAGE_BYTES
_MAX_PROMPT_IMAGES = 16
_EVENT_QUEUE_LIMIT = 4096
_EVENT_QUEUE_BYTE_LIMIT = 64 * 1024 * 1024
_READ_CHUNK = 64 * 1024


class ClaudeStreamError(RuntimeError):
    """Claude Code process, transport, or acceptance failure."""


class ClaudeStreamProtocolError(ClaudeStreamError):
    """Claude Code emitted a malformed or out-of-contract NDJSON frame."""


class ClaudeStreamDisconnected(ClaudeStreamError):
    """The owned Claude Code process disconnected."""


class ClaudeResumeNotFoundError(ClaudeStreamDisconnected):
    """Claude Code rejected ``--resume`` because the session is gone.

    This is the one precisely documented resume failure
    (``No conversation found with session ID: <id>``); the adapter is
    allowed to fall back to a fresh session on exactly this error.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            "Claude resume 会话不存在："
            + redact_sensitive_text(detail, limit=2000)
        )


class _ClaudeWriteError(ClaudeStreamError):
    def __init__(self, message: str, *, may_have_written: bool) -> None:
        super().__init__(message)
        self.may_have_written = may_have_written


class ClaudeStreamClient:
    """One headless Claude Code subprocess and its single-writer turn stream.

    ``cmd`` must already contain the complete, policy-constrained invocation.
    The client executes it verbatim and does not discover tools, sessions,
    settings, or MCP servers itself.
    """

    def __init__(
        self,
        cmd: list[str],
        cwd: str,
        env_overrides: Mapping[str, str] | None = None,
        *,
        acceptance_timeout: float = 120.0,
        frame_limit: int = _DEFAULT_FRAME_LIMIT,
        event_queue_byte_limit: int = _EVENT_QUEUE_BYTE_LIMIT,
        interrupt_timeout: float = 10.0,
        shutdown_timeout: float = 5.0,
    ) -> None:
        # Empty-string argv parts are legitimate: `--setting-sources ""`
        # disables every settings source on the CLI.
        if not cmd or not all(isinstance(part, str) for part in cmd):
            raise ValueError("Claude command must be a non-empty string list")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("Claude cwd must be non-empty")
        if (
            acceptance_timeout <= 0
            or interrupt_timeout <= 0
            or shutdown_timeout <= 0
        ):
            raise ValueError("Claude timeouts must be positive")
        if not isinstance(frame_limit, int) or frame_limit < 128:
            raise ValueError("Claude frame_limit must be at least 128 bytes")
        if (
            not isinstance(event_queue_byte_limit, int)
            or isinstance(event_queue_byte_limit, bool)
            or not 128 <= event_queue_byte_limit <= _EVENT_QUEUE_BYTE_LIMIT
        ):
            raise ValueError(
                "Claude event_queue_byte_limit must be between 128 bytes "
                f"and {_EVENT_QUEUE_BYTE_LIMIT} bytes")

        self.command = list(cmd)
        self.cwd = cwd
        self._env_overrides = dict(env_overrides or {})
        self._acceptance_timeout = acceptance_timeout
        self._frame_limit = frame_limit
        self._event_queue_byte_limit = event_queue_byte_limit
        self._interrupt_timeout = interrupt_timeout
        self._shutdown_timeout = shutdown_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._stderr = BoundedLog()
        self._disconnect_error: BaseException | None = None
        self._init_message: dict | None = None
        self._init_event: asyncio.Event | None = None
        self._active_turn_events: asyncio.Queue[
            tuple[dict | BaseException, int]] | None = None
        self._active_turn_event_bytes = 0
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def running(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._disconnect_error is None
            and not self._closed
        )

    async def start(self) -> None:
        """Spawn the configured command in its own process group.

        Claude 的 stream-json 输入模式是输入驱动的：收到第一条 stdin 消息
        之前进程完全静默（2026-09-10 真实探针证实），``system/init`` 与
        replay 回执都在首个 turn 内到达。因此本方法只负责 spawn 与读循环，
        启动失败（非法 flag、resume 未命中等）在首个 turn 的回执等待中
        以 :class:`ClaudeResumeNotFoundError` 或断连错误浮出。
        """
        if self.running:
            raise ClaudeStreamError("Claude client is already running")
        if self._proc is not None:
            raise ClaudeStreamError("stale Claude connection; close it first")
        if self._closed:
            raise ClaudeStreamError("Claude client is closed")

        process_env = os.environ.copy()
        process_env.update(self._env_overrides)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.cwd,
                env=process_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ClaudeStreamError(f"Claude 进程无法启动：{exc}") from exc
        if self._closed:
            await self.close()
            raise ClaudeStreamError("Claude client is closed")
        self._init_event = asyncio.Event()
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="claude-stream-reader")
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="claude-stream-stderr")
        # There is no pre-input handshake.  Yield once so an immediate exec
        # error is observed by the reader before the first public turn.
        await asyncio.sleep(0)
        if self._disconnect_error is not None:
            error = self._disconnect_error
            await self.close()
            raise error

    @property
    def init_message(self) -> dict | None:
        """The ``system/init`` frame, once the first turn has been submitted."""
        return self._init_message

    async def send_turn(
        self,
        message: str,
        images: tuple[TrustedImage, ...] = (),
    ) -> AsyncIterator[dict]:
        """Submit one turn and stream raw frames through the ``result`` frame.

        The documented ``--replay-user-messages`` echo is the authoritative
        acceptance signal: the synthetic ``delivery_committed`` event is
        yielded only after it, always before any other raw frame.
        """
        if not isinstance(message, str):
            raise TypeError("Claude turn message must be a string")
        if not isinstance(images, tuple) or not all(
                isinstance(image, TrustedImage) for image in images):
            raise TypeError(
                "Claude turn images must be a TrustedImage tuple")
        if len(images) > _MAX_PROMPT_IMAGES:
            raise ClaudeStreamError(
                f"Claude turn accepts at most {_MAX_PROMPT_IMAGES} images")
        image_bytes = sum(len(image.data) for image in images)
        if image_bytes > _MAX_PROMPT_IMAGE_BYTES:
            raise ClaudeStreamError(
                "Claude turn image payload exceeds the 20 MiB aggregate limit")
        if self._active_turn_events is not None:
            raise ClaudeStreamError("Claude stream allows only one active turn")

        event_queue: asyncio.Queue[
            tuple[dict | BaseException, int]] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_LIMIT)
        self._active_turn_events = event_queue
        self._active_turn_event_bytes = 0
        sent = False
        accepted = False
        result_seen = False
        try:
            content: str | list[dict[str, Any]] = message
            if images:
                content = [{
                    "type": "text",
                    "text": message,
                }, *({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(image.data).decode("ascii"),
                    },
                } for image in images)]
            frame: dict[str, Any] = {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
            }
            try:
                await self._send(frame)
                sent = True
            except _ClaudeWriteError as exc:
                if exc.may_have_written:
                    raise AgentDeliveryUncertainError(
                        f"Claude turn may have been written: {exc}") from exc
                # Pre-write failure: the frame never left, but a client whose
                # stdin is gone is dead weight; close so the adapter's reuse
                # check rebuilds on the next turn.
                await self.close()
                raise ClaudeStreamError(str(exc)) from exc

            # Claude 只在第一条 stdin 输入之后才输出 system/init，其后是
            # replay 回执。等待二者到齐（后续 turn 只需回执）。
            echo: dict | None = None
            try:
                while echo is None or self._init_message is None:
                    frame = await asyncio.wait_for(
                        self._next_turn_event(event_queue),
                        timeout=self._acceptance_timeout,
                    )
                    if isinstance(frame, BaseException):
                        raise frame
                    frame_type = frame.get("type")
                    if frame_type == "system":
                        # 回执前的 system 帧（init/status/hook_*）是启动
                        # 杂音；init 记录下来供 adapter 校验，其余丢弃。
                        if (frame.get("subtype") == "init"
                                and self._init_message is None):
                            self._init_message = frame
                            if self._init_event is not None:
                                self._init_event.set()
                        continue
                    if frame_type == "user" and echo is None:
                        if frame.get("parent_tool_use_id") is not None:
                            raise ClaudeStreamProtocolError(
                                "Claude 的首条回执挂在了子代理消息下")
                        echo = frame
                        continue
                    if echo is None:
                        raise ClaudeStreamProtocolError(
                            "Claude 的首条回执不是 replay 的 user 消息")
                    # echo 已到而 init 未到：把帧塞回不可行，但 init 实测
                    # 先于回执；此处只有畸形流会走到。
                    raise ClaudeStreamProtocolError(
                        "Claude 在 system/init 之前先发出了其他事件")
            except ClaudeResumeNotFoundError:
                # CLI 在读取 stdin 之前因 --resume 失败退出；本轮必然未执行。
                raise
            except ClaudeStreamProtocolError:
                # 流已确定损坏；交由上层判定，不伪装成 uncertain。
                raise
            except ClaudeStreamDisconnected as exc:
                if self._init_message is None:
                    # init 都未输出：CLI 死于处理输入之前，本轮不可能执行
                    # 过任何工具。如实报告启动失败，不稀释 uncertain 语义。
                    raise ClaudeStreamError(
                        f"Claude 启动失败（本轮未执行）：{exc}") from exc
                raise AgentDeliveryUncertainError(
                    f"Claude turn was sent but acceptance is uncertain: {exc}"
                ) from exc
            except asyncio.CancelledError:
                await self._interrupt_after_cancel(event_queue)
                raise AgentDeliveryCancelledError(
                    "Claude turn was cancelled after send")
            except TimeoutError as exc:
                raise AgentDeliveryUncertainError(
                    "Claude turn was sent but the delivery echo never "
                    "arrived; the outcome is uncertain") from exc
            except BaseException as exc:
                raise AgentDeliveryUncertainError(
                    f"Claude turn was sent but acceptance is uncertain: {exc}"
                ) from exc

            accepted = True
            yield {"type": "delivery_committed", "session_id":
                   echo.get("session_id")}

            result_seen = False
            while True:
                try:
                    event = await self._next_turn_event(event_queue)
                except asyncio.CancelledError:
                    await self._interrupt_after_cancel(event_queue)
                    raise AgentDeliveryCancelledError(
                        "accepted Claude turn was cancelled")
                if isinstance(event, BaseException):
                    raise AgentDeliveryUncertainError(
                        f"accepted Claude turn ended before result: {event}"
                    ) from event
                if event.get("type") == "result":
                    result_seen = True
                yield event
                if result_seen:
                    return
        except GeneratorExit:
            # A turn still in flight may already sit in Claude's stdin
            # buffer or be executing: close so it cannot race the next one.
            # A finished turn (result already yielded) leaves the healthy
            # process alive for reuse.
            if sent and not result_seen:
                await self.close()
            raise
        finally:
            if self._active_turn_events is event_queue:
                self._active_turn_events = None
                self._active_turn_event_bytes = 0

    async def close(self) -> None:
        """Idempotently cancel owned tasks and terminate the full process group."""
        self._closed = True
        proc = self._proc
        if proc is None:
            return

        self._fail_connection(ClaudeStreamDisconnected("Claude client closed"))
        current = asyncio.current_task()
        tasks = (self._reader_task, self._stderr_task)
        for task in tasks:
            if task is not None and task is not current:
                task.cancel()
        await self._terminate_process(proc)
        for task in tasks:
            if task is not None and task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        # Give the subprocess transports one loop iteration to deliver
        # connection_lost before the caller's event loop may close.
        await asyncio.sleep(0)

        self._proc = None
        self._reader_task = None
        self._stderr_task = None

    aclose = close

    async def _send(self, value: dict) -> None:
        proc = self._proc
        if (
            proc is None
            or proc.stdin is None
            or proc.returncode is not None
            or self._disconnect_error is not None
        ):
            raise _ClaudeWriteError(
                "Claude stdin is unavailable", may_have_written=False)
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8") + b"\n"
        if len(payload) - 1 > self._frame_limit:
            raise _ClaudeWriteError(
                f"Claude outbound frame exceeds {self._frame_limit} bytes",
                may_have_written=False,
            )
        async with self._write_lock:
            wrote = False
            try:
                proc.stdin.write(payload)
                wrote = True
                await proc.stdin.drain()
            except asyncio.CancelledError as exc:
                raise _ClaudeWriteError(
                    "Claude write was cancelled",
                    may_have_written=wrote,
                ) from exc
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                error = ClaudeStreamDisconnected(
                    f"failed to write Claude stdin: {exc}")
                self._fail_connection(error)
                raise _ClaudeWriteError(
                    str(error), may_have_written=wrote) from exc

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        error: BaseException | None = None
        try:
            async for frame in self._read_frames(self._proc.stdout):
                self._handle_frame(frame)
            returncode = await self._proc.wait()
            if self._init_message is None:
                # stderr 由独立 task 排空；精确的 resume-miss 句子可能
                # 晚于 stdout EOF 落账，给一次有界缓冲再分类。
                await asyncio.sleep(0.1)
            stderr = redact_sensitive_text(
                self._stderr.render().strip(), limit=4096)
            if self._init_message is None:
                if "No conversation found with session ID:" in stderr:
                    error = ClaudeResumeNotFoundError(stderr)
                else:
                    suffix = f": {stderr}" if stderr else ""
                    error = ClaudeStreamDisconnected(
                        f"Claude process exited with {returncode} "
                        f"before system/init{suffix}")
            else:
                suffix = f": {stderr}" if stderr else ""
                error = ClaudeStreamDisconnected(
                    f"Claude process exited with {returncode}{suffix}")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error = exc
        finally:
            if error is not None:
                self._fail_connection(error)
                assert self._proc is not None
                await self._terminate_process(self._proc)

    async def _read_frames(
        self,
        stream: asyncio.StreamReader,
    ) -> AsyncIterator[dict]:
        buffer = bytearray()
        while True:
            remaining = self._frame_limit + 1 - len(buffer)
            if remaining <= 0:
                raise ClaudeStreamProtocolError(
                    f"Claude frame exceeds {self._frame_limit} bytes")
            chunk = await stream.read(min(_READ_CHUNK, remaining))
            if not chunk:
                if buffer:
                    raise ClaudeStreamProtocolError(
                        "Claude stream ended with an unterminated frame")
                return
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                raw = bytes(buffer[:newline])
                del buffer[:newline + 1]
                if len(raw) > self._frame_limit:
                    raise ClaudeStreamProtocolError(
                        f"Claude frame exceeds {self._frame_limit} bytes")
                if not raw:
                    raise ClaudeStreamProtocolError(
                        "Claude emitted an empty frame")
                try:
                    decoded = raw.decode("utf-8", errors="strict")
                    value = json.loads(decoded)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    preview = redact_sensitive_text(
                        raw.decode("utf-8", errors="replace"), limit=160)
                    raise ClaudeStreamProtocolError(
                        f"Claude emitted invalid JSON: {preview}") from exc
                if not isinstance(value, dict):
                    raise ClaudeStreamProtocolError(
                        "Claude frame is not an object")
                yield value
            if len(buffer) > self._frame_limit:
                raise ClaudeStreamProtocolError(
                    f"Claude frame exceeds {self._frame_limit} bytes")

    def _handle_frame(self, frame: dict) -> None:
        frame_type = frame.get("type")
        if (
            frame_type == "system"
            and frame.get("subtype") == "init"
            and self._init_message is None
        ):
            self._init_message = frame
            if self._init_event is not None:
                self._init_event.set()
            return
        queue = self._active_turn_events
        if queue is None:
            # Claude must stay quiet between turns.  Idle chatter cannot be
            # attached to a future turn because that would cross delivery
            # boundaries.
            return
        self._queue_turn_event(queue, frame)

    def _queue_turn_event(
        self,
        queue: asyncio.Queue[tuple[dict | BaseException, int]],
        frame: dict,
    ) -> None:
        encoded_bytes = len(json.dumps(
            frame,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"))
        if (
            encoded_bytes > self._event_queue_byte_limit
            or self._active_turn_event_bytes
            > self._event_queue_byte_limit - encoded_bytes
        ):
            raise ClaudeStreamProtocolError(
                "Claude turn event buffer exceeds "
                f"{self._event_queue_byte_limit}-byte budget")
        try:
            queue.put_nowait((frame, encoded_bytes))
        except asyncio.QueueFull as exc:
            raise ClaudeStreamProtocolError(
                "Claude turn event buffer exceeds "
                f"{_EVENT_QUEUE_LIMIT} frames") from exc
        self._active_turn_event_bytes += encoded_bytes

    async def _next_turn_event(
        self,
        queue: asyncio.Queue[tuple[dict | BaseException, int]],
    ) -> dict | BaseException:
        event, encoded_bytes = await queue.get()
        if self._active_turn_events is queue:
            self._active_turn_event_bytes = max(
                0,
                self._active_turn_event_bytes - encoded_bytes,
            )
        return event

    async def _interrupt_after_cancel(
        self,
        event_queue: asyncio.Queue[tuple[dict | BaseException, int]],
    ) -> None:
        """Best-effort documented SIGINT interrupt, then fail closed.

        SIGINT ends the current turn while the process stays alive.  If no
        ``result`` frame confirms the interrupt within the bounded window,
        the whole process group is reclaimed: an unconfirmed interrupt makes
        this connection unusable for the next turn.
        """
        try:
            proc = self._proc
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(
                        ProcessLookupError, PermissionError, OSError):
                    # Only the CLI process, never the process group: the
                    # permission MCP server shares the group.
                    os.kill(proc.pid, signal.SIGINT)
            deadline = asyncio.get_running_loop().time() + (
                self._interrupt_timeout)
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                event = await asyncio.wait_for(
                    self._next_turn_event(event_queue), timeout=remaining)
                if isinstance(event, BaseException):
                    raise event
                if event.get("type") == "result":
                    return
        except BaseException:
            await self.close()
        finally:
            # The turn is over either way; a stale queue must not leak into
            # the next turn.
            if self._active_turn_events is event_queue:
                self._active_turn_events = None
                self._active_turn_event_bytes = 0

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while chunk := await self._proc.stderr.read(8192):
            self._stderr.append(chunk.decode("utf-8", errors="replace"))

    def _fail_connection(self, error: BaseException) -> None:
        if self._disconnect_error is None:
            self._disconnect_error = error
        terminal_error = self._disconnect_error
        assert terminal_error is not None
        queue = self._active_turn_events
        if queue is not None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._active_turn_event_bytes = 0
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait((terminal_error, 0))
        # A reader that dies before system/init must also release the
        # pending start() waiter; the failure surfaces through
        # _disconnect_error instead of the startup timeout.
        if self._init_event is not None and not self._init_event.is_set():
            self._init_event.set()

    async def _terminate_process(
        self,
        proc: asyncio.subprocess.Process,
    ) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(
                proc.wait(), timeout=self._shutdown_timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()

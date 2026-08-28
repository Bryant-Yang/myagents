"""Strict async client for Pi's native LF-delimited RPC mode.

Pi RPC is not ACP and not JSON-RPC.  A long-lived Pi process accepts command
objects on stdin and writes response/event objects to stdout.  This client
keeps that vendor protocol behind a deliberately small public surface: it does
not expose Pi's raw RPC ``bash`` command or any generic request primitive.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

from adapters.base import (
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    BoundedLog,
    redact_sensitive_text,
)
from clipboard_image import MAX_CLIPBOARD_IMAGE_BYTES, TrustedImage


ExtensionUiHandler = Callable[[dict], Awaitable[dict | None] | dict | None]

# Pi echoes the full user message in message_start/message_end, including
# base64 image data.  One allowed 20 MiB PNG expands to about 26.7 MiB, so the
# transport needs a larger but still finite frame ceiling.
_DEFAULT_FRAME_LIMIT = 32 * 1024 * 1024
_MAX_PROMPT_IMAGE_BYTES = MAX_CLIPBOARD_IMAGE_BYTES
_MAX_PROMPT_IMAGES = 16
_EVENT_QUEUE_LIMIT = 4096
_EVENT_QUEUE_BYTE_LIMIT = 64 * 1024 * 1024
_EXTENSION_UI_TASK_LIMIT = 32
_READ_CHUNK = 64 * 1024
_INTERACTIVE_EXTENSION_METHODS = {"select", "confirm", "input", "editor"}


class PiRpcError(RuntimeError):
    """Pi process, transport, or response failure."""


class PiRpcProtocolError(PiRpcError):
    """Pi emitted a malformed or out-of-contract LF JSONL frame."""


class PiRpcDisconnected(PiRpcError):
    """The owned Pi RPC process disconnected."""


class PiRpcRemoteError(PiRpcError):
    """Pi explicitly rejected a command."""

    def __init__(self, command: str, detail: object) -> None:
        self.command = command
        self.detail = str(detail)
        super().__init__(
            f"Pi RPC {command} rejected: "
            f"{redact_sensitive_text(self.detail, limit=2000)}")


class _PiRpcWriteError(PiRpcError):
    def __init__(self, message: str, *, may_have_written: bool) -> None:
        super().__init__(message)
        self.may_have_written = may_have_written


class PiRpcClient:
    """One Pi RPC subprocess and its single-writer command stream.

    ``cmd`` must already contain the complete, policy-constrained invocation
    (normally ``pi --mode rpc ...``).  The client executes it verbatim and does
    not discover extensions, tools, sessions, or profiles itself.
    """

    def __init__(
        self,
        cmd: list[str],
        cwd: str,
        env_overrides: Mapping[str, str] | None = None,
        extension_ui_handler: ExtensionUiHandler | None = None,
        *,
        request_timeout: float = 30.0,
        frame_limit: int = _DEFAULT_FRAME_LIMIT,
        event_queue_byte_limit: int = _EVENT_QUEUE_BYTE_LIMIT,
        abort_timeout: float = 5.0,
        extension_ui_timeout: float = 300.0,
        shutdown_timeout: float = 5.0,
    ) -> None:
        if not cmd or not all(isinstance(part, str) and part for part in cmd):
            raise ValueError("Pi RPC command must be a non-empty string list")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("Pi RPC cwd must be non-empty")
        if (
            request_timeout <= 0
            or abort_timeout <= 0
            or extension_ui_timeout <= 0
            or shutdown_timeout <= 0
        ):
            raise ValueError("Pi RPC timeouts must be positive")
        if not isinstance(frame_limit, int) or frame_limit < 128:
            raise ValueError("Pi RPC frame_limit must be at least 128 bytes")
        if (
            not isinstance(event_queue_byte_limit, int)
            or isinstance(event_queue_byte_limit, bool)
            or not 128 <= event_queue_byte_limit <= _EVENT_QUEUE_BYTE_LIMIT
        ):
            raise ValueError(
                "Pi RPC event_queue_byte_limit must be between 128 bytes "
                f"and {_EVENT_QUEUE_BYTE_LIMIT} bytes")

        self.command = list(cmd)
        self.cwd = cwd
        self._env_overrides = dict(env_overrides or {})
        self._extension_ui_handler = extension_ui_handler
        self._request_timeout = request_timeout
        self._frame_limit = frame_limit
        self._event_queue_byte_limit = event_queue_byte_limit
        self._abort_timeout = abort_timeout
        self._extension_ui_timeout = extension_ui_timeout
        self._shutdown_timeout = shutdown_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._extension_tasks: dict[asyncio.Task, str] = {}
        self._pending: dict[str, tuple[str, asyncio.Future[dict]]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._stderr = BoundedLog()
        self._disconnect_error: BaseException | None = None
        self._active_prompt_events: asyncio.Queue[
            tuple[dict | BaseException, int]] | None = None
        self._active_prompt_event_bytes = 0
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
        """Spawn the exact configured command in its own process group."""
        if self.running:
            raise PiRpcError("Pi RPC client is already running")
        if self._proc is not None:
            raise PiRpcError("stale Pi RPC connection; close it first")
        if self._closed:
            raise PiRpcError("Pi RPC client is closed")

        process_env = os.environ.copy()
        process_env.update(self._env_overrides)
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=process_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if self._closed:
            await self.close()
            raise PiRpcError("Pi RPC client is closed")
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="pi-rpc-reader")
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="pi-rpc-stderr")
        # There is no Pi RPC handshake.  Yield once so an immediate exec error
        # is observed by the reader before the first public request.
        await asyncio.sleep(0)
        if self._disconnect_error is not None:
            error = self._disconnect_error
            await self.close()
            raise PiRpcDisconnected(str(error)) from error

    async def get_state(self) -> dict:
        response = await self._request("get_state")
        data = response.get("data")
        if not isinstance(data, dict):
            raise PiRpcProtocolError("get_state response is missing object data")
        return data

    async def get_commands(self) -> list[dict]:
        response = await self._request("get_commands")
        data = response.get("data")
        commands = data.get("commands") if isinstance(data, dict) else None
        if not isinstance(commands, list) or not all(
                isinstance(item, dict) for item in commands):
            raise PiRpcProtocolError(
                "get_commands response is missing commands array")
        return commands

    async def prompt(
        self,
        message: str,
        images: tuple[TrustedImage, ...] = (),
    ) -> AsyncIterator[dict]:
        """Submit one prompt and stream raw Pi events through ``agent_settled``.

        Events that race ahead of the authoritative prompt response are held in
        a bounded queue.  Once Pi explicitly accepts the prompt, the synthetic
        ``delivery_committed`` event is always yielded before any raw event.
        """
        if not isinstance(message, str):
            raise TypeError("Pi RPC prompt message must be a string")
        if not isinstance(images, tuple) or not all(
                isinstance(image, TrustedImage) for image in images):
            raise TypeError(
                "Pi RPC prompt images must be a TrustedImage tuple")
        if len(images) > _MAX_PROMPT_IMAGES:
            raise PiRpcError(
                f"Pi RPC prompt accepts at most {_MAX_PROMPT_IMAGES} images")
        image_bytes = sum(len(image.data) for image in images)
        if image_bytes > _MAX_PROMPT_IMAGE_BYTES:
            raise PiRpcError(
                "Pi RPC prompt image payload exceeds the 20 MiB aggregate limit")
        if self._active_prompt_events is not None:
            raise PiRpcError("Pi RPC allows only one active prompt")

        event_queue: asyncio.Queue[
            tuple[dict | BaseException, int]] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_LIMIT)
        self._active_prompt_events = event_queue
        self._active_prompt_event_bytes = 0
        request_id: str | None = None
        response_future: asyncio.Future[dict] | None = None
        sent = False
        accepted = False
        try:
            payload: dict[str, Any] = {
                "message": message,
            }
            if images:
                payload["images"] = [{
                    "type": "image",
                    "mimeType": "image/png",
                    "data": base64.b64encode(image.data).decode("ascii"),
                } for image in images]
            try:
                request_id, response_future = await self._begin_request(
                    "prompt", payload)
                sent = True
            except _PiRpcWriteError as exc:
                if exc.may_have_written:
                    raise AgentDeliveryUncertainError(
                        f"Pi RPC prompt may have been written: {exc}") from exc
                raise PiRpcError(str(exc)) from exc

            try:
                await self._await_response(
                    request_id, response_future, "prompt")
            except PiRpcRemoteError:
                raise
            except asyncio.CancelledError:
                # The frame was drained, so even without an acceptance response
                # it is unsafe to replay.  Best-effort abort the live process.
                await self._abort_after_cancel(event_queue)
                raise AgentDeliveryCancelledError(
                    "Pi RPC prompt was cancelled after send")
            except BaseException as exc:
                raise AgentDeliveryUncertainError(
                    f"Pi RPC prompt was sent but acceptance is uncertain: {exc}"
                ) from exc

            accepted = True
            yield {"type": "delivery_committed"}

            while True:
                try:
                    event = await self._next_prompt_event(event_queue)
                except asyncio.CancelledError:
                    await self._abort_after_cancel(event_queue)
                    raise AgentDeliveryCancelledError(
                        "accepted Pi RPC prompt was cancelled")
                if isinstance(event, BaseException):
                    raise AgentDeliveryUncertainError(
                        f"accepted Pi RPC prompt ended before agent_settled: "
                        f"{event}") from event
                yield event
                if event.get("type") == "agent_settled":
                    return
        except GeneratorExit:
            if accepted:
                await self._abort_after_cancel(event_queue)
            raise
        finally:
            if request_id is not None:
                self._discard_pending(request_id, response_future)
            if self._active_prompt_events is event_queue:
                self._active_prompt_events = None
                self._active_prompt_event_bytes = 0

    async def abort(self) -> None:
        await self._request("abort")

    async def steer(self, message: str) -> None:
        """Queue one native Pi steering message with strict no-replay semantics."""
        if not isinstance(message, str) or not message.strip():
            raise TypeError("Pi RPC steer message must be a non-empty string")
        request_id: str | None = None
        response_future: asyncio.Future[dict] | None = None
        try:
            try:
                request_id, response_future = await self._begin_request(
                    "steer", {"message": message.strip()})
            except _PiRpcWriteError as exc:
                if exc.may_have_written:
                    raise AgentDeliveryUncertainError(
                        f"Pi RPC steer may have been written: {exc}"
                    ) from exc
                raise PiRpcError(str(exc)) from exc
            try:
                await self._await_response(
                    request_id, response_future, "steer")
            except PiRpcRemoteError:
                raise
            except BaseException as exc:
                raise AgentDeliveryUncertainError(
                    f"Pi RPC steer was sent but acceptance is uncertain: {exc}"
                ) from exc
        finally:
            if request_id is not None:
                self._discard_pending(request_id, response_future)

    async def extension_ui_response(self, response: dict) -> None:
        """Send one validated extension UI response, never an arbitrary RPC command."""
        normalized = self._validated_extension_response(response)
        await self._send(normalized)

    async def close(self) -> None:
        """Idempotently cancel owned tasks and terminate the full process group."""
        self._closed = True
        proc = self._proc
        if proc is None:
            return

        self._fail_connection(PiRpcDisconnected("Pi RPC client closed"))
        current = asyncio.current_task()
        tasks = (
            self._reader_task,
            self._stderr_task,
            *tuple(self._extension_tasks),
        )
        for task in tasks:
            if task is not None and task is not current:
                task.cancel()
        await self._terminate_process(proc)
        for task in tasks:
            if task is not None and task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        self._proc = None
        self._reader_task = None
        self._stderr_task = None
        self._extension_tasks.clear()

    aclose = close

    async def _request(self, command: str, params: dict | None = None) -> dict:
        request_id, future = await self._begin_request(command, params or {})
        return await self._await_response(request_id, future, command)

    async def _begin_request(
        self,
        command: str,
        params: dict,
    ) -> tuple[str, asyncio.Future[dict]]:
        if not self.running:
            if self._disconnect_error is not None:
                raise PiRpcDisconnected(str(self._disconnect_error)) from (
                    self._disconnect_error)
            raise PiRpcError("Pi RPC client is not running")
        self._next_id += 1
        request_id = f"req_{self._next_id}"
        future: asyncio.Future[dict] = (
            asyncio.get_running_loop().create_future())
        self._pending[request_id] = (command, future)
        try:
            await self._send({"id": request_id, "type": command, **params})
        except BaseException:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise
        return request_id, future

    async def _await_response(
        self,
        request_id: str,
        future: asyncio.Future[dict],
        command: str,
    ) -> dict:
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=self._request_timeout)
        except TimeoutError as exc:
            raise PiRpcError(
                f"timed out waiting for Pi RPC {command} response") from exc
        finally:
            self._discard_pending(request_id, future)

    def _discard_pending(
        self,
        request_id: str,
        future: asyncio.Future[dict] | None,
    ) -> None:
        self._pending.pop(request_id, None)
        if future is None:
            return
        if not future.done():
            future.cancel()
        elif not future.cancelled():
            # Consume a private late failure so shutdown never emits
            # "Future exception was never retrieved".
            future.exception()

    async def _send(self, value: dict) -> None:
        proc = self._proc
        if (
            proc is None
            or proc.stdin is None
            or proc.returncode is not None
            or self._disconnect_error is not None
        ):
            raise _PiRpcWriteError(
                "Pi RPC stdin is unavailable", may_have_written=False)
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8") + b"\n"
        if len(payload) - 1 > self._frame_limit:
            raise _PiRpcWriteError(
                f"Pi RPC outbound frame exceeds {self._frame_limit} bytes",
                may_have_written=False,
            )
        async with self._write_lock:
            wrote = False
            try:
                proc.stdin.write(payload)
                wrote = True
                await proc.stdin.drain()
            except asyncio.CancelledError as exc:
                raise _PiRpcWriteError(
                    "Pi RPC write was cancelled",
                    may_have_written=wrote,
                ) from exc
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                error = PiRpcDisconnected(f"failed to write Pi RPC stdin: {exc}")
                self._fail_connection(error)
                raise _PiRpcWriteError(
                    str(error), may_have_written=wrote) from exc

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        error: BaseException | None = None
        try:
            async for frame in self._read_frames(self._proc.stdout):
                self._handle_frame(frame)
            returncode = await self._proc.wait()
            stderr = redact_sensitive_text(
                self._stderr.render().strip(), limit=4096)
            suffix = f": {stderr}" if stderr else ""
            error = PiRpcDisconnected(
                f"Pi RPC process exited with {returncode}{suffix}")
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
                raise PiRpcProtocolError(
                    f"Pi RPC frame exceeds {self._frame_limit} bytes")
            chunk = await stream.read(min(_READ_CHUNK, remaining))
            if not chunk:
                if buffer:
                    raise PiRpcProtocolError(
                        "Pi RPC stream ended with an unterminated frame")
                return
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                raw = bytes(buffer[:newline])
                del buffer[:newline + 1]
                if len(raw) > self._frame_limit:
                    raise PiRpcProtocolError(
                        f"Pi RPC frame exceeds {self._frame_limit} bytes")
                if not raw:
                    raise PiRpcProtocolError("Pi RPC emitted an empty frame")
                try:
                    decoded = raw.decode("utf-8", errors="strict")
                    value = json.loads(decoded)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    preview = redact_sensitive_text(
                        raw.decode("utf-8", errors="replace"), limit=160)
                    raise PiRpcProtocolError(
                        f"Pi RPC emitted invalid JSON: {preview}") from exc
                if not isinstance(value, dict):
                    raise PiRpcProtocolError("Pi RPC frame is not an object")
                yield value
            if len(buffer) > self._frame_limit:
                raise PiRpcProtocolError(
                    f"Pi RPC frame exceeds {self._frame_limit} bytes")

    def _handle_frame(self, frame: dict) -> None:
        frame_type = frame.get("type")
        if frame_type == "response":
            self._handle_response(frame)
            return
        if frame_type == "extension_ui_request":
            self._start_extension_handler(frame)
            return
        if not isinstance(frame_type, str) or not frame_type:
            raise PiRpcProtocolError("Pi RPC event is missing type")
        queue = self._active_prompt_events
        if queue is None:
            # Pi may emit lifecycle chatter while idle.  It cannot be attached
            # to a future prompt because that would cross delivery boundaries.
            return
        self._queue_prompt_event(queue, frame)

    def _queue_prompt_event(
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
            or self._active_prompt_event_bytes
            > self._event_queue_byte_limit - encoded_bytes
        ):
            raise PiRpcProtocolError(
                "Pi RPC prompt event buffer exceeds "
                f"{self._event_queue_byte_limit}-byte budget")
        try:
            queue.put_nowait((frame, encoded_bytes))
        except asyncio.QueueFull as exc:
            raise PiRpcProtocolError(
                f"Pi RPC prompt event buffer exceeds {_EVENT_QUEUE_LIMIT} frames"
            ) from exc
        self._active_prompt_event_bytes += encoded_bytes

    async def _next_prompt_event(
        self,
        queue: asyncio.Queue[tuple[dict | BaseException, int]],
    ) -> dict | BaseException:
        event, encoded_bytes = await queue.get()
        if self._active_prompt_events is queue:
            self._active_prompt_event_bytes = max(
                0,
                self._active_prompt_event_bytes - encoded_bytes,
            )
        return event

    def _handle_response(self, frame: dict) -> None:
        request_id = frame.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise PiRpcProtocolError("Pi RPC response is missing a string id")
        pending = self._pending.pop(request_id, None)
        if pending is None:
            # A response can arrive after a bounded caller timeout.  Request
            # ids never repeat, so ignoring it cannot satisfy another command.
            return
        expected_command, future = pending
        if future.done():
            return
        command = frame.get("command")
        if command != expected_command:
            error = PiRpcProtocolError(
                f"Pi RPC response command mismatch: expected "
                f"{expected_command}, got {command}")
            future.set_exception(error)
            raise error
        success = frame.get("success")
        if success is False:
            future.set_exception(PiRpcRemoteError(
                expected_command, frame.get("error", "unknown error")))
        elif success is True:
            future.set_result(frame)
        else:
            error = PiRpcProtocolError(
                "Pi RPC response is missing boolean success")
            future.set_exception(error)
            raise error

    def _start_extension_handler(self, request: dict) -> None:
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(request_id, str) or not request_id:
            raise PiRpcProtocolError(
                "extension_ui_request is missing a string id")
        if not isinstance(method, str) or not method:
            raise PiRpcProtocolError(
                "extension_ui_request is missing method")
        if request_id in self._extension_tasks.values():
            raise PiRpcProtocolError(
                f"duplicate extension UI request id: {request_id}")
        if len(self._extension_tasks) >= _EXTENSION_UI_TASK_LIMIT:
            raise PiRpcProtocolError(
                "Pi RPC exceeds the concurrent extension UI request limit")
        task = asyncio.create_task(
            self._handle_extension_request(dict(request)),
            name=f"pi-extension-ui-{request_id}",
        )
        self._extension_tasks[task] = request_id
        task.add_done_callback(self._extension_task_done)

    async def _handle_extension_request(self, request: dict) -> None:
        method = request["method"]
        interactive = method in _INTERACTIVE_EXTENSION_METHODS
        response: dict | None = None
        try:
            handler = self._extension_ui_handler
            if handler is not None:
                outcome = handler(request)
                if inspect.isawaitable(outcome):
                    timeout = self._extension_timeout(request)
                    outcome = await asyncio.wait_for(outcome, timeout=timeout)
                if outcome is not None:
                    try:
                        response = self._validated_extension_response(outcome)
                    except (TypeError, ValueError):
                        response = None
                    if response is not None and response["id"] != request["id"]:
                        response = None
            if response is None and interactive:
                response = {
                    "type": "extension_ui_response",
                    "id": request["id"],
                    "cancelled": True,
                }
            if response is not None:
                await self.extension_ui_response(response)
        except asyncio.CancelledError:
            if interactive:
                with contextlib.suppress(Exception):
                    await self.extension_ui_response({
                        "type": "extension_ui_response",
                        "id": request["id"],
                        "cancelled": True,
                    })
            raise
        except BaseException:
            if interactive:
                with contextlib.suppress(Exception):
                    await self.extension_ui_response({
                        "type": "extension_ui_response",
                        "id": request["id"],
                        "cancelled": True,
                    })

    def _extension_timeout(self, request: dict) -> float:
        advertised = request.get("timeout")
        if isinstance(advertised, (int, float)) and advertised > 0:
            return min(self._extension_ui_timeout, advertised / 1000)
        return self._extension_ui_timeout

    @staticmethod
    def _validated_extension_response(response: object) -> dict:
        if not isinstance(response, dict):
            raise TypeError("extension UI response must be an object")
        if response.get("type") != "extension_ui_response":
            raise ValueError("invalid extension UI response type")
        request_id = response.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("extension UI response requires a string id")
        keys = set(response)
        if (
            keys == {"type", "id", "value"}
            and isinstance(response.get("value"), str)
        ):
            return dict(response)
        if (
            keys == {"type", "id", "confirmed"}
            and isinstance(response.get("confirmed"), bool)
        ):
            return dict(response)
        if (
            keys == {"type", "id", "cancelled"}
            and response.get("cancelled") is True
        ):
            return dict(response)
        raise ValueError("malformed extension UI response")

    def _extension_task_done(self, task: asyncio.Task) -> None:
        self._extension_tasks.pop(task, None)
        if not task.cancelled():
            task.exception()

    async def _abort_after_cancel(
        self,
        event_queue: asyncio.Queue[tuple[dict | BaseException, int]],
    ) -> None:
        abort_task = asyncio.create_task(
            self.abort(), name="pi-rpc-abort-after-cancel")
        try:
            await asyncio.wait_for(
                asyncio.shield(abort_task), timeout=self._abort_timeout)
            deadline = asyncio.get_running_loop().time() + self._abort_timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                event = await asyncio.wait_for(
                    self._next_prompt_event(event_queue), timeout=remaining)
                if isinstance(event, BaseException):
                    raise event
                if event.get("type") == "agent_settled":
                    return
        except BaseException:
            # Cancellation recovery is fail-closed: an unconfirmed abort makes
            # this connection unusable, so reclaim the full process group.
            await self.close()
        finally:
            # wait_for shields the request so a timeout cannot interrupt a
            # partially written abort frame. Once the process is reclaimed,
            # explicitly consume that task as well; otherwise close() may set
            # its pending response to an exception that is reported later as
            # an orphan task failure.
            if not abort_task.done():
                abort_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await abort_task

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while chunk := await self._proc.stderr.read(8192):
            self._stderr.append(chunk.decode("utf-8", errors="replace"))

    def _fail_connection(self, error: BaseException) -> None:
        if self._disconnect_error is None:
            self._disconnect_error = error
        terminal_error = self._disconnect_error
        assert terminal_error is not None
        for _command, future in self._pending.values():
            if not future.done():
                future.set_exception(terminal_error)
        self._pending.clear()
        queue = self._active_prompt_events
        if queue is not None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._active_prompt_event_bytes = 0
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait((terminal_error, 0))

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

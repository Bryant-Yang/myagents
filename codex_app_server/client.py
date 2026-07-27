"""Minimal async client for ``codex app-server`` over stdio JSONL.

The protocol uses JSON-RPC-like request/response shapes but intentionally
omits the ``jsonrpc`` header.  One client owns one subprocess and is the only
writer to its stdin.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import signal
from collections.abc import Awaitable, Callable
from typing import Any

from adapters.base import BoundedLog, read_lines, redact_sensitive_text

PermissionHandler = Callable[[dict], Awaitable[dict] | dict]
_FORWARDED_NOTIFICATIONS = {
    "item/agentMessage/delta",
    "item/started",
    "item/completed",
    "error",
    "turn/completed",
}
_EVENT_QUEUE_LIMIT = 4096


class CodexAppServerError(RuntimeError):
    """Protocol, process, or remote request failure."""


class CodexAppServerDisconnected(CodexAppServerError):
    """The app-server connection ended while work was pending."""


class CodexAppServerRemoteError(CodexAppServerError):
    """The server explicitly rejected a request before accepting its work."""


class CodexAppServerRequestUncertain(CodexAppServerError):
    """A sent request has no trustworthy response and may have been accepted."""


class CodexAppServerRequestCancelled(asyncio.CancelledError):
    """The caller cancelled after a request may already have been written."""


class CodexAppServerClient:
    """One long-lived Codex app-server process.

    Public methods are intentionally close to the official method names while
    still returning convenient ids.  ``next_event`` exposes server
    notifications to the adapter without leaking private queues.
    """

    def __init__(
        self,
        cmd: list[str] | None = None,
        *,
        command: list[str] | None = None,
        permission_handler: PermissionHandler | None = None,
        request_timeout: float = 30.0,
        shutdown_timeout: float = 5.0,
    ) -> None:
        if cmd is not None and command is not None:
            raise ValueError("cmd 和 command 只能指定一个")
        if request_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        self.command = list(command or cmd or ["codex", "app-server"])
        self._permission_handler = permission_handler
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._reverse_tasks: set[asyncio.Task] = set()
        self._pending_approvals = 0
        self._stderr = BoundedLog()
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue[dict | BaseException] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_LIMIT)
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._closed = False
        self._disconnect_error: BaseException | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def running(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._disconnect_error is None
        )

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        self._permission_handler = handler

    @property
    def pending_approvals(self) -> int:
        return self._pending_approvals

    async def start(self, workdir: str) -> None:
        """Spawn and complete initialize/initialized exactly once."""
        if self.running:
            return
        if self._proc is not None:
            raise CodexAppServerError("旧 app-server 连接已失效；请先 close")
        if self._closed:
            raise CodexAppServerError("client 已关闭")
        self._disconnect_error = None
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=workdir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if self._closed:
            # close() may race the create_subprocess await.  Once the handle
            # exists, reap it before any initialize request can be sent.
            await self.close()
            raise CodexAppServerError("client 已关闭")
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="codex-app-server-reader")
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="codex-app-server-stderr")
        try:
            await self.request("initialize", {
                "clientInfo": {
                    "name": "myagents",
                    "title": "myagents",
                    "version": "0.4",
                },
            })
            await self.notify("initialized")
        except BaseException:
            await self.close()
            raise

    async def request(self, method: str, params: dict | None = None) -> dict:
        if not self.running:
            if self._disconnect_error is not None:
                raise CodexAppServerDisconnected(
                    str(self._disconnect_error)) from self._disconnect_error
            raise CodexAppServerError("app-server 未启动")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        sent = False
        try:
            await self._send({
                "id": request_id,
                "method": method,
                "params": params or {},
            })
            sent = True
            result = await asyncio.wait_for(
                asyncio.shield(future), timeout=self._request_timeout)
        except CodexAppServerRemoteError:
            raise
        except CodexAppServerRequestCancelled:
            raise
        except asyncio.CancelledError as exc:
            if sent:
                raise CodexAppServerRequestCancelled(
                    f"{method} 已发送后被取消") from exc
            raise
        except CodexAppServerRequestUncertain:
            raise
        except Exception as exc:
            if sent:
                raise CodexAppServerRequestUncertain(
                    f"{method} 已发送但结果不确定：{exc}") from exc
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A concurrent close may complete the private Future with an
                # exception after the request task itself was cancelled.
                # Retrieve it here so shutdown never emits "Future exception
                # was never retrieved"; normal awaits still propagate it.
                future.exception()
        if not isinstance(result, dict):
            raise CodexAppServerRequestUncertain(
                f"{method} 已发送但返回值不是 object")
        return result

    async def notify(self, method: str, params: dict | None = None) -> None:
        if not self.running:
            raise CodexAppServerError("app-server 未启动")
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def thread_start(
        self,
        workdir: str,
        *,
        sandbox: str | None = None,
    ) -> str:
        params: dict[str, Any] = {"cwd": workdir}
        if sandbox is not None:
            params["sandbox"] = sandbox
        result = await self.request("thread/start", params)
        return self._thread_id(result, "thread/start")

    async def thread_resume(
        self,
        thread_id: str,
        workdir: str,
        *,
        sandbox: str | None = None,
    ) -> str:
        params: dict[str, Any] = {"threadId": thread_id, "cwd": workdir}
        if sandbox is not None:
            params["sandbox"] = sandbox
        result = await self.request("thread/resume", params)
        return self._thread_id(result, "thread/resume")

    async def turn_start(
        self,
        thread_id: str,
        prompt: str,
        *,
        workdir: str | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if workdir is not None:
            params["cwd"] = workdir
        result = await self.request("turn/start", params)
        turn = result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexAppServerRequestUncertain(
                "turn/start 已发送但响应缺少 turn.id")
        return turn_id

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> None:
        await self.request("turn/interrupt", {
            "threadId": thread_id,
            "turnId": turn_id,
        })

    async def next_event(self) -> dict:
        """Return the next notification or raise a disconnect failure."""
        event = await self._events.get()
        if isinstance(event, BaseException):
            raise event
        return event

    async def close(self) -> None:
        """Idempotently fail pending work and terminate the process group."""
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        self._fail_connection(CodexAppServerDisconnected("app-server 已关闭"))
        current = asyncio.current_task()
        owned_tasks = (
            self._reader_task,
            self._stderr_task,
            *self._reverse_tasks,
        )
        # reader 可能正阻塞在满事件队列的 put() 上；仅终止子进程不会唤醒
        # 这个协程。先取消所有 owned task，确保 close 的时延与消费者无关。
        for task in owned_tasks:
            if task is not None and task is not current:
                task.cancel()
        await self._terminate_process(proc)
        for task in owned_tasks:
            if task is not None and task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._proc = None
        self._reader_task = None
        self._stderr_task = None
        self._reverse_tasks.clear()

    aclose = close

    @staticmethod
    def _thread_id(result: dict, method: str) -> str:
        thread = result.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexAppServerError(f"{method} 缺少 thread.id")
        return thread_id

    async def _send(self, message: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise CodexAppServerDisconnected("app-server 已断开")
        payload = json.dumps(
            message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        async with self._write_lock:
            wrote = False
            try:
                proc.stdin.write(payload)
                wrote = True
                await proc.stdin.drain()
            except asyncio.CancelledError as exc:
                if wrote:
                    raise CodexAppServerRequestCancelled(
                        f"{message.get('method', 'request')} 写入后被取消"
                    ) from exc
                raise
            except (BrokenPipeError, ConnectionError) as exc:
                error = CodexAppServerDisconnected(f"写入 app-server 失败：{exc}")
                self._fail_connection(error)
                if wrote:
                    raise CodexAppServerRequestUncertain(str(error)) from exc
                raise error from exc

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while chunk := await self._proc.stderr.read(8192):
            self._stderr.append(chunk.decode("utf-8", errors="replace"))

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        error: BaseException | None = None
        try:
            async for line in read_lines(self._proc.stdout):
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CodexAppServerError(
                        "app-server 输出非法 JSON："
                        f"{redact_sensitive_text(line, limit=160)}") from exc
                if not isinstance(message, dict):
                    raise CodexAppServerError("app-server frame 不是 object")
                if "id" in message and "method" in message:
                    task = asyncio.create_task(self._handle_server_request(message))
                    self._reverse_tasks.add(task)
                    task.add_done_callback(self._reverse_tasks.discard)
                elif "id" in message:
                    self._handle_response(message)
                elif isinstance(message.get("method"), str):
                    # The adapter only consumes turn/item lifecycle methods.
                    # Ignore account/rate-limit/config chatter so a warm idle
                    # process cannot grow an unbounded queue between turns.
                    if message["method"] in _FORWARDED_NOTIFICATIONS:
                        await self._events.put(message)
                else:
                    raise CodexAppServerError("app-server frame 缺少 method/id")
            returncode = await self._proc.wait()
            stderr = redact_sensitive_text(
                self._stderr.render().strip(), limit=4096)
            suffix = f"：{stderr}" if stderr else ""
            error = CodexAppServerDisconnected(
                f"app-server 断开（exit {returncode}）{suffix}")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            error = exc
        finally:
            if error is not None:
                self._fail_connection(error)
                # A dead reader makes the stdio connection permanently
                # unusable.  Kill the owned process here as well as waking
                # pending calls, so standalone clients do not leave an orphan
                # and adapter cancellation does not wait for request timeout.
                assert self._proc is not None
                await self._terminate_process(self._proc)

    def _handle_response(self, message: dict) -> None:
        request_id = message.get("id")
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        if "error" in message:
            error = message.get("error")
            if isinstance(error, dict):
                detail = error.get("message") or json.dumps(error, ensure_ascii=False)
            else:
                detail = str(error)
            future.set_exception(CodexAppServerRemoteError(
                redact_sensitive_text(str(detail), limit=2000)))
        else:
            future.set_result(message.get("result", {}))

    async def _handle_server_request(self, message: dict) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                result = await self._approval_result(method, params)
                await self._send({"id": request_id, "result": result})
            elif method == "item/permissions/requestApproval":
                result = await self._permissions_result(method, params)
                await self._send({
                    "id": request_id,
                    "result": result,
                })
            else:
                await self._send({
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"unsupported server request: {method}",
                    },
                })
        except BaseException as exc:
            with contextlib.suppress(Exception):
                await self._send({
                    "id": request_id,
                    "error": {
                        "code": -32603,
                        "message": f"approval handler failed: {exc}",
                    },
                })

    async def _approval_result(self, method: str, params: dict) -> dict:
        normalized = self._approval_prompt(method, params)
        self._pending_approvals += 1
        await self._events.put({
            "method": "client/approval/requested",
            "params": normalized,
        })
        decision = "decline"
        try:
            handler = self._permission_handler
            if handler is not None:
                outcome = handler(normalized)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                allowed = {
                    option.get("optionId")
                    for option in normalized.get("options", [])
                    if isinstance(option, dict)
                }
                decision = self._map_approval_outcome(outcome, allowed)
        except BaseException:
            decision = "decline"
        finally:
            self._pending_approvals -= 1
        await self._events.put({
            "method": "client/approval/resolved",
            "params": {
                "threadId": params.get("threadId"),
                "turnId": params.get("turnId"),
                "itemId": params.get("itemId"),
                "decision": decision,
            },
        })
        return {"decision": decision}

    async def _permissions_result(self, method: str, params: dict) -> dict:
        """Ask the shared UI about an additional-permissions request.

        The wire response has no explicit decline variant.  Decline therefore
        returns an empty permission profile; accept echoes only the profile the
        server requested, with a turn/session scope selected by the user.
        """
        normalized = {
            **params,
            "approvalMethod": method,
            "toolCall": {
                "title": self._permission_title(params),
                "kind": "permissions",
            },
            "options": [
                {"optionId": "accept", "name": "允许本轮", "kind": "allow_once"},
                {
                    "optionId": "acceptForSession",
                    "name": "本会话允许",
                    "kind": "allow_always",
                },
                {"optionId": "decline", "name": "拒绝", "kind": "reject_once"},
            ],
        }
        self._pending_approvals += 1
        await self._events.put({
            "method": "client/approval/requested",
            "params": normalized,
        })
        decision = "decline"
        try:
            handler = self._permission_handler
            if handler is not None:
                outcome = handler(normalized)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                decision = self._map_approval_outcome(
                    outcome, {"accept", "acceptForSession", "decline"})
        except BaseException:
            decision = "decline"
        finally:
            self._pending_approvals -= 1
        await self._events.put({
            "method": "client/approval/resolved",
            "params": {
                "threadId": params.get("threadId"),
                "turnId": params.get("turnId"),
                "itemId": params.get("itemId"),
                "decision": decision,
            },
        })
        requested = params.get("permissions")
        if decision in {"accept", "acceptForSession"} and isinstance(
                requested, dict):
            return {
                "permissions": requested,
                "scope": (
                    "session" if decision == "acceptForSession" else "turn"),
            }
        return {"permissions": {}, "scope": "turn"}

    @staticmethod
    def _permission_title(params: dict) -> str:
        """Build a bounded, user-readable summary of the requested grant."""
        parts: list[str] = []
        profile = params.get("permissions")
        if isinstance(profile, dict):
            network = profile.get("network")
            if isinstance(network, dict) and network.get("enabled"):
                parts.append("网络访问")
            filesystem = profile.get("fileSystem")
            if isinstance(filesystem, dict):
                entries = filesystem.get("entries")
                if isinstance(entries, list):
                    for entry in entries[:3]:
                        if not isinstance(entry, dict):
                            continue
                        access = entry.get("access") or "访问"
                        path = entry.get("path")
                        if isinstance(path, dict):
                            value = (
                                path.get("path") or path.get("pattern")
                                or path.get("value") or path.get("type"))
                        else:
                            value = path
                        parts.append(f"文件 {access}: {value or '(未指定)'}")
                for access in ("read", "write"):
                    values = filesystem.get(access)
                    if isinstance(values, list):
                        for value in values[:3]:
                            parts.append(f"文件 {access}: {value}")
        reason = params.get("reason")
        prefix = str(reason) if reason else "额外权限"
        if parts:
            prefix += "（" + "；".join(parts[:6]) + "）"
        return redact_sensitive_text(prefix, limit=500)

    @staticmethod
    def _approval_prompt(method: str, params: dict) -> dict:
        command = params.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        title = command or params.get("reason")
        if not title:
            title = "文件变更" if "fileChange" in method else "命令执行"
        title = redact_sensitive_text(str(title), limit=500)
        names = {
            "accept": ("允许一次", "allow_once"),
            "acceptForSession": ("本会话允许", "allow_always"),
            "decline": ("拒绝", "reject_once"),
            "cancel": ("拒绝并取消", "reject_always"),
        }
        available = params.get("availableDecisions")
        if "commandExecution" in method and isinstance(available, list):
            decisions = [
                value for value in available
                if isinstance(value, str) and value in names]
        else:
            decisions = list(names)
        # A malformed/empty advertised list must not manufacture an allow
        # choice.  Keep one local decline option so the UI can fail closed.
        if not decisions:
            decisions = ["decline"]
        return {
            **params,
            "approvalMethod": method,
            "toolCall": {
                "title": title,
                "kind": "edit" if "fileChange" in method else "execute",
            },
            "options": [{
                "optionId": decision,
                "name": names[decision][0],
                "kind": names[decision][1],
            } for decision in decisions],
        }

    @staticmethod
    def _map_approval_outcome(
        outcome: Any,
        allowed: set[object],
    ) -> str:
        if not isinstance(outcome, dict):
            return "decline"
        direct = outcome.get("decision")
        if isinstance(direct, str) and direct in allowed:
            return direct
        if outcome.get("outcome") == "selected":
            option_id = outcome.get("optionId")
            if isinstance(option_id, str) and option_id in allowed:
                return option_id
        return "decline"

    def _fail_connection(self, error: BaseException) -> None:
        if self._disconnect_error is None:
            self._disconnect_error = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        if self._events.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._events.get_nowait()
        self._events.put_nowait(error)

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

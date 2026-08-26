"""ACP（Agent Client Protocol）client 层。

关键概念（学习要点）：
- **ACP vs 无头 JSONL**：无头模式是"一次性命令"——每次调用新进程、
  新会话。ACP 是**有状态协议**：一个长驻进程，stdio 上跑换行分隔的
  JSON-RPC 2.0，会话在 agent 侧保持（不用 transcript 转发），还能
  中断（session/cancel）、加载旧会话（session/load）、反向请求
  （session/request_permission 问客户端要权限）。
- **请求/响应关联**：自增 id + pending Future 表；通知（无 id）走回调；
  agent→client 的反向请求（有 id 且有 method）要回响应。
- **权限默认拒绝**：未注册权限处理器（TUI 决策回调）时，
  session/request_permission 一律 cancelled（docs/acp-migration.md 的
  安全契约）。auto 放行必须显式 opt-in（permission="auto"），默认 "deny"。
  TUI 通过 permission_handler 注入异步决策回调；等待用户期间不阻塞
  read loop，client 关闭时等待中的决策被取消并尽量回 cancelled。
- **会话唯一持有者**：一个 session 同一时刻只能有一个 writer。
  本 client 持有的 session，不要再从别的进程（如普通 kimi TUI）并发写。
- 协议细节以实测为准（见 docs/acp-migration.md 的实测消息样例）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import math
import os
import signal
from typing import Any, Awaitable, Callable, Iterable, Mapping, Union

from adapters.base import BoundedLog, LineFrameTooLargeError, read_lines
from clipboard_image import TrustedImage

# 通知回调：(method, params) -> None
NotifyCallback = Callable[[str, dict], None]
JsonRpcRequestId = int | float | str
# 权限生命周期回调：(phase, request_id, params) -> None。
# adapter 用它暂停 agent inactivity timeout；不承载授权结果。
PermissionActivityCallback = Callable[[str, JsonRpcRequestId, dict], None]
# 权限决策：params -> ACP outcome，如 {"outcome": "selected", "optionId": "allow"}
# TUI 注入的处理器是 async 的（要等用户选择）；内置 policy 是同步的。
PermissionHandler = Callable[[dict], Union[dict, Awaitable[dict]]]

_INIT_TIMEOUT = 10  # initialize 握手超时（秒）
_SESSION_REQUEST_TIMEOUT = 30  # initialize 后 session new/load 的有界上限
_INBOUND_FRAME_BYTE_LIMIT = 32 * 1024 * 1024
_MAX_PENDING_PERMISSIONS = 16
_PENDING_PERMISSION_BYTE_LIMIT = 32 * 1024 * 1024
_PERMISSION_REAP_TIMEOUT = 1.0
_MAX_REVERSE_REQUEST_ID_BYTES = 256


class AcpError(Exception):
    """ACP 层的错误：agent 返回 error、进程断开、握手失败等。"""


class AcpRequestNotSentError(AcpError):
    """请求在调用 stdin.write 前失败；调用方可按明确未提交处理。"""


class AcpRequestWriteUncertainError(AcpError):
    """stdio write 已开始但 drain 失败；服务端是否接收不可判定。"""


class AcpRemoteError(AcpError):
    """agent 返回显式 JSON-RPC error；请求已被明确拒绝。"""

    def __init__(self, code: object, message: object) -> None:
        self.code = code
        self.remote_message = str(message)
        super().__init__(f"{code}: {message}")


def _validate_reverse_request_id(value: object) -> JsonRpcRequestId:
    """Return an unambiguous, hashable JSON-RPC request id.

    JSON-RPC permits string and numeric ids.  ``bool`` must not pass as an
    integer, non-finite numbers cannot be represented portably, and ``null``
    is reserved for responses whose request id could not be recovered.  ACP
    reverse requests with any other JSON shape cannot be correlated safely,
    so the connection must be treated as poisoned instead of echoing it.
    """
    if isinstance(value, str):
        try:
            request_id_bytes = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise AcpError(
                "ACP reverse request string id 无法编码；连接已关闭"
            ) from exc
        if request_id_bytes > _MAX_REVERSE_REQUEST_ID_BYTES:
            raise AcpError(
                "ACP reverse request string id 超过 "
                f"{_MAX_REVERSE_REQUEST_ID_BYTES} 字节上限；连接已关闭")
        return value
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise AcpError(
        "ACP reverse request id 必须是有限 number 或 string；连接已关闭")


def _auto_permission(params: dict) -> dict:
    """自动放行策略（显式 opt-in 才会启用）：优先 allow_once，其次任何
    allow*，否则 cancelled。"""
    options = params.get("options", [])
    for opt in options:
        if opt.get("kind") == "allow_once":
            return {"outcome": "selected", "optionId": opt["optionId"]}
    for opt in options:
        if str(opt.get("kind", "")).startswith("allow"):
            return {"outcome": "selected", "optionId": opt["optionId"]}
    return {"outcome": "cancelled"}


def _deny_permission(params: dict) -> dict:
    """默认策略：一律拒绝。UI 确认流接入前，不替用户做任何授权。"""
    return {"outcome": "cancelled"}


def _validate_outcome(outcome: object, params: dict) -> dict:
    """只接受合法 ACP outcome；None / 畸形 dict 一律 fail-closed 成 cancelled。

    决策器是外部注入的（TUI 回调），它的返回值不可信：发一个畸形 outcome
    给 agent 比拒绝更糟糕——agent 可能把它当成协议错误中断会话。
    selected 的 optionId 必须非空且确实出现在本次请求的 params.options 里，
    空字符串和任意编造的 ID 同样 fail-closed。"""
    options = params.get("options")
    if not isinstance(options, list):
        return {"outcome": "cancelled"}
    valid_ids: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            return {"outcome": "cancelled"}
        option_id = option.get("optionId")
        # Duplicate IDs are ambiguous: a peer could attach allow and reject
        # semantics to the same value, making a human rejection look like an
        # authorization.  Treat the whole request as malformed/fail-closed.
        if (
            not isinstance(option_id, str)
            or not option_id
            or option_id in valid_ids
        ):
            return {"outcome": "cancelled"}
        valid_ids.add(option_id)
    if isinstance(outcome, dict):
        if outcome.get("outcome") == "cancelled":
            return {"outcome": "cancelled"}
        if outcome.get("outcome") == "selected":
            option_id = outcome.get("optionId")
            if (isinstance(option_id, str) and option_id
                    and option_id in valid_ids):
                return {"outcome": "selected", "optionId": option_id}
    return {"outcome": "cancelled"}


class AcpClient:
    """一个 agent ACP 进程 + 其 session 的唯一持有者。

    用法：
        client = AcpClient(["kimi", "acp"])   # 权限默认 deny
        await client.start()
        sid = await client.session_new("/path/to/project")
        client.on_notification = lambda m, p: print(m, p)
        result = await client.prompt(sid, "你好")
        await client.close()
    """

    def __init__(
        self,
        cmd: list[str],
        cwd: str = ".",
        permission: str = "deny",  # 默认拒绝；"auto" 必须显式 opt-in
        permission_handler: PermissionHandler | None = None,
        env_overrides: Mapping[str, str] | None = None,
        env_removals: Iterable[str] | None = None,
        inbound_frame_byte_limit: int = _INBOUND_FRAME_BYTE_LIMIT,
        permission_reap_timeout: float = _PERMISSION_REAP_TIMEOUT,
        pending_permission_byte_limit: int = (
            _PENDING_PERMISSION_BYTE_LIMIT),
    ) -> None:
        if inbound_frame_byte_limit <= 0:
            raise ValueError("inbound_frame_byte_limit 必须大于 0")
        if permission_reap_timeout <= 0:
            raise ValueError("permission_reap_timeout 必须大于 0")
        if pending_permission_byte_limit <= 0:
            raise ValueError("pending_permission_byte_limit 必须大于 0")
        self.cmd = cmd
        self.cwd = cwd
        self.on_notification: NotifyCallback | None = None
        self.on_permission_activity: PermissionActivityCallback | None = None
        self._permission = permission
        # TUI 注入的权限决策器；None 时用内置 policy（deny/auto）
        self._permission_handler: PermissionHandler | None = permission_handler
        self._env_overrides = dict(env_overrides or {})
        self._env_removals = frozenset(env_removals or ())
        if any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            for key in self._env_removals
        ):
            raise ValueError("env_removals 只能包含合法的非空环境变量名")
        self._inbound_frame_byte_limit = inbound_frame_byte_limit
        self._permission_reap_timeout = permission_reap_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        # 进行中的权限应答 task：等用户决策可能很久，不能阻塞 read loop，
        # 也不能在 close 后还挂着——close() 会统一取消它们。
        self._permission_tasks: set[asyncio.Task] = set()
        self._permission_task_scopes: dict[
            asyncio.Task, tuple[int, str, JsonRpcRequestId, int]
        ] = {}
        # Reverse request ids are unique while their response is outstanding.
        # Reuse after a response is fine; reuse while active is ambiguous and
        # poisons the connection before a second request can reach the UI.
        self._permission_request_ids: set[JsonRpcRequestId] = set()
        self._pending_permission_bytes = 0
        self._pending_permission_byte_limit = pending_permission_byte_limit
        # permission 只能授权当前 prompt。token 阻断上一轮的迟到
        # UI 选择；session id 阻断其他 session 伪造请求。
        self._prompt_generation = 0
        self._active_prompt_token: int | None = None
        self._active_prompt_session_id: str | None = None
        self._active_prompt_request_id: int | None = None
        self._permission_state_lock = asyncio.Lock()
        self._current_session_id: str | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._stderr = BoundedLog()
        self.protocol_version: int | None = None
        self.capabilities: dict = {}
        self.auth_methods: list[dict] = []
        self.agent_info: dict = {}

    # ---- 生命周期 ----

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        """运行时注入/替换权限决策器（TUI 挂载后注入）；None 恢复内置 policy。"""
        self._permission_handler = handler

    def _default_permission(self, params: dict) -> dict:
        """内置 policy：未注入 TUI 决策器时的兜底，绝不隐式放行。"""
        if self._permission == "auto":
            return _auto_permission(params)
        return _deny_permission(params)

    async def start(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            # 重复 start 会覆盖活进程和 reader/stderr 句柄，留下没人管的
            # 孤儿进程。要重启请先 close()。
            raise AcpError("client 已在运行：先 close() 再 start()")
        process_env = None
        if self._env_overrides or self._env_removals:
            process_env = os.environ.copy()
            for key in self._env_removals:
                process_env.pop(key, None)
            # Explicit overrides intentionally win when a caller lists the
            # same key in both collections: removal protects against ambient
            # leakage, while the override is the reviewed child value.
            process_env.update(self._env_overrides)
        self._proc = await asyncio.create_subprocess_exec(
            *self.cmd,
            cwd=self.cwd,
            env=process_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # 自立进程组，close() 时整组回收
        )
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            result = await self.request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {
                    # MVP 不代理文件读写和终端：agent 用自带工具，权限走 policy
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            }, timeout=_INIT_TIMEOUT)
        except BaseException:
            await self.close()  # 握手失败不留进程
            raise
        self.protocol_version = result.get("protocolVersion")
        self.capabilities = result.get("agentCapabilities", {})
        methods = result.get("authMethods", [])
        self.auth_methods = [
            item for item in methods
            if (isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item["id"])
        ] if isinstance(methods, list) else []
        self.agent_info = result.get("agentInfo", {})

    async def close(self) -> None:
        """断线清理：取消并等待后台 task（含等待用户决策的权限应答），
        杀掉整个进程组。可重复调用。"""
        # 等待用户决策的权限应答先取消：_answer_permission 会尽量回
        # cancelled（不让 agent 侧傻等），随后 task 被取消回收。
        # 必须先取快照：await task 时 done callback 会从原 set discard，
        # 直接遍历会在两个以上并发权限请求时触发
        # RuntimeError("Set changed size during iteration")。
        await self._close_prompt_scope()
        permission_tasks = tuple(self._permission_tasks)
        for task in permission_tasks:
            task.cancel()
        for task in (self._reader, self._stderr_task):
            if task:
                task.cancel()
        if self._proc and self._proc.returncode is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except TimeoutError:
                try:
                    os.killpg(self._proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self._proc.wait()
        for task in (self._reader, self._stderr_task):
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        for task in permission_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._permission_tasks.clear()
        self._permission_task_scopes.clear()
        self._permission_request_ids.clear()
        self._pending_permission_bytes = 0
        self._current_session_id = None

    # ---- 会话方法 ----

    async def authenticate(
        self,
        method_id: str,
        *,
        timeout: float | None = None,
    ) -> None:
        """使用 initialize 明确公布的认证方式完成 ACP 连接认证。"""
        available = {
            item["id"] for item in self.auth_methods
            if isinstance(item.get("id"), str)
        }
        if not isinstance(method_id, str) or not method_id:
            raise AcpError("ACP auth method id 不能为空")
        if method_id not in available:
            rendered = "、".join(sorted(available)) or "无"
            raise AcpError(
                f"ACP agent 未公布认证方式 {method_id!r}；可用：{rendered}")
        try:
            await self.request(
                "authenticate", {"methodId": method_id}, timeout=timeout)
        except TimeoutError as exc:
            rendered = f"{timeout:g}" if timeout is not None else "有限"
            raise AcpError(
                f"ACP 认证在 {rendered} 秒内未完成；连接已关闭，可重试"
            ) from exc

    async def session_new(
        self,
        cwd: str,
        *,
        timeout: float = _SESSION_REQUEST_TIMEOUT,
    ) -> str:
        """Create a session with a mandatory finite post-initialize timeout."""
        if timeout <= 0:
            raise ValueError("session/new timeout 必须大于 0")
        try:
            result = await self.request(
                "session/new", {"cwd": cwd, "mcpServers": []}, timeout=timeout)
        except TimeoutError as exc:
            await self.close()
            raise AcpError(
                f"ACP session/new 在 {timeout:g} 秒内未完成；连接已回收"
            ) from exc
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise AcpError("ACP session/new 未返回有效 sessionId")
        self._current_session_id = session_id
        return session_id

    async def session_list(self) -> list[dict]:
        result = await self.request("session/list", {})
        return result.get("sessions", [])

    async def session_load(
        self,
        session_id: str,
        cwd: str,
        *,
        timeout: float = _SESSION_REQUEST_TIMEOUT,
    ) -> None:
        """Load a session with a mandatory finite post-initialize timeout."""
        if timeout <= 0:
            raise ValueError("session/load timeout 必须大于 0")
        try:
            await self.request("session/load", {
                "sessionId": session_id, "cwd": cwd, "mcpServers": [],
            }, timeout=timeout)
        except TimeoutError as exc:
            await self.close()
            raise AcpError(
                f"ACP session/load 在 {timeout:g} 秒内未完成；连接已回收"
            ) from exc
        self._current_session_id = session_id

    async def session_close(
        self,
        session_id: str,
        *,
        timeout: float | None = None,
    ) -> None:
        await self.request(
            "session/close", {"sessionId": session_id}, timeout=timeout)
        if self._current_session_id == session_id:
            self._current_session_id = None

    async def prompt(
        self,
        session_id: str,
        text: str,
        images: tuple[TrustedImage, ...] = (),
    ) -> dict:
        """发一轮对话；过程中的 session/update 走 on_notification。"""
        if session_id != self._current_session_id:
            raise AcpError(
                f"ACP prompt session {session_id!r} 不是当前 session "
                f"{self._current_session_id!r}")
        if self._active_prompt_token is not None:
            raise AcpError("ACP client 同一时刻只允许一个 active prompt")
        self._prompt_generation += 1
        prompt_token = self._prompt_generation
        self._active_prompt_token = prompt_token
        self._active_prompt_session_id = session_id
        content: list[dict] = [{"type": "text", "text": text}]
        for image in images:
            content.append({
                "type": "image",
                "mimeType": "image/png",
                "data": base64.b64encode(image.data).decode("ascii"),
            })
        try:
            return await self.request("session/prompt", {
                "sessionId": session_id,
                "prompt": content,
            })
        finally:
            # A terminal/error is not allowed to leave a permission decision
            # alive.  Mark the scope closed before cancellation, so even a
            # handler that suppresses CancelledError cannot send a late allow.
            await self._close_prompt_scope(prompt_token)

    async def cancel(
        self,
        session_id: str,
        *,
        permission_reap_timeout: float | None = None,
    ) -> None:
        """中断正在进行的 prompt（通知，无响应）。调用方负责等待原 prompt
        的终止响应（见 AcpAdapter.stream 的取消契约）。"""
        permission_reaped = True
        if self._active_prompt_session_id == session_id:
            permission_reaped = await self._close_prompt_scope(
                self._active_prompt_token,
                timeout=permission_reap_timeout,
            )
        await self._send({"jsonrpc": "2.0", "method": "session/cancel",
                          "params": {"sessionId": session_id}})
        if not permission_reaped:
            raise AcpError(
                "ACP permission task 未在有界时间内回收；连接已关闭")

    def _permission_scope_is_active(
        self,
        prompt_token: int,
        session_id: str,
    ) -> bool:
        return (
            self._active_prompt_token == prompt_token
            and self._active_prompt_session_id == session_id
            and self._current_session_id == session_id
        )

    async def _close_prompt_scope(
        self,
        prompt_token: int | None = None,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Invalidate a prompt and boundedly reap its permission tasks.

        A callback is untrusted application code and may suppress
        ``CancelledError``.  Returning ``False`` lets terminal/cancel paths
        poison the connection instead of stalling the ACP read loop forever.
        """
        reap_timeout = (
            self._permission_reap_timeout if timeout is None else timeout)
        if reap_timeout <= 0:
            raise ValueError("permission reap timeout 必须大于 0")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + reap_timeout
        active_token = self._active_prompt_token
        target_token = active_token if prompt_token is None else prompt_token
        if target_token is None:
            return True
        tasks = tuple(
            task for task, (token, _session_id, _request_id, _item_bytes)
            in self._permission_task_scopes.items()
            if token == target_token
        )
        # Cancel before waiting for the state lock.  A permission response may
        # currently hold that lock while blocked in stdin drain; cancelling it
        # is what releases the lock and prevents close/reset deadlock.
        for task in tasks:
            task.cancel()
        lock_acquired = False
        remaining = max(0.0, deadline - loop.time())
        if remaining:
            try:
                await asyncio.wait_for(
                    self._permission_state_lock.acquire(), timeout=remaining)
                lock_acquired = True
            except TimeoutError:
                pass
        try:
            if active_token == target_token:
                self._active_prompt_token = None
                self._active_prompt_session_id = None
                self._active_prompt_request_id = None
        finally:
            if lock_acquired:
                self._permission_state_lock.release()
        pending: set[asyncio.Task] = set()
        if tasks:
            remaining = max(0.0, deadline - loop.time())
            if remaining:
                _done, pending = await asyncio.wait(
                    tasks, timeout=remaining)
            else:
                pending = set(tasks)
        # Clear prompt-owned registries synchronously, even when buggy handler
        # code ignored cancellation.  A second cancellation narrows the window
        # further; the caller must poison/reset when ``False`` is returned.
        for task in pending:
            task.cancel()
        for task in tasks:
            self._permission_tasks.discard(task)
            scope = self._permission_task_scopes.pop(task, None)
            if scope is not None:
                self._permission_request_ids.discard(scope[2])
                self._pending_permission_bytes = max(
                    0, self._pending_permission_bytes - scope[3])
        return lock_acquired and not pending

    # ---- JSON-RPC 基础 ----

    async def request(self, method: str, params: dict | None = None,
                      timeout: float | None = None) -> Any:
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut

        def retire_pending_future() -> None:
            self._pending.pop(rid, None)
            if not fut.done():
                fut.cancel()
            elif not fut.cancelled():
                fut.exception()

        if method == "session/prompt":
            self._active_prompt_request_id = rid

        async def submit_and_wait() -> Any:
            await self._send({
                "jsonrpc": "2.0",
                "id": rid,
                "method": method,
                "params": params or {},
            })
            return await fut

        try:
            if timeout is not None:
                return await asyncio.wait_for(
                    submit_and_wait(), timeout=timeout)
            return await submit_and_wait()
        except asyncio.CancelledError:
            retire_pending_future()
            raise
        except AcpRequestNotSentError as exc:
            retire_pending_future()
            raise AcpRequestNotSentError(
                f"{method} 请求未发送：{exc}") from exc
        except Exception:
            retire_pending_future()
            raise

    async def _send(self, obj: dict) -> None:
        """写锁 + drain：并发 JSON-RPC 写入不会交错，大消息（session/prompt
        可能很大）不会在输出缓冲里无界堆积。"""
        try:
            data = json.dumps(obj).encode() + b"\n"
        except (TypeError, UnicodeError, ValueError) as exc:
            raise AcpRequestNotSentError(
                f"JSON 序列化失败：{exc}") from exc
        async with self._write_lock:
            if self._proc is None or self._proc.stdin is None:
                raise AcpRequestNotSentError("ACP stdin 尚未建立")
            write_started = False
            try:
                # Once StreamWriter.write is invoked, a later failure cannot
                # prove that the peer observed zero bytes.  Only failures above
                # this point qualify as definitely not sent.
                write_started = True
                self._proc.stdin.write(data)
                await self._proc.stdin.drain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if write_started:
                    raise AcpRequestWriteUncertainError(
                        f"ACP stdio write 已开始但 drain 失败：{exc}") from exc
                raise AcpRequestNotSentError(str(exc)) from exc

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        read_error: BaseException | None = None
        try:
            async for line in read_lines(
                    self._proc.stdout,
                    max_frame_bytes=self._inbound_frame_byte_limit):
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "method" in msg and "id" in msg:
                    await self._handle_incoming_request(msg)
                elif "method" in msg:
                    if self.on_notification:
                        self.on_notification(msg["method"], msg.get("params", {}))
                else:
                    response_id = msg.get("id")
                    if type(response_id) is not int:
                        raise AcpError(
                            "ACP response id 必须是当前 client 分配的 integer；"
                            "连接已关闭")
                    if response_id == self._active_prompt_request_id:
                        # Resolve/reap every outstanding permission before a
                        # prompt terminal/error becomes visible to its caller.
                        # This also serializes against a permission response
                        # already being drained under _permission_state_lock.
                        permission_reaped = await self._close_prompt_scope(
                            self._active_prompt_token)
                        if not permission_reaped:
                            raise AcpError(
                                "ACP permission task 未在有界时间内回收；"
                                "连接已关闭")
                    fut = self._pending.pop(response_id, None)
                    if fut and not fut.done():
                        if "error" in msg:
                            err = msg["error"]
                            fut.set_exception(AcpRemoteError(
                                err.get("code"), err.get("message")))
                        else:
                            fut.set_result(msg.get("result", {}))
        except LineFrameTooLargeError as exc:
            read_error = AcpError(
                "ACP inbound frame 超过 "
                f"{self._inbound_frame_byte_limit} 字节上限；连接已关闭")
            raise read_error from exc
        except BaseException as exc:
            read_error = exc
            raise
        finally:  # 进程断开：所有 pending 立即失败，不许挂起调用方
            # Poison the active prompt before pending futures wake.  A
            # permission handler completing after read-loop death can then
            # only produce cancelled, never a stale selected outcome.
            self._active_prompt_token = None
            self._active_prompt_session_id = None
            self._active_prompt_request_id = None
            for task in tuple(self._permission_tasks):
                task.cancel()
            self._permission_tasks.clear()
            self._permission_task_scopes.clear()
            self._permission_request_ids.clear()
            self._pending_permission_bytes = 0
            for fut in self._pending.values():
                if not fut.done():
                    if isinstance(read_error, AcpError):
                        fut.set_exception(read_error)
                    else:
                        fut.set_exception(AcpError(
                            "agent 进程断开："
                            f"{self._stderr.render()[-300:]}"))
            self._pending.clear()

    async def _handle_incoming_request(self, msg: dict) -> None:
        """agent→client 的反向请求。目前只认权限请求，其余回 method not found。"""
        rid = _validate_reverse_request_id(msg["id"])
        if msg["method"] == "session/request_permission":
            if rid in self._permission_request_ids:
                raise AcpError(
                    "ACP duplicate active reverse request id "
                    f"{rid!r}；连接已关闭")
            params = msg.get("params", {})
            session_id = params.get("sessionId") if isinstance(params, dict) else None
            prompt_token = self._active_prompt_token
            if (
                not isinstance(prompt_token, int)
                or not isinstance(session_id, str)
                or not self._permission_scope_is_active(
                    prompt_token, session_id)
            ):
                # Cross-session, post-terminal and otherwise stale requests do
                # not reach the UI and can never pause the active watchdog.
                await self._send({"jsonrpc": "2.0", "id": rid, "result": {
                    "outcome": {"outcome": "cancelled"}}})
                return
            if len(self._permission_tasks) >= _MAX_PENDING_PERMISSIONS:
                # Do not create a task or publish UI activity for overflow.
                # This request has not entered the active-id set, so a single
                # immediate cancelled response is unambiguous.
                await self._send({"jsonrpc": "2.0", "id": rid, "result": {
                    "outcome": {"outcome": "cancelled"}}})
                return
            try:
                permission_bytes = len(json.dumps(
                    params,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"))
            except (TypeError, UnicodeError, ValueError) as exc:
                raise AcpError(
                    "ACP permission params 无法进行有界字节计量；"
                    "连接已关闭") from exc
            if (self._pending_permission_bytes + permission_bytes
                    > self._pending_permission_byte_limit):
                await self._send({"jsonrpc": "2.0", "id": rid, "result": {
                    "outcome": {"outcome": "cancelled"}}})
                return
            # 权限决策可能等用户很久：独立 task 应答，不阻塞 read loop
            #（否则同 session 的 session/update 全部饿死）。
            self._permission_request_ids.add(rid)
            self._pending_permission_bytes += permission_bytes
            if self.on_permission_activity is not None:
                self.on_permission_activity(
                    "requested", rid, params)
            try:
                task = asyncio.create_task(
                    self._answer_permission(
                        rid, params, prompt_token, session_id))
            except BaseException:
                self._permission_request_ids.discard(rid)
                self._pending_permission_bytes = max(
                    0, self._pending_permission_bytes - permission_bytes)
                raise
            self._permission_tasks.add(task)
            self._permission_task_scopes[task] = (
                prompt_token, session_id, rid, permission_bytes)
            task.add_done_callback(self._on_permission_task_done)
        else:
            await self._send({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32601,
                                        "message": "method not found"}})

    async def _answer_permission(
        self,
        rid: JsonRpcRequestId,
        params: dict,
        prompt_token: int | None = None,
        session_id: str | None = None,
    ) -> None:
        """调权限决策器并回响应。任何失败/取消/畸形结果都兜底 cancelled：
        不替用户授权，也不让 agent 挂等一个永远不会来的响应。"""
        try:
            handler = self._permission_handler or self._default_permission
            try:
                outcome = handler(params)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                outcome = _validate_outcome(outcome, params)
            except asyncio.CancelledError:
                # close() 取消等待中的决策：尽量回 cancelled 再退出
                with contextlib.suppress(Exception):
                    await self._send({"jsonrpc": "2.0", "id": rid, "result": {
                        "outcome": {"outcome": "cancelled"}}})
                raise
            except Exception:
                outcome = {"outcome": "cancelled"}
            async with self._permission_state_lock:
                if (
                    prompt_token is None
                    or session_id is None
                    or not self._permission_scope_is_active(
                        prompt_token, session_id)
                ):
                    outcome = {"outcome": "cancelled"}
                await self._send({"jsonrpc": "2.0", "id": rid,
                                  "result": {"outcome": outcome}})
        finally:
            callback = self.on_permission_activity
            if callback is not None:
                callback("resolved", rid, params)

    def _on_permission_task_done(self, task: asyncio.Task) -> None:
        """权限应答 task 收尾：移除注册并消费非取消异常。

        最后的 _send 可能因连接已断而失败（close 竞态）；异常不取出的话，
        task 被回收时 event loop 会报 "Task exception was never retrieved"。
        fail-closed 已在 _answer_permission 里尽力（cancelled 兜底），
        这里只需安静消费。"""
        self._permission_tasks.discard(task)
        scope = self._permission_task_scopes.pop(task, None)
        if scope is not None:
            self._permission_request_ids.discard(scope[2])
            self._pending_permission_bytes = max(
                0, self._pending_permission_bytes - scope[3])
        if not task.cancelled():
            task.exception()  # 取出即消费，不需要处理

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while chunk := await self._proc.stderr.read(8192):
            self._stderr.append(chunk.decode("utf-8", errors="replace"))

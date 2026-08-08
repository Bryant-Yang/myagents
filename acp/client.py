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
import os
import signal
from typing import Any, Awaitable, Callable, Mapping, Union

from adapters.base import BoundedLog, read_lines
from clipboard_image import TrustedImage

# 通知回调：(method, params) -> None
NotifyCallback = Callable[[str, dict], None]
# 权限生命周期回调：(phase, request_id, params) -> None。
# adapter 用它暂停 agent inactivity timeout；不承载授权结果。
PermissionActivityCallback = Callable[[str, int, dict], None]
# 权限决策：params -> ACP outcome，如 {"outcome": "selected", "optionId": "allow"}
# TUI 注入的处理器是 async 的（要等用户选择）；内置 policy 是同步的。
PermissionHandler = Callable[[dict], Union[dict, Awaitable[dict]]]

_INIT_TIMEOUT = 10  # initialize 握手超时（秒）


class AcpError(Exception):
    """ACP 层的错误：agent 返回 error、进程断开、握手失败等。"""


class AcpRequestNotSentError(AcpError):
    """请求在 stdio drain 完成前失败；调用方可按未提交处理。"""


class AcpRemoteError(AcpError):
    """agent 返回显式 JSON-RPC error；请求已被明确拒绝。"""


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
    if isinstance(outcome, dict):
        if outcome.get("outcome") == "cancelled":
            return {"outcome": "cancelled"}
        if outcome.get("outcome") == "selected":
            option_id = outcome.get("optionId")
            valid_ids = {opt.get("optionId")
                         for opt in params.get("options", [])
                         if isinstance(opt, dict)}
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
    ) -> None:
        self.cmd = cmd
        self.cwd = cwd
        self.on_notification: NotifyCallback | None = None
        self.on_permission_activity: PermissionActivityCallback | None = None
        self._permission = permission
        # TUI 注入的权限决策器；None 时用内置 policy（deny/auto）
        self._permission_handler: PermissionHandler | None = permission_handler
        self._env_overrides = dict(env_overrides or {})
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        # 进行中的权限应答 task：等用户决策可能很久，不能阻塞 read loop，
        # 也不能在 close 后还挂着——close() 会统一取消它们。
        self._permission_tasks: set[asyncio.Task] = set()
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._stderr = BoundedLog()
        self.protocol_version: int | None = None
        self.capabilities: dict = {}
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
        if self._env_overrides:
            process_env = os.environ.copy()
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
        self.agent_info = result.get("agentInfo", {})

    async def close(self) -> None:
        """断线清理：取消并等待后台 task（含等待用户决策的权限应答），
        杀掉整个进程组。可重复调用。"""
        # 等待用户决策的权限应答先取消：_answer_permission 会尽量回
        # cancelled（不让 agent 侧傻等），随后 task 被取消回收。
        # 必须先取快照：await task 时 done callback 会从原 set discard，
        # 直接遍历会在两个以上并发权限请求时触发
        # RuntimeError("Set changed size during iteration")。
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

    # ---- 会话方法 ----

    async def session_new(self, cwd: str) -> str:
        result = await self.request("session/new", {"cwd": cwd, "mcpServers": []})
        return result["sessionId"]

    async def session_list(self) -> list[dict]:
        result = await self.request("session/list", {})
        return result.get("sessions", [])

    async def session_load(self, session_id: str, cwd: str) -> None:
        await self.request("session/load", {
            "sessionId": session_id, "cwd": cwd, "mcpServers": [],
        })

    async def prompt(
        self,
        session_id: str,
        text: str,
        images: tuple[TrustedImage, ...] = (),
    ) -> dict:
        """发一轮对话；过程中的 session/update 走 on_notification。"""
        content: list[dict] = [{"type": "text", "text": text}]
        for image in images:
            content.append({
                "type": "image",
                "mimeType": "image/png",
                "data": base64.b64encode(image.data).decode("ascii"),
            })
        return await self.request("session/prompt", {
            "sessionId": session_id,
            "prompt": content,
        })

    async def cancel(self, session_id: str) -> None:
        """中断正在进行的 prompt（通知，无响应）。调用方负责等待原 prompt
        的终止响应（见 AcpAdapter.stream 的取消契约）。"""
        await self._send({"jsonrpc": "2.0", "method": "session/cancel",
                          "params": {"sessionId": session_id}})

    # ---- JSON-RPC 基础 ----

    async def request(self, method: str, params: dict | None = None,
                      timeout: float | None = None) -> Any:
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                              "params": params or {}})
        except Exception as exc:
            self._pending.pop(rid, None)
            raise AcpRequestNotSentError(
                f"{method} 请求未发送：{exc}") from exc
        if timeout is not None:
            return await asyncio.wait_for(fut, timeout)
        return await fut

    async def _send(self, obj: dict) -> None:
        """写锁 + drain：并发 JSON-RPC 写入不会交错，大消息（session/prompt
        可能很大）不会在输出缓冲里无界堆积。"""
        assert self._proc and self._proc.stdin
        data = json.dumps(obj).encode() + b"\n"
        async with self._write_lock:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            async for line in read_lines(self._proc.stdout):
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
                    fut = self._pending.pop(msg.get("id"), None)
                    if fut and not fut.done():
                        if "error" in msg:
                            err = msg["error"]
                            fut.set_exception(AcpRemoteError(
                                f"{err.get('code')}: {err.get('message')}"))
                        else:
                            fut.set_result(msg.get("result", {}))
        finally:  # 进程断开：所有 pending 立即失败，不许挂起调用方
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AcpError(
                        f"agent 进程断开：{self._stderr.render()[-300:]}"))
            self._pending.clear()

    async def _handle_incoming_request(self, msg: dict) -> None:
        """agent→client 的反向请求。目前只认权限请求，其余回 method not found。"""
        rid = msg["id"]
        if msg["method"] == "session/request_permission":
            # 权限决策可能等用户很久：独立 task 应答，不阻塞 read loop
            #（否则同 session 的 session/update 全部饿死）。
            if self.on_permission_activity is not None:
                self.on_permission_activity(
                    "requested", rid, msg.get("params", {}))
            task = asyncio.create_task(
                self._answer_permission(rid, msg.get("params", {})))
            self._permission_tasks.add(task)
            task.add_done_callback(self._on_permission_task_done)
        else:
            await self._send({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32601,
                                        "message": "method not found"}})

    async def _answer_permission(self, rid: int, params: dict) -> None:
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
        if not task.cancelled():
            task.exception()  # 取出即消费，不需要处理

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while chunk := await self._proc.stderr.read(8192):
            self._stderr.append(chunk.decode("utf-8", errors="replace"))

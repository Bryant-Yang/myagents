"""AgentAdapter bridge for Codex's native app-server protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from adapters.base import (
    DEFAULT_AGENT_INACTIVITY_TIMEOUT,
    AgentAdapter,
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    AgentEvent,
    ExecutionMode,
    redact_sensitive_text,
)
from adapters.codex_adapter import CodexAdapter
from clipboard_image import prompt_images

from .client import (
    CodexAppServerClient,
    CodexAppServerError,
    CodexAppServerRequestCancelled,
    CodexAppServerRequestUncertain,
    PermissionHandler,
)

AgentPermissionHandler = Callable[[str, dict], Awaitable[dict]]


@dataclass(frozen=True)
class ThreadPreparation:
    """Compatibility shape consumed by Orchestrator.stream_prepared."""

    session_id: str
    restored: bool
    load_failed: bool
    fresh: bool


class CodexAppServerAdapter:
    """Stateful Codex adapter backed by one long-lived app-server process."""

    name = "codex"
    stateful_session = True
    # Codex turn 可能执行有副作用的工具。旧 thread 无法恢复时，宁可丢失旧
    # 上下文也不能把已经投递过的 transcript 自动 bootstrap 到新 thread。
    replay_history_on_fresh_session = False

    def __init__(
        self,
        cmd: list[str] | None = None,
        *,
        command: list[str] | None = None,
        sandbox: str | None = "workspace-write",
        approval_policy: str = "on-request",
        permission_handler: PermissionHandler | None = None,
        reuse_thread: bool = True,
        ephemeral_thread: bool = False,
        fallback_jsonl: bool | None = None,
        fallback_adapter: AgentAdapter | None = None,
        cancel_timeout: float = 10.0,
        inactivity_timeout: float = DEFAULT_AGENT_INACTIVITY_TIMEOUT,
        request_timeout: float = 30.0,
    ) -> None:
        if cmd is not None and command is not None:
            raise ValueError("cmd 和 command 只能指定一个")
        if cancel_timeout <= 0 or inactivity_timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        if approval_policy not in {"untrusted", "on-request", "never"}:
            raise ValueError(f"未知 approval_policy：{approval_policy!r}")
        if ephemeral_thread and reuse_thread:
            raise ValueError(
                "ephemeral_thread=True 要求 reuse_thread=False")
        self.session_id: str | None = None
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.reuse_thread = reuse_thread
        self.ephemeral_thread = ephemeral_thread
        custom_command = command is not None or cmd is not None
        self._command = list(command or cmd or ["codex", "app-server"])
        self._fallback_jsonl = (
            (not custom_command or fallback_adapter is not None)
            if fallback_jsonl is None else fallback_jsonl)
        fallback_sandbox = sandbox or "workspace-write"
        self._fallback = fallback_adapter or CodexAdapter(
            sandbox=fallback_sandbox,
            ephemeral=ephemeral_thread,
        )
        self._permission_handler = permission_handler
        self._attachment_root: Path | None = None
        self._cancel_timeout = cancel_timeout
        self._inactivity_timeout = inactivity_timeout
        self._request_timeout = request_timeout
        self._client = self._new_client()
        self._started = False
        self._active_sandbox: str | None = None
        self._closed = False
        self._active_inner: AsyncIterator[AgentEvent] | None = None
        self._stream_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def pid(self) -> int | None:
        return self._client.pid

    def _new_client(self) -> CodexAppServerClient:
        return CodexAppServerClient(
            self._command,
            permission_handler=self._permission_handler,
            request_timeout=self._request_timeout,
        )

    def set_permission_handler(
        self,
        handler: AgentPermissionHandler | None,
    ) -> None:
        if handler is None:
            self._permission_handler = None
        else:
            self._permission_handler = lambda params: handler(
                self.name,
                {
                    **params,
                    "_myagents_mirrors_permission_events": True,
                },
            )
        self._client.set_permission_handler(self._permission_handler)

    def set_attachment_root(self, root: Path | None) -> None:
        """限制可作为 app-server localImage 发送的本地附件目录。"""
        self._attachment_root = None if root is None else Path(root).absolute()

    async def _reset(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.close()
        self._started = False
        self._active_sandbox = None
        self.session_id = None
        if not self._closed:
            self._client = self._new_client()

    async def _prepare_locked(
        self,
        workdir: str,
        resume_session_id: str | None = None,
        sandbox: str | None = None,
    ) -> ThreadPreparation:
        if sandbox is None:
            sandbox = self.sandbox
        if self._closed:
            raise CodexAppServerError("adapter 已关闭")
        if self._started:
            assert self.session_id is not None
            if (resume_session_id is not None
                    and resume_session_id != self.session_id):
                raise CodexAppServerError(
                    f"resume thread {resume_session_id} 与活跃 thread "
                    f"{self.session_id} 不一致")
            return ThreadPreparation(
                self.session_id,
                restored=resume_session_id is not None,
                load_failed=False,
                fresh=False,
            )

        try:
            await self._client.start(workdir)
            if self._closed:
                await self._client.close()
                raise CodexAppServerError("adapter 已关闭")
            restored = False
            load_failed = False
            if resume_session_id:
                try:
                    self.session_id = await self._client.thread_resume(
                        resume_session_id,
                        workdir,
                        sandbox=sandbox,
                        approval_policy=self.approval_policy,
                    )
                    restored = True
                except CodexAppServerError:
                    load_failed = True
            if not restored:
                self.session_id = await self._client.thread_start(
                    workdir,
                    sandbox=sandbox,
                    approval_policy=self.approval_policy,
                    ephemeral=True if self.ephemeral_thread else None,
                )
        except BaseException:
            await self._reset()
            raise
        self._started = True
        self._active_sandbox = sandbox
        assert self.session_id is not None
        return ThreadPreparation(
            self.session_id,
            restored=restored,
            load_failed=load_failed,
            fresh=True,
        )

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
        make_prompt: Callable[[ThreadPreparation], str],
        workdir: str,
        resume_session_id: str | None = None,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        if self._closed:
            raise CodexAppServerError("adapter 已关闭")
        async with self._lock:
            if self._closed:
                raise CodexAppServerError("adapter 已关闭")
            owner = asyncio.current_task()
            self._stream_task = owner
            try:
                sandbox = self._sandbox_for(execution_mode)
                if self._started and self._active_sandbox != sandbox:
                    await self._reset()
                try:
                    prep = await self._prepare_locked(
                        workdir, resume_session_id, sandbox)
                except Exception:
                    if self._closed or not self._fallback_jsonl:
                        raise
                    # No turn exists yet: replay is side-effect safe.  Mark this as
                    # a fresh pseudo-session so Orchestrator bootstraps a bounded
                    # transcript instead of trusting an old app-server cursor.
                    prep = ThreadPreparation(
                        "codex-jsonl-fallback",
                        restored=False,
                        load_failed=resume_session_id is not None,
                        fresh=True,
                    )
                    prompt = make_prompt(prep)
                    yield AgentEvent(
                        "info",
                        "Codex app-server 启动失败，使用 JSONL fallback")
                    if execution_mode is ExecutionMode.DEFAULT:
                        inner = self._fallback.stream(prompt, workdir)
                    else:
                        inner = self._fallback.stream(
                            prompt, workdir, execution_mode=execution_mode)
                    self._active_inner = inner
                    try:
                        async for event in inner:
                            yield event
                    finally:
                        with contextlib.suppress(RuntimeError):
                            await inner.aclose()
                        if self._active_inner is inner:
                            self._active_inner = None
                    return
                try:
                    prompt = make_prompt(prep)
                except BaseException:
                    if prep.fresh:
                        await self._reset()
                    raise
                if prep.fresh:
                    verb = "已恢复" if prep.restored else "已建立"
                    yield AgentEvent(
                        "info", f"Codex thread {verb}：{prep.session_id}")
                inner = self._turn_locked(
                    prep.session_id, prompt, workdir, execution_mode)
                self._active_inner = inner
                try:
                    async for event in inner:
                        yield event
                finally:
                    with contextlib.suppress(RuntimeError):
                        await inner.aclose()
                    if self._active_inner is inner:
                        self._active_inner = None
                    if not self.reuse_thread:
                        # Host prompts carry their own bounded transcript snapshot.
                        # Reuse the warm process, but start a clean thread next time
                        # so those snapshots are not duplicated in native history.
                        self._started = False
                        self._active_sandbox = None
                        self.session_id = None
            finally:
                if self._stream_task is owner:
                    self._stream_task = None

    async def _turn_locked(
        self,
        thread_id: str,
        prompt: str,
        workdir: str,
        execution_mode: ExecutionMode,
    ) -> AsyncIterator[AgentEvent]:
        turn_id: str | None = None
        terminal = False
        if execution_mode is ExecutionMode.READ_ONLY:
            self._client.set_permission_handler(
                lambda _params: {
                    "outcome": "selected",
                    "optionId": "decline",
                })
        else:
            self._client.set_permission_handler(self._permission_handler)
        try:
            try:
                images = prompt_images(
                    prompt, self._attachment_root)
                turn_id = await self._client.turn_start(
                    thread_id,
                    prompt,
                    workdir=workdir,
                    images=images,
                )
            except CodexAppServerRequestCancelled as exc:
                await self._reset()
                raise AgentDeliveryCancelledError(
                    "Codex turn/start 写入后被取消，禁止自动重投"
                ) from exc
            except CodexAppServerRequestUncertain as exc:
                # The request may have reached the server even if its response
                # was lost.  Never replay and never trust this connection for
                # a later turn: closing the process group is the only way to
                # rule out a still-running tool call.
                await self._reset()
                raise AgentDeliveryUncertainError(
                    "Codex turn/start 结果不确定；连接已作废，禁止自动重投"
                ) from exc
            except BaseException:
                # Explicit server rejection or a confirmed pre-send failure did
                # not start a turn.  Rebuild the connection, but preserve the
                # ordinary retry semantics by not marking a no-replay boundary.
                await self._reset()
                raise
            # turn/start 已明确接受。在任何正文、工具或权限事件到达外部 sink
            # 前，让 Orchestrator 先持久化 no-replay cursor。
            yield AgentEvent("delivery_committed", meta={
                "threadId": thread_id,
                "turnId": turn_id,
            })
            while True:
                try:
                    if self._client.pending_approvals:
                        # Human approval is not agent inactivity.  Cancellation
                        # and disconnect still wake next_event immediately.
                        message = await self._client.next_event()
                    else:
                        message = await asyncio.wait_for(
                            self._client.next_event(),
                            timeout=self._inactivity_timeout,
                        )
                except TimeoutError:
                    raise CodexAppServerError(
                        f"Codex turn 连续 {self._inactivity_timeout:g}s "
                        "无活动，正在取消") from None
                method = message.get("method")
                params = message.get("params")
                if not isinstance(params, dict):
                    params = {}
                if not self._belongs(params, thread_id, turn_id):
                    continue

                if method == "item/agentMessage/delta":
                    text = params.get("delta")
                    if isinstance(text, str) and text:
                        yield AgentEvent("text", text)
                elif method == "item/started":
                    event = self._tool_started(params)
                    if event is not None:
                        yield event
                elif method == "item/completed":
                    event = self._tool_completed(params)
                    if event is not None:
                        yield event
                elif method == "client/approval/requested":
                    title = params.get("toolCall", {}).get(
                        "title", "工具调用")
                    yield AgentEvent(
                        "permission", f"等待权限：{title}",
                        meta={"item_id": params.get("itemId")})
                elif method == "client/approval/resolved":
                    yield AgentEvent(
                        "permission",
                        f"权限决定：{params.get('decision', 'decline')}",
                        meta={"item_id": params.get("itemId")})
                elif method == "error":
                    error = params.get("error")
                    if isinstance(error, dict):
                        detail = error.get("message") or json.dumps(
                            error, ensure_ascii=False)
                    else:
                        detail = str(error or "Codex turn error")
                    if not params.get("willRetry", False):
                        yield AgentEvent(
                            "error", redact_sensitive_text(detail, limit=2000))
                elif method == "turn/completed":
                    terminal = True
                    turn = params.get("turn")
                    status = turn.get("status") if isinstance(turn, dict) else None
                    if status == "failed":
                        error = turn.get("error") if isinstance(turn, dict) else None
                        raise CodexAppServerError(
                            self._error_text(error) or "Codex turn failed")
                    if status == "interrupted":
                        raise CodexAppServerError("Codex turn 已中断")
                    if status != "completed":
                        await self._reset()
                        raise CodexAppServerError(
                            f"turn/completed 携带非法状态：{status!r}")
                    yield AgentEvent("done", meta={
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "status": status or "completed",
                        "appServerPid": self._client.pid,
                    })
                    break
        except AgentDeliveryCancelledError:
            raise
        except asyncio.CancelledError as exc:
            if turn_id is not None:
                # Orchestrator/CommandBus still sees cancellation, but must
                # also persist the post-submit no-replay boundary.
                raise AgentDeliveryCancelledError(
                    "Codex turn 已提交后被取消，禁止自动重投"
                ) from exc
            raise
        except AgentDeliveryUncertainError:
            raise
        except Exception as exc:
            if turn_id is None:
                # Explicit rejection/pre-send failure: no turn was accepted.
                raise
            # turn_id exists: the server accepted turn/start.  Any later
            # non-success (failed/interrupted/disconnect/timeout/bad terminal)
            # is a no-replay failure even when the terminal status is known.
            raise AgentDeliveryUncertainError(
                f"Codex turn 已提交但未成功完成：{exc}"
            ) from exc
        finally:
            self._client.set_permission_handler(self._permission_handler)
            if turn_id is not None and not terminal:
                await self._interrupt_and_confirm(thread_id, turn_id)

    def _sandbox_for(self, execution_mode: ExecutionMode) -> str | None:
        if execution_mode is ExecutionMode.READ_ONLY:
            return "read-only"
        if execution_mode is ExecutionMode.WORKSPACE_WRITE:
            return "workspace-write"
        return self.sandbox

    async def _interrupt_and_confirm(
        self,
        thread_id: str,
        turn_id: str,
    ) -> None:
        try:
            await self._client.turn_interrupt(thread_id, turn_id)

            async def wait_terminal() -> None:
                while True:
                    message = await self._client.next_event()
                    if message.get("method") != "turn/completed":
                        continue
                    params = message.get("params")
                    if (isinstance(params, dict)
                            and params.get("threadId") == thread_id
                            and isinstance(params.get("turn"), dict)
                            and params["turn"].get("id") == turn_id):
                        return

            await asyncio.wait_for(wait_terminal(), timeout=self._cancel_timeout)
        except BaseException:
            await self._reset()

    @staticmethod
    def _belongs(params: dict, thread_id: str, turn_id: str) -> bool:
        event_thread = params.get("threadId")
        event_turn = params.get("turnId")
        turn = params.get("turn")
        if event_turn is None and isinstance(turn, dict):
            event_turn = turn.get("id")
        if event_thread is not None and event_thread != thread_id:
            return False
        if event_turn is not None and event_turn != turn_id:
            return False
        return True

    @staticmethod
    def _tool_started(params: dict) -> AgentEvent | None:
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        kind = item.get("type")
        if kind == "reasoning":
            return AgentEvent("status", "Codex 正在分析…")
        if kind == "commandExecution":
            command = item.get("command")
            safe = redact_sensitive_text(command) if isinstance(command, str) else ""
            return AgentEvent(
                "tool", "命令执行",
                meta={"tool_call_id": item.get("id"), "command": safe})
        if kind == "fileChange":
            return AgentEvent(
                "tool", "文件变更",
                meta={"tool_call_id": item.get("id")})
        if kind == "mcpToolCall":
            title = f"{item.get('server', 'MCP')}/{item.get('tool', 'tool')}"
            return AgentEvent(
                "tool", title,
                meta={"tool_call_id": item.get("id"), "tool_kind": kind})
        if kind == "dynamicToolCall":
            return AgentEvent(
                "tool", str(item.get("tool") or "动态工具"),
                meta={"tool_call_id": item.get("id"), "tool_kind": kind})
        if kind in {"webSearch", "imageGeneration"}:
            return AgentEvent(
                "tool", "网页搜索" if kind == "webSearch" else "图片生成",
                meta={"tool_call_id": item.get("id"), "tool_kind": kind})
        return None

    @staticmethod
    def _tool_completed(params: dict) -> AgentEvent | None:
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        kind = item.get("type")
        if kind in {
            "commandExecution", "fileChange", "mcpToolCall",
            "dynamicToolCall", "webSearch", "imageGeneration",
        }:
            status = item.get("status") or "completed"
            started = CodexAppServerAdapter._tool_started(params)
            if started is not None and started.kind == "tool":
                return AgentEvent(
                    "tool",
                    started.text,
                    meta={
                        **started.meta,
                        "status": status,
                        "update": True,
                    },
                )
        return None

    @staticmethod
    def _error_text(error: object) -> str:
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return redact_sensitive_text(message, limit=1000)
            return redact_sensitive_text(
                json.dumps(error, ensure_ascii=False), limit=1000)
        return redact_sensitive_text(str(error), limit=1000) if error else ""

    async def aclose(self) -> None:
        # Mark closed before touching the process so a concurrent prepare cannot
        # spawn a replacement after this close begins.  Closing the active
        # client outside the writer lock wakes a stream blocked in next_event;
        # the lock is then acquired only to wait for its cancellation cleanup.
        self._closed = True
        current = asyncio.current_task()
        owner = self._stream_task
        inner = self._active_inner
        if owner is not None and owner is not current and not owner.done():
            owner.cancel()
        elif inner is not None:
            # Same-task close after breaking from async-for: the outer
            # generator is parked at yield and still owns the lock.  Closing
            # the inner generator is enough to interrupt/reap its transport.
            with contextlib.suppress(
                    asyncio.CancelledError, RuntimeError, Exception):
                await asyncio.wait_for(
                    inner.aclose(), timeout=self._cancel_timeout)
        await self._client.close()
        if owner is not None and owner is not current:
            with contextlib.suppress(
                    asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(
                    asyncio.shield(owner), timeout=self._cancel_timeout)
        acquired = False
        try:
            await asyncio.wait_for(
                self._lock.acquire(),
                timeout=min(self._cancel_timeout, 1.0),
            )
            acquired = True
            # Covers a create_subprocess race that began before _closed was set.
            await self._client.close()
        except TimeoutError:
            # A caller may have abandoned the outer async generator at a yield
            # point.  Its inner transport and process are already closed above;
            # do not let a stale generator lock make shutdown unbounded.
            pass
        finally:
            if acquired:
                self._lock.release()
        self._active_inner = None
        self._started = False
        self._active_sandbox = None
        self.session_id = None

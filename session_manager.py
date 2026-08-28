"""TUI 多会话生命周期的深模块。

调用方只按稳定 room_id 激活、提交和观察；每个 runtime 内部仍保留独立
Orchestrator/CommandBus/ControlServer 与房间 lease。全局执行 gate 只限制实际
dispatch 数量，不改变每个 CommandBus 的 FIFO 与取消语义。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from adapters.base import AgentEvent, redact_sensitive_text
from control import CommandBus, ControlServer
from agent_readiness import AgentReadiness
from orchestrator import AGENT_SPECS, AgentSpec, Message, Orchestrator
from session_catalog import (
    SessionCatalog,
    SessionSummary,
    derive_session_title,
)
from storage.store import DEFAULT_SESSION_NAME, RoomStore, normalize_workdir


SessionEventSink = Callable[[str, str, AgentEvent], None]
SessionPermissionHandler = Callable[[str, str, dict], Awaitable[dict]]


@dataclass(frozen=True)
class ManagedCommand:
    session_id: str
    command_id: str


@dataclass(frozen=True)
class SessionNotice:
    session_id: str
    title: str
    project_name: str
    status: str
    error: str | None = None


@dataclass(frozen=True)
class SessionTerminal:
    session_id: str
    command_id: str
    status: str
    error: str | None = None
    created_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class SessionSnapshot:
    summary: SessionSummary
    status: str
    unread: bool
    loaded: bool
    draft: str
    cursor_position: int
    history: tuple[Message, ...]


@dataclass
class _Runtime:
    summary: SessionSummary
    orch: Orchestrator
    bus: CommandBus
    control: ControlServer | None
    status: str = "idle"
    unread: bool = False
    draft: str = ""
    cursor_position: int = 0
    last_used: float = 0.0
    permission_waits: int = 0


class _GatedOrchestrator:
    """CommandBus 内部 adapter：共享执行 gate，其他行为委托给房间 owner。"""

    def __init__(
        self,
        orch: Orchestrator,
        gate: asyncio.Semaphore,
    ) -> None:
        self._orch = orch
        self._gate = gate
        self.store = orch.store

    async def dispatch(self, message, on_event, *, command_id=None):
        waited = self._gate.locked()
        if waited:
            on_event("system", AgentEvent(
                "status",
                "等待可用的会话执行资源",
                {"agent_state": "waiting_resource", "phase": "等待资源"},
            ))
        await self._gate.acquire()
        try:
            if waited:
                on_event("system", AgentEvent(
                    "status",
                    "执行资源已就绪",
                    {"agent_state": "running", "phase": "准备执行"},
                ))
            return await self._orch.dispatch(
                message,
                on_event,
                command_id=command_id,
            )
        finally:
            self._gate.release()

    def prepare_workflow_steering(self, command_id, instruction):
        return self._orch.prepare_workflow_steering(command_id, instruction)

    def prepare_interjection(self, command_id, instruction):
        return self._orch.prepare_interjection(command_id, instruction)


class SessionManager:
    """拥有多个隔离房间 runtime，并把 UI 行为集中到稳定 room_id。"""

    def __init__(
        self,
        workdir: str | Path,
        *,
        session_name: str = DEFAULT_SESSION_NAME,
        state_root: str | Path | None = None,
        specs: tuple[AgentSpec, ...] = AGENT_SPECS,
        event_sink: SessionEventSink | None = None,
        permission_handler: SessionPermissionHandler | None = None,
        notification_sink: Callable[[SessionNotice], None] | None = None,
        terminal_sink: Callable[[SessionTerminal], None] | None = None,
        max_running_sessions: int = 3,
        idle_timeout: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        enable_control: bool = True,
        initial_orchestrator: Orchestrator | None = None,
    ) -> None:
        if max_running_sessions < 1:
            raise ValueError("max_running_sessions 必须 >= 1")
        if idle_timeout <= 0:
            raise ValueError("idle_timeout 必须为正数")
        self.initial_workdir = normalize_workdir(workdir)
        self.initial_session_name = (
            initial_orchestrator.session_name
            if initial_orchestrator is not None
            else session_name
        )
        inferred_root = (
            initial_orchestrator.store.state_root
            if initial_orchestrator is not None
            and initial_orchestrator.store is not None
            else state_root
        )
        self.catalog = SessionCatalog(inferred_root)
        self._specs = specs
        self._event_sink = event_sink
        self._permission_handler = permission_handler
        self._notification_sink = notification_sink
        self._terminal_sink = terminal_sink
        self._gate = asyncio.Semaphore(max_running_sessions)
        self._idle_timeout = float(idle_timeout)
        self._clock = clock
        self._enable_control = enable_control
        self._initial_orchestrator = initial_orchestrator
        self._discover_agents = (
            initial_orchestrator.discover_agents
            if initial_orchestrator is not None else False
        )
        self._host_probe = (
            initial_orchestrator.host_readiness_probe
            if initial_orchestrator is not None else None
        )
        self._host_model_factory = (
            initial_orchestrator.host_model_factory
            if initial_orchestrator is not None else None
        )
        self._agent_enablement = (
            initial_orchestrator.agent_enablement
            if initial_orchestrator is not None else None
        )
        self._runtimes: dict[str, _Runtime] = {}
        self._drafts: dict[str, tuple[str, int]] = {}
        self._detached_states: dict[str, tuple[str, bool]] = {}
        self._watchers: set[asyncio.Task] = set()
        self._watcher_errors: list[BaseException] = []
        self._watched_commands: set[tuple[str, str]] = set()
        self._active_id: str | None = None
        self._started = False
        self._closed = False
        if initial_orchestrator is not None:
            if initial_orchestrator.store is None:
                raise ValueError("多会话管理需要持久 Orchestrator")
            summary = self.catalog.get_session(initial_orchestrator.store.room_id)
            self._build_runtime(summary, initial_orchestrator)
            self._active_id = summary.room_id
            self._initial_orchestrator = None

    @property
    def active_session_id(self) -> str:
        if self._active_id is None:
            raise RuntimeError("SessionManager 尚未 start")
        return self._active_id

    @property
    def active_runtime(self) -> _Runtime:
        return self._runtime(self.active_session_id)

    def refresh_agent_readiness(self) -> tuple[AgentReadiness, ...]:
        """重扫所有已加载 room；本机 CLI 就绪事实不随会话分叉。"""
        self._require_started()
        active_statuses: tuple[AgentReadiness, ...] | None = None
        errors: list[Exception] = []
        for room_id, runtime in self._runtimes.items():
            try:
                statuses = runtime.orch.refresh_agent_readiness()
            except Exception as exc:
                errors.append(exc)
                continue
            if room_id == self._active_id:
                active_statuses = statuses
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise ExceptionGroup("部分会话的 agent 重新检测失败", errors)
        if active_statuses is None:
            raise RuntimeError("活动会话未完成 agent 重新检测")
        return active_statuses

    def set_agent_enabled(
        self,
        name: str,
        *,
        enabled: bool,
    ) -> tuple[AgentReadiness, ...]:
        """写入一次全局开关，再同步所有已加载 room。"""
        self._require_started()
        active = self.active_runtime.orch
        active.set_agent_enabled(name, enabled=enabled, write_config=True)
        errors: list[Exception] = []
        active_statuses = active.agent_readiness_snapshot()
        for room_id, runtime in self._runtimes.items():
            if room_id == self._active_id:
                continue
            try:
                runtime.orch.set_agent_enabled(
                    name, enabled=enabled, write_config=False)
            except Exception as exc:
                errors.append(exc)
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise ExceptionGroup("部分会话未同步 Agent 开关", errors)
        return active_statuses

    async def start(self) -> SessionSnapshot:
        if self._closed:
            raise RuntimeError("SessionManager 已关闭")
        if self._started:
            return self.snapshot(self.active_session_id)
        if not self._runtimes:
            store = RoomStore(
                self.initial_workdir,
                state_root=self.catalog.state_root,
                session_name=self.initial_session_name,
            )
            summary = self.catalog.get_session(store.room_id)
            orch = Orchestrator(
                self.initial_workdir,
                specs=self._specs,
                store=store,
                session_name=self.initial_session_name,
                discover_agents=self._discover_agents,
                host_probe=self._host_probe,
                host_model_factory=self._host_model_factory,
                agent_enablement=self._agent_enablement,
            )
            self._build_runtime(summary, orch)
            self._active_id = summary.room_id
        runtimes = list(self._runtimes.values())
        try:
            for runtime in runtimes:
                await self._start_runtime(runtime)
        except BaseException as start_error:
            errors: list[BaseException] = [start_error]
            for runtime in reversed(runtimes):
                try:
                    await self._close_runtime(runtime)
                except BaseException as close_error:
                    errors.append(close_error)
            self._runtimes.clear()
            self._active_id = None
            self._started = False
            if len(errors) == 1:
                raise start_error
            raise BaseExceptionGroup(
                "SessionManager 启动失败且回滚不完整",
                errors,
            )
        self._started = True
        summary = self.active_runtime.summary
        self._runtimes[summary.room_id].last_used = self._clock()
        return self.snapshot(summary.room_id)

    async def create_session(
        self,
        workdir: str | Path | None = None,
    ) -> SessionSnapshot:
        self._require_started()
        summary = self.catalog.create_session(workdir or self.active_runtime.summary.workdir)
        return await self.activate(summary.room_id)

    async def activate(self, room_id: str) -> SessionSnapshot:
        self._require_started()
        runtime = self._runtimes.get(room_id)
        if runtime is None:
            summary = self.catalog.get_session(room_id)
            store = RoomStore(
                summary.workdir,
                state_root=self.catalog.state_root,
                session_name=summary.session_name,
            )
            orch = Orchestrator(
                summary.workdir,
                specs=self._specs,
                store=store,
                session_name=summary.session_name,
                discover_agents=self._discover_agents,
                host_probe=self._host_probe,
                host_model_factory=self._host_model_factory,
                agent_enablement=self._agent_enablement,
            )
            runtime = self._build_runtime(summary, orch)
            try:
                await self._start_runtime(runtime)
            except BaseException as start_error:
                errors: list[BaseException] = [start_error]
                try:
                    await self._close_runtime(runtime)
                except BaseException as close_error:
                    errors.append(close_error)
                self._runtimes.pop(room_id, None)
                if len(errors) == 1:
                    raise start_error
                raise BaseExceptionGroup(
                    f"会话 {room_id} 启动失败且回滚不完整",
                    errors,
                )
        self._active_id = room_id
        runtime.unread = False
        runtime.last_used = self._clock()
        return self.snapshot(room_id)

    async def submit(self, message: str) -> ManagedCommand:
        runtime = self.active_runtime
        snap = await runtime.bus.submit(message)
        runtime.status = snap.status.value
        runtime.last_used = self._clock()
        command = ManagedCommand(runtime.summary.room_id, snap.command_id)
        self._watch_command(command)
        return command

    async def wait(self, command: ManagedCommand) -> dict:
        return await self._runtime(command.session_id).bus.wait(command.command_id)

    async def cancel(self, session_id: str | None = None):
        runtime = self._runtime(session_id or self.active_session_id)
        active = runtime.bus.active()
        if active is None:
            return None
        return await runtime.bus.cancel(active.command_id)

    def active_command_id(self, session_id: str | None = None) -> str | None:
        runtime = self._runtime(session_id or self.active_session_id)
        active = runtime.bus.active()
        return active.command_id if active is not None else None

    def save_draft(
        self,
        text: str,
        *,
        cursor_position: int,
        session_id: str | None = None,
    ) -> None:
        if not isinstance(text, str):
            raise ValueError("draft 必须是字符串")
        if isinstance(cursor_position, bool) or not isinstance(cursor_position, int):
            raise ValueError("cursor_position 必须是整数")
        runtime = self._runtime(session_id or self.active_session_id)
        runtime.draft = text
        runtime.cursor_position = max(0, min(cursor_position, len(text)))
        self._drafts[runtime.summary.room_id] = (
            runtime.draft,
            runtime.cursor_position,
        )
        runtime.last_used = self._clock()

    async def rename_session(
        self,
        room_id: str,
        title: str,
    ) -> SessionSnapshot:
        runtime = self._runtimes.get(room_id)
        if runtime is None:
            summary = self.catalog.rename_session(room_id, title)
            status, unread = self._detached_states.get(
                room_id, ("idle", False)
            )
            draft, cursor = self._drafts.get(room_id, ("", 0))
            return SessionSnapshot(
                summary=summary,
                status=status,
                unread=unread,
                loaded=False,
                draft=draft,
                cursor_position=cursor,
                history=(),
            )
        store = runtime.orch.store
        if store is None:
            raise RuntimeError("多会话重命名需要持久 store")
        store.set_session_title(title, pending=False)
        runtime.summary = self.catalog.get_session(room_id)
        runtime.last_used = self._clock()
        return self.snapshot(room_id)

    async def delete_session(self, room_id: str, *, confirmation: str) -> None:
        if room_id == self.active_session_id:
            raise ValueError("当前会话不能永久删除；请先切换到其他会话")
        runtime = self._runtimes.get(room_id)
        if runtime is not None:
            if runtime.bus.has_pending() or runtime.permission_waits:
                raise ValueError("运行中或等待权限的会话不能永久删除")
            await self._close_runtime(runtime)
            del self._runtimes[room_id]
        self.catalog.delete_session(room_id, confirmation=confirmation)
        self._drafts.pop(room_id, None)
        self._detached_states.pop(room_id, None)

    async def reap_idle(self) -> tuple[str, ...]:
        """回收超过空闲期限的后台 runtime；当前/pending/权限等待均保留。"""
        now = self._clock()
        reaped: list[str] = []
        for room_id, runtime in list(self._runtimes.items()):
            if room_id == self._active_id:
                continue
            if runtime.bus.has_pending() or runtime.permission_waits:
                continue
            if now - runtime.last_used < self._idle_timeout:
                continue
            self._drafts[room_id] = (
                runtime.draft,
                runtime.cursor_position,
            )
            self._detached_states[room_id] = (
                runtime.status,
                runtime.unread,
            )
            await self._close_runtime(runtime)
            del self._runtimes[room_id]
            reaped.append(room_id)
        return tuple(reaped)

    def snapshot(self, room_id: str | None = None) -> SessionSnapshot:
        runtime = self._runtime(room_id or self.active_session_id)
        runtime.summary = self.catalog.get_session(runtime.summary.room_id)
        return SessionSnapshot(
            summary=runtime.summary,
            status=runtime.status,
            unread=runtime.unread,
            loaded=True,
            draft=runtime.draft,
            cursor_position=runtime.cursor_position,
            history=tuple(runtime.orch.history),
        )

    def list_sessions(
        self,
        *,
        include_all: bool = False,
        query: str = "",
    ) -> tuple[SessionSnapshot, ...]:
        current = self.active_runtime.summary.workdir
        summaries = self.catalog.list_sessions(
            current_workdir=current,
            include_all=include_all,
            query=query,
        )
        result: list[SessionSnapshot] = []
        for summary in summaries:
            runtime = self._runtimes.get(summary.room_id)
            if runtime is None:
                draft, cursor = self._drafts.get(summary.room_id, ("", 0))
                status, unread = self._detached_states.get(
                    summary.room_id, ("idle", False)
                )
                result.append(SessionSnapshot(
                    summary=summary,
                    status=status,
                    unread=unread,
                    loaded=False,
                    draft=draft,
                    cursor_position=cursor,
                    history=(),
                ))
            else:
                result.append(self.snapshot(summary.room_id))
        return tuple(result)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for runtime in list(self._runtimes.values()):
            try:
                await self._close_runtime(runtime)
            except BaseException as exc:
                errors.append(exc)
        if self._watchers:
            await asyncio.gather(
                *tuple(self._watchers), return_exceptions=True
            )
        # done callback 负责消费异常并留存；让已完成 callback 有机会执行，
        # 再统一作为关闭错误报告，避免后台 watcher 异常静默丢失。
        await asyncio.sleep(0)
        errors.extend(self._watcher_errors)
        self._watcher_errors.clear()
        self._runtimes.clear()
        if errors:
            cancellation = next(
                (
                    error for error in errors
                    if isinstance(error, asyncio.CancelledError)
                ),
                None,
            )
            if cancellation is not None:
                raise cancellation
            raise BaseExceptionGroup("SessionManager 关闭失败", errors)

    def _build_runtime(
        self,
        summary: SessionSummary,
        orch: Orchestrator,
    ) -> _Runtime:
        gated = _GatedOrchestrator(orch, self._gate)
        bus = CommandBus(
            gated,
            lambda name, event: self._on_event(summary.room_id, name, event),
        )
        control = (
            ControlServer(
                orch,
                bus,
                command_submit_sink=lambda snapshot: self._watch_command(
                    ManagedCommand(summary.room_id, snapshot.command_id)
                ),
            )
            if self._enable_control else None
        )
        runtime = _Runtime(
            summary=summary,
            orch=orch,
            bus=bus,
            control=control,
            last_used=self._clock(),
        )
        runtime.draft, runtime.cursor_position = self._drafts.get(
            summary.room_id, ("", 0)
        )
        runtime.status, runtime.unread = self._detached_states.pop(
            summary.room_id, ("idle", False)
        )
        self._runtimes[summary.room_id] = runtime
        orch.set_permission_handler(
            lambda name, params: self._handle_permission(
                summary.room_id, name, params
            )
        )
        return runtime

    async def _start_runtime(self, runtime: _Runtime) -> None:
        runtime.bus.start()
        if runtime.control is not None:
            await runtime.control.start()

    def _watch_command(self, command: ManagedCommand) -> None:
        key = (command.session_id, command.command_id)
        if key in self._watched_commands:
            return
        self._watched_commands.add(key)
        try:
            watcher = asyncio.create_task(
                self._watch(command),
                name=f"session-command-{command.command_id}",
            )
        except Exception as exc:
            self._watched_commands.discard(key)
            if self._event_sink is not None:
                try:
                    self._event_sink(
                        command.session_id,
                        "system",
                        AgentEvent(
                            "error",
                            "任务终态观察启动失败："
                            f"{type(exc).__name__}",
                            {"command_id": command.command_id},
                        ),
                    )
                except Exception:
                    pass
            return
        self._watchers.add(watcher)

        def discard(done: asyncio.Task) -> None:
            self._watchers.discard(done)
            try:
                watcher_error = done.exception()
            except asyncio.CancelledError as exc:
                watcher_error = exc
            if watcher_error is not None:
                self._watcher_errors.append(watcher_error)
                if self._event_sink is not None:
                    detail = redact_sensitive_text(
                        str(watcher_error), limit=300)
                    try:
                        self._event_sink(
                            command.session_id,
                            "system",
                            AgentEvent(
                                "error",
                                "会话终态同步失败："
                                f"{type(watcher_error).__name__}: {detail}",
                                {"command_id": command.command_id},
                            ),
                        )
                    except Exception:
                        pass
            # 带 request_id 的命令在 CommandBus 生命周期内永久幂等；保留其
            # observer key，避免重复 submit 再触发一次后台通知。普通命令 ID
            # 不会被调用方重放，terminal 后即可释放。
            try:
                snapshot = self._runtime(command.session_id).bus.get(
                    command.command_id)
            except Exception:
                self._watched_commands.discard(key)
            else:
                if snapshot.request_id is None:
                    self._watched_commands.discard(key)

        watcher.add_done_callback(discard)

    async def _watch(self, command: ManagedCommand) -> None:
        runtime = self._runtime(command.session_id)
        while True:
            result = await runtime.bus.wait(command.command_id)
            if not result["timed_out"]:
                break
        runtime.status = result["status"]
        runtime.last_used = self._clock()
        if self._terminal_sink is not None:
            self._terminal_sink(SessionTerminal(
                session_id=command.session_id,
                command_id=command.command_id,
                status=result["status"],
                error=result.get("error"),
                created_at=result.get("created_at"),
                finished_at=result.get("finished_at"),
            ))
        background = command.session_id != self._active_id
        if background:
            runtime.unread = True
        if background and result["status"] in {"completed", "failed"} \
                and self._notification_sink is not None:
            runtime.summary = self.catalog.get_session(command.session_id)
            self._notification_sink(SessionNotice(
                session_id=command.session_id,
                title=runtime.summary.title,
                project_name=runtime.summary.project_name,
                status=result["status"],
                error=result.get("error"),
            ))

    def _on_event(self, room_id: str, name: str, event: AgentEvent) -> None:
        runtime = self._runtime(room_id)
        runtime.last_used = self._clock()
        state = event.meta.get("agent_state")
        if state == "waiting_resource":
            runtime.status = "waiting_resource"
        elif event.kind == "committed":
            runtime.status = "running"
            if name == "user" and runtime.orch.store is not None \
                    and runtime.orch.store.session_title_pending:
                title = derive_session_title(event.text)
                runtime.orch.store.set_session_title(title, pending=False)
                runtime.summary = self.catalog.get_session(room_id)
        elif state == "running" and runtime.status == "waiting_resource":
            runtime.status = "running"
        if self._event_sink is not None:
            self._event_sink(room_id, name, event)

    async def _handle_permission(
        self,
        room_id: str,
        agent_name: str,
        params: dict,
    ) -> dict:
        runtime = self._runtime(room_id)
        runtime.permission_waits += 1
        runtime.status = "waiting_permission"
        mirrors_events = (
            params.get("_myagents_mirrors_permission_events") is True
        )
        try:
            if not mirrors_events:
                self._record_permission_activity(
                    runtime,
                    agent_name,
                    self._permission_requested_text(params),
                    waiting=True,
                )
            if self._permission_handler is None:
                outcome = {"outcome": "cancelled"}
            else:
                outcome = await self._permission_handler(
                    room_id, agent_name, params
                )
            if not mirrors_events:
                selected = outcome.get("outcome") == "selected"
                detail = (
                    f"权限已选择：{outcome.get('optionId', '')}"
                    if selected else "权限已取消或拒绝"
                )
                self._record_permission_activity(
                    runtime,
                    agent_name,
                    detail,
                    waiting=False,
                )
            return outcome
        finally:
            runtime.permission_waits -= 1
            if runtime.status == "waiting_permission":
                runtime.status = "running"

    @staticmethod
    def _permission_requested_text(params: dict) -> str:
        tool = params.get("toolCall", {})
        if not isinstance(tool, dict):
            tool = {}
        title = redact_sensitive_text(
            str(tool.get("title") or "(未命名工具)"),
            limit=500,
        )
        return f"等待权限：{title}"

    def _record_permission_activity(
        self,
        runtime: _Runtime,
        agent_name: str,
        text: str,
        *,
        waiting: bool,
    ) -> None:
        active = runtime.bus.active()
        if active is None:
            raise RuntimeError("权限请求没有所属的 active command")
        command_id = active.command_id
        store = runtime.orch.store
        if store is None:
            raise RuntimeError("多会话权限审计需要持久 store")
        store.append_event(
            command_id=command_id,
            agent=agent_name,
            kind="permission",
            text=text,
        )
        if self._event_sink is not None:
            self._event_sink(
                runtime.summary.room_id,
                agent_name,
                AgentEvent(
                    "permission",
                    text,
                    {
                        "command_id": command_id,
                        "agent_state": (
                            "waiting_permission" if waiting else "running"
                        ),
                    },
                ),
            )

    async def _close_runtime(self, runtime: _Runtime) -> None:
        errors: list[BaseException] = []
        if runtime.control is not None:
            try:
                await runtime.control.aclose()
            except BaseException as exc:
                errors.append(exc)
        try:
            await runtime.bus.aclose()
        except BaseException as exc:
            errors.append(exc)
        try:
            await runtime.orch.aclose()
        except BaseException as exc:
            errors.append(exc)
        room_id = runtime.summary.room_id
        self._watched_commands.difference_update(
            tuple(
                key for key in self._watched_commands if key[0] == room_id
            )
        )
        if errors:
            cancellation = next(
                (
                    error for error in errors
                    if isinstance(error, asyncio.CancelledError)
                ),
                None,
            )
            if cancellation is not None:
                raise cancellation
            raise BaseExceptionGroup(
                f"会话 {runtime.summary.room_id} 关闭失败",
                errors,
            )

    def _runtime(self, room_id: str) -> _Runtime:
        try:
            return self._runtimes[room_id]
        except KeyError:
            raise KeyError(f"会话 runtime 未加载：{room_id}") from None

    def _require_started(self) -> None:
        if not self._started or self._closed:
            raise RuntimeError("SessionManager 未启动或已关闭")

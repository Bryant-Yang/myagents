"""CommandBus：Orchestrator 之上的单 worker 命令队列。

只依赖一个 duck-typed orch（本模块不 import Orchestrator）：

    await orch.dispatch(message, on_event, command_id=command_id)

on_event 签名是 ``on_event(agent_name, event)``；bus 配置的 event_sink
按同一签名原样收到事件。bus 不拥有 orch——``aclose()`` 只停自己的
worker、取消自己排队的命令，绝不调用 ``orch.aclose()``。

容量与清理（max_commands）：max_commands 是**硬上限**，记录总数永远
不超过它。带 request_id 的 terminal 记录**永不自动清理**——这是 bus
生命周期内 request_id 永久幂等的代价；新命令提交前只清理无 request_id
的最老 terminal 记录。清理后若仍满（全是 keyed terminal 或
active/queued），submit 抛 CommandCapacityError 拒绝入队；但已有
request_id 的重复提交永远幂等返回原记录，不受容量限制。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from adapters.base import AgentEvent

MAX_MESSAGE_BYTES = 64 * 1024      # message 的 UTF-8 字节上限
MAX_REQUEST_ID_CHARS = 128         # request_id 的字符上限
MAX_WAIT_TIMEOUT = 30.0            # wait() 的 timeout 上限（秒）
_MAX_ERROR_CHARS = 200             # failed 记录里保存的错误摘要上限


class CommandStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {CommandStatus.COMPLETED, CommandStatus.FAILED, CommandStatus.CANCELLED})


class CommandBusError(Exception):
    """CommandBus 公共异常基类。"""


class CommandBusClosedError(CommandBusError):
    """bus 已关闭：拒绝 submit / start。"""


class CommandNotFoundError(CommandBusError):
    """command_id 不存在（或记录已被清理）。"""


class CommandValidationError(CommandBusError):
    """提交参数或 wait 参数不合法。"""


class CommandCapacityError(CommandBusError):
    """容量硬上限：清理后仍满（keyed terminal 或 active/queued 占满）。"""


class _ExecutionEventPersistenceError(CommandBusError):
    """events.jsonl 写入失败。"""


def _utc_now() -> str:
    """UTC ISO-8601，Z 后缀。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CommandSnapshot:
    """一条命令记录的不可变快照。"""

    command_id: str               # UUID4 字符串
    request_id: str | None
    message: str
    status: CommandStatus
    created_at: str               # UTC-Z
    started_at: str | None        # UTC-Z；未开始为 None
    finished_at: str | None       # UTC-Z；未结束为 None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "request_id": self.request_id,
            "message": self.message,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


@dataclass
class _Command:
    """bus 内部的可变命令记录；对外只暴露 CommandSnapshot。"""

    command_id: str
    request_id: str | None
    message: str
    status: CommandStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    def snapshot(self) -> CommandSnapshot:
        return CommandSnapshot(
            command_id=self.command_id,
            request_id=self.request_id,
            message=self.message,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            error=self.error,
        )


# event_sink(agent_name, event)：与 orch.dispatch 的 on_event 同签名
EventSink = Callable[[str, Any], None]


class CommandBus:
    """单 worker 串行执行的 FIFO 命令队列。

    - ``start()`` 在当前 running loop 创建唯一 worker，重复调用安全；
      已关闭后再调用抛 CommandBusClosedError。
    - ``submit()`` 校验通过即 FIFO 入队并立即返回 queued 快照；
      request_id 在 bus 生命周期内永久幂等——重复提交（无论原记录是
      queued/running/terminal）都返回原记录当前快照，不重复入队。
    - ``wait()`` 返回 dict（``snapshot.to_dict()`` + ``timed_out`` 键）：
      命令到达 terminal 时 ``timed_out=False``；超时 ``timed_out=True``，
      超时不取消命令本身。
    - ``aclose()`` 幂等：停止接收新命令，取消 worker 与 active dispatch，
      active 与全部 queued 命令置 cancelled（补 finished_at 并唤醒
      等待者），等待 worker 退出，不残留 task；不关闭 orch。
    """

    def __init__(self, orch: Any, event_sink: EventSink | None = None,
                 max_commands: int = 1000,
                 heartbeat_interval: float = 10.0) -> None:
        if (isinstance(max_commands, bool)
                or not isinstance(max_commands, int) or max_commands < 1):
            raise CommandValidationError("max_commands 必须是 >= 1 的整数")
        if (isinstance(heartbeat_interval, bool)
                or not isinstance(heartbeat_interval, (int, float))
                or heartbeat_interval <= 0):
            raise CommandValidationError("heartbeat_interval 必须是正数")
        self._orch = orch
        self._event_sink = event_sink
        self._max_commands = max_commands
        self._heartbeat_interval = float(heartbeat_interval)
        self._commands: dict[str, _Command] = {}      # command_id → 记录（保序）
        self._by_request_id: dict[str, str] = {}      # request_id → command_id，永不清理
        self._queue: asyncio.Queue[_Command] | None = None
        self._cond: asyncio.Condition | None = None
        self._worker_task: asyncio.Task | None = None
        self._active: _Command | None = None          # worker 已取走、未 terminal 的命令
        self._dispatch_task: asyncio.Task | None = None
        self._cancel_requested: set[str] = set()
        self._last_activity = 0.0
        self._partial_buffers: dict[tuple[str, str], str] = {}
        self._partial_persisted_at: dict[tuple[str, str], float] = {}
        self._closed = False

    # ---- 生命周期 ----

    def start(self) -> None:
        """在当前 running loop 创建唯一 worker；重复调用安全。"""
        if self._closed:
            raise CommandBusClosedError("CommandBus 已关闭，拒绝 start")
        if self._worker_task is not None and not self._worker_task.done():
            return
        if self._queue is None:
            self._queue = asyncio.Queue()
            self._cond = asyncio.Condition()
        self._worker_task = asyncio.create_task(
            self._run_worker(), name="command-bus-worker")

    async def aclose(self) -> None:
        """关闭 bus（幂等）。不关闭 orch。"""
        if self._closed:
            return
        self._closed = True
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ---- 提交与查询 ----

    async def submit(self, message: str,
                     request_id: str | None = None) -> CommandSnapshot:
        """校验后 FIFO 入队，立即返回 queued 快照；request_id 永久幂等。"""
        if self._closed:
            raise CommandBusClosedError("CommandBus 已关闭，拒绝 submit")
        if self._worker_task is None:
            raise CommandBusError("先调用 start() 才能 submit")
        if self._worker_task.done():
            raise CommandBusError(
                "worker 已停止，拒绝 submit；可调用 start() 重启")
        if not isinstance(message, str) or not message.strip():
            raise CommandValidationError("message 必须是非空字符串")
        if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise CommandValidationError(
                f"message 超过 UTF-8 {MAX_MESSAGE_BYTES} 字节上限")
        if request_id is not None and (
                not isinstance(request_id, str) or not request_id
                or len(request_id) > MAX_REQUEST_ID_CHARS):
            raise CommandValidationError(
                f"request_id 必须是 1..{MAX_REQUEST_ID_CHARS} 字符的字符串或 None")
        if request_id is not None:
            existing = self._by_request_id.get(request_id)
            if existing is not None:
                # 永久幂等：无论原记录 queued/running/terminal 都原样返回，
                # 即使容量已满也不受限制
                return self._commands[existing].snapshot()
        # 硬上限：先为新记录腾一个位置（清理无 request_id 的最老 terminal
        # 记录到 max-1），腾不出来（keyed terminal 或 active/queued 占满）
        # 则拒绝，记录总数永不超过 max_commands
        self._evict(self._max_commands - 1)
        if len(self._commands) >= self._max_commands:
            raise CommandCapacityError(
                f"命令记录已达上限 {self._max_commands}，且无可清理记录")
        cmd = _Command(command_id=str(uuid.uuid4()), request_id=request_id,
                       message=message, status=CommandStatus.QUEUED,
                       created_at=_utc_now())
        self._append_execution_event(
            cmd.command_id, "system", "queued", "命令已进入队列")
        self._commands[cmd.command_id] = cmd
        if request_id is not None:
            self._by_request_id[request_id] = cmd.command_id
        self._queue.put_nowait(cmd)
        return cmd.snapshot()

    def get(self, command_id: str) -> CommandSnapshot:
        """按 command_id 查快照；不存在抛 CommandNotFoundError。"""
        return self._lookup(command_id).snapshot()

    def active(self) -> CommandSnapshot | None:
        """返回当前 running 命令；没有则为 None。"""
        if self._active is None \
                or self._active.status in TERMINAL_STATUSES:
            return None
        return self._active.snapshot()

    async def cancel(self, command_id: str) -> CommandSnapshot:
        """精确取消 queued/running 命令；terminal 命令幂等返回。"""
        cmd = self._lookup(command_id)
        if cmd.status in TERMINAL_STATUSES:
            return cmd.snapshot()
        self._cancel_requested.add(command_id)
        if cmd.status is CommandStatus.QUEUED:
            await self._transition(
                cmd, CommandStatus.CANCELLED, error="用户取消")
            return cmd.snapshot()
        if self._event_sink is not None:
            # 同步通知 TUI 先结束权限等待；否则 ACP server 可能正阻塞在
            # request_permission，收不到随后发出的 session/cancel。
            self._event_sink("system", AgentEvent(
                "cancel_requested", "正在取消当前任务…",
                {"command_id": command_id}))
        task = self._dispatch_task
        if self._active is cmd and task is not None and not task.done():
            task.cancel()
        result = await self.wait(command_id, timeout=MAX_WAIT_TIMEOUT)
        if result["timed_out"]:
            raise CommandBusError(f"取消命令超时：{command_id}")
        return self._lookup(command_id).snapshot()

    async def wait(self, command_id: str, timeout: float = MAX_WAIT_TIMEOUT
                   ) -> dict[str, Any]:
        """等命令到 terminal；返回 to_dict() + timed_out 键。

        timeout 必须在 [0, 30] 秒；terminal 立即返回（timeout 不起作用）；
        超时返回当前状态快照 + timed_out=True，不取消命令。状态变化通过
        Condition 唤醒，不靠轮询。
        """
        if (isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not 0 <= timeout <= MAX_WAIT_TIMEOUT):
            raise CommandValidationError(
                f"timeout 必须在 0..{MAX_WAIT_TIMEOUT} 秒内")
        cmd = self._lookup(command_id)
        cond = self._cond
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        timed_out = False
        async with cond:
            while cmd.status not in TERMINAL_STATUSES:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    await asyncio.wait_for(cond.wait(), remaining)
                except asyncio.TimeoutError:
                    timed_out = True
                    break
        result = cmd.snapshot().to_dict()
        result["timed_out"] = timed_out
        return result

    # ---- worker ----

    async def _run_worker(self) -> None:
        """唯一 worker：串行取命令执行。

        退出时（无论在哪一行被取消）兜底：active（已取走但未 terminal 的
        命令）与剩余 queued 全部置 cancelled、补 finished_at 并唤醒等待者，
        不留 ghost queued。
        """
        try:
            while True:
                cmd = await self._queue.get()
                if cmd.status in TERMINAL_STATUSES:
                    continue
                self._active = cmd
                try:
                    await self._run_one(cmd)
                finally:
                    # 已 terminal（正常结束或 _run_one 内部记 cancelled 后
                    # 继续抛）就清掉；否则留给外层 finally 兜底取消
                    if cmd.status in TERMINAL_STATUSES:
                        self._active = None
        finally:
            reason = "CommandBus 已关闭" if self._closed else "worker 已停止"
            pending: list[_Command] = []
            orphan = self._active
            self._active = None
            if orphan is not None and orphan.status not in TERMINAL_STATUSES:
                pending.append(orphan)
            while True:
                try:
                    queued = self._queue.get_nowait()
                    if queued.status not in TERMINAL_STATUSES:
                        pending.append(queued)
                except asyncio.QueueEmpty:
                    break
            if pending:
                async with self._cond:
                    now = _utc_now()
                    for cmd in pending:
                        cmd.status = CommandStatus.CANCELLED
                        cmd.finished_at = now
                        cmd.error = reason
                    self._cond.notify_all()
                for cmd in pending:
                    try:
                        self._flush_partial(cmd.command_id)
                        self._append_execution_event(
                            cmd.command_id, "system", "cancelled", reason)
                    except Exception as exc:
                        self._report_event_persistence_error(
                            cmd.command_id, exc)
                self._evict()

    async def _run_one(self, cmd: _Command) -> None:
        sink = self._event_sink
        loop = asyncio.get_running_loop()
        self._last_activity = loop.time()

        def on_event(name: str, event: Any) -> None:
            self._last_activity = loop.time()
            if isinstance(event, AgentEvent):
                event = AgentEvent(
                    event.kind, event.text,
                    {**event.meta, "command_id": cmd.command_id})
                self._persist_agent_event(cmd.command_id, name, event)
            if sink is not None:
                sink(name, event)

        heartbeat: asyncio.Task | None = None
        try:
            await self._transition(cmd, CommandStatus.RUNNING)
            if cmd.command_id in self._cancel_requested:
                await self._safe_terminal(
                    cmd, CommandStatus.CANCELLED, error="用户取消")
                return
            heartbeat = asyncio.create_task(
                self._heartbeat(cmd),
                name=f"command-heartbeat-{cmd.command_id}")
            self._dispatch_task = asyncio.create_task(
                self._orch.dispatch(
                    cmd.message, on_event, command_id=cmd.command_id),
                name=f"command-dispatch-{cmd.command_id}")
            if cmd.command_id in self._cancel_requested:
                self._dispatch_task.cancel()
            await self._dispatch_task
        except asyncio.CancelledError:
            await self._safe_terminal(
                cmd, CommandStatus.CANCELLED, error="已取消")
            if asyncio.current_task().cancelling():
                # bus 自己在关闭：继续向上传播，让 worker 退出
                raise
            # orch 自发抛 CancelledError：命令记 cancelled，worker 活下去
        except Exception as exc:
            if isinstance(exc, _ExecutionEventPersistenceError):
                error = str(exc)[:_MAX_ERROR_CHARS]
                self._report_event_persistence_error(
                    cmd.command_id, exc)
            else:
                error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            await self._safe_terminal(
                cmd, CommandStatus.FAILED, error=error)
        else:
            await self._safe_terminal(cmd, CommandStatus.COMPLETED)
        finally:
            self._cancel_requested.discard(cmd.command_id)
            self._dispatch_task = None
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    # 正常回收 heartbeat 会在当前 worker 未被取消时抛；
                    # 若此刻是 aclose 取消 worker，不能吞掉父 task 的取消。
                    if asyncio.current_task().cancelling():
                        raise
                except Exception as exc:
                    self._report_event_persistence_error(
                        cmd.command_id, exc)

    async def _heartbeat(self, cmd: _Command) -> None:
        loop = asyncio.get_running_loop()
        while True:
            due = self._last_activity + self._heartbeat_interval
            await asyncio.sleep(max(0.0, due - loop.time()))
            silent_for = loop.time() - self._last_activity
            if silent_for + 0.001 < self._heartbeat_interval:
                continue
            event = AgentEvent(
                "status",
                f"仍在运行，等待 agent 输出（已静默 {int(silent_for)} 秒）",
                {"command_id": cmd.command_id})
            self._append_execution_event(
                cmd.command_id, "system", "status", event.text)
            if self._event_sink is not None:
                self._event_sink("system", event)
            # heartbeat 本身成为新的可见活动，下一次正好在一个 interval 后，
            # 不受固定轮询相位影响。
            self._last_activity = loop.time()

    async def _transition(self, cmd: _Command, status: CommandStatus,
                          error: str | None = None) -> None:
        text = {
            CommandStatus.RUNNING: "开始执行",
            CommandStatus.COMPLETED: "执行完成",
            CommandStatus.FAILED: error or "执行失败",
            CommandStatus.CANCELLED: error or "执行已取消",
        }.get(status)
        if status in TERMINAL_STATUSES:
            self._flush_partial(cmd.command_id)
        # 先持久化再公开状态，防止 waiter 看到一个没有 durable event 的
        # terminal 命令。
        if text is not None:
            self._append_execution_event(
                cmd.command_id, "system", status.value, text)
        async with self._cond:
            now = _utc_now()
            if status is CommandStatus.RUNNING:
                cmd.started_at = now
            if status in TERMINAL_STATUSES:
                cmd.finished_at = now
                cmd.error = error
            cmd.status = status
            self._cond.notify_all()
        if status in TERMINAL_STATUSES:
            self._evict()

    async def _safe_terminal(
            self, cmd: _Command, status: CommandStatus,
            error: str | None = None) -> None:
        """terminal event 落盘失败时仍唤醒 waiter，且唯一 worker 继续服务。"""
        try:
            await self._transition(cmd, status, error=error)
            return
        except Exception as exc:
            storage_error = (
                f"执行事件持久化失败：{type(exc).__name__}: {exc}"
            )[:_MAX_ERROR_CHARS]
            fallback_status = (
                CommandStatus.CANCELLED
                if status is CommandStatus.CANCELLED
                else CommandStatus.FAILED)
            async with self._cond:
                now = _utc_now()
                cmd.status = fallback_status
                cmd.finished_at = now
                cmd.error = error or storage_error
                self._cond.notify_all()
            self._report_event_persistence_error(
                cmd.command_id, exc)
            self._evict()

    def _report_event_persistence_error(
            self, command_id: str, exc: Exception) -> None:
        if self._event_sink is None:
            return
        detail = (
            str(exc) if isinstance(exc, _ExecutionEventPersistenceError)
            else f"执行事件持久化失败：{type(exc).__name__}: {exc}")
        self._event_sink("system", AgentEvent(
            "error", detail,
            {"command_id": command_id}))

    def _persist_agent_event(
            self, command_id: str, agent: str, event: AgentEvent) -> None:
        kind_map = {
            "text": "partial",
            "status": "status",
            "tool": "tool",
            "permission": "permission",
            "info": "status",
            "error": "status",
        }
        kind = kind_map.get(event.kind)
        if kind is None or not event.text:
            return
        text = event.text if event.kind != "error" else f"错误：{event.text}"
        if kind == "tool":
            command = event.meta.get("command")
            if isinstance(command, str) and command:
                text = f"{text}\n{command}"
        if kind == "partial":
            key = (command_id, agent)
            buffered = self._partial_buffers.get(key, "")
            combined = (buffered + text)[-8192:]
            self._partial_buffers[key] = combined
            now = time.monotonic()
            last = self._partial_persisted_at.get(key, 0.0)
            if now - last < 1.0:
                return
            self._flush_partial(command_id, agent)
            self._partial_persisted_at[key] = now
            return
        self._append_execution_event(command_id, agent, kind, text)

    def _flush_partial(
            self, command_id: str, agent: str | None = None) -> None:
        keys = (
            [(command_id, agent)]
            if agent is not None
            else [
                key for key in (
                    self._partial_buffers.keys()
                    | self._partial_persisted_at.keys()
                )
                if key[0] == command_id
            ]
        )
        for key in keys:
            text = self._partial_buffers.pop(key, None)
            if text:
                self._append_execution_event(
                    key[0], key[1], "partial", text)
            self._partial_persisted_at.pop(key, None)

    def _append_execution_event(
            self, command_id: str, agent: str, kind: str, text: str) -> None:
        store = getattr(self._orch, "store", None)
        if store is None or not hasattr(store, "append_event"):
            return
        # RoomStore 的单条上限为 64 KiB；按 UTF-8 安全截断，避免异常输出
        # 反过来摧毁命令生命周期。
        raw = text.encode("utf-8")
        if len(raw) > 64 * 1024:
            text = raw[:64 * 1024].decode("utf-8", errors="ignore")
        try:
            store.append_event(
                command_id=command_id, agent=agent, kind=kind, text=text)
        except Exception as exc:
            raise _ExecutionEventPersistenceError(
                f"执行事件持久化失败：{type(exc).__name__}: {exc}") from exc

    # ---- 内部工具 ----

    def _lookup(self, command_id: str) -> _Command:
        try:
            return self._commands[command_id]
        except KeyError:
            raise CommandNotFoundError(f"命令不存在：{command_id}") from None

    def _evict(self, limit: int | None = None) -> None:
        """把无 request_id 的最老 terminal 记录清理到 limit（默认上限本身）。

        带 request_id 的 terminal 记录和未结束记录都不可清理；调用方
        （submit）负责在清理后仍满时拒绝，保证硬上限不被突破。
        """
        if limit is None:
            limit = self._max_commands
        if len(self._commands) <= limit:
            return
        for command_id, cmd in list(self._commands.items()):
            if len(self._commands) <= limit:
                break
            if cmd.request_id is None and cmd.status in TERMINAL_STATUSES:
                del self._commands[command_id]

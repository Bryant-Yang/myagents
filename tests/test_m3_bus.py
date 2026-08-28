"""CommandBus 纯总线测试：FakeOrch，不接真实 agent、不接 gate。

运行：.venv/bin/python tests/test_m3_bus.py
"""

import asyncio
import sys
import uuid
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import (CommandBus, CommandBusClosedError, CommandBusError,
                     CommandCapacityError, CommandNotFoundError,
                     CommandStatus, CommandValidationError)
from adapters.base import AgentEvent
from host import HostDecision
from storage.store import RoomStore


class FakeOrch:
    """duck-typed orch：记录 dispatch 顺序，可用 gate 卡住、可注入异常。"""

    def __init__(self) -> None:
        self.calls = []       # (message, command_id)，按 dispatch 开始顺序
        self.log = []         # "start:msg" / "end:msg"，验证串行
        self.gate: asyncio.Event | None = None
        self.fail_with: BaseException | None = None
        self.closed = False
        self.store = None

    async def dispatch(self, message, on_event, command_id=None):
        self.calls.append((message, command_id))
        self.log.append(f"start:{message}")
        on_event("fake", ("text", message))
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.fail_with is not None:
                raise self.fail_with
            on_event("fake", ("done", message))
        finally:
            self.log.append(f"end:{message}")

    async def aclose(self) -> None:
        self.closed = True


class _Raises:
    """极简 with 断言（与 tests/test_basic.py 同款，避免 pytest 依赖）。"""

    def __init__(self, exc_type):
        self.exc_type = exc_type
        self.exc = None

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        if t is None:
            raise AssertionError(f"未抛出 {self.exc_type.__name__}")
        if issubclass(t, self.exc_type):
            self.exc = v
            return True
        return False


def _parse(ts: str) -> datetime:
    assert ts.endswith("Z"), f"时间戳缺 Z 后缀：{ts}"
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def make_bus(orch: FakeOrch, **kwargs) -> CommandBus:
    bus = CommandBus(orch, **kwargs)
    bus.start()
    return bus


def test_validation() -> None:
    async def run() -> None:
        orch = FakeOrch()
        bus = CommandBus(orch)
        # start 前 submit 拒绝
        with _Raises(CommandBusError):
            await bus.submit("hi")
        bus.start()
        bus.start()  # 重复安全
        assert bus._worker_task is not None
        first_task = bus._worker_task
        bus.start()
        assert bus._worker_task is first_task  # 没有新建 worker

        for bad in ("", "   ", 123):
            with _Raises(CommandValidationError):
                await bus.submit(bad)
        with _Raises(CommandValidationError):
            await bus.submit("a" * (64 * 1024) + "x")       # 超 64KiB
        big_ok = await bus.submit("a" * (64 * 1024))         # 恰好 64KiB 放行
        assert big_ok.status is CommandStatus.QUEUED
        for bad in ("", "r" * 129, 42):
            with _Raises(CommandValidationError):
                await bus.submit("m", request_id=bad)
        ok = await bus.submit("m", request_id="r" * 128)     # 恰好 128 字符放行
        assert ok.request_id == "r" * 128

        with _Raises(CommandNotFoundError):
            bus.get(str(uuid.uuid4()))
        for bad in (-1, 31, "1", True):
            with _Raises(CommandValidationError):
                await bus.wait(big_ok.command_id, timeout=bad)

        with _Raises(CommandValidationError):
            CommandBus(orch, max_commands=0)
        for bad in (True, False):                     # bool 不是合法 int
            with _Raises(CommandValidationError):
                CommandBus(orch, max_commands=bad)
        await bus.aclose()

    asyncio.run(run())
    print("ok  参数校验（message/request_id/timeout/max_commands/start 幂等）")


def test_fifo_serial_and_timestamps() -> None:
    async def run() -> None:
        orch = FakeOrch()
        bus = make_bus(orch)
        snaps = [await bus.submit(f"m{i}") for i in range(3)]
        # 入队即返回：queued，时间只有 created_at
        for snap in snaps:
            assert snap.status is CommandStatus.QUEUED
            assert snap.started_at is None and snap.finished_at is None
            uuid.UUID(snap.command_id)  # command_id 是合法 UUID
            _parse(snap.created_at)
        for i, snap in enumerate(snaps):
            result = await bus.wait(snap.command_id, timeout=5)
            assert result["timed_out"] is False
            assert result["status"] == "completed"
        # FIFO + 串行：start/end 严格交替且按提交顺序
        assert [c[0] for c in orch.calls] == ["m0", "m1", "m2"]
        assert orch.log == ["start:m0", "end:m0",
                            "start:m1", "end:m1",
                            "start:m2", "end:m2"]
        # command_id 透传给 orch.dispatch
        assert [c[1] for c in orch.calls] == [s.command_id for s in snaps]
        # 时间戳单调：created <= started <= finished
        final = bus.get(snaps[0].command_id)
        assert final.status is CommandStatus.COMPLETED
        created, started, finished = (_parse(final.created_at),
                                      _parse(final.started_at),
                                      _parse(final.finished_at))
        assert created <= started <= finished
        assert final.error is None
        # to_dict 形状
        d = final.to_dict()
        assert set(d) == {"command_id", "request_id", "message", "status",
                          "created_at", "started_at", "finished_at", "error"}
        assert d["status"] == "completed"
        await bus.aclose()

    asyncio.run(run())
    print("ok  FIFO 串行 + 状态时间戳 + to_dict")


def test_dispatch_failure() -> None:
    async def run() -> None:
        orch = FakeOrch()
        bus = make_bus(orch)
        orch.fail_with = RuntimeError("炸了")
        snap = await bus.submit("会失败")
        result = await bus.wait(snap.command_id, timeout=5)
        assert result["status"] == "failed" and result["timed_out"] is False
        assert "RuntimeError" in result["error"] and "炸了" in result["error"]
        assert result["finished_at"] is not None
        # 失败后 worker 还活着，后续命令照常执行
        orch.fail_with = None
        snap2 = await bus.submit("恢复正常")
        assert (await bus.wait(snap2.command_id, timeout=5))["status"] == "completed"

        # orch 自发抛 CancelledError：命令记 cancelled，worker 不死
        orch.fail_with = asyncio.CancelledError()
        snap3 = await bus.submit("被取消")
        result3 = await bus.wait(snap3.command_id, timeout=5)
        assert result3["status"] == "cancelled"
        orch.fail_with = None
        snap4 = await bus.submit("还活着")
        assert (await bus.wait(snap4.command_id, timeout=5))["status"] == "completed"
        await bus.aclose()

    asyncio.run(run())
    print("ok  dispatch 异常 → failed；自发 CancelledError → cancelled 且 worker 存活")


def test_wait_timeout_and_terminal_immediate() -> None:
    async def run() -> None:
        orch = FakeOrch()
        orch.gate = asyncio.Event()
        bus = make_bus(orch)
        snap = await bus.submit("卡住")
        # 超时：timed_out=True，状态仍 running，命令不被取消
        result = await bus.wait(snap.command_id, timeout=0.1)
        assert result["timed_out"] is True and result["status"] == "running"
        assert bus.get(snap.command_id).status is CommandStatus.RUNNING
        # timeout=0 对 running 命令立即超时
        assert (await bus.wait(snap.command_id, timeout=0))["timed_out"] is True
        # 放行后 wait 成功
        orch.gate.set()
        result = await bus.wait(snap.command_id, timeout=5)
        assert result["timed_out"] is False and result["status"] == "completed"
        # terminal 后 wait 立即返回，timeout 再小也不超时
        result = await bus.wait(snap.command_id, timeout=0)
        assert result["timed_out"] is False and result["status"] == "completed"
        with _Raises(CommandNotFoundError):
            await bus.wait(str(uuid.uuid4()), timeout=0)
        await bus.aclose()

    asyncio.run(run())
    print("ok  wait 超时（不取消命令）/ 成功 / terminal 立即返回")


def test_active_cancel_heartbeat_and_worker_survives() -> None:
    async def run() -> None:
        orch = FakeOrch()
        orch.gate = asyncio.Event()
        events = []
        bus = make_bus(
            orch, event_sink=lambda n, e: events.append((n, e)),
            heartbeat_interval=0.05)
        first = await bus.submit("长任务")
        await asyncio.sleep(0.13)
        active = bus.active()
        assert active is not None and active.command_id == first.command_id
        heartbeats = [
            ev for _, ev in events
            if isinstance(ev, AgentEvent) and ev.kind == "status"
            and ev.meta.get("command_id") == first.command_id
            and ev.meta.get("heartbeat") is True
        ]
        assert heartbeats and "仍在运行" in heartbeats[-1].text
        elapsed = [ev.meta["silent_seconds"] for ev in heartbeats]
        assert len(elapsed) >= 2 and elapsed == sorted(elapsed), elapsed
        assert elapsed[-1] > elapsed[0], elapsed
        assert len({ev.text for ev in heartbeats}) == len(heartbeats), heartbeats

        cancelled = await bus.cancel(first.command_id)
        assert cancelled.status is CommandStatus.CANCELLED
        assert bus.active() is None

        # 取消 active 只停本轮 dispatch，不杀 bus worker。
        orch.gate = None
        second = await bus.submit("后续任务")
        result = await bus.wait(second.command_id, timeout=5)
        assert result["status"] == "completed"

        # queued 命令也可单独取消，永不 dispatch。
        orch.gate = asyncio.Event()
        blocker = await bus.submit("阻塞")
        queued = await bus.submit("排队取消")
        queued_result = await bus.cancel(queued.command_id)
        assert queued_result.status is CommandStatus.CANCELLED
        assert "排队取消" not in [message for message, _ in orch.calls]
        orch.gate.set()
        await bus.wait(blocker.command_id, timeout=5)
        await bus.aclose()

    asyncio.run(run())
    print("ok  active/queued 精确取消 + 心跳 + worker 继续服务")


def test_event_persistence_failure_fails_command_not_worker() -> None:
    class FlakyStore:
        def __init__(self) -> None:
            self.calls = 0
            self.events = []

        def append_event(self, **event) -> None:
            self.calls += 1
            if self.calls == 2:  # 第一条命令的 running event
                raise OSError("disk full")
            self.events.append(event)

    async def run() -> None:
        orch = FakeOrch()
        orch.store = FlakyStore()
        events = []
        bus = make_bus(
            orch, event_sink=lambda n, e: events.append((n, e)))
        first = await bus.submit("事件写失败")
        failed = await bus.wait(first.command_id, timeout=5)
        assert failed["status"] == "failed"
        assert "执行事件持久化失败" in failed["error"]
        assert any(
            isinstance(event, AgentEvent) and event.kind == "error"
            for _, event in events)

        second = await bus.submit("worker 继续")
        assert (await bus.wait(
            second.command_id, timeout=5))["status"] == "completed"
        await bus.aclose()

    asyncio.run(run())
    print("ok  events 写失败 → command failed，worker 继续服务")


def test_fanout_partial_events_keep_agent_identity() -> None:
    """同一 command 的并发 agent 正文必须分别持久化，不能串流归错人。"""
    class FanoutOrch(FakeOrch):
        async def dispatch(self, message, on_event, command_id=None):
            on_event("kimi", AgentEvent("text", "KIMI_1"))
            on_event("codex", AgentEvent("text", "CODEX_1"))
            on_event("kimi", AgentEvent("text", "KIMI_2"))
            on_event("codex", AgentEvent("text", "CODEX_2"))
            on_event("solo", AgentEvent("text", "SOLO_ONLY"))

    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            orch = FanoutOrch()
            orch.store = store
            bus = make_bus(orch)
            snap = await bus.submit("@kimi @codex fanout")
            result = await bus.wait(snap.command_id, timeout=5)
            assert result["status"] == "completed"
            page = store.read_events(after_seq=0, limit=50)
            partials: dict[str, str] = {}
            for event in page["items"]:
                if event.kind == "partial":
                    partials[event.agent] = (
                        partials.get(event.agent, "") + event.text)
            assert partials == {
                "kimi": "KIMI_1KIMI_2",
                "codex": "CODEX_1CODEX_2",
                "solo": "SOLO_ONLY",
            }, partials
            assert not bus._partial_buffers
            assert not bus._partial_persisted_at
            await bus.aclose()

    asyncio.run(run())
    print("ok  fan-out partial 按 agent 独立持久化")


def test_workflow_steering_uses_active_owner_and_persists_event() -> None:
    class SteeringOrch(FakeOrch):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.current = None
            self.accepted: list[str] = []

        async def dispatch(self, message, on_event, command_id=None):
            self.current = (command_id, on_event)
            self.started.set()
            await self.release.wait()

        def prepare_workflow_steering(self, command_id, instruction):
            assert self.current is not None
            current_id, _on_event = self.current
            assert command_id == current_id
            event = AgentEvent(
                "steering", instruction,
                {"workflow": True, "workflow_stage": "review"})
            receipt = SimpleNamespace(to_dict=lambda: {
                "command_id": command_id,
                "accepted": 1,
                "total_chars": len(instruction),
                "applies_after": "review",
            })
            return SimpleNamespace(
                event=event,
                commit=lambda: (
                    self.accepted.append(instruction) or receipt),
            )

        def prepare_interjection(self, command_id, instruction):
            return self.prepare_workflow_steering(command_id, instruction)

    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            orch = SteeringOrch()
            orch.store = store
            bus = make_bus(orch)
            snap = await bus.submit("/workflow ...")
            await orch.started.wait()
            receipt = bus.steer(snap.command_id, "补充空输入回归测试")
            assert receipt["accepted"] == 1
            interjected = await bus.interject(
                snap.command_id, "Alt+Up 补充验收约束")
            assert interjected["mode"] == "boundary"
            persisted = [
                event for event in store.read_events(0, 50)["items"]
                if event.command_id == snap.command_id
                and event.kind == "steering"]
            assert [(item.agent, item.text) for item in persisted] == [
                ("user", "补充空输入回归测试"),
                ("user", "Alt+Up 补充验收约束"),
            ]
            assert orch.accepted == [
                "补充空输入回归测试", "Alt+Up 补充验收约束"]
            orch.release.set()
            assert (await bus.wait(
                snap.command_id, timeout=5))["status"] == "completed"
            with _Raises(CommandValidationError):
                bus.steer(snap.command_id, "太晚")
            await bus.aclose()

    asyncio.run(run())
    print("ok  steering 复用活动 owner 并独立持久化 execution event")


def test_workflow_steering_persistence_failure_does_not_commit() -> None:
    class FailingSteeringStore(RoomStore):
        def append_event(self, *, command_id, agent, kind, text):
            if kind == "steering":
                raise OSError("disk unavailable")
            return super().append_event(
                command_id=command_id, agent=agent, kind=kind, text=text)

    class SteeringOrch(FakeOrch):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.accepted: list[str] = []

        async def dispatch(self, message, on_event, command_id=None):
            self.started.set()
            await self.release.wait()

        def prepare_workflow_steering(self, command_id, instruction):
            receipt = SimpleNamespace(to_dict=lambda: {
                "command_id": command_id,
                "accepted": 1,
                "total_chars": len(instruction),
                "applies_after": "review",
            })
            return SimpleNamespace(
                event=AgentEvent(
                    "steering", instruction,
                    {"workflow": True, "workflow_stage": "review"}),
                commit=lambda: (
                    self.accepted.append(instruction) or receipt),
            )

    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            orch = SteeringOrch()
            orch.store = FailingSteeringStore(
                workdir, state_root=root / "state")
            bus = make_bus(orch)
            snap = await bus.submit("/workflow ...")
            await orch.started.wait()
            with _Raises(CommandBusError):
                bus.steer(snap.command_id, "不得成为幽灵指令")
            assert orch.accepted == []
            persisted = orch.store.read_events(0, 50)["items"]
            assert not any(item.kind == "steering" for item in persisted)
            orch.release.set()
            await bus.wait(snap.command_id, timeout=5)
            await bus.aclose()

    asyncio.run(run())
    print("ok  steering 持久化失败时不修改 workflow 内存")


def test_runtime_interjection_is_durable_before_transport_acceptance() -> None:
    class RuntimeProposal:
        def __init__(self, command_id, instruction, accepted):
            self.event = AgentEvent(
                "interjection", instruction,
                {"agent": "pi", "mode": "in_flight"},
            )
            self.accepted = accepted
            self.command_id = command_id

        async def commit(self):
            self.accepted.append(self.event.text)
            return {
                "command_id": self.command_id,
                "accepted": 1,
                "agent": "pi",
                "mode": "in_flight",
                "applies_after": "current_turn",
            }

    class InterjectionOrch(FakeOrch):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.accepted: list[str] = []

        async def dispatch(self, message, on_event, command_id=None):
            self.started.set()
            await self.release.wait()

        def prepare_interjection(self, command_id, instruction):
            return RuntimeProposal(command_id, instruction, self.accepted)

    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            orch = InterjectionOrch()
            orch.store = store
            bus = make_bus(orch)
            snap = await bus.submit("@pi 长任务")
            await orch.started.wait()
            receipt = await bus.interject(snap.command_id, "先给结论")
            assert receipt == {
                "command_id": snap.command_id,
                "accepted": 1,
                "agent": "pi",
                "mode": "in_flight",
                "applies_after": "current_turn",
            }
            assert orch.accepted == ["先给结论"]
            events = [
                item for item in store.read_events(0, 100)["items"]
                if item.command_id == snap.command_id
                and item.kind.startswith("interjection")
            ]
            assert [(item.agent, item.kind, item.text) for item in events] == [
                ("user", "interjection_requested", "先给结论"),
                ("system", "interjection_accepted", "pi 已接受运行中插话"),
            ]
            orch.release.set()
            await bus.wait(snap.command_id, timeout=5)
            await bus.aclose()

    asyncio.run(run())
    print("ok  runtime interjection persists intent before transport acceptance")


def test_runtime_interjection_persistence_failure_never_calls_transport() -> None:
    class RuntimeProposal:
        def __init__(self, command_id, instruction, accepted):
            self.event = AgentEvent(
                "interjection", instruction,
                {"agent": "pi", "mode": "in_flight"},
            )
            self.accepted = accepted
            self.command_id = command_id

        async def commit(self):
            self.accepted.append(self.event.text)
            return {
                "command_id": self.command_id,
                "accepted": 1,
                "agent": "pi",
                "mode": "in_flight",
            }

    class FailingInterjectionStore(RoomStore):
        def append_event(self, *, command_id, agent, kind, text):
            if kind == "interjection_requested":
                raise OSError("simulated persistence failure")
            return super().append_event(
                command_id=command_id, agent=agent, kind=kind, text=text)

    class InterjectionOrch(FakeOrch):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.accepted: list[str] = []

        async def dispatch(self, message, on_event, command_id=None):
            self.started.set()
            await self.release.wait()

        def prepare_interjection(self, command_id, instruction):
            return RuntimeProposal(command_id, instruction, self.accepted)

    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            store = FailingInterjectionStore(
                workdir, state_root=root / "state")
            orch = InterjectionOrch()
            orch.store = store
            bus = make_bus(orch)
            snap = await bus.submit("@pi 长任务")
            await orch.started.wait()
            try:
                await bus.interject(snap.command_id, "不能越过落盘边界")
            except Exception as exc:
                assert "执行事件持久化失败" in str(exc)
            else:
                raise AssertionError("persistence failure was not propagated")
            assert orch.accepted == []
            orch.release.set()
            await bus.wait(snap.command_id, timeout=5)
            await bus.aclose()

    asyncio.run(run())
    print("ok  runtime interjection persistence failure blocks transport")


def test_duplicate_tool_updates_are_forwarded_and_persisted_once() -> None:
    """adapter 失控重复吐同一工具状态时，bus 仍保护 UI 与 events 日志。"""
    class ToolSpamOrch(FakeOrch):
        async def dispatch(self, message, on_event, command_id=None):
            event = AgentEvent(
                "tool",
                "检查 JavaScript",
                {
                    "tool_call_id": "tool-1",
                    "status": "in_progress",
                    "command": "node --check demo.js",
                    "update": True,
                },
            )
            for _ in range(200):
                on_event("kimi", event)

    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            orch = ToolSpamOrch()
            orch.store = store
            forwarded = []
            bus = make_bus(
                orch, event_sink=lambda name, event: forwarded.append(
                    (name, event)))
            snap = await bus.submit("@kimi tool spam")
            result = await bus.wait(snap.command_id, timeout=5)
            assert result["status"] == "completed"
            visible = [
                event for name, event in forwarded
                if name == "kimi"
                and isinstance(event, AgentEvent)
                and event.kind == "tool"
            ]
            persisted = [
                event for event in store.read_events(0, 50)["items"]
                if event.command_id == snap.command_id
                and event.kind == "tool"
            ]
            assert len(visible) == 1, len(visible)
            assert len(persisted) == 1, len(persisted)
            await bus.aclose()

    asyncio.run(run())
    print("ok  CommandBus 工具状态防御性去重")


def test_activity_only_events_refresh_silence_without_becoming_visible() -> None:
    """无可见增量的协议活动应压住 heartbeat，但不进入 sink/events。"""
    class ActivityOrch(FakeOrch):
        async def dispatch(self, message, on_event, command_id=None):
            for _ in range(7):
                await asyncio.sleep(0.02)
                on_event("kimi", AgentEvent(
                    "activity", meta={"phase": "tool"}))

    async def run() -> None:
        forwarded = []
        bus = CommandBus(
            ActivityOrch(),
            event_sink=lambda name, event: forwarded.append((name, event)),
            heartbeat_interval=0.05,
        )
        bus.start()
        snap = await bus.submit("@kimi activity")
        result = await bus.wait(snap.command_id, timeout=5)
        assert result["status"] == "completed"
        assert not [
            event for _name, event in forwarded
            if isinstance(event, AgentEvent)
            and (event.kind == "activity"
                 or event.meta.get("heartbeat") is True)
        ], forwarded
        await bus.aclose()

    asyncio.run(run())
    print("ok  activity-only 刷新静默时钟且不进入可见事件")


def test_status_dedup_keeps_visible_phase_changes() -> None:
    """文本相同但可见阶段不同不是重复状态。"""
    class PhaseOrch(FakeOrch):
        async def dispatch(self, message, on_event, command_id=None):
            on_event("kimi", AgentEvent(
                "status", "仍在运行",
                {"agent_state": "running", "phase": "分析"}))
            on_event("kimi", AgentEvent(
                "status", "仍在运行",
                {"agent_state": "running", "phase": "实现"}))

    async def run() -> None:
        forwarded = []
        bus = make_bus(
            PhaseOrch(),
            event_sink=lambda name, event: forwarded.append((name, event)),
        )
        snap = await bus.submit("@kimi phases")
        result = await bus.wait(snap.command_id, timeout=5)
        assert result["status"] == "completed"
        phases = [
            event.meta.get("phase")
            for name, event in forwarded
            if name == "kimi" and isinstance(event, AgentEvent)
            and event.kind == "status"
        ]
        assert phases == ["分析", "实现"], phases
        await bus.aclose()

    asyncio.run(run())
    print("ok  CommandBus 去重保留可见阶段变化")


def test_orchestrator_does_not_swallow_event_persistence_failure() -> None:
    """事件 sink 写盘失败必须穿透 Orchestrator，使 command 诚实失败。"""
    async def run() -> None:
        from orchestrator import Orchestrator

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            orch = Orchestrator(str(workdir), store=store)
            orch.adapters["kimi"] = FakeAgentAdapter("kimi")
            original_append_event = store.append_event

            def fail_partial(**event) -> None:
                if event.get("kind") == "partial":
                    raise OSError("disk full on partial")
                original_append_event(**event)

            store.append_event = fail_partial  # type: ignore[method-assign]
            bus = make_bus(orch)
            snap = await bus.submit("@kimi 触发 partial 写失败")
            result = await bus.wait(snap.command_id, timeout=5)
            assert result["status"] == "failed", result
            assert "执行事件持久化失败" in result["error"]
            assert not any(
                event.kind == "completed"
                for event in store.read_events(0, 50)["items"]
                if event.command_id == snap.command_id
            )
            await bus.aclose()
            await orch.aclose()

    asyncio.run(run())
    print("ok  Orchestrator 不吞执行事件持久化失败")


def test_request_id_idempotency() -> None:
    async def run() -> None:
        orch = FakeOrch()
        orch.gate = asyncio.Event()
        bus = make_bus(orch)
        running = await bus.submit("第一条", request_id="r-run")
        queued = await bus.submit("第二条", request_id="r-queue")
        # dispatch 独立 task 让单命令可精确取消；等它真正进入 FakeOrch。
        for _ in range(10):
            if orch.calls:
                break
            await asyncio.sleep(0)
        # running 状态重复提交：返回原记录，不重复入队
        dup = await bus.submit("篡改内容", request_id="r-run")
        assert dup.command_id == running.command_id
        assert dup.message == "第一条" and dup.status is CommandStatus.RUNNING
        # queued 状态重复提交
        dup = await bus.submit("篡改内容", request_id="r-queue")
        assert dup.command_id == queued.command_id
        assert dup.message == "第二条" and dup.status is CommandStatus.QUEUED
        assert len(orch.calls) == 1
        orch.gate.set()
        await bus.wait(running.command_id, timeout=5)
        await bus.wait(queued.command_id, timeout=5)
        # terminal 状态重复提交：仍返回原记录
        dup = await bus.submit("又篡改", request_id="r-run")
        assert dup.command_id == running.command_id
        assert dup.status is CommandStatus.COMPLETED
        assert len(orch.calls) == 2  # 全程只真正 dispatch 了两次
        await bus.aclose()

    asyncio.run(run())
    print("ok  request_id 三状态（queued/running/terminal）永久幂等")


def test_event_sink_forwarding() -> None:
    async def run() -> None:
        orch = FakeOrch()
        events = []
        bus = make_bus(orch, event_sink=lambda n, e: events.append((n, e)))
        snap = await bus.submit("hello")
        await bus.wait(snap.command_id, timeout=5)
        # event_sink 原样收到 (name, event)
        assert events == [("fake", ("text", "hello")),
                          ("fake", ("done", "hello"))]
        # 无 sink 也能正常跑
        bus2 = make_bus(FakeOrch())
        snap2 = await bus2.submit("no sink")
        await bus2.wait(snap2.command_id, timeout=5)
        await bus.aclose()
        await bus2.aclose()

    asyncio.run(run())
    print("ok  event_sink 原样转发")


def test_close_cancels_active_and_queued() -> None:
    async def run() -> None:
        orch = FakeOrch()
        orch.gate = asyncio.Event()
        bus = make_bus(orch)
        active = await bus.submit("active")
        queued = await bus.submit("queued")
        await asyncio.sleep(0)  # worker 取走 active
        assert bus.get(active.command_id).status is CommandStatus.RUNNING
        assert bus.get(queued.command_id).status is CommandStatus.QUEUED
        # 一个正在 wait queued 的等待者，close 后应被唤醒
        waiter = asyncio.create_task(bus.wait(queued.command_id, timeout=30))
        await asyncio.sleep(0)
        await bus.aclose()
        await bus.aclose()  # 幂等
        result = await waiter
        assert result["status"] == "cancelled" and result["timed_out"] is False
        # active 与 queued 都 terminal cancelled，finished_at 补齐
        a, q = bus.get(active.command_id), bus.get(queued.command_id)
        assert a.status is CommandStatus.CANCELLED and a.finished_at is not None
        assert q.status is CommandStatus.CANCELLED and q.finished_at is not None
        assert a.started_at is not None      # active 曾开始
        assert q.started_at is None          # queued 从未开始
        # 无 task 泄漏
        assert bus._worker_task is None
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()]
        assert pending == [], f"残留 task：{pending}"
        # bus 不拥有 orch：aclose 不关闭 orch
        assert orch.closed is False
        await orch.aclose()

    asyncio.run(run())
    print("ok  aclose 取消 active+queued（timestamps + notify + 幂等 + 无泄漏）")


def test_closed_rejects() -> None:
    async def run() -> None:
        orch = FakeOrch()
        bus = make_bus(orch)
        snap = await bus.submit("先完成")
        await bus.wait(snap.command_id, timeout=5)
        await bus.aclose()
        with _Raises(CommandBusClosedError):
            await bus.submit("拒绝")
        with _Raises(CommandBusClosedError):
            bus.start()
        # 已 terminal 的记录 close 后仍可查可等
        assert bus.get(snap.command_id).status is CommandStatus.COMPLETED
        assert (await bus.wait(snap.command_id, timeout=0))["status"] == "completed"

    asyncio.run(run())
    print("ok  close 后拒绝 submit/start，历史记录仍可查")


def test_eviction_policy() -> None:
    async def run() -> None:
        # 无 request_id：最老 terminal 清理到 max_commands
        orch = FakeOrch()
        bus = make_bus(orch, max_commands=2)
        ids = []
        for i in range(4):
            snap = await bus.submit(f"m{i}")
            await bus.wait(snap.command_id, timeout=5)
            ids.append(snap.command_id)
        assert len(bus._commands) == 2
        with _Raises(CommandNotFoundError):
            bus.get(ids[0])
        with _Raises(CommandNotFoundError):
            bus.get(ids[1])
        assert bus.get(ids[2]).status is CommandStatus.COMPLETED
        assert bus.get(ids[3]).status is CommandStatus.COMPLETED
        await bus.aclose()

        # 带 request_id 的 terminal 永不清理：占满容量时新命令被拒绝（硬上限）
        orch2 = FakeOrch()
        bus2 = make_bus(orch2, max_commands=1)
        snap = await bus2.submit("k0", request_id="keep-0")
        await bus2.wait(snap.command_id, timeout=5)
        assert len(bus2._commands) == 1
        # keyed terminal 不可清理，容量满 → CommandCapacityError，绝不超限
        with _Raises(CommandCapacityError):
            await bus2.submit("k1", request_id="keep-1")
        with _Raises(CommandCapacityError):
            await bus2.submit("k1")  # 无 request_id 同样拒绝
        assert len(bus2._commands) == 1
        # 满容量下已有 request_id 仍幂等返回原记录
        dup = await bus2.submit("又篡改", request_id="keep-0")
        assert dup.command_id == snap.command_id
        assert dup.status is CommandStatus.COMPLETED
        assert len(orch2.calls) == 1
        await bus2.aclose()

    asyncio.run(run())
    print("ok  清理策略 + 硬上限（keyed terminal 占满拒绝，满容量下 request_id 幂等）")


def test_hard_capacity_active_queued() -> None:
    """active/queued 占满容量时拒绝；terminal 后可清理记录腾出空间。"""

    async def run() -> None:
        orch = FakeOrch()
        orch.gate = asyncio.Event()
        bus = make_bus(orch, max_commands=2)
        running = await bus.submit("m0")
        queued = await bus.submit("m1")
        await asyncio.sleep(0)  # worker 取走 m0
        assert bus.get(running.command_id).status is CommandStatus.RUNNING
        # 两条都不可清理（running + queued），容量满 → 拒绝
        with _Raises(CommandCapacityError):
            await bus.submit("m2")
        assert len(bus._commands) == 2
        # 放行执行完：最老无 request_id terminal 被清理，容量腾出
        orch.gate.set()
        await bus.wait(running.command_id, timeout=5)
        await bus.wait(queued.command_id, timeout=5)
        snap = await bus.submit("m2")
        assert (await bus.wait(snap.command_id, timeout=5))["status"] == "completed"
        assert len(bus._commands) <= 2
        await bus.aclose()

    asyncio.run(run())
    print("ok  硬上限（active/queued 占满拒绝；terminal 清理后恢复）")


def test_cancel_window_and_worker_restart() -> None:
    """精确制造 queue.get() 后、RUNNING transition 前的取消窗口。"""

    async def run() -> None:
        orch = FakeOrch()
        bus = make_bus(orch)
        # 占住 cond 锁：worker 的 RUNNING transition 必被卡在锁外，
        # 即精确停在 queue.get() 之后、transition 之前的窗口
        await bus._cond.acquire()
        try:
            snap = await bus.submit("窗口")
            queued = await bus.submit("排队")
            worker = bus._worker_task
            for _ in range(1000):  # 等 worker 取走命令并卡在窗口
                if bus._active is not None:
                    break
                await asyncio.sleep(0)
            assert bus._active is not None, "worker 未进入窗口"
            # 窗口内：命令已取走但还没 transition，仍是 queued
            assert bus.get(snap.command_id).status is CommandStatus.QUEUED
            worker.cancel()
        finally:
            bus._cond.release()
        with _Raises(asyncio.CancelledError):
            await worker
        # active（窗口中被取消）与剩余 queued 都转 cancelled，无 ghost
        for s, started in ((snap, False), (queued, False)):
            final = bus.get(s.command_id)
            assert final.status is CommandStatus.CANCELLED
            assert final.finished_at is not None
            assert (final.started_at is not None) is started
        assert bus._active is None
        # worker 死后 submit 拒绝：不能接受无消费者命令
        with _Raises(CommandBusError):
            await bus.submit("没人消费")
        # 显式 start() 可恢复
        bus.start()
        snap2 = await bus.submit("复活")
        assert (await bus.wait(snap2.command_id, timeout=5))["status"] == "completed"
        assert orch.calls[-1][0] == "复活"
        # 正常 aclose 后 start/submit 仍拒绝
        await bus.aclose()
        with _Raises(CommandBusClosedError):
            bus.start()
        with _Raises(CommandBusClosedError):
            await bus.submit("拒绝")

    asyncio.run(run())
    print("ok  取消窗口（active+queued 兜底 cancelled）+ worker 死后拒绝/start 恢复")


# ---- 集成：CommandBus ↔ 真实 Orchestrator / TUI ----


class FakeAgentAdapter:
    """假工人 agent（JSONL 语义）：记录 prompt，吐固定回复。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.prompts: list[str] = []

    async def stream(self, prompt: str, workdir: str):
        self.prompts.append(prompt)
        yield AgentEvent("text", f"{self.name} 回复{len(self.prompts)}")
        yield AgentEvent("done")

    async def aclose(self) -> None:
        pass


class FakeHostAdapter(FakeAgentAdapter):
    """假主持人：decide 固定路由到 kimi。"""

    def __init__(self) -> None:
        super().__init__("host")

    async def decide(
        self, transcript: str, workdir: str, on_event=None, *, choices=None,
    ):
        return HostDecision(["kimi"], "测试路由")


def make_fake_orch():
    """真实 Orchestrator（内存模式）+ 全 fake agent。"""
    from orchestrator import Orchestrator

    orch = Orchestrator(workdir=".", persistent=False)
    for n in ("kimi", "opencode", "codex"):
        orch.adapters[n] = FakeAgentAdapter(n)
    orch.host = FakeHostAdapter()
    orch.adapters["host"] = orch.host
    return orch


def test_bus_orchestrator_integration() -> None:
    """real Orchestrator + fake agent 经 CommandBus 连发两条：
    FIFO 串行；每轮 user/agent timeline 带同一个 command_id，两轮不同。"""

    async def run() -> None:
        orch = make_fake_orch()
        events = []
        bus = CommandBus(orch, lambda n, e: events.append((n, e)))
        bus.start()
        s1 = await bus.submit("@kimi 第一条")
        s2 = await bus.submit("@kimi 第二条")
        r1 = await bus.wait(s1.command_id, timeout=5)
        r2 = await bus.wait(s2.command_id, timeout=5)
        assert r1["status"] == r2["status"] == "completed"
        # FIFO：第一轮 user+agent 完整落 timeline 后才第二轮
        assert [(m.speaker, m.command_id) for m in orch.history] == [
            ("user", s1.command_id), ("kimi", s1.command_id),
            ("user", s2.command_id), ("kimi", s2.command_id)]
        assert s1.command_id != s2.command_id
        # user committed 事件 meta：保留 seq，加入 command_id
        committed = [e for _, e in events if e.kind == "committed"]
        assert [e.meta["command_id"] for e in committed] == [
            s1.command_id, s2.command_id]
        assert all(isinstance(e.meta["seq"], int) and e.meta["seq"] > 0
                   for e in committed)
        # 串行证据：第二轮 prompt 能看到第一轮的回复
        kimi = orch.adapters["kimi"]
        assert len(kimi.prompts) == 2
        assert "kimi 回复1" not in kimi.prompts[0]
        assert "kimi 回复1" in kimi.prompts[1]
        await bus.aclose()
        await orch.aclose()

    asyncio.run(run())
    print("ok  集成：Orchestrator 经 bus FIFO + 每轮 timeline 带对应 command_id")


def test_worker_failure_marks_command_failed_after_fanout() -> None:
    """任一 worker 失败使 command failed，但其他 fan-out 仍完整收尾。"""
    class PartialFailAdapter(FakeAgentAdapter):
        async def stream(self, prompt: str, workdir: str):
            self.prompts.append(prompt)
            yield AgentEvent("text", "只完成了一部分")
            raise RuntimeError("worker boom")

    async def run() -> None:
        orch = make_fake_orch()
        orch.adapters["kimi"] = PartialFailAdapter("kimi")
        bus = CommandBus(orch)
        bus.start()

        submitted = await bus.submit("@kimi @opencode 并行处理")
        result = await bus.wait(submitted.command_id, timeout=5)
        assert result["status"] == "failed"
        assert "kimi" in result["error"] and "worker boom" in result["error"]

        records = [
            message for message in orch.history
            if message.command_id == submitted.command_id
        ]
        assert any(message.speaker == "opencode" for message in records)
        kimi_reply = next(
            message.text for message in records if message.speaker == "kimi")
        assert "只完成了一部分" in kimi_reply
        assert "调用失败" in kimi_reply

        await bus.aclose()
        await orch.aclose()

    asyncio.run(run())
    print("ok  worker 失败 → command failed（fan-out 其余目标仍收尾）")


def test_tui_bus_integration() -> None:
    """Textual pilot 快速提交两条：都经 app.bus 串行执行并显示；
    退出后 bus 与 orch 都关闭。"""
    from textual.widgets import Input, RichLog
    from main import ChatApp

    async def run() -> None:
        orch = make_fake_orch()
        app = ChatApp(workdir=".", orchestrator=orch)
        assert isinstance(app.bus, CommandBus)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(Input)
            box.value = "@kimi 甲"
            await pilot.press("enter")
            box.value = "@kimi 乙"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            kimi = orch.adapters["kimi"]
            # 两条都经 app.bus：user/agent timeline 带 command_id，
            # 且对应命令在 bus 里 completed
            assert [(m.speaker,) for m in orch.history] == [
                ("user",), ("kimi",), ("user",), ("kimi",)]
            ids = [m.command_id for m in orch.history]
            assert all(cid is not None for cid in ids)
            assert ids[0] == ids[1] and ids[2] == ids[3] and ids[0] != ids[2]
            for cid in (ids[0], ids[2]):
                assert app.bus.get(cid).status is CommandStatus.COMPLETED
            # 串行执行：第二轮 prompt 含第一轮回复
            assert len(kimi.prompts) == 2
            assert "kimi 回复1" in kimi.prompts[1]
            # 都显示了
            text = "\n".join(str(line.text)
                             for line in app.query_one(RichLog).lines)
            assert "[user] @kimi 甲" in text and "[user] @kimi 乙" in text
            assert "[kimi] kimi 回复1" in text and "[kimi] kimi 回复2" in text
        # 退出后：bus 与 orch 都关闭（bus 不拥有 orch，但 TUI 负责两个都关）
        assert app.bus._closed is True
        assert orch._closed is True

    asyncio.run(run())
    print("ok  集成：TUI 经 bus 快速连发两条（FIFO + 显示 + 退出双关闭）")


if __name__ == "__main__":
    test_validation()
    test_fifo_serial_and_timestamps()
    test_dispatch_failure()
    test_wait_timeout_and_terminal_immediate()
    test_active_cancel_heartbeat_and_worker_survives()
    test_event_persistence_failure_fails_command_not_worker()
    test_fanout_partial_events_keep_agent_identity()
    test_workflow_steering_uses_active_owner_and_persists_event()
    test_workflow_steering_persistence_failure_does_not_commit()
    test_runtime_interjection_is_durable_before_transport_acceptance()
    test_runtime_interjection_persistence_failure_never_calls_transport()
    test_duplicate_tool_updates_are_forwarded_and_persisted_once()
    test_activity_only_events_refresh_silence_without_becoming_visible()
    test_status_dedup_keeps_visible_phase_changes()
    test_orchestrator_does_not_swallow_event_persistence_failure()
    test_request_id_idempotency()
    test_event_sink_forwarding()
    test_close_cancels_active_and_queued()
    test_closed_rejects()
    test_eviction_policy()
    test_hard_capacity_active_queued()
    test_cancel_window_and_worker_restart()
    test_bus_orchestrator_integration()
    test_worker_failure_marks_command_failed_after_fanout()
    test_tui_bus_integration()
    print("\n全部通过")

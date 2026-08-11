"""M4.7 SessionManager 公开生命周期验收。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import AgentEvent, ExecutionMode
from orchestrator import AgentSpec, Orchestrator
from session_manager import SessionManager, SessionNotice
from storage.store import RoomStore


class BlockingAdapter:
    name = "worker"
    session_id = None
    stateful_session = False
    started: dict[str, asyncio.Event] = {}
    release: dict[str, asyncio.Event] = {}
    started_count = 0
    all_started: asyncio.Event | None = None

    async def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ):
        del prompt, execution_mode
        type(self).started_count += 1
        if type(self).started_count >= 2 and type(self).all_started is not None:
            type(self).all_started.set()
        self.started.setdefault(workdir, asyncio.Event()).set()
        await self.release.setdefault(workdir, asyncio.Event()).wait()
        yield AgentEvent("text", f"完成：{Path(workdir).name}")


class ProjectBlockingAdapter:
    name = "worker"
    session_id = None
    stateful_session = False
    started: set[str] = set()
    changed: asyncio.Event | None = None
    releases: dict[str, asyncio.Event] = {}

    async def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ):
        del prompt, execution_mode
        type(self).started.add(workdir)
        if type(self).changed is not None:
            type(self).changed.set()
        await type(self).releases.setdefault(workdir, asyncio.Event()).wait()
        yield AgentEvent("text", f"完成：{Path(workdir).name}")


class PermissionAdapter:
    name = "worker"
    session_id = None
    stateful_session = False
    requested: asyncio.Event
    release: asyncio.Event

    def __init__(self) -> None:
        self._handler = None

    def set_permission_handler(self, handler) -> None:
        self._handler = handler

    async def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ):
        del prompt, workdir, execution_mode
        assert self._handler is not None
        outcome = await self._handler(self.name, {
            "toolCall": {"title": "写入临时结果"},
            "options": [
                {"optionId": "allow_once", "name": "允许一次"},
                {"optionId": "reject", "name": "拒绝"},
            ],
        })
        yield AgentEvent("text", f"权限结果：{outcome['outcome']}")


class ImmediateAdapter:
    name = "worker"
    session_id = None
    stateful_session = False

    async def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ):
        del prompt, workdir, execution_mode
        yield AgentEvent("text", "完成")


class ExplodingControl:
    async def aclose(self) -> None:
        raise OSError("control close failed")


class FailingStartControl:
    def __init__(self) -> None:
        self.closed = False

    async def start(self) -> None:
        raise OSError("control start failed")

    async def aclose(self) -> None:
        self.closed = True


def test_switch_keeps_background_commands_running_and_isolated() -> None:
    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            resolved = str(workdir.resolve())
            BlockingAdapter.started = {resolved: asyncio.Event()}
            BlockingAdapter.release = {resolved: asyncio.Event()}
            BlockingAdapter.started_count = 0
            BlockingAdapter.all_started = asyncio.Event()
            events: list[tuple[str, str, str]] = []
            manager = SessionManager(
                workdir,
                state_root=root / "state",
                specs=(AgentSpec("worker", "jsonl", BlockingAdapter),),
                event_sink=lambda sid, name, event: events.append(
                    (sid, name, event.kind)
                ),
                enable_control=False,
            )
            await manager.start()
            first_id = manager.active_session_id
            first_command = await manager.submit("@worker 第一会话")
            await asyncio.wait_for(
                BlockingAdapter.started[resolved].wait(), 1
            )

            second = await manager.create_session(workdir)
            second_id = second.summary.room_id
            second_command = await manager.submit("@worker 第二会话")
            await asyncio.wait_for(BlockingAdapter.all_started.wait(), 1)

            assert first_id != second_id
            assert manager.snapshot(first_id).status == "running"
            assert manager.snapshot(second_id).status == "running"
            await manager.activate(first_id)
            assert manager.active_session_id == first_id
            assert manager.snapshot(second_id).status == "running"

            BlockingAdapter.release[resolved].set()
            first_result, second_result = await asyncio.gather(
                manager.wait(first_command),
                manager.wait(second_command),
            )
            assert first_result["status"] == "completed"
            assert second_result["status"] == "completed"
            first_history = [m.text for m in manager.snapshot(first_id).history]
            second_history = [m.text for m in manager.snapshot(second_id).history]
            assert "@worker 第一会话" in first_history
            assert "@worker 第二会话" not in first_history
            assert "@worker 第二会话" in second_history
            assert "@worker 第一会话" not in second_history
            assert {sid for sid, _, _ in events} == {first_id, second_id}
            await manager.aclose()

    asyncio.run(run())


def test_each_session_keeps_an_independent_in_process_draft() -> None:
    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            manager = SessionManager(
                workdir,
                state_root=root / "state",
                specs=(AgentSpec("worker", "jsonl", BlockingAdapter),),
                enable_control=False,
            )
            await manager.start()
            first_id = manager.active_session_id
            manager.save_draft("第一会话草稿", cursor_position=4)
            second = await manager.create_session(workdir)
            manager.save_draft("第二会话 [图片 1]", cursor_position=8)

            await manager.activate(first_id)
            first = manager.snapshot()
            assert first.draft == "第一会话草稿"
            assert first.cursor_position == 4
            await manager.activate(second.summary.room_id)
            other = manager.snapshot()
            assert other.draft == "第二会话 [图片 1]"
            assert other.cursor_position == 8
            await manager.aclose()

    asyncio.run(run())


def test_manager_renames_loaded_session_and_guards_permanent_delete() -> None:
    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            manager = SessionManager(
                workdir,
                state_root=root / "state",
                specs=(AgentSpec("worker", "jsonl", BlockingAdapter),),
                enable_control=False,
            )
            await manager.start()
            first_id = manager.active_session_id
            second = await manager.create_session(workdir)
            second_id = second.summary.room_id

            renamed = await manager.rename_session(second_id, "临时排障")
            assert renamed.summary.title == "临时排障"
            assert renamed.summary.room_id == second_id
            try:
                await manager.delete_session(second_id, confirmation="临时排障")
            except ValueError as exc:
                assert "当前会话" in str(exc)
            else:
                raise AssertionError("当前会话不得永久删除")

            await manager.activate(first_id)
            await manager.delete_session(second_id, confirmation="临时排障")
            assert second_id not in {
                item.summary.room_id for item in manager.list_sessions()
            }
            await manager.aclose()

    asyncio.run(run())


def test_idle_reap_closes_only_inactive_background_runtime() -> None:
    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            now = [0.0]
            manager = SessionManager(
                workdir,
                state_root=root / "state",
                specs=(AgentSpec("worker", "jsonl", BlockingAdapter),),
                idle_timeout=10,
                clock=lambda: now[0],
                enable_control=False,
            )
            await manager.start()
            first_id = manager.active_session_id
            second = await manager.create_session(workdir)
            second_id = second.summary.room_id
            await manager.activate(first_id)

            now[0] = 11.0
            reaped = await manager.reap_idle()

            assert reaped == (second_id,)
            states = {
                item.summary.room_id: item for item in manager.list_sessions()
            }
            assert states[first_id].loaded is True
            assert states[second_id].loaded is False
            await manager.aclose()

    asyncio.run(run())


def test_global_gate_limits_running_sessions_and_waiter_is_cancellable() -> None:
    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = [root / f"project-{index}" for index in range(4)]
            for project in projects:
                project.mkdir()
            ProjectBlockingAdapter.started = set()
            ProjectBlockingAdapter.changed = asyncio.Event()
            ProjectBlockingAdapter.releases = {}
            manager = SessionManager(
                projects[0],
                state_root=root / "state",
                specs=(
                    AgentSpec("worker", "jsonl", ProjectBlockingAdapter),
                ),
                max_running_sessions=3,
                enable_control=False,
            )
            await manager.start()
            commands = []
            session_ids = []
            for index, project in enumerate(projects):
                if index:
                    await manager.create_session(project)
                session_ids.append(manager.active_session_id)
                commands.append(await manager.submit(f"@worker task-{index}"))

            for _ in range(100):
                if len(ProjectBlockingAdapter.started) == 3 \
                        and manager.snapshot(session_ids[3]).status \
                        == "waiting_resource":
                    break
                ProjectBlockingAdapter.changed.clear()
                try:
                    await asyncio.wait_for(
                        ProjectBlockingAdapter.changed.wait(), 0.02
                    )
                except TimeoutError:
                    pass
            assert len(ProjectBlockingAdapter.started) == 3
            assert str(projects[3].resolve()) not in ProjectBlockingAdapter.started
            assert manager.snapshot(session_ids[3]).status == "waiting_resource"

            cancelled = await manager.cancel(session_ids[3])
            assert cancelled is not None
            fourth = await manager.wait(commands[3])
            assert fourth["status"] == "cancelled"
            for release in ProjectBlockingAdapter.releases.values():
                release.set()
            results = await asyncio.gather(
                *(manager.wait(command) for command in commands[:3])
            )
            assert {item["status"] for item in results} == {"completed"}
            await manager.aclose()

    asyncio.run(run())


def test_background_terminal_sets_unread_notice_until_activation() -> None:
    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            resolved = str(workdir.resolve())
            BlockingAdapter.started = {resolved: asyncio.Event()}
            BlockingAdapter.release = {resolved: asyncio.Event()}
            notices: list[SessionNotice] = []
            now = [0.0]
            manager = SessionManager(
                workdir,
                state_root=root / "state",
                specs=(AgentSpec("worker", "jsonl", BlockingAdapter),),
                notification_sink=notices.append,
                idle_timeout=10,
                clock=lambda: now[0],
                enable_control=False,
            )
            await manager.start()
            first_id = manager.active_session_id
            second = await manager.create_session(workdir)
            second_id = second.summary.room_id
            runtime = manager.active_runtime
            original_wait = runtime.bus.wait
            injected_timeout = False

            async def wait_with_one_watcher_timeout(
                command_id: str,
                timeout: float = 30.0,
            ) -> dict:
                nonlocal injected_timeout
                task = asyncio.current_task()
                is_watcher = task is not None and task.get_name().startswith(
                    "session-command-"
                )
                if is_watcher and not injected_timeout:
                    injected_timeout = True
                    return {
                        "command_id": command_id,
                        "status": "running",
                        "error": None,
                        "timed_out": True,
                    }
                return await original_wait(command_id, timeout=timeout)

            runtime.bus.wait = wait_with_one_watcher_timeout
            command = await manager.submit("@worker 后台任务")
            await BlockingAdapter.started[resolved].wait()
            await manager.activate(first_id)
            BlockingAdapter.release[resolved].set()
            await manager.wait(command)
            for _ in range(20):
                if notices:
                    break
                await asyncio.sleep(0)

            assert manager.snapshot(second_id).unread is True
            assert injected_timeout is True
            assert [(item.session_id, item.status) for item in notices] == [
                (second_id, "completed")
            ]
            now[0] = 11.0
            assert await manager.reap_idle() == (second_id,)
            detached = next(
                item for item in manager.list_sessions()
                if item.summary.room_id == second_id
            )
            assert detached.loaded is False
            assert detached.status == "completed"
            assert detached.unread is True
            await manager.activate(second_id)
            assert manager.snapshot(second_id).unread is False
            await manager.aclose()

    asyncio.run(run())


def test_background_permission_keeps_session_and_command_provenance() -> None:
    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            PermissionAdapter.requested = asyncio.Event()
            PermissionAdapter.release = asyncio.Event()
            callbacks: list[tuple[str, str, str]] = []

            async def permission(
                room_id: str,
                agent_name: str,
                params: dict,
            ) -> dict:
                callbacks.append((
                    room_id,
                    agent_name,
                    params["toolCall"]["title"],
                ))
                PermissionAdapter.requested.set()
                await PermissionAdapter.release.wait()
                return {"outcome": "cancelled"}

            manager = SessionManager(
                workdir,
                state_root=root / "state",
                specs=(AgentSpec("worker", "jsonl", PermissionAdapter),),
                permission_handler=permission,
                enable_control=False,
            )
            await manager.start()
            first_id = manager.active_session_id
            second = await manager.create_session(workdir)
            second_id = second.summary.room_id
            command = await manager.submit("@worker 需要权限")
            await asyncio.wait_for(PermissionAdapter.requested.wait(), 1)
            await manager.activate(first_id)

            assert callbacks == [
                (second_id, "worker", "写入临时结果")
            ]
            assert manager.snapshot(second_id).status == "waiting_permission"
            runtime = manager._runtimes[second_id]
            events = runtime.orch.store.read_events()["items"]
            assert events[-1].command_id == command.command_id
            assert events[-1].kind == "permission"
            assert events[-1].text == "等待权限：写入临时结果"

            PermissionAdapter.release.set()
            result = await manager.wait(command)
            assert result["status"] == "completed"
            settled = runtime.orch.store.read_events()["items"]
            assert any(
                item.command_id == command.command_id
                and item.kind == "permission"
                and item.text == "权限已取消或拒绝"
                for item in settled
            )
            await manager.aclose()

    asyncio.run(run())


def test_first_committed_message_derives_title_without_model_or_image_noise() -> None:
    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            manager = SessionManager(
                workdir,
                state_root=root / "state",
                specs=(AgentSpec("worker", "jsonl", ImmediateAdapter),),
                enable_control=False,
            )
            await manager.start()
            created = await manager.create_session(workdir)
            assert created.summary.title == "新会话"
            command = await manager.submit(
                "@worker [图片 1] 请检查 OpenCode 输出"
            )
            result = await manager.wait(command)
            assert result["status"] == "completed"
            assert manager.snapshot().summary.title == "请检查 OpenCode 输出"
            await manager.aclose()

    asyncio.run(run())


def test_permission_audit_failure_blocks_ui_decision_and_command() -> None:
    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            callbacks = 0

            async def permission(*_args) -> dict:
                nonlocal callbacks
                callbacks += 1
                return {"outcome": "selected", "optionId": "allow_once"}

            manager = SessionManager(
                workdir,
                state_root=root / "state",
                specs=(AgentSpec("worker", "jsonl", PermissionAdapter),),
                permission_handler=permission,
                enable_control=False,
            )
            await manager.start()
            store = manager.active_runtime.orch.store
            original_append_event = store.append_event

            def fail_permission_event(**kwargs):
                if kwargs.get("kind") == "permission":
                    raise OSError("permission audit failed")
                return original_append_event(**kwargs)

            store.append_event = fail_permission_event
            command = await manager.submit("@worker 请求权限")
            result = await manager.wait(command)
            assert result["status"] == "failed"
            assert "permission audit failed" in result["error"]
            assert callbacks == 0
            await manager.aclose()

    asyncio.run(run())


def test_close_failure_does_not_skip_bus_orchestrator_or_other_runtime() -> None:
    async def run() -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            manager = SessionManager(
                workdir,
                state_root=root / "state",
                specs=(AgentSpec("worker", "jsonl", ImmediateAdapter),),
                enable_control=False,
            )
            await manager.start()
            first_id = manager.active_session_id
            second_id = (await manager.create_session(workdir)).summary.room_id
            first_runtime = manager._runtimes[first_id]
            second_runtime = manager._runtimes[second_id]
            first_runtime.control = ExplodingControl()

            try:
                await manager.aclose()
            except BaseExceptionGroup as exc:
                runtime_group = exc.exceptions[0]
                assert isinstance(runtime_group, BaseExceptionGroup)
                assert "control close failed" in str(
                    runtime_group.exceptions[0]
                )
            else:
                raise AssertionError("control close 失败必须向调用方报告")

            assert first_runtime.bus._closed is True
            assert second_runtime.bus._closed is True
            assert first_runtime.orch._closed is True
            assert second_runtime.orch._closed is True
            assert first_runtime.orch._lease is None
            assert second_runtime.orch._lease is None

    asyncio.run(run())


def test_start_failure_rolls_back_runtime_and_retry_is_fully_started() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            store = RoomStore(
                workdir,
                state_root=root / "state",
                session_name="named",
            )
            orch = Orchestrator(
                str(workdir),
                specs=(AgentSpec("worker", "jsonl", ImmediateAdapter),),
                store=store,
                session_name="named",
            )
            manager = SessionManager(
                workdir,
                specs=orch.specs,
                initial_orchestrator=orch,
                enable_control=True,
            )
            runtime = manager.active_runtime
            failing = FailingStartControl()
            runtime.control = failing

            try:
                await manager.start()
            except OSError as exc:
                assert "control start failed" in str(exc)
            else:
                raise AssertionError("control start 失败必须回滚")

            assert manager._started is False
            assert manager._runtimes == {}
            assert runtime.bus._closed is True
            assert runtime.orch._closed is True
            assert runtime.orch._lease is None
            assert failing.closed is True

            retried = await manager.start()
            assert retried.summary.session_name == "named"
            assert manager.active_runtime.bus._closed is False
            assert manager.active_runtime.orch._lease is not None
            assert manager.active_runtime.control is not None
            assert manager.active_runtime.control._server is not None
            await manager.aclose()

    asyncio.run(run())


if __name__ == "__main__":
    test_switch_keeps_background_commands_running_and_isolated()
    test_each_session_keeps_an_independent_in_process_draft()
    test_manager_renames_loaded_session_and_guards_permanent_delete()
    test_idle_reap_closes_only_inactive_background_runtime()
    test_global_gate_limits_running_sessions_and_waiter_is_cancellable()
    test_background_terminal_sets_unread_notice_until_activation()
    test_background_permission_keeps_session_and_command_provenance()
    test_first_committed_message_derives_title_without_model_or_image_noise()
    test_permission_audit_failure_blocks_ui_decision_and_command()
    test_close_failure_does_not_skip_bus_orchestrator_or_other_runtime()
    test_start_failure_rolls_back_runtime_and_retry_is_fully_started()
    print("ok  多会话切换不取消后台任务且事件/历史隔离")

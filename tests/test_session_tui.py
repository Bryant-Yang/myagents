"""M4.7 Textual 会话选择器验收。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from main import (
    ChatApp,
    DeleteSessionScreen,
    PermissionScreen,
    RenameSessionScreen,
    SessionPickerScreen,
)
from orchestrator import AgentSpec, Orchestrator
from adapters.base import AgentEvent, ExecutionMode
from storage.store import RoomStore
from textual.widgets import Input, OptionList, RichLog, Static


def _picker_text(screen: SessionPickerScreen) -> str:
    options = screen.query_one("#session-options", OptionList)
    return "\n".join(str(option.prompt) for option in options.options)


def test_ctrl_o_opens_searchable_current_project_picker() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            state_root = root / "state"
            default = RoomStore(workdir, state_root=state_root)
            default.append("user", "默认会话里的问题")
            talk = RoomStore(
                workdir,
                state_root=state_root,
                session_name="talk",
            )
            talk.append(
                "user",
                "讨论 Qwen 本地模型 "
                "[图片附件：/Users/example/a/very/long/path/image.png]",
            )
            orch = Orchestrator(str(workdir), store=default)
            app = ChatApp(workdir=str(workdir), orchestrator=orch)

            async with app.run_test() as pilot:
                await pilot.press("ctrl+o")
                await pilot.pause()
                assert isinstance(app.screen, SessionPickerScreen)
                rendered = _picker_text(app.screen)
                assert "default" in rendered, rendered
                assert "talk" in rendered
                assert "讨论 Qwen 本地模型" in rendered
                assert "● 空闲" in rendered
                assert "当前" in rendered
                assert "[图片]" in rendered
                assert "/Users/example" not in rendered

                options = app.screen.query_one("#session-options", OptionList)
                session_options = [
                    option for option in options.options
                    if option.id in {
                        default.room_id,
                        talk.room_id,
                    }
                ]
                assert all(
                    str(option.prompt).count("\n") == 1
                    for option in session_options
                )
                assert app.screen.selected().summary.room_id == default.room_id
                selected_before = app.screen.selected().summary.room_id
                await pilot.press("down")
                assert app.screen.selected().summary.room_id != selected_before
                assert options.highlighted_option.id \
                    == app.screen.selected().summary.room_id

                search = app.screen.query_one("#session-search", Input)
                search.value = "qwen"
                await pilot.pause()
                filtered = _picker_text(app.screen)
                assert "talk" in filtered
                assert "default" not in filtered

    asyncio.run(run())


def test_picker_keeps_two_line_rows_and_scrolls_in_narrow_terminals() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            state_root = root / "state"
            first: RoomStore | None = None
            for index in range(12):
                store = RoomStore(
                    workdir,
                    state_root=state_root,
                    session_name=f"session-{index}",
                )
                store.set_session_title(
                    f"第 {index + 1} 个窄窗口会话",
                    pending=False,
                )
                store.append(
                    "user",
                    "@kimi @opencode 一段很长的预览，"
                    "不应该把会话卡片挤成第三行",
                )
                first = first or store
            assert first is not None
            orch = Orchestrator(
                str(workdir),
                store=first,
                session_name=first.session_name,
            )
            app = ChatApp(workdir=str(workdir), orchestrator=orch)

            async with app.run_test(size=(80, 28)) as pilot:
                await pilot.press("ctrl+o")
                await pilot.pause()
                options = app.screen.query_one(
                    "#session-options", OptionList
                )
                assert options.option_count == 12
                assert options.virtual_size.height == 24
                initial = options.highlighted
                assert initial is not None
                for _ in range(10):
                    await pilot.press("down")
                await pilot.pause()
                assert options.highlighted == (initial + 10) % 12
                assert options.scroll_y > 0

    asyncio.run(run())


def test_picker_switches_history_and_restores_per_session_drafts() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            state_root = root / "state"
            default = RoomStore(workdir, state_root=state_root)
            default.append("user", "默认历史")
            talk = RoomStore(
                workdir,
                state_root=state_root,
                session_name="talk",
            )
            talk.append("user", "Talk 历史")
            app = ChatApp(
                workdir=str(workdir),
                orchestrator=Orchestrator(str(workdir), store=default),
            )

            async with app.run_test() as pilot:
                composer = app.query_one("#composer", Input)
                composer.value = "默认草稿"
                composer.cursor_position = 2
                await pilot.press("ctrl+o")
                picker = app.screen
                picker.query_one("#session-search", Input).value = "talk"
                await pilot.pause()
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app.session_name == "talk"
                rendered = "\n".join(
                    str(line.text) for line in app.query_one(RichLog).lines
                )
                assert "Talk 历史" in rendered
                assert "默认历史" not in rendered

                composer = app.query_one("#composer", Input)
                composer.value = "Talk 草稿"
                await pilot.press("ctrl+o")
                app.screen.query_one("#session-search", Input).value = "default"
                await pilot.pause()
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app.session_name == "default"
                assert app.query_one("#composer", Input).value == "默认草稿"
                assert app.query_one("#composer", Input).cursor_position == 2

    asyncio.run(run())


def test_ctrl_n_creates_untitled_session_without_name_dialog() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            app = ChatApp(
                workdir=str(workdir),
                orchestrator=Orchestrator(str(workdir), store=store),
            )

            async with app.run_test() as pilot:
                app.query_one("#composer", Input).value = "原会话草稿"
                old_id = app.session_manager.active_session_id
                await pilot.press("ctrl+n")
                await app.workers.wait_for_complete()
                await pilot.pause()

                assert app.session_manager.active_session_id != old_id
                assert app.session_manager.snapshot().summary.title == "新会话"
                assert app.query_one("#composer", Input).value == ""
                assert len(app.session_manager.list_sessions()) == 2

    asyncio.run(run())


def test_yolo_mode_is_isolated_by_room_and_not_persisted() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            state_root = root / "state"
            first = RoomStore(workdir, state_root=state_root)
            second = RoomStore(
                workdir,
                state_root=state_root,
                session_name="second",
            )
            app = ChatApp(
                workdir=str(workdir),
                orchestrator=Orchestrator(str(workdir), store=first),
            )

            async with app.run_test() as pilot:
                first_id = app.session_manager.active_session_id
                app.action_toggle_yolo()
                await pilot.pause()
                assert app.auto_approve is True
                assert "YOLO" in app.title

                await app.session_manager.activate(second.room_id)
                app._bind_active_runtime()
                app._render_active_session()
                assert app.auto_approve is False
                assert "YOLO" not in app.title

                # 后台 runtime 的空闲回收不能误清当前进程内的 room 状态。
                app.session_manager._idle_timeout = 0
                await app._reap_idle_sessions()
                assert first_id not in app.session_manager._runtimes
                assert first_id in app._auto_approve_rooms

                await app.session_manager.activate(first_id)
                app._bind_active_runtime()
                app._render_active_session()
                assert app.auto_approve is True
                assert "YOLO" in app.title

            assert not app._auto_approve_rooms

            # 新 ChatApp 不从 room state 恢复 /yolo。
            reopened = ChatApp(
                workdir=str(workdir),
                orchestrator=Orchestrator(
                    str(workdir),
                    store=RoomStore(workdir, state_root=state_root),
                ),
            )
            async with reopened.run_test() as pilot:
                await pilot.pause()
                assert reopened.auto_approve is False
                assert "YOLO" not in reopened.title

    asyncio.run(run())


def test_picker_renames_and_exact_title_deletes_inactive_session() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            state_root = root / "state"
            current = RoomStore(workdir, state_root=state_root)
            other = RoomStore(
                workdir, state_root=state_root, session_name="other"
            )
            other.append("user", "需要整理的历史")
            app = ChatApp(
                workdir=str(workdir),
                orchestrator=Orchestrator(str(workdir), store=current),
            )

            async with app.run_test() as pilot:
                await pilot.press("ctrl+o")
                app.screen.query_one("#session-search", Input).value = "other"
                await pilot.pause()
                await pilot.press("f2")
                assert isinstance(app.screen, RenameSessionScreen)
                rename = app.screen.query_one("#session-title", Input)
                rename.value = "排障记录"
                await pilot.press("enter")
                await app.workers.wait_for_complete()

                renamed = app.session_manager.catalog.get_session(other.room_id)
                assert renamed.title == "排障记录"
                assert renamed.room_id == other.room_id

                await pilot.press("ctrl+o")
                app.screen.query_one("#session-search", Input).value = "排障记录"
                await pilot.pause()
                await pilot.press("ctrl+d")
                assert isinstance(app.screen, DeleteSessionScreen)
                confirm = app.screen.query_one("#delete-confirmation", Input)
                confirm.value = "排障记录"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                assert not (state_root / "rooms" / other.room_id).exists()

    asyncio.run(run())


def test_picker_can_search_all_projects_grouped_by_workdir() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            current_workdir = root / "alpha"
            other_workdir = root / "beta"
            current_workdir.mkdir()
            other_workdir.mkdir()
            state_root = root / "state"
            current = RoomStore(current_workdir, state_root=state_root)
            remote = RoomStore(other_workdir, state_root=state_root)
            remote.append("user", "跨项目检索词")
            app = ChatApp(
                workdir=str(current_workdir),
                orchestrator=Orchestrator(str(current_workdir), store=current),
            )

            async with app.run_test() as pilot:
                await pilot.press("ctrl+o")
                search = app.screen.query_one("#session-search", Input)
                search.value = "跨项目检索词"
                await pilot.pause()
                assert "没有匹配" in _picker_text(app.screen)
                await pilot.press("tab")
                await pilot.pause()
                rendered = _picker_text(app.screen)
                assert "beta" in rendered
                assert str(other_workdir.resolve()) in rendered

    asyncio.run(run())


class _BlockingAdapter:
    name = "worker"
    session_id = None
    stateful_session = False
    started: asyncio.Event
    release: asyncio.Event

    async def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ):
        del prompt, workdir, execution_mode
        type(self).started.set()
        await type(self).release.wait()
        yield AgentEvent("text", "后台完成")


class _PermissionBlockingAdapter:
    name = "worker"
    session_id = None
    stateful_session = False
    started: asyncio.Event
    request_now: asyncio.Event

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
        type(self).started.set()
        await type(self).request_now.wait()
        outcome = await self._handler(self.name, {
            "toolCall": {"title": "后台写入请求"},
            "options": [
                {"optionId": "allow_once", "name": "允许一次"},
                {"optionId": "reject", "name": "拒绝"},
            ],
        })
        yield AgentEvent("text", outcome["outcome"])


def test_ui_switch_does_not_make_runner_wait_on_the_new_session_bus() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            _BlockingAdapter.started = asyncio.Event()
            _BlockingAdapter.release = asyncio.Event()
            orch = Orchestrator(
                str(workdir),
                specs=(AgentSpec("worker", "jsonl", _BlockingAdapter),),
                store=store,
            )
            app = ChatApp(workdir=str(workdir), orchestrator=orch)

            async with app.run_test() as pilot:
                composer = app.query_one("#composer", Input)
                composer.value = "@worker 后台任务"
                await pilot.press("enter")
                await asyncio.wait_for(_BlockingAdapter.started.wait(), 1)
                first_id = app.session_manager.active_session_id

                await pilot.press("ctrl+n")
                for _ in range(100):
                    if app.session_manager.active_session_id != first_id:
                        break
                    await asyncio.sleep(0.01)
                second_id = app.session_manager.active_session_id
                assert first_id != second_id

                await app.session_manager.activate(first_id)
                app._bind_active_runtime()
                app._render_active_session()
                rendered = "\n".join(
                    str(line.text) for line in app.query_one(RichLog).lines
                )
                status = str(app.query_one("#task-status", Static).content)
                assert "上次任务" not in rendered
                assert "已中断" not in rendered
                assert "运行" in status

                await app.session_manager.activate(second_id)
                app._bind_active_runtime()
                app._render_active_session()
                _BlockingAdapter.release.set()
                for _ in range(100):
                    if app.session_manager.snapshot(first_id).status == "completed":
                        break
                    await asyncio.sleep(0.01)
                assert app.session_manager.snapshot(first_id).status == "completed"
                assert app.session_manager.snapshot(first_id).unread is True

    asyncio.run(run())


def test_background_external_cancel_resolves_its_permission_future() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            _PermissionBlockingAdapter.started = asyncio.Event()
            _PermissionBlockingAdapter.request_now = asyncio.Event()
            orch = Orchestrator(
                str(workdir),
                specs=(AgentSpec(
                    "worker", "jsonl", _PermissionBlockingAdapter
                ),),
                store=store,
            )
            app = ChatApp(workdir=str(workdir), orchestrator=orch)

            async with app.run_test() as pilot:
                manager = app.session_manager
                first_id = manager.active_session_id
                second_id = (await manager.create_session(workdir)).summary.room_id
                command = await manager.submit("@worker 后台权限")
                await asyncio.wait_for(
                    _PermissionBlockingAdapter.started.wait(), 1
                )
                await manager.activate(first_id)
                app._bind_active_runtime()
                app._render_active_session()

                _PermissionBlockingAdapter.request_now.set()
                for _ in range(100):
                    if any(
                        isinstance(screen, PermissionScreen)
                        for screen in app.screen_stack
                    ):
                        break
                    await asyncio.sleep(0.01)
                assert any(
                    isinstance(screen, PermissionScreen)
                    and screen._session_id == second_id
                    for screen in app.screen_stack
                )

                result = await asyncio.wait_for(
                    manager._runtimes[second_id].bus.cancel(
                        command.command_id
                    ),
                    2,
                )
                await pilot.pause()
                assert result.status.value == "cancelled"
                assert not app._permission_futures
                assert not any(
                    isinstance(screen, PermissionScreen)
                    for screen in app.screen_stack
                )

    asyncio.run(run())


if __name__ == "__main__":
    test_ctrl_o_opens_searchable_current_project_picker()
    test_picker_keeps_two_line_rows_and_scrolls_in_narrow_terminals()
    test_picker_switches_history_and_restores_per_session_drafts()
    test_ctrl_n_creates_untitled_session_without_name_dialog()
    test_yolo_mode_is_isolated_by_room_and_not_persisted()
    test_picker_renames_and_exact_title_deletes_inactive_session()
    test_picker_can_search_all_projects_grouped_by_workdir()
    test_ui_switch_does_not_make_runner_wait_on_the_new_session_bus()
    test_background_external_cancel_resolves_its_permission_future()
    print("ok  Ctrl+O 打开可搜索的当前项目会话选择器")

"""聊天室执行活动折叠与展开验收。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from adapters.base import AgentEvent
from control import ControlClient
from main import ChatApp
from orchestrator import AgentSpec, Orchestrator
from storage.store import RoomStore
from test_basic import make_orch
from textual.widgets import RichLog
from tui_activity import ActivityFeed


def test_activity_feed_coalesces_progress_and_tool_updates() -> None:
    feed = ActivityFeed()
    feed.begin("cmd-activity")
    assert feed.record_status(
        "cmd-activity", "kimi", "正在分析", state="running"
    )
    assert feed.record_status(
        "cmd-activity", "kimi", "仍在运行（已等待 10 秒）",
        state="running", heartbeat=True,
    )

    assert feed.record_tool(
        "cmd-activity", "kimi", "tool-1", "检查 JavaScript",
        status="in_progress", detail="node --check demo.js",
    )
    assert not feed.record_tool(
        "cmd-activity", "kimi", "tool-1", "检查 JavaScript",
        status="in_progress", detail="node --check demo.js",
    )
    assert feed.record_tool(
        "cmd-activity", "kimi", "tool-1", "检查 JavaScript",
        status="completed", detail="node --check demo.js",
    )
    feed.set_command_state(
        "cmd-activity", "completed", elapsed_seconds=5.4)

    collapsed = feed.render("cmd-activity", expanded=False)
    assert "任务 cmd-acti" in collapsed
    assert "已结束" in collapsed
    assert "1 个工具" in collapsed
    assert "响应耗时 5.4秒" in collapsed
    assert "node --check demo.js" not in collapsed
    assert "/details 展开" in collapsed

    expanded = feed.render("cmd-activity", expanded=True)
    assert "kimi · 检查 JavaScript · 已完成" in expanded
    assert "node --check demo.js" in expanded
    assert "仍在运行（已等待 10 秒）" in expanded
    assert "/details 收起" in expanded

    feed.set_command_state(
        "cmd-activity", "completed", elapsed_seconds=999.0)
    assert "响应耗时 5.4秒" in feed.render(
        "cmd-activity", expanded=False)

    migrating = ActivityFeed()
    migrating.begin("cmd-migrate")
    migrating.record_tool(
        "cmd-migrate",
        "kimi",
        "检查 JavaScript",
        "检查 JavaScript",
        identity_is_fallback=True,
    )
    migrating.record_tool(
        "cmd-migrate", "kimi", "tool-late", "检查 JavaScript",
        status="completed",
    )
    assert "1 个工具" in migrating.render("cmd-migrate", expanded=False)
    migrating.record_tool(
        "cmd-migrate", "kimi", "tool-second", "检查 JavaScript",
        status="completed",
    )
    assert "2 个工具" in migrating.render("cmd-migrate", expanded=False)

    opaque = ActivityFeed()
    opaque.begin("cmd-opaque")
    opaque.record_tool(
        "cmd-opaque", "kimi", "Build", "Build", status="completed")
    opaque.record_tool(
        "cmd-opaque", "kimi", "formal-2", "Build", status="completed")
    assert "2 个工具" in opaque.render("cmd-opaque", expanded=False)


def test_activity_feed_tracks_latest_agent_and_bounds_terminal_details() -> None:
    feed = ActivityFeed(max_terminal_cards=2, max_tools_per_card=2)
    feed.begin("cmd-focus")
    feed.record_status(
        "cmd-focus", "reviewer", "等待审查", state="queued")
    feed.record_status(
        "cmd-focus", "writer", "等待实现", state="queued")
    feed.record_status(
        "cmd-focus", "reviewer", "正在审查", state="running")
    assert "reviewer：正在审查" in feed.render(
        "cmd-focus", expanded=False)

    for index in range(3):
        feed.record_tool(
            "cmd-focus",
            "reviewer",
            f"tool-{index}",
            f"工具 {index}",
            status="completed",
            detail=f"command-{index}",
        )
    bounded = feed.render("cmd-focus", expanded=True)
    assert "≥2 个工具" in bounded
    assert "command-0" not in bounded
    assert "仅保留最近 2 个工具" in bounded

    truthful = ActivityFeed(max_tools_per_card=2)
    truthful.begin("cmd-truth")
    truthful.record_tool(
        "cmd-truth", "worker", "failed", "首次工具", status="failed")
    truthful.record_tool(
        "cmd-truth", "worker", "ok-1", "后续工具一", status="completed")
    truthful.record_tool(
        "cmd-truth", "worker", "ok-2", "后续工具二", status="completed")
    truthful_summary = truthful.render("cmd-truth", expanded=True)
    assert "≥2 个工具（失败）" in truthful_summary
    assert "历史工具异常 · 失败" in truthful_summary

    feed.set_command_state("cmd-focus", "completed")
    for command_id in ("cmd-two", "cmd-three"):
        feed.begin(command_id)
        feed.set_command_state(command_id, "completed")
    assert feed.command_ids() == ("cmd-two", "cmd-three")
    evicted = feed.take_evicted()
    assert evicted and evicted[0][0] == "cmd-focus"
    assert "活动已归档" in evicted[0][1]

    hard_bound = ActivityFeed(max_terminal_cards=2)
    for index in range(10_000):
        command_id = f"cmd-{index}"
        hard_bound.begin(command_id)
        hard_bound.set_command_state(command_id, "completed")
    assert len(hard_bound.command_ids()) == 2
    assert len(hard_bound.take_evicted()) <= 2


def test_tui_keeps_one_activity_card_and_primary_messages() -> None:
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
            command_id = "cmd-visible"
            app._on_agent_event(
                "user",
                AgentEvent(
                    "committed", "@kimi 检查项目",
                    {"command_id": command_id},
                ),
            )
            for seconds in (10, 20, 30):
                app._on_agent_event(
                    "system",
                    AgentEvent(
                        "status",
                        f"kimi 仍在运行（已等待 {seconds} 秒）",
                        {
                            "command_id": command_id,
                            "heartbeat": True,
                            "phase": "kimi",
                        },
                    ),
                )
            tool_meta = {
                "command_id": command_id,
                "tool_call_id": "tool-1",
                "command": "API_KEY=supersecret python -m unittest",
            }
            for status in ("in_progress", "in_progress", "completed"):
                app._on_agent_event(
                    "kimi",
                    AgentEvent(
                        "tool", "运行测试", {**tool_meta, "status": status}
                    ),
                )
            app._on_agent_event(
                "kimi", AgentEvent("text", "测试已经通过。", tool_meta)
            )
            app._on_agent_event(
                "kimi", AgentEvent("done", meta={"command_id": command_id})
            )
            await pilot.pause()

            lines = [str(line.text) for line in app.query_one(RichLog).lines]
            activity = [line for line in lines if line.startswith("[activity] ")]
            assert len(activity) == 1, activity
            collapsed = "\n".join(lines)
            assert "1 个工具" in collapsed.replace("\n", "")
            assert "python -m unittest" not in collapsed
            assert "[user] @kimi 检查项目" in lines
            assert "[kimi] 测试已经通过。" in lines
            assert len([
                line for line in lines if "仍在运行（已等待" in line
            ]) == 0

            app.action_toggle_details()
            await pilot.pause()
            expanded = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines
            )
            assert "python -m unittest" in expanded
            assert "API_KEY=[已隐藏]" in expanded
            assert "supersecret" not in expanded
            assert "kimi 仍在运行（已等待 30 秒）" in expanded
            assert len([
                line for line in app.query_one(RichLog).lines
                if str(line.text).startswith("[activity] ")
            ]) == 1

    asyncio.run(run())


def test_tui_failure_stays_visible_outside_activity_details() -> None:
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
            command_id = "cmd-failed"
            app._on_agent_event(
                "user",
                AgentEvent(
                    "committed", "@kimi 执行危险操作",
                    {"command_id": command_id},
                ),
            )
            app._on_agent_event(
                "kimi",
                AgentEvent("error", "权限被拒绝", {"command_id": command_id}),
            )
            app._set_command_status(command_id, "failed", "权限被拒绝")
            await pilot.pause()

            lines = [str(line.text) for line in app.query_one(RichLog).lines]
            assert "[kimi] 出错：权限被拒绝" in lines
            activity = [line for line in lines if line.startswith("[activity] ")]
            assert len(activity) == 1
            assert "失败" in "".join(lines)

    asyncio.run(run())


def test_details_toggles_only_the_latest_activity_card() -> None:
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
            for command_id, title, command in (
                ("cmd-first", "第一项检查", "python first.py"),
                ("cmd-second", "第二项检查", "python second.py"),
            ):
                app._on_agent_event(
                    "user",
                    AgentEvent(
                        "committed",
                        f"@kimi {title}",
                        {"command_id": command_id},
                    ),
                )
                app._on_agent_event(
                    "kimi",
                    AgentEvent(
                        "tool",
                        title,
                        {
                            "command_id": command_id,
                            "tool_call_id": f"tool-{command_id}",
                            "status": "completed",
                            "command": command,
                        },
                    ),
                )
                app._on_agent_event(
                    "kimi",
                    AgentEvent("done", meta={"command_id": command_id}),
                )

            app.action_toggle_details()
            await pilot.pause()
            expanded = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines
            )
            assert "python second.py" in expanded
            assert "python first.py" not in expanded

            app.action_toggle_details()
            await pilot.pause()
            collapsed = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines
            )
            assert "python second.py" not in collapsed
            assert "python first.py" not in collapsed

    asyncio.run(run())


def test_keyboard_navigates_and_toggles_one_activity_card() -> None:
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
            for command_id, title, command in (
                ("cmd-first", "第一项检查", "python first.py"),
                ("cmd-second", "第二项检查", "python second.py"),
            ):
                app._on_agent_event(
                    "user",
                    AgentEvent(
                        "committed",
                        f"@kimi {title}",
                        {"command_id": command_id},
                    ),
                )
                app._on_agent_event(
                    "kimi",
                    AgentEvent(
                        "tool",
                        title,
                        {
                            "command_id": command_id,
                            "tool_call_id": f"tool-{command_id}",
                            "status": "completed",
                            "command": command,
                        },
                    ),
                )
                app._on_agent_event(
                    "kimi",
                    AgentEvent("done", meta={"command_id": command_id}),
                )

            await pilot.press("ctrl+g")
            await pilot.pause()
            log = app.query_one(RichLog)
            assert log.has_focus
            selected = "\n".join(str(line.text) for line in log.lines)
            assert "▶ 任务 cmd-seco" in selected

            await pilot.press("up")
            await pilot.pause()
            selected = "\n".join(str(line.text) for line in log.lines)
            assert "▶ 任务 cmd-firs" in selected
            assert "▶ 任务 cmd-seco" not in selected

            await pilot.press("enter")
            await pilot.pause()
            expanded = "\n".join(str(line.text) for line in log.lines)
            assert "python first.py" in expanded
            assert "python second.py" not in expanded

            await pilot.press("down", "enter")
            await pilot.pause()
            both_expanded = "\n".join(str(line.text) for line in log.lines)
            assert "python first.py" in both_expanded
            assert "python second.py" in both_expanded

            await pilot.press("up", "enter")
            await pilot.pause()
            first_collapsed = "\n".join(
                str(line.text) for line in log.lines
            )
            assert "python first.py" not in first_collapsed
            assert "python second.py" in first_collapsed

            await pilot.press("escape")
            await pilot.pause()
            assert app.query_one("#composer").has_focus
            closed = "\n".join(str(line.text) for line in log.lines)
            assert "▶ 任务" not in closed

    asyncio.run(run())


class _BackgroundActivityAdapter:
    name = "worker"
    session_id = None
    stateful_session = False
    started: asyncio.Event
    release: asyncio.Event

    async def stream(self, prompt: str, workdir: str):
        del prompt, workdir
        yield AgentEvent(
            "tool",
            "后台测试",
            {
                "tool_call_id": "background-tool",
                "status": "in_progress",
                "command": "python -m unittest",
            },
        )
        type(self).started.set()
        await type(self).release.wait()
        yield AgentEvent(
            "tool",
            "后台测试",
            {"tool_call_id": "background-tool", "status": "completed"},
        )
        yield AgentEvent("text", "后台回复已完成")
        yield AgentEvent("done")


def test_activity_card_survives_background_session_switch() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            _BackgroundActivityAdapter.started = asyncio.Event()
            _BackgroundActivityAdapter.release = asyncio.Event()
            orch = Orchestrator(
                str(workdir),
                specs=(
                    AgentSpec(
                        "worker", "jsonl", _BackgroundActivityAdapter),
                ),
                store=store,
            )
            app = ChatApp(workdir=str(workdir), orchestrator=orch)
            async with app.run_test() as pilot:
                app._dispatch_from_ui("@worker 后台任务")
                await asyncio.wait_for(
                    _BackgroundActivityAdapter.started.wait(), timeout=1)
                first_id = app.session_manager.active_session_id
                app.action_toggle_details()
                await pilot.pause()
                before_switch = "\n".join(
                    str(line.text) for line in app.query_one(RichLog).lines
                )
                assert "python -m unittest" in before_switch

                await app.session_manager.create_session()
                app._bind_active_runtime()
                app._render_active_session()
                _BackgroundActivityAdapter.release.set()
                for _ in range(100):
                    if app.session_manager.snapshot(first_id).status \
                            == "completed":
                        break
                    await asyncio.sleep(0.01)
                assert app.session_manager.snapshot(first_id).status \
                    == "completed"

                await app.session_manager.activate(first_id)
                app._bind_active_runtime()
                app._render_active_session()
                await pilot.pause()
                rendered = [
                    str(line.text) for line in app.query_one(RichLog).lines
                ]
                assert "[worker] 后台回复已完成" in rendered
                assert len([
                    line for line in rendered
                    if line.startswith("[activity] ")
                ]) == 1
                expanded = "\n".join(
                    str(line.text) for line in app.query_one(RichLog).lines
                )
                assert "后台测试 · 已完成" in expanded
                assert "python -m unittest" in expanded
                assert "响应耗时" in expanded
                assert "响应耗时 未知" not in expanded

    asyncio.run(run())


def test_external_background_command_freezes_authoritative_duration() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            state_root = root / "state"
            store = RoomStore(workdir, state_root=state_root)
            _BackgroundActivityAdapter.started = asyncio.Event()
            _BackgroundActivityAdapter.release = asyncio.Event()
            orch = Orchestrator(
                str(workdir),
                specs=(
                    AgentSpec(
                        "worker", "jsonl", _BackgroundActivityAdapter),
                ),
                store=store,
            )
            app = ChatApp(workdir=str(workdir), orchestrator=orch)
            client = ControlClient(workdir, state_root=state_root)
            async with app.run_test() as pilot:
                first_id = app.session_manager.active_session_id
                await app.session_manager.create_session()
                app._bind_active_runtime()
                app._render_active_session()

                submitted = await client.submit(
                    "@worker 外部后台任务", request_id="external-background")
                duplicate = await client.submit(
                    "@worker 不应重放", request_id="external-background")
                assert duplicate["command_id"] == submitted["command_id"]
                await asyncio.wait_for(
                    _BackgroundActivityAdapter.started.wait(), timeout=1)
                queued = await client.submit("@worker 排队后取消")
                cancelled = await client.cancel_command(queued["command_id"])
                assert cancelled["status"] == "cancelled"
                _BackgroundActivityAdapter.release.set()
                result = await client.wait_command(
                    submitted["command_id"], timeout=5)
                assert result["status"] == "completed"
                for _ in range(100):
                    if app.session_manager.snapshot(first_id).status \
                            == "completed":
                        break
                    await asyncio.sleep(0.01)

                await app.session_manager.activate(first_id)
                app._bind_active_runtime()
                app._render_active_session()
                await pilot.pause()
                rendered = "\n".join(
                    str(line.text) for line in app.query_one(RichLog).lines
                )
                assert "[worker] 后台回复已完成" in rendered
                assert "响应耗时" in rendered
                assert "响应耗时 未知" not in rendered
                assert "已取消" in rendered
                assert len([
                    line for line in rendered.splitlines()
                    if line.startswith("[activity] ")
                ]) == 2, rendered

    asyncio.run(run())


def test_background_churn_does_not_reuse_stale_expansion_state() -> None:
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
                first_id = app.session_manager.active_session_id
                first_feed = app._activity_feed
                first_feed.begin("cmd-reused")
                first_feed.record_tool(
                    "cmd-reused",
                    "kimi",
                    "tool-original",
                    "原任务",
                    status="completed",
                    detail="python original.py",
                )
                app._upsert_activity_card("cmd-reused")
                app.action_toggle_details()
                first_feed.set_command_state("cmd-reused", "completed")

                await app.session_manager.create_session()
                app._bind_active_runtime()
                app._render_active_session()

                # 后台淘汰量超过通知队列容量，旧实现会漏清最早的展开 ID。
                for index in range(250):
                    command_id = f"cmd-churn-{index}"
                    first_feed.begin(command_id)
                    first_feed.set_command_state(command_id, "completed")

                await app.session_manager.activate(first_id)
                app._bind_active_runtime()
                app._render_active_session()

                first_feed.begin("cmd-reused")
                first_feed.record_tool(
                    "cmd-reused",
                    "kimi",
                    "tool-new",
                    "新任务",
                    status="completed",
                    detail="python new.py",
                )
                app._upsert_activity_card("cmd-reused")
                await pilot.pause()
                rendered = "\n".join(
                    str(line.text) for line in app.query_one(RichLog).lines
                )
                assert "python new.py" not in rendered

    asyncio.run(run())


if __name__ == "__main__":
    test_activity_feed_coalesces_progress_and_tool_updates()
    test_activity_feed_tracks_latest_agent_and_bounds_terminal_details()
    test_tui_keeps_one_activity_card_and_primary_messages()
    test_tui_failure_stays_visible_outside_activity_details()
    test_details_toggles_only_the_latest_activity_card()
    test_keyboard_navigates_and_toggles_one_activity_card()
    test_activity_card_survives_background_session_switch()
    test_external_background_command_freezes_authoritative_duration()
    test_background_churn_does_not_reuse_stale_expansion_state()
    print("ok  TUI 执行活动折叠、展开与失败可见性")

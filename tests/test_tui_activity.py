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
from collaboration import (
    CollaborationPlan,
    CollaborationPlanEvent,
    CollaborationStep,
)
from control import ControlClient
from main import ChatApp
from orchestrator import AgentSpec, Orchestrator
from storage.store import RoomStore
from test_basic import make_orch
from textual.widgets import RichLog, Static
from tui_activity import ActivityDetailEvent, ActivityFeed


def _activity_text(app: ChatApp) -> str:
    return str(app.query_one("#activity-panel", Static).render())


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


def test_activity_feed_shows_pending_requests_without_duplicate_text() -> None:
    feed = ActivityFeed()
    feed.begin("cmd-running", "running")
    feed.begin("cmd-queued-one", "queued")
    feed.record_pending_request(
        "cmd-queued-one", "@qwen   补充一个\n黄段子的例子")
    feed.begin("cmd-queued-two", "queued")
    feed.record_pending_request("cmd-queued-two", "@kimi 检查上一条结论")

    first = feed.render("cmd-queued-one", expanded=False)
    second = feed.render("cmd-queued-two", expanded=False)
    assert "排队 1" in first
    assert "待发送：@qwen 补充一个 黄段子的例子" in first
    assert "排队 2" in second
    assert "待发送：@kimi 检查上一条结论" in second

    assert feed.mark_request_committed("cmd-queued-one")
    feed.set_command_state("cmd-queued-one", "running")
    assert "@qwen 补充一个" not in feed.render(
        "cmd-queued-one", expanded=False)
    assert "排队 1" in feed.render("cmd-queued-two", expanded=False)

    feed.set_command_state("cmd-queued-two", "cancelled")
    assert "未发送：@kimi 检查上一条结论" in feed.render(
        "cmd-queued-two", expanded=False)

    feed.begin("cmd-failed", "queued")
    feed.record_pending_request("cmd-failed", "不能在失败卡残留的原文")
    feed.set_command_state("cmd-failed", "failed")
    assert "不能在失败卡残留的原文" not in feed.render(
        "cmd-failed", expanded=False)


def test_activity_feed_compact_view_prioritizes_actionable_tasks() -> None:
    feed = ActivityFeed()
    for index in range(4):
        command_id = f"cmd-old-{index}"
        feed.begin(command_id)
        feed.set_command_state(command_id, "completed")
    for index in range(4):
        feed.begin(f"cmd-queued-{index}", "queued")
    feed.begin("cmd-running")
    feed.begin("cmd-latest")
    feed.set_command_state("cmd-latest", "completed")

    assert feed.compact_command_ids() == (
        "cmd-queued-0",
        "cmd-queued-1",
        "cmd-running",
        "cmd-latest",
    )

    completion_order = ActivityFeed()
    completion_order.begin("cmd-created-first")
    completion_order.begin("cmd-created-second")
    completion_order.set_command_state("cmd-created-second", "completed")
    completion_order.set_command_state("cmd-created-first", "completed")
    assert completion_order.compact_command_ids() == (
        "cmd-created-first",
    )


def test_activity_feed_renders_first_class_collaboration_handoff() -> None:
    long_assignment = (
        "基于前序证据制定方案，并逐项核对约束、风险、回滚和验收证据，"
        "再检查权限、会话隔离、失败终态与重启恢复，并核对实时与持久"
        "投影在竞态下仍然一致，最后给出完整尾部"
    )
    plan = CollaborationPlan((
        CollaborationStep("kimi", "调查现状并列出证据"),
        CollaborationStep("opencode", long_assignment),
        CollaborationStep("kimi", "复核并向用户完成最终交付"),
    ))
    feed = ActivityFeed()
    command_id = "cmd-plan-view"
    feed.begin(command_id)
    assert feed.record_plan_event(
        command_id, CollaborationPlanEvent.created(plan))
    assert feed.record_plan_event(
        command_id, CollaborationPlanEvent.transition(plan, 1, "running"))
    collapsed = feed.render(command_id, expanded=False)
    assert "步骤 1/3 · kimi：调查现状并列出证据" in collapsed

    expanded = feed.render(command_id, expanded=True)
    assert "协作计划 · 1/3" in expanded
    assert "› 1  kimi · 进行中 · 调查现状并列出证据" in expanded
    assert f"○ 2  opencode · 等待 · {long_assignment}" in expanded
    assert "完整尾部" in expanded
    assert "暂无可显示" not in expanded

    feed.record_plan_event(
        command_id, CollaborationPlanEvent.transition(plan, 1, "completed"))
    feed.record_plan_event(
        command_id, CollaborationPlanEvent.transition(plan, 2, "running"))
    expanded = feed.render(command_id, expanded=True)
    assert "协作计划 · 2/3" in expanded
    assert "✓ 1  kimi · 已结束" in expanded
    assert "交接 · kimi → opencode" in expanded
    assert not feed.record_plan_event(command_id, "not-json")

    restored = ActivityFeed()
    restored.begin(command_id)
    plan_events = (
        CollaborationPlanEvent.created(plan),
        CollaborationPlanEvent.transition(plan, 1, "running"),
        CollaborationPlanEvent.transition(plan, 1, "completed"),
        CollaborationPlanEvent.transition(plan, 2, "running"),
    )
    details = tuple(
        ActivityDetailEvent(
            index,
            "host" if index == 1 else "opencode",
            "plan",
            event.encode(),
            f"2026-08-28T01:00:0{index}Z",
        )
        for index, event in enumerate(plan_events, start=1)
    )
    restored.set_persisted_details(
        command_id,
        details,
        total_count=len(details),
        omitted_count=0,
    )
    restored_text = restored.render(command_id, expanded=True)
    assert "协作计划 · 2/3" in restored_text
    assert "交接 · kimi → opencode" in restored_text
    assert '"event":"created"' not in restored_text


def test_older_detail_snapshot_cannot_roll_back_live_plan() -> None:
    plan = CollaborationPlan((
        CollaborationStep("kimi", "调查现状"),
        CollaborationStep("opencode", "复核结论"),
    ))
    feed = ActivityFeed()
    command_id = "cmd-plan-race"
    feed.begin(command_id)
    live = (
        CollaborationPlanEvent.created(plan),
        CollaborationPlanEvent.transition(plan, 1, "running"),
        CollaborationPlanEvent.transition(plan, 1, "completed"),
        CollaborationPlanEvent.transition(plan, 2, "running"),
    )
    for event in live:
        feed.record_plan_event(command_id, event)

    stale = tuple(
        ActivityDetailEvent(
            index, "host", "plan", event.encode(),
            f"2026-08-28T01:00:0{index}Z",
        )
        for index, event in enumerate(live[:2], start=1)
    )
    feed.set_persisted_details(
        command_id, stale,
        total_count=len(stale), omitted_count=0,
    )
    rendered = feed.render(command_id, expanded=True)
    assert "协作计划 · 2/2" in rendered
    assert "✓ 1  kimi · 已结束" in rendered
    assert "› 2  opencode · 进行中" in rendered


def test_details_restores_collaboration_plan_after_restart() -> None:
    async def run() -> None:
        plan = CollaborationPlan((
            CollaborationStep("kimi", "调查并列出证据"),
            CollaborationStep("qwen", "复核并完成最终交付"),
        ))
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            command_id = "cmd-plan-restart"
            store.append_event(
                command_id=command_id, agent="system",
                kind="running", text="开始执行")
            for agent, event in (
                ("host", CollaborationPlanEvent.created(plan)),
                ("kimi", CollaborationPlanEvent.transition(
                    plan, 1, "running")),
                ("kimi", CollaborationPlanEvent.transition(
                    plan, 1, "completed")),
                ("qwen", CollaborationPlanEvent.transition(
                    plan, 2, "running")),
                ("qwen", CollaborationPlanEvent.transition(
                    plan, 2, "completed")),
            ):
                store.append_event(
                    command_id=command_id,
                    agent=agent,
                    kind="plan",
                    text=event.encode(),
                )
            store.append_event(
                command_id=command_id, agent="system",
                kind="completed", text="本轮响应结束")
            app = ChatApp(
                workdir=str(workdir),
                orchestrator=Orchestrator(str(workdir), store=store),
            )
            async with app.run_test():
                restored_status = str(
                    app.query_one("#task-status").render())
                assert "协作计划 2/2 · kimi → qwen" in restored_status
                # 会话切换会清空瞬时 TaskProgress；切回时必须能从同一
                # execution event 重新构建固定任务区，而不只恢复活动卡。
                app._task_progresses.clear()
                app._latest_task_id = None
                app._restore_latest_collaboration_task()
                switched_status = str(
                    app.query_one("#task-status").render())
                assert "协作计划 2/2 · kimi → qwen" in switched_status
                app.action_toggle_details()
                await app.workers.wait_for_complete()
                rendered = _activity_text(app)
                assert "协作计划 · 2/2" in rendered, rendered
                assert "✓ 1  kimi · 已结束 · 调查并列出证据" in rendered
                assert "✓ 2  qwen · 已结束 · 复核并完成最终交付" in rendered
                assert '"event":"created"' not in rendered
                task_status = str(app.query_one("#task-status").render())
                assert "协作计划 2/2 · kimi → qwen" in task_status

    asyncio.run(run())


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


def test_activity_panel_keeps_process_out_of_chat_timeline() -> None:
    """活动是独立工作区，不再伪装成聊天 speaker。"""

    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test(size=(100, 30)) as pilot:
            command_id = "cmd-separated"
            app._on_agent_event(
                "user",
                AgentEvent(
                    "committed", "@kimi 检查项目",
                    {"command_id": command_id},
                ),
            )
            app._on_agent_event(
                "kimi",
                AgentEvent(
                    "tool", "运行测试",
                    {
                        "command_id": command_id,
                        "tool_call_id": "tool-separated",
                        "status": "completed",
                        "command": "python -m unittest",
                    },
                ),
            )
            await pilot.pause()

            chat = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines
            )
            activity = str(app.query_one("#activity-panel", Static).render())
            assert "[user] @kimi 检查项目" in chat
            assert "[activity]" not in chat
            assert "任务 cmd-sepa" in activity
            assert "1 个工具" in activity

    asyncio.run(run())
    print("ok  活动面板与聊天主线分层")


def test_persisted_details_project_process_without_repeating_answer() -> None:
    feed = ActivityFeed()
    command_id = "cmd-persisted-detail"
    feed.begin(command_id)
    answer_chunk = "这段回答正文只应出现在聊天主线"
    events = (
        ActivityDetailEvent(1, "system", "queued", "进入队列",
                            "2026-08-28T01:00:00Z"),
        ActivityDetailEvent(2, "host", "running", "开始执行",
                            "2026-08-28T01:00:01Z"),
        ActivityDetailEvent(3, "host", "status", "正在制定协作计划",
                            "2026-08-28T01:00:02Z"),
        ActivityDetailEvent(4, "host", "partial", answer_chunk,
                            "2026-08-28T01:00:03Z"),
        ActivityDetailEvent(5, "host", "partial", "第二段",
                            "2026-08-28T01:00:04Z"),
        ActivityDetailEvent(6, "host", "completed", "本轮响应结束",
                            "2026-08-28T01:00:05Z"),
    )

    assert feed.set_persisted_details(
        command_id, events, total_count=6, omitted_count=0)
    rendered = feed.render(command_id, expanded=True)
    assert "过程概览 · 6 条事件 · host" in rendered
    assert "阶段 · 正在制定协作计划" in rendered
    assert (
        f"输出 · 2 个片段 / {len(answer_chunk) + len('第二段')} 字"
        "（正文见聊天主线）"
    ) in rendered
    assert "本轮未调用工具或请求权限" in rendered
    assert answer_chunk not in rendered


def test_persisted_details_redact_and_compact_noisy_process_events() -> None:
    feed = ActivityFeed(max_detail_rows=20)
    command_id = "cmd-safe-detail"
    feed.begin(command_id)
    events = (
        ActivityDetailEvent(1, "qwen", "running", "开始执行",
                            "2026-08-28T01:00:00Z"),
        ActivityDetailEvent(2, "qwen", "status", "仍在运行（已等待 10 秒）",
                            "2026-08-28T01:00:10Z"),
        ActivityDetailEvent(3, "qwen", "status", "仍在运行（已等待 20 秒）",
                            "2026-08-28T01:00:20Z"),
        ActivityDetailEvent(
            4, "qwen", "tool",
            "Shell · running\nAPI_KEY=supersecret python inspect.py",
            "2026-08-28T01:00:21Z"),
        ActivityDetailEvent(5, "qwen", "permission", "允许读取一次",
                            "2026-08-28T01:00:22Z"),
        ActivityDetailEvent(6, "system", "interjection_requested",
                            "请求插入最早排队输入",
                            "2026-08-28T01:00:23Z"),
        ActivityDetailEvent(7, "qwen", "status", "正在整理结果",
                            "2026-08-28T01:00:24Z"),
        ActivityDetailEvent(8, "qwen", "completed", "本轮响应结束",
                            "2026-08-28T01:00:25Z"),
    )

    feed.set_persisted_details(
        command_id,
        events,
        total_count=12,
        omitted_count=4,
        agents=("qwen", "API_KEY=agentsecret"),
    )
    rendered = feed.render(command_id, expanded=True)
    assert "API_KEY=[已隐藏]" in rendered
    assert "supersecret" not in rendered
    assert "agentsecret" not in rendered
    assert "已等待 10 秒" not in rendered
    assert "已等待 20 秒" in rendered
    assert "已合并 1 条重复心跳" in rendered
    assert "权限 · 允许读取一次" in rendered
    assert "插话 · 请求插入最早排队输入" in rendered
    assert "持久日志读取省略 4 条" in rendered

    bounded = ActivityFeed(max_detail_rows=5)
    bounded.begin("cmd-bounded")
    noisy = tuple(
        ActivityDetailEvent(
            index, "qwen", "status", f"阶段 {index}",
            f"2026-08-28T01:01:{index:02d}Z",
        )
        for index in range(1, 13)
    )
    bounded.set_persisted_details(
        "cmd-bounded", noisy, total_count=12, omitted_count=0)
    assert "过程视图再省略 7 条高频事件" in bounded.render(
        "cmd-bounded", expanded=True)


def test_persisted_detail_loading_error_is_visible_and_redacted() -> None:
    feed = ActivityFeed()
    feed.begin("cmd-load-error")
    feed.start_details_loading("cmd-load-error")
    assert "正在读取持久记录" in feed.render(
        "cmd-load-error", expanded=True)

    feed.set_details_error(
        "cmd-load-error", "API_KEY=supersecret 无法读取事件")
    rendered = feed.render("cmd-load-error", expanded=True)
    assert "详情加载失败" in rendered
    assert "API_KEY=[已隐藏]" in rendered
    assert "supersecret" not in rendered


def test_terminal_card_rejects_stale_running_detail_snapshot() -> None:
    feed = ActivityFeed()
    command_id = "cmd-detail-race"
    feed.begin(command_id)
    feed.set_command_state(command_id, "completed")
    running_snapshot = (
        ActivityDetailEvent(1, "codex", "running", "开始执行",
                            "2026-08-28T01:00:00Z"),
        ActivityDetailEvent(2, "codex", "status", "回复中",
                            "2026-08-28T01:00:01Z"),
    )
    assert not feed.set_persisted_details(
        command_id, running_snapshot, total_count=2, omitted_count=0)
    assert "过程概览" not in feed.render(command_id, expanded=True)

    terminal_snapshot = running_snapshot + (
        ActivityDetailEvent(3, "codex", "completed", "本轮响应结束",
                            "2026-08-28T01:00:02Z"),
    )
    assert feed.set_persisted_details(
        command_id, terminal_snapshot, total_count=3, omitted_count=0)
    assert not feed.set_persisted_details(
        command_id, running_snapshot, total_count=2, omitted_count=0)
    rendered = feed.render(command_id, expanded=True)
    assert "完成 · 本轮响应结束" in rendered
    assert "过程概览 · 3 条事件" in rendered

    running = ActivityFeed()
    running.begin("cmd-generation")
    old_generation = running.start_details_loading("cmd-generation")
    new_generation = running.start_details_loading("cmd-generation")
    assert running.set_persisted_details(
        "cmd-generation",
        (ActivityDetailEvent(
            3, "codex", "status", "新快照",
            "2026-08-28T01:00:03Z"),),
        total_count=3,
        omitted_count=2,
        generation=new_generation,
    )
    assert not running.set_persisted_details(
        "cmd-generation",
        (ActivityDetailEvent(
            2, "codex", "status", "旧快照",
            "2026-08-28T01:00:02Z"),),
        total_count=2,
        omitted_count=1,
        generation=old_generation,
    )
    running_rendered = running.render("cmd-generation", expanded=True)
    assert "新快照" in running_rendered
    assert "旧快照" not in running_rendered


def test_persisted_detail_overview_uses_complete_category_counts() -> None:
    feed = ActivityFeed()
    command_id = "cmd-complete-counts"
    feed.begin(command_id, "completed")
    sampled = (
        ActivityDetailEvent(1, "codex", "running", "开始执行",
                            "2026-08-28T01:00:00Z"),
        ActivityDetailEvent(70, "codex", "completed", "本轮响应结束",
                            "2026-08-28T01:01:00Z"),
    )
    feed.set_persisted_details(
        command_id,
        sampled,
        total_count=71,
        omitted_count=69,
        kind_counts={
            "running": 1,
            "status": 40,
            "tool": 10,
            "permission": 4,
            "steering": 2,
            "interjection_requested": 3,
            "partial": 9,
            "completed": 1,
            "cancelled": 1,
        },
        partial_char_count=321,
    )
    rendered = feed.render(command_id, expanded=True)
    assert (
        "完整统计 · 生命周期 3 · 阶段 40 · 工具 10 · 权限 4 · 控制 6"
    ) in rendered
    assert "输出 · 9 个片段 / 321 字" in rendered


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
            collapsed = _activity_text(app)
            assert "1 个工具" in collapsed.replace("\n", "")
            assert "python -m unittest" not in collapsed
            assert "[user] @kimi 检查项目" in lines
            assert "[kimi] 测试已经通过。" in lines
            assert len([
                line for line in lines if "仍在运行（已等待" in line
            ]) == 0

            app.action_toggle_details()
            await pilot.pause()
            expanded = _activity_text(app)
            assert "python -m unittest" in expanded
            assert "API_KEY=[已隐藏]" in expanded
            assert "supersecret" not in expanded
            assert "kimi 仍在运行（已等待 30 秒）" in expanded
            assert expanded.count("任务 cmd-visi") == 1

    asyncio.run(run())


def test_tui_failure_stays_visible_outside_activity_details() -> None:
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
            command_id = "cmd-failed"
            error = "权限被拒绝 API_KEY=supersecret"
            app._on_agent_event(
                "user",
                AgentEvent(
                    "committed", "@kimi 执行危险操作",
                    {"command_id": command_id},
                ),
            )
            app._on_agent_event(
                "kimi",
                AgentEvent("error", error, {"command_id": command_id}),
            )
            app._set_command_status(command_id, "failed", error)
            await pilot.pause()

            lines = [str(line.text) for line in app.query_one(RichLog).lines]
            visible = "\n".join(lines)
            assert "[kimi] 出错：权限被拒绝 API_KEY=[已隐藏]" in visible
            activity = _activity_text(app)
            assert activity.count("任务 cmd-fail") == 1
            assert "失败" in activity
            status = str(app.query_one("#task-status", Static).render())
            notice = str(app.query_one("#notice-strip", Static).render())
            assert "supersecret" not in "\n".join(
                (visible, activity, status, notice))

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
            expanded = _activity_text(app)
            assert "python second.py" in expanded
            assert "python first.py" not in expanded

            app.action_toggle_details()
            await pilot.pause()
            collapsed = _activity_text(app)
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
            panel = app.query_one("#activity-panel", Static)
            assert panel.has_focus
            selected = _activity_text(app)
            assert "▶ 任务 cmd-seco" in selected

            await pilot.press("up")
            await pilot.pause()
            selected = _activity_text(app)
            assert "▶ 任务 cmd-firs" in selected
            assert "▶ 任务 cmd-seco" not in selected

            await pilot.press("enter")
            await pilot.pause()
            expanded = _activity_text(app)
            assert "python first.py" in expanded
            assert "python second.py" not in expanded

            await pilot.press("down", "enter")
            await pilot.pause()
            both_expanded = _activity_text(app)
            assert "python first.py" in both_expanded
            assert "python second.py" in both_expanded

            await pilot.press("up", "enter")
            await pilot.pause()
            first_collapsed = _activity_text(app)
            assert "python first.py" not in first_collapsed
            assert "python second.py" in first_collapsed

            await pilot.press("escape")
            await pilot.pause()
            assert app.query_one("#composer").has_focus
            closed = _activity_text(app)
            assert "▶ 任务" not in closed

    asyncio.run(run())


def test_activity_panel_compacts_history_until_keyboard_browse() -> None:
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test(size=(100, 30)) as pilot:
            for index in range(12):
                command_id = f"cmd-history-{index:02d}"
                app._activity_feed.begin(command_id)
                app._activity_feed.record_status(
                    command_id, "host", "本轮响应结束", state="completed")
                app._activity_feed.set_command_state(
                    command_id, "completed", elapsed_seconds=1.0)
            app._refresh_activity_cards()
            await pilot.pause()

            panel = app.query_one("#activity-panel", Static)
            compact = _activity_text(app)
            compact_height = panel.size.height
            assert "任务 cmd-hist" in compact
            assert compact.count("任务 cmd-hist") == 1
            assert "已收起 11 个任务" in compact
            assert compact_height <= 6

            await pilot.press("ctrl+g")
            await pilot.pause()
            browsing = _activity_text(app)
            assert browsing.count("任务 cmd-hist") == 12
            assert "已收起" not in browsing
            assert panel.size.height > compact_height
            assert panel.max_scroll_y > 0

            await pilot.click("#composer")
            await pilot.pause()
            assert app.query_one("#composer").has_focus
            assert _activity_text(app).count("任务 cmd-hist") == 1

            await pilot.press("ctrl+g")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert _activity_text(app).count("任务 cmd-hist") == 1

    asyncio.run(run())


def test_activity_keyboard_reaches_latest_card_in_long_panel() -> None:
    """长活动列表必须形成真实滚动区域，选中末卡后自动进入视口。"""
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test(size=(60, 40)) as pilot:
            for index in range(20):
                command_id = f"cmd-{index:02d}"
                app._activity_feed.begin(command_id, "completed")
                app._activity_feed.record_status(
                    command_id, "kimi", "完成", state="completed")
            app._refresh_activity_cards()
            await pilot.press("ctrl+g")
            await pilot.pause()

            panel = app.query_one("#activity-panel", Static)
            assert app._selected_activity_id == "cmd-19"
            assert panel.max_scroll_y > 0
            assert panel.scroll_y > 0

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


def test_consecutive_tui_inputs_show_and_consume_pending_queue() -> None:
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
                specs=(AgentSpec(
                    "worker", "jsonl", _BackgroundActivityAdapter),),
                store=store,
            )
            app = ChatApp(workdir=str(workdir), orchestrator=orch)
            async with app.run_test() as pilot:
                app._dispatch_from_ui("@worker 第一条长任务")
                await asyncio.wait_for(
                    _BackgroundActivityAdapter.started.wait(), timeout=1)
                app._dispatch_from_ui("@worker 第二条排队消息")

                for _ in range(100):
                    if len(app._activity_feed.command_ids()) == 2:
                        break
                    await asyncio.sleep(0.01)
                await pilot.pause()
                queued = _activity_text(app)
                assert "排队 1" in queued
                assert "待发送：@worker 第二条排队消息" in queued
                assert "[user] @worker 第二条排队消息" not in queued

                _BackgroundActivityAdapter.release.set()
                for _ in range(200):
                    if not app.session_manager.active_runtime.bus.has_pending():
                        break
                    await asyncio.sleep(0.01)
                await pilot.pause()
                completed_chat = "\n".join(
                    str(line.text) for line in app.query_one(RichLog).lines)
                completed_activity = _activity_text(app)
                assert completed_chat.count(
                    "[user] @worker 第二条排队消息") == 1
                assert "待发送：@worker 第二条排队消息" \
                    not in completed_activity

    asyncio.run(run())


def test_background_commit_clears_room_pending_preview() -> None:
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
            async with app.run_test():
                room_id = app.session_manager.active_session_id
                command_id = "cmd-background-pending"
                feed = app._activity_feed
                feed.begin(command_id, "queued")
                feed.record_pending_request(command_id, "后台排队消息")

                app._record_background_activity(
                    room_id,
                    "user",
                    AgentEvent(
                        "committed",
                        "后台排队消息",
                        {"command_id": command_id},
                    ),
                )

                committed = feed.render(command_id, expanded=False)
                assert "后台排队消息" not in committed
                assert "运行中" in committed
                assert "排队" not in committed

    asyncio.run(run())


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
                before_switch = _activity_text(app)
                assert "python -m unittest" in before_switch

                await app.session_manager.create_session()
                app._bind_active_runtime()
                app._render_active_session()
                background_status = str(
                    app.query_one("#task-status", Static).render())
                assert "后台 1 运行" in background_status
                _BackgroundActivityAdapter.release.set()
                for _ in range(100):
                    if app.session_manager.snapshot(first_id).status \
                            == "completed":
                        break
                    await asyncio.sleep(0.01)
                assert app.session_manager.snapshot(first_id).status \
                    == "completed"
                app._render_task_status()
                unread_status = str(
                    app.query_one("#task-status", Static).render())
                assert "1 未读" in unread_status

                await app.session_manager.activate(first_id)
                app._bind_active_runtime()
                app._render_active_session()
                await pilot.pause()
                rendered = [
                    str(line.text) for line in app.query_one(RichLog).lines
                ]
                assert "[worker] 后台回复已完成" in rendered
                expanded = _activity_text(app)
                assert "后台测试 · 已完成" in expanded
                assert "python -m unittest" in expanded
                assert "过程概览" in expanded
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
                chat = "\n".join(
                    str(line.text) for line in app.query_one(RichLog).lines
                )
                rendered = _activity_text(app)
                assert "[worker] 后台回复已完成" in chat
                assert "响应耗时" in rendered
                assert "响应耗时 未知" not in rendered
                assert "已结束" in rendered
                assert "已收起 1 个任务" in rendered
                assert rendered.count("· /details 展开") == 1, rendered
                await pilot.press("ctrl+g")
                await pilot.pause()
                browsing = _activity_text(app)
                assert browsing.count("任务 ") == 2, browsing
                assert "已收起" not in browsing
                assert "已取消" in browsing

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
                rendered = _activity_text(app)
                assert "python new.py" not in rendered

    asyncio.run(run())


def test_details_restores_latest_persisted_task_after_restart() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            store.append_event(
                command_id="cmd-restored-detail", agent="system",
                kind="queued", text="进入队列")
            store.append_event(
                command_id="cmd-restored-detail", agent="codex",
                kind="status", text="正在检查项目")
            store.append_event(
                command_id="cmd-restored-detail", agent="codex",
                kind="partial", text="主线回答")
            store.append_event(
                command_id="cmd-restored-detail",
                agent="API_KEY=agentsecret",
                kind="completed", text="本轮响应结束")
            app = ChatApp(
                workdir=str(workdir),
                orchestrator=Orchestrator(str(workdir), store=store),
            )

            async with app.run_test():
                assert not app._activity_feed.command_ids()
                app.action_toggle_details()
                await app.workers.wait_for_complete()
                rendered = _activity_text(app)
                assert "任务 cmd-rest" in rendered
                assert "过程概览 · 4 条事件 · codex" in rendered
                assert "阶段 · 正在检查项目" in rendered
                assert "输出 · 1 个片段 / 4 字（正文见聊天主线）" in rendered
                assert "主线回答" not in rendered
                assert "agentsecret" not in rendered

    asyncio.run(run())


def test_interrupted_partial_is_summarized_without_repeating_body() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "project"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            store.append_event(
                command_id="cmd-interrupted", agent="codex",
                kind="running", text="开始执行")
            store.append_event(
                command_id="cmd-interrupted", agent="codex",
                kind="partial", text="不应在详情重复的正文")
            app = ChatApp(
                workdir=str(workdir),
                orchestrator=Orchestrator(str(workdir), store=store),
            )

            async with app.run_test():
                app.action_toggle_details()
                await app.workers.wait_for_complete()
                rendered = _activity_text(app)
                assert "上次响应在输出过程中中断" in rendered
                assert "输出 · 1 个片段" in rendered
                assert "不应在详情重复的正文" not in rendered

    asyncio.run(run())


if __name__ == "__main__":
    test_activity_feed_coalesces_progress_and_tool_updates()
    test_activity_feed_shows_pending_requests_without_duplicate_text()
    test_activity_feed_compact_view_prioritizes_actionable_tasks()
    test_activity_feed_renders_first_class_collaboration_handoff()
    test_older_detail_snapshot_cannot_roll_back_live_plan()
    test_details_restores_collaboration_plan_after_restart()
    test_activity_feed_tracks_latest_agent_and_bounds_terminal_details()
    test_activity_panel_keeps_process_out_of_chat_timeline()
    test_persisted_details_project_process_without_repeating_answer()
    test_persisted_details_redact_and_compact_noisy_process_events()
    test_persisted_detail_loading_error_is_visible_and_redacted()
    test_terminal_card_rejects_stale_running_detail_snapshot()
    test_persisted_detail_overview_uses_complete_category_counts()
    test_tui_keeps_one_activity_card_and_primary_messages()
    test_tui_failure_stays_visible_outside_activity_details()
    test_details_toggles_only_the_latest_activity_card()
    test_keyboard_navigates_and_toggles_one_activity_card()
    test_activity_panel_compacts_history_until_keyboard_browse()
    test_activity_keyboard_reaches_latest_card_in_long_panel()
    test_consecutive_tui_inputs_show_and_consume_pending_queue()
    test_background_commit_clears_room_pending_preview()
    test_activity_card_survives_background_session_switch()
    test_external_background_command_freezes_authoritative_duration()
    test_background_churn_does_not_reuse_stale_expansion_state()
    test_details_restores_latest_persisted_task_after_restart()
    test_interrupted_partial_is_summarized_without_repeating_body()
    print("ok  TUI 执行活动折叠、展开与失败可见性")

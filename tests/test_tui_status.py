"""任务状态模型与 TUI 固定状态栏验收。"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from adapters.base import AgentEvent
from collaboration import (
    CollaborationPlan,
    CollaborationPlanEvent,
    CollaborationStep,
)
from main import ChatApp
from textual.widgets import Static
from test_basic import make_orch
from tui_status import (
    TaskProgress,
    command_elapsed_seconds,
    format_response_duration,
)


def test_partial_completion_keeps_per_agent_truth() -> None:
    progress = TaskProgress("cmd-status")
    progress.set_command("running")
    progress.set_agent("kimi", "running", "工程子代理执行中")
    progress.set_agent("codex", "completed", "实现与 Review 完成")
    progress.set_agent("kimi", "failed", "ACP 超时")
    progress.set_command("failed", "kimi: ACP 超时")

    assert progress.overall_label == "部分完成"
    rendered = progress.render(elapsed_seconds=95)
    assert "任务 cmd-stat" in rendered
    assert "部分完成" in rendered
    assert "响应耗时 1分35秒" in rendered
    assert "kimi 失败 · ACP 超时" in rendered
    assert "codex 完成 · 实现与 Review 完成" in rendered

    progress.set_workflow(
        "implement",
        {"reviewer": "codex", "implementer": "kimi", "verifier": "opencode"},
        True,
    )
    workflow = progress.render(elapsed_seconds=95)
    assert "workflow implement" in workflow
    assert "审 codex → 写 kimi → 验 opencode" in workflow
    assert "可追加 /steer" in workflow

    routed_failure = TaskProgress("cmd-routed")
    routed_failure.set_agent("host", "completed", "路由完成")
    routed_failure.set_agent("kimi", "failed", "ACP 超时")
    routed_failure.set_command("failed", "kimi: ACP 超时")
    assert routed_failure.overall_label == "失败"
    assert format_response_duration(0.84) == "0.8秒"
    assert format_response_duration(8.44) == "8.4秒"
    assert format_response_duration(61) == "1分01秒"
    assert format_response_duration(3661) == "1小时01分01秒"
    assert command_elapsed_seconds(
        "2026-08-27T00:00:00Z",
        "2026-08-27T00:01:01Z",
    ) == 61
    assert command_elapsed_seconds(
        "2026-08-27T00:00:00Z",
        now=datetime(2026, 8, 27, 0, 0, 8, 440000, tzinfo=timezone.utc),
    ) == 8.44
    assert command_elapsed_seconds(None) is None
    assert command_elapsed_seconds("not-a-timestamp") is None
    assert command_elapsed_seconds(
        "2026-08-27T00:00:00Z",
        allow_open_interval=False,
    ) is None
    active = TaskProgress("cmd-active", status="running")
    assert "已用 8.4秒" in active.render(elapsed_seconds=8.44)
    active.set_command("running", elapsed_seconds=8.44)
    active.set_command("interrupted")
    assert "响应耗时 未知" in active.render()
    print("ok  部分完成保留每个 agent 的真实终态")


def test_tui_status_panel_tracks_agent_lifecycle() -> None:
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
            idle_panel = str(app.query_one("#task-status", Static).render())
            assert "主持 模型" in idle_panel
            assert "Agent 7/7" in idle_panel
            command_id = "cmd-panel"
            app._on_agent_event("user", AgentEvent(
                "committed",
                "@kimi @codex 实现并验收",
                {"command_id": command_id},
            ))
            app._on_agent_event("kimi", AgentEvent(
                "status",
                "已接收任务，准备执行",
                {
                    "command_id": command_id,
                    "agent_state": "running",
                    "phase": "实现中",
                },
            ))
            app._on_agent_event("codex", AgentEvent(
                "done", meta={"command_id": command_id}))
            app._on_agent_event("kimi", AgentEvent(
                "error", "ACP 超时", {"command_id": command_id}))
            app._set_command_status(
                command_id,
                "failed",
                "kimi: ACP 超时",
                elapsed_seconds=5.4,
            )
            await pilot.pause()

            panel = str(app.query_one("#task-status").render())
            assert "部分完成" in panel, panel
            assert "kimi 失败" in panel, panel
            assert "codex 完成" in panel, panel
            assert "响应耗时 5.4秒" in panel, panel
            assert "Ctrl+X" not in panel, panel
            activity = str(
                app.query_one("#activity-panel", Static).render())
            assert "响应耗时 5.4秒" in activity, activity

            app._render_task_status()
            frozen = str(app.query_one("#task-status").render())
            assert "响应耗时 5.4秒" in frozen, frozen

            workflow_id = "cmd-workflow"
            app._on_agent_event("user", AgentEvent(
                "committed",
                "/workflow ...",
                {
                    "command_id": workflow_id,
                    "workflow": True,
                    "workflow_stage": "queued",
                    "workflow_roles": {
                        "reviewer": "codex",
                        "implementer": "kimi",
                        "verifier": "opencode",
                    },
                    "steering_available": True,
                },
            ))
            await pilot.pause()
            workflow_panel = str(app.query_one("#task-status").render())
            assert "workflow queued" in workflow_panel
            assert "审 codex → 写 kimi → 验 opencode" in workflow_panel
            assert "可追加 /steer" in workflow_panel
            assert "codex 进行中 · 审查中" in workflow_panel
            assert "kimi 排队 · 等待实现" in workflow_panel
            assert "opencode 排队 · 等待复核" in workflow_panel
            collapsed = str(
                app.query_one("#activity-panel", Static).render())
            assert "codex：审查中" in collapsed
            app.action_toggle_details()
            await pilot.pause()
            rendered = str(
                app.query_one("#activity-panel", Static).render())
            assert "workflow 已创建" in rendered
            assert "kimi 思考中" not in rendered

            running_id = "cmd-cancel"
            app._on_agent_event("user", AgentEvent(
                "committed", "@kimi 长任务",
                {"command_id": running_id}))
            app._on_agent_event("system", AgentEvent(
                "cancel_requested", "正在取消当前任务…",
                {"command_id": running_id}))
            await pilot.pause()
            cancelling = str(app.query_one("#task-status").render())
            assert "运行中" in cancelling, cancelling
            assert "Ctrl+X" in cancelling, cancelling
            assert "已取消" not in cancelling, cancelling

    asyncio.run(run())
    print("ok  TUI 固定状态栏跟踪 agent 生命周期")


def test_periodic_status_render_uses_cached_host_identity() -> None:
    """每秒状态刷新只读内存，不重新解析模型配置。"""
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        with patch(
            "orchestrator.resolve_native_model_config",
            side_effect=AssertionError("周期刷新不应读取模型配置"),
        ):
            async with app.run_test() as pilot:
                app._render_task_status()
                app._workspace_status_line()
                app._completion_agents()
                await pilot.pause()

    asyncio.run(run())
    print("ok  周期状态刷新复用 Host 内存身份")


def test_interrupted_execution_restores_into_status_panel() -> None:
    class InterruptedStore:
        @staticmethod
        def read_events(*_args, **_kwargs):
            return {"items": []}

        @staticmethod
        def latest_execution_events():
            return [SimpleNamespace(
                command_id="cmd-old-run",
                agent="kimi",
                kind="tool",
                text="工程子代理执行中",
            )]

    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
            app.orch.store = InterruptedStore()
            app._restore_interrupted_executions()
            await pilot.pause()
            panel = str(app.query_one("#task-status").render())
            assert "已中断" in panel, panel
            assert "kimi 已中断 · 工程子代理执行中" in panel, panel
            assert "响应耗时 未知" in panel, panel
            assert "Ctrl+X" not in panel, panel
            app.orch.store = None

    asyncio.run(run())
    print("ok  重启中断任务恢复到固定状态栏")


def test_collaboration_status_queues_future_steps_and_allows_reentry() -> None:
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
            command_id = "cmd-collaboration"
            plan = CollaborationPlan((
                CollaborationStep("kimi", "调查现状并列出证据"),
                CollaborationStep("opencode", "基于证据完成审查"),
                CollaborationStep("kimi", "修订并完成最终交付"),
            ))
            app._on_agent_event("user", AgentEvent(
                "committed",
                "先调查，再审查，最后修订",
                {"command_id": command_id},
            ))
            app._on_agent_event("host", AgentEvent(
                "info",
                "协作计划 → kimi → opencode → kimi",
                {
                    "command_id": command_id,
                    "route_targets": ["kimi", "opencode"],
                    "collaboration_steps": ["kimi", "opencode", "kimi"],
                    "collaboration": True,
                    "collaboration_total": 3,
                },
            ))
            app._on_agent_event("host", AgentEvent(
                "plan", CollaborationPlanEvent.created(plan).encode(),
                {"command_id": command_id},
            ))
            app._on_agent_event("kimi", AgentEvent(
                "plan",
                CollaborationPlanEvent.transition(
                    plan, 1, "running").encode(),
                {"command_id": command_id},
            ))
            progress = app._task_progresses[command_id]
            assert progress.agents["kimi"].state == "running"
            assert progress.agents["opencode"].state == "queued"
            assert progress.agents["opencode"].phase == "等待前序步骤"

            app._on_agent_event("kimi", AgentEvent(
                "done", meta={
                    "command_id": command_id,
                    "collaboration": True,
                    "collaboration_step": 1,
                    "collaboration_total": 3,
                },
            ))
            app._on_agent_event("kimi", AgentEvent(
                "plan",
                CollaborationPlanEvent.transition(
                    plan, 1, "completed").encode(),
                {"command_id": command_id},
            ))
            assert progress.agents["kimi"].state == "completed"
            app._on_agent_event("opencode", AgentEvent(
                "plan",
                CollaborationPlanEvent.transition(
                    plan, 2, "running").encode(),
                {"command_id": command_id},
            ))
            app._on_agent_event("opencode", AgentEvent(
                "done", meta={
                    "command_id": command_id,
                    "collaboration": True,
                    "collaboration_step": 2,
                    "collaboration_total": 3,
                },
            ))
            app._on_agent_event("opencode", AgentEvent(
                "plan",
                CollaborationPlanEvent.transition(
                    plan, 2, "completed").encode(),
                {"command_id": command_id},
            ))
            app._on_agent_event("kimi", AgentEvent(
                "status",
                "协作第 3/3 步：已接收任务，准备执行",
                {
                    "command_id": command_id,
                    "agent_state": "running",
                    "phase": "协作 3/3",
                    "collaboration": True,
                    "collaboration_step": 3,
                    "collaboration_total": 3,
                },
            ))
            assert progress.agents["kimi"].state == "running"
            assert progress.agents["kimi"].phase == "协作 3/3"
            app._on_agent_event("kimi", AgentEvent(
                "plan",
                CollaborationPlanEvent.transition(
                    plan, 3, "running").encode(),
                {"command_id": command_id},
            ))
            await pilot.pause()
            panel = str(app.query_one("#task-status").render())
            assert "协作计划 3/3 · kimi → opencode → kimi" in panel, panel
            assert "✓ 1 kimi 已结束 · 调查现状并列出证据" in panel, panel
            assert "✓ 2 opencode 已结束 · 基于证据完成审查" in panel, panel
            assert "› 3 kimi 进行中 · 修订并完成最终交付" in panel, panel

    asyncio.run(run())
    print("ok  有序协作 TUI 排队与重复 agent 再进入")


def test_future_duplicate_skipped_does_not_overwrite_executed_truth() -> None:
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test():
            for command_id, executed_state in (
                ("cmd-duplicate-completed", "completed"),
                ("cmd-duplicate-cancelled", "cancelled"),
            ):
                app._on_agent_event("user", AgentEvent(
                    "committed", "重复 agent 协作",
                    {"command_id": command_id},
                ))
                app._on_agent_event("kimi", AgentEvent(
                    "status", "已发生步骤终态", {
                        "command_id": command_id,
                        "agent_state": executed_state,
                        "phase": "首个步骤已结束",
                        "collaboration": True,
                        "collaboration_step": 1,
                        "collaboration_total": 3,
                    },
                ))
                app._on_agent_event("kimi", AgentEvent(
                    "status", "协作第 3/3 步未执行：前序停止", {
                        "command_id": command_id,
                        "agent_state": "skipped",
                        "phase": "因前序失败未执行",
                        "collaboration": True,
                        "collaboration_step": 3,
                        "collaboration_total": 3,
                        "collaboration_preserve_agent_state": True,
                    },
                ))
                progress = app._task_progresses[command_id]
                assert progress.agents["kimi"].state == executed_state
                card = app._activity_feed.render(command_id, expanded=True)
                assert "协作第 3/3 步未执行" in card
                assert "已发生步骤终态" in card, card

    asyncio.run(run())
    print("ok  重复 agent 的未来 skipped 不覆盖已执行终态")


if __name__ == "__main__":
    test_partial_completion_keeps_per_agent_truth()
    test_tui_status_panel_tracks_agent_lifecycle()
    test_periodic_status_render_uses_cached_host_identity()
    test_interrupted_execution_restores_into_status_panel()
    test_collaboration_status_queues_future_steps_and_allows_reentry()
    test_future_duplicate_skipped_does_not_overwrite_executed_truth()
    print("\nTUI status 全部通过")

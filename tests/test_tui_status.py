"""任务状态模型与 TUI 固定状态栏验收。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from adapters.base import AgentEvent
from main import ChatApp
from test_basic import make_orch
from tui_status import TaskProgress


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
    assert "01:35" in rendered
    assert "kimi 失败 · ACP 超时" in rendered
    assert "codex 完成 · 实现与 Review 完成" in rendered

    routed_failure = TaskProgress("cmd-routed")
    routed_failure.set_agent("host", "completed", "路由完成")
    routed_failure.set_agent("kimi", "failed", "ACP 超时")
    routed_failure.set_command("failed", "kimi: ACP 超时")
    assert routed_failure.overall_label == "失败"
    print("ok  部分完成保留每个 agent 的真实终态")


def test_tui_status_panel_tracks_agent_lifecycle() -> None:
    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
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
                command_id, "failed", "kimi: ACP 超时")
            await pilot.pause()

            panel = str(app.query_one("#task-status").render())
            assert "部分完成" in panel, panel
            assert "kimi 失败" in panel, panel
            assert "codex 完成" in panel, panel
            assert "Ctrl+X" not in panel, panel

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
            assert "Ctrl+X" not in panel, panel
            app.orch.store = None

    asyncio.run(run())
    print("ok  重启中断任务恢复到固定状态栏")


if __name__ == "__main__":
    test_partial_completion_keeps_per_agent_truth()
    test_tui_status_panel_tracks_agent_lifecycle()
    test_interrupted_execution_restores_into_status_panel()
    print("\nTUI status 全部通过")

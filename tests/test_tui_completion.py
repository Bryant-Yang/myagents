"""输入候选的纯解析与 Textual 交互验收。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from adapters.base import AgentEvent
from main import ChatApp, ComposerInput
from host import HostDecision
from session_roles import SessionRole, SessionRoleChanges
from test_basic import make_orch
from tui_completion import (
    completion_context,
    local_command_for,
    unknown_mentions,
)


AGENTS = (
    ("kimi", "ACP"),
    ("opencode", "ACP+JSONL"),
    ("qwen", "ACP"),
    ("workbuddy", "ACP"),
    ("codex", "APP-SERVER"),
    ("host", "MODERATOR"),
)


def test_completion_parser_and_command_boundary() -> None:
    mention = completion_context("@co", 3, AGENTS)
    assert mention is not None
    assert [item.value for item in mention.items] == ["@codex"]

    multi = completion_context("@kimi @", 7, AGENTS)
    assert multi is not None
    assert "@kimi" not in [item.value for item in multi.items]
    assert [item.value for item in multi.items] == [
        "@opencode", "@qwen", "@workbuddy", "@codex", "@host"]

    slash = completion_context("/ca", 3, AGENTS)
    assert slash is not None
    assert [item.value for item in slash.items] == ["/cancel"]
    assert completion_context("请看 /ca", 6, AGENTS) is None
    roles = completion_context("/ro", 3, AGENTS)
    assert roles is not None
    assert [item.value for item in roles.items] == ["/roles", "/roles clear"]

    assert local_command_for("/new") is not None
    assert local_command_for("/discuss") is not None
    assert local_command_for("/workflow") is not None
    assert local_command_for("/steer") is not None
    assert local_command_for("/roles").description == "查看当前会话角色"
    assert local_command_for("/roles clear").description == "清空当前会话角色"
    assert local_command_for("/details").description == "展开或收起当前活动卡"
    assert local_command_for("/discuss @kimi @opencode -- 主题") is None
    assert local_command_for(
        "/workflow --reviewer @host --implementer @kimi -- 主题") is None
    assert local_command_for("/steer -- 新约束") is None
    assert local_command_for("/roles delete") is None
    assert local_command_for(" /new ") is not None
    assert local_command_for("/new task") is None
    assert local_command_for("/unknown") is None

    assert unknown_mentions(
        "@kimi @ghost @ghost @codex", (name for name, _ in AGENTS)
    ) == ("ghost",)
    assert unknown_mentions(
        "mail@ghost", (name for name, _ in AGENTS)
    ) == ("ghost",)
    print("ok  候选解析（筛选/多目标/命令边界/未知 agent）")


def test_agent_completion_keyboard_and_focus() -> None:
    async def run() -> None:
        orch = make_orch()
        app = ChatApp(workdir=".", orchestrator=orch)
        async with app.run_test() as pilot:
            box = app.query_one("#composer", ComposerInput)
            assert box.has_focus

            await pilot.press("@")
            await pilot.pause()
            assert app._completion is not None
            assert [item.value for item in app._completion.items] == [
                "@kimi", "@opencode", "@qwen", "@workbuddy", "@codex",
                "@host"]

            # ↑ 从首项循环到末项；↓ 回首项后再选第二项。
            await pilot.press("up")
            await pilot.pause()
            assert app._completion_index == 5
            await pilot.press("down")
            await pilot.pause()
            assert app._completion_index == 0

            # ↓ + Enter 只补全第二项，不提交。
            await pilot.press("down", "enter")
            await pilot.pause()
            assert box.value == "@opencode "
            assert orch.history == []
            assert box.has_focus

            # 第二个 mention 按前缀筛选，Tab 补全且保留第一个目标。
            await pilot.press("@", "c", "o", "tab")
            await pilot.pause()
            assert box.value == "@opencode @codex "
            assert app._completion is None
            assert orch.history == []

            # Esc 关闭候选，不挪走输入焦点。
            await pilot.press("@")
            await pilot.pause()
            assert app._completion is not None
            await pilot.press("escape")
            await pilot.pause()
            assert app._completion is None
            assert box.has_focus

            # 光标离开活动 token 后，Enter 不得使用过期位置强行补全。
            box.value = "@co"
            box.cursor_position = len(box.value)
            await pilot.pause()
            assert app._completion is not None
            await pilot.press("home", "enter")
            await pilot.pause()
            assert box.value == "@co"
            assert app._completion is None
            assert orch.history == []

    asyncio.run(run())
    print("ok  @agent 键盘导航/补全/多目标/Esc/焦点")


def test_slash_commands_unknown_agent_and_submit_behaviour() -> None:
    async def run() -> None:
        orch = make_orch()
        app = ChatApp(workdir=".", orchestrator=orch)
        async with app.run_test() as pilot:
            box = app.query_one("#composer", ComposerInput)

            # 第一次 Enter 补全，第二次 Enter 才执行本地命令。
            box.value = "/ag"
            box.cursor_position = len(box.value)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert box.value == "/agents"
            assert orch.history == []
            await pilot.press("enter")
            await pilot.pause()
            assert box.value == ""
            assert orch.history == []

            # 精确 /discuss 只显示用法；带参数形式才经 CommandBus。
            box.value = "/discuss"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            assert box.value == ""
            assert orch.history == []

            # 未知 agent 阻止提交并保留草稿，方便原地修正。
            box.value = "@ghost 请处理"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            assert box.value == "@ghost 请处理"
            assert orch.history == []
            assert box.has_focus

            # 带参数的 slash 文本不是本地命令，仍进入正常派发路径。
            box.value = "/new task"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert orch.history[0].speaker == "user"
            assert orch.history[0].text == "/new task"

    asyncio.run(run())
    print("ok  /command 补全/本地执行/未知 agent/命令误判")


def test_roles_commands_view_and_clear_without_dispatch() -> None:
    from textual.widgets import RichLog

    async def run() -> None:
        orch = make_orch()
        orch.host.route = HostDecision(
            ["qwen"],
            "设置会话角色",
            tasks={"qwen": "以产品研究员视角分析"},
            role_changes=SessionRoleChanges({
                "qwen": SessionRole("产品研究员", "核对事实并列出未知项。"),
            }, ()),
        )
        await orch.dispatch(
            "让 qwen 在当前会话担任产品研究员",
            lambda _name, _event: None,
        )
        before_history = list(orch.history)
        before_decide_calls = orch.host.decide_calls
        app = ChatApp(workdir=".", orchestrator=orch)
        async with app.run_test() as pilot:
            box = app.query_one("#composer", ComposerInput)
            box.value = "/roles"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            rendered = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines)
            assert "当前会话角色：" in rendered
            assert "@qwen · 产品研究员" in rendered
            assert "核对事实并列出未知项。" in rendered
            assert "清空全部：/roles clear" in rendered
            assert orch.history == before_history
            assert orch.host.decide_calls == before_decide_calls

            box.value = "/roles clear"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            assert orch.session_roles == {}
            assert orch.history == before_history
            assert orch.host.decide_calls == before_decide_calls
            rendered = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines)
            assert "已清空当前会话角色：@qwen" in rendered

            box.value = "/roles"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            rendered = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines)
            assert "当前会话没有设置角色" in rendered

    asyncio.run(run())
    print("ok  /roles 查看与清空均不进入 timeline 或调用 host")


def test_roles_clear_waits_for_current_command_boundary() -> None:
    from textual.widgets import RichLog

    class HangingAdapter:
        session_id = None

        def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
            self.started = started
            self.release = release

        async def stream(self, prompt: str, workdir: str):
            self.started.set()
            await self.release.wait()
            yield AgentEvent("text", "完成")
            yield AgentEvent("done")

    async def run() -> None:
        orch = make_orch()
        orch.host.route = HostDecision(
            ["qwen"],
            "设置会话角色",
            tasks={"qwen": "以产品研究员视角分析"},
            role_changes=SessionRoleChanges({
                "qwen": SessionRole("产品研究员", "核对事实。"),
            }, ()),
        )
        await orch.dispatch(
            "让 qwen 在当前会话担任产品研究员",
            lambda _name, _event: None,
        )
        started = asyncio.Event()
        release = asyncio.Event()
        orch.adapters["qwen"] = HangingAdapter(started, release)
        app = ChatApp(workdir=".", orchestrator=orch)
        async with app.run_test() as pilot:
            box = app.query_one("#composer", ComposerInput)
            box.value = "@qwen 执行长任务"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await asyncio.wait_for(started.wait(), timeout=2)

            box.value = "/roles clear"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            assert orch.session_roles["qwen"].label == "产品研究员"
            rendered = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines)
            assert "当前会话有任务正在运行或排队，结束后再清空角色" \
                in rendered

            release.set()
            await app.workers.wait_for_complete()

    asyncio.run(run())
    print("ok  /roles clear 不跨越运行中 command 边界")


if __name__ == "__main__":
    test_completion_parser_and_command_boundary()
    test_agent_completion_keyboard_and_focus()
    test_slash_commands_unknown_agent_and_submit_behaviour()
    test_roles_commands_view_and_clear_without_dispatch()
    test_roles_clear_waits_for_current_command_boundary()
    print("\nTUI completion 全部通过")

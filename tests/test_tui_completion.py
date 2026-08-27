"""输入候选的纯解析与 Textual 交互验收。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from adapters.base import AgentEvent
from agent_readiness import AgentReadiness, ReadinessState
from main import ChatApp, ComposerInput
from host import HostDecision
from orchestrator import AgentSpec, Orchestrator
from session_roles import SessionRole, SessionRoleChanges
from test_basic import make_orch
from textual.geometry import Region
from textual.widgets import OptionList
from tui_completion import (
    completion_context,
    local_command_for,
    unknown_mentions,
)


AGENTS = (
    ("kimi", "ACP"),
    ("opencode", "ACP+JSONL"),
    ("qwen", "ACP"),
    ("codebuddy", "ACP"),
    ("dsh", "ACP"),
    ("pi", "RPC"),
    ("codex", "APP-SERVER"),
    ("host", "MODERATOR · NATIVE MODEL"),
)


def visible_widget_text(widget: OptionList) -> str:
    viewport = Region(0, 0, widget.outer_size.width, widget.outer_size.height)
    return "\n".join(strip.text for strip in widget.render_lines(viewport))


def test_completion_parser_and_command_boundary() -> None:
    mention = completion_context("@co", 3, AGENTS)
    assert mention is not None
    assert [item.value for item in mention.items] == [
        "@codebuddy", "@codex"]

    multi = completion_context("@kimi @", 7, AGENTS)
    assert multi is not None
    assert "@kimi" not in [item.value for item in multi.items]
    assert [item.value for item in multi.items] == [
        "@opencode", "@qwen", "@codebuddy", "@dsh", "@pi", "@codex",
        "@host"]

    slash = completion_context("/ca", 3, AGENTS)
    assert slash is not None
    assert [item.value for item in slash.items] == ["/cancel"]
    assert completion_context("请看 /ca", 6, AGENTS) is None
    roles = completion_context("/ro", 3, AGENTS)
    assert roles is not None
    assert [item.value for item in roles.items] == ["/roles", "/roles clear"]
    host = completion_context("/host a", 7, AGENTS)
    assert host is not None
    assert [item.value for item in host.items] == ["/host agent"]

    assert local_command_for("/new") is not None
    assert local_command_for("/discuss") is not None
    assert local_command_for("/workflow") is not None
    assert local_command_for("/steer") is not None
    assert local_command_for("/yolo").description == "切换当前会话自动完全授权"
    assert local_command_for("/roles").description == "查看当前会话角色"
    assert local_command_for("/roles clear").description == "清空当前会话角色"
    assert local_command_for("/host").description == "查看当前会话主持后端"
    assert local_command_for("/agents rescan").description == "重新检测本机 agent"
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
                "@kimi", "@opencode", "@qwen", "@codebuddy", "@dsh", "@pi",
                "@codex", "@host"]
            popup = app.query_one("#completion-list", OptionList)
            assert "@dsh" in visible_widget_text(popup)

            # ↑ 从首项循环到末项；↓ 回首项后再选第二项。
            await pilot.press("up")
            await pilot.pause()
            assert app._completion_index == 7
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
            assert box.value == "@opencode @codebuddy "
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

            # 点击候选必须使用鼠标所在项补全，不能沿用键盘旧索引。
            box.value = "@"
            box.cursor_position = len(box.value)
            await pilot.pause()
            popup = app.query_one("#completion-list", OptionList)
            values = {
                popup.get_option_at_index(index).id
                for index in range(popup.option_count)
            }
            await pilot.click("#completion-list", offset=(8, 4))
            await pilot.pause()
            assert app._completion is None
            assert box.value.removesuffix(" ") in values
            assert box.value != "@kimi "
            assert box.has_focus

    asyncio.run(run())
    print("ok  @agent 键盘导航/补全/多目标/Esc/焦点")


def test_agent_completion_scrolls_selected_item_into_view() -> None:
    class Adapter:
        session_id = None

        async def stream(self, _prompt, _workdir):
            yield AgentEvent("done")

        async def aclose(self) -> None:
            return None

    def ready(name: str):
        return lambda: AgentReadiness(
            name,
            ReadinessState.READY,
            "fake ready",
            "setup fake",
            f"/tmp/{name}",
        )

    specs = tuple(
        AgentSpec(
            f"agent{index}",
            "acp-with-long-transport-description",
            Adapter,
            ready(f"agent{index}"),
        )
        for index in range(10)
    )
    orch = Orchestrator(
        ".",
        specs=specs,
        persistent=False,
        discover_agents=True,
        host_probe=ready("host"),
    )

    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=orch)
        try:
            async with app.run_test(size=(44, 24)) as pilot:
                box = app.query_one("#composer", ComposerInput)
                await pilot.press("@")
                await pilot.pause()
                assert app._completion is not None
                assert len(app._completion.items) == 11
                popup = app.query_one("#completion-list", OptionList)
                assert popup.max_scroll_y > 0
                assert "@agent9" not in visible_widget_text(popup)

                await pilot.press("up", "up")
                await pilot.pause()
                assert app._completion_index == 9
                assert popup.highlighted == 9
                assert popup.scroll_y > 0
                assert "@agent9" in visible_widget_text(popup)

                await pilot.press("enter")
                await pilot.pause()
                assert box.value == "@agent9 "
                assert box.has_focus
        finally:
            await orch.aclose()

    asyncio.run(run())
    print("ok  超出弹层高度的 @agent 候选可滚动且选中项始终可见")


def test_unready_dsh_remains_visible_after_ready_candidates() -> None:
    class Adapter:
        session_id = None

        async def stream(self, _prompt, _workdir):
            yield AgentEvent("done")

        async def aclose(self) -> None:
            return None

    def probe(name: str, state: ReadinessState):
        return lambda: AgentReadiness(
            name,
            state,
            "fake readiness",
            "setup fake",
            f"/tmp/{name}" if state is ReadinessState.READY else None,
        )

    transports = (
        ("kimi", "acp+jsonl"),
        ("opencode", "acp+jsonl"),
        ("qwen", "acp"),
        ("codebuddy", "acp"),
        ("dsh", "acp"),
        ("pi", "rpc"),
        ("codex", "app-server"),
    )
    specs = tuple(
        AgentSpec(
            name,
            transport,
            Adapter,
            probe(
                name,
                ReadinessState.NOT_FOUND
                if name == "dsh" else ReadinessState.READY,
            ),
        )
        for name, transport in transports
    )
    orch = Orchestrator(
        ".",
        specs=specs,
        persistent=False,
        discover_agents=True,
        host_probe=probe("host", ReadinessState.READY),
    )

    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=orch)
        try:
            async with app.run_test(size=(120, 32)) as pilot:
                await pilot.press("@")
                await pilot.pause()
                assert app._completion is not None
                assert app._completion.items[-1].value == "@dsh"
                popup = app.query_one("#completion-list", OptionList)
                visible = visible_widget_text(popup)
                assert "@dsh" in visible
                assert "未检测到 CLI" in visible
        finally:
            await orch.aclose()

    asyncio.run(run())
    print("ok  DSH 未就绪且排在最后时仍可见")


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


def test_yolo_is_session_scoped_visible_and_not_dispatched() -> None:
    """/yolo 只切换当前会话内存状态，不进入 timeline 或调用 host。"""
    async def run() -> None:
        from textual.widgets import Static

        orch = make_orch()
        app = ChatApp(workdir=".", orchestrator=orch)
        async with app.run_test() as pilot:
            box = app.query_one("#composer", ComposerInput)

            box.value = "/yolo"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            assert orch.history == []
            assert orch.host.decide_calls == 0
            assert app.auto_approve is True
            assert "YOLO" in app.title
            assert "自动完全授权已开启" in str(
                app.query_one("#task-status", Static).render())

            box.value = "/yolo"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            assert app.auto_approve is False
            assert "YOLO" not in app.title
            assert "自动完全授权已开启" not in str(
                app.query_one("#task-status", Static).render())
            assert orch.history == []

    asyncio.run(run())
    print("ok  /yolo 当前会话切换 + 持续可见 + 不进入 timeline")


def test_agent_readiness_status_rescan_and_draft_preservation() -> None:
    from textual.widgets import RichLog

    class Adapter:
        session_id = None

        async def stream(self, _prompt, _workdir):
            yield AgentEvent("text", "ok")
            yield AgentEvent("done")

    states = {
        "ready": ReadinessState.READY,
        "missing": ReadinessState.NOT_FOUND,
    }

    def probe(name: str):
        return lambda: AgentReadiness(
            name,
            states[name],
            "fake 已找到" if states[name] is ReadinessState.READY
            else "当前进程 PATH 未检测到 fake",
            f"安装 fake {name}",
            f"/tmp/fake-{name}"
            if states[name] is ReadinessState.READY else None,
        )

    specs = (
        AgentSpec("missing", "acp", Adapter, probe("missing")),
        AgentSpec("ready", "jsonl", Adapter, probe("ready")),
    )
    orch = Orchestrator(
        ".",
        specs=specs,
        persistent=False,
        discover_agents=True,
        host_probe=lambda: AgentReadiness(
            "host", ReadinessState.READY, "fake host", "setup host"),
    )

    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=orch)
        async with app.run_test() as pilot:
            box = app.query_one("#composer", ComposerInput)
            completion = app._completion_agents()
            assert [name for name, _ in completion] == [
                "ready", "host", "missing"]
            assert "可用" in completion[0][1]
            assert "未检测到 CLI" in completion[-1][1]

            box.value = "@missing 请处理"
            original_cursor = len("@missing 请")
            box.cursor_position = original_cursor
            await pilot.press("enter")
            await pilot.pause()
            assert box.value == "@missing 请处理"
            assert box.cursor_position == original_cursor
            assert box.has_focus
            assert orch.history == []

            states["missing"] = ReadinessState.READY
            box.value = "/agents rescan"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            assert box.value == ""
            assert orch.history == []
            assert [name for name, _ in app._completion_agents()] == [
                "missing", "ready", "host"]
            rendered = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines)
            assert "Agent 就绪状态" in rendered
            assert "@missing · 可用" in rendered
            assert "@host · 可用 · NATIVE-MODEL" in rendered
            assert "未安装或改动任何 agent" in rendered

    asyncio.run(run())
    print("ok  Agent 状态/重新检测/缺失目标草稿保留")


def test_tui_starts_with_zero_or_all_fake_clis() -> None:
    class Adapter:
        session_id = None

        async def stream(self, _prompt, _workdir):
            yield AgentEvent("done")

    async def scenario(state: ReadinessState, expected: str) -> None:
        created: list[str] = []

        def factory(name: str):
            def build():
                created.append(name)
                return Adapter()
            return build

        def probe(name: str):
            return lambda: AgentReadiness(
                name,
                state,
                "fake ready" if state is ReadinessState.READY
                else "当前进程 PATH 未检测到 fake",
                "安装 fake",
            )

        specs = tuple(
            AgentSpec(name, "acp", factory(name), probe(name))
            for name in ("one", "two")
        )
        orch = Orchestrator(
            ".",
            specs=specs,
            persistent=False,
            discover_agents=True,
            host_probe=probe("host"),
        )
        app = ChatApp(workdir=".", orchestrator=orch)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert expected in "\n".join(
                text for _speaker, text, _style in app._display_lines)
            if state is ReadinessState.READY:
                assert created == ["one", "two"]
            else:
                assert created == []

    async def run() -> None:
        await scenario(ReadinessState.NOT_FOUND, "已就绪 0/3")
        await scenario(ReadinessState.READY, "已就绪 3/3")

    asyncio.run(run())
    print("ok  零 CLI/全 CLI fake 环境均可启动 TUI")


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


def test_host_command_switches_locally_without_timeline_write() -> None:
    from textual.widgets import RichLog

    class Adapter:
        session_id = None

        async def stream(self, _prompt, _workdir, **_kwargs):
            yield AgentEvent("done")

        async def aclose(self):
            return None

    specs = (AgentSpec(
        "codex", "app-server", Adapter, None, Adapter, None),)
    orch = Orchestrator(
        ".", specs=specs, persistent=False,
        host_model_factory=lambda _selection: Adapter())

    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=orch)
        async with app.run_test() as pilot:
            box = app.query_one("#composer", ComposerInput)
            box.value = "/host"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            assert orch.history == []

            box.value = "/host agent codex"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            assert orch.host_backend_selection.kind == "agent"
            assert orch.host_backend_selection.target == "codex"
            assert orch.history == []
            rendered = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines)
            assert "当前会话 Host" in rendered
            assert "Host 已切换：agent:codex" in rendered

    asyncio.run(run())
    print("ok  /host 查看与切换均为会话本地命令且不进 timeline")


if __name__ == "__main__":
    test_completion_parser_and_command_boundary()
    test_agent_completion_keyboard_and_focus()
    test_agent_completion_scrolls_selected_item_into_view()
    test_unready_dsh_remains_visible_after_ready_candidates()
    test_slash_commands_unknown_agent_and_submit_behaviour()
    test_yolo_is_session_scoped_visible_and_not_dispatched()
    test_agent_readiness_status_rescan_and_draft_preservation()
    test_tui_starts_with_zero_or_all_fake_clis()
    test_roles_commands_view_and_clear_without_dispatch()
    test_roles_clear_waits_for_current_command_boundary()
    test_host_command_switches_locally_without_timeline_write()
    print("\nTUI completion 全部通过")

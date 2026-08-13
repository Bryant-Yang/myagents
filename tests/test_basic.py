"""不依赖真实 agent CLI 的测试：路由、编排、TUI。

运行：.venv/bin/python tests/test_basic.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.base import AgentEvent, stream_jsonl
from host import HostAgent, HostDecision
from orchestrator import Message, Orchestrator


class FakeAdapter:
    """假 agent：不回 CLI，直接吐固定事件，用来测编排逻辑。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.session_id = None
        self.last_prompt: str | None = None

    async def stream(self, prompt: str, workdir: str):
        self.last_prompt = prompt
        yield AgentEvent("text", f"{self.name} 收到")
        yield AgentEvent("done")


class FakeHost(FakeAdapter):
    """假主持人：decide() 返回预设路由，stream() 行为和工人一样。"""

    def __init__(self) -> None:
        super().__init__("host")
        self.decide_calls = 0
        self.route = HostDecision(["kimi"], "测试路由")

    async def decide(
        self, transcript: str, workdir: str, on_event=None, *, choices=None,
    ):
        self.decide_calls += 1
        return self.route


def make_orch() -> Orchestrator:
    orch = Orchestrator(workdir=".", persistent=False)
    orch.adapters = {
        n: FakeAdapter(n)
        for n in ("kimi", "opencode", "qwen", "workbuddy", "codex")
    }
    orch.host = FakeHost()
    orch.adapters["host"] = orch.host
    return orch


def test_parse_mentions() -> None:
    orch = make_orch()
    assert orch.parse_mentions("@kimi 看看这个") == ["kimi"]
    assert orch.parse_mentions("@kimi @opencode 比比谁快") == ["kimi", "opencode"]
    assert orch.parse_mentions("@kimi @kimi 重复只算一次") == ["kimi"]
    assert orch.parse_mentions("@qwen 看看这个") == ["qwen"]
    assert orch.parse_mentions("@workbuddy 看看这个") == ["workbuddy"]
    assert orch.parse_mentions("@nobody 不存在") == []
    assert orch.parse_mentions("没有提到任何人") == []
    print("ok  parse_mentions")


def test_dispatch() -> None:
    orch = make_orch()
    events = []
    asyncio.run(orch.dispatch("@kimi 你好", lambda n, e: events.append((n, e))))

    kimi = orch.adapters["kimi"]
    assert "你好" in kimi.last_prompt  # 用户消息进了 prompt
    # history：user 一条 + kimi 回复一条
    assert [(m.speaker) for m in orch.history] == ["user", "kimi"]
    assert orch.history[1].text == "kimi 收到"
    # 事件流里有 text 和 done
    kinds = [e.kind for _, e in events]
    assert "text" in kinds and "done" in kinds
    print("ok  dispatch 单 agent")


def test_dispatch_multi_and_transcript() -> None:
    orch = make_orch()
    asyncio.run(orch.dispatch("@kimi 第一句", lambda n, e: None))
    asyncio.run(orch.dispatch("@opencode 继续", lambda n, e: None))
    # 第二个 agent 的 prompt 里应包含第一轮对话（transcript 转发）
    prompt = orch.adapters["opencode"].last_prompt
    assert "第一句" in prompt and "kimi 收到" in prompt
    # 显式 @ 不会触发 host 路由
    assert orch.host.decide_calls == 0
    print("ok  多 agent + transcript 转发 + 显式 @ 绕过路由")


def test_host_routing() -> None:
    """无 @ 消息 → host 路由到预设目标。"""
    orch = make_orch()
    orch.host.route = HostDecision(
        ["kimi"],
        "测试路由",
        tasks={"kimi": "自行构思一个极简小游戏并直接实现，不要等待用户补充方案。"},
    )
    events = []
    asyncio.run(orch.dispatch("帮我看看这段代码", lambda n, e: events.append((n, e))))
    assert orch.host.decide_calls == 1
    # 路由信息作为 host 的 info 事件出现
    assert any(n == "host" and e.kind == "info" and "路由" in e.text for n, e in events)
    # kimi 被派发并回复
    assert [m.speaker for m in orch.history] == ["user", "kimi"]
    prompt = orch.adapters["kimi"].last_prompt
    assert "主持人本轮明确委托" in prompt
    assert "自行构思一个极简小游戏并直接实现" in prompt
    assert "不要把任务退回给主持人或用户" in prompt
    print("ok  host 路由（无 @ → 携带明确任务派给 kimi）")


def test_host_routing_assignment_reaches_stateful_agent_and_has_fallback() -> None:
    """真实 Kimi 所在的 stateful 路径也收到 assignment；旧 host JSON 不丢任务。"""
    class StatefulFakeAdapter(FakeAdapter):
        stateful_session = True

    orch = make_orch()
    orch.adapters["kimi"] = StatefulFakeAdapter("kimi")
    orch.host.route = HostDecision(["kimi"], "旧格式未提供 tasks")

    asyncio.run(orch.dispatch(
        "你构思一个极简小游戏，让 kimi 实现",
        lambda n, e: None,
    ))

    prompt = orch.adapters["kimi"].last_prompt
    assert "主持人本轮明确委托" in prompt
    assert "构思、规划、实现或验证" in prompt
    assert "你构思一个极简小游戏，让 kimi 实现" in prompt
    assert "不要把任务退回给主持人或用户" in prompt
    print("ok  stateful 委托透传 + host 旧格式任务回退")


def test_host_direct_answer_single_call() -> None:
    """任意语言的闲聊由 host 在判断调用中直接回答，不写死问候词。"""
    orch = make_orch()
    orch.host.route = HostDecision([], "host 直接回答", "Hi，我在。")
    events = []
    asyncio.run(orch.dispatch("hi", lambda n, e: events.append((n, e))))

    assert orch.host.decide_calls == 1
    assert orch.host.last_prompt is None  # 没有第二次调用 host.stream()
    assert [m.speaker for m in orch.history] == ["user", "host"]
    assert orch.history[-1].text == "Hi，我在。"
    assert any(n == "host" and e.kind == "text" for n, e in events)
    assert any(n == "host" and e.kind == "done" for n, e in events)
    print("ok  host 直接回答（自然语言不写死 + 单次 LLM）")


def test_host_answers_itself() -> None:
    """host 的判断调用直接产出答案，不再做第二次主持人调用。"""
    orch = make_orch()
    orch.host.route = HostDecision([], "闲聊自己答", "这是我的看法")
    asyncio.run(orch.dispatch("你怎么看", lambda n, e: None))
    assert [m.speaker for m in orch.history] == ["user", "host"]
    assert orch.history[-1].text == "这是我的看法"
    assert orch.host.decide_calls == 1 and orch.host.last_prompt is None
    print("ok  host 判断与回答合并为一次调用")


def test_at_host_explicit() -> None:
    """@host 显式点名：直接派发，不走路由。"""
    orch = make_orch()
    asyncio.run(orch.dispatch("@host 总结一下", lambda n, e: None))
    assert orch.host.decide_calls == 0
    assert [m.speaker for m in orch.history] == ["user", "host"]
    print("ok  @host 显式点名（绕过路由）")


def test_transcript_snapshot() -> None:
    """P1 回归：host 路由等待期间提交新消息，第一条任务的 prompt 不串话。"""
    orch = make_orch()
    gate = asyncio.Event()

    async def run() -> None:
        async def decide_slow(
            transcript: str, workdir: str, on_event=None, *, choices=None,
        ):
            await gate.wait()  # 模拟 codex 路由要几秒钟
            return HostDecision(["kimi"], "慢路由")
        orch.host.decide = decide_slow

        t1 = asyncio.create_task(orch.dispatch("第一条消息", lambda n, e: None))
        await asyncio.sleep(0)  # 让 dispatch1 跑到 decide 的 await
        await orch.dispatch("@kimi 第二条消息", lambda n, e: None)  # 先跑完
        gate.set()
        await t1

        # dispatch1 的 prompt 用的是快照：有第一条，没有第二条及其回复
        prompt = orch.adapters["kimi"].last_prompt
        assert "第一条消息" in prompt
        assert "第二条消息" not in prompt

    asyncio.run(run())
    print("ok  transcript 快照（并发不串话）")


def test_decide_failure_fallback() -> None:
    """P2 回归：host 路由抛异常 → error 事件 + 确定性回退到第一个工人。"""
    orch = make_orch()

    async def boom(
        transcript: str, workdir: str, on_event=None, *, choices=None,
    ):
        raise RuntimeError("codex 挂了")
    orch.host.decide = boom

    events = []
    asyncio.run(orch.dispatch("随便一条消息", lambda n, e: events.append((n, e))))
    assert any(n == "host" and e.kind == "error" and "处理失败" in e.text
               for n, e in events)
    # 回退到第一个工人 kimi，而不是可能同样故障的 host
    assert [m.speaker for m in orch.history] == ["user", "kimi"]
    print("ok  decide 异常兜底（error 事件 + 回退第一个工人）")


def test_persistent_failure() -> None:
    """P2 契约回归：路由和工人持续故障时，history 诚实记录"调用失败"。"""
    orch = make_orch()

    async def boom(
        transcript: str, workdir: str, on_event=None, *, choices=None,
    ):
        raise RuntimeError("codex 挂了")
    orch.host.decide = boom

    class FailingAdapter(FakeAdapter):
        async def stream(self, prompt: str, workdir: str):
            raise RuntimeError("kimi 也挂了")
            yield  # pragma: no cover - 让函数保持 async generator
    orch.adapters["kimi"] = FailingAdapter("kimi")

    events = []
    asyncio.run(orch.dispatch("随便一条消息", lambda n, e: events.append((n, e))))
    kinds = [(n, e.kind) for n, e in events]
    assert ("host", "error") in kinds and ("kimi", "error") in kinds
    last = orch.history[-1]
    assert last.speaker == "kimi" and "调用失败" in last.text
    assert "无文本回复" not in last.text
    print("ok  持续失败契约（error 事件 + 诚实的 history）")


def test_route_dedupe() -> None:
    """P2 回归：目标去重；只保留目标对应的有界明确任务。"""
    host = HostAgent(workers=["kimi", "opencode"])
    choices = ["kimi", "opencode"]
    decision = host._parse(
        '{"targets": ["kimi", "kimi", "nobody"], "reason": "x",'
        ' "tasks": {"kimi": "直接实现小游戏", "nobody": "忽略"}}',
        choices)
    assert decision.targets == ["kimi"] and decision.answer is None
    assert decision.tasks == {"kimi": "直接实现小游戏"}
    decision = host._parse(
        '{"targets": ["opencode", "kimi", "opencode"]}', choices)
    assert decision.targets == ["opencode", "kimi"]
    assert decision.tasks == {}
    decision = host._parse("这是主持人的直接回答", choices)
    assert decision.targets == [] and decision.answer == "这是主持人的直接回答"
    assert "禁止调用工具" in host._build_route_prompt("对话", choices)
    print("ok  路由目标去重 + 明确任务解析 + host 路由禁用工具")


def test_host_decide_surfaces_safe_progress_events() -> None:
    """host 路由期间的安全阶段/工具事件不能在后台被吞掉。"""
    class RoutingAdapter:
        session_id = None

        async def stream(self, prompt: str, workdir: str):
            yield AgentEvent("status", "正在分析")
            yield AgentEvent("tool", "不应调用但必须可见")
            yield AgentEvent(
                "text",
                '{"targets":["kimi"],"reason":"实现",'
                '"tasks":{"kimi":"直接实现并验证"}}',
            )
            yield AgentEvent("done")

    host = HostAgent(adapter=RoutingAdapter(), workers=["kimi"])
    progress = []
    decision = asyncio.run(host.decide(
        "[user] 做一个游戏", ".", progress.append))
    assert [event.kind for event in progress] == ["status", "tool"]
    assert decision.tasks == {"kimi": "直接实现并验证"}
    print("ok  host 路由安全进度可见（不再后台吞事件）")


def test_host_progress_sink_failure_propagates() -> None:
    """host 进度写盘失败必须停止派发，不能伪装成路由失败继续执行。"""
    class ProgressHost(FakeHost):
        async def decide(
            self, transcript: str, workdir: str, on_event=None, *, choices=None,
        ):
            assert on_event is not None
            on_event(AgentEvent("status", "内部进度"))
            return HostDecision(["kimi"], "不应继续")

    orch = make_orch()
    orch.host = ProgressHost()
    orch.adapters["host"] = orch.host

    def sink(name: str, event: AgentEvent) -> None:
        if name == "host" and event.text == "内部进度":
            raise OSError("events disk full")

    with _Raises(OSError):
        asyncio.run(orch.dispatch("执行任务", sink))
    assert orch.adapters["kimi"].last_prompt is None
    assert [message.speaker for message in orch.history] == ["user"]
    print("ok  host 进度 sink 失败穿透（不继续派发）")


def test_stderr_backpressure() -> None:
    """P1 回归：子进程 stderr 写 200KB（远超 64KB 管道缓冲）不死锁。"""
    script = (
        'import sys;'
        'sys.stdout.write("{\\"a\\":1}\\n");sys.stdout.flush();'
        'sys.stderr.write("x"*200000);sys.stderr.flush();'
        'sys.stdout.write("{\\"b\\":2}\\n");sys.stdout.flush()'
    )

    async def run() -> None:
        lines = [line async for line in stream_jsonl([sys.executable, "-c", script], ".")]
        assert lines == ['{"a":1}', '{"b":2}']

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    print("ok  stderr 背压（200KB 不死锁）")


def test_kill_on_cancel() -> None:
    """P1 回归：取消消费方时，整个进程组（含孙进程）被终止，不留孤儿。"""
    pidfile = "/tmp/myagents_test_pid"
    if os.path.exists(pidfile):
        os.remove(pidfile)
    # 子进程先 spawn 一个孙进程（继承管道），再自己 sleep
    script = (
        'import os,sys,subprocess,time;'
        'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);'
        f'open("{pidfile}","w").write(f"{{os.getpid()}} {{p.pid}}");'
        'time.sleep(60)'
    )

    async def run() -> None:
        async def consume() -> None:
            async for _ in stream_jsonl([sys.executable, "-c", script], "."):
                pass
        task = asyncio.create_task(consume())
        await asyncio.sleep(1)  # 等子进程启动并写下两个 pid
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        with open(pidfile) as f:
            child_pid, grandchild_pid = map(int, f.read().split())
        await asyncio.sleep(0.5)
        for pid, label in ((child_pid, "子进程"), (grandchild_pid, "孙进程")):
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            assert not alive, f"{label} {pid} 未被终止"

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    print("ok  取消即杀整个进程组（子+孙都不留）")


def test_bounded_stderr() -> None:
    """P2 回归：stderr 持续排空但内存有界，报错保留首段+尾段。"""
    from adapters.base import BoundedLog

    # 单元层面：BoundedLog 本身
    log = BoundedLog(limit=100)
    log.append("H" * 100)
    log.append("m" * 10000)
    log.append("T" * 100)
    rendered = log.render()
    assert len(rendered) < 300
    assert rendered.startswith("H" * 100) and rendered.endswith("T" * 100)
    assert "省略" in rendered

    # 边界回归（Codex review）：limit < total <= 2*limit 时 tail 不得丢失。
    # limit=5：长度 5=正好装满、6=多 1、10=正好 2 倍、11=超出 2 倍。
    cases = {
        "abcde": "abcde",            # 5
        "abcdef": "abcdef",          # 6
        "abcdefghij": "abcdefghij",  # 10
    }
    for text, expected in cases.items():
        log = BoundedLog(limit=5)
        log.append(text)
        assert log.render() == expected, f"{len(text)} 字符：{log.render()!r}"
    log = BoundedLog(limit=5)      # 11：head 5 + 省略 1 + tail 5
    log.append("abcdefghijk")
    r11 = log.render()
    assert r11.startswith("abcde") and r11.endswith("ghijk") and "省略 1" in r11

    # 集成层面：子进程灌 200KB stderr 并非零退出，异常消息有界且含首尾
    script = (
        'import sys;'
        'sys.stderr.write("HEAD-"+"x"*200000+"-TAIL");sys.stderr.flush();'
        'sys.exit(3)'
    )

    async def run() -> None:
        with _Raises(RuntimeError) as ctx:
            async for _ in stream_jsonl([sys.executable, "-c", script], "."):
                pass
        msg = str(ctx.exc)
        assert len(msg) < 5000, f"异常消息无界：{len(msg)} 字符"
        assert "HEAD-" in msg and "-TAIL" in msg and "省略" in msg

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    print("ok  stderr 有界（200KB → 首尾各留一段）")


class _Raises:
    """极简 with 断言（不想为这一个用例引入 pytest 依赖）。"""

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


def test_tui() -> None:
    from textual.widgets import Input, RichLog
    from main import ChatApp

    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())  # 假 agent，不发真实请求
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(Input)
            box.value = "不点名的消息"
            await pilot.press("enter")
            await pilot.pause()
            box.value = "@kimi 你好"
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            rendered = [
                str(line.text) for line in app.query_one(RichLog).lines
            ]
            assert len([
                line for line in rendered
                if line.startswith("[activity] ")
            ]) == 2
            assert "[kimi] kimi 收到" in rendered
            await pilot.press("ctrl+g", "up", "enter")
            await pilot.pause()
            expanded = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines
            )
            assert "路由 → kimi" in expanded

    asyncio.run(run())
    print("ok  TUI（Textual pilot）")


def test_qwen_has_distinct_tui_color() -> None:
    """新 worker 不应退化为未知 speaker 的默认白色。"""
    from main import ChatApp

    rendered = ChatApp._line("qwen", "收到")
    assert rendered.spans[0].style == "bold bright_blue"
    print("ok  Qwen TUI speaker 颜色")


def test_tui_coalesces_stream_chunks() -> None:
    """一条流式回复的 token/chunk 不应各占一行。"""
    from textual.widgets import Input, RichLog
    from main import ChatApp

    class ChunkAdapter(FakeAdapter):
        async def stream(self, prompt: str, workdir: str):
            for chunk in ("我", "来", "处理", "。"):
                yield AgentEvent("text", chunk)
            yield AgentEvent("done")

    async def run() -> None:
        orch = make_orch()
        orch.adapters["kimi"] = ChunkAdapter("kimi")
        app = ChatApp(workdir=".", orchestrator=orch)
        async with app.run_test() as pilot:
            box = app.query_one(Input)
            box.value = "@kimi 测试流式显示"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            kimi_lines = [
                str(line.text) for line in app.query_one(RichLog).lines
                if str(line.text).startswith("[kimi] ")
            ]
            assert kimi_lines == ["[kimi] 我来处理。"], kimi_lines

    asyncio.run(run())
    print("ok  TUI 合并同一回复的流式 chunk")


def test_tui_renders_agent_markdown_without_visible_delimiters() -> None:
    """Agent 的常用 Markdown 应转成终端样式，而不是泄露控制符。"""
    from textual.widgets import Input, RichLog
    from main import ChatApp
    from tui_markdown import render_chat_markdown

    class MarkdownAdapter(FakeAdapter):
        async def stream(self, prompt: str, workdir: str):
            yield AgentEvent("text", "**共识：** 保留探索，使用 `KPI` 验证。\n")
            yield AgentEvent("text", "## 关键分歧\n- 供给侧\n> 玩家侧")
            yield AgentEvent("done")

    async def run() -> None:
        orch = make_orch()
        orch.adapters["kimi"] = MarkdownAdapter("kimi")
        app = ChatApp(workdir=".", orchestrator=orch)
        async with app.run_test() as pilot:
            box = app.query_one(Input)
            box.value = "@kimi 测试 Markdown 显示"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            lines = [
                line.text for line in app.query_one(RichLog).lines
                if str(line.text).startswith("[kimi] ")
                or str(line.text).startswith("关键分歧")
                or str(line.text).startswith("• ")
                or str(line.text).startswith("│ ")
            ]
            plain = "\n".join(str(line) for line in lines)
            assert "**" not in plain and "`" not in plain and "##" not in plain
            assert "[kimi] 共识： 保留探索，使用 KPI 验证。" in plain
            assert "关键分歧\n• 供给侧\n│ 玩家侧" in plain
            rendered = render_chat_markdown(
                "**共识：** 保留探索，使用 `KPI` 验证。")
            styles = {str(span.style) for span in rendered.spans}
            assert "bold" in styles
            assert "bold cyan on grey15" in styles
            chinese_emphasis = render_chat_markdown(
                "这是**重点**内容，中文*强调*中文。")
            assert chinese_emphasis.plain == "这是重点内容，中文强调中文。"
            assert [str(span.style) for span in chinese_emphasis.spans] == [
                "bold", "italic"]

            fenced = render_chat_markdown(
                "```python\nprint('hello')\n```\n**尚未结束")
            assert fenced.plain == "print('hello')\n**尚未结束"
            assert "cyan on grey15" in {
                str(span.style) for span in fenced.spans
            }
            incomplete_fence = render_chat_markdown(
                "回答前缀\n```python\nprint('still streaming')")
            assert incomplete_fence.plain == (
                "回答前缀\n```python\nprint('still streaming')")
            assert "cyan on grey15" not in {
                str(span.style) for span in incomplete_fence.spans
            }
            assert ChatApp._line(
                "user", "**保持原样**").plain == "[user] **保持原样**"
            assert ChatApp._line(
                "system", "`状态` 保持原样").plain == "[system] `状态` 保持原样"
            prose_with_code_names = render_chat_markdown(
                "实现 __init__，计算 2 * 3 * 4")
            assert prose_with_code_names.plain == "实现 __init__，计算 2 * 3 * 4"
            for source, expected in (
                (r"\*literal\*", "*literal*"),
                (r"\**literal\**", "**literal**"),
                (r"\`literal\`", "`literal`"),
                ("src/*/test*", "src/*/test*"),
                ("glob **/*.py", "glob **/*.py"),
                ("a*b*c", "a*b*c"),
                ("https://example.test/*/docs*", "https://example.test/*/docs*"),
            ):
                technical = render_chat_markdown(source)
                assert technical.plain == expected
                assert not technical.spans

    asyncio.run(run())
    print("ok  TUI 常用 Markdown 转成终端样式")


def test_tui_reuses_rendered_markdown_during_stream_redraw() -> None:
    """流式增长只能重算活动回复，不能反复解析全部历史。"""
    from textual.widgets import RichLog
    import main
    from main import ChatApp

    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
            original = main.render_chat_markdown
            rendered_values: list[str] = []

            def counted(value: str, style: str = ""):
                rendered_values.append(value)
                return original(value, style)

            main.render_chat_markdown = counted
            try:
                old_reply = "**历史结论** " + "x" * 200_000
                app._write("kimi", old_reply)
                app._buffer_stream_text("opencode", "**新")
                app._flush_stream_text("opencode")
                app._buffer_stream_text("opencode", "回复**")
                app._flush_stream_text("opencode")
                await pilot.pause()
            finally:
                main.render_chat_markdown = original

            assert rendered_values.count(old_reply) == 1
            assert rendered_values[-2:] == ["**新", "**新回复**"]
            plain = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines)
            assert "[opencode] 新回复" in plain

    asyncio.run(run())
    print("ok  TUI 流式重绘复用历史 Markdown")


def test_tui_coalesces_heartbeat_and_avoids_false_success_copy() -> None:
    """heartbeat 折叠进一张活动卡；done 不伪装任务验收。"""
    from textual.widgets import RichLog
    from main import ChatApp

    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
            meta = {"command_id": "cmd-1", "heartbeat": True}
            app._on_agent_event(
                "system", AgentEvent("status", "host 正在路由（已等待 10 秒）", meta))
            app._on_agent_event(
                "system", AgentEvent("status", "host 正在路由（已等待 20 秒）", meta))
            app._on_agent_event("kimi", AgentEvent(
                "done", meta={"command_id": "cmd-1"}))
            await pilot.pause()
            lines = [
                str(line.text) for line in app.query_one(RichLog).lines
            ]
            activity = [
                line for line in lines if line.startswith("[activity] ")
            ]
            assert len(activity) == 1, activity
            assert not [line for line in lines if "已等待 20 秒" in line]
            app.action_toggle_details()
            expanded = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines
            )
            assert "host 正在路由（已等待 20 秒）" in expanded
            assert "本轮响应结束" in expanded
            assert "kimi 完成" not in expanded

    asyncio.run(run())
    print("ok  TUI 合并 heartbeat + done 不伪装任务成功")


def test_tui_updates_one_activity_card_per_command() -> None:
    """工具状态原位更新，完成后仍可展开历史活动卡。"""
    from textual.widgets import RichLog
    from main import ChatApp

    async def run() -> None:
        app = ChatApp(workdir=".", orchestrator=make_orch())
        async with app.run_test() as pilot:
            render_count = 0
            original_render = app._render_display_lines

            def counted_render() -> None:
                nonlocal render_count
                render_count += 1
                original_render()

            app._render_display_lines = counted_render
            base = {
                "command_id": "cmd-tool",
                "tool_call_id": "tool-1",
                "command": "node --check demo.js",
            }
            app._on_agent_event(
                "kimi", AgentEvent("tool", "检查 JavaScript", dict(base)))
            for _ in range(50):
                app._on_agent_event(
                    "kimi",
                    AgentEvent(
                        "tool", "检查 JavaScript",
                        {**base, "status": "in_progress", "update": True},
                    ),
                )
            app._on_agent_event(
                "kimi",
                AgentEvent(
                    "tool", "检查 JavaScript",
                    {**base, "status": "completed", "update": True},
                ),
            )
            await pilot.pause()
            logical = [
                text for _speaker, text, _style in app._display_lines
                if _speaker == "activity"
            ]
            assert len(logical) == 1, logical
            assert "node --check demo.js" not in logical[0], logical
            assert "/details" in logical[0], logical
            assert render_count == 3, render_count
            app.action_toggle_details()
            logical = [
                text for _speaker, text, _style in app._display_lines
                if _speaker == "activity"
            ]
            assert "node --check demo.js" in logical[0], logical
            lines = [
                str(line.text) for line in app.query_one(RichLog).lines
                if "检查 JavaScript" in str(line.text)
            ]
            assert len([
                line for line in app.query_one(RichLog).lines
                if str(line.text).startswith("[activity] ")
            ]) == 1
            assert "已完成" in "\n".join(lines), lines
            assert render_count == 4, render_count
            app._on_agent_event(
                "kimi", AgentEvent(
                    "done", meta={"command_id": "cmd-tool"}))
            after_done = [
                text for speaker, text, _style in app._display_lines
                if speaker == "activity"
            ]
            assert len(after_done) == 1
            assert "本轮响应结束" in after_done[0]

    asyncio.run(run())
    print("ok  TUI 工具状态原位更新")


def test_main_loop_reopens_requested_session() -> None:
    """顶层只启动一个 App；会话切换由 App 内 SessionManager 完成。"""
    from main import parse_args, run_chat_loop

    calls = []

    class FakeApp:
        def __init__(self, workdir: str, *, session_name: str) -> None:
            calls.append((workdir, session_name))

        def run(self):
            return None

    args = parse_args(["--session", "review", "/tmp"])
    assert (args.workdir, args.session) == ("/tmp", "review")
    run_chat_loop("/tmp", "default", app_factory=FakeApp)
    assert calls == [
        ("/tmp", "default"),
    ]
    print("ok  顶层单 App 启动 + --session 解析")


def test_cli_room_busy_is_actionable_without_traceback() -> None:
    """重复打开同一会话时，CLI 给出短提示与可执行替代方案。"""
    from main import run_cli
    from storage.store import RoomBusyError

    def busy_runner(workdir: str, session_name: str) -> None:
        raise RoomBusyError("房间已被其他进程持有（owner PID=123）")

    try:
        run_cli(
            ["--session", "default", "/tmp/project with spaces"],
            runner=busy_runner,
        )
        raise AssertionError("RoomBusyError 应转换为干净的 CLI 退出")
    except SystemExit as exc:
        message = str(exc)
        assert "Traceback" not in message
        assert "会话 'default' 已在另一个 TUI 中运行" in message
        assert "owner PID=123" in message
        assert "uv run main.py --session <新名称> '/tmp/project with spaces'" \
            in message

    print("ok  CLI 房间冲突短提示（无 traceback + 可执行替代方案）")


if __name__ == "__main__":
    test_parse_mentions()
    test_dispatch()
    test_dispatch_multi_and_transcript()
    test_host_routing()
    test_host_routing_assignment_reaches_stateful_agent_and_has_fallback()
    test_host_direct_answer_single_call()
    test_host_answers_itself()
    test_at_host_explicit()
    test_transcript_snapshot()
    test_decide_failure_fallback()
    test_persistent_failure()
    test_route_dedupe()
    test_host_decide_surfaces_safe_progress_events()
    test_host_progress_sink_failure_propagates()
    test_stderr_backpressure()
    test_kill_on_cancel()
    test_bounded_stderr()
    test_tui()
    test_qwen_has_distinct_tui_color()
    test_tui_coalesces_stream_chunks()
    test_tui_renders_agent_markdown_without_visible_delimiters()
    test_tui_reuses_rendered_markdown_during_stream_redraw()
    test_tui_coalesces_heartbeat_and_avoids_false_success_copy()
    test_tui_updates_one_activity_card_per_command()
    test_main_loop_reopens_requested_session()
    test_cli_room_busy_is_actionable_without_traceback()
    print("\n全部通过")

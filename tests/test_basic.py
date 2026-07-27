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

    async def decide(self, transcript: str, workdir: str):
        self.decide_calls += 1
        return self.route


def make_orch() -> Orchestrator:
    orch = Orchestrator(workdir=".", persistent=False)
    orch.adapters = {n: FakeAdapter(n) for n in ("kimi", "opencode", "codex")}
    orch.host = FakeHost()
    orch.adapters["host"] = orch.host
    return orch


def test_parse_mentions() -> None:
    orch = make_orch()
    assert orch.parse_mentions("@kimi 看看这个") == ["kimi"]
    assert orch.parse_mentions("@kimi @opencode 比比谁快") == ["kimi", "opencode"]
    assert orch.parse_mentions("@kimi @kimi 重复只算一次") == ["kimi"]
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
    events = []
    asyncio.run(orch.dispatch("帮我看看这段代码", lambda n, e: events.append((n, e))))
    assert orch.host.decide_calls == 1
    # 路由信息作为 host 的 info 事件出现
    assert any(n == "host" and e.kind == "info" and "路由" in e.text for n, e in events)
    # kimi 被派发并回复
    assert [m.speaker for m in orch.history] == ["user", "kimi"]
    print("ok  host 路由（无 @ → 派给 kimi）")


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
        async def decide_slow(transcript: str, workdir: str):
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

    async def boom(transcript: str, workdir: str):
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

    async def boom(transcript: str, workdir: str):
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
    """P2 回归：LLM 返回重复/非法目标时按序去重并过滤。"""
    host = HostAgent(workers=["kimi", "opencode"])
    choices = ["kimi", "opencode"]
    decision = host._parse(
        '{"targets": ["kimi", "kimi", "nobody"], "reason": "x"}', choices)
    assert decision.targets == ["kimi"] and decision.answer is None
    decision = host._parse(
        '{"targets": ["opencode", "kimi", "opencode"]}', choices)
    assert decision.targets == ["opencode", "kimi"]
    decision = host._parse("这是主持人的直接回答", choices)
    assert decision.targets == [] and decision.answer == "这是主持人的直接回答"
    print("ok  路由目标去重 + 非 JSON 作为 host 直接回答")


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
            box.value = "不带@的消息"
            await pilot.press("enter")
            await pilot.pause()
            box.value = "@kimi 你好"
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            lines = "\n".join(str(line.text) for line in app.query_one(RichLog).lines)
            assert "host 处理中" in lines          # 无 @ → host 判断或回答
            assert "路由 → kimi" in lines          # 路由结果提示
            assert "[kimi] kimi 收到" in lines

    asyncio.run(run())
    print("ok  TUI（Textual pilot）")


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


if __name__ == "__main__":
    test_parse_mentions()
    test_dispatch()
    test_dispatch_multi_and_transcript()
    test_host_routing()
    test_host_direct_answer_single_call()
    test_host_answers_itself()
    test_at_host_explicit()
    test_transcript_snapshot()
    test_decide_failure_fallback()
    test_persistent_failure()
    test_route_dedupe()
    test_stderr_backpressure()
    test_kill_on_cancel()
    test_bounded_stderr()
    test_tui()
    test_tui_coalesces_stream_chunks()
    print("\n全部通过")

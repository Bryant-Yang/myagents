"""Codex app-server client/adapter 契约测试：全部走 fake server，不发真实请求。

这是 M4 的 contract tests，面向生产接口（编写时生产代码尚不存在，预期 red）：

    from codex_app_server.client import CodexAppServerClient
    from codex_app_server.adapter import CodexAppServerAdapter

公共契约面（与 tests/fake_codex_app_server.py 的行为约定配套）：

- CodexAppServerClient(cmd: list[str])
    await client.start(workdir)                    # spawn + initialize + initialized
    await client.thread_start(workdir) -> str      # thread/start，返回 thread id
    await client.turn_start(thread_id, text) -> str  # turn/start，返回 turn id
    await client.turn_interrupt(thread_id, turn_id)  # turn/interrupt
    await client.next_event() -> dict              # 下一条 server 通知
    await client.aclose()                          # 回收子进程，无残留

- CodexAppServerAdapter(cmd: list[str])（对齐 AgentAdapter 契约）
    adapter.stream(prompt, workdir) -> AsyncIterator[AgentEvent]
    await adapter.aclose()

验收面（只通过公开 API、AgentEvent 和 fake 状态文件观察，不碰私有字段）：
1. fake 讲 app-server V2 的 JSONL-over-stdio，无 "jsonrpc" 头（fake 对违规
   记 VIOLATION:jsonrpc，测试断言状态文件干净）
2. start 后依次 initialize → initialized；同一进程内 thread/start 一次、
   连续两次 turn/start，能收到 item/agentMessage/delta 与 turn/completed
3. 两轮复用同一 thread 与同一 app-server PID（状态文件独立证明）
4. adapter 映射：agent message delta → AgentEvent("text")；command/file/
   tool item → 安全的 tool/status 事件（脱敏、不含 reasoning 正文）；
   turn/completed → done
5. server 反向审批请求（item/commandExecution/requestApproval）默认拒绝
6. 取消 async generator → turn/interrupt，等待 terminal 后才允许下一轮
7. server 断开时 pending request 立即失败；aclose 回收 fake server

运行：.venv/bin/python tests/test_codex_app_server.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.base import AgentEvent  # noqa: E402
from codex_app_server.adapter import CodexAppServerAdapter  # noqa: E402
from codex_app_server.client import (  # noqa: E402
    CodexAppServerClient,
    CodexAppServerRequestUncertain,
)
from control import CommandBus  # noqa: E402
from orchestrator import AgentSpec, Orchestrator  # noqa: E402
from storage.store import RoomStore  # noqa: E402

SERVER = str(Path(__file__).parent / "fake_codex_app_server.py")
CMD = [sys.executable, SERVER]
STATE = "/tmp/myagents_fake_codex_app_state"
TIMEOUT = 15  # 每个用例的整体上限（秒），防 red/green 时挂死


def reset_state() -> None:
    if os.path.exists(STATE):
        os.remove(STATE)
    os.environ["FAKE_CODEX_STATE"] = STATE  # fake server 继承此环境变量


def state_events() -> list[str]:
    if not os.path.exists(STATE):
        return []
    with open(STATE) as f:
        return f.read().splitlines()


async def wait_state(prefix: str, timeout: float = 5.0) -> str:
    """轮询状态文件直到出现 prefix 开头的行（fake 与测试进程的同步点）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for line in state_events():
            if line.startswith(prefix):
                return line
        await asyncio.sleep(0.02)
    raise AssertionError(f"状态文件里等不到 {prefix!r}；当前：{state_events()}")


def assert_no_violation() -> None:
    bad = [e for e in state_events() if e.startswith("VIOLATION")]
    assert not bad, f"fake 记录了协议违规：{bad}"


async def make_client() -> CodexAppServerClient:
    client = CodexAppServerClient(CMD)
    await client.start("/tmp")
    return client


async def wait_notification(methods: list[str], method: str, count: int,
                            timeout: float = 5.0) -> None:
    """轮询通知收集列表，直到某 method 累计出现 count 次。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if methods.count(method) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"等不到 {count} 次 {method}；已收到：{methods}")


async def collect(agen, limit: int = 200) -> list[AgentEvent]:
    events = []
    async for ev in agen:
        events.append(ev)
        if len(events) >= limit:
            raise AssertionError("事件流超过上限，疑似失控")
    return events


def run(coro) -> None:
    asyncio.run(asyncio.wait_for(coro, TIMEOUT))


def test_handshake_two_turns_same_thread_and_pid() -> None:
    """验收 1/2/3（client 层）：握手次序、delta/completed 通知、两轮同 thread 同 pid。"""
    async def body() -> None:
        reset_state()
        client = await make_client()
        notifications: list[str] = []

        async def pump() -> None:
            while True:
                event = await client.next_event()
                method = event.get("method")
                if isinstance(method, str):
                    notifications.append(method)

        pumper = asyncio.create_task(pump())
        try:
            thread_id = await client.thread_start("/tmp")
            assert isinstance(thread_id, str) and thread_id
            turn1 = await client.turn_start(thread_id, "第一轮")
            turn2 = await client.turn_start(thread_id, "第二轮")
            assert turn1 != turn2
            await wait_notification(notifications, "turn/completed", 2)
        finally:
            pumper.cancel()
            await client.aclose()

        assert notifications.count("item/agentMessage/delta") >= 2

        events = state_events()
        init = [e for e in events if e.startswith("initialize:")]
        assert len(init) == 1, events  # 每条连接只握手一次
        assert events.index("initialized") > events.index(init[0])
        threads = [e for e in events if e.startswith("thread:")]
        assert len(threads) == 1, events  # 两轮只 thread/start 一次
        turns = [e for e in events if e.startswith("turn:")]
        assert len(turns) == 2, events
        # 两轮复用同一个 thread
        assert {e.split(":")[1] for e in turns} == {"thr_fake_1"}
        # 同一个 app-server 进程（pid）服务了 thread/start 和两轮 turn/start
        pids = {e.rsplit(":", 1)[1] for e in threads + turns}
        assert len(pids) == 1, pids
        forbidden = {
            "model", "effort", "config", "collaborationMode",
            "developerInstructions", "baseInstructions",
        }
        request_params = [
            json.loads(e.split(":", 1)[1])
            for e in events
            if e.startswith(("thread-params:", "turn-params:"))
        ]
        assert request_params
        assert all(not forbidden.intersection(p) for p in request_params), (
            request_params)
        assert_no_violation()  # 含 VIOLATION:jsonrpc（线上必须无 jsonrpc 头）
    run(body())
    print("ok  握手 + 两轮复用同一 thread 与 pid（无 jsonrpc 头）")


def test_command_approval_default_deny() -> None:
    """验收 5：server 反向 command 审批请求，client 默认拒绝。"""
    async def body() -> None:
        reset_state()
        client = await make_client()  # 不显式 opt-in 任何自动批准
        thread_id = await client.thread_start("/tmp")
        await client.turn_start(thread_id, "需要 approval 一下")
        line = await wait_state("approval:")
        await client.aclose()
        assert '"decision": "decline"' in line or '"decision": "cancel"' in line, line
        assert "accept" not in line, line
        assert_no_violation()
    run(body())
    print("ok  command 审批默认拒绝（deny by default）")


def test_pending_request_fails_on_disconnect() -> None:
    """验收 7：server 崩溃 → pending request 立即失败；aclose 回收进程。"""
    async def body() -> None:
        reset_state()
        client = await make_client()
        thread_id = await client.thread_start("/tmp")
        failed = None
        try:
            await client.turn_start(thread_id, "请 die 一下")  # fake 直接退出
        except Exception as exc:  # 契约：必须抛错，不许挂起
            failed = exc
        assert failed is not None, "server 断开后 pending 的 turn/start 没有失败"
        await client.aclose()  # 不得挂起、不得抛异常
        pid = int((await wait_state("thread:")).rsplit(":", 1)[1])
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass  # 进程已退出且被 reap（zombie 也算未回收）
        else:
            raise AssertionError(f"aclose 后 fake server 进程 {pid} 仍残留")
    run(body())
    print("ok  server 断开 pending 立即失败 + aclose 无残留")


def test_client_close_is_bounded_when_event_queue_is_full() -> None:
    """消费者不读事件且队列饱和时，close 仍必须回收 reader 与进程。"""
    async def body() -> None:
        reset_state()
        client = CodexAppServerClient(
            [*CMD, "--event-flood"],
            shutdown_timeout=0.5,
        )
        await client.start("/tmp")
        pid = client.pid
        await wait_state("event-flood-start")
        await asyncio.sleep(0.2)
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(client.aclose(), timeout=2)
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 1, f"满事件队列关闭耗时 {elapsed:.3f}s"
        assert not client.running
        if pid is not None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError(f"满事件队列关闭后进程 {pid} 仍存活")

    run(body())
    print("ok  满事件队列时 aclose 有界且无残留")


def test_preturn_failure_uses_explicit_fallback() -> None:
    """initialize/thread 建立前失败可安全 fallback，且必须明确可见。"""
    class Fallback:
        name = "codex"
        session_id = None

        def __init__(self) -> None:
            self.prompts = []

        async def stream(self, prompt: str, workdir: str):
            self.prompts.append((prompt, workdir))
            yield AgentEvent("text", "FALLBACK")
            yield AgentEvent("done")

    async def body() -> None:
        fallback = Fallback()
        adapter = CodexAppServerAdapter(
            [sys.executable, "-c", "raise SystemExit(7)"],
            fallback_jsonl=True,
            fallback_adapter=fallback,
        )
        events = await collect(adapter.stream("安全回退", "/tmp"))
        await adapter.aclose()
        assert fallback.prompts == [("安全回退", "/tmp")]
        assert any(e.kind == "info" and "fallback" in e.text for e in events)
        assert [e.text for e in events if e.kind == "text"] == ["FALLBACK"]
        assert any(e.kind == "done" for e in events)
    run(body())
    print("ok  turn 建立前失败显式使用 JSONL fallback")


def test_uncertain_turn_start_is_not_replayed_and_connection_rebuilds() -> None:
    """turn/start 响应丢失时禁止 fallback 重放；下轮必须用新连接。"""
    async def body() -> None:
        reset_state()
        fallback_calls = []

        class ForbiddenFallback:
            name = "codex"
            session_id = None

            async def stream(self, prompt: str, workdir: str):
                fallback_calls.append(prompt)
                yield AgentEvent("done")

        adapter = CodexAppServerAdapter(
            CMD, fallback_jsonl=True,
            fallback_adapter=ForbiddenFallback())
        failed = None
        try:
            await collect(adapter.stream("die", "/tmp"))
        except Exception as exc:
            failed = exc
        assert failed is not None
        assert fallback_calls == [], "不确定 turn 被自动重放到 fallback"

        events = await collect(adapter.stream("重建后的下一轮", "/tmp"))
        await adapter.aclose()
        assert any(e.kind == "done" for e in events)
        lines = state_events()
        assert len([e for e in lines if e.startswith("initialize:")]) == 2
        assert len({e.rsplit(":", 1)[1] for e in lines
                    if e.startswith("initialize:")}) == 2
    run(body())
    print("ok  不确定 turn 不重放，连接作废后下一轮重建")


def test_orchestrator_does_not_redeliver_uncertain_codex_turn() -> None:
    """Orchestrator 后续轮次也不能把可能已提交的旧 turn 再次投递。"""
    async def body() -> None:
        reset_state()
        spec = AgentSpec(
            "codex",
            "app-server",
            lambda: CodexAppServerAdapter(CMD, fallback_jsonl=False),
        )
        orch = Orchestrator("/tmp", specs=(spec,), persistent=False)
        events: list[tuple[str, AgentEvent]] = []
        try:
            await orch.dispatch(
                "@codex die",
                lambda name, event: events.append((name, event)),
            )
            await orch.dispatch(
                "@codex NEXT_AFTER_UNCERTAIN",
                lambda name, event: events.append((name, event)),
            )
        finally:
            await orch.aclose()

        turn_params = [
            json.loads(line.split(":", 1)[1])
            for line in state_events()
            if line.startswith("turn-params:")
        ]
        assert len(turn_params) == 2, state_events()
        prompts = [
            "".join(
                part.get("text", "")
                for part in params.get("input", [])
                if isinstance(part, dict) and part.get("type") == "text"
            )
            for params in turn_params
        ]
        assert "@codex die" in prompts[0]
        assert "NEXT_AFTER_UNCERTAIN" in prompts[1]
        assert "@codex die" not in prompts[1], prompts[1]
        assert any(
            name == "codex" and event.kind == "done"
            for name, event in events
        )

    run(body())
    print("ok  Orchestrator 不重投不确定 Codex turn")


def test_orchestrator_does_not_redeliver_post_submit_failure_or_cancel() -> None:
    """turn 已发送后的失败与取消都必须形成 no-replay 边界。"""
    async def assert_next_prompt_excludes(
        first_message: str,
        *,
        cancel_first: bool,
    ) -> None:
        reset_state()
        spec = AgentSpec(
            "codex",
            "app-server",
            lambda: CodexAppServerAdapter(CMD, fallback_jsonl=False),
        )
        orch = Orchestrator("/tmp", specs=(spec,), persistent=False)
        events: list[tuple[str, AgentEvent]] = []
        try:
            first = asyncio.create_task(orch.dispatch(
                f"@codex {first_message}",
                lambda name, event: events.append((name, event)),
            ))
            if cancel_first:
                await wait_state("turn:")
                first.cancel()
                try:
                    await first
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("取消后的 dispatch 没有传播 CancelledError")
            else:
                await first

            await orch.dispatch(
                "@codex NEXT_AFTER_STOP",
                lambda name, event: events.append((name, event)),
            )
        finally:
            await orch.aclose()

        turn_params = [
            json.loads(line.split(":", 1)[1])
            for line in state_events()
            if line.startswith("turn-params:")
        ]
        assert len(turn_params) == 2, state_events()
        second_prompt = "".join(
            part.get("text", "")
            for part in turn_params[1].get("input", [])
            if isinstance(part, dict) and part.get("type") == "text"
        )
        assert "NEXT_AFTER_STOP" in second_prompt
        assert first_message not in second_prompt, second_prompt
        assert any(
            name == "codex" and event.kind == "done"
            for name, event in events
        )

    async def body() -> None:
        await assert_next_prompt_excludes(
            "serverinterrupt", cancel_first=False)
        await assert_next_prompt_excludes("slow", cancel_first=True)

    run(body())
    print("ok  Codex post-submit 失败/取消均不重投")


def test_definite_turn_rejection_and_presend_failure_remain_retryable() -> None:
    """明确未接受的请求不能被误记为 no-replay。"""
    async def body() -> None:
        # client 尚未启动：错误发生在写入前，不能标成“已发送但不确定”。
        client = CodexAppServerClient(CMD)
        presend_error: Exception | None = None
        try:
            await client.turn_start("missing-thread", "never-sent")
        except Exception as exc:
            presend_error = exc
        assert presend_error is not None
        assert not isinstance(
            presend_error, CodexAppServerRequestUncertain)

        reset_state()
        spec = AgentSpec(
            "codex",
            "app-server",
            lambda: CodexAppServerAdapter(CMD, fallback_jsonl=False),
        )
        orch = Orchestrator("/tmp", specs=(spec,), persistent=False)
        try:
            await orch.dispatch("@codex reject-turn", lambda _n, _e: None)
            await orch.dispatch("@codex RETRY_AFTER_REJECT", lambda _n, _e: None)
        finally:
            await orch.aclose()
        turn_params = [
            json.loads(line.split(":", 1)[1])
            for line in state_events()
            if line.startswith("turn-params:")
        ]
        assert len(turn_params) == 2, state_events()
        second_prompt = "".join(
            part.get("text", "")
            for part in turn_params[1].get("input", [])
            if isinstance(part, dict) and part.get("type") == "text"
        )
        assert "@codex reject-turn" in second_prompt
        assert "RETRY_AFTER_REJECT" in second_prompt

    run(body())
    print("ok  明确拒绝/写入前失败保持可重试")


def test_no_replay_boundary_precedes_error_callback_and_timeline() -> None:
    """error callback 失败也不能阻止不确定 turn 的 cursor 先持久化。"""
    async def body() -> None:
        reset_state()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            state_root = root / "state"
            spec = AgentSpec(
                "codex",
                "app-server",
                lambda: CodexAppServerAdapter(CMD, fallback_jsonl=False),
            )
            first = Orchestrator(
                str(workdir),
                specs=(spec,),
                store=RoomStore(workdir, state_root=state_root),
            )

            def fail_on_error(_name: str, event: AgentEvent) -> None:
                if event.kind == "error":
                    raise RuntimeError("UI sink unavailable")

            try:
                failed = None
                try:
                    await first.dispatch("@codex die", fail_on_error)
                except RuntimeError as exc:
                    failed = exc
                assert failed is not None
            finally:
                await first.aclose()

            persisted = RoomStore(
                workdir, state_root=state_root).get_agent_state("codex")
            assert persisted["cursor"] > 0, persisted

            second = Orchestrator(
                str(workdir),
                specs=(spec,),
                store=RoomStore(workdir, state_root=state_root),
            )
            try:
                await second.dispatch(
                    "@codex NEXT_AFTER_SINK_FAILURE",
                    lambda _name, _event: None,
                )
            finally:
                await second.aclose()

            turn_params = [
                json.loads(line.split(":", 1)[1])
                for line in state_events()
                if line.startswith("turn-params:")
            ]
            assert len(turn_params) == 2, state_events()
            second_prompt = "".join(
                part.get("text", "")
                for part in turn_params[1].get("input", [])
                if isinstance(part, dict) and part.get("type") == "text"
            )
            assert "NEXT_AFTER_SINK_FAILURE" in second_prompt
            assert "@codex die" not in second_prompt

    run(body())
    print("ok  no-replay cursor 先于 error callback/timeline 持久化")


def test_accepted_turn_persists_no_replay_before_partial_event_sink() -> None:
    """turn 接受后，任何 partial/tool callback 前必须先持久 no-replay。"""
    async def body() -> None:
        reset_state()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            state_root = root / "state"
            spec = AgentSpec(
                "codex",
                "app-server",
                lambda: CodexAppServerAdapter(CMD, fallback_jsonl=False),
            )
            store = RoomStore(workdir, state_root=state_root)
            first = Orchestrator(
                str(workdir), specs=(spec,), store=store)
            original_append_event = store.append_event

            def fail_partial(**event) -> None:
                if event.get("kind") == "partial":
                    raise OSError("events disk full")
                original_append_event(**event)

            store.append_event = fail_partial  # type: ignore[method-assign]
            bus = CommandBus(first)
            bus.start()
            try:
                snap = await bus.submit("@codex FIRST_ACCEPTED")
                result = await bus.wait(snap.command_id, timeout=5)
                assert result["status"] == "failed", result
            finally:
                await bus.aclose()
                await first.aclose()

            persisted = RoomStore(
                workdir, state_root=state_root).get_agent_state("codex")
            assert persisted["cursor"] > 0, persisted

            second = Orchestrator(
                str(workdir),
                specs=(spec,),
                store=RoomStore(workdir, state_root=state_root),
            )
            try:
                await second.dispatch(
                    "@codex NEXT_AFTER_PARTIAL_FAILURE",
                    lambda _name, _event: None,
                )
            finally:
                await second.aclose()

            turn_params = [
                json.loads(line.split(":", 1)[1])
                for line in state_events()
                if line.startswith("turn-params:")
            ]
            assert len(turn_params) == 2, state_events()
            second_prompt = "".join(
                part.get("text", "")
                for part in turn_params[1].get("input", [])
                if isinstance(part, dict) and part.get("type") == "text"
            )
            assert "NEXT_AFTER_PARTIAL_FAILURE" in second_prompt
            assert "FIRST_ACCEPTED" not in second_prompt

    run(body())
    print("ok  accepted turn 在 partial sink 前持久 no-replay")


def test_close_during_prepare_never_starts_fallback() -> None:
    """aclose 与 initialize 竞态时，关闭错误不能被误判成 fallback 条件。"""
    async def body() -> None:
        reset_state()
        fallback_calls = []

        class ForbiddenFallback:
            name = "codex"
            session_id = None

            async def stream(self, prompt: str, workdir: str):
                fallback_calls.append(prompt)
                yield AgentEvent("done")

        adapter = CodexAppServerAdapter(
            [sys.executable, SERVER, "--slow-initialize"],
            fallback_jsonl=True,
            fallback_adapter=ForbiddenFallback())
        running = asyncio.create_task(
            collect(adapter.stream("不能回退", "/tmp")))
        await wait_state("initialize:")
        await asyncio.wait_for(adapter.aclose(), timeout=2)
        await asyncio.gather(running, return_exceptions=True)
        assert fallback_calls == []
    run(body())
    print("ok  close/prepare 竞态不启动 fallback")


def test_invalid_frame_fails_immediately_and_reaps_process() -> None:
    """reader 遇非法 JSON 后立即作废连接并回收进程，不等 request timeout。"""
    async def body() -> None:
        reset_state()
        adapter = CodexAppServerAdapter(CMD, request_timeout=10)
        started = asyncio.get_running_loop().time()
        failed = None
        try:
            await collect(adapter.stream("badframe", "/tmp"))
        except Exception as exc:
            failed = exc
        elapsed = asyncio.get_running_loop().time() - started
        assert failed is not None and "非法 JSON" in str(failed)
        assert elapsed < 2, elapsed
        pid = int((await wait_state("turn:")).rsplit(":", 1)[1])
        await adapter.aclose()
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f"非法 frame 后 fake server {pid} 仍存活")
    run(body())
    print("ok  非法协议帧立即失败并回收进程")


def test_adapter_event_mapping() -> None:
    """验收 4：delta→text、tool/file/command→安全 tool/status、完成→done、
    reasoning 正文与凭据不泄露。"""
    async def body() -> None:
        reset_state()
        adapter = CodexAppServerAdapter(CMD)
        events = await collect(adapter.stream("跑点 tools 看看", "/tmp"))
        await adapter.aclose()

        texts = [e.text for e in events if e.kind == "text"]
        assert "PO" in texts and "NG" in texts, texts  # delta → text
        tools = [e for e in events if e.kind in ("tool", "status")]
        assert tools, "command/file/tool item 没有映射出 tool/status 事件"
        assert any(e.kind == "done" for e in events), "turn/completed 没有映射出 done"
        # reasoning 正文不暴露；命令里的凭据必须被脱敏
        blob = " ".join(
            e.text + " " + " ".join(str(v) for v in e.meta.values())
            for e in events)
        assert "REASONING-SECRET-MARKER" not in blob, "reasoning 正文泄露"
        assert "secret-value" not in blob, "命令凭据未脱敏"
        assert_no_violation()
    run(body())
    print("ok  adapter 事件映射（text/tool/status/done + 不泄露 reasoning/凭据）")


def test_adapter_approval_events_are_denied_and_redacted() -> None:
    """adapter 默认拒绝审批，TUI 可见事件不得泄漏命令凭据。"""
    async def body() -> None:
        reset_state()
        prompts = []

        async def deny(params: dict) -> dict:
            prompts.append(params)
            # fake 本轮没有提供 acceptForSession；即使 handler 越权返回，
            # client 也必须校验本次 options 并 fail closed。
            return {"outcome": "selected", "optionId": "acceptForSession"}

        adapter = CodexAppServerAdapter(CMD, permission_handler=deny)
        events = await collect(adapter.stream("approval", "/tmp"))
        await adapter.aclose()
        permissions = [e for e in events if e.kind == "permission"]
        assert len(permissions) >= 2
        blob = " ".join(
            e.text + " " + " ".join(str(v) for v in e.meta.values())
            for e in permissions)
        assert "secret-value" not in blob
        assert "[已隐藏]" in blob
        assert prompts
        assert [opt["optionId"] for opt in prompts[0]["options"]] == [
            "accept", "decline", "cancel"]
        approval = await wait_state("approval:")
        assert '"decision": "decline"' in approval
    run(body())
    print("ok  adapter 审批默认拒绝且可见事件脱敏")


def test_additional_permissions_use_shared_handler_and_fail_closed() -> None:
    """通用权限请求进入共享 handler；允许只回传服务端实际请求的 profile。"""
    async def body() -> None:
        reset_state()
        prompts = []

        async def allow_once(params: dict) -> dict:
            prompts.append(params)
            return {"outcome": "selected", "optionId": "accept"}

        adapter = CodexAppServerAdapter(CMD, permission_handler=allow_once)
        events = await collect(adapter.stream("permissions", "/tmp"))
        await adapter.aclose()
        assert prompts and prompts[0]["toolCall"]["kind"] == "permissions"
        assert "网络访问" in prompts[0]["toolCall"]["title"]
        assert len([e for e in events if e.kind == "permission"]) >= 2
        line = await wait_state("permissions:")
        assert '"network": {"enabled": true}' in line
        assert '"scope": "turn"' in line

        reset_state()
        denied = CodexAppServerAdapter(CMD)
        await collect(denied.stream("permissions", "/tmp"))
        await denied.aclose()
        line = await wait_state("permissions:")
        assert '"permissions": {}' in line
    run(body())
    print("ok  additional permissions 进入共享 UI handler，默认空授权")


def test_human_approval_wait_does_not_trigger_agent_inactivity_timeout() -> None:
    """人类审批等待不是 agent 静默；短 inactivity_timeout 不得误取消。"""
    async def body() -> None:
        reset_state()

        async def deliberate(_params: dict) -> dict:
            await asyncio.sleep(0.15)
            return {"outcome": "selected", "optionId": "decline"}

        adapter = CodexAppServerAdapter(
            CMD, permission_handler=deliberate,
            inactivity_timeout=0.05)
        events = await collect(adapter.stream("approval", "/tmp"))
        await adapter.aclose()
        assert any(e.kind == "done" for e in events)
        assert '"decision": "decline"' in await wait_state("approval:")
    run(body())
    print("ok  人类审批等待不计入 agent inactivity timeout")


def test_invalid_terminal_status_fails_and_rebuilds() -> None:
    """turn/completed 只接受 completed/interrupted/failed，不吞协议漂移。"""
    async def body() -> None:
        reset_state()
        adapter = CodexAppServerAdapter(CMD)
        failed = None
        try:
            await collect(adapter.stream("badstatus", "/tmp"))
        except Exception as exc:
            failed = exc
        assert failed is not None and "非法状态" in str(failed)
        events = await collect(adapter.stream("下一轮", "/tmp"))
        await adapter.aclose()
        assert any(e.kind == "done" for e in events)
        assert len([e for e in state_events()
                    if e.startswith("initialize:")]) == 2
    run(body())
    print("ok  非法 terminal 状态 fail loudly 并重建连接")


def test_server_interrupted_is_regular_failure_not_task_cancellation() -> None:
    """server 主动 interrupted 不得伪装成调用方 CancelledError。"""
    async def body() -> None:
        reset_state()
        adapter = CodexAppServerAdapter(CMD)
        failed = None
        try:
            await collect(adapter.stream("serverinterrupt", "/tmp"))
        except Exception as exc:
            failed = exc
        await adapter.aclose()
        assert failed is not None
        assert "已中断" in str(failed)
        assert not isinstance(failed, asyncio.CancelledError)
    run(body())
    print("ok  server interrupted 映射为普通失败，不逃逸 CancelledError")


def test_adapter_two_streams_reuse_thread_and_pid() -> None:
    """验收 3（adapter 层）：两次 stream 复用同一 thread 与同一 app-server 进程。"""
    async def body() -> None:
        reset_state()
        adapter = CodexAppServerAdapter(CMD)
        first = await collect(adapter.stream("第一轮", "/tmp"))
        second = await collect(adapter.stream("第二轮", "/tmp"))
        await adapter.aclose()
        assert any(e.kind == "done" for e in first)
        assert any(e.kind == "done" for e in second)

        events = state_events()
        threads = [e for e in events if e.startswith("thread:")]
        turns = [e for e in events if e.startswith("turn:")]
        assert len(threads) == 1 and len(turns) == 2, events
        assert len({e.rsplit(":", 1)[1] for e in threads + turns}) == 1
        thread_params = [
            json.loads(e.split(":", 1)[1])
            for e in events if e.startswith("thread-params:")]
        assert thread_params == [{
            "cwd": "/tmp", "sandbox": "workspace-write"}]
        assert_no_violation()
    run(body())
    print("ok  adapter 两轮 stream 复用 thread 与进程")


def test_adapter_resumes_thread_after_process_restart() -> None:
    """持久 session id 在新 app-server 进程中通过 thread/resume 恢复。"""
    async def body() -> None:
        reset_state()
        first = CodexAppServerAdapter(CMD)
        await collect(first.stream("第一轮", "/tmp"))
        thread_id = first.session_id
        await first.aclose()
        assert thread_id == "thr_fake_1"

        preparations = []
        second = CodexAppServerAdapter(CMD)
        events = await collect(second.stream_prepared(
            lambda prep: preparations.append(prep) or "恢复后的第二轮",
            "/tmp",
            thread_id,
        ))
        await second.aclose()

        assert preparations and preparations[0].restored is True
        assert preparations[0].session_id == thread_id
        assert any(e.kind == "done" for e in events)
        lines = state_events()
        assert len([e for e in lines if e.startswith("initialize:")]) == 2
        assert any(e.startswith(f"resume:{thread_id}:") for e in lines)
        assert_no_violation()
    run(body())
    print("ok  app-server 进程重启后 thread/resume 恢复")


def test_adapter_cancel_interrupts_then_allows_next_turn() -> None:
    """验收 6：取消 generator → turn/interrupt + 等 terminal，下一轮不得重叠。"""
    async def body() -> None:
        reset_state()
        adapter = CodexAppServerAdapter(CMD)
        agen = adapter.stream("slow 慢活", "/tmp")
        async for ev in agen:  # 等到第一个正文事件说明 turn 已在 server 侧活跃
            if ev.kind == "text":
                break
        await agen.aclose()  # 取消：契约要求内部走 turn/interrupt 并等 terminal
        interrupt = await wait_state("interrupt:", timeout=5)

        events = await collect(adapter.stream("下一轮", "/tmp"))
        await adapter.aclose()
        assert any(e.kind == "done" for e in events), "取消后下一轮没有正常完成"

        lines = state_events()
        second_turn = [e for e in lines if e.startswith("turn:")][-1]
        # interrupt 必须发生在第二轮 turn/start 之前（不重叠）
        assert lines.index(interrupt) < lines.index(second_turn), lines
        assert_no_violation()  # 含 VIOLATION:overlap
    run(body())
    print("ok  取消走 turn/interrupt 且等 terminal 后才放行下一轮")


def test_aclose_interrupts_active_stream_and_reaps_process() -> None:
    """直接 aclose 也必须唤醒活跃 turn，而不是无限等 writer lock。"""
    async def body() -> None:
        reset_state()
        adapter = CodexAppServerAdapter(CMD)
        running = asyncio.create_task(
            collect(adapter.stream("slow", "/tmp")))
        turn = await wait_state("turn:")
        pid = int(turn.rsplit(":", 1)[1])
        await asyncio.wait_for(adapter.aclose(), timeout=2)
        results = await asyncio.gather(running, return_exceptions=True)
        assert isinstance(results[0], BaseException)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f"aclose 后 fake server {pid} 仍存活")
    run(body())
    print("ok  aclose 主动中断活跃 turn 并回收进程")


def test_aclose_after_consumer_break_is_bounded() -> None:
    """消费者停在 yield 且未先关 generator，aclose 仍有界并回收进程。"""
    async def body() -> None:
        reset_state()
        adapter = CodexAppServerAdapter(CMD)
        agen = adapter.stream("slow", "/tmp")
        async for event in agen:
            if event.kind == "text":
                break
        turn = await wait_state("turn:")
        pid = int(turn.rsplit(":", 1)[1])
        await asyncio.wait_for(adapter.aclose(), timeout=2)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f"aclose 后 fake server {pid} 仍存活")
        closed_stream = adapter.stream("不得启动", "/tmp")
        failed = None
        try:
            await asyncio.wait_for(anext(closed_stream), timeout=0.1)
        except Exception as exc:
            failed = exc
        assert failed is not None and "已关闭" in str(failed)
        await agen.aclose()
    run(body())
    print("ok  consumer break 后 aclose 有界且无残留")


def test_aclose_is_bounded_when_owner_swallows_cancellation() -> None:
    """外层消费者吞掉 cancellation 时，adapter close 仍不得无限等待。"""
    async def body() -> None:
        reset_state()
        adapter = CodexAppServerAdapter(CMD, cancel_timeout=0.1)
        keep_waiting = asyncio.Event()

        async def stubborn_consumer() -> None:
            try:
                await collect(adapter.stream("slow", "/tmp"))
            except asyncio.CancelledError:
                await keep_waiting.wait()

        owner = asyncio.create_task(stubborn_consumer())
        turn = await wait_state("turn:")
        pid = int(turn.rsplit(":", 1)[1])
        await asyncio.wait_for(adapter.aclose(), timeout=1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f"aclose 后 fake server {pid} 仍存活")
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
    run(body())
    print("ok  owner 吞 cancellation 时 aclose 仍有界")


if __name__ == "__main__":
    test_handshake_two_turns_same_thread_and_pid()
    test_command_approval_default_deny()
    test_pending_request_fails_on_disconnect()
    test_client_close_is_bounded_when_event_queue_is_full()
    test_preturn_failure_uses_explicit_fallback()
    test_uncertain_turn_start_is_not_replayed_and_connection_rebuilds()
    test_orchestrator_does_not_redeliver_uncertain_codex_turn()
    test_orchestrator_does_not_redeliver_post_submit_failure_or_cancel()
    test_definite_turn_rejection_and_presend_failure_remain_retryable()
    test_no_replay_boundary_precedes_error_callback_and_timeline()
    test_accepted_turn_persists_no_replay_before_partial_event_sink()
    test_close_during_prepare_never_starts_fallback()
    test_invalid_frame_fails_immediately_and_reaps_process()
    test_adapter_event_mapping()
    test_adapter_approval_events_are_denied_and_redacted()
    test_additional_permissions_use_shared_handler_and_fail_closed()
    test_human_approval_wait_does_not_trigger_agent_inactivity_timeout()
    test_invalid_terminal_status_fails_and_rebuilds()
    test_server_interrupted_is_regular_failure_not_task_cancellation()
    test_adapter_two_streams_reuse_thread_and_pid()
    test_adapter_resumes_thread_after_process_restart()
    test_adapter_cancel_interrupts_then_allows_next_turn()
    test_aclose_interrupts_active_stream_and_reaps_process()
    test_aclose_after_consumer_break_is_bounded()
    test_aclose_is_bounded_when_owner_swallows_cancellation()
    print("\nCodex app-server 契约测试全部通过")

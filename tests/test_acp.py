"""ACP 层回归测试：全部走 fake server，不发真实请求。

运行：FAKE_ACP_STATE=/tmp/myagents_fake_acp_state .venv/bin/python tests/test_acp.py
（文件顶部已自设该环境变量，直接跑即可）
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acp.adapter import AcpAdapter
from acp.client import AcpClient, AcpError
from adapters.base import AgentDeliveryUncertainError
from clipboard_image import TrustedImage

SERVER = str(Path(__file__).parent / "fake_acp_server.py")
STATE = "/tmp/myagents_fake_acp_state"
os.environ["FAKE_ACP_STATE"] = STATE  # fake server 继承此环境变量


def state_events() -> list[str]:
    if not os.path.exists(STATE):
        return []
    with open(STATE) as f:
        return f.read().splitlines()


def reset_state() -> None:
    if os.path.exists(STATE):
        os.remove(STATE)


async def first_text(agen) -> "object":
    """从 adapter 事件流里取第一条 text（跳过 session 建立等 info 事件）。"""
    async for ev in agen:
        if ev.kind == "text":
            return ev
    raise AssertionError("流结束也没有 text 事件")


async def make_client(**kwargs) -> AcpClient:
    client = AcpClient([sys.executable, SERVER], **kwargs)
    await client.start()
    return client


def test_initialize() -> None:
    async def run() -> None:
        client = await make_client()
        assert client.protocol_version == 1
        assert client.agent_info["name"] == "fake-acp"
        assert client.capabilities["loadSession"] is True
        await client.close()
    asyncio.run(run())
    print("ok  initialize 握手")


def test_session_new_list_load() -> None:
    async def run() -> None:
        reset_state()
        client = await make_client()
        sid = await client.session_new("/tmp")
        assert sid == "fake-session-1"
        sessions = await client.session_list()
        assert sessions[0]["sessionId"] == "fake-session-1"
        await client.session_load(sid, "/tmp")
        await client.close()
        events = state_events()
        assert "new:/tmp" in events and "load:fake-session-1" in events
    asyncio.run(run())
    print("ok  session/new|list|load")


def test_prompt_streaming() -> None:
    async def run() -> None:
        client = await make_client()
        sid = await client.session_new("/tmp")
        updates = []
        client.on_notification = lambda m, p: updates.append(
            p["update"]["sessionUpdate"] if m == "session/update" else m)
        result = await client.prompt(sid, "说 PONG")
        assert result["stopReason"] == "end_turn"
        assert "agent_thought_chunk" in updates
        assert updates.count("agent_message_chunk") == 2
        await client.close()
    asyncio.run(run())
    print("ok  session/prompt + update 流")


def test_image_prompt_uses_fake_wire_contract() -> None:
    async def run() -> None:
        reset_state()
        client = await make_client()
        assert client.capabilities["promptCapabilities"]["image"] is True
        sid = await client.session_new("/tmp")
        image = TrustedImage(
            path=Path("/tmp/myagents_fake_acp_image.png"),
            attachment_root=Path("/tmp"),
            relative_path=Path("myagents_fake_acp_image.png"),
            data=b"fake image bytes for wire test",
        )
        await client.prompt(sid, "看图", (image,))
        await client.close()
        assert "prompt-types:text,image" in state_events()

    asyncio.run(run())
    print("ok  ACP fake contract 接收原生 image block")


def test_adapter_prompt_without_image_capability() -> None:
    """无 image capability 时不得 UnboundLocalError，且不发 image block。"""
    async def run() -> None:
        reset_state()
        os.environ["FAKE_ACP_NO_IMAGE_CAP"] = "1"
        try:
            adapter = AcpAdapter("fake", [sys.executable, SERVER])
            texts = [
                ev.text async for ev in adapter.stream("看图", "/tmp")
                if ev.kind == "text"
            ]
            await adapter.aclose()
        finally:
            os.environ.pop("FAKE_ACP_NO_IMAGE_CAP", None)
        assert texts
        assert "prompt-types:text" in state_events()
        assert "prompt-types:text,image" not in state_events()

    asyncio.run(run())
    print("ok  ACP adapter 在无 image capability 时仍可 prompt")


def test_permission_default_deny() -> None:
    """安全契约：不显式 opt-in 时，权限请求一律 cancelled。"""
    async def run() -> None:
        reset_state()
        client = await make_client()  # 不传 permission：默认必须 deny
        sid = await client.session_new("/tmp")
        await client.prompt(sid, "需要 perm 一下")
        await client.close()
        perm = [e for e in state_events() if e.startswith("permission:")][0]
        assert '"outcome": "cancelled"' in perm, perm
    asyncio.run(run())
    print("ok  权限默认拒绝（deny by default）")


def test_permission_auto_optin() -> None:
    """auto 是显式 opt-in：选 allow_once。"""
    async def run() -> None:
        reset_state()
        client = await make_client(permission="auto")
        sid = await client.session_new("/tmp")
        await client.prompt(sid, "需要 perm 一下")
        await client.close()
        perm = [e for e in state_events() if e.startswith("permission:")][0]
        assert '"outcome": "selected"' in perm and "allow" in perm
    asyncio.run(run())
    print("ok  权限 auto 需显式 opt-in")


def test_permission_wait_pauses_inactivity_timeout() -> None:
    """等待人类权限选择不是 agent 静默；等待时间可超过 inactivity 阈值。"""
    async def run() -> None:
        reset_state()

        async def slow_human(_params: dict) -> dict:
            await asyncio.sleep(0.2)
            return {"outcome": "selected", "optionId": "allow"}

        adapter = AcpAdapter(
            "fake",
            [sys.executable, SERVER],
            inactivity_timeout=0.05,
            cancel_timeout=0.05,
            permission_handler=slow_human,
        )
        texts = [
            event.text
            async for event in adapter.stream("需要 perm 一下", "/tmp")
            if event.kind == "text"
        ]
        assert texts == ["PO", "NG"]
        permission = [
            item for item in state_events()
            if item.startswith("permission:")
        ][0]
        assert '"outcome": "selected"' in permission
        await adapter.aclose()

    asyncio.run(run())
    print("ok  人类权限等待暂停 ACP inactivity timeout")


def test_cancel_notification() -> None:
    async def run() -> None:
        reset_state()
        client = await make_client()
        sid = await client.session_new("/tmp")
        started = asyncio.Event()
        client.on_notification = lambda m, p: started.set()
        task = asyncio.create_task(client.prompt(sid, "slow task"))
        await asyncio.wait_for(started.wait(), timeout=5)
        await client.cancel(sid)
        # server 会在延迟后回 cancelled，prompt 正常结束
        result = await asyncio.wait_for(task, timeout=5)
        assert result["stopReason"] == "cancelled"
        await client.close()
        events = state_events()
        assert "cancel:fake-session-1" in events
        assert "cancel-complete" in events
    asyncio.run(run())
    print("ok  session/cancel 通知 + cancelled 终止响应")


def test_cancel_serialization() -> None:
    """P1 契约：取消的流在确认停止前不释放锁，下一轮不重叠。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        # 第一轮：slow，读到第一个 chunk 后关闭流（触发取消契约）
        agen = adapter.stream("slow task", "/tmp")
        first = await first_text(agen)
        assert "开始" in first.text
        await agen.aclose()  # 内部：cancel → 等 cancelled 确认 → 才释放锁
        # 第二轮：如果上一轮没确认停止就放行，server 会记 VIOLATION
        texts = [ev.text async for ev in adapter.stream("fast round", "/tmp")
                 if ev.kind == "text"]
        assert texts == ["PO", "NG"]
        await adapter.aclose()
        events = state_events()
        assert "VIOLATION:overlap" not in events, events
        assert events.index("cancel-complete") < events.index("prompt:fast round")
    asyncio.run(run())
    print("ok  取消串行化（下一轮不提前开始）")


def test_cancel_timeout_rebuild() -> None:
    """P1 契约：agent 不确认 cancelled（超时）→ 关闭并重建连接。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER],
                             cancel_timeout=0.3)
        old_client = adapter._client
        agen = adapter.stream("slow-never task", "/tmp")
        first = await first_text(agen)
        assert first.kind == "text"
        await agen.aclose()  # cancel 后等 0.3s 超时 → _reset()
        assert adapter._client is not old_client, "超时后未重建连接"
        assert adapter._started is False
        # 重建后下一轮正常（新进程、新 session）
        texts = [ev.text async for ev in adapter.stream("fast round", "/tmp")
                 if ev.kind == "text"]
        assert texts == ["PO", "NG"]
        await adapter.aclose()
    asyncio.run(run())
    print("ok  cancel 超时 → 关闭并重建连接")


def test_inactivity_timeout_unblocks_next_round() -> None:
    """ACP 中途无任何事件时应自动取消，不能永久堵住 CommandBus。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter(
            "fake", [sys.executable, SERVER],
            inactivity_timeout=0.15, cancel_timeout=0.1,
        )
        old_client = adapter._client

        async def consume_stalled() -> None:
            async for _ in adapter.stream("slow-never task", "/tmp"):
                pass

        try:
            await asyncio.wait_for(consume_stalled(), timeout=0.8)
            raise AssertionError("无活动的流应该超时")
        except AgentDeliveryUncertainError as exc:
            assert "无活动" in str(exc), exc

        assert adapter._client is not old_client
        assert adapter._started is False
        texts = [
            ev.text async for ev in adapter.stream("fast round", "/tmp")
            if ev.kind == "text"
        ]
        assert texts == ["PO", "NG"]
        await adapter.aclose()

    asyncio.run(run())
    print("ok  ACP 无活动超时 → 自动取消并放行下一轮")


def test_close_during_prompt() -> None:
    """P2 生命周期：prompt 进行中 close()，pending 失败且进程无残留。"""
    async def run() -> None:
        client = await make_client()
        sid = await client.session_new("/tmp")
        started = asyncio.Event()
        client.on_notification = lambda m, p: started.set()
        task = asyncio.create_task(client.prompt(sid, "slow task"))
        await asyncio.wait_for(started.wait(), timeout=5)
        pid = client._proc.pid
        await client.close()
        try:
            await task
            raise AssertionError("prompt 应该失败")
        except AcpError:
            pass
        await asyncio.sleep(0.2)
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        assert not alive, "close 后进程仍存活"
        assert client._reader.done() and client._stderr_task.done()
    asyncio.run(run())
    print("ok  close-during-prompt（pending 失败 + 无残留进程）")


def test_initialize_failure() -> None:
    """P2 生命周期：initialize 失败 → start 抛错且回收进程。"""
    async def run() -> None:
        os.environ["FAKE_ACP_FAIL_INIT"] = "1"
        try:
            client = AcpClient([sys.executable, SERVER])
            try:
                await client.start()
                raise AssertionError("start 应该抛 AcpError")
            except AcpError as exc:
                assert "init boom" in str(exc)
            await asyncio.sleep(0.2)
            assert client._proc.returncode is not None, "进程未被回收"
        finally:
            del os.environ["FAKE_ACP_FAIL_INIT"]
    asyncio.run(run())
    print("ok  initialize 失败 → 进程回收")


def test_session_new_failure_atomic() -> None:
    """P1 生命周期：start 成功但 session/new 失败 → 原子回收，无孤儿，可重建。"""
    async def run() -> None:
        os.environ["FAKE_ACP_FAIL_NEW"] = "1"
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        try:
            try:
                async for _ in adapter.stream("hi", "/tmp"):
                    pass
                raise AssertionError("stream 应该抛 AcpError")
            except AcpError as exc:
                assert "new boom" in str(exc)
            # 原子回收：状态复位、旧 client 被替换、旧进程组已关闭
            assert adapter._started is False and adapter.session_id is None
            await asyncio.sleep(0.2)
        finally:
            del os.environ["FAKE_ACP_FAIL_NEW"]
        # 重建后正常（新进程没有 FAIL_NEW）
        texts = [ev.text async for ev in adapter.stream("fast round", "/tmp")
                 if ev.kind == "text"]
        assert texts == ["PO", "NG"]
        await adapter.aclose()
        # 最终无残留 fake server 进程
        await asyncio.sleep(0.2)
        import subprocess
        out = subprocess.run(["pgrep", "-f", "fake_acp_server"],
                             capture_output=True, text=True).stdout.split()
        assert not out, f"残留 fake server 进程: {out}"
    asyncio.run(run())
    print("ok  session/new 失败 → 原子回收 + 可重建")


def test_double_start_guard() -> None:
    """P1 生命周期：重复 start 直接报错，不覆盖活进程。"""
    async def run() -> None:
        client = await make_client()
        try:
            await client.start()
            raise AssertionError("重复 start 应该抛 AcpError")
        except AcpError:
            pass
        await client.close()  # 原进程仍归原 client 管理，正常回收
    asyncio.run(run())
    print("ok  重复 start 防护")


def test_aclose_serialized_with_stream() -> None:
    """aclose 与 stream 同一把锁：close 等 stream 结束，不竞态杀进程。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        agen = adapter.stream("slow task", "/tmp")
        first = await first_text(agen)
        assert first.kind == "text"
        # stream 持有锁进行中：aclose 必须阻塞等锁
        close_task = asyncio.create_task(adapter.aclose())
        await asyncio.sleep(0.2)
        assert not close_task.done(), "aclose 未等锁，与 stream 竞态"
        # 结束流（走取消契约，server 回 cancelled 后放锁）
        await agen.aclose()
        await asyncio.wait_for(close_task, timeout=5)
        assert adapter._started is False
        # close 后重建正常
        texts = [ev.text async for ev in adapter.stream("fast round", "/tmp")
                 if ev.kind == "text"]
        assert texts == ["PO", "NG"]
        await adapter.aclose()
    asyncio.run(run())
    print("ok  aclose 与 stream 同锁串行")


def test_adapter_bridge() -> None:
    """AcpAdapter 桥接：事件映射 + session 所有权。"""
    async def run() -> None:
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        events = []
        async for ev in adapter.stream("说 PONG", "/tmp"):
            events.append(ev)
        texts = [e.text for e in events if e.kind == "text"]
        assert texts == ["PO", "NG"]
        assert events[-1].kind == "done"
        assert events[-1].meta["stopReason"] == "end_turn"
        assert adapter.session_id == "fake-session-1"
        await adapter.aclose()
    asyncio.run(run())
    print("ok  AcpAdapter 桥接（text 事件 + stopReason）")


def test_adapter_observability_without_thought_content() -> None:
    """ACP 阶段/工具可见，但不把 agent thought 正文当作输出泄露。"""
    async def run() -> None:
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        events = [
            event async for event in adapter.stream(
                "observe secret-title", "/tmp")
        ]
        assert any(e.kind == "status" and "分析" in e.text for e in events)
        tools = [e for e in events if e.kind == "tool"]
        assert tools and tools[0].text == \
            "API_TOKEN=[已隐藏] 检查 JavaScript"
        assert tools[0].meta["command"] == \
            "API_TOKEN=[已隐藏] node --check demo.js"
        assert not any("secret-value" in e.text for e in events)
        assert "secret-value" not in tools[0].meta["command"]
        assert not any("想想" in e.text for e in events)
        await adapter.aclose()
    asyncio.run(run())
    print("ok  ACP 可观测事件不暴露 thought 正文")


def test_tool_update_spam_is_coalesced_and_keeps_context() -> None:
    """同一工具的重复状态没有信息增量；只保留状态迁移并继承初始标题。"""
    async def run() -> None:
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        events = [
            event async for event in adapter.stream(
                "observe tool-spam", "/tmp")
        ]
        tools = [event for event in events if event.kind == "tool"]
        assert [event.meta.get("status") for event in tools] == [
            None, "in_progress", "completed"
        ], [(event.kind, event.text, event.meta) for event in events]
        assert all(event.text == "检查 JavaScript" for event in tools), tools
        assert all(
            event.meta.get("tool_call_id") == "tool-1" for event in tools)
        assert not [
            event for event in events
            if event.kind == "status"
            and event.meta.get("tool_call_id") == "tool-1"
        ], events
        assert len([
            event for event in events if event.kind == "activity"
        ]) == 199
        await adapter.aclose()

    asyncio.run(run())
    print("ok  ACP 工具状态去重 + 标题继承")


def test_active_tool_uses_separate_inactivity_watchdog() -> None:
    """长工具静默可超过普通 120s 等价阈值，但仍受独立 watchdog 约束。"""
    async def run() -> None:
        adapter = AcpAdapter(
            "fake",
            [sys.executable, SERVER],
            inactivity_timeout=0.05,
            tool_inactivity_timeout=0.4,
        )
        events = [
            event async for event in adapter.stream(
                "observe tool-pause", "/tmp")
        ]
        tools = [event for event in events if event.kind == "tool"]
        assert [event.meta.get("status") for event in tools] == [
            None, "in_progress", "completed"
        ], tools
        assert events[-1].kind == "done"
        await adapter.aclose()

    asyncio.run(run())
    print("ok  ACP 活跃长工具使用独立 inactivity watchdog")


def test_restore_load_success() -> None:
    """M2.5：resume id + loadSession capability → session/load，结果不可歧义。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        seen = {}

        def make_prompt(prep):
            seen["prep"] = prep
            # 惰性回调在判定之后、锁内执行：可以安全使用实际 session id
            assert adapter._lock.locked()
            return f"hi from {prep.session_id}"

        events = [ev async for ev in adapter.stream_prepared(
            make_prompt, "/tmp", resume_session_id="old-session-9")]
        prep = seen["prep"]
        assert prep.session_id == "old-session-9"
        assert prep.restored is True and prep.load_failed is False
        assert adapter.session_id == "old-session-9"
        infos = [e.text for e in events if e.kind == "info"]
        assert infos == ["ACP session 已恢复：old-session-9"], infos
        texts = [e.text for e in events if e.kind == "text"]
        assert texts == ["PO", "NG"]
        evs = state_events()
        assert "load:old-session-9" in evs
        assert not any(e.startswith("new:") for e in evs), evs
        assert evs.index("load:old-session-9") < evs.index(
            "prompt:hi from old-session-9")
        # 第二轮复用活跃 session：不再 emit info，不再 load/new
        reset_state()
        events2 = [ev async for ev in adapter.stream_prepared(
            lambda p: "round2", "/tmp", resume_session_id="old-session-9")]
        assert not [e for e in events2 if e.kind == "info"
                    and "session" in e.text], events2
        evs2 = state_events()
        assert not any(e.startswith(("load:", "new:")) for e in evs2), evs2
        await adapter.aclose()
    asyncio.run(run())
    print("ok  restore：load 成功 → 复用 resume session")


def test_restore_load_error_fallback_new() -> None:
    """M2.5：session/load 的 AcpError 被吞并回退 new，load_failed 如实标记。"""
    async def run() -> None:
        os.environ["FAKE_ACP_FAIL_LOAD"] = "1"
        try:
            reset_state()
            adapter = AcpAdapter("fake", [sys.executable, SERVER])
            seen = {}

            def make_prompt(prep):
                seen["prep"] = prep
                return "hi"

            events = [ev async for ev in adapter.stream_prepared(
                make_prompt, "/tmp", resume_session_id="old-session-9")]
            prep = seen["prep"]
            assert prep.session_id == "fake-session-1"
            assert prep.restored is False and prep.load_failed is True
            infos = [e.text for e in events if e.kind == "info"]
            assert infos == ["ACP session 已建立：fake-session-1"], infos
            evs = state_events()
            assert evs.index("load:old-session-9") < evs.index("new:/tmp")
            assert evs.index("new:/tmp") < evs.index("prompt:hi")
            await adapter.aclose()
        finally:
            del os.environ["FAKE_ACP_FAIL_LOAD"]
    asyncio.run(run())
    print("ok  restore：load 失败 → 回退 session/new")


def test_restore_unsupported_fallback_new() -> None:
    """M2.5：agent 不声明 loadSession → 直接 session/new，不尝试 load。"""
    async def run() -> None:
        os.environ["FAKE_ACP_NO_LOAD_CAP"] = "1"
        try:
            reset_state()
            adapter = AcpAdapter("fake", [sys.executable, SERVER])
            seen = {}

            def make_prompt(prep):
                seen["prep"] = prep
                return "hi"

            async for _ev in adapter.stream_prepared(
                    make_prompt, "/tmp", resume_session_id="old-session-9"):
                pass
            prep = seen["prep"]
            assert prep.session_id == "fake-session-1"
            assert prep.restored is False and prep.load_failed is False
            evs = state_events()
            assert not any(e.startswith("load:") for e in evs), evs
            assert "new:/tmp" in evs
            await adapter.aclose()
        finally:
            del os.environ["FAKE_ACP_NO_LOAD_CAP"]
    asyncio.run(run())
    print("ok  restore：capability 不支持 → 直接 session/new")


def test_prompt_factory_after_decision() -> None:
    """M2.5：prompt 工厂在 prepare 判定之后执行（状态文件已有 load/new）。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])

        def make_prompt(prep):
            evs = state_events()
            assert "load:resume-1" in evs, evs  # 判定已发生
            assert not any(e.startswith("prompt:") for e in evs), evs
            return "after-decision"

        texts = [ev.text async for ev in adapter.stream_prepared(
            make_prompt, "/tmp", resume_session_id="resume-1")
            if ev.kind == "text"]
        assert texts == ["PO", "NG"]
        await adapter.aclose()
    asyncio.run(run())
    print("ok  prompt 工厂在 prepare 判定后执行")


def test_aclose_cannot_interleave_prepare_prompt() -> None:
    """M2.5：prepare→prompt 同锁生命周期，aclose 无法插入中间。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        prepared = asyncio.Event()

        def make_prompt(prep):
            assert adapter._lock.locked(), "prompt 构造不在锁内"
            prepared.set()
            return "slow task"

        agen = adapter.stream_prepared(make_prompt, "/tmp",
                                       resume_session_id="resume-1")
        first = await first_text(agen)
        assert "开始" in first.text
        assert prepared.is_set()
        # prompt 进行中：aclose 必须等锁，不能插在 prepare 与 prompt 之间
        close_task = asyncio.create_task(adapter.aclose())
        await asyncio.sleep(0.2)
        assert not close_task.done(), "aclose 插入了 prepare→prompt 的锁生命周期"
        await agen.aclose()
        await asyncio.wait_for(close_task, timeout=5)
        evs = state_events()
        assert evs.index("load:resume-1") < evs.index("prompt:slow task")
        # 重建后下轮 fresh：重新 load 并再次如实报告（不缓存、不误报）
        events = [ev async for ev in adapter.stream_prepared(
            lambda p: "round2", "/tmp", resume_session_id="resume-1")]
        infos = [e.text for e in events if e.kind == "info"]
        assert infos == ["ACP session 已恢复：resume-1"], infos
        await adapter.aclose()
    asyncio.run(run())
    print("ok  aclose 不能插入 prepare→prompt")


def test_resume_no_steal_active_session() -> None:
    """M2.5：resume id 与活跃 session 不同 → 报错拒绝偷换，不发 load。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        async for _ in adapter.stream_prepared(lambda p: "hi", "/tmp",
                                               resume_session_id="s1"):
            pass
        assert adapter.session_id == "s1"
        try:
            async for _ in adapter.stream_prepared(lambda p: "hi", "/tmp",
                                                   resume_session_id="s2"):
                pass
            raise AssertionError("不同 resume id 应该抛 AcpError")
        except AcpError as exc:
            assert "偷换" in str(exc), exc
        assert adapter.session_id == "s1", "活跃 session 被改了"
        evs = state_events()
        assert "load:s2" not in evs and "new:/tmp" not in evs, evs
        await adapter.aclose()
    asyncio.run(run())
    print("ok  resume id 不偷换活跃 session")


def test_restore_new_error_propagates() -> None:
    """M2.5：只吞 load 失败；回退的 session/new 失败照常传播并原子回收。"""
    async def run() -> None:
        os.environ["FAKE_ACP_FAIL_LOAD"] = "1"
        os.environ["FAKE_ACP_FAIL_NEW"] = "1"
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        try:
            reset_state()
            try:
                async for _ in adapter.stream_prepared(
                        lambda p: "hi", "/tmp", resume_session_id="old-9"):
                    pass
                raise AssertionError("session/new 失败应该抛 AcpError")
            except AcpError as exc:
                assert "new boom" in str(exc), exc
            assert adapter._started is False and adapter.session_id is None
            evs = state_events()
            assert evs.index("load:old-9") < evs.index("new:/tmp")
            await asyncio.sleep(0.2)
        finally:
            del os.environ["FAKE_ACP_FAIL_LOAD"]
            del os.environ["FAKE_ACP_FAIL_NEW"]
            await adapter.aclose()
    asyncio.run(run())
    print("ok  restore：session/new 失败照常传播")


def test_prompt_error_propagates() -> None:
    """M2.5：prompt 错误不被 restore 原语吞掉，照常传播给调用方。"""
    async def run() -> None:
        os.environ["FAKE_ACP_FAIL_PROMPT"] = "1"
        try:
            adapter = AcpAdapter("fake", [sys.executable, SERVER])
            try:
                async for _ in adapter.stream_prepared(
                        lambda p: "hi", "/tmp", resume_session_id="resume-1"):
                    pass
                raise AssertionError("prompt 失败应该抛 AcpError")
            except AcpError as exc:
                assert "prompt boom" in str(exc), exc
            # load 已成功、错误发生在 prompt：连接与 session 仍归 adapter
            assert adapter._started is True
            assert adapter.session_id == "resume-1"
            await adapter.aclose()
        finally:
            del os.environ["FAKE_ACP_FAIL_PROMPT"]
    asyncio.run(run())
    print("ok  prompt 失败照常传播")


def test_factory_runs_before_first_event() -> None:
    """M2.5 原子性：第一个 info 事件返回前，prompt factory 已执行完毕。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        called = []

        def make_prompt(prep):
            called.append(prep.session_id)
            return "hi"

        agen = adapter.stream_prepared(make_prompt, "/tmp",
                                       resume_session_id="resume-1")
        first = await agen.__anext__()
        assert first.kind == "info", first
        assert called == ["resume-1"], "info 之前 factory 未执行"
        async for _ in agen:
            pass
        await adapter.aclose()
    asyncio.run(run())
    print("ok  factory 在第一个 info 事件前执行")


def test_fresh_factory_error_resets() -> None:
    """M2.5 原子性：fresh 轮 factory 抛异常 → 无 info/prompt、连接回收、可重试。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])

        def bad_factory(prep):
            raise RuntimeError("checkpoint 写盘失败")

        events = []
        try:
            async for ev in adapter.stream_prepared(
                    bad_factory, "/tmp", resume_session_id="retry-1"):
                events.append(ev)
            raise AssertionError("factory 异常应该传播")
        except RuntimeError as exc:
            assert "checkpoint" in str(exc)
        assert events == [], "factory 失败后不应 emit 任何事件"
        evs = state_events()
        assert "load:retry-1" in evs  # load 已发生但未被认领
        assert not any(e.startswith("prompt:") for e in evs), evs
        # 不留未提交的活跃 session：连接已回收
        assert adapter._started is False and adapter.session_id is None
        # 同一 resume id 可重试：重新 load 并成功
        texts = [ev.text async for ev in adapter.stream_prepared(
            lambda p: "hi again", "/tmp", resume_session_id="retry-1")
            if ev.kind == "text"]
        assert texts == ["PO", "NG"]
        assert adapter.session_id == "retry-1"
        evs = state_events()
        assert evs.count("load:retry-1") == 2, evs
        await adapter.aclose()
        # 无残留 fake server 进程
        await asyncio.sleep(0.2)
        import subprocess
        out = subprocess.run(["pgrep", "-f", "fake_acp_server"],
                             capture_output=True, text=True).stdout.split()
        assert not out, f"残留 fake server 进程: {out}"
    asyncio.run(run())
    print("ok  fresh factory 异常 → 回收连接 + 可原 id 重试")


def test_nonfresh_factory_error_keeps_session() -> None:
    """M2.5 原子性：复用活跃 session 时 factory 异常不销毁现有 session。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        async for _ in adapter.stream_prepared(lambda p: "hi", "/tmp",
                                               resume_session_id="keep-1"):
            pass
        assert adapter.session_id == "keep-1"

        def bad_factory(prep):
            raise RuntimeError("boom")

        try:
            async for _ in adapter.stream_prepared(
                    bad_factory, "/tmp", resume_session_id="keep-1"):
                pass
            raise AssertionError("factory 异常应该传播")
        except RuntimeError:
            pass
        # 非 fresh：已有 session 不受影响，下轮正常
        assert adapter._started is True and adapter.session_id == "keep-1"
        texts = [ev.text async for ev in adapter.stream_prepared(
            lambda p: "still alive", "/tmp", resume_session_id="keep-1")
            if ev.kind == "text"]
        assert texts == ["PO", "NG"]
        evs = state_events()
        assert not any(e.startswith("prompt:boom") for e in evs)
        await adapter.aclose()
    asyncio.run(run())
    print("ok  非 fresh factory 异常 → 保留活跃 session")


if __name__ == "__main__":
    test_initialize()
    test_session_new_list_load()
    test_prompt_streaming()
    test_image_prompt_uses_fake_wire_contract()
    test_adapter_prompt_without_image_capability()
    test_permission_default_deny()
    test_permission_auto_optin()
    test_permission_wait_pauses_inactivity_timeout()
    test_cancel_notification()
    test_cancel_serialization()
    test_cancel_timeout_rebuild()
    test_inactivity_timeout_unblocks_next_round()
    test_close_during_prompt()
    test_initialize_failure()
    test_session_new_failure_atomic()
    test_double_start_guard()
    test_aclose_serialized_with_stream()
    test_adapter_bridge()
    test_adapter_observability_without_thought_content()
    test_tool_update_spam_is_coalesced_and_keeps_context()
    test_active_tool_uses_separate_inactivity_watchdog()
    test_restore_load_success()
    test_restore_load_error_fallback_new()
    test_restore_unsupported_fallback_new()
    test_prompt_factory_after_decision()
    test_aclose_cannot_interleave_prepare_prompt()
    test_resume_no_steal_active_session()
    test_restore_new_error_propagates()
    test_prompt_error_propagates()
    test_factory_runs_before_first_event()
    test_fresh_factory_error_resets()
    test_nonfresh_factory_error_keeps_session()
    print("\nACP 全部通过")

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


if __name__ == "__main__":
    test_initialize()
    test_session_new_list_load()
    test_prompt_streaming()
    test_permission_default_deny()
    test_permission_auto_optin()
    test_cancel_notification()
    test_cancel_serialization()
    test_cancel_timeout_rebuild()
    test_close_during_prompt()
    test_initialize_failure()
    test_session_new_failure_atomic()
    test_double_start_guard()
    test_aclose_serialized_with_stream()
    test_adapter_bridge()
    print("\nACP 全部通过")

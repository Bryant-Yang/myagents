"""ACP 层回归测试：全部走 fake server，不发真实请求。

运行：FAKE_ACP_STATE=/tmp/myagents_fake_acp_state .venv/bin/python tests/test_acp.py
（文件顶部已自设该环境变量，直接跑即可）
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acp.adapter import AcpAdapter, AcpOpenCodeAdapter
from acp.client import AcpClient, AcpError
from adapters.base import (
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    ExecutionMode,
)
from clipboard_image import TrustedImage

SERVER = str(Path(__file__).parent / "fake_acp_server.py")
STATE = "/tmp/myagents_fake_acp_state"
TMP_WORKDIR = str(Path("/tmp").resolve())
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


def test_env_removals_override_ambient_and_survive_reset() -> None:
    """Every rebuilt child drops explicit ambient keys before overrides."""
    async def run() -> None:
        reset_state()
        removed_key = "MYAGENTS_TEST_REMOVE"
        replaced_key = "MYAGENTS_TEST_REPLACE"
        with patch.dict(os.environ, {
            removed_key: "ambient-secret",
            replaced_key: "ambient-value",
            "FAKE_ACP_ENV_PROBE_KEYS": f"{removed_key},{replaced_key}",
        }, clear=False):
            adapter = AcpAdapter(
                "fake", [sys.executable, SERVER],
                env_removals={removed_key, replaced_key},
                env_overrides={replaced_key: "explicit-safe-value"},
            )
            async for _event in adapter.stream("first", "/tmp"):
                pass
            try:
                async for _event in adapter.stream("disconnect", "/tmp"):
                    pass
            except AgentDeliveryUncertainError:
                pass
            else:
                raise AssertionError("disconnect 必须 poison 并触发 reset")
            async for _event in adapter.stream("after reset", "/tmp"):
                pass
            await adapter.aclose()

        env_events = [
            item for item in state_events() if item.startswith("process-env:")
        ]
        assert len(env_events) == 2, env_events
        expected = {
            removed_key: None,
            replaced_key: "explicit-safe-value",
        }
        assert all(
            json.loads(item.removeprefix("process-env:"))
            == expected
            for item in env_events
        ), env_events

    asyncio.run(run())
    print("ok  env removals 先清 ambient、override 后写入、reset 后保留")


def test_default_acp_byte_limits_fit_20_mib_image_frame() -> None:
    """Default bounds remain compatible with a base64-encoded 20 MiB PNG."""
    raw_bytes = 20 * 1024 * 1024
    base64_bytes = ((raw_bytes + 2) // 3) * 4
    client = AcpClient([sys.executable, SERVER])
    adapter = AcpAdapter("fake", [sys.executable, SERVER])
    assert client._inbound_frame_byte_limit > base64_bytes
    assert adapter._prompt_update_queue_byte_limit >= (
        client._inbound_frame_byte_limit)
    asyncio.run(adapter.aclose())

    print("ok  ACP 默认字节上限容纳 20 MiB PNG base64 帧")


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


def test_permission_rejects_cross_session_without_ui_activity() -> None:
    """A permission from any session except the active prompt is stale."""
    async def run() -> None:
        reset_state()
        handler_calls = []
        activity = []

        async def handler(params: dict) -> dict:
            handler_calls.append(params)
            return {"outcome": "selected", "optionId": "allow"}

        client = await make_client(permission_handler=handler)
        sid = await client.session_new("/tmp")
        client.on_permission_activity = (
            lambda phase, rid, params: activity.append((phase, rid, params)))
        result = await client.prompt(sid, "perm-wrong-session")
        assert result["stopReason"] == "end_turn"
        await client.close()
        permission = [
            item for item in state_events() if item.startswith("permission:")
        ][0]
        assert '"outcome": "cancelled"' in permission, permission
        assert handler_calls == []
        assert activity == [], "foreign permission 不得暂停当前 watchdog"

    asyncio.run(run())
    print("ok  跨 session permission 不进 UI、fail-closed")


def test_permission_duplicate_option_ids_fail_closed() -> None:
    """Ambiguous allow/reject option IDs can never authorize a tool."""
    async def run() -> None:
        reset_state()

        async def handler(_params: dict) -> dict:
            # A UI can only return the duplicated wire value.  Even if the
            # user intended reject, a malicious peer could interpret the first
            # matching option as allow, so validation must cancel instead.
            return {"outcome": "selected", "optionId": "duplicate"}

        client = await make_client(permission_handler=handler)
        sid = await client.session_new("/tmp")
        result = await client.prompt(sid, "perm-duplicate")
        assert result["stopReason"] == "end_turn"
        await client.close()
        permission = [
            item for item in state_events()
            if item.startswith("permission:")
        ][0]
        assert '"outcome": "cancelled"' in permission, permission
        assert '"optionId"' not in permission, permission

    asyncio.run(run())
    print("ok  permission 重复 optionId 歧义 fail-closed")


def test_permission_after_terminal_is_stale() -> None:
    """A request arriving after terminal must never enter the UI."""
    async def run() -> None:
        reset_state()
        handler_calls = []
        activity = []

        async def handler(params: dict) -> dict:
            handler_calls.append(params)
            return {"outcome": "selected", "optionId": "allow"}

        client = await make_client(permission_handler=handler)
        sid = await client.session_new("/tmp")
        client.on_permission_activity = (
            lambda phase, rid, params: activity.append((phase, rid, params)))
        result = await client.prompt(sid, "perm-after-terminal")
        assert result["stopReason"] == "end_turn"
        for _ in range(100):
            if any(item.startswith("async-permission:903:")
                   for item in state_events()):
                break
            await asyncio.sleep(0.01)
        response = [
            item for item in state_events()
            if item.startswith("async-permission:903:")
        ][0]
        assert '"outcome": "cancelled"' in response, response
        assert handler_calls == [] and activity == []
        await client.close()

    asyncio.run(run())
    print("ok  terminal 后迟到 permission 直接 cancelled")


def test_terminal_cancels_unresolved_permission_and_blocks_late_allow() -> None:
    """Terminal reaps its permission task before prompt can complete."""
    async def run() -> None:
        reset_state()
        started = asyncio.Event()
        never = asyncio.Event()
        activity = []

        async def stubborn_handler(_params: dict) -> dict:
            started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                # Simulate a buggy UI handler suppressing cancellation.  The
                # closed prompt token must still turn this late allow into deny.
                return {"outcome": "selected", "optionId": "allow"}

        client = await make_client(permission_handler=stubborn_handler)
        sid = await client.session_new("/tmp")
        client.on_permission_activity = (
            lambda phase, rid, params: activity.append((phase, rid)))
        result = await client.prompt(sid, "perm-terminal-first")
        assert result["stopReason"] == "end_turn"
        await asyncio.wait_for(started.wait(), timeout=1)
        for _ in range(100):
            if any(item.startswith("async-permission:902:")
                   for item in state_events()):
                break
            await asyncio.sleep(0.01)
        response = [
            item for item in state_events()
            if item.startswith("async-permission:902:")
        ][0]
        assert '"outcome": "cancelled"' in response, response
        assert [phase for phase, _rid in activity] == ["requested", "resolved"]
        assert not client._permission_tasks
        await client.close()

    asyncio.run(run())
    print("ok  terminal 先取消 permission，迟到 allow 无效")


def test_terminal_permission_reap_is_bounded_and_poisons() -> None:
    """A handler that suppresses cancellation cannot stall terminal forever."""
    async def run() -> None:
        reset_state()
        first_wait = asyncio.Event()
        second_wait = asyncio.Event()
        started = asyncio.Event()

        async def cancellation_suppressing_handler(_params: dict) -> dict:
            started.set()
            try:
                await first_wait.wait()
            except asyncio.CancelledError:
                # A broken UI callback ignores the first cancellation and
                # starts another indefinite wait.
                await second_wait.wait()
            return {"outcome": "selected", "optionId": "allow"}

        client = await make_client(
            permission_handler=cancellation_suppressing_handler,
            permission_reap_timeout=0.05,
        )
        sid = await client.session_new("/tmp")
        try:
            await asyncio.wait_for(
                client.prompt(sid, "perm-terminal-first"), timeout=0.5)
        except AcpError as exc:
            assert "permission" in str(exc) and "回收" in str(exc), exc
        else:
            raise AssertionError("不可回收 permission task 必须 poison")
        await asyncio.wait_for(started.wait(), timeout=0.1)
        assert not client._permission_tasks
        assert not client._permission_task_scopes
        assert not client._permission_request_ids
        await asyncio.wait_for(client.close(), timeout=0.5)

    asyncio.run(run())
    print("ok  permission handler 吞取消也只能有界 poison")


def test_permission_response_backpressure_cannot_deadlock_terminal() -> None:
    """Terminal cancellation can break a permission send holding the state lock."""
    async def run() -> None:
        reset_state()
        client = await make_client(
            permission="auto",
            permission_reap_timeout=0.2,
        )
        sid = await client.session_new("/tmp")
        result = await asyncio.wait_for(
            client.prompt(sid, "perm-response-backpressure"), timeout=1)
        assert result["stopReason"] == "end_turn"
        assert not client._permission_tasks
        assert not client._permission_task_scopes
        assert not client._permission_request_ids
        await asyncio.wait_for(client.close(), timeout=1)

    asyncio.run(run())
    print("ok  permission response drain 背压不锁死 terminal/close")


def test_cancel_cancels_permission_before_session_cancel() -> None:
    """Cancel resolves the human wait fail-closed before cancelling the turn."""
    async def run() -> None:
        reset_state()
        started = asyncio.Event()
        never = asyncio.Event()

        async def stubborn_handler(_params: dict) -> dict:
            started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                return {"outcome": "selected", "optionId": "allow"}

        adapter = AcpAdapter(
            "fake", [sys.executable, SERVER],
            permission_handler=stubborn_handler,
            cancel_timeout=1,
        )

        async def consume() -> None:
            async for _event in adapter.stream("perm slow", "/tmp"):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        try:
            await task
        except AgentDeliveryCancelledError:
            pass
        else:
            raise AssertionError("submitted prompt cancel 必须保持 no-replay")
        permission = [
            item for item in state_events() if item.startswith("permission:")
        ][0]
        assert '"outcome": "cancelled"' in permission, permission
        events = state_events()
        assert events.index(permission) < events.index("cancel:fake-session-1")
        assert not adapter._client._permission_tasks
        await adapter.aclose()

    asyncio.run(run())
    print("ok  cancel 先 fail-closed 回收 permission，再中断轮次")


def test_permission_queue_poison_cancels_stale_decision() -> None:
    """Prompt queue overflow poisons the connection and invalidates permission."""
    async def run() -> None:
        reset_state()
        started = asyncio.Event()
        never = asyncio.Event()

        async def stubborn_handler(_params: dict) -> dict:
            started.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                return {"outcome": "selected", "optionId": "allow"}

        adapter = AcpAdapter(
            "fake", [sys.executable, SERVER],
            permission_handler=stubborn_handler,
            cancel_timeout=1,
        )
        old_client = adapter._client
        try:
            async for _event in adapter.stream("perm-overflow", "/tmp"):
                pass
        except AgentDeliveryUncertainError as exc:
            assert "更新队列" in str(exc), exc
        else:
            raise AssertionError("permission + queue overflow 必须失败")
        await asyncio.wait_for(started.wait(), timeout=1)
        response = [
            item for item in state_events()
            if item.startswith("async-permission:904:")
        ][0]
        assert '"outcome": "cancelled"' in response, response
        assert adapter._client is not old_client
        assert adapter._started is False and adapter.session_id is None
        await adapter.aclose()

    asyncio.run(run())
    print("ok  queue poison 回收 permission 并重建连接")


def test_permission_flood_is_bounded_and_overflow_is_cancelled() -> None:
    """Only sixteen permission decisions may be pending for one prompt."""
    async def run() -> None:
        reset_state()
        never = asyncio.Event()
        handler_calls: list[dict] = []
        requested: list[int | str] = []

        async def hanging_handler(params: dict) -> dict:
            handler_calls.append(params)
            await never.wait()
            return {"outcome": "selected", "optionId": "allow"}

        client = await make_client(permission_handler=hanging_handler)
        sid = await client.session_new("/tmp")
        client.on_permission_activity = (
            lambda phase, rid, _params: requested.append(rid)
            if phase == "requested" else None)
        result = await asyncio.wait_for(
            client.prompt(sid, "permission-flood-17"), timeout=2)
        assert result["stopReason"] == "end_turn"
        for _ in range(100):
            responses = [
                item for item in state_events()
                if item.startswith("permission-flood-response:")
            ]
            if len(responses) == 17:
                break
            await asyncio.sleep(0.01)
        assert len(handler_calls) == 16, handler_calls
        assert len(requested) == 16, requested
        assert len(responses) == 17, responses
        assert all('"outcome": "cancelled"' in item for item in responses)
        assert not client._permission_tasks
        await client.close()

    asyncio.run(run())
    print("ok  permission flood 限界为 16，超额请求直接 cancelled")


def test_pending_permission_bytes_are_bounded() -> None:
    """Several medium permission payloads cannot multiply frame memory."""
    async def run() -> None:
        reset_state()
        never = asyncio.Event()
        handler_calls: list[dict] = []

        async def hanging_handler(params: dict) -> dict:
            handler_calls.append(params)
            await never.wait()
            return {"outcome": "selected", "optionId": "allow"}

        client = await make_client(
            permission_handler=hanging_handler,
            pending_permission_byte_limit=1024,
        )
        sid = await client.session_new("/tmp")
        result = await client.prompt(sid, "permission-byte-flood")
        assert result["stopReason"] == "end_turn"
        for _ in range(100):
            responses = [
                item for item in state_events()
                if item.startswith("permission-byte-response:")
            ]
            if len(responses) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(handler_calls) == 1, handler_calls
        assert len(responses) == 2, responses
        assert all('"outcome": "cancelled"' in item for item in responses)
        assert client._pending_permission_bytes == 0
        assert not client._permission_tasks
        await client.close()

    asyncio.run(run())
    print("ok  pending permission 同时受 16 条与累计字节预算约束")


def test_duplicate_active_permission_id_poisons_and_recovers() -> None:
    """A duplicate in-flight reverse request id is connection ambiguity."""
    async def run() -> None:
        reset_state()
        never = asyncio.Event()
        handler_calls: list[dict] = []
        requested: list[int | str] = []

        async def hanging_handler(params: dict) -> dict:
            handler_calls.append(params)
            await never.wait()
            return {"outcome": "selected", "optionId": "allow"}

        adapter = AcpAdapter(
            "fake", [sys.executable, SERVER],
            permission_handler=hanging_handler,
            cancel_timeout=0.2,
        )
        old_client = adapter._client
        old_client.on_permission_activity = (
            lambda phase, rid, _params: requested.append(rid)
            if phase == "requested" else None)
        events = []
        try:
            async for event in adapter.stream(
                    "perm-duplicate-active-id", "/tmp"):
                events.append(event)
        except AgentDeliveryUncertainError as exc:
            assert "duplicate active reverse request id" in str(exc), exc
        else:
            raise AssertionError("重复 active reverse request id 必须 poison")
        assert [event.kind for event in events].count(
            "delivery_committed") == 1, events
        assert len(handler_calls) == 1, handler_calls
        # AcpAdapter replaces its callback during a prompt; the handler count
        # is the public proof that only the first request reached the UI seam.
        responses = [
            item for item in state_events()
            if item.startswith("duplicate-permission-response:950:")
        ]
        assert len(responses) <= 1, responses
        assert adapter._client is not old_client
        assert adapter._started is False and adapter.session_id is None

        texts = [
            event.text
            async for event in adapter.stream("fast recovery", "/tmp")
            if event.kind == "text"
        ]
        assert texts == ["PO", "NG"]
        assert state_events().count(f"new:{TMP_WORKDIR}") == 2, state_events()
        await adapter.aclose()

    asyncio.run(run())
    print("ok  duplicate permission id → poison/no-replay，新连接恢复")


def test_invalid_permission_request_id_poisons_without_ui() -> None:
    """Reverse request ids must be non-empty string or exact integer scalars."""
    async def run() -> None:
        reset_state()
        handler_calls: list[dict] = []

        async def handler(params: dict) -> dict:
            handler_calls.append(params)
            return {"outcome": "selected", "optionId": "allow"}

        adapter = AcpAdapter(
            "fake", [sys.executable, SERVER],
            permission_handler=handler,
        )
        events = []
        try:
            async for event in adapter.stream(
                    "perm-invalid-request-id", "/tmp"):
                events.append(event)
        except AgentDeliveryUncertainError as exc:
            assert "reverse request id" in str(exc), exc
        else:
            raise AssertionError("非法 reverse request id 必须 poison")
        assert handler_calls == []
        assert [event.kind for event in events].count(
            "delivery_committed") == 1, events
        assert adapter._started is False and adapter.session_id is None
        await adapter.aclose()

    asyncio.run(run())
    print("ok  非 scalar reverse request id 不进 UI、poison 连接")


def test_string_permission_request_id_is_supported() -> None:
    """ACP peers may use a string JSON-RPC id for reverse requests."""
    async def run() -> None:
        reset_state()
        client = await make_client()
        sid = await client.session_new("/tmp")
        result = await client.prompt(sid, "perm-string-id")
        assert result["stopReason"] == "end_turn"
        permission = [
            item for item in state_events() if item.startswith("permission:")
        ][0]
        assert '"outcome": "cancelled"' in permission, permission
        await client.close()

    asyncio.run(run())
    print("ok  string reverse request id 正常关联")


def test_giant_string_permission_request_id_poisons_before_ui() -> None:
    """A frame-sized string id cannot bypass the permission byte budget."""
    async def run() -> None:
        reset_state()
        handler_calls = []

        async def handler(params: dict) -> dict:
            handler_calls.append(params)
            await asyncio.Event().wait()
            return {"outcome": "cancelled"}

        client = await make_client(permission_handler=handler)
        sid = await client.session_new("/tmp")
        try:
            await client.prompt(sid, "perm-giant-string-id")
        except AcpError as exc:
            assert "string id" in str(exc) and "上限" in str(exc), exc
        else:
            raise AssertionError("巨型 reverse request id 必须 poison")
        assert handler_calls == []
        assert not client._permission_tasks
        assert not client._permission_request_ids
        assert client._pending_permission_bytes == 0
        await client.close()

    asyncio.run(run())
    print("ok  巨型 string permission id 不绕过字节预算")


def test_inbound_frame_limits_poison_connection_no_replay() -> None:
    """Oversized newline and unterminated frames are both bounded."""
    async def exercise(prompt: str) -> None:
        reset_state()
        with patch.dict(os.environ, {
            "FAKE_ACP_FRAME_PAYLOAD_BYTES": "2048",
        }, clear=False):
            adapter = AcpAdapter(
                "fake", [sys.executable, SERVER],
                # Keep initialize (including the DSH compatibility metadata)
                # below the test bound while the 2 KiB prompt payload still
                # exceeds it in both newline and unterminated forms.
                inbound_frame_byte_limit=1024,
                cancel_timeout=0.1,
            )
            old_client = adapter._client
            events = []
            try:
                async for event in adapter.stream(prompt, "/tmp"):
                    events.append(event)
            except AgentDeliveryUncertainError as exc:
                assert "inbound frame" in str(exc), exc
            else:
                raise AssertionError(f"oversized ACP frame 必须失败: {prompt}")
            assert [event.kind for event in events].count(
                "delivery_committed") == 1, events
            assert adapter._client is not old_client
            assert adapter._started is False and adapter.session_id is None
            await adapter.aclose()

    async def run() -> None:
        await exercise("giant-frame")
        await exercise("unterminated-frame")

    asyncio.run(run())
    print("ok  ACP 巨帧/无换行超限 → poison + no-replay")


def test_prompt_update_queue_byte_limit_poison_no_replay() -> None:
    """Multiple medium notifications cannot bypass the queue count limit."""
    async def run() -> None:
        reset_state()
        with patch.dict(os.environ, {
            "FAKE_ACP_MEDIUM_UPDATE_COUNT": "12",
            "FAKE_ACP_MEDIUM_UPDATE_BYTES": "256",
        }, clear=False):
            adapter = AcpAdapter(
                "fake", [sys.executable, SERVER],
                inbound_frame_byte_limit=4096,
                prompt_update_queue_byte_limit=700,
            )
            old_client = adapter._client
            events = []
            try:
                async for event in adapter.stream("medium-byte-flood", "/tmp"):
                    events.append(event)
            except AgentDeliveryUncertainError as exc:
                assert "字节上限" in str(exc), exc
            else:
                raise AssertionError("prompt queue 累计字节超限必须失败")
            assert [event.kind for event in events].count(
                "delivery_committed") == 1, events
            assert adapter._client is not old_client
            assert adapter._started is False and adapter.session_id is None
            await adapter.aclose()

    asyncio.run(run())
    print("ok  ACP 多条中等通知字节超限 → poison + no-replay")


def test_single_oversized_notification_wakes_queue_consumer() -> None:
    """A rejected first item must wake a consumer already blocked on get()."""
    async def run() -> None:
        reset_state()
        with patch.dict(os.environ, {
            "FAKE_ACP_MEDIUM_UPDATE_COUNT": "1",
            "FAKE_ACP_MEDIUM_UPDATE_BYTES": "2048",
        }, clear=False):
            adapter = AcpAdapter(
                "fake", [sys.executable, SERVER],
                inbound_frame_byte_limit=4096,
                prompt_update_queue_byte_limit=700,
                inactivity_timeout=0.1,
            )
            old_client = adapter._client
            events = []
            try:
                async for event in adapter.stream(
                        "single-byte-overflow", "/tmp"):
                    events.append(event)
            except AgentDeliveryUncertainError as exc:
                assert "字节上限" in str(exc), exc
            else:
                raise AssertionError("首条通知超限必须立即唤醒并失败")
            assert [event.kind for event in events].count(
                "delivery_committed") == 1, events
            assert adapter._client is not old_client
            assert adapter._started is False and adapter.session_id is None
            await adapter.aclose()

    asyncio.run(run())
    print("ok  单通知 byte overflow 立即唤醒 consumer")


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


def test_cancel_send_backpressure_is_bounded_and_rebuilds() -> None:
    """A full stdin pipe cannot trap cancellation behind the write lock."""
    async def run() -> None:
        reset_state()
        with patch.dict(os.environ, {
            "FAKE_ACP_STOP_READING_AFTER_NEW": "1",
        }, clear=False):
            adapter = AcpAdapter(
                "fake", [sys.executable, SERVER], cancel_timeout=0.05)
            old_client = adapter._client

            async def consume() -> None:
                async for _event in adapter.stream(
                        "X" * (4 * 1024 * 1024), "/tmp"):
                    pass

            task = asyncio.create_task(consume())
            for _ in range(100):
                if "stop-reading-after-new" in state_events():
                    break
                await asyncio.sleep(0.01)
            assert "stop-reading-after-new" in state_events()
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1)
            except AgentDeliveryCancelledError:
                pass
            else:
                raise AssertionError("cancel drain 背压必须形成 no-replay 取消")
            assert adapter._client is not old_client
            assert adapter._started is False and adapter.session_id is None

            texts = [
                event.text
                async for event in adapter.stream("recovered", "/tmp")
                if event.kind == "text"
            ]
            assert texts == ["PO", "NG"]
            await adapter.aclose()

    asyncio.run(run())
    print("ok  client.cancel 写背压受 cancel_timeout 限界并重建")


def test_prompt_drain_failure_after_write_is_no_replay() -> None:
    """Once write() starts, drain failure is uncertain rather than not-sent."""
    async def run() -> None:
        reset_state()
        with patch.dict(os.environ, {
            "FAKE_ACP_EXIT_AFTER_NEW": "1",
        }, clear=False):
            adapter = AcpAdapter("fake", [sys.executable, SERVER])
            old_client = adapter._client
            events = []
            try:
                async for event in adapter.stream(
                        "Y" * (4 * 1024 * 1024), "/tmp"):
                    events.append(event)
            except AgentDeliveryUncertainError:
                pass
            else:
                raise AssertionError("write-started drain failure 必须 no-replay")
            assert [event.kind for event in events].count(
                "delivery_committed") == 1, events
            assert adapter._client is not old_client
            assert adapter._started is False and adapter.session_id is None
            assert "exit-after-new" in state_events()

            texts = [
                event.text
                async for event in adapter.stream("recovered", "/tmp")
                if event.kind == "text"
            ]
            assert texts == ["PO", "NG"]
            await adapter.aclose()

    asyncio.run(run())
    print("ok  stdin.write 后 drain 失败 → committed/uncertain/no-replay")


def test_cancel_before_first_update_is_no_replay() -> None:
    """prompt 已到 server 但还没有 update 时，取消仍须形成 no-replay。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        old_client = adapter._client

        async def consume() -> None:
            async for _event in adapter.stream("silent-slow task", "/tmp"):
                pass

        task = asyncio.create_task(consume())
        for _ in range(200):
            if "prompt:silent-slow task" in state_events():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("fake server 未收到 silent prompt")
        task.cancel()
        try:
            await task
        except AgentDeliveryCancelledError:
            pass
        else:
            raise AssertionError("已发送 prompt 的取消必须携带 no-replay 语义")

        assert adapter._client is old_client, (
            "严格 cancelled 终局后可以复用已确认停止的连接")
        assert "cancel-complete" in state_events()
        await adapter.aclose()

    asyncio.run(run())
    print("ok  首个 update 前取消 → confirmed cancelled + no-replay")


def test_cancel_requires_exact_cancelled_terminal() -> None:
    """end_turn/空/未知终局都不能当成中断确认，连接必须作废。"""
    async def run() -> None:
        for stop_reason in ("end_turn", "__missing__", "future_cancel_state"):
            reset_state()
            with patch.dict(os.environ, {
                "FAKE_ACP_CANCEL_STOP_REASON": stop_reason,
            }, clear=False):
                adapter = AcpAdapter(
                    "fake", [sys.executable, SERVER], cancel_timeout=1)
                old_client = adapter._client

                async def consume() -> None:
                    async for _event in adapter.stream(
                        "silent-slow task", "/tmp",
                    ):
                        pass

                task = asyncio.create_task(consume())
                for _ in range(200):
                    if "prompt:silent-slow task" in state_events():
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("fake server 未收到 silent prompt")
                task.cancel()
                try:
                    await task
                except AgentDeliveryCancelledError:
                    pass
                else:
                    raise AssertionError(
                        f"{stop_reason!r} 取消竞态必须携带 no-replay 语义")
                assert adapter._client is not old_client
                assert adapter._started is False
                await adapter.aclose()

    asyncio.run(run())
    print("ok  取消只接受 exact cancelled terminal")


def test_stream_aclose_requires_exact_cancelled_terminal() -> None:
    """stream.aclose 主动 cancel 后也只能在 exact cancelled 后复用。"""
    async def run() -> None:
        for stop_reason in ("end_turn", "__missing__", "future_cancel_state"):
            reset_state()
            with patch.dict(os.environ, {
                "FAKE_ACP_CANCEL_STOP_REASON": stop_reason,
            }, clear=False):
                adapter = AcpAdapter(
                    "fake", [sys.executable, SERVER], cancel_timeout=1)
                old_client = adapter._client
                stream = adapter.stream("slow task", "/tmp")
                first = await first_text(stream)
                assert first.kind == "text"

                await stream.aclose()

                assert "cancel-complete" in state_events()
                assert adapter._client is not old_client, stop_reason
                assert adapter._started is False
                await adapter.aclose()

    asyncio.run(run())
    print("ok  stream.aclose 取消只接受 exact cancelled terminal")


def test_inactivity_cancel_requires_exact_cancelled_terminal() -> None:
    """inactivity 主动 cancel 不得把 end_turn/空/未知当中断确认。"""
    async def run() -> None:
        for stop_reason in ("end_turn", "__missing__", "future_cancel_state"):
            reset_state()
            with patch.dict(os.environ, {
                "FAKE_ACP_CANCEL_STOP_REASON": stop_reason,
            }, clear=False):
                adapter = AcpAdapter(
                    "fake", [sys.executable, SERVER],
                    inactivity_timeout=0.05, cancel_timeout=1,
                )
                old_client = adapter._client

                try:
                    async for _event in adapter.stream(
                        "silent-slow task", "/tmp",
                    ):
                        pass
                except AgentDeliveryUncertainError as exc:
                    assert "无活动" in str(exc), exc
                else:
                    raise AssertionError("inactivity 必须保持 no-replay 失败语义")

                assert "cancel-complete" in state_events()
                assert adapter._client is not old_client, stop_reason
                assert adapter._started is False
                await adapter.aclose()

    asyncio.run(run())
    print("ok  inactivity 取消只接受 exact cancelled terminal")


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


def test_bool_response_id_cannot_alias_integer_request() -> None:
    """Python bool/int equality must not mis-correlate JSON-RPC responses."""
    async def run() -> None:
        reset_state()
        with patch.dict(os.environ, {
            "FAKE_ACP_BOOL_NEW_RESPONSE_ID": "1",
        }, clear=False):
            adapter = AcpAdapter("fake", [sys.executable, SERVER])
            old_client = adapter._client
            try:
                async for _event in adapter.stream("hi", "/tmp"):
                    pass
            except AcpError as exc:
                assert "response id" in str(exc), exc
            else:
                raise AssertionError("bool response id 必须 poison")
            assert adapter._client is not old_client
            assert adapter._started is False and adapter.session_id is None
            await adapter.aclose()

    asyncio.run(run())
    print("ok  bool response id 不与 integer request id 混淆")


def test_session_new_timeout_atomic() -> None:
    """Post-initialize session/new cannot hang the adapter indefinitely."""
    async def run() -> None:
        reset_state()
        with patch.dict(os.environ, {"FAKE_ACP_HANG_NEW": "1"}, clear=False):
            adapter = AcpAdapter(
                "fake", [sys.executable, SERVER],
                session_prepare_timeout=0.05,
            )
            old_client = adapter._client
            try:
                async for _event in adapter.stream("hi", "/tmp"):
                    pass
            except AcpError as exc:
                assert "session/new" in str(exc) and "连接已回收" in str(exc)
            else:
                raise AssertionError("session/new 超时必须失败")
            assert adapter._client is not old_client
            assert adapter._started is False and adapter.session_id is None
            assert old_client._proc.returncode is not None
            assert not any(item.startswith("prompt:") for item in state_events())
            await adapter.aclose()

    asyncio.run(run())
    print("ok  session/new 超时 → 原子回收")


def test_session_load_timeout_covers_stdio_drain() -> None:
    """The load timeout starts before stdio drain, not after the write."""
    async def run() -> None:
        reset_state()
        old_client: AcpClient | None = None
        with patch.dict(os.environ, {
            "FAKE_ACP_STOP_READING_AFTER_INIT": "1",
        }, clear=False):
            old_client = await make_client()
            try:
                # Once the peer stops reading, a request much larger than the
                # pipe's high-water mark blocks in drain.  session/load must
                # still time out and atomically recycle the child.
                await asyncio.wait_for(
                    old_client.session_load(
                        "blocked-" + ("s" * (2 * 1024 * 1024)),
                        TMP_WORKDIR,
                        timeout=0.05,
                    ),
                    timeout=2,
                )
            except AcpError as exc:
                assert "session/load" in str(exc) and "连接已回收" in str(exc)
            else:
                raise AssertionError("session/load drain 背压必须有界失败")
            assert old_client._proc is not None
            assert old_client._proc.returncode is not None

        # A poisoned transport is never reused; a fresh child remains usable.
        recovered = await make_client()
        assert await recovered.session_new(TMP_WORKDIR) == "fake-session-1"
        await recovered.close()
        assert "stop-reading-after-init" in state_events()

    asyncio.run(run())
    print("ok  session/load timeout 覆盖 stdio drain + 新连接恢复")


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


def test_aclose_session_close_backpressure_is_outer_bounded() -> None:
    """Exit reaches force teardown even if session/close never reaches reply."""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        async for _event in adapter.stream("fast round", "/tmp"):
            pass
        old_proc = adapter._client._proc
        assert old_proc is not None and old_proc.returncode is None

        blocked = asyncio.Event()

        async def blocked_session_close(
            _session_id: str,
            *,
            timeout: float | None = None,
        ) -> None:
            del timeout
            await blocked.wait()

        # This represents a client stuck before its own request timeout can
        # begin (for example waiting on write_lock/drain).  The adapter-level
        # timeout must still advance to client.close()/process-group teardown.
        adapter._client.session_close = blocked_session_close  # type: ignore[method-assign]
        with patch("acp.adapter._SESSION_CLOSE_TIMEOUT", 0.05):
            await asyncio.wait_for(adapter.aclose(), timeout=0.5)
        assert old_proc.returncode is not None

    asyncio.run(run())
    print("ok  aclose session/close 写背压外层有界 + force teardown")


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


def test_child_process_cwd_is_bound_to_session_workdir() -> None:
    """The ACP host process must boot inside the canonical session workdir."""
    async def run() -> None:
        reset_state()
        with tempfile.TemporaryDirectory() as workdir:
            adapter = AcpAdapter("fake", [sys.executable, SERVER])
            async for _event in adapter.stream("hi", workdir):
                pass
            expected = str(Path(workdir).resolve())
            assert f"process-cwd:{expected}" in state_events()
            assert adapter._client.cwd == expected
            await adapter.aclose()

    asyncio.run(run())
    print("ok  ACP child cwd 绑定 session workdir")


def test_relative_workdir_is_absolute_on_process_and_wire() -> None:
    """The canonical cwd returned by binding is also sent on ACP session/new."""
    async def run() -> None:
        reset_state()
        with tempfile.TemporaryDirectory(dir=".") as workdir:
            relative = os.path.relpath(workdir, Path.cwd())
            expected = str(Path(workdir).resolve())
            adapter = AcpAdapter("fake", [sys.executable, SERVER])
            async for _event in adapter.stream("hi", relative):
                pass
            events = state_events()
            assert f"process-cwd:{expected}" in events, events
            assert f"new:{expected}" in events, events
            await adapter.aclose()

    asyncio.run(run())
    print("ok  相对 workdir 在 process cwd 与 ACP wire 上均绝对化")


def test_active_session_rejects_workdir_switch() -> None:
    """A live native session cannot be silently retargeted to another cwd."""
    async def run() -> None:
        reset_state()
        with (tempfile.TemporaryDirectory() as first,
              tempfile.TemporaryDirectory() as second):
            adapter = AcpAdapter("fake", [sys.executable, SERVER])
            async for _event in adapter.stream("first", first):
                pass
            session_id = adapter.session_id
            client = adapter._client
            try:
                async for _event in adapter.stream("second", second):
                    pass
            except AcpError as exc:
                assert "拒绝切换" in str(exc), exc
            else:
                raise AssertionError("活跃 ACP session 不得切换 cwd")
            assert adapter._client is client
            assert adapter.session_id == session_id
            events = state_events()
            assert "prompt:first" in events
            assert "prompt:second" not in events
            assert events.count("new:" + str(Path(first).resolve())) == 1
            await adapter.aclose()

    asyncio.run(run())
    print("ok  活跃 ACP session 拒绝异 cwd 重定向")


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


def test_tool_tracking_fields_are_independently_bounded() -> None:
    """Each untrusted tool metadata field is bounded before tracking."""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        events = [
            event async for event in adapter.stream(
                "oversized-tool-fields", "/tmp")
        ]
        tools = [event for event in events if event.kind == "tool"]
        assert len(tools) == 2, tools
        first, terminal = tools
        assert len(first.text.encode()) <= 500
        assert len(first.meta["tool_call_id"].encode()) <= 256
        assert len(first.meta["tool_kind"].encode()) <= 100
        assert len(first.meta["status"].encode()) <= 200
        assert len(first.meta["command"].encode()) <= 2000
        assert terminal.meta["tool_call_id"] == first.meta["tool_call_id"]
        assert terminal.meta["status"] == "completed"
        await adapter.aclose()

    asyncio.run(run())
    print("ok  tool title/id/kind/status/command 入 tracking 前独立限界")


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


def test_restore_missing_resource_fallback_new() -> None:
    """M2.5：standard resource-not-found may create a fresh session."""
    async def run() -> None:
        with patch.dict(os.environ, {
            "FAKE_ACP_FAIL_LOAD": "1",
            "FAKE_ACP_FAIL_LOAD_CODE": "-32002",
            "FAKE_ACP_FAIL_LOAD_MESSAGE": "session resource not found",
        }, clear=False):
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
            assert evs.index("load:old-session-9") < evs.index(
                f"new:{TMP_WORKDIR}")
            assert evs.index(f"new:{TMP_WORKDIR}") < evs.index("prompt:hi")
            await adapter.aclose()
    asyncio.run(run())
    print("ok  restore：standard resource-not-found → session/new")


def test_restore_standard_load_rejections_fallback_new() -> None:
    """Resource/method absence are deterministic fresh-fallback cases."""
    async def run() -> None:
        for code in (-32002, -32601):
            reset_state()
            with patch.dict(os.environ, {
                "FAKE_ACP_FAIL_LOAD": "1",
                "FAKE_ACP_FAIL_LOAD_CODE": str(code),
            }, clear=False):
                adapter = AcpAdapter("fake", [sys.executable, SERVER])
                seen = []
                async for _event in adapter.stream_prepared(
                        lambda prep: seen.append(prep) or "hi",
                        "/tmp", resume_session_id="missing-session"):
                    pass
                assert seen[0].load_failed is True
                events = state_events()
                assert events.index("load:missing-session") < events.index(
                    f"new:{TMP_WORKDIR}")
                await adapter.aclose()

    asyncio.run(run())
    print("ok  restore：resource/method 明确拒绝 → fresh")


def test_opencode_exact_session_not_found_fallback_is_adapter_local() -> None:
    """OpenCode's evidenced -32602 mapping must not weaken generic ACP."""
    async def run() -> None:
        reset_state()
        with patch.dict(os.environ, {
            "FAKE_ACP_FAIL_LOAD": "1",
            "FAKE_ACP_FAIL_LOAD_CODE": "-32602",
            "FAKE_ACP_FAIL_LOAD_MESSAGE": "session not found: old-session-9",
        }, clear=False):
            adapter = AcpOpenCodeAdapter(
                fallback_jsonl=False,
                cmd=[sys.executable, SERVER],
            )
            seen = []
            async for _event in adapter.stream_prepared(
                    lambda prep: seen.append(prep) or "hi",
                    "/tmp", resume_session_id="old-session-9"):
                pass
            assert seen[0].load_failed is True
            events = state_events()
            assert events.index("load:old-session-9") < events.index(
                f"new:{TMP_WORKDIR}")
            await adapter.aclose()

    asyncio.run(run())
    print("ok  OpenCode -32602 exact session-not-found 只在具体 adapter fresh")


def test_restore_load_fatal_remote_errors_do_not_fallback() -> None:
    """Generic server/policy/quota errors cannot be bypassed by new."""
    async def run() -> None:
        cases = (
            (-32000, "Authentication required"),
            (-32000, "Permission denied by policy"),
            (-32000, "Unauthorized for tenant"),
            (-32000, "Unauthenticated user"),
            (-32000, "Access denied for workspace"),
            (-32000, "Tenant policy rejected load"),
            (-32000, "Backend database unavailable"),
            (-32000, "Policy rejected load"),
            (-32000, "Quota exceeded"),
            (-32602, "Invalid params"),
            (-32603, "Internal error"),
            (-32800, "Request cancelled"),
        )
        for code, message in cases:
            reset_state()
            with patch.dict(os.environ, {
                "FAKE_ACP_FAIL_LOAD": "1",
                "FAKE_ACP_FAIL_LOAD_CODE": str(code),
                "FAKE_ACP_FAIL_LOAD_MESSAGE": message,
            }, clear=False):
                adapter = AcpAdapter("fake", [sys.executable, SERVER])
                old_client = adapter._client
                try:
                    async for _event in adapter.stream_prepared(
                            lambda prep: "hi", "/tmp",
                            resume_session_id="protected-session"):
                        pass
                except AcpError as exc:
                    assert message in str(exc), exc
                else:
                    raise AssertionError(f"{code} {message} 不得 fresh fallback")
                assert not any(item.startswith("new:") for item in state_events())
                assert adapter._client is not old_client
                assert adapter._started is False and adapter.session_id is None
                await adapter.aclose()

    asyncio.run(run())
    print("ok  restore：backend/policy/quota/protocol 错误不回退")


def test_auth_notification_queue_has_byte_budget() -> None:
    """One giant auth notification cannot consume a frame-sized queue slot."""
    async def run() -> None:
        reset_state()

        async def handle_auth_notification(_method: str, _params: dict) -> None:
            return None

        with patch.dict(os.environ, {
            "FAKE_ACP_REQUIRE_AUTH": "1",
            "FAKE_ACP_AUTH_NOTIFICATION_COUNT": "1",
            "FAKE_ACP_AUTH_NOTIFICATION_BYTES": "2048",
        }, clear=False):
            adapter = AcpAdapter(
                "fake", [sys.executable, SERVER],
                auth_method="internal",
                auth_required=True,
                auth_notification_handler=handle_auth_notification,
                auth_notification_queue_byte_limit=512,
            )
            old_client = adapter._client
            try:
                async for _event in adapter.stream("hi", "/tmp"):
                    pass
            except AcpError as exc:
                assert "认证通知" in str(exc) and "字节上限" in str(exc), exc
            else:
                raise AssertionError("认证通知字节超限必须原子失败")
            assert not any(
                item.startswith("new:") for item in state_events()), state_events()
            assert adapter._client is not old_client
            assert adapter._started is False and adapter.session_id is None
            await adapter.aclose()

    asyncio.run(run())
    print("ok  auth prepare queue 同时受条数与字节预算约束")


def test_restore_load_transport_timeout_and_overflow_do_not_fallback() -> None:
    """Uncertain load failures poison/reset instead of creating a new session."""
    async def exercise(env: dict[str, str], *, auth_queue: bool = False) -> None:
        reset_state()
        with patch.dict(os.environ, env, clear=False):
            adapter = AcpAdapter(
                "fake", [sys.executable, SERVER],
                session_prepare_timeout=0.05,
                auth_method="internal" if auth_queue else None,
            )
            old_client = adapter._client
            try:
                async for _event in adapter.stream_prepared(
                        lambda prep: "hi", "/tmp",
                        resume_session_id="resume-uncertain"):
                    pass
            except AcpError:
                pass
            else:
                raise AssertionError(f"uncertain load 必须失败: {env}")
            assert "load:resume-uncertain" in state_events()
            assert not any(item.startswith("new:") for item in state_events())
            assert adapter._client is not old_client
            assert adapter._started is False and adapter.session_id is None
            await adapter.aclose()

    async def run() -> None:
        await exercise({"FAKE_ACP_HANG_LOAD": "1"})
        await exercise({"FAKE_ACP_DISCONNECT_LOAD": "1"})
        await exercise(
            {"FAKE_ACP_LOAD_NOTIFICATION_COUNT": "65"},
            auth_queue=True,
        )

    asyncio.run(run())
    print("ok  restore：timeout/disconnect/overflow 原子 reset、不 new")


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
            assert f"new:{TMP_WORKDIR}" in evs
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
        assert "load:s2" not in evs and f"new:{TMP_WORKDIR}" not in evs, evs
        await adapter.aclose()
    asyncio.run(run())
    print("ok  resume id 不偷换活跃 session")


def test_restore_new_error_propagates() -> None:
    """M2.5：只吞标准 load rejection；fresh new 失败依然传播。"""
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
            assert evs.index("load:old-9") < evs.index(f"new:{TMP_WORKDIR}")
            await asyncio.sleep(0.2)
        finally:
            del os.environ["FAKE_ACP_FAIL_LOAD"]
            del os.environ["FAKE_ACP_FAIL_NEW"]
            await adapter.aclose()
    asyncio.run(run())
    print("ok  restore：session/new 失败照常传播")


def test_prompt_error_propagates() -> None:
    """A drained prompt remote error is deterministic but still no-replay."""
    async def run() -> None:
        os.environ["FAKE_ACP_FAIL_PROMPT"] = "1"
        try:
            adapter = AcpAdapter("fake", [sys.executable, SERVER])
            events = []
            try:
                async for event in adapter.stream_prepared(
                        lambda p: "hi", "/tmp", resume_session_id="resume-1"):
                    events.append(event)
                raise AssertionError("prompt 失败应该抛 AcpError")
            except AcpError as exc:
                assert "prompt boom" in str(exc), exc
            assert [event.kind for event in events].count(
                "delivery_committed") == 1, events
            # load 已成功、错误发生在 prompt：连接与 session 仍归 adapter
            assert adapter._started is True
            assert adapter.session_id == "resume-1"
            await adapter.aclose()
        finally:
            del os.environ["FAKE_ACP_FAIL_PROMPT"]
    asyncio.run(run())
    print("ok  prompt remote error 确定失败 + no-replay")


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


def test_readonly_mode_does_not_inherit_allow_always_session() -> None:
    """普通 session 的 allow_always 不得跨到后续 read_only 轮次。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        adapter.set_permission_handler(
            lambda _name, _params: {
                "outcome": "selected", "optionId": "allow"})
        first_preps = []
        async for _ in adapter.stream_prepared(
                lambda prep: first_preps.append(prep) or "perm-always",
                "/tmp"):
            pass
        first_session = first_preps[0].session_id
        first_pid = adapter._client._proc.pid

        second_preps = []
        async for _ in adapter.stream_prepared(
                lambda prep: second_preps.append(prep) or "requires-write",
                "/tmp",
                resume_session_id=first_session,
                execution_mode=ExecutionMode.READ_ONLY):
            pass
        second_pid = adapter._client._proc.pid
        await adapter.aclose()

        assert second_preps[0].fresh is True
        assert second_preps[0].restored is False
        assert first_pid != second_pid, "read_only 必须隔离旧 ACP 进程"
        events = state_events()
        assert events.count(f"new:{TMP_WORKDIR}") == 2, events
        assert not any(item.startswith("load:") for item in events), events
        assert not any(
            item.startswith("permission-bypassed:") for item in events), events
        permissions = [
            item for item in events if item.startswith("permission:")]
        assert len(permissions) == 2, permissions
        assert '"outcome": "selected"' in permissions[0]
        assert '"outcome": "cancelled"' in permissions[1]

    asyncio.run(run())
    print("ok  read_only fresh ACP session 不继承 allow_always")


def test_execution_mode_command_profile_restarts_process() -> None:
    """进程级 CLI profile 前后切换必须 fresh，不能复用旧 session。"""
    async def run() -> None:
        reset_state()
        default_cmd = [sys.executable, SERVER, "default-profile"]
        readonly_cmd = [sys.executable, SERVER, "readonly-profile"]
        adapter = AcpAdapter(
            "fake",
            default_cmd,
            execution_cmd_overrides={
                ExecutionMode.READ_ONLY: readonly_cmd,
            },
        )

        async for _ in adapter.stream("first", "/tmp"):
            pass
        default_pid = adapter._client._proc.pid
        assert adapter._active_cmd == default_cmd

        async for _ in adapter.stream(
                "readonly", "/tmp",
                execution_mode=ExecutionMode.READ_ONLY):
            pass
        readonly_pid = adapter._client._proc.pid
        assert adapter._active_cmd == readonly_cmd

        async for _ in adapter.stream("default-again", "/tmp"):
            pass
        restored_default_pid = adapter._client._proc.pid
        assert adapter._active_cmd == default_cmd
        await adapter.aclose()

        assert len({default_pid, readonly_pid, restored_default_pid}) == 3
        events = state_events()
        assert events.count(f"new:{TMP_WORKDIR}") == 3, events
        assert not any(item.startswith("load:") for item in events), events
        assert events.count(f"process-cwd:{Path('/tmp').resolve()}") == 3, events

    asyncio.run(run())
    print("ok  execution mode CLI profile 切换重建进程/session")


if __name__ == "__main__":
    test_initialize()
    test_env_removals_override_ambient_and_survive_reset()
    test_default_acp_byte_limits_fit_20_mib_image_frame()
    test_session_new_list_load()
    test_prompt_streaming()
    test_image_prompt_uses_fake_wire_contract()
    test_adapter_prompt_without_image_capability()
    test_permission_default_deny()
    test_permission_auto_optin()
    test_permission_wait_pauses_inactivity_timeout()
    test_permission_rejects_cross_session_without_ui_activity()
    test_permission_duplicate_option_ids_fail_closed()
    test_permission_after_terminal_is_stale()
    test_terminal_cancels_unresolved_permission_and_blocks_late_allow()
    test_terminal_permission_reap_is_bounded_and_poisons()
    test_permission_response_backpressure_cannot_deadlock_terminal()
    test_cancel_cancels_permission_before_session_cancel()
    test_permission_queue_poison_cancels_stale_decision()
    test_permission_flood_is_bounded_and_overflow_is_cancelled()
    test_pending_permission_bytes_are_bounded()
    test_duplicate_active_permission_id_poisons_and_recovers()
    test_invalid_permission_request_id_poisons_without_ui()
    test_string_permission_request_id_is_supported()
    test_giant_string_permission_request_id_poisons_before_ui()
    test_inbound_frame_limits_poison_connection_no_replay()
    test_prompt_update_queue_byte_limit_poison_no_replay()
    test_single_oversized_notification_wakes_queue_consumer()
    test_cancel_notification()
    test_cancel_serialization()
    test_cancel_timeout_rebuild()
    test_cancel_send_backpressure_is_bounded_and_rebuilds()
    test_prompt_drain_failure_after_write_is_no_replay()
    test_cancel_before_first_update_is_no_replay()
    test_cancel_requires_exact_cancelled_terminal()
    test_stream_aclose_requires_exact_cancelled_terminal()
    test_inactivity_cancel_requires_exact_cancelled_terminal()
    test_inactivity_timeout_unblocks_next_round()
    test_close_during_prompt()
    test_initialize_failure()
    test_session_new_failure_atomic()
    test_bool_response_id_cannot_alias_integer_request()
    test_session_new_timeout_atomic()
    test_session_load_timeout_covers_stdio_drain()
    test_double_start_guard()
    test_aclose_serialized_with_stream()
    test_aclose_session_close_backpressure_is_outer_bounded()
    test_adapter_bridge()
    test_child_process_cwd_is_bound_to_session_workdir()
    test_relative_workdir_is_absolute_on_process_and_wire()
    test_active_session_rejects_workdir_switch()
    test_adapter_observability_without_thought_content()
    test_tool_tracking_fields_are_independently_bounded()
    test_tool_update_spam_is_coalesced_and_keeps_context()
    test_active_tool_uses_separate_inactivity_watchdog()
    test_restore_load_success()
    test_restore_missing_resource_fallback_new()
    test_restore_standard_load_rejections_fallback_new()
    test_opencode_exact_session_not_found_fallback_is_adapter_local()
    test_restore_load_fatal_remote_errors_do_not_fallback()
    test_auth_notification_queue_has_byte_budget()
    test_restore_load_transport_timeout_and_overflow_do_not_fallback()
    test_restore_unsupported_fallback_new()
    test_prompt_factory_after_decision()
    test_aclose_cannot_interleave_prepare_prompt()
    test_resume_no_steal_active_session()
    test_restore_new_error_propagates()
    test_prompt_error_propagates()
    test_factory_runs_before_first_event()
    test_fresh_factory_error_resets()
    test_nonfresh_factory_error_keeps_session()
    test_readonly_mode_does_not_inherit_allow_always_session()
    test_execution_mode_command_profile_restarts_process()
    print("\nACP 全部通过")

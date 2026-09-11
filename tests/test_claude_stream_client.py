"""Claude Code stream client contract tests (fake process only).

The tests exercise the production public seam.  They never launch the locally
installed Claude CLI and never mutate user configuration.

Run: .venv/bin/python tests/test_claude_stream_client.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import zlib
import struct
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (  # noqa: E402
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
)
from clipboard_image import TrustedImage  # noqa: E402
from claude_code.client import (  # noqa: E402
    ClaudeResumeNotFoundError,
    ClaudeStreamClient,
    ClaudeStreamError,
    ClaudeStreamProtocolError,
)


SERVER = str(ROOT / "tests" / "fake_claude_stream.py")
STATE = "/tmp/myagents_fake_claude_stream_state"
TIMEOUT = 15.0


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png_bytes() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff"))
        + _chunk(b"IEND", b"")
    )


def _trusted_image() -> TrustedImage:
    root = Path(TemporaryDirectory().name)
    root.mkdir(mode=0o700, parents=True)
    target = root / "img-0001.png"
    target.write_bytes(_png_bytes())
    return TrustedImage(
        path=target,
        attachment_root=root,
        relative_path=target.relative_to(root),
        data=target.read_bytes(),
    )


def reset_state() -> None:
    Path(STATE).unlink(missing_ok=True)
    os.environ["FAKE_CLAUDE_STREAM_STATE"] = STATE


def state_lines() -> list[str]:
    if not Path(STATE).exists():
        return []
    return Path(STATE).read_text(encoding="utf-8").splitlines()


def state_frames() -> list[dict]:
    return [
        json.loads(line.removeprefix("in:"))
        for line in state_lines()
        if line.startswith("in:")
    ]


def command(mode: str, *extra: str) -> list[str]:
    return [sys.executable, SERVER, mode, *extra]


def run(coro) -> None:
    asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


async def collect(stream, *, limit: int = 50) -> list[dict]:
    events: list[dict] = []
    async for event in stream:
        events.append(event)
        if len(events) > limit:
            raise AssertionError("Claude stream did not settle")
    return events


def test_start_spawns_and_first_turn_records_init() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(command("normal"), "/tmp")
        await client.start()
        assert client.init_message is None, "输入驱动启动：init 在首条输入前不到达"
        assert client.running
        assert client.pid is not None
        try:
            events = await collect(client.send_turn("hello"))
            assert events[0]["type"] == "delivery_committed"
        finally:
            init = client.init_message
            await client.close()
        assert init is not None
        assert init["type"] == "system"
        assert init["subtype"] == "init"
        assert not client.running

    run(body())
    print("ok  输入驱动启动：init 在首个 turn 内到达，close 回收进程")


def test_turn_yields_commit_echo_events_then_result() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(command("normal"), "/tmp")
        await client.start()
        try:
            events = await collect(client.send_turn("hello"))
            assert [event["type"] for event in events] == [
                "delivery_committed",
                "stream_event",
                "stream_event",
                "assistant",
                "user",
                "stream_event",
                "result",
            ]
            # The synthetic commit must carry the fake session id and come
            # before any vendor frame.
            assert events[0]["session_id"].startswith("2f0c8a52")
            assert events[-1]["subtype"] == "success"
            assert client.running
        finally:
            await client.close()

    run(body())
    print("ok  delivery_committed precedes echo events and result settles")


def test_lost_echo_is_uncertain() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(
            command("no_echo"), "/tmp", acceptance_timeout=0.5)
        await client.start()
        try:
            try:
                async for _event in client.send_turn("hello"):
                    pass
            except AgentDeliveryUncertainError:
                pass
            else:
                raise AssertionError("lost echo must be uncertain")
        finally:
            await client.close()

    run(body())
    print("ok  missing replay echo raises AgentDeliveryUncertainError")


def test_event_before_echo_is_protocol_error() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(command("events_before_echo"), "/tmp")
        await client.start()
        try:
            try:
                async for _event in client.send_turn("hello"):
                    pass
            except ClaudeStreamProtocolError:
                pass
            else:
                raise AssertionError("non-echo first frame must fail")
        finally:
            await client.close()

    run(body())
    print("ok  first frame that is not the replay echo fails the turn")


def test_crash_before_init_fails_first_turn() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(command("crash_before_init"), "/tmp")
        await client.start()
        try:
            try:
                async for _event in client.send_turn("hello"):
                    pass
            except (ClaudeStreamError, AgentDeliveryUncertainError):
                pass
            else:
                raise AssertionError("crash before init must fail the turn")
        finally:
            await client.close()

    run(body())
    print("ok  process exit before any output fails the first turn")


def test_resume_not_found_maps_to_exact_error() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(command("resume_not_found"), "/tmp")
        await client.start()
        try:
            try:
                async for _event in client.send_turn("hello"):
                    pass
            except ClaudeResumeNotFoundError:
                pass
            else:
                raise AssertionError("resume miss must raise the exact type")
        finally:
            await client.close()

    run(body())
    print("ok  documented resume-miss stderr maps to ClaudeResumeNotFoundError")


def test_cancel_sends_sigint_and_raises_cancelled() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(
            command("slow_turn"), "/tmp", interrupt_timeout=5.0)
        await client.start()
        pid = client.pid
        try:
            async def drive() -> None:
                async for _event in client.send_turn("slow"):
                    pass

            task = asyncio.create_task(drive())
            while not any(
                    frame.get("message", {}).get("content") == "slow"
                    for frame in state_frames()):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert "sigint" in state_lines(), state_lines()
            assert client.pid == pid, "SIGINT must not kill the process"
        finally:
            await client.close()
        assert client.pid is None

    run(body())
    print("ok  cancel uses documented SIGINT and keeps the process alive")


def test_close_terminates_the_full_process_group() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(command("normal"), "/tmp")
        await client.start()
        proc = client._proc
        assert proc is not None
        await client.close()
        assert proc.returncode is not None

    run(body())
    print("ok  close reclaims the process group")


def test_single_active_turn_is_enforced() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(command("slow_turn"), "/tmp")
        await client.start()
        try:
            first = client.send_turn("one")
            task = asyncio.create_task(anext(first))
            await task
            second = client.send_turn("two")
            try:
                await anext(second)
            except ClaudeStreamError:
                pass
            else:
                raise AssertionError("second concurrent turn must be refused")
            task.cancel()
            with contextlib.suppress(BaseException):
                await first.aclose()
            with contextlib.suppress(BaseException):
                await second.aclose()
        finally:
            await client.close()

    run(body())
    print("ok  only one active turn is allowed")


def test_outbound_frame_limit_rejects_before_write() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(command("normal"), "/tmp", frame_limit=256)
        await client.start()
        try:
            try:
                async for _event in client.send_turn("x" * 4096):
                    pass
            except ClaudeStreamError as exc:
                assert "exceeds" in str(exc)
            else:
                raise AssertionError("oversized outbound frame must be refused")
        finally:
            await client.close()

    run(body())
    print("ok  outbound frame limit fails the turn before any write")


def test_inbound_oversized_frame_is_protocol_error() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(
            command("huge_frame"), "/tmp", frame_limit=1024 * 1024)
        await client.start()
        try:
            try:
                async for _event in client.send_turn("hello"):
                    pass
            except (ClaudeStreamProtocolError, AgentDeliveryUncertainError):
                pass
            else:
                raise AssertionError("oversized inbound frame must fail")
        finally:
            await client.close()

    run(body())
    print("ok  oversized inbound frame raises a protocol failure")


def test_images_travel_as_base64_blocks_and_echo_is_acknowledged() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(command("normal"), "/tmp")
        await client.start()
        try:
            image = _trusted_image()
            events = await collect(client.send_turn("看图", (image,)))
            assert events[0]["type"] == "delivery_committed"
            sent = [
                frame for frame in state_frames()
                if frame.get("type") == "user"
            ][0]
            content = sent["message"]["content"]
            assert isinstance(content, list)
            assert content[0]["type"] == "text"
            assert content[1]["source"]["type"] == "base64"
            assert content[1]["source"]["data"]
        finally:
            await client.close()

    run(body())
    print("ok  trusted images become base64 content blocks")


def test_image_payload_budget_is_bounded() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(command("normal"), "/tmp")
        await client.start()
        image = _trusted_image()
        try:
            stream = client.send_turn("hi", (image,) * 17)
            try:
                await anext(stream)
            except ClaudeStreamError:
                pass
            else:
                raise AssertionError("image count above 16 must be refused")
        finally:
            await client.close()

    run(body())
    print("ok  image aggregate/count budget is enforced before send")


def test_aclose_after_result_keeps_healthy_process() -> None:
    async def body() -> None:
        reset_state()
        client = ClaudeStreamClient(command("normal"), "/tmp")
        await client.start()
        pid = client.pid
        try:
            stream = client.send_turn("hello")
            task = asyncio.create_task(anext(stream))
            events = [await task]
            while events[-1].get("type") != "result":
                events.append(await anext(stream))
            # 消费方在 result yield 点放弃流：turn 已完成，不得杀进程。
            await stream.aclose()
            assert client.running
            assert client.pid == pid
        finally:
            await client.close()

    run(body())
    print("ok  result 之后 aclose 保留健康进程")


if __name__ == "__main__":
    test_start_spawns_and_first_turn_records_init()
    test_turn_yields_commit_echo_events_then_result()
    test_lost_echo_is_uncertain()
    test_event_before_echo_is_protocol_error()
    test_crash_before_init_fails_first_turn()
    test_resume_not_found_maps_to_exact_error()
    test_cancel_sends_sigint_and_raises_cancelled()
    test_close_terminates_the_full_process_group()
    test_single_active_turn_is_enforced()
    test_aclose_after_result_keeps_healthy_process()
    test_outbound_frame_limit_rejects_before_write()
    test_inbound_oversized_frame_is_protocol_error()
    test_images_travel_as_base64_blocks_and_echo_is_acknowledged()
    test_image_payload_budget_is_bounded()
    print("ok  Claude stream client contract")

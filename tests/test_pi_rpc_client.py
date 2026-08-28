"""Pi native RPC client contract tests (fake process only).

The tests exercise the production public seam.  They never launch the locally
installed Pi CLI and never mutate user configuration.
"""

from __future__ import annotations

import asyncio
import base64
import gc
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.base import (  # noqa: E402
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
)
from clipboard_image import TrustedImage  # noqa: E402
from pi_rpc.client import (  # noqa: E402
    PiRpcClient,
    PiRpcError,
    PiRpcProtocolError,
    PiRpcRemoteError,
)


SERVER = str(Path(__file__).parent / "fake_pi_rpc_server.py")
STATE = "/tmp/myagents_fake_pi_rpc_state"
TIMEOUT = 12.0


def reset_state() -> None:
    Path(STATE).unlink(missing_ok=True)
    os.environ["FAKE_PI_RPC_STATE"] = STATE


def state_frames() -> list[dict]:
    if not Path(STATE).exists():
        return []
    return [
        json.loads(line.removeprefix("in:"))
        for line in Path(STATE).read_text(encoding="utf-8").splitlines()
        if line.startswith("in:")
    ]


async def wait_for_frame(command: str, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for frame in state_frames():
            if frame.get("type") == command:
                return frame
        await asyncio.sleep(0.01)
    raise AssertionError(f"did not receive {command!r}: {state_frames()}")


def run(coro) -> None:
    asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


def command(*extra: str) -> list[str]:
    return [sys.executable, SERVER, *extra]


async def collect(stream, *, limit: int = 50) -> list[dict]:
    events: list[dict] = []
    async for event in stream:
        events.append(event)
        if len(events) > limit:
            raise AssertionError("Pi RPC event stream did not settle")
    return events


def test_state_commands_and_closed_public_surface() -> None:
    async def body() -> None:
        reset_state()
        client = PiRpcClient(command(), "/tmp")
        await client.start()
        pid = client.pid
        assert isinstance(pid, int) and pid > 0
        state = await client.get_state()
        commands = await client.get_commands()
        assert state["sessionId"] == "fake-pi-session"
        assert commands[0]["name"] == "myagents-policy"
        assert not hasattr(client, "bash")
        assert not hasattr(client, "request")
        await client.close()
        await client.close()
        assert client.pid is None
        with_error = False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            with_error = True
        assert with_error, f"Pi process {pid} survived close"

        frames = state_frames()
        assert [frame["type"] for frame in frames] == [
            "get_state", "get_commands"]
        assert all(isinstance(frame.get("id"), str) for frame in frames)

    run(body())
    print("ok  Pi RPC state/commands + closed command surface")


def test_prompt_commit_precedes_buffered_events_and_waits_for_settled() -> None:
    async def body() -> None:
        reset_state()
        client = PiRpcClient(command(), "/tmp")
        await client.start()
        try:
            events = await collect(client.prompt("before-response"))
        finally:
            await client.close()

        assert [event["type"] for event in events] == [
            "delivery_committed",
            "agent_start",
            "message_update",
            "agent_end",
            "turn_tail",
            "agent_settled",
        ]
        assert events[2]["assistantMessageEvent"]["delta"] == "EARLY"
        assert events[4]["value"] == "AFTER_AGENT_END"

    run(body())
    print("ok  prompt buffers pre-response events and ends on agent_settled")


def test_prompt_rejection_is_explicit_and_uncommitted() -> None:
    async def body() -> None:
        reset_state()
        client = PiRpcClient(command(), "/tmp")
        await client.start()
        try:
            try:
                await collect(client.prompt("reject"))
            except PiRpcRemoteError as exc:
                assert "preflight rejected" in str(exc)
            else:
                raise AssertionError("explicit rejection unexpectedly succeeded")
        finally:
            await client.close()

    run(body())
    print("ok  preflight rejection remains uncommitted")


def test_prompt_lost_response_is_delivery_uncertain() -> None:
    async def body() -> None:
        reset_state()
        client = PiRpcClient(command(), "/tmp", request_timeout=1.0)
        await client.start()
        try:
            try:
                await collect(client.prompt("die-before-response"))
            except AgentDeliveryUncertainError as exc:
                assert "prompt" in str(exc)
            else:
                raise AssertionError("lost prompt response was replay-safe")
        finally:
            await client.close()
        assert (await wait_for_frame("prompt"))["message"] == (
            "die-before-response")

    run(body())
    print("ok  drained prompt without response is uncertain/no-replay")


def test_cancel_accepted_prompt_aborts_and_waits_for_settled() -> None:
    async def body() -> None:
        reset_state()
        client = PiRpcClient(
            command(), "/tmp", request_timeout=2.0, abort_timeout=2.0)
        await client.start()
        stream = client.prompt("slow")
        assert (await anext(stream))["type"] == "delivery_committed"
        assert (await anext(stream))["type"] == "agent_start"
        assert (await anext(stream))["type"] == "message_update"

        waiter = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.03)
        waiter.cancel()
        try:
            await waiter
        except AgentDeliveryCancelledError:
            pass
        else:
            raise AssertionError("accepted prompt cancellation was not typed")
        await wait_for_frame("abort")
        await client.close()

    run(body())
    print("ok  accepted prompt cancellation aborts and settles")


def test_steer_is_accepted_during_active_prompt() -> None:
    async def body() -> None:
        reset_state()
        client = PiRpcClient(command(), "/tmp")
        await client.start()
        try:
            stream = client.prompt("slow")
            assert (await anext(stream))["type"] == "delivery_committed"
            assert (await anext(stream))["type"] == "agent_start"
            assert (await anext(stream))["type"] == "message_update"

            await client.steer("先给结论")
            remaining = await collect(stream)
            assert any(
                event.get("type") == "queue_update"
                and event.get("steering") == ["先给结论"]
                for event in remaining
            )
            assert remaining[-1]["type"] == "agent_settled"
        finally:
            await client.close()

        frame = await wait_for_frame("steer")
        assert frame["message"] == "先给结论"
        assert isinstance(frame.get("id"), str)

    run(body())
    print("ok  active Pi prompt accepts native steer without a second prompt")


def test_unconfirmed_abort_closes_without_orphan_task_warning() -> None:
    async def body() -> None:
        reset_state()
        loop = asyncio.get_running_loop()
        reported: list[dict] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        try:
            client = PiRpcClient(
                command(),
                "/tmp",
                request_timeout=1.0,
                abort_timeout=0.05,
                shutdown_timeout=0.05,
            )
            await client.start()
            stream = client.prompt("slow-no-abort")
            for expected in (
                "delivery_committed", "agent_start", "message_update"):
                assert (await anext(stream))["type"] == expected
            waiter = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.01)
            waiter.cancel()
            try:
                await waiter
            except AgentDeliveryCancelledError:
                pass
            else:
                raise AssertionError("cancel did not retain typed semantics")
            assert client.pid is None
            await asyncio.sleep(0)
            gc.collect()
            await asyncio.sleep(0)
            leaked = [
                item for item in reported
                if "never retrieved" in str(item.get("message", ""))
            ]
            assert not leaked, leaked
        finally:
            loop.set_exception_handler(previous)

    run(body())
    print("ok  unconfirmed abort closes without an orphan request task")


def test_extension_ui_handler_does_not_block_reader() -> None:
    async def body() -> None:
        reset_state()
        release = asyncio.Event()
        seen: list[dict] = []

        async def handle(request: dict) -> dict:
            seen.append(request)
            await release.wait()
            return {
                "type": "extension_ui_response",
                "id": request["id"],
                "confirmed": True,
            }

        client = PiRpcClient(
            command(), "/tmp", extension_ui_handler=handle)
        await client.start()
        stream = client.prompt("extension")
        assert (await anext(stream))["type"] == "delivery_committed"
        assert (await anext(stream))["type"] == "agent_start"
        update = await anext(stream)
        assert update["assistantMessageEvent"]["delta"] == "WHILE_WAITING"
        assert seen and seen[0]["method"] == "confirm"
        assert not any(
            frame.get("type") == "extension_ui_response"
            for frame in state_frames())

        release.set()
        tail = await collect(stream)
        assert tail[-1]["type"] == "agent_settled"
        reply = await wait_for_frame("extension_ui_response")
        assert reply == {
            "type": "extension_ui_response",
            "id": "ext-1",
            "confirmed": True,
        }
        await client.close()

    run(body())
    print("ok  extension UI callback is async and reader remains live")


def test_extension_ui_invalid_result_fails_closed() -> None:
    async def body() -> None:
        reset_state()

        async def handle(_request: dict) -> dict:
            return {"type": "extension_ui_response", "confirmed": True}

        client = PiRpcClient(
            command(), "/tmp", extension_ui_handler=handle)
        await client.start()
        try:
            events = await collect(client.prompt("extension"))
            assert events[-1]["type"] == "agent_settled"
            reply = await wait_for_frame("extension_ui_response")
            assert reply == {
                "type": "extension_ui_response",
                "id": "ext-1",
                "cancelled": True,
            }
        finally:
            await client.close()

    run(body())
    print("ok  malformed extension decision is cancelled")


def test_extension_ui_timeout_is_independent_from_request_timeout() -> None:
    client = PiRpcClient(command(), "/tmp", request_timeout=30.0)
    assert client._extension_timeout({"timeout": 60_000}) == 60.0
    assert client._extension_timeout({}) == 300.0

    bounded = PiRpcClient(
        command(), "/tmp", request_timeout=1.0, extension_ui_timeout=75.0)
    assert bounded._extension_timeout({"timeout": 120_000}) == 75.0
    assert bounded._extension_timeout({"timeout": 20_000}) == 20.0
    try:
        PiRpcClient(command(), "/tmp", extension_ui_timeout=0)
    except ValueError:
        pass
    else:
        raise AssertionError("non-positive extension UI timeout was accepted")

    print("ok  human approval timeout is independent from RPC timeout")


def test_extension_ui_concurrency_overflow_closes_connection() -> None:
    async def body() -> None:
        reset_state()
        never = asyncio.Event()

        async def handle(_request: dict) -> dict:
            await never.wait()
            return {"outcome": "unreachable"}

        client = PiRpcClient(
            command(),
            "/tmp",
            extension_ui_handler=handle,
        )
        await client.start()
        try:
            try:
                await collect(client.prompt("extension-overflow"))
            except AgentDeliveryUncertainError as exc:
                assert isinstance(exc.__cause__, PiRpcProtocolError)
                assert "concurrent extension UI" in str(exc.__cause__)
            else:
                raise AssertionError(
                    "unbounded extension UI requests remained active")
            assert not client.running
        finally:
            await client.close()

    run(body())
    print("ok  extension UI concurrency overflow closes fail-closed")


def test_duplicate_active_extension_ui_id_closes_connection() -> None:
    async def body() -> None:
        reset_state()
        never = asyncio.Event()

        async def handle(_request: dict) -> dict:
            await never.wait()
            return {"outcome": "unreachable"}

        client = PiRpcClient(
            command(),
            "/tmp",
            extension_ui_handler=handle,
            request_timeout=1.0,
            abort_timeout=0.05,
            shutdown_timeout=0.05,
        )
        await client.start()
        try:
            try:
                await asyncio.wait_for(
                    collect(client.prompt("extension-duplicate")),
                    timeout=0.5,
                )
            except AgentDeliveryUncertainError as exc:
                assert isinstance(exc.__cause__, PiRpcProtocolError)
                assert "duplicate extension UI request id" in str(exc.__cause__)
            else:
                raise AssertionError("duplicate active extension UI id was accepted")
            assert not client.running
        finally:
            await client.close()

    run(body())
    print("ok  duplicate active extension UI id closes fail-closed")


def test_extension_ui_id_is_released_after_task_finishes() -> None:
    async def body() -> None:
        reset_state()
        seen: list[str] = []

        async def handle(request: dict) -> dict:
            seen.append(request["id"])
            return {
                "type": "extension_ui_response",
                "id": request["id"],
                "confirmed": True,
            }

        client = PiRpcClient(
            command(),
            "/tmp",
            extension_ui_handler=handle,
        )
        await client.start()
        try:
            events = await collect(client.prompt("extension-id-reuse"))
            assert events[-1]["type"] == "agent_settled"
            assert seen == ["ext-reuse", "ext-reuse"]
            assert client.running
        finally:
            await client.close()

    run(body())
    print("ok  completed extension UI task releases its request id")


def test_frame_limit_and_unterminated_frame_fail_strictly() -> None:
    async def oversized() -> None:
        reset_state()
        client = PiRpcClient(command(), "/tmp", frame_limit=512)
        await client.start()
        try:
            try:
                await collect(client.prompt("oversized"))
            except AgentDeliveryUncertainError as exc:
                assert isinstance(exc.__cause__, PiRpcProtocolError)
            else:
                raise AssertionError("oversized frame was accepted")
        finally:
            await client.close()

    async def unterminated() -> None:
        reset_state()
        client = PiRpcClient(command(), "/tmp")
        await client.start()
        try:
            try:
                await collect(client.prompt("unterminated"))
            except AgentDeliveryUncertainError as exc:
                assert isinstance(exc.__cause__, PiRpcProtocolError)
            else:
                raise AssertionError("unterminated JSON frame was accepted")
        finally:
            await client.close()

    run(oversized())
    run(unterminated())
    print("ok  strict LF framing + maximum frame size")


def test_prompt_event_byte_budget_is_cumulative_and_closes_connection() -> None:
    async def body() -> None:
        reset_state()
        client = PiRpcClient(
            command(),
            "/tmp",
            event_queue_byte_limit=700,
        )
        await client.start()
        try:
            try:
                await collect(client.prompt("event-byte-overflow"))
            except AgentDeliveryUncertainError as exc:
                assert isinstance(exc.__cause__, PiRpcProtocolError)
                assert "byte budget" in str(exc.__cause__)
            else:
                raise AssertionError("cumulative prompt events exceeded budget")
            assert not client.running
        finally:
            await client.close()

    run(body())
    print("ok  cumulative prompt event bytes are bounded fail-closed")


def test_prompt_event_byte_budget_is_released_after_dequeue() -> None:
    async def body() -> None:
        reset_state()
        client = PiRpcClient(
            command(),
            "/tmp",
            event_queue_byte_limit=700,
        )
        await client.start()
        try:
            events = await collect(client.prompt("event-byte-release"))
            assert [event["type"] for event in events] == [
                "delivery_committed",
                "message_update",
                "message_update",
                "message_update",
                "agent_settled",
            ]
            assert client.running
        finally:
            await client.close()

    run(body())
    print("ok  dequeued prompt events release their byte budget")


def test_default_frame_limit_accepts_real_pi_large_user_image_echo() -> None:
    async def body() -> None:
        reset_state()
        client = PiRpcClient(command(), "/tmp")
        await client.start()
        try:
            events = await collect(client.prompt("large-user-echo"))
            assert events[-1]["type"] == "agent_settled"
            assert len(events[1]["message"]["content"][0]["data"]) > 1024 * 1024
        finally:
            await client.close()

    run(body())
    print("ok  default frame limit accepts Pi image echo over 1 MiB")


def test_prompt_image_aggregate_is_bounded_before_send() -> None:
    async def body() -> None:
        reset_state()
        shared = b"x" * (11 * 1024 * 1024)
        image = TrustedImage(
            path=Path("/tmp/image.png"),
            attachment_root=Path("/tmp"),
            relative_path=Path("image.png"),
            data=shared,
        )
        client = PiRpcClient(command(), "/tmp")
        await client.start()
        try:
            try:
                await collect(client.prompt("images", images=(image, image)))
            except PiRpcError as exc:
                assert "image" in str(exc).lower()
            else:
                raise AssertionError("unbounded aggregate images were sent")
            tiny = TrustedImage(
                path=Path("/tmp/tiny.png"),
                attachment_root=Path("/tmp"),
                relative_path=Path("tiny.png"),
                data=b"x",
            )
            try:
                await collect(client.prompt("many-images", images=(tiny,) * 17))
            except PiRpcError as exc:
                assert "16" in str(exc)
            else:
                raise AssertionError("unbounded image count was sent")
            assert not any(
                frame.get("type") == "prompt" for frame in state_frames())
        finally:
            await client.close()

    run(body())
    print("ok  aggregate Pi prompt images are bounded before send")


def test_response_command_mismatch_poison_connection() -> None:
    async def body() -> None:
        reset_state()
        client = PiRpcClient(command(), "/tmp")
        await client.start()
        try:
            try:
                await collect(client.prompt("mismatched-response"))
            except AgentDeliveryUncertainError as exc:
                assert isinstance(exc.__cause__, PiRpcProtocolError)
            else:
                raise AssertionError("mismatched response was accepted")
            deadline = asyncio.get_running_loop().time() + 1
            while client.running:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        "protocol-corrupted connection remained reusable")
                await asyncio.sleep(0.01)
        finally:
            await client.close()

    run(body())
    print("ok  mismatched response poisons the RPC connection")


def test_images_are_encoded_without_exposing_a_path() -> None:
    async def body() -> None:
        reset_state()
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "attachments"
            root.mkdir()
            os.chmod(root, 0o700)
            path = root / "image.png"
            payload = b"fake-png-bytes"
            path.write_bytes(payload)
            os.chmod(path, 0o600)
            image = TrustedImage(
                path=path.resolve(),
                attachment_root=root.resolve(),
                relative_path=Path("image.png"),
                data=payload,
            )
            client = PiRpcClient(command(), tmp)
            await client.start()
            try:
                await collect(client.prompt("image", images=(image,)))
            finally:
                await client.close()
        prompt = next(
            frame for frame in state_frames()
            if frame.get("type") == "prompt")
        assert prompt["images"] == [{
            "type": "image",
            "mimeType": "image/png",
            "data": base64.b64encode(payload).decode("ascii"),
        }]
        assert str(path) not in json.dumps(prompt)

    run(body())
    print("ok  Pi image payload uses bytes, never upgrades a path")


def test_prompt_images_require_a_trusted_image_tuple() -> None:
    async def body() -> None:
        reset_state()
        client = PiRpcClient(command(), "/tmp")
        await client.start()
        try:
            for invalid in ([], (object(),)):
                try:
                    await collect(client.prompt("image", images=invalid))
                except TypeError:
                    pass
                else:
                    raise AssertionError(
                        f"untrusted image collection was accepted: {invalid!r}")
        finally:
            await client.close()
        assert not any(
            frame.get("type") == "prompt" for frame in state_frames())

    run(body())
    print("ok  prompt image seam accepts TrustedImage tuples only")


def test_close_uses_pi_graceful_shutdown_to_reap_detached_children() -> None:
    async def body() -> None:
        reset_state()
        with TemporaryDirectory() as tmp:
            child_file = Path(tmp) / "child.pid"
            client = PiRpcClient(
                command("--spawn-child"),
                "/tmp",
                env_overrides={"FAKE_PI_CHILD": str(child_file)},
                shutdown_timeout=0.15,
            )
            await client.start()
            deadline = asyncio.get_running_loop().time() + 2
            while not child_file.exists():
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("fake child did not start")
                await asyncio.sleep(0.01)
            parent_pid = client.pid
            child_pid = int(child_file.read_text())
            await client.close()
            assert client.pid is None

            deadline = asyncio.get_running_loop().time() + 2
            while True:
                alive = []
                for pid in (parent_pid, child_pid):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        continue
                    alive.append(pid)
                if not alive:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        f"Pi graceful shutdown left a tracked process: {alive}")
                await asyncio.sleep(0.02)

    run(body())
    print("ok  Pi graceful shutdown reaps its tracked detached child")


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"\n{len(tests)} Pi RPC client contract tests passed")

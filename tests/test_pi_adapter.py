"""Pi RPC adapter contract tests.

All tests use an in-memory client fake.  They never start or modify the real
Pi installation.

Run: .venv/bin/python tests/test_pi_adapter.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import ExecutionMode
from adapters.base import AgentDeliveryUncertainError
from pi_rpc.client import PiRpcRemoteError


def _encoded(payload: dict) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class FakePiClient:
    instances: list["FakePiClient"] = []
    bad_attestation = False
    bad_ready = False

    def __init__(
        self,
        cmd,
        *,
        cwd,
        env_overrides,
        extension_ui_handler,
        **_kwargs,
    ) -> None:
        self.cmd = list(cmd)
        self.cwd = cwd
        self.env = dict(env_overrides)
        self.extension_ui_handler = extension_ui_handler
        self.closed = False
        self.prompts: list[tuple[str, tuple]] = []
        self.permission_request: dict | None = None
        self.permission_response: dict | None = None
        self.attestation_request: dict | None = None
        self.pid = 4100 + len(self.instances)
        self.instances.append(self)

        session_dir = Path(self.cmd[self.cmd.index("--session-dir") + 1])
        session_dir.mkdir(parents=True, exist_ok=True)
        if "--session" in self.cmd:
            self.session_file = Path(
                self.cmd[self.cmd.index("--session") + 1]
            )
        else:
            self.session_file = session_dir / (
                f"2026-08-25T00-00-00-000Z_sid-{self.pid}.jsonl"
            )
            self._write_session_header()

    def _write_session_header(self) -> None:
        self.session_file.write_text(
            json.dumps({
                "type": "session",
                "version": 3,
                "id": f"sid-{self.pid}",
                "cwd": str(Path(self.cwd).resolve()),
            }) + "\n",
            encoding="utf-8",
        )

    async def start(self) -> None:
        await self._attest()

    async def _attest(self) -> None:
        from pi_rpc.adapter import (
            PI_ATTEST_PREFIX,
            PI_POLICY_VERSION,
            PI_READY_STATUS_KEY,
        )

        bridge = str(Path(self.env["MYAGENTS_PI_POLICY_PATH"]).resolve())
        nonce = self.env["MYAGENTS_PI_POLICY_NONCE"]
        policy_hash = self.env["MYAGENTS_PI_POLICY_HASH"]
        tools = self.cmd[self.cmd.index("--tools") + 1].split(",")
        payload = {
            "version": PI_POLICY_VERSION,
            "nonce": nonce,
            "policyHash": (
                "0" * 64 if self.bad_attestation else policy_hash
            ),
            "profile": self.env["MYAGENTS_PI_PROFILE"],
            "workspace": str(Path(self.cwd).resolve()),
            "activeTools": tools,
            "tools": [
                {
                    "name": name,
                    "sourceInfo": {
                        "path": bridge,
                        "source": "extension",
                    },
                }
                for name in tools
            ],
        }
        request = {
            "type": "extension_ui_request",
            "id": "attest-1",
            "method": "select",
            "title": PI_ATTEST_PREFIX + _encoded(payload),
            "options": [f"ack:{nonce}", f"deny:{nonce}"],
        }
        self.attestation_request = request
        response = await self.extension_ui_handler(request)
        if not self.bad_attestation:
            assert response == {
                "type": "extension_ui_response",
                "id": "attest-1",
                "value": f"ack:{nonce}",
            }
            ready_request = {
                "type": "extension_ui_request",
                "id": "ready-1",
                "method": "setStatus",
                "statusKey": PI_READY_STATUS_KEY,
                "statusText": (
                    "ready:forged:policy"
                    if self.bad_ready
                    else f"ready:{nonce}:{policy_hash}"
                ),
            }
            if self.bad_ready:
                async def send_late_bad_ready() -> None:
                    # Exercise the window after the adapter has accepted the
                    # attestation but while it is waiting for bridge readiness.
                    await asyncio.sleep(0.01)
                    await self.extension_ui_handler(ready_request)

                asyncio.create_task(send_late_bad_ready())
            else:
                await self.extension_ui_handler(ready_request)

    async def get_commands(self) -> list[dict]:
        return [{
            "name": "myagents-policy-v1",
            "source": "extension",
            "sourceInfo": {
                "path": str(Path(
                    self.env["MYAGENTS_PI_POLICY_PATH"]
                ).resolve()),
                "source": "extension",
            },
        }]

    async def get_state(self) -> dict:
        return {
            "sessionFile": str(self.session_file.resolve()),
            "sessionId": f"sid-{self.pid}",
        }

    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        if self.permission_request is not None:
            self.permission_response = await self.extension_ui_handler(
                self.permission_request)
        yield {"type": "delivery_committed"}
        yield {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "thinking_delta",
                "delta": "private reasoning",
            },
        }
        yield {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": "Pi reply",
            },
        }
        yield {
            "type": "tool_execution_start",
            "toolCallId": "tool-1",
            "toolName": "myagents_read",
            "args": {"path": "README.md"},
        }
        yield {
            "type": "tool_execution_end",
            "toolCallId": "tool-1",
            "toolName": "myagents_read",
            "isError": False,
            "result": {"content": []},
        }
        yield {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
            },
        }
        yield {"type": "agent_end", "willRetry": False}
        yield {"type": "agent_settled"}

    async def abort(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class UncertainPiClient(FakePiClient):
    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        if False:
            yield {}
        raise AgentDeliveryUncertainError(
            "prompt drained but acceptance response was lost")


class SteeringPiClient(FakePiClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.release = asyncio.Event()
        self.steers: list[str] = []

    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        yield {"type": "delivery_committed"}
        yield {"type": "agent_start"}
        await self.release.wait()
        yield {
            "type": "message_end",
            "message": {"role": "assistant", "stopReason": "stop"},
        }
        yield {"type": "agent_end", "willRetry": False}
        yield {"type": "agent_settled"}

    async def steer(self, message: str) -> None:
        self.steers.append(message)
        self.release.set()


class BurstPiClient(FakePiClient):
    """Expose whether the adapter drains a producer ahead of its consumer."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.produced = 0

    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        yield {"type": "delivery_committed"}
        for index in range(10_000):
            self.produced += 1
            yield {"type": "queue_update", "index": index}
        yield {
            "type": "message_end",
            "message": {"role": "assistant", "stopReason": "stop"},
        }
        yield {"type": "agent_settled"}


class LazySessionPiClient(FakePiClient):
    """Pi 0.84 returns a reserved path before persisting the first reply."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.session_file.unlink()

    async def prompt(self, message: str, images=()):
        self._write_session_header()
        async for event in super().prompt(message, images):
            yield event


class NeverMaterializedSessionPiClient(LazySessionPiClient):
    async def prompt(self, message: str, images=()):
        async for event in FakePiClient.prompt(self, message, images):
            yield event


class PreflightRejectedLazySessionPiClient(LazySessionPiClient):
    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        if False:
            yield {}
        raise PiRpcRemoteError("prompt", "preflight rejected")


class AcceptedWaitingLazySessionPiClient(LazySessionPiClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.waiting_after_commit = asyncio.Event()

    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        yield {"type": "delivery_committed"}
        self.waiting_after_commit.set()
        await asyncio.Future()


class WaitingBeforeCommitPiClient(FakePiClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.prompt_started = asyncio.Event()

    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        self.prompt_started.set()
        await asyncio.Future()
        if False:
            yield {}


class QuietToolPiClient(FakePiClient):
    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        yield {"type": "delivery_committed"}
        yield {
            "type": "tool_execution_start",
            "toolCallId": "quiet-tool-1",
            "toolName": "myagents_bash",
            "args": {"command": "slow-build"},
        }
        await asyncio.sleep(0.06)
        yield {
            "type": "tool_execution_end",
            "toolCallId": "quiet-tool-1",
            "toolName": "myagents_bash",
            "isError": False,
        }
        yield {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
            },
        }
        yield {"type": "agent_settled"}


class MissingAssistantTerminalPiClient(FakePiClient):
    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        yield {"type": "delivery_committed"}
        yield {"type": "agent_settled"}


class TerminalErrorPiClient(FakePiClient):
    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        yield {"type": "delivery_committed"}
        yield {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": "partial reply",
            },
        }
        yield {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "provider unavailable",
            },
        }
        yield {"type": "agent_end", "willRetry": False}
        yield {"type": "agent_settled"}


class RetriedTerminalPiClient(FakePiClient):
    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        yield {"type": "delivery_committed"}
        yield {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "temporary provider failure",
            },
        }
        yield {
            "type": "auto_retry_start",
            "attempt": 1,
            "maxAttempts": 3,
            "delayMs": 0,
            "errorMessage": "temporary provider failure",
        }
        yield {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": "recovered reply",
            },
        }
        yield {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
            },
        }
        yield {"type": "agent_end", "willRetry": False}
        yield {"type": "agent_settled"}


class TerminatedToolPiClient(FakePiClient):
    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        yield {"type": "delivery_committed"}
        yield {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
            },
        }
        yield {
            "type": "tool_execution_start",
            "toolCallId": "blocked-write-1",
            "toolName": "myagents_write",
            "args": {"path": "blocked.txt", "content": "blocked"},
        }
        yield {
            "type": "tool_execution_end",
            "toolCallId": "blocked-write-1",
            "toolName": "myagents_write",
            "isError": True,
            "result": {
                "content": [{"type": "text", "text": "Permission denied"}],
                "terminate": True,
            },
        }
        yield {"type": "agent_end", "willRetry": False}
        yield {"type": "agent_settled"}


class RecoveringToolErrorPiClient(FakePiClient):
    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        yield {"type": "delivery_committed"}
        yield {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
            },
        }
        yield {
            "type": "tool_execution_end",
            "toolCallId": "recoverable-read-1",
            "toolName": "myagents_read",
            "isError": True,
            "result": {
                "content": [{"type": "text", "text": "temporary failure"}],
            },
        }
        yield {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": "recovered after tool error",
            },
        }
        yield {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
            },
        }
        yield {"type": "agent_end", "willRetry": False}
        yield {"type": "agent_settled"}


class MalformedStopReasonPiClient(FakePiClient):
    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        yield {"type": "delivery_committed"}
        yield {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "future-success-shape",
            },
        }
        yield {"type": "agent_end", "willRetry": False}
        yield {"type": "agent_settled"}


class DeferredStopReasonPiClient(FakePiClient):
    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        yield {"type": "delivery_committed"}
        yield {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "deferred",
            },
        }
        yield {"type": "agent_end", "willRetry": False}
        yield {"type": "agent_settled"}


class BlockingClosePiClient(FakePiClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.block_close = False
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def close(self) -> None:
        if self.block_close:
            self.close_started.set()
            await self.close_release.wait()
        self.closed = True


def _permission_request_for_nonce(
    nonce: str,
    *,
    tool: str = "myagents_write",
) -> dict:
    from pi_rpc.adapter import PI_PERMISSION_PREFIX, PI_POLICY_VERSION

    call_nonce = "call-nonce-1"
    raw_input = (
        {"path": "notes.txt", "content": "hello"}
        if tool != "myagents_bash"
        else {"command": "pwd"}
    )
    return {
        "type": "extension_ui_request",
        "id": "permission-1",
        "method": "select",
        "title": PI_PERMISSION_PREFIX + _encoded({
            "version": PI_POLICY_VERSION,
            "processNonce": nonce,
            "callNonce": call_nonce,
            "toolCallId": "tool-write-1",
            "toolName": tool,
            "argsHash": hashlib.sha256(
                json.dumps(
                    raw_input, separators=(",", ":"), sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "input": raw_input,
        }),
        "options": [
            f"allow_once:{call_nonce}",
            f"reject_once:{call_nonce}",
        ],
    }


def _permission_request(adapter, *, tool="myagents_write") -> dict:
    return _permission_request_for_nonce(adapter._process_nonce, tool=tool)


class EarlyPermissionPiClient(FakePiClient):
    permission_tool = "myagents_write"

    async def prompt(self, message: str, images=()):
        self.prompts.append((message, tuple(images)))
        request = _permission_request_for_nonce(
            self.env["MYAGENTS_PI_POLICY_NONCE"],
            tool=self.permission_tool,
        )
        decision = asyncio.create_task(self.extension_ui_handler(request))
        # The extension UI callback is independent from stdout response/event
        # processing in the real client.  Schedule it before acceptance to
        # reproduce the security-sensitive race.
        await asyncio.sleep(0)
        yield {"type": "delivery_committed"}
        self.permission_response = await decision
        yield {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
            },
        }
        yield {"type": "agent_settled"}


def test_pi_is_registered_as_stateful_rpc() -> None:
    from orchestrator import AGENTS, Orchestrator
    from pi_rpc.adapter import PiRpcAdapter

    spec = AGENTS["pi"]
    assert spec.transport == "rpc"
    orch = Orchestrator("/tmp", persistent=False)
    assert isinstance(orch.adapters["pi"], PiRpcAdapter)
    assert orch.adapters["pi"].stateful_session is True
    assert orch.adapters["pi"].replay_history_on_fresh_session is False


def test_stream_prepared_attests_maps_events_and_checkpoints() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-adapter-") as raw:
            adapter = PiRpcAdapter(
                client_factory=FakePiClient,
                state_root=Path(raw) / "state",
            )
            prepared = []
            try:
                events = [event async for event in adapter.stream_prepared(
                    lambda prep: prepared.append(prep) or "hello",
                    raw,
                )]
            finally:
                await adapter.aclose()

            assert len(prepared) == 1
            assert prepared[0].fresh is True
            assert prepared[0].restored is False
            assert prepared[0].session_id.startswith("pi:v1:")
            assert [event.kind for event in events] == [
                "delivery_committed", "info", "status", "text",
                "tool", "tool", "activity", "activity", "done",
            ]
            assert events[2].text == "Pi 正在分析…"
            assert events[3].text == "Pi reply"
            assert events[4].text == "read"
            assert events[4].meta["status"] == "in_progress"
            assert events[5].meta["status"] == "completed"
            assert "private reasoning" not in "".join(
                event.text for event in events)
            cmd = FakePiClient.instances[0].cmd
            assert cmd[:2] == ["pi", "--mode"]
            assert cmd[2] == "rpc"
            assert "--no-extensions" in cmd
            assert "--no-skills" in cmd
            assert "--no-prompt-templates" in cmd
            assert "--no-themes" in cmd
            assert "--no-builtin-tools" in cmd
            assert "--no-approve" in cmd
            assert "--offline" in cmd
            assert FakePiClient.instances[0].env["PI_OFFLINE"] == "1"

    asyncio.run(run())


def test_interject_uses_native_steer_only_after_delivery_commit() -> None:
    from pi_rpc.adapter import PiRpcAdapter, PiRpcError

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-steer-") as raw:
            adapter = PiRpcAdapter(
                client_factory=SteeringPiClient,
                state_root=Path(raw) / "state",
            )
            stream = adapter.stream("hello", raw)
            try:
                try:
                    await adapter.interject("too early")
                except PiRpcError:
                    pass
                else:
                    raise AssertionError("pre-commit Pi interjection was accepted")

                assert (await anext(stream)).kind == "delivery_committed"
                assert (await anext(stream)).kind == "info"
                assert (await anext(stream)).kind == "activity"
                pending = asyncio.create_task(anext(stream))
                await asyncio.sleep(0)

                await adapter.interject("先给结论")
                client = SteeringPiClient.instances[-1]
                assert client.steers == ["先给结论"]
                await pending
                remaining = [event async for event in stream]
                assert remaining[-1].kind == "done"
            finally:
                await stream.aclose()
                await adapter.aclose()

    asyncio.run(run())


def test_profiles_rebuild_process_and_never_restore_across_modes() -> None:
    from pi_rpc.adapter import (
        PI_ALL_TOOLS,
        PI_READ_ONLY_TOOLS,
        PiRpcAdapter,
    )

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-profiles-") as raw:
            adapter = PiRpcAdapter(
                client_factory=FakePiClient,
                state_root=Path(raw) / "state",
            )
            try:
                first = [event async for event in adapter.stream("one", raw)]
                first_token = adapter.session_id
                second = [event async for event in adapter.stream("two", raw)]
                assert len(FakePiClient.instances) == 1
                assert first and second

                readonly = [event async for event in adapter.stream(
                    "review", raw,
                    execution_mode=ExecutionMode.READ_ONLY,
                )]
                assert readonly
                assert len(FakePiClient.instances) == 2
                assert FakePiClient.instances[0].closed is True
                assert "--session" not in FakePiClient.instances[1].cmd
                assert FakePiClient.instances[1].env[
                    "MYAGENTS_PI_PROFILE"] == "read_only"
                ro_tools = FakePiClient.instances[1].cmd[
                    FakePiClient.instances[1].cmd.index("--tools") + 1
                ]
                assert ro_tools == ",".join(PI_READ_ONLY_TOOLS)

                write = [event async for event in adapter.stream_prepared(
                    lambda _prep: "implement",
                    raw,
                    first_token,
                    execution_mode=ExecutionMode.WORKSPACE_WRITE,
                )]
                assert write
                assert len(FakePiClient.instances) == 3
                assert "--session" not in FakePiClient.instances[2].cmd
                assert FakePiClient.instances[2].env[
                    "MYAGENTS_PI_PROFILE"] == "workspace_write"
                write_tools = FakePiClient.instances[2].cmd[
                    FakePiClient.instances[2].cmd.index("--tools") + 1
                ]
                assert write_tools == ",".join(PI_ALL_TOOLS)
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_new_pi_session_path_may_materialize_on_first_prompt() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-lazy-session-") as raw:
            adapter = PiRpcAdapter(
                client_factory=LazySessionPiClient,
                state_root=Path(raw) / "state",
            )
            try:
                events = [event async for event in adapter.stream("hello", raw)]
                assert events[-1].kind == "done"
                assert LazySessionPiClient.instances[-1].session_file.is_file()
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_fresh_session_info_is_not_exposed_before_prompt_acceptance() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-info-order-") as raw:
            adapter = PiRpcAdapter(
                client_factory=LazySessionPiClient,
                state_root=Path(raw) / "state",
            )
            generator = adapter.stream("hello", raw)
            try:
                first = await anext(generator)
                client = FakePiClient.instances[-1]
                assert first.kind == "delivery_committed"
                assert client.prompts == [("hello", ())]
                assert client.session_file.is_file()
                second = await anext(generator)
                assert second.kind == "info"
            finally:
                await generator.aclose()
                await adapter.aclose()

    asyncio.run(run())


def test_success_without_materialized_session_is_uncertain_and_reclaimed() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-no-session-") as raw:
            adapter = PiRpcAdapter(
                client_factory=NeverMaterializedSessionPiClient,
                state_root=Path(raw) / "state",
            )
            try:
                try:
                    async for _event in adapter.stream("hello", raw):
                        pass
                except AgentDeliveryUncertainError:
                    pass
                else:
                    raise AssertionError("settled 后 session 未落盘必须失败")
                assert adapter.pid is None
                assert FakePiClient.instances[-1].closed is True
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_preflight_rejected_reservation_recovers_fresh_after_restart() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-reserved-reject-") as raw:
            state_root = Path(raw) / "state"
            tokens: list[str] = []
            first = PiRpcAdapter(
                client_factory=PreflightRejectedLazySessionPiClient,
                state_root=state_root,
            )
            try:
                try:
                    async for _event in first.stream_prepared(
                        lambda prep: tokens.append(prep.session_id) or "reject",
                        raw,
                    ):
                        pass
                except PiRpcRemoteError:
                    pass
                else:
                    raise AssertionError("preflight reject 必须保持未提交语义")
                assert len(tokens) == 1
                assert not FakePiClient.instances[-1].session_file.exists()
            finally:
                await first.aclose()

            second = PiRpcAdapter(
                client_factory=LazySessionPiClient,
                state_root=state_root,
            )
            try:
                events = [event async for event in second.stream_prepared(
                    lambda prep: "retry",
                    raw,
                    tokens[0],
                )]
                assert events[-1].kind == "done"
                assert "--session" not in FakePiClient.instances[-1].cmd
            finally:
                await second.aclose()

    asyncio.run(run())


def test_reserved_marker_tamper_fails_before_restart() -> None:
    from pi_rpc.adapter import PiRpcAdapter, PiRpcError

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-marker-tamper-") as raw:
            state_root = Path(raw) / "state"
            tokens: list[str] = []
            first = PiRpcAdapter(
                client_factory=PreflightRejectedLazySessionPiClient,
                state_root=state_root,
            )
            try:
                try:
                    async for _event in first.stream_prepared(
                        lambda prep: tokens.append(prep.session_id) or "reject",
                        raw,
                    ):
                        pass
                except PiRpcRemoteError:
                    pass
                else:
                    raise AssertionError("fixture 必须留下 reserved marker")
            finally:
                await first.aclose()

            marker = next(state_root.rglob("*.checkpoint"))
            payload = json.loads(marker.read_text(encoding="utf-8"))
            payload["profile"] = "read_only"
            marker.write_text(
                json.dumps(payload, separators=(",", ":"), sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            os.chmod(marker, 0o600)

            second = PiRpcAdapter(
                client_factory=LazySessionPiClient,
                state_root=state_root,
            )
            try:
                try:
                    async for _event in second.stream_prepared(
                        lambda _prep: "must not run",
                        raw,
                        tokens[0],
                    ):
                        pass
                except PiRpcError as exc:
                    assert "marker" in str(exc) or "绑定" in str(exc)
                else:
                    raise AssertionError("篡改 reserved marker 后不得启动 Pi")
                assert len(FakePiClient.instances) == 1
            finally:
                await second.aclose()

    asyncio.run(run())


def test_accepted_cancelled_reservation_recovers_fresh_after_restart() -> None:
    from adapters.base import AgentDeliveryCancelledError
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-reserved-cancel-") as raw:
            state_root = Path(raw) / "state"
            tokens: list[str] = []
            first = PiRpcAdapter(
                client_factory=AcceptedWaitingLazySessionPiClient,
                state_root=state_root,
            )
            generator = first.stream_prepared(
                lambda prep: tokens.append(prep.session_id) or "cancel",
                raw,
            )
            try:
                assert (await anext(generator)).kind == "delivery_committed"
                assert (await anext(generator)).kind == "info"
                client = FakePiClient.instances[-1]
                await asyncio.wait_for(client.waiting_after_commit.wait(), 1)
                pending = asyncio.create_task(anext(generator))
                await asyncio.sleep(0)
                pending.cancel()
                try:
                    await pending
                except AgentDeliveryCancelledError:
                    pass
                else:
                    raise AssertionError("accepted cancel 必须保持 no-replay 语义")
                assert not client.session_file.exists()
            finally:
                await generator.aclose()
                await first.aclose()

            second = PiRpcAdapter(
                client_factory=LazySessionPiClient,
                state_root=state_root,
            )
            try:
                events = [event async for event in second.stream_prepared(
                    lambda prep: "after cancel",
                    raw,
                    tokens[0],
                )]
                assert events[-1].kind == "done"
                assert "--session" not in FakePiClient.instances[-1].cmd
            finally:
                await second.aclose()

    asyncio.run(run())


def test_materialized_session_deletion_remains_fail_closed_after_restart() -> None:
    from pi_rpc.adapter import PiRpcAdapter, PiRpcError

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-materialized-delete-") as raw:
            state_root = Path(raw) / "state"
            tokens: list[str] = []
            first = PiRpcAdapter(
                client_factory=LazySessionPiClient,
                state_root=state_root,
            )
            try:
                events = [event async for event in first.stream_prepared(
                    lambda prep: tokens.append(prep.session_id) or "persist",
                    raw,
                )]
                assert events[-1].kind == "done"
                materialized = FakePiClient.instances[-1].session_file
                assert materialized.is_file()
            finally:
                await first.aclose()
            materialized.unlink()

            second = PiRpcAdapter(
                client_factory=LazySessionPiClient,
                state_root=state_root,
            )
            try:
                try:
                    async for _event in second.stream_prepared(
                        lambda prep: "must not run",
                        raw,
                        tokens[0],
                    ):
                        pass
                except PiRpcError as exc:
                    assert "持久化" in str(exc) or "缺失" in str(exc)
                else:
                    raise AssertionError("materialized session 删除后不得降 fresh")
                assert len(FakePiClient.instances) == 1
            finally:
                await second.aclose()

    asyncio.run(run())


def test_unknown_terminal_stop_reason_is_uncertain_and_never_done() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-stop-reason-") as raw:
            adapter = PiRpcAdapter(
                client_factory=MalformedStopReasonPiClient,
                state_root=Path(raw) / "state",
            )
            events = []
            try:
                try:
                    async for event in adapter.stream("malformed", raw):
                        events.append(event)
                except AgentDeliveryUncertainError as exc:
                    assert "stopReason" in str(exc) or "终止" in str(exc)
                else:
                    raise AssertionError("未知 terminal stopReason 不得报告成功")
                assert not any(event.kind == "done" for event in events)
                assert adapter.pid is None
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_settled_without_assistant_terminal_is_uncertain_and_never_done() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-no-terminal-") as raw:
            adapter = PiRpcAdapter(
                client_factory=MissingAssistantTerminalPiClient,
                state_root=Path(raw) / "state",
            )
            events = []
            try:
                try:
                    async for event in adapter.stream("missing terminal", raw):
                        events.append(event)
                except AgentDeliveryUncertainError as exc:
                    assert "message_end" in str(exc) or "终态" in str(exc)
                else:
                    raise AssertionError(
                        "agent_settled 不能替代 assistant terminal")
                assert not any(event.kind == "done" for event in events)
                assert adapter.pid is None
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_deferred_terminal_stop_reason_is_an_explicit_success() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-deferred-") as raw:
            adapter = PiRpcAdapter(
                client_factory=DeferredStopReasonPiClient,
                state_root=Path(raw) / "state",
            )
            try:
                events = [event async for event in adapter.stream(
                    "deferred",
                    raw,
                )]
                assert events[-1].kind == "done"
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_cancel_after_prompt_pump_starts_is_no_replay_even_before_commit() -> None:
    from adapters.base import AgentDeliveryCancelledError
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-cancel-send-") as raw:
            adapter = PiRpcAdapter(
                client_factory=WaitingBeforeCommitPiClient,
                state_root=Path(raw) / "state",
            )
            generator = adapter.stream("cancel me", raw)
            try:
                pending = asyncio.create_task(anext(generator))
                await asyncio.sleep(0)
                client = FakePiClient.instances[-1]
                await asyncio.wait_for(client.prompt_started.wait(), 1)
                pending.cancel()
                try:
                    await pending
                except AgentDeliveryCancelledError:
                    pass
                else:
                    raise AssertionError(
                        "prompt pump 启动后的取消必须建立 no-replay 边界")
                assert adapter.pid is None
                assert client.closed is True
            finally:
                await generator.aclose()
                await adapter.aclose()

    asyncio.run(run())


def test_active_tool_uses_longer_watchdog_than_idle_prompt() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-tool-watchdog-") as raw:
            adapter = PiRpcAdapter(
                client_factory=QuietToolPiClient,
                state_root=Path(raw) / "state",
                inactivity_timeout=0.02,
                tool_inactivity_timeout=0.2,
            )
            try:
                events = [event async for event in adapter.stream("build", raw)]
                assert events[-1].kind == "done"
                assert [
                    event.meta.get("status") for event in events
                    if event.kind == "tool"
                ] == ["in_progress", "completed"]
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_terminal_assistant_error_is_failed_and_never_done() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-terminal-") as raw:
            adapter = PiRpcAdapter(
                client_factory=TerminalErrorPiClient,
                state_root=Path(raw) / "state",
            )
            events = []
            try:
                try:
                    async for event in adapter.stream("fail", raw):
                        events.append(event)
                except AgentDeliveryUncertainError as exc:
                    assert "provider unavailable" in str(exc)
                else:
                    raise AssertionError("terminal assistant error 必须失败")
                assert "partial reply" in "".join(
                    event.text for event in events if event.kind == "text")
                assert not any(event.kind == "done" for event in events)
                assert adapter.pid is None
                assert FakePiClient.instances[-1].closed is True
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_terminal_error_followed_by_successful_retry_is_done() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-retry-") as raw:
            adapter = PiRpcAdapter(
                client_factory=RetriedTerminalPiClient,
                state_root=Path(raw) / "state",
            )
            try:
                events = [event async for event in adapter.stream("retry", raw)]
                assert any(event.kind == "done" for event in events)
                assert "recovered reply" in "".join(
                    event.text for event in events if event.kind == "text")
                assert adapter.pid is not None
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_permission_terminated_tool_is_failed_and_never_done() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-blocked-tool-") as raw:
            adapter = PiRpcAdapter(
                client_factory=TerminatedToolPiClient,
                state_root=Path(raw) / "state",
            )
            events = []
            try:
                try:
                    async for event in adapter.stream("write", raw):
                        events.append(event)
                except AgentDeliveryUncertainError as exc:
                    assert "write" in str(exc)
                else:
                    raise AssertionError("permission terminate 必须失败")
                assert not any(event.kind == "done" for event in events)
                assert adapter.pid is None
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_recoverable_tool_error_followed_by_assistant_success_is_done() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-tool-recover-") as raw:
            adapter = PiRpcAdapter(
                client_factory=RecoveringToolErrorPiClient,
                state_root=Path(raw) / "state",
            )
            try:
                events = [event async for event in adapter.stream("read", raw)]
                assert any(event.kind == "done" for event in events)
                assert "recovered after tool error" in "".join(
                    event.text for event in events if event.kind == "text")
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_profile_switch_cannot_restart_after_concurrent_close() -> None:
    from pi_rpc.adapter import PiRpcAdapter, PiRpcError

    async def collect(stream) -> list:
        return [event async for event in stream]

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-close-race-") as raw:
            adapter = PiRpcAdapter(
                client_factory=BlockingClosePiClient,
                state_root=Path(raw) / "state",
                cancel_timeout=0.02,
            )
            await collect(adapter.stream("first", raw))
            old_client = FakePiClient.instances[-1]
            old_client.block_close = True
            switch = asyncio.create_task(collect(adapter.stream(
                "review",
                raw,
                execution_mode=ExecutionMode.READ_ONLY,
            )))
            await asyncio.wait_for(old_client.close_started.wait(), 1)
            await adapter.aclose()
            old_client.close_release.set()
            try:
                await switch
            except PiRpcError as exc:
                assert "关闭" in str(exc)
            else:
                raise AssertionError("close 返回后不得重启 Pi")
            assert len(FakePiClient.instances) == 1
            assert adapter.pid is None
            await adapter.aclose()

    asyncio.run(run())


def test_permission_bridge_reuses_shared_dialog_and_validates_option() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        seen: list[tuple[str, dict]] = []

        async def allow(agent_name: str, params: dict) -> dict:
            seen.append((agent_name, params))
            option = next(
                item for item in params["options"]
                if item["kind"] == "allow_once"
            )
            return {"outcome": "selected", "optionId": option["optionId"]}

        with tempfile.TemporaryDirectory(prefix="myagents-pi-permission-") as raw:
            adapter = PiRpcAdapter(
                client_factory=EarlyPermissionPiClient,
                state_root=Path(raw) / "state",
            )
            adapter.set_permission_handler(allow)
            generator = adapter.stream("write", raw)
            try:
                first = await anext(generator)
                assert first.kind == "delivery_committed"
                assert seen == []
                second = await anext(generator)
                assert second.kind == "info"
                await asyncio.sleep(0)
                assert seen and seen[0][0] == "pi"
                events = [first, second]
                events.extend([event async for event in generator])
            finally:
                await generator.aclose()
                await adapter.aclose()

            assert seen[0][1]["_myagents_mirrors_permission_events"] is True
            client = FakePiClient.instances[-1]
            assert client.permission_response == {
                "type": "extension_ui_response",
                "id": "permission-1",
                "value": "allow_once:call-nonce-1",
            }
            permission_events = [
                event.text for event in events if event.kind == "permission"
            ]
            assert permission_events == [
                "等待权限：write",
                "权限已允许一次：write",
            ]

    asyncio.run(run())


def test_read_only_profile_rejects_mutating_permission_before_dialog() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        seen: list[tuple[str, dict]] = []

        async def should_not_run(agent_name: str, params: dict) -> dict:
            seen.append((agent_name, params))
            return {"outcome": "cancelled"}

        with tempfile.TemporaryDirectory(prefix="myagents-pi-ro-permission-") as raw:
            adapter = PiRpcAdapter(
                client_factory=EarlyPermissionPiClient,
                state_root=Path(raw) / "state",
                permission_handler=should_not_run,
            )
            try:
                events = [event async for event in adapter.stream(
                    "review",
                    raw,
                    execution_mode=ExecutionMode.READ_ONLY,
                )]
                assert events[-1].kind == "done"
                assert FakePiClient.instances[-1].permission_response == {
                    "type": "extension_ui_response",
                    "id": "permission-1",
                    "cancelled": True,
                }
                assert seen == []
            finally:
                await adapter.aclose()

    asyncio.run(run())


def test_late_duplicate_attestation_reclaims_generation_before_prompt() -> None:
    from pi_rpc.adapter import PiRpcAdapter, PiRpcError

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-late-attest-") as raw:
            adapter = PiRpcAdapter(
                client_factory=FakePiClient,
                state_root=Path(raw) / "state",
            )
            try:
                async with adapter._lock:
                    prepared = await adapter._prepare_locked(
                        raw,
                        None,
                        ExecutionMode.DEFAULT,
                    )
                    client = FakePiClient.instances[-1]
                    assert client.attestation_request is not None
                    duplicate = dict(client.attestation_request)
                    duplicate["id"] = "attest-late-duplicate"
                    response = await client.extension_ui_handler(duplicate)
                    assert response == {
                        "type": "extension_ui_response",
                        "id": "attest-late-duplicate",
                        "cancelled": True,
                    }
                    assert client.closed is True
                    try:
                        async for _event in adapter._prompt_locked(
                            "must not send",
                            prepared.session_id,
                        ):
                            pass
                    except PiRpcError as exc:
                        assert "attestation" in str(exc)
                    else:
                        raise AssertionError(
                            "迟到的重复 attestation 必须阻断当前 generation")
                    assert client.prompts == []
                    assert adapter.session_id is None
            finally:
                await adapter.aclose()

    asyncio.run(run())
    print("ok  late duplicate attestation reclaims before prompt")


def test_adapter_does_not_prefetch_unbounded_client_events() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-backpressure-") as raw:
            adapter = PiRpcAdapter(
                client_factory=BurstPiClient,
                state_root=Path(raw) / "state",
            )
            generator = adapter.stream("burst", raw)
            try:
                first = await anext(generator)
                assert first.kind == "delivery_committed"
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                client = FakePiClient.instances[-1]
                assert isinstance(client, BurstPiClient)
                assert client.produced <= 1
            finally:
                await generator.aclose()
                await adapter.aclose()

            assert FakePiClient.instances[-1].closed is True

    asyncio.run(run())
    print("ok  adapter keeps one-event backpressure over client budget")


def test_bad_attestation_fails_before_prompt_and_closes_process() -> None:
    from pi_rpc.adapter import PiRpcAdapter, PiRpcError

    async def run() -> None:
        FakePiClient.instances.clear()
        FakePiClient.bad_attestation = True
        with tempfile.TemporaryDirectory(prefix="myagents-pi-attest-") as raw:
            adapter = PiRpcAdapter(
                client_factory=FakePiClient,
                state_root=Path(raw) / "state",
                startup_timeout=0.05,
            )
            try:
                try:
                    async for _event in adapter.stream("must not send", raw):
                        pass
                except PiRpcError as exc:
                    assert "握手" in str(exc) or "attestation" in str(exc)
                else:
                    raise AssertionError("伪造 bridge attestation 必须失败")
            finally:
                await adapter.aclose()
                FakePiClient.bad_attestation = False

            assert FakePiClient.instances[0].prompts == []
            assert FakePiClient.instances[0].closed is True

    asyncio.run(run())


def test_bad_ready_status_fails_before_prompt_and_closes_process() -> None:
    from pi_rpc.adapter import PiRpcAdapter, PiRpcError

    async def run() -> None:
        FakePiClient.instances.clear()
        FakePiClient.bad_ready = True
        with tempfile.TemporaryDirectory(prefix="myagents-pi-ready-") as raw:
            adapter = PiRpcAdapter(
                client_factory=FakePiClient,
                state_root=Path(raw) / "state",
                startup_timeout=0.05,
            )
            try:
                try:
                    async for _event in adapter.stream("must not send", raw):
                        pass
                except PiRpcError as exc:
                    assert "ready" in str(exc) or "attestation" in str(exc)
                else:
                    raise AssertionError("伪造 bridge ready status 必须失败")
            finally:
                await adapter.aclose()
                FakePiClient.bad_ready = False

            assert FakePiClient.instances[0].prompts == []
            assert FakePiClient.instances[0].closed is True

    asyncio.run(run())


def test_uncertain_delivery_rebuilds_process_before_any_next_turn() -> None:
    from pi_rpc.adapter import PiRpcAdapter

    async def run() -> None:
        FakePiClient.instances.clear()
        with tempfile.TemporaryDirectory(prefix="myagents-pi-uncertain-") as raw:
            adapter = PiRpcAdapter(
                client_factory=UncertainPiClient,
                state_root=Path(raw) / "state",
            )
            try:
                try:
                    async for _event in adapter.stream("uncertain", raw):
                        pass
                except AgentDeliveryUncertainError:
                    pass
                else:
                    raise AssertionError("uncertain delivery 必须向上层传播")
                assert adapter.session_id is None
                assert adapter.pid is None
                assert FakePiClient.instances[0].closed is True
            finally:
                await adapter.aclose()

    asyncio.run(run())


if __name__ == "__main__":
    test_pi_is_registered_as_stateful_rpc()
    test_stream_prepared_attests_maps_events_and_checkpoints()
    test_interject_uses_native_steer_only_after_delivery_commit()
    test_profiles_rebuild_process_and_never_restore_across_modes()
    test_new_pi_session_path_may_materialize_on_first_prompt()
    test_fresh_session_info_is_not_exposed_before_prompt_acceptance()
    test_success_without_materialized_session_is_uncertain_and_reclaimed()
    test_preflight_rejected_reservation_recovers_fresh_after_restart()
    test_reserved_marker_tamper_fails_before_restart()
    test_accepted_cancelled_reservation_recovers_fresh_after_restart()
    test_materialized_session_deletion_remains_fail_closed_after_restart()
    test_cancel_after_prompt_pump_starts_is_no_replay_even_before_commit()
    test_active_tool_uses_longer_watchdog_than_idle_prompt()
    test_terminal_assistant_error_is_failed_and_never_done()
    test_terminal_error_followed_by_successful_retry_is_done()
    test_permission_terminated_tool_is_failed_and_never_done()
    test_recoverable_tool_error_followed_by_assistant_success_is_done()
    test_unknown_terminal_stop_reason_is_uncertain_and_never_done()
    test_settled_without_assistant_terminal_is_uncertain_and_never_done()
    test_deferred_terminal_stop_reason_is_an_explicit_success()
    test_profile_switch_cannot_restart_after_concurrent_close()
    test_permission_bridge_reuses_shared_dialog_and_validates_option()
    test_read_only_profile_rejects_mutating_permission_before_dialog()
    test_late_duplicate_attestation_reclaims_generation_before_prompt()
    test_adapter_does_not_prefetch_unbounded_client_events()
    test_bad_attestation_fails_before_prompt_and_closes_process()
    test_bad_ready_status_fails_before_prompt_and_closes_process()
    test_uncertain_delivery_rebuilds_process_before_any_next_turn()
    print("ok  Pi RPC adapter")

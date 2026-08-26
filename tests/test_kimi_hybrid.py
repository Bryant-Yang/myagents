"""Kimi ACP-first + prepare-only JSONL fallback 契约。

运行：.venv/bin/python tests/test_kimi_hybrid.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapters.kimi_adapter as kimi_jsonl
from acp.adapter import AcpAdapter, AcpKimiAdapter
from adapters.base import (
    AgentDeliveryUncertainError,
    AgentEvent,
    ExecutionMode,
    ReadOnlyFallbackError,
)
from adapters.kimi_adapter import KIMI_READONLY_AGENT_FILE, KimiAdapter
from orchestrator import AGENTS


SERVER = str(Path(__file__).with_name("fake_acp_server.py"))
CMD = [sys.executable, SERVER]


async def collect(stream) -> list[AgentEvent]:
    return [event async for event in stream]


@contextmanager
def fake_acp_env(**values: str | None):
    keys = {
        "FAKE_ACP_STATE",
        "FAKE_ACP_FAIL_INIT",
        "FAKE_ACP_FAIL_NEW",
        "FAKE_ACP_FAIL_LOAD",
        "FAKE_ACP_FAIL_LOAD_CODE",
        "FAKE_ACP_FAIL_LOAD_MESSAGE",
        "FAKE_ACP_HANG_LOAD",
        "FAKE_ACP_DISCONNECT_LOAD",
        "FAKE_ACP_FAIL_PROMPT",
        "FAKE_ACP_NO_LOAD_CAP",
        "FAKE_ACP_CANCEL_DELAY",
    }
    old = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        for key, value in values.items():
            if value is not None:
                os.environ[key] = value
        yield
    finally:
        for key in keys:
            os.environ.pop(key, None)
            value = old[key]
            if value is not None:
                os.environ[key] = value


class RecordingFallback:
    name = "fake"
    session_id = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def stream(self, prompt: str, workdir: str):
        self.calls.append((prompt, workdir))
        yield AgentEvent("text", "READ_ONLY_FALLBACK")
        yield AgentEvent("done")


def test_kimi_jsonl_uses_enforced_readonly_profile() -> None:
    async def body() -> None:
        calls: list[tuple[list[str], str]] = []
        original = kimi_jsonl.stream_jsonl

        async def fake_stream(cmd: list[str], workdir: str):
            calls.append((cmd, workdir))
            yield json.dumps({"role": "assistant", "content": "SAFE"})
            yield json.dumps({
                "role": "meta",
                "type": "session.resume_hint",
                "session_id": "session-jsonl",
            })

        kimi_jsonl.stream_jsonl = fake_stream
        try:
            adapter = KimiAdapter.readonly_fallback()
            events = await collect(adapter.stream("inspect", "/tmp"))
        finally:
            kimi_jsonl.stream_jsonl = original

        assert [event.text for event in events if event.kind == "text"] == [
            "SAFE"]
        assert len(calls) == 1
        cmd, workdir = calls[0]
        assert workdir == "/tmp"
        assert cmd[:2] == ["kimi", "-p"]
        assert "--output-format" in cmd and "stream-json" in cmd
        assert "--agent-file" in cmd
        profile_index = cmd.index("--agent-file") + 1
        assert Path(cmd[profile_index]) == KIMI_READONLY_AGENT_FILE

        profile = KIMI_READONLY_AGENT_FILE.read_text(encoding="utf-8")
        assert "  - Read\n" in profile
        assert "  - Grep\n" in profile
        assert "  - Glob\n" in profile
        assert "subagents: []" in profile
        for forbidden in ("  - Bash\n", "  - Write\n", "  - Edit\n",
                          "  - Skill\n", "  - Agent\n"):
            assert forbidden not in profile

    asyncio.run(body())
    print("ok  Kimi JSONL 降级强制只读 agent profile")


def test_prepare_failure_uses_visible_jsonl_fallback() -> None:
    async def body(state: str) -> None:
        fallback = RecordingFallback()
        adapter = AcpAdapter("fake", CMD, fallback_adapter=fallback)
        preparations = []

        def make_prompt(prep) -> str:
            preparations.append(prep)
            return "safe degraded task"

        events = await collect(adapter.stream_prepared(
            make_prompt, "/tmp", "old-session"))
        await adapter.aclose()

        assert len(preparations) == 1
        prep = preparations[0]
        assert prep.session_id == "fallback:jsonl:fake"
        assert prep.fresh is True and prep.restored is False
        assert prep.load_failed is True
        assert fallback.calls == [("safe degraded task", "/tmp")]
        info = [event for event in events if event.kind == "info"]
        assert len(info) == 1 and "只读 JSONL fallback" in info[0].text
        assert info[0].meta["fallback_scope"] == "prepare-only"
        assert [event.text for event in events if event.kind == "text"] == [
            "READ_ONLY_FALLBACK"]

    with tempfile.TemporaryDirectory() as td:
        state = str(Path(td) / "state")
        with fake_acp_env(FAKE_ACP_STATE=state, FAKE_ACP_FAIL_INIT="1"):
            asyncio.run(body(state))
    print("ok  ACP prepare 失败显式进入只读 JSONL fallback")


def test_fatal_session_prepare_errors_never_use_jsonl_fallback() -> None:
    """Load/auth/policy/quota/transport errors stay on the ACP failure path."""
    cases = (
        {"FAKE_ACP_FAIL_LOAD": "1",
         "FAKE_ACP_FAIL_LOAD_CODE": "-32000",
         "FAKE_ACP_FAIL_LOAD_MESSAGE": "Authentication required"},
        {"FAKE_ACP_FAIL_LOAD": "1",
         "FAKE_ACP_FAIL_LOAD_CODE": "-32000",
         "FAKE_ACP_FAIL_LOAD_MESSAGE": "Policy rejected load"},
        {"FAKE_ACP_FAIL_LOAD": "1",
         "FAKE_ACP_FAIL_LOAD_CODE": "-32000",
         "FAKE_ACP_FAIL_LOAD_MESSAGE": "Quota exceeded"},
        {"FAKE_ACP_FAIL_LOAD": "1",
         "FAKE_ACP_FAIL_LOAD_CODE": "-32000",
         "FAKE_ACP_FAIL_LOAD_MESSAGE": "Backend database unavailable"},
        {"FAKE_ACP_HANG_LOAD": "1"},
        {"FAKE_ACP_DISCONNECT_LOAD": "1"},
    )

    async def body() -> None:
        fallback = RecordingFallback()
        adapter = AcpAdapter(
            "fake",
            CMD,
            fallback_adapter=fallback,
            session_prepare_timeout=0.05,
        )
        try:
            await collect(adapter.stream_prepared(
                lambda _prep: "must-not-cross-protocol",
                "/tmp",
                "persisted-session",
            ))
        except Exception:
            pass
        else:
            raise AssertionError("fatal ACP prepare 错误必须 fail-closed")
        assert fallback.calls == []
        await adapter.aclose()

    with tempfile.TemporaryDirectory() as td:
        for index, flags in enumerate(cases):
            with fake_acp_env(
                FAKE_ACP_STATE=str(Path(td) / f"state-{index}"),
                **flags,
            ):
                asyncio.run(body())
    print("ok  Kimi load/auth/policy/quota/transport/timeout 零 fallback")


def test_execution_mode_denies_readonly_and_blocks_write_fallback() -> None:
    async def readonly_body(state: str) -> None:
        adapter = AcpAdapter("fake", CMD, fallback_adapter=RecordingFallback())
        adapter.set_permission_handler(
            lambda _name, _params: {
                "outcome": "selected", "optionId": "allow"})
        await collect(adapter.stream(
            "perm", "/tmp", execution_mode=ExecutionMode.READ_ONLY))
        await adapter.aclose()
        permission = next(
            line for line in Path(state).read_text(encoding="utf-8").splitlines()
            if line.startswith("permission:"))
        assert "cancelled" in permission
        assert "allow" not in permission

    async def write_body() -> None:
        adapter = AcpAdapter("fake", CMD, fallback_adapter=RecordingFallback())
        try:
            await collect(adapter.stream_prepared(
                lambda _prep: "write",
                "/tmp",
                execution_mode=ExecutionMode.WORKSPACE_WRITE,
            ))
        except ReadOnlyFallbackError:
            pass
        else:
            raise AssertionError("workspace_write 不得进入只读 fallback")
        finally:
            await adapter.aclose()

    with tempfile.TemporaryDirectory() as td:
        state = str(Path(td) / "state")
        with fake_acp_env(FAKE_ACP_STATE=state):
            asyncio.run(readonly_body(state))
        with fake_acp_env(
            FAKE_ACP_STATE=state,
            FAKE_ACP_FAIL_INIT="1",
        ):
            asyncio.run(write_body())
    print("ok  execution mode 只读拒权 + 写阶段 fallback fail-closed")


def test_fallback_checkpoint_is_not_loaded_as_acp_session() -> None:
    async def body(state: str) -> None:
        fallback = RecordingFallback()
        adapter = AcpAdapter("fake", CMD, fallback_adapter=fallback)

        os.environ["FAKE_ACP_FAIL_INIT"] = "1"
        first_preps = []
        await collect(adapter.stream_prepared(
            lambda prep: first_preps.append(prep) or "fallback round",
            "/tmp",
        ))
        os.environ.pop("FAKE_ACP_FAIL_INIT", None)

        second_preps = []
        events = await collect(adapter.stream_prepared(
            lambda prep: second_preps.append(prep) or "live round",
            "/tmp",
            first_preps[0].session_id,
        ))
        await adapter.aclose()

        assert fallback.calls == [("fallback round", "/tmp")]
        assert second_preps[0].fresh is True
        assert second_preps[0].restored is False
        assert any(event.kind == "done" for event in events)
        lines = Path(state).read_text(encoding="utf-8").splitlines()
        assert f"new:{Path('/tmp').resolve()}" in lines
        assert not any(line.startswith("load:fallback:") for line in lines)

    with tempfile.TemporaryDirectory() as td:
        state = str(Path(td) / "state")
        with fake_acp_env(FAKE_ACP_STATE=state):
            asyncio.run(body(state))
    print("ok  JSONL checkpoint 下轮直接新建 ACP session")


def test_prompt_rejection_never_uses_fallback() -> None:
    async def body() -> None:
        fallback = RecordingFallback()
        adapter = AcpAdapter("fake", CMD, fallback_adapter=fallback)
        failed = None
        try:
            await collect(adapter.stream("rejected prompt", "/tmp"))
        except Exception as exc:
            failed = exc
        finally:
            await adapter.aclose()
        assert failed is not None
        assert fallback.calls == []

    with tempfile.TemporaryDirectory() as td:
        with fake_acp_env(
            FAKE_ACP_STATE=str(Path(td) / "state"),
            FAKE_ACP_FAIL_PROMPT="1",
        ):
            asyncio.run(body())
    print("ok  ACP prompt 明确拒绝不切换 JSONL")


def test_post_submit_uncertainty_never_uses_fallback() -> None:
    async def body() -> None:
        fallback = RecordingFallback()
        adapter = AcpAdapter(
            "fake",
            CMD,
            fallback_adapter=fallback,
            inactivity_timeout=0.05,
            cancel_timeout=0.05,
        )
        failed = None
        try:
            await collect(adapter.stream("slow-never", "/tmp"))
        except AgentDeliveryUncertainError as exc:
            failed = exc
        finally:
            await adapter.aclose()
        assert failed is not None
        assert fallback.calls == []

    with tempfile.TemporaryDirectory() as td:
        with fake_acp_env(
            FAKE_ACP_STATE=str(Path(td) / "state"),
            FAKE_ACP_CANCEL_DELAY="0.01",
        ):
            asyncio.run(body())
    print("ok  ACP 已提交结果不确定时禁止 JSONL 重放")


def test_prompt_disconnect_is_uncertain_and_never_uses_fallback() -> None:
    async def body(state: str) -> None:
        fallback = RecordingFallback()
        adapter = AcpAdapter("fake", CMD, fallback_adapter=fallback)
        failed = None
        try:
            await collect(adapter.stream("disconnect after submit", "/tmp"))
        except AgentDeliveryUncertainError as exc:
            failed = exc
        finally:
            await adapter.aclose()

        assert failed is not None
        assert fallback.calls == []
        lines = Path(state).read_text(encoding="utf-8").splitlines()
        assert "prompt:disconnect after submit" in lines
        assert "disconnect:disconnect after submit" in lines

    with tempfile.TemporaryDirectory() as td:
        state = str(Path(td) / "state")
        with fake_acp_env(FAKE_ACP_STATE=state):
            asyncio.run(body(state))
    print("ok  ACP prompt 写入后断线标记不确定且禁止 JSONL 重放")


def test_delivery_commit_precedes_visible_acp_output() -> None:
    async def body() -> None:
        adapter = AcpAdapter("fake", CMD)
        try:
            events = await collect(adapter.stream("normal round", "/tmp"))
        finally:
            await adapter.aclose()

        kinds = [event.kind for event in events]
        assert kinds.count("delivery_committed") == 1
        committed = kinds.index("delivery_committed")
        visible = min(
            index for index, kind in enumerate(kinds)
            if kind in {"status", "text", "tool", "done"}
        )
        assert committed < visible
        event = events[committed]
        assert event.meta["transport"] == "acp"
        assert event.meta["sessionId"] == "fake-session-1"

    with tempfile.TemporaryDirectory() as td:
        with fake_acp_env(FAKE_ACP_STATE=str(Path(td) / "state")):
            asyncio.run(body())
    print("ok  ACP 首个可见输出前先持久化 delivery commit")


def test_production_registration_is_acp_first_hybrid() -> None:
    spec = AGENTS["kimi"]
    assert spec.transport == "acp+jsonl"
    adapter = spec.factory()
    assert isinstance(adapter, AcpKimiAdapter)
    assert adapter._cmd == ["kimi", "acp"]
    assert isinstance(adapter._fallback, KimiAdapter)
    assert adapter._fallback.agent_file == KIMI_READONLY_AGENT_FILE
    asyncio.run(adapter.aclose())
    print("ok  生产 Kimi 注册 ACP-first + 只读 JSONL fallback")


if __name__ == "__main__":
    test_kimi_jsonl_uses_enforced_readonly_profile()
    test_prepare_failure_uses_visible_jsonl_fallback()
    test_fatal_session_prepare_errors_never_use_jsonl_fallback()
    test_execution_mode_denies_readonly_and_blocks_write_fallback()
    test_fallback_checkpoint_is_not_loaded_as_acp_session()
    test_prompt_rejection_never_uses_fallback()
    test_post_submit_uncertainty_never_uses_fallback()
    test_prompt_disconnect_is_uncertain_and_never_uses_fallback()
    test_delivery_commit_precedes_visible_acp_output()
    test_production_registration_is_acp_first_hybrid()
    print("\nKimi hybrid transport 契约测试全部通过")

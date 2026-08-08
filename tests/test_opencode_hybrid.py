"""OpenCode ACP-first + prepare-only JSONL fallback 契约。

运行：.venv/bin/python tests/test_opencode_hybrid.py
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

import adapters.opencode_adapter as opencode_jsonl
from acp.adapter import (
    OPENCODE_ACP_PERMISSION_POLICY,
    AcpAdapter,
    AcpOpenCodeAdapter,
)
from adapters.base import AgentDeliveryUncertainError, AgentEvent
from adapters.opencode_adapter import (
    OPENCODE_READONLY_AGENT,
    OPENCODE_READONLY_CONFIG_FILE,
    OPENCODE_READONLY_PERMISSION,
    OpenCodeAdapter,
)
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
    name = "opencode"
    session_id = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def stream(self, prompt: str, workdir: str):
        self.calls.append((prompt, workdir))
        yield AgentEvent("text", "OPENCODE_READ_ONLY_FALLBACK")
        yield AgentEvent("done")


def test_jsonl_fallback_enforces_readonly_inline_agent() -> None:
    async def body() -> None:
        calls: list[tuple[list[str], str, dict[str, str] | None]] = []
        original = opencode_jsonl.stream_jsonl

        async def fake_stream(
            cmd: list[str],
            workdir: str,
            *,
            env_overrides: dict[str, str] | None = None,
        ):
            calls.append((cmd, workdir, env_overrides))
            yield json.dumps({
                "type": "text",
                "sessionID": "ses-jsonl",
                "part": {"type": "text", "text": "SAFE"},
            })
            yield json.dumps({
                "type": "step_finish",
                "sessionID": "ses-jsonl",
                "part": {"reason": "stop", "tokens": {"total": 7}},
            })

        opencode_jsonl.stream_jsonl = fake_stream
        try:
            adapter = OpenCodeAdapter.readonly_fallback()
            events = await collect(adapter.stream("inspect", "/tmp"))
        finally:
            opencode_jsonl.stream_jsonl = original

        assert [event.text for event in events if event.kind == "text"] == [
            "SAFE"]
        assert adapter.session_id == "ses-jsonl"
        assert len(calls) == 1
        cmd, workdir, env = calls[0]
        assert workdir == "/tmp"
        assert cmd[:3] == ["opencode", "--pure", "run"]
        assert "--auto" not in cmd
        assert cmd[cmd.index("--agent") + 1] == OPENCODE_READONLY_AGENT
        assert env is not None
        assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
        assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
        assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
        assert json.loads(env["OPENCODE_PERMISSION"]) == (
            OPENCODE_READONLY_PERMISSION)

        profile = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert OPENCODE_READONLY_CONFIG_FILE.is_file()
        readonly = profile["agent"][OPENCODE_READONLY_AGENT]
        assert readonly["mode"] == "primary"
        assert readonly["permission"] == {
            "*": "deny",
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
        }

    asyncio.run(body())
    print("ok  OpenCode JSONL fallback 使用隔离只读 inline agent")


def test_acp_policy_is_ask_by_default() -> None:
    adapter = AcpOpenCodeAdapter(fallback_jsonl=False)
    policy = json.loads(
        adapter._client._env_overrides["OPENCODE_PERMISSION"])
    assert policy == OPENCODE_ACP_PERMISSION_POLICY
    assert policy["*"] == "ask"
    for safe in ("read", "glob", "grep", "list", "lsp", "todowrite"):
        assert policy[safe] == "allow"
    for guarded in (
        "edit", "bash", "task", "skill", "webfetch", "websearch",
        "external_directory",
    ):
        assert policy[guarded] == "ask"
    asyncio.run(adapter.aclose())
    print("ok  OpenCode ACP 未知及有副作用工具统一进入 TUI ask")


def test_prepare_failure_uses_visible_jsonl_fallback() -> None:
    async def body() -> None:
        fallback = RecordingFallback()
        adapter = AcpAdapter("opencode", CMD, fallback_adapter=fallback)
        preparations = []
        events = await collect(adapter.stream_prepared(
            lambda prep: preparations.append(prep) or "safe degraded task",
            "/tmp",
        ))
        await adapter.aclose()

        assert preparations[0].session_id == "fallback:jsonl:opencode"
        assert fallback.calls == [("safe degraded task", "/tmp")]
        info = next(event for event in events if event.kind == "info")
        assert info.meta == {
            "primary_transport": "acp",
            "fallback_transport": "jsonl",
            "fallback_scope": "prepare-only",
        }

    with tempfile.TemporaryDirectory() as td:
        with fake_acp_env(
            FAKE_ACP_STATE=str(Path(td) / "state"),
            FAKE_ACP_FAIL_INIT="1",
        ):
            asyncio.run(body())
    print("ok  OpenCode ACP prepare 失败显式进入只读 JSONL fallback")


def test_post_submit_disconnect_never_uses_fallback() -> None:
    async def body() -> None:
        fallback = RecordingFallback()
        adapter = AcpAdapter("opencode", CMD, fallback_adapter=fallback)
        failed = None
        try:
            await collect(adapter.stream("disconnect after submit", "/tmp"))
        except AgentDeliveryUncertainError as exc:
            failed = exc
        finally:
            await adapter.aclose()
        assert failed is not None
        assert fallback.calls == []

    with tempfile.TemporaryDirectory() as td:
        with fake_acp_env(FAKE_ACP_STATE=str(Path(td) / "state")):
            asyncio.run(body())
    print("ok  OpenCode prompt 写入后断线禁止 JSONL 重放")


def test_production_registration_is_acp_first_hybrid() -> None:
    spec = AGENTS["opencode"]
    assert spec.transport == "acp+jsonl"
    adapter = spec.factory()
    assert isinstance(adapter, AcpOpenCodeAdapter)
    assert adapter._cmd == ["opencode", "acp"]
    assert isinstance(adapter._fallback, OpenCodeAdapter)
    assert (adapter._fallback.readonly_config_file
            == OPENCODE_READONLY_CONFIG_FILE)
    asyncio.run(adapter.aclose())
    print("ok  生产 OpenCode 注册 ACP-first + 只读 JSONL fallback")


if __name__ == "__main__":
    test_jsonl_fallback_enforces_readonly_inline_agent()
    test_acp_policy_is_ask_by_default()
    test_prepare_failure_uses_visible_jsonl_fallback()
    test_post_submit_disconnect_never_uses_fallback()
    test_production_registration_is_acp_first_hybrid()
    print("\nOpenCode hybrid transport 契约测试全部通过")

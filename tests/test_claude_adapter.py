"""Claude Code adapter contract tests (in-memory client fake).

All tests inject a fake stream client; they never start the real Claude CLI
and never touch ``~/.claude``.

Run: .venv/bin/python tests/test_claude_adapter.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import struct
import sys
import tempfile
import uuid as uuid_module
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (  # noqa: E402
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    ExecutionMode,
)
from agent_readiness import ReadinessState  # noqa: E402
from claude_code.adapter import (  # noqa: E402
    CLAUDE_PERMISSION_SERVER,
    CLAUDE_PERMISSION_SOCKET_ENV,
    CLAUDE_PERMISSION_TOKEN_ENV,
    ClaudeAdapterError,
    ClaudeCodeAdapter,
    claude_readiness_probe,
)
from claude_code.client import ClaudeResumeNotFoundError  # noqa: E402
from clipboard_image import TrustedImage  # noqa: E402
from orchestrator import AGENTS  # noqa: E402


WORKDIR_NAME = "claude-adapter-work"


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


class FakeClaudeClient:
    instances: list["FakeClaudeClient"] = []
    resume_miss_once = False
    missing_bridge = False
    bridge_errors = False
    raise_uncertain_after_echo = False
    hang_after_echo = False
    error_result = False
    wrong_session = False
    synthetic_error_text: str | None = None

    def __init__(
        self,
        cmd,
        *,
        cwd,
        env_overrides=None,
        **_kwargs,
    ) -> None:
        self.cmd = list(cmd)
        self.cwd = cwd
        self.env = dict(env_overrides or {})
        self.closed = False
        self.start_calls = 0
        self.turns: list[tuple[str, tuple]] = []
        self.pid = 5000 + len(FakeClaudeClient.instances)
        self._missed = False
        self.init_message: dict | None = None
        self.session_args: tuple[str, ...] = ()
        if "--session-id" in self.cmd:
            index = self.cmd.index("--session-id")
            self.session_args = ("--session-id", self.cmd[index + 1])
        elif "--resume" in self.cmd:
            index = self.cmd.index("--resume")
            self.session_args = ("--resume", self.cmd[index + 1])
        self.mcp_config = None
        if "--mcp-config" in self.cmd:
            index = self.cmd.index("--mcp-config")
            self.mcp_config = json.loads(
                Path(self.cmd[index + 1]).read_text(encoding="utf-8"))
            config_path = Path(self.cmd[index + 1])
            assert config_path.stat().st_mode & 0o777 == 0o600, (
                "mcp-config 文件必须 0600")
        FakeClaudeClient.instances.append(self)

    @property
    def running(self) -> bool:
        return not self.closed

    async def start(self) -> None:
        self.start_calls += 1
        if "--restricted" in self.cmd:
            servers: list = []
        elif FakeClaudeClient.missing_bridge:
            servers = []
        else:
            servers = [{"name": CLAUDE_PERMISSION_SERVER}]
        init: dict = {
            "type": "system",
            "subtype": "init",
            "model": "fake-model",
            "mcp_servers": servers,
        }
        if "--restricted" not in self.cmd and FakeClaudeClient.bridge_errors:
            init["mcp_server_errors"] = [{
                "name": CLAUDE_PERMISSION_SERVER,
                "type": "invalid_config",
                "message": "boom",
            }]
        self.init_message = init

    @property
    def init(self) -> dict | None:
        return self.init_message

    def _session_id(self) -> str:
        return self.session_args[1] if self.session_args else "ephemeral"

    def send_turn(self, message: str, images=()):
        adapter_script = self

        async def _stream():
            adapter_script.turns.append((message, tuple(images)))
            if (adapter_script.session_args[:1] == ("--resume",)
                    and FakeClaudeClient.resume_miss_once
                    and not adapter_script._missed):
                # 真实 CLI 在读取 stdin 之前因 --resume 失败退出。
                adapter_script._missed = True
                raise ClaudeResumeNotFoundError(
                    "No conversation found with session ID: whatever")
            yield {
                "type": "delivery_committed",
                "session_id": adapter_script._session_id(),
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "FAKE "},
                },
                "parent_tool_use_id": None,
            }
            if FakeClaudeClient.synthetic_error_text:
                # CLI 对 API 错误/未登录会本地合成 assistant 消息并仍返回
                # success result（model == "<synthetic>"，无流式 delta）。
                yield {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "<synthetic>",
                        "content": [{
                            "type": "text",
                            "text": FakeClaudeClient.synthetic_error_text,
                        }],
                    },
                    "parent_tool_use_id": None,
                }
                yield {
                    "type": "result",
                    "subtype": "success",
                    "session_id": adapter_script._session_id(),
                }
                return
            yield {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "echo hi"},
                    }],
                },
                "parent_tool_use_id": None,
            }
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "hi",
                    }],
                },
                "parent_tool_use_id": None,
            }
            if FakeClaudeClient.raise_uncertain_after_echo:
                raise AgentDeliveryUncertainError("fake uncertain")
            if FakeClaudeClient.hang_after_echo:
                await asyncio.sleep(30)
            if FakeClaudeClient.wrong_session:
                yield {
                    "type": "result",
                    "subtype": "success",
                    "session_id": "not-the-session",
                }
                return
            if FakeClaudeClient.error_result:
                yield {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "result": "boom",
                }
                return
            yield {
                "type": "result",
                "subtype": "success",
                "session_id": adapter_script._session_id(),
            }

        return _stream()

    async def close(self) -> None:
        self.closed = True

    aclose = close


def reset_fakes() -> None:
    FakeClaudeClient.instances = []
    FakeClaudeClient.resume_miss_once = False
    FakeClaudeClient.missing_bridge = False
    FakeClaudeClient.bridge_errors = False
    FakeClaudeClient.raise_uncertain_after_echo = False
    FakeClaudeClient.hang_after_echo = False
    FakeClaudeClient.error_result = False
    FakeClaudeClient.wrong_session = False
    FakeClaudeClient.synthetic_error_text = None
    # 测试不依赖开发机的真实 ~/.claude：指向独立空目录（无认证可挑拣）。
    global FAKE_CLAUDE_CONFIG_DIR
    FAKE_CLAUDE_CONFIG_DIR = Path(tempfile.mkdtemp(
        prefix="m415-claude-home-"))
    FAKE_CLAUDE_CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(FAKE_CLAUDE_CONFIG_DIR)


FAKE_CLAUDE_CONFIG_DIR = ""


def make_adapter(
    tmp: Path,
    *,
    permission_handler=None,
    **kwargs,
) -> ClaudeCodeAdapter:
    # 状态根必须短：权限桥 socket 受 macOS AF_UNIX 104 字节路径上限约束。
    state_root = Path("/tmp/myagents-claude-test") / uuid_module.uuid4().hex[:8]
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return ClaudeCodeAdapter(
        state_root=state_root,
        client_factory=FakeClaudeClient,
        permission_handler=permission_handler,
        **kwargs,
    )


def workdir(tmp: Path) -> str:
    target = tmp / WORKDIR_NAME
    target.mkdir(exist_ok=True)
    # 与 adapter 的 canonical_workdir 一致：macOS 上 /var/folders 会被
    # resolve 成 /private/var/folders，token 工作区哈希必须用同一形态。
    return str(target.resolve())


def run(coro) -> None:
    asyncio.run(asyncio.wait_for(coro, timeout=20.0))


async def drain(stream, *, limit: int = 50) -> list:
    events = []
    async for event in stream:
        events.append(event)
        if len(events) > limit:
            raise AssertionError("Claude adapter stream did not settle")
    return events


def token_for(workdir_value: str, uuid_value: str, profile: str) -> str:
    return ClaudeCodeAdapter._session_token(
        uuid_value, workdir=workdir_value, profile=profile)


def test_claude_is_registered_as_stateful_stream_json() -> None:
    spec = AGENTS["claude"]
    assert spec.transport == "stream-json"
    assert spec.factory is ClaudeCodeAdapter
    assert ClaudeCodeAdapter.stateful_session is True
    assert ClaudeCodeAdapter.replay_history_on_fresh_session is False
    capability = spec.agent_host_capability()
    assert capability is not None
    assert capability.transport == "stream-json"
    print("ok  Claude 注册为 stateful stream-json 并声明 host capability")


def test_stream_prepared_maps_events_checkpoints_and_argv() -> None:
    async def body() -> None:
        reset_fakes()
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            events = await drain(adapter.stream(
                "hello", workdir(tmp)))
            assert [event.kind for event in events] == [
                "delivery_committed", "info", "text", "tool", "tool", "done",
            ]
            assert events[2].text == "FAKE "
            assert events[3].meta["status"] == "in_progress"
            assert events[4].meta["status"] == "completed"
            assert events[5].meta["claudePid"] == adapter.pid
            session_id = adapter.session_id
            assert session_id and session_id.startswith("claude:v1:")

            client = FakeClaudeClient.instances[0]
            cmd = client.cmd
            assert cmd[:2] == ["claude", "-p"]
            assert {
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                "--setting-sources", "",
                "--strict-mcp-config",
                "--permission-prompt-tool",
                "--permission-mode", "default",
                "--replay-user-messages",
            } <= set(cmd)
            prompt_tool_index = cmd.index("--permission-prompt-tool")
            assert cmd[prompt_tool_index + 1] == (
                f"mcp__{CLAUDE_PERMISSION_SERVER}__request_permission")
            assert "--restricted" not in cmd
            assert client.session_args[:1] == ("--session-id",)
            assert uuid_module.UUID(client.session_args[1])
            for key, value in {
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
            }.items():
                assert client.env[key] == value
            assert client.mcp_config is not None
            server = client.mcp_config["mcpServers"][CLAUDE_PERMISSION_SERVER]
            assert CLAUDE_PERMISSION_SOCKET_ENV in server["env"]
            assert CLAUDE_PERMISSION_TOKEN_ENV in server["env"]
            assert server["args"][0].endswith("permission_server.py")
            with contextlib.suppress(BaseException):
                await adapter.aclose()

    run(body())
    print("ok  普通轮事件映射、argv 闭集与权限桥注入")


def test_second_turn_reuses_process_and_session(tmp_path=None) -> None:
    async def body() -> None:
        reset_fakes()
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            target = workdir(tmp)
            await drain(adapter.stream("one", target))
            first_token = adapter.session_id
            await drain(adapter.stream("two", target))
            assert len(FakeClaudeClient.instances) == 1
            assert adapter.session_id == first_token
            await adapter.aclose()

    run(body())
    print("ok  同 profile 复用同一进程与 session")


def test_read_only_uses_restricted_closure_without_bridge() -> None:
    async def body() -> None:
        reset_fakes()
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            target = workdir(tmp)
            native = str(uuid_module.uuid4())
            durable = token_for(target, native, "default")
            stream = adapter.stream_prepared(
                lambda prep: "review only", target, durable,
                execution_mode=ExecutionMode.READ_ONLY,
            )
            events = await drain(stream)
            assert events[0].kind == "delivery_committed"
            assert any(
                event.kind == "info" and "只读" in event.text
                for event in events)
            client = FakeClaudeClient.instances[0]
            cmd = client.cmd
            assert {
                "--restricted",
                "--tools", "Read,Glob,Grep",
                "--permission-prompts", "none",
                "--no-session-persistence",
                "--strict-mcp-config",
            } <= set(cmd)
            assert "--mcp-config" not in cmd
            assert "--session-id" not in cmd
            assert "--resume" not in cmd
            assert "--permission-mode" not in cmd
            assert client.mcp_config is None
            # 只读轮保留持久 checkpoint 原样，后续普通轮可继续 resume。
            assert adapter.session_id == durable
            # 后续普通轮应重建进程并 resume 原 durable session。
            await drain(adapter.stream_prepared(
                lambda prep: "back to normal", target, durable))
            normal_client = FakeClaudeClient.instances[1]
            assert normal_client.session_args == ("--resume", native)
            assert "--restricted" not in normal_client.cmd
            assert adapter.session_id == durable
            await adapter.aclose()

    run(body())
    print("ok  只读轮使用受限闭集且不破坏持久 session")


def test_profile_switch_rebuilds_process() -> None:
    async def body() -> None:
        reset_fakes()
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            target = workdir(tmp)
            await drain(adapter.stream("write", target))
            await drain(adapter.stream(
                "review", target,
                execution_mode=ExecutionMode.READ_ONLY))
            assert len(FakeClaudeClient.instances) == 2
            assert FakeClaudeClient.instances[0].closed is True
            await drain(adapter.stream("write again", target))
            assert len(FakeClaudeClient.instances) == 3
            assert FakeClaudeClient.instances[1].closed is True
            await adapter.aclose()

    run(body())
    print("ok  跨 profile 重建进程且旧进程已回收")


def test_resume_token_binds_workspace_and_profile() -> None:
    async def body() -> None:
        reset_fakes()
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            target = workdir(tmp)
            native = str(uuid_module.uuid4())
            durable = token_for(target, native, "default")
            adapter = make_adapter(tmp)
            stream = adapter.stream_prepared(
                lambda prep: "continue", target, durable)
            await drain(stream)
            assert FakeClaudeClient.instances[0].session_args == (
                "--resume", native)
            await adapter.aclose()

            other_dir = tmp / "other-work"
            other_dir.mkdir()
            foreign = token_for(str(other_dir), native, "default")
            adapter2 = make_adapter(tmp)
            try:
                await drain(adapter2.stream("cross", target))
                # cross-workspace token never enters this flow, but a direct
                # decode of a foreign token must fail closed.
                adapter2._decode_resume_token(
                    foreign, workdir=target, profile="default")
            except ClaudeAdapterError:
                pass
            else:
                raise AssertionError("foreign workspace token must fail")
            finally:
                await adapter2.aclose()

            adapter3 = make_adapter(tmp)
            readonly_token = token_for(target, native, "read_only")
            # profile 不匹配按跨 profile 规则回退 fresh，而不是报错。
            assert adapter3._decode_resume_token(
                readonly_token, workdir=target, profile="default") is None
            await adapter3.aclose()

            adapter4 = make_adapter(tmp)
            try:
                adapter4._decode_resume_token(
                    "bogus", workdir=target, profile="default")
            except ClaudeAdapterError:
                pass
            else:
                raise AssertionError("malformed token must fail")
            finally:
                await adapter4.aclose()

    run(body())
    print("ok  resume token 绑定工作区与 profile")


def test_resume_miss_falls_back_to_fresh_once() -> None:
    async def body() -> None:
        reset_fakes()
        FakeClaudeClient.resume_miss_once = True
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            target = workdir(tmp)
            native = str(uuid_module.uuid4())
            durable = token_for(target, native, "default")
            adapter = make_adapter(tmp)
            events = await drain(adapter.stream_prepared(
                lambda prep: "continue", target, durable))
            assert events[0].kind == "info"
            assert "回退" in events[0].text
            assert events[1].kind == "delivery_committed"
            assert len(FakeClaudeClient.instances) == 2
            assert FakeClaudeClient.instances[0].closed is True
            second = FakeClaudeClient.instances[1]
            assert second.session_args[:1] == ("--session-id",)
            assert adapter.session_id != durable
            await adapter.aclose()

    run(body())
    print("ok  resume 未命中在首个 turn 精确回退 fresh 一次")


def test_missing_permission_bridge_fails_closed() -> None:
    async def body() -> None:
        reset_fakes()
        FakeClaudeClient.missing_bridge = True
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            try:
                await drain(adapter.stream("hello", workdir(tmp)))
            except AgentDeliveryUncertainError as exc:
                # 回执已消费：必须走 uncertain 让 orchestrator 推进
                # no-replay cursor，禁止把已交付轮洗成普通失败。
                assert "权限桥" in str(exc)
            else:
                raise AssertionError("missing bridge must fail closed")
            assert FakeClaudeClient.instances[0].closed is True
            assert adapter.session_id is None
            await adapter.aclose()

    run(body())
    print("ok  权限桥未注册时普通轮交付后 uncertain 且 fail-closed")


def test_bridge_errors_fail_closed() -> None:
    async def body() -> None:
        reset_fakes()
        FakeClaudeClient.bridge_errors = True
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            try:
                await drain(adapter.stream("hello", workdir(tmp)))
            except AgentDeliveryUncertainError:
                pass
            else:
                raise AssertionError("bridge errors must fail closed")
            assert FakeClaudeClient.instances[0].closed is True
            await adapter.aclose()

    run(body())
    print("ok  权限桥连接错误 fail-closed")


def test_dead_client_is_rebuilt_on_next_turn() -> None:
    async def body() -> None:
        reset_fakes()
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            target = workdir(tmp)
            await drain(adapter.stream("one", target))
            durable = adapter.session_id
            native = adapter._active_uuid
            # 模拟轮间死亡：client 关闭但 _started 仍为 True。orchestrator
            # 每轮都会传持久 session_id，死后重建应继续 --resume。
            await FakeClaudeClient.instances[0].close()
            await drain(adapter.stream_prepared(
                lambda prep: "two", target, durable))
            assert len(FakeClaudeClient.instances) == 2
            assert FakeClaudeClient.instances[1].session_args == (
                "--resume", native)
            assert adapter.session_id == durable
            await adapter.aclose()

    run(body())
    print("ok  死连接在下一轮 prepare 时自动重建并续接 checkpoint")


def test_uncertain_delivery_rebuilds_process_before_next_turn() -> None:
    async def body() -> None:
        reset_fakes()
        FakeClaudeClient.raise_uncertain_after_echo = True
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            target = workdir(tmp)
            try:
                await drain(adapter.stream("hello", target))
            except AgentDeliveryUncertainError:
                pass
            else:
                raise AssertionError("uncertain must propagate")
            first_token = adapter.session_id
            FakeClaudeClient.raise_uncertain_after_echo = False
            await drain(adapter.stream("hello again", target))
            assert len(FakeClaudeClient.instances) == 2
            assert FakeClaudeClient.instances[0].closed is True
            assert adapter.session_id != first_token
            await adapter.aclose()

    run(body())
    print("ok  uncertain 交付后重建进程且更换 session")


def test_cancel_after_commit_is_no_replay_and_rebuilds() -> None:
    async def body() -> None:
        reset_fakes()
        FakeClaudeClient.hang_after_echo = True
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            target = workdir(tmp)

            task = asyncio.create_task(
                drain(adapter.stream("hello", target)))
            # Wait until the turn is submitted, then cancel while the pump
            # runs; the cancel may land before or after delivery_committed.
            while not FakeClaudeClient.instances:
                await asyncio.sleep(0.01)
            while not FakeClaudeClient.instances[0].turns:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert FakeClaudeClient.instances[0].closed is True
            assert adapter.session_id is None
            FakeClaudeClient.hang_after_echo = False
            await drain(adapter.stream("next", target))
            assert len(FakeClaudeClient.instances) == 2
            await adapter.aclose()

    run(body())
    print("ok  提交后取消是 no-replay 并重建进程")


def test_watchdog_times_out_idle_stream() -> None:
    async def body() -> None:
        reset_fakes()
        FakeClaudeClient.hang_after_echo = True
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp, inactivity_timeout=0.2)
            try:
                await drain(adapter.stream("hello", workdir(tmp)))
            except AgentDeliveryUncertainError as exc:
                assert "无活动" in str(exc)
            else:
                raise AssertionError("idle watchdog must fire")
            finally:
                await adapter.aclose()

    run(body())
    print("ok  空闲看护超时判为 uncertain")


def test_only_success_result_settles_the_turn() -> None:
    for flag, message in (
            ("error_result", "执行失败"), ("wrong_session", "不一致")):
        async def body() -> None:
            reset_fakes()
            setattr(FakeClaudeClient, flag, True)
            with TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                adapter = make_adapter(tmp)
                try:
                    await drain(adapter.stream("hello", workdir(tmp)))
                except AgentDeliveryUncertainError as exc:
                    assert message in str(exc)
                else:
                    raise AssertionError(f"{flag} must be uncertain")
                finally:
                    await adapter.aclose()

        run(body())
    print("ok  仅 result subtype success 是成功终态")


def test_permission_denied_before_commit() -> None:
    async def body() -> None:
        reset_fakes()
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            reply = await adapter._answer_permission(
                "c1", "Bash", {"command": "ls"})
            assert reply["behavior"] == "deny"
            assert "尚未提交" in reply["message"]
            await adapter.aclose()

    run(body())
    print("ok  交付确认前权限请求一律拒绝")


def test_permission_allow_reject_and_invalid_outcome() -> None:
    async def body() -> None:
        reset_fakes()
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            async def handler(_name: str, params: dict) -> dict:
                raw = params["toolCall"]["rawInput"]
                allow_ids = [
                    item["optionId"] for item in params["options"]
                    if item["kind"] == "allow_once"]
                command = raw.get("command")
                if command == "cmd-3":
                    return {"outcome": "selected", "optionId": allow_ids[0]}
                if command == "cmd-1":
                    return {"outcome": "selected", "optionId": "bogus"}
                return {"outcome": "cancelled"}

            adapter = make_adapter(tmp, permission_handler=handler)
            target = workdir(tmp)
            stream = adapter.stream("hello", target)
            ait = stream.__aiter__()
            first = await ait.__anext__()
            assert first.kind == "delivery_committed"
            pending = [
                asyncio.create_task(adapter._answer_permission(
                    f"c{i}", "Bash", {"command": f"cmd-{i}"}))
                for i in range(4)
            ]
            second = await ait.__anext__()
            assert second.kind == "info"
            replies = [await task for task in pending]
            assert replies[0]["behavior"] == "deny"
            assert replies[1]["behavior"] == "deny"
            assert replies[2]["behavior"] == "deny"
            assert replies[3]["behavior"] == "allow"
            assert replies[3]["updatedInput"] == {"command": "cmd-3"}
            events = []
            while True:
                event = await ait.__anext__()
                events.append(event)
                if event.kind == "done":
                    break
            texts = [event.text for event in events
                     if event.kind == "permission"]
            assert any("等待权限" in text for text in texts)
            assert any("权限已允许一次" in text for text in texts)
            await adapter.aclose()

    run(body())
    print("ok  权限 outcome 校验 fail-closed 且允许路径回写 allow")


def test_permission_socket_roundtrip() -> None:
    async def body() -> None:
        reset_fakes()
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)

            async def handler(_name: str, params: dict) -> dict:
                allow_ids = [
                    item["optionId"] for item in params["options"]
                    if item["kind"] == "allow_once"]
                return {"outcome": "selected", "optionId": allow_ids[0]}

            adapter = make_adapter(tmp, permission_handler=handler)
            target = workdir(tmp)
            stream = adapter.stream("hello", target)
            ait = stream.__aiter__()
            first = await ait.__anext__()
            assert first.kind == "delivery_committed"
            client = FakeClaudeClient.instances[0]
            env = client.mcp_config["mcpServers"][CLAUDE_PERMISSION_SERVER][
                "env"]
            socket_path = env[CLAUDE_PERMISSION_SOCKET_ENV]
            token = env[CLAUDE_PERMISSION_TOKEN_ENV]

            async def talk(payload: dict) -> dict:
                reader, writer = await asyncio.open_unix_connection(
                    socket_path)
                writer.write(json.dumps(payload).encode() + b"\n")
                await writer.drain()
                line = await reader.readline()
                writer.close()
                with contextlib.suppress(BaseException):
                    await writer.wait_closed()
                return json.loads(line)

            bad = await talk({
                "version": 1, "token": "wrong",
                "toolName": "Bash", "input": {"command": "ls"},
            })
            assert bad["behavior"] == "deny"

            pending = asyncio.create_task(talk({
                "version": 1, "token": token,
                "toolName": "Bash", "input": {"command": "rm -rf /tmp/x"},
            }))
            await ait.__anext__()  # resume -> sets the permission gate
            reply = await asyncio.wait_for(pending, timeout=5.0)
            assert reply["behavior"] == "allow"
            assert reply["updatedInput"] == {"command": "rm -rf /tmp/x"}

            while (await ait.__anext__()).kind != "done":
                pass
            await adapter.aclose()

    run(body())
    print("ok  权限桥 socket 全链路校验 token 并回写 allow")


def test_images_forwarded_to_client_turn(tmp_path=None) -> None:
    async def body() -> None:
        reset_fakes()
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            root = tmp / "attachments"
            root.mkdir(mode=0o700)
            image_path = root / "img-0001.png"
            image_path.write_bytes(_png_bytes())
            image_path.chmod(0o600)
            adapter.set_attachment_root(root)
            await drain(adapter.stream(
                "[图片 1] 看这张图", workdir(tmp)))
            client = FakeClaudeClient.instances[0]
            assert len(client.turns) == 1
            _message, images = client.turns[0]
            assert len(images) == 1
            assert isinstance(images[0], TrustedImage)
            await adapter.aclose()

    run(body())
    print("ok  受信图片进入 client turn")


def test_aclose_reclaims_and_blocks_further_streams() -> None:
    async def body() -> None:
        reset_fakes()
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            await drain(adapter.stream("hello", workdir(tmp)))
            await adapter.aclose()
            assert FakeClaudeClient.instances[0].closed is True
            assert adapter.session_id is None
            assert adapter.pid is None
            try:
                await drain(adapter.stream("again", workdir(tmp)))
            except ClaudeAdapterError:
                pass
            else:
                raise AssertionError("closed adapter must refuse streams")

    run(body())
    print("ok  aclose 回收进程并拒绝后续流")


def test_host_capability_builds_read_only_adapter() -> None:
    async def body() -> None:
        reset_fakes()
        capability = ClaudeCodeAdapter.host_capability()
        adapter = capability.factory()
        assert isinstance(adapter, ClaudeCodeAdapter)
        assert adapter._host_read_only is True
        # 测试注入 fake client，绝不拉起真实 CLI。
        adapter._client_factory = FakeClaudeClient
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            # 状态根必须短：权限桥 socket 受 AF_UNIX 路径上限约束。
            state_root = (
                Path("/tmp/myagents-claude-test")
                / uuid_module.uuid4().hex[:8])
            state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            adapter._state_root = state_root
            await drain(adapter.stream(
                "host question", workdir(tmp),
                execution_mode=ExecutionMode.DEFAULT))
            client = FakeClaudeClient.instances[0]
            assert "--restricted" in client.cmd
            assert "--mcp-config" not in client.cmd
            await adapter.aclose()

    run(body())
    print("ok  Host 构造器强制只读闭集")


def test_synthetic_assistant_error_is_uncertain() -> None:
    async def body() -> None:
        reset_fakes()
        FakeClaudeClient.synthetic_error_text = "Not logged in · Please run /login"
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            try:
                await drain(adapter.stream("hello", workdir(tmp)))
            except AgentDeliveryUncertainError as exc:
                assert "未完成模型调用" in str(exc)
                assert "Not logged in" in str(exc)
            else:
                raise AssertionError("synthetic assistant must be uncertain")
            finally:
                await adapter.aclose()

    run(body())
    print("ok  CLI 合成错误消息如实判为 uncertain 且附错误原文")


def test_auth_settings_copied_from_user_config() -> None:
    async def body() -> None:
        reset_fakes()
        (FAKE_CLAUDE_CONFIG_DIR / "settings.json").write_text(json.dumps({
            "env": {"ANTHROPIC_BASE_URL": "https://proxy.example",
                    "ANTHROPIC_AUTH_TOKEN": "tok"},
            "model": "glm-x",
            "permissions": {"allow": ["Bash"]},
            "enabledPlugins": {"evil": True},
        }), encoding="utf-8")
        with TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            adapter = make_adapter(tmp)
            await drain(adapter.stream("hello", workdir(tmp)))
            client = FakeClaudeClient.instances[0]
            assert "--settings" in client.cmd
            auth_path = Path(client.cmd[client.cmd.index("--settings") + 1])
            assert auth_path.stat().st_mode & 0o777 == 0o600
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
            assert auth["env"]["ANTHROPIC_AUTH_TOKEN"] == "tok"
            assert auth["model"] == "glm-x"
            assert "permissions" not in auth
            assert "enabledPlugins" not in auth
            await adapter.aclose()
            assert not auth_path.exists(), "reset 后认证副本必须清理"

    run(body())
    print("ok  认证键挑拣进 0600 --settings 文件且 permissions 不加载")


def test_readiness_probe_states() -> None:
    def fake_resolver(name: str) -> str | None:
        return None

    missing = claude_readiness_probe(resolver=fake_resolver)
    assert missing.state is ReadinessState.NOT_FOUND

    def good_resolver(name: str) -> str | None:
        return "/usr/local/bin/claude"

    ready = claude_readiness_probe(resolver=good_resolver)
    assert ready.state is ReadinessState.READY
    assert ready.executable == "/usr/local/bin/claude"

    invalid = claude_readiness_probe(
        environ={"MYAGENTS_CLAUDE_CLI": "/nonexistent/claude"},
        resolver=good_resolver,
    )
    assert invalid.state is ReadinessState.INVALID
    print("ok  readiness 探针被动解析 PATH 与显式路径")


if __name__ == "__main__":
    test_claude_is_registered_as_stateful_stream_json()
    test_stream_prepared_maps_events_checkpoints_and_argv()
    test_second_turn_reuses_process_and_session()
    test_read_only_uses_restricted_closure_without_bridge()
    test_profile_switch_rebuilds_process()
    test_resume_token_binds_workspace_and_profile()
    test_resume_miss_falls_back_to_fresh_once()
    test_missing_permission_bridge_fails_closed()
    test_bridge_errors_fail_closed()
    test_dead_client_is_rebuilt_on_next_turn()
    test_uncertain_delivery_rebuilds_process_before_next_turn()
    test_cancel_after_commit_is_no_replay_and_rebuilds()
    test_watchdog_times_out_idle_stream()
    test_only_success_result_settles_the_turn()
    test_permission_denied_before_commit()
    test_permission_allow_reject_and_invalid_outcome()
    test_permission_socket_roundtrip()
    test_synthetic_assistant_error_is_uncertain()
    test_auth_settings_copied_from_user_config()
    test_images_forwarded_to_client_turn()
    test_aclose_reclaims_and_blocks_further_streams()
    test_host_capability_builds_read_only_adapter()
    test_readiness_probe_states()
    print("ok  Claude adapter contract")

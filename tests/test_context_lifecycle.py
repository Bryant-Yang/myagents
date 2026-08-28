"""Context lifecycle, durable checkpoint and TUI acceptance tests."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_readiness import AgentReadiness, ReadinessState
from adapters.base import AgentEvent
from context_lifecycle import (
    AdapterContextSnapshot,
    ContextCheckpoint,
    ContextCommandValidationError,
    ContextCompactionResult,
    ContextLifecycleError,
    ContextPolicy,
    parse_context_command,
)
from main import ChatApp, ComposerInput
from native_agent import NativeAgentRuntime, OpenAICompatibleProvider
from orchestrator import AgentSpec, Orchestrator
from storage.store import CorruptedStorageError, RoomStore
from tests.fake_openai_compatible_server import FakeOpenAICompatibleServer
from textual.widgets import RichLog


def _ready_host() -> AgentReadiness:
    return AgentReadiness(
        "host", ReadinessState.READY, "fixture native model", "none")


class _StatefulUnsupported:
    stateful_session = True
    session_id = "unsupported-session"

    async def stream(self, _prompt, _workdir, **_kwargs):
        yield AgentEvent("text", "unsupported reply")
        yield AgentEvent("done")

    async def stream_prepared(
        self, make_prompt, workdir, resume_session_id=None, **_kwargs,
    ):
        del workdir, resume_session_id
        prep = type("Prep", (), {
            "session_id": self.session_id,
            "restored": False,
            "fresh": True,
            "load_failed": False,
        })()
        make_prompt(prep)
        yield AgentEvent("text", "unsupported reply")
        yield AgentEvent("done")

    async def aclose(self):
        return None


class _CompactableFixture(_StatefulUnsupported):
    session_id = "compactable-session"

    def __init__(self) -> None:
        self.characters = 120
        self.messages = 6
        self.compactions = 0

    def context_snapshot(self) -> AdapterContextSnapshot:
        return AdapterContextSnapshot(
            "fixture-summary", True, self.messages, self.characters)

    async def compact_context(
        self, _policy: ContextPolicy,
    ) -> ContextCompactionResult:
        self.compactions += 1
        before_messages = self.messages
        before_characters = self.characters
        self.messages = 4
        self.characters = 48
        return ContextCompactionResult(
            changed=True,
            summary="fixture durable summary",
            before_messages=before_messages,
            after_messages=self.messages,
            source_messages=before_messages,
            retained_messages=2,
            before_characters=before_characters,
            after_characters=self.characters,
        )


class _BrokenSnapshot(_StatefulUnsupported):
    session_id = "broken-snapshot-session"

    def context_snapshot(self) -> AdapterContextSnapshot:
        raise RuntimeError("fixture secret must not escape")

    async def compact_context(
        self, _policy: ContextPolicy,
    ) -> ContextCompactionResult:
        raise AssertionError("broken snapshot must block before compaction")


def test_context_policy_and_command_boundary() -> None:
    policy = ContextPolicy()
    assert policy.auto_compact is True
    assert policy.retain_messages == 4
    assert parse_context_command("/context").action == "show"
    assert parse_context_command("/compact").target == "host"
    assert parse_context_command("/compact @codex").target == "codex"
    assert parse_context_command("/compactness") is None
    try:
        parse_context_command("/compact codex")
    except ContextCommandValidationError:
        pass
    else:
        raise AssertionError("explicit compact target must use @")
    try:
        parse_context_command("/compact @host extra")
    except ContextCommandValidationError:
        pass
    else:
        raise AssertionError("extra compact arguments must fail")
    try:
        ContextPolicy(retain_messages=3)
    except ValueError:
        pass
    else:
        raise AssertionError("odd retained message count must fail")
    try:
        AdapterContextSnapshot(42, True, 1, 1)
    except ValueError:
        pass
    else:
        raise AssertionError("non-string strategy must fail validation")
    try:
        ContextCheckpoint.create(
            boundary_seq=0,
            summary=42,
            generation=1,
            source_messages=2,
            retained_messages=2,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("non-string checkpoint summary must fail")
    print("ok  ContextPolicy 与 /context /compact 命令边界")


def test_native_runtime_compacts_only_after_authoritative_summary() -> None:
    async def run() -> None:
        with FakeOpenAICompatibleServer() as server:
            runtime = NativeAgentRuntime(
                OpenAICompatibleProvider(server.base_url),
                model_id="fake-model",
                name="native-context",
                system_prompt="tool-less fixture",
            )
            try:
                for index in range(3):
                    _ = [
                        event async for event in runtime.stream(
                            f"turn-{index}-" + "x" * 800, "/tmp")
                    ]
                before = runtime.context_snapshot()
                result = await runtime.compact_context(ContextPolicy(
                    auto_compact=False,
                    trigger_characters=1_000,
                    retain_messages=2,
                    summary_characters=400,
                    source_characters=4_000,
                ))
                after = runtime.context_snapshot()
                assert result.changed is True
                assert after.message_count == 4
                assert after.character_count < before.character_count
                _ = [
                    event async for event in runtime.stream(
                        "after-compaction", "/tmp")
                ]
            finally:
                await runtime.aclose()
            posts = [
                item["json"] for item in server.requests
                if item["method"] == "POST"
            ]
            assert any(
                "MYAGENTS_CONTEXT_COMPACTION_V1"
                in payload["messages"][-1]["content"]
                for payload in posts
            )
            assert any(
                "[myagents 已验证上下文摘要]"
                in message["content"]
                for message in posts[-1]["messages"]
            )
            assert all("tools" not in payload for payload in posts)

    asyncio.run(run())
    print("ok  原生 runtime 权威摘要后才压缩且始终无工具")


def test_failed_summary_keeps_original_runtime_context() -> None:
    async def run() -> None:
        with FakeOpenAICompatibleServer() as server:
            runtime = NativeAgentRuntime(
                OpenAICompatibleProvider(server.base_url),
                model_id="fake-model",
                name="native-context-failure",
                system_prompt="tool-less fixture",
            )
            try:
                for text in (
                    "CONTEXT_SUMMARY_EMPTY " + "x" * 400,
                    "second " + "y" * 400,
                ):
                    _ = [
                        event async for event in runtime.stream(text, "/tmp")
                    ]
                before = runtime.context_snapshot()
                try:
                    await runtime.compact_context(ContextPolicy(
                        auto_compact=False,
                        retain_messages=2,
                        summary_characters=400,
                        source_characters=4_000,
                    ))
                except ContextLifecycleError as exc:
                    assert "空上下文摘要" in str(exc)
                else:
                    raise AssertionError("empty summary must not compact")
                assert runtime.context_snapshot() == before

                _ = [
                    event async for event in runtime.stream(
                        "CONTEXT_SUMMARY_FILTERED " + "z" * 400, "/tmp")
                ]
                before_filtered = runtime.context_snapshot()
                try:
                    await runtime.compact_context(ContextPolicy(
                        auto_compact=False,
                        retain_messages=2,
                        summary_characters=400,
                        source_characters=4_000,
                    ))
                except ContextLifecycleError as exc:
                    assert "权威终态" in str(exc)
                else:
                    raise AssertionError(
                        "non-success terminal must not compact")
                assert runtime.context_snapshot() == before_filtered
            finally:
                await runtime.aclose()

    asyncio.run(run())
    print("ok  摘要失败保持原 runtime 上下文且不假提交")


def test_oversize_summary_source_never_claims_full_boundary() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="myagents-context-oversize-",
        ) as raw, FakeOpenAICompatibleServer() as server:
            workdir = Path(raw) / "workspace"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=Path(raw) / "state")

            def factory(_selection):
                return NativeAgentRuntime(
                    OpenAICompatibleProvider(server.base_url),
                    model_id="fake-model",
                    name="host",
                    system_prompt="tool-less fixture",
                )

            orch = Orchestrator(
                str(workdir), specs=(), store=store,
                host_probe=_ready_host, host_model_factory=factory,
                context_policy=ContextPolicy(
                    auto_compact=True,
                    trigger_characters=1,
                    retain_messages=2,
                    summary_characters=200,
                    source_characters=500,
                ),
            )
            try:
                for index in range(2):
                    await orch.dispatch(
                        "@host " + f"oversize-{index}-" + "x" * 400,
                        lambda _name, _event: None,
                    )
                runtime = orch.host.adapter
                before_context = runtime.context_snapshot()
                before_cursor = store.get_agent_state("host")["cursor"]
                before_requests = len(server.requests)
                outcome = await orch.dispatch(
                    "@host SHOULD_NOT_REACH_OVERSIZE_PROVIDER",
                    lambda _name, _event: None,
                )
                assert outcome.failures
                assert "摘要输入安全上限" in outcome.failures[0].error
                assert runtime.context_snapshot() == before_context
                assert store.get_agent_state("host")["cursor"] == before_cursor
                assert store.get_context_checkpoints() == {}
                assert len(server.requests) == before_requests
            finally:
                await orch.aclose()

    asyncio.run(run())
    print("ok  超限摘要源不截断、不假称覆盖完整边界")


def test_checkpoint_restore_and_automatic_boundary_compaction() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="myagents-context-restore-",
        ) as raw, FakeOpenAICompatibleServer() as server:
            workdir = Path(raw) / "workspace"
            workdir.mkdir()
            state_root = Path(raw) / "state"

            def factory(_selection):
                return NativeAgentRuntime(
                    OpenAICompatibleProvider(server.base_url),
                    model_id="fake-model",
                    name="host",
                    system_prompt="tool-less fixture",
                )

            store = RoomStore(workdir, state_root=state_root)
            first = Orchestrator(
                str(workdir), specs=(), store=store,
                host_probe=_ready_host, host_model_factory=factory,
                context_policy=ContextPolicy(
                    auto_compact=True,
                    trigger_characters=1,
                    retain_messages=2,
                    summary_characters=400,
                    source_characters=4_000,
                ),
            )
            events: list[AgentEvent] = []
            try:
                for text in (
                    "@host RESTORE_OLD_A " + "a" * 600,
                    "@host RESTORE_OLD_B " + "b" * 600,
                    "@host trigger automatic compaction",
                ):
                    await first.dispatch(
                        text,
                        lambda _name, event: events.append(event),
                    )
                checkpoint = store.get_context_checkpoints()["host"]
                assert checkpoint.generation == 1
                assert checkpoint.boundary_seq > 0
                assert checkpoint.source_messages == 4
                assert checkpoint.retained_messages == 2
                assert any(
                    event.kind == "info"
                    and event.text == "上下文已自动压缩"
                    for event in events
                )
            finally:
                await first.aclose()

            second_store = RoomStore(workdir, state_root=state_root)
            second = Orchestrator(
                str(workdir), specs=(), store=second_store,
                host_probe=_ready_host, host_model_factory=factory,
                context_policy=ContextPolicy(auto_compact=False),
            )
            try:
                await second.dispatch(
                    "@host RESTORE_NEW", lambda _name, _event: None)
            finally:
                await second.aclose()
            posts = [
                item["json"] for item in server.requests
                if item["method"] == "POST"
            ]
            restored = posts[-1]["messages"][-1]["content"]
            assert "安全边界持久化的既有对话摘要" in restored
            assert "RESTORE_OLD_B" in restored
            assert "RESTORE_NEW" in restored
            assert "RESTORE_OLD_A" not in restored

    asyncio.run(run())
    print("ok  自动边界压缩、checkpoint 持久化与 fresh runtime 恢复")


def test_automatic_unknown_terminal_keeps_context_and_cursor() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="myagents-context-auto-failure-",
        ) as raw, FakeOpenAICompatibleServer() as server:
            workdir = Path(raw) / "workspace"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=Path(raw) / "state")

            def factory(_selection):
                return NativeAgentRuntime(
                    OpenAICompatibleProvider(server.base_url),
                    model_id="fake-model",
                    name="host",
                    system_prompt="tool-less fixture",
                )

            orch = Orchestrator(
                str(workdir), specs=(), store=store,
                host_probe=_ready_host, host_model_factory=factory,
                context_policy=ContextPolicy(
                    auto_compact=True,
                    trigger_characters=1,
                    retain_messages=2,
                    summary_characters=400,
                    source_characters=4_000,
                ),
            )
            try:
                await orch.dispatch(
                    "@host CONTEXT_SUMMARY_PARTIAL_EOF old fact",
                    lambda _name, _event: None,
                )
                await orch.dispatch(
                    "@host second old fact",
                    lambda _name, _event: None,
                )
                runtime = orch.host.adapter
                before_context = runtime.context_snapshot()
                before_cursor = store.get_agent_state("host")["cursor"]
                outcome = await orch.dispatch(
                    "@host SHOULD_NOT_REACH_PROVIDER",
                    lambda _name, _event: None,
                )
                assert outcome.failures
                assert runtime.context_snapshot() == before_context
                assert store.get_agent_state("host")["cursor"] == before_cursor
                posts = [
                    item["json"] for item in server.requests
                    if item["method"] == "POST"
                ]
                assert "MYAGENTS_CONTEXT_COMPACTION_V1" \
                    in posts[-1]["messages"][-1]["content"]
                assert "SHOULD_NOT_REACH_PROVIDER" not in \
                    posts[-1]["messages"][-1]["content"]
            finally:
                await orch.aclose()

    asyncio.run(run())
    print("ok  自动摘要断流保持 runtime context 与 durable cursor")


def test_unsupported_adapter_and_checkpoint_failure_are_fail_closed() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="myagents-context-fail-closed-",
        ) as raw:
            workdir = Path(raw) / "workspace"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=Path(raw) / "state")
            compactable = _CompactableFixture()
            unsupported = _StatefulUnsupported()
            specs = (AgentSpec(
                "worker", "acp", lambda: unsupported),)
            orch = Orchestrator(
                str(workdir), specs=specs, store=store,
                host_probe=_ready_host,
                host_model_factory=lambda _selection: compactable,
            )
            try:
                statuses = {
                    item.name: item for item in orch.context_status_snapshot()
                }
                assert statuses["host"].compactable is True
                assert statuses["worker"].state == "由 transport 管理"
                try:
                    await orch.compact_context("worker")
                except ContextLifecycleError as exc:
                    assert "未声明" in str(exc)
                else:
                    raise AssertionError("unsupported transport must block")

                def fail_checkpoint(_name, _checkpoint):
                    raise OSError("fixture disk failure")

                store.set_context_checkpoint = fail_checkpoint
                try:
                    await orch.compact_context("host")
                except ContextLifecycleError as exc:
                    assert "fail-closed" in str(exc)
                else:
                    raise AssertionError("checkpoint failure must block room")
                assert orch._closed is True
            finally:
                await orch.aclose()

    asyncio.run(run())
    print("ok  未获证 transport 与 checkpoint 失败均 fail-closed")


def test_broken_context_capability_is_safe_and_fail_closed() -> None:
    async def run() -> None:
        broken = _BrokenSnapshot()
        orch = Orchestrator(
            ".", specs=(), persistent=False,
            host_probe=_ready_host,
            host_model_factory=lambda _selection: broken,
        )
        try:
            status = orch.context_status_snapshot()[0]
            assert status.state == "状态异常"
            assert status.compactable is False
            assert "fixture secret" not in status.detail
            try:
                await orch.compact_context("host")
            except ContextLifecycleError as exc:
                assert "状态读取失败" in str(exc)
                assert "fixture secret" not in str(exc)
            else:
                raise AssertionError("broken context capability must block")
        finally:
            await orch.aclose()

    asyncio.run(run())
    print("ok  context capability 异常不泄露细节且 fail-closed")


def test_store_validates_checkpoint_and_host_switch_clears_it() -> None:
    with tempfile.TemporaryDirectory(
        prefix="myagents-context-store-",
    ) as raw:
        workdir = Path(raw) / "workspace"
        workdir.mkdir()
        root = Path(raw) / "state"
        store = RoomStore(workdir, state_root=root)
        checkpoint = ContextCheckpoint.create(
            boundary_seq=0,
            summary="private summary",
            generation=1,
            source_messages=6,
            retained_messages=4,
        )
        store.set_context_checkpoint("host", checkpoint)
        assert store.get_context_checkpoints()["host"] == checkpoint
        store.set_host_backend(store.get_host_backend(), cursor=0)
        assert store.get_context_checkpoints() == {}

        data = json.loads(store.state_path.read_text(encoding="utf-8"))
        data["context_checkpoints"] = {
            "host": {**checkpoint.to_state(), "boundary_seq": -1},
        }
        store.state_path.write_text(
            json.dumps(data), encoding="utf-8")
        try:
            RoomStore(workdir, state_root=root)
        except CorruptedStorageError:
            pass
        else:
            raise AssertionError("corrupted checkpoint must fail loudly")
    print("ok  checkpoint roundtrip、Host 切换清理与损坏拒绝")


def test_context_tui_commands_are_local_and_do_not_touch_timeline() -> None:
    async def run() -> None:
        compactable = _CompactableFixture()
        unsupported = _StatefulUnsupported()
        orch = Orchestrator(
            ".",
            specs=(AgentSpec(
                "worker", "acp", lambda: unsupported),),
            persistent=False,
            host_probe=_ready_host,
            host_model_factory=lambda _selection: compactable,
        )
        app = ChatApp(workdir=".", orchestrator=orch)
        async with app.run_test() as pilot:
            box = app.query_one("#composer", ComposerInput)
            for command in (
                "/context", "/compact @host", "/compact @worker",
            ):
                box.value = command
                box.cursor_position = len(command)
                await pilot.press("enter")
                await pilot.pause()
            assert orch.history == []
            assert compactable.compactions == 1
            app.bus.has_pending = lambda: True
            box.value = "/compact @host"
            box.cursor_position = len(box.value)
            await pilot.press("enter")
            await pilot.pause()
            assert compactable.compactions == 1
            rendered = "\n".join(
                str(line.text) for line in app.query_one(RichLog).lines)
            assert "当前会话上下文" in rendered
            assert "完整聊天时间线未删除" in rendered
            assert "未声明已验收的安全压缩能力" in rendered
            assert "当前会话有任务正在运行或排队" in rendered

    asyncio.run(run())
    print("ok  /context 与 /compact 为本地命令且 TUI 反馈完整")


if __name__ == "__main__":
    test_context_policy_and_command_boundary()
    test_native_runtime_compacts_only_after_authoritative_summary()
    test_failed_summary_keeps_original_runtime_context()
    test_oversize_summary_source_never_claims_full_boundary()
    test_checkpoint_restore_and_automatic_boundary_compaction()
    test_automatic_unknown_terminal_keeps_context_and_cursor()
    test_unsupported_adapter_and_checkpoint_failure_are_fail_closed()
    test_broken_context_capability_is_safe_and_fail_closed()
    test_store_validates_checkpoint_and_host_switch_clears_it()
    test_context_tui_commands_are_local_and_do_not_touch_timeline()
    print("\nContext lifecycle 全部通过")

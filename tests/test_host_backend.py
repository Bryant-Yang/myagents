"""Room-scoped HostBackend selection and provider capability contracts."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (
    AgentDeliveryUncertainError,
    AgentEvent,
    ExecutionMode,
)
from agent_readiness import (
    AgentReadiness,
    AgentUnavailableError,
    ReadinessState,
)
from control import CommandBus
from host_backend import (
    HostBackendSelection,
    HostBackendValidationError,
    parse_host_command,
)
from host import HostAgent
from native_agent import (
    ModelProviderError,
    OpenAICompatibleProvider,
    create_native_host_runtime,
    load_native_model_catalog,
    resolve_native_model_config,
)
from orchestrator import AgentSpec, Orchestrator
from storage.store import RoomStore
from tests.fake_openai_compatible_server import FakeOpenAICompatibleServer


class RecordingAdapter:
    name = "recording"
    session_id = None

    def __init__(self, label: str) -> None:
        self.label = label
        self.modes: list[ExecutionMode] = []
        self.closed = 0
        self.permission_handlers: list[object] = []
        self.attachment_roots: list[object] = []

    async def stream(self, _prompt, _workdir, *,
                     execution_mode=ExecutionMode.DEFAULT):
        self.modes.append(execution_mode)
        yield AgentEvent("text", f"{self.label}-reply")
        yield AgentEvent("done")

    def set_permission_handler(self, handler) -> None:
        self.permission_handlers.append(handler)

    def set_attachment_root(self, root) -> None:
        self.attachment_roots.append(root)

    async def aclose(self) -> None:
        self.closed += 1


class PreparedRecordingAdapter(RecordingAdapter):
    stateful_session = True
    replay_history_on_fresh_session = True

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.session_id = f"{label}-session"
        self.prompts: list[str] = []

    async def stream_prepared(
        self,
        make_prompt,
        _workdir,
        resume_session_id=None,
        *,
        execution_mode=ExecutionMode.DEFAULT,
    ):
        self.modes.append(execution_mode)
        prompt = make_prompt(SimpleNamespace(
            session_id=self.session_id,
            restored=resume_session_id == self.session_id,
            load_failed=(
                resume_session_id is not None
                and resume_session_id != self.session_id
            ),
            fresh=True,
        ))
        self.prompts.append(prompt)
        yield AgentEvent("delivery_committed")
        yield AgentEvent("text", f"{self.label}-reply")
        yield AgentEvent("done")


class CloseFailAdapter(RecordingAdapter):
    async def aclose(self) -> None:
        self.closed += 1
        raise RuntimeError("fake close failed")


class UncertainPreparedAdapter(PreparedRecordingAdapter):
    async def stream_prepared(
        self,
        make_prompt,
        _workdir,
        resume_session_id=None,
        *,
        execution_mode=ExecutionMode.DEFAULT,
    ):
        self.modes.append(execution_mode)
        prompt = make_prompt(SimpleNamespace(
            session_id=self.session_id,
            restored=resume_session_id == self.session_id,
            load_failed=False,
            fresh=True,
        ))
        self.prompts.append(prompt)
        yield AgentEvent("delivery_committed")
        raise AgentDeliveryUncertainError("fake uncertain host turn")


class InterjectingPreparedAdapter(PreparedRecordingAdapter):
    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.interjections: list[str] = []

    async def stream_prepared(
        self,
        make_prompt,
        _workdir,
        resume_session_id=None,
        *,
        execution_mode=ExecutionMode.DEFAULT,
    ):
        self.modes.append(execution_mode)
        self.prompts.append(make_prompt(SimpleNamespace(
            session_id=self.session_id,
            restored=False,
            load_failed=False,
            fresh=True,
        )))
        yield AgentEvent("delivery_committed")
        self.started.set()
        await self.release.wait()
        yield AgentEvent("text", f"{self.label}-reply")
        yield AgentEvent("done")

    async def interject(self, instruction: str) -> None:
        self.interjections.append(instruction)


def ready(name: str):
    return lambda: AgentReadiness(
        name, ReadinessState.READY, "fake ready", "none")


def unavailable(name: str):
    return lambda: AgentReadiness(
        name, ReadinessState.NOT_FOUND, "fake missing", "install fake")


def test_selection_and_command_validation() -> None:
    assert parse_host_command("/host").action == "show"
    command = parse_host_command("/host model glm")
    assert (command.kind, command.target) == ("model", "glm")
    assert parse_host_command("/help") is None
    assert parse_host_command("/hostile") is None
    assert HostBackendSelection.from_state(
        HostBackendSelection.agent("codex").to_state()
    ) == HostBackendSelection.agent("codex")
    try:
        parse_host_command("/host agent")
    except HostBackendValidationError:
        pass
    else:
        raise AssertionError("incomplete /host command must be local error")


def test_agent_host_projects_native_interjection_capability() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            store = RoomStore(workdir, Path(tmp) / "state")
            store.set_host_backend(
                HostBackendSelection.agent("codex"), cursor=0)
            host_adapter = InterjectingPreparedAdapter("agent-host")
            spec = AgentSpec(
                "codex",
                "app-server",
                lambda: RecordingAdapter("worker"),
                ready("codex"),
                lambda: host_adapter,
                ready("codex"),
            )
            orch = Orchestrator(
                str(workdir),
                specs=(spec,),
                store=store,
                discover_agents=True,
            )
            bus = CommandBus(orch)
            bus.start()
            active = await bus.submit("@host 执行长任务")
            await asyncio.wait_for(host_adapter.started.wait(), timeout=2)
            queued = await bus.submit("先回答最关键的结论")
            try:
                receipt = await bus.interject_next(active.command_id)
                assert receipt["source_command_id"] == queued.command_id
                assert receipt["agent"] == "host"
                assert host_adapter.interjections == ["先回答最关键的结论"]
            finally:
                host_adapter.release.set()
                await bus.wait(active.command_id, timeout=5)
                await bus.aclose()
                await orch.aclose()

    asyncio.run(run())


def test_host_interjection_capability_remains_fail_closed() -> None:
    model_host = HostAgent(
        InterjectingPreparedAdapter("model"), backend_kind="model")
    unsupported_agent_host = HostAgent(
        PreparedRecordingAdapter("unsupported"), backend_kind="agent")
    assert getattr(model_host, "interject", None) is None
    assert getattr(unsupported_agent_host, "interject", None) is None
    asyncio.run(model_host.aclose())
    asyncio.run(unsupported_agent_host.aclose())


def test_room_persistence_isolated_and_sets_cross_backend_boundary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / "work"
        workdir.mkdir()
        state_root = Path(tmp) / "state"
        first = RoomStore(workdir, state_root, session_name="first")
        second = RoomStore(workdir, state_root, session_name="second")
        first.set_agent_state("host", cursor=9, session_id="old-host")
        first.set_agent_state("codex", cursor=7, session_id="worker-thread")
        first.set_host_backend(
            HostBackendSelection.agent("codex"), cursor=9)
        assert first.get_host_backend() == HostBackendSelection.agent("codex")
        assert first.get_agent_state("host") == {
            "cursor": 9, "session_id": None}
        assert first.get_agent_state("codex") == {
            "cursor": 7, "session_id": "worker-thread"}
        assert second.get_host_backend() == HostBackendSelection.default()
        reopened = RoomStore(workdir, state_root, session_name="first")
        assert reopened.get_host_backend() == HostBackendSelection.agent(
            "codex")


def test_named_glm_profile_without_model_discovery() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                FakeOpenAICompatibleServer(
                    ("glm-5.3-flash",), models_discovery=False) as server:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[host.models.glm]\n"
                "provider = \"openai-compatible\"\n"
                f"base_url = \"{server.base_url}\"\n"
                "model_id = \"glm-5.3-flash\"\n"
                "api_key_env = \"ZAI_API_KEY\"\n"
                "models_discovery = false\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            environ = {"ZAI_API_KEY": "private-test-token"}
            config = resolve_native_model_config(
                "glm", reference="profile", environ=environ,
                config_path=path)
            assert config.model_id == "glm-5.3-flash"
            assert config.models_discovery is False
            assert "private-test-token" not in repr(config)
            runtime = create_native_host_runtime(
                environ=environ,
                config_path=path,
                target="glm",
                reference="profile",
            )
            try:
                events = [event async for event in runtime.stream(
                    "short answer", str(ROOT))]
            finally:
                await runtime.aclose()
            assert any(event.kind == "delivery_committed" for event in events)
            assert not any(item["method"] == "GET" for item in server.requests)
            post = next(item for item in server.requests
                        if item["method"] == "POST")
            assert post["json"]["model"] == "glm-5.3-flash"
            assert post["authorization"] == "Bearer private-test-token"

        with FakeOpenAICompatibleServer(
                ("accepted",), models_discovery=False) as server:
            provider = OpenAICompatibleProvider(
                server.base_url, models_discovery=False)
            try:
                try:
                    _ = [event async for event in provider.stream(
                        [], model_id="rejected-exact-id")]
                except ModelProviderError as exc:
                    assert "HTTP 400" in str(exc)
                else:
                    raise AssertionError("service must reject unknown exact id")
            finally:
                await provider.aclose()
            assert [item["method"] for item in server.requests] == ["POST"]

    asyncio.run(run())


def test_unselected_profile_does_not_require_its_secret() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text(
            "[host.model]\n"
            "model_id = \"local-model\"\n"
            "[host.models.remote]\n"
            "base_url = \"https://example.invalid/v1\"\n"
            "model_id = \"remote-model\"\n"
            "api_key_env = \"REMOTE_API_KEY\"\n"
            "models_discovery = false\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        catalog = load_native_model_catalog({}, config_path=path)
        assert catalog.require_profile("default").model_id == "local-model"
        try:
            catalog.require_profile("remote")
        except Exception as exc:
            assert "REMOTE_API_KEY 未设置" in str(exc)
        else:
            raise AssertionError("selected credentialed profile must require key")


def test_agent_host_switch_is_independent_read_only_and_fresh() -> None:
    async def run() -> None:
        worker_instances: list[RecordingAdapter] = []
        host_instances: list[RecordingAdapter] = []
        model_instances: list[RecordingAdapter] = []

        def worker_factory():
            item = RecordingAdapter("worker")
            worker_instances.append(item)
            return item

        def host_factory():
            item = RecordingAdapter("agent-host")
            host_instances.append(item)
            return item

        def model_factory(selection):
            item = RecordingAdapter(f"model-{selection.target}")
            model_instances.append(item)
            return item

        specs = (AgentSpec(
            "codex", "app-server", worker_factory, ready("codex"),
            host_factory, ready("codex")),)
        orch = Orchestrator(
            str(ROOT), specs=specs, persistent=False,
            host_model_factory=model_factory)
        old_model = orch.host.adapter
        try:
            await orch.switch_host_backend("agent", "codex")
            assert old_model.closed == 1
            assert orch.host.adapter is host_instances[-1]
            assert orch.adapters["codex"] is worker_instances[0]
            assert orch.host.adapter is not orch.adapters["codex"]

            async def allow(*_args):
                return {"outcome": "selected", "optionId": "allow_once"}

            orch.set_permission_handler(allow)
            events = []
            await orch.dispatch("@host summarize", lambda *item: events.append(item))
            assert host_instances[-1].modes == [ExecutionMode.READ_ONLY]
            assert host_instances[-1].permission_handlers == []
            assert host_instances[-1].attachment_roots == []
            assert worker_instances[0].permission_handlers == [allow]
            assert worker_instances[0].attachment_roots == [None]

            first_agent_host = host_instances[-1]
            await orch.switch_host_backend("model", "other-model")
            assert first_agent_host.closed == 1
            assert orch.host.adapter is model_instances[-1]
            assert orch.host.adapter is not old_model
            assert orch.host_backend_selection == \
                HostBackendSelection.exact_model("other-model")
        finally:
            await orch.aclose()

    asyncio.run(run())


def test_switch_boundary_survives_restart_without_cross_backend_replay() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            state_root = Path(tmp) / "state"
            created_hosts: list[PreparedRecordingAdapter] = []

            def host_factory():
                adapter = PreparedRecordingAdapter(
                    f"agent-host-{len(created_hosts) + 1}")
                created_hosts.append(adapter)
                return adapter

            specs = (AgentSpec(
                "codex",
                "app-server",
                lambda: RecordingAdapter("worker"),
                ready("codex"),
                host_factory,
                ready("codex"),
            ),)
            first = Orchestrator(
                str(workdir),
                specs=specs,
                store=RoomStore(workdir, state_root),
                host_model_factory=lambda _selection:
                    PreparedRecordingAdapter("model-host"),
            )
            try:
                await first.dispatch("@codex OLD_BACKEND_TURN", lambda *_: None)
                await first.switch_host_backend("agent", "codex")
                boundary = first.history[-1].seq
                assert first.store is not None
                assert first.store.get_agent_state("host") == {
                    "cursor": boundary,
                    "session_id": None,
                }
                messages, upto = first._messages_for(
                    "host", max_seq=boundary)
                assert messages == []
                assert upto == boundary
            finally:
                await first.aclose()

            reopened = Orchestrator(
                str(workdir),
                specs=specs,
                store=RoomStore(workdir, state_root),
                host_model_factory=lambda _selection:
                    PreparedRecordingAdapter("unused-model-host"),
            )
            try:
                await reopened.dispatch("@host NEW_BACKEND_TURN", lambda *_: None)
                prompt = created_hosts[-1].prompts[-1]
                assert "NEW_BACKEND_TURN" in prompt
                assert "OLD_BACKEND_TURN" not in prompt
                assert created_hosts[-1].attachment_roots == []
            finally:
                await reopened.aclose()

            restarted_again = Orchestrator(
                str(workdir),
                specs=specs,
                store=RoomStore(workdir, state_root),
                host_model_factory=lambda _selection:
                    PreparedRecordingAdapter("unused-model-host"),
            )
            try:
                await restarted_again.dispatch(
                    "@host SECOND_NEW_BACKEND_TURN", lambda *_: None)
                prompt = created_hosts[-1].prompts[-1]
                assert "SECOND_NEW_BACKEND_TURN" in prompt
                assert "NEW_BACKEND_TURN" in prompt
                assert "OLD_BACKEND_TURN" not in prompt
            finally:
                await restarted_again.aclose()

    asyncio.run(run())


def test_uncertain_host_turn_advances_persistent_replay_floor() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            state_root = Path(tmp) / "state"
            first = Orchestrator(
                str(workdir),
                specs=(),
                store=RoomStore(workdir, state_root),
                host_model_factory=lambda _selection:
                    UncertainPreparedAdapter("uncertain-host"),
            )
            try:
                outcome = await first.dispatch(
                    "@host UNCERTAIN_HOST_TURN", lambda *_: None)
                assert outcome.failures
                assert first.store is not None
                assert first.store.get_agent_state("host")["cursor"] == 1
                assert first.store.get_host_replay_floor() == 1
            finally:
                await first.aclose()

            recovered_hosts: list[PreparedRecordingAdapter] = []

            def recovered_factory(_selection):
                adapter = PreparedRecordingAdapter("recovered-host")
                recovered_hosts.append(adapter)
                return adapter

            reopened = Orchestrator(
                str(workdir),
                specs=(),
                store=RoomStore(workdir, state_root),
                host_model_factory=recovered_factory,
            )
            try:
                await reopened.dispatch(
                    "@host AFTER_UNCERTAIN_RESTART", lambda *_: None)
                prompt = recovered_hosts[-1].prompts[-1]
                assert "AFTER_UNCERTAIN_RESTART" in prompt
                assert "UNCERTAIN_HOST_TURN" not in prompt
            finally:
                await reopened.aclose()

    asyncio.run(run())


def test_running_unknown_and_unready_switches_are_blocked_without_fallback() -> None:
    async def run() -> None:
        candidates: list[RecordingAdapter] = []

        def candidate_factory():
            item = RecordingAdapter("candidate")
            candidates.append(item)
            return item

        specs = (
            AgentSpec(
                "codex", "app-server", lambda: RecordingAdapter("worker"),
                ready("codex"), candidate_factory, unavailable("codex")),
        )
        orch = Orchestrator(
            str(ROOT), specs=specs, persistent=False,
            discover_agents=True,
            host_probe=ready("host"),
            host_model_factory=lambda _selection: RecordingAdapter("model"),
        )
        try:
            original = orch.host
            try:
                await orch.switch_host_backend("agent", "unknown")
            except AgentUnavailableError:
                pass
            else:
                raise AssertionError("unknown agent host must be blocked")
            try:
                await orch.switch_host_backend("agent", "codex")
            except AgentUnavailableError:
                pass
            else:
                raise AssertionError("unready agent host must be blocked")
            assert orch.host is original
            assert orch.host_backend_selection == HostBackendSelection.default()
            assert candidates == []

            lock = orch._delivery_lock("host")
            await lock.acquire()
            try:
                try:
                    await orch.switch_host_backend("model", "fresh-model")
                except RuntimeError as exc:
                    assert "正在运行" in str(exc)
                else:
                    raise AssertionError("running host switch must be rejected")
            finally:
                lock.release()
            assert orch.host is original
        finally:
            await orch.aclose()

    asyncio.run(run())


def test_accepted_dispatch_blocks_switch_before_host_lock() -> None:
    async def run() -> None:
        hosts: list[PreparedRecordingAdapter] = []

        def model_factory(selection):
            adapter = PreparedRecordingAdapter(
                f"model-{selection.target}-{len(hosts) + 1}")
            hosts.append(adapter)
            return adapter

        orch = Orchestrator(
            str(ROOT),
            specs=(),
            persistent=False,
            host_model_factory=model_factory,
        )
        queued = asyncio.Event()
        release = asyncio.Event()
        original_run_one = orch._run_one

        async def delayed_run_one(*args, **kwargs):
            queued.set()
            await release.wait()
            return await original_run_one(*args, **kwargs)

        orch._run_one = delayed_run_one
        task = asyncio.create_task(
            orch.dispatch("@host QUEUED_HOST_TURN", lambda *_: None))
        try:
            await asyncio.wait_for(queued.wait(), timeout=1)
            assert not orch._delivery_lock("host").locked()
            try:
                await orch.switch_host_backend("model", "new-model")
            except RuntimeError as exc:
                assert "运行或已进入派发" in str(exc)
            else:
                raise AssertionError("accepted dispatch must block Host switch")
            assert orch.host_backend_selection == HostBackendSelection.default()
            release.set()
            await task
            assert "QUEUED_HOST_TURN" in hosts[0].prompts[-1]
            assert len(hosts) == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await orch.aclose()

    asyncio.run(run())


def test_dispatch_waiting_behind_switch_uses_new_host() -> None:
    async def run() -> None:
        stateful_checked = asyncio.Event()

        class CheckedPreparedAdapter(PreparedRecordingAdapter):
            @property
            def stateful_session(self):
                stateful_checked.set()
                return True

        hosts: list[PreparedRecordingAdapter] = []

        def model_factory(selection):
            adapter: PreparedRecordingAdapter
            if not hosts:
                adapter = CheckedPreparedAdapter("old-model")
            else:
                adapter = PreparedRecordingAdapter(
                    f"model-{selection.target}-{len(hosts) + 1}")
            hosts.append(adapter)
            return adapter

        orch = Orchestrator(
            str(ROOT),
            specs=(),
            persistent=False,
            host_model_factory=model_factory,
        )
        closing = asyncio.Event()
        release_close = asyncio.Event()
        old_adapter = hosts[0]
        original_close = old_adapter.aclose

        async def blocked_close():
            closing.set()
            await release_close.wait()
            await original_close()

        old_adapter.aclose = blocked_close
        switch_task = asyncio.create_task(
            orch.switch_host_backend("model", "new-model"))
        dispatch_task = None
        try:
            await asyncio.wait_for(closing.wait(), timeout=1)
            stateful_checked.clear()
            dispatch_task = asyncio.create_task(
                orch.dispatch("@host RACE_TURN", lambda *_: None))
            await asyncio.wait_for(stateful_checked.wait(), timeout=1)
            release_close.set()
            await switch_task
            await dispatch_task
            assert orch.host_backend_selection == \
                HostBackendSelection.exact_model("new-model")
            assert old_adapter.prompts == []
            assert "RACE_TURN" in hosts[-1].prompts[-1]
        finally:
            release_close.set()
            for task in (switch_task, dispatch_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (switch_task, dispatch_task)
                  if task is not None),
                return_exceptions=True,
            )
            await orch.aclose()

    asyncio.run(run())


def test_plain_dispatch_waiting_behind_switch_uses_new_host() -> None:
    async def run() -> None:
        hosts: list[PreparedRecordingAdapter] = []

        def model_factory(selection):
            adapter = PreparedRecordingAdapter(
                f"model-{selection.target}-{len(hosts) + 1}")
            hosts.append(adapter)
            return adapter

        orch = Orchestrator(
            str(ROOT),
            specs=(),
            persistent=False,
            host_model_factory=model_factory,
        )
        waiter_started = asyncio.Event()

        class ObservedLock:
            def __init__(self):
                self.inner = asyncio.Lock()

            def locked(self):
                return self.inner.locked()

            async def acquire(self):
                if self.inner.locked():
                    waiter_started.set()
                return await self.inner.acquire()

            def release(self):
                self.inner.release()

            async def __aenter__(self):
                await self.acquire()
                return self

            async def __aexit__(self, _exc_type, _exc, _tb):
                self.release()

        lock = ObservedLock()
        orch._delivery_locks["host"] = lock
        closing = asyncio.Event()
        release_close = asyncio.Event()
        old_adapter = hosts[0]
        original_close = old_adapter.aclose

        async def blocked_close():
            closing.set()
            await release_close.wait()
            await original_close()

        old_adapter.aclose = blocked_close
        switch_task = asyncio.create_task(
            orch.switch_host_backend("model", "new-model"))
        dispatch_task = None
        try:
            await asyncio.wait_for(closing.wait(), timeout=1)
            dispatch_task = asyncio.create_task(
                orch.dispatch("PLAIN_RACE_TURN", lambda *_: None))
            await asyncio.wait_for(waiter_started.wait(), timeout=1)
            release_close.set()
            await switch_task
            await dispatch_task
            assert orch.host_backend_selection == \
                HostBackendSelection.exact_model("new-model")
            assert old_adapter.prompts == []
            assert "PLAIN_RACE_TURN" in hosts[-1].prompts[-1]
        finally:
            release_close.set()
            for task in (switch_task, dispatch_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (switch_task, dispatch_task)
                  if task is not None),
                return_exceptions=True,
            )
            await orch.aclose()

    asyncio.run(run())


def test_close_failure_stays_invalid_after_rescan() -> None:
    async def run() -> None:
        created: list[RecordingAdapter] = []

        def model_factory(selection):
            adapter: RecordingAdapter
            if selection.target == "default":
                adapter = CloseFailAdapter("old-host")
            else:
                adapter = RecordingAdapter("candidate")
            created.append(adapter)
            return adapter

        orch = Orchestrator(
            str(ROOT),
            specs=(),
            persistent=False,
            discover_agents=True,
            host_probe=ready("host"),
            host_model_factory=model_factory,
        )
        old_host = orch.host
        try:
            try:
                await orch.switch_host_backend("model", "new-model")
            except RuntimeError as exc:
                assert "fake close failed" in str(exc)
            else:
                raise AssertionError("old Host close failure must abort switch")
            assert orch.host is old_host
            assert created[-1].label == "candidate"
            assert created[-1].closed == 1
            assert not orch.host_backend_status().readiness.ready

            rescanned = orch.refresh_agent_readiness()
            host_status = next(item for item in rescanned if item.name == "host")
            assert not host_status.ready
            assert "关闭失败" in host_status.detail
            try:
                await orch.switch_host_backend("model", "another-model")
            except RuntimeError as exc:
                assert "避免双 writer" in str(exc)
            else:
                raise AssertionError("poisoned Host must reject later switches")
        finally:
            await orch.aclose()

    asyncio.run(run())


def test_persisted_unready_agent_host_does_not_fallback_to_model() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            store = RoomStore(workdir, Path(tmp) / "state")
            store.append("user", "old user turn")
            store.append("codex", "old worker reply")
            store.set_host_backend(
                HostBackendSelection.agent("codex"), cursor=2)
            model_created: list[str] = []
            specs = (AgentSpec(
                "codex", "app-server", lambda: RecordingAdapter("worker"),
                ready("codex"), lambda: RecordingAdapter("agent-host"),
                unavailable("codex")),)
            orch = Orchestrator(
                str(workdir), specs=specs, store=store,
                discover_agents=True,
                host_model_factory=lambda _selection: (
                    model_created.append("model") or RecordingAdapter("model")
                ),
            )
            try:
                assert orch.host_backend_selection == \
                    HostBackendSelection.agent("codex")
                assert not orch.host_backend_status().readiness.ready
                assert orch._cursors["host"] == 2
                assert model_created == []
                try:
                    orch.require_message_agents("hello")
                except AgentUnavailableError:
                    pass
                else:
                    raise AssertionError("selected unready host must block")
            finally:
                await orch.aclose()

    asyncio.run(run())


if __name__ == "__main__":
    test_selection_and_command_validation()
    test_agent_host_projects_native_interjection_capability()
    test_host_interjection_capability_remains_fail_closed()
    test_room_persistence_isolated_and_sets_cross_backend_boundary()
    test_named_glm_profile_without_model_discovery()
    test_unselected_profile_does_not_require_its_secret()
    test_agent_host_switch_is_independent_read_only_and_fresh()
    test_switch_boundary_survives_restart_without_cross_backend_replay()
    test_uncertain_host_turn_advances_persistent_replay_floor()
    test_running_unknown_and_unready_switches_are_blocked_without_fallback()
    test_accepted_dispatch_blocks_switch_before_host_lock()
    test_dispatch_waiting_behind_switch_uses_new_host()
    test_plain_dispatch_waiting_behind_switch_uses_new_host()
    test_close_failure_stays_invalid_after_rescan()
    test_persisted_unready_agent_host_does_not_fallback_to_model()
    print("ok  HostBackend model/agent 切换、持久化、权限与 provider capability")

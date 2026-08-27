"""Native model-backed host/runtime contract tests.

The production network seam is exercised through a local fake
OpenAI-compatible server; no real agent CLI or model service is used here.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_readiness import AgentReadiness, AgentUnavailableError, ReadinessState
from adapters.base import (
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    AgentEvent,
)
from host import HostAgent
from native_agent import (
    ModelEvent,
    ModelMessage,
    ModelProviderError,
    NativeAgentRuntime,
    NativeModelConfig,
    OpenAICompatibleConfigResolver,
    OpenAICompatibleProvider,
    create_native_host_runtime,
    native_host_readiness_probe,
    native_model_config_path,
)
from orchestrator import AgentSpec, Orchestrator
from tests.fake_openai_compatible_server import FakeOpenAICompatibleServer


class _Worker:
    def __init__(self, name: str) -> None:
        self.name = name
        self.session_id = None
        self.prompts: list[str] = []

    async def stream(self, prompt: str, _workdir: str):
        self.prompts.append(prompt)
        yield AgentEvent("text", f"{self.name}-answer")
        yield AgentEvent("done")


class _BareDoneProvider:
    async def list_models(self):
        return ("fake-model",)

    async def stream(self, _messages, *, model_id):
        assert model_id == "fake-model"
        yield ModelEvent("committed")
        yield ModelEvent("text", "untrusted")
        yield ModelEvent("done")

    async def aclose(self):
        return None


def _ready_host() -> AgentReadiness:
    return AgentReadiness(
        "host", ReadinessState.READY, "fixture native model", "none")


def test_native_host_readiness_requires_explicit_model_configuration() -> None:
    missing = native_host_readiness_probe(environ={})
    assert missing.state is ReadinessState.NOT_FOUND
    assert "未配置原生 host 模型" in missing.detail
    assert ".config/myagents/config.toml" in missing.setup_hint
    assert "MYAGENTS_MODEL_*" in missing.setup_hint
    assert "/v1/models" in missing.setup_hint

    invalid = native_host_readiness_probe(environ={
        "MYAGENTS_MODEL_ID": "fake-model",
        "MYAGENTS_MODEL_BASE_URL": "file:///tmp/not-http",
    })
    assert invalid.state is ReadinessState.INVALID
    assert "http:// 或 https://" in invalid.detail

    bad_secret = native_host_readiness_probe(environ={
        "MYAGENTS_MODEL_ID": "fake-model",
        "MYAGENTS_MODEL_API_KEY": "secret\r\ninjected: true",
    })
    assert bad_secret.state is ReadinessState.INVALID
    assert "API key" in bad_secret.detail
    assert "secret" not in repr(bad_secret)

    ready = native_host_readiness_probe(environ={
        "MYAGENTS_MODEL_ID": "fake-model",
        "MYAGENTS_MODEL_BASE_URL": "http://127.0.0.1:1234/v1",
        "MYAGENTS_MODEL_API_KEY": "must-not-appear",
    })
    assert ready.state is ReadinessState.READY
    assert "fake-model" in ready.detail
    assert "OpenAI-compatible" in ready.detail
    assert "must-not-appear" not in repr(ready)

    oversized = native_host_readiness_probe(environ={
        "MYAGENTS_MODEL_PROVIDER": "p" * 10000,
        "MYAGENTS_MODEL_ID": "m" * 10000,
    })
    assert oversized.state is ReadinessState.INVALID
    assert len(oversized.detail) < 300
    assert "p" * 1000 not in repr(oversized)


def test_native_host_uses_private_xdg_config_with_environment_overrides() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="myagents-native-config-",
        ) as raw, FakeOpenAICompatibleServer() as server:
            config_dir = Path(raw) / "myagents"
            config_dir.mkdir(mode=0o700)
            config_path = config_dir / "config.toml"
            config_path.write_text(
                "[host.model]\n"
                'provider = "openai-compatible"\n'
                f'base_url = "{server.base_url}"\n'
                'model_id = "fake-model"\n'
                'api_key = "file-secret"\n',
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            environ = {"XDG_CONFIG_HOME": raw}

            ready = native_host_readiness_probe(environ=environ)
            assert ready.state is ReadinessState.READY
            assert "fake-model" in ready.detail
            assert "配置文件" in ready.detail

            loaded = NativeModelConfig.from_sources(environ=environ)
            assert loaded.model_id == "fake-model"
            assert loaded.base_url == server.base_url
            assert "file-secret" not in repr(loaded)

            overridden = NativeModelConfig.from_sources(
                environ={
                    "XDG_CONFIG_HOME": raw,
                    "MYAGENTS_MODEL_ID": "temporary-model",
                },
            )
            assert overridden.model_id == "temporary-model"
            assert overridden.base_url == server.base_url

            runtime = create_native_host_runtime(environ=environ)
            try:
                events = [
                    event
                    async for event in runtime.stream(
                        "PROVIDER_STREAM", "/tmp")
                ]
            finally:
                await runtime.aclose()
            assert "".join(
                event.text for event in events if event.kind == "text"
            ) == "hello native"
            post = next(
                request for request in server.requests
                if request["method"] == "POST"
            )
            assert post["authorization"] == "Bearer file-secret"

    asyncio.run(run())


def test_native_host_rejects_unsafe_or_broken_config_file() -> None:
    with tempfile.TemporaryDirectory(
        prefix="myagents-native-bad-config-",
    ) as raw:
        config_dir = Path(raw) / "myagents"
        config_dir.mkdir(mode=0o700)
        config_path = config_dir / "config.toml"
        config_path.write_text(
            '[host.model]\nmodel_id = "fake-model"\n',
            encoding="utf-8",
        )
        config_path.chmod(0o644)
        insecure = native_host_readiness_probe(
            environ={"XDG_CONFIG_HOME": raw})
        assert insecure.state is ReadinessState.INVALID
        assert "0600" in insecure.detail

        config_path.chmod(0o400)
        overly_restrictive = native_host_readiness_probe(
            environ={"XDG_CONFIG_HOME": raw})
        assert overly_restrictive.state is ReadinessState.INVALID
        assert "0600" in overly_restrictive.detail

        target_path = config_dir / "target.toml"
        target_path.write_text(
            '[host.model]\nmodel_id = "fake-model"\n',
            encoding="utf-8",
        )
        target_path.chmod(0o600)
        config_path.unlink()
        config_path.symlink_to(target_path)
        symlinked = native_host_readiness_probe(
            environ={"XDG_CONFIG_HOME": raw})
        assert symlinked.state is ReadinessState.INVALID
        assert "安全读取" in symlinked.detail

        config_path.unlink()
        os.mkfifo(config_path, mode=0o600)
        fifo = native_host_readiness_probe(
            environ={"XDG_CONFIG_HOME": raw})
        assert fifo.state is ReadinessState.INVALID
        assert "普通文件" in fifo.detail

        config_path.unlink()
        config_path.write_text(
            '[host.model]\napi_key = "must-not-leak\n',
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        broken = native_host_readiness_probe(
            environ={"XDG_CONFIG_HOME": raw})
        assert broken.state is ReadinessState.INVALID
        assert "TOML" in broken.detail
        assert "must-not-leak" not in repr(broken)


def test_relative_xdg_config_home_falls_back_to_user_config_home() -> None:
    with tempfile.TemporaryDirectory(
        prefix="myagents-native-relative-xdg-",
    ) as raw:
        fake_home = Path(raw) / "home"
        expected = fake_home / ".config/myagents/config.toml"
        with patch("native_agent.config.Path.home", return_value=fake_home):
            assert native_model_config_path({
                "XDG_CONFIG_HOME": "workspace-config",
            }) == expected


def test_custom_config_path_is_preserved_in_runtime_setup_hint() -> None:
    with tempfile.TemporaryDirectory(
        prefix="myagents-native-custom-config-",
    ) as raw:
        custom_path = Path(raw) / "config.toml"
        resolver = OpenAICompatibleConfigResolver(
            environ={},
            config_path=custom_path,
        )
        try:
            resolver()
        except ModelProviderError as exc:
            assert str(custom_path) in str(exc)
        else:
            raise AssertionError(
                "missing custom config must fail with its path")


def test_openai_compatible_provider_lists_exact_models_and_streams_text() -> None:
    async def run() -> None:
        with FakeOpenAICompatibleServer(("fake-model", "other-model")) as server:
            provider = OpenAICompatibleProvider(
                server.base_url,
                api_key="fixture-secret",
            )
            try:
                models = await provider.list_models()
                assert models == ("fake-model", "other-model")
                events = [
                    event
                    async for event in provider.stream(
                        (ModelMessage("user", "PROVIDER_STREAM"),),
                        model_id="fake-model",
                    )
                ]
            finally:
                await provider.aclose()

            assert [event.kind for event in events] == [
                "committed", "text", "text", "done"]
            assert "".join(
                event.text for event in events if event.kind == "text"
            ) == "hello native"
            post = next(
                item for item in server.requests if item["method"] == "POST")
            assert post["authorization"] == "Bearer fixture-secret"
            assert post["json"]["model"] == "fake-model"
            assert post["json"]["stream"] is True
            assert "tools" not in post["json"]
            assert "tool_choice" not in post["json"]

    asyncio.run(run())


def test_native_runtime_streams_and_preserves_isolated_session_context() -> None:
    async def consume(runtime: NativeAgentRuntime, prompt: str):
        return [event async for event in runtime.stream(prompt, "/tmp")]

    async def run() -> None:
        with FakeOpenAICompatibleServer() as server:
            provider = OpenAICompatibleProvider(server.base_url)
            runtime = NativeAgentRuntime(
                provider,
                model_id="fake-model",
                name="native-test",
                system_prompt="tool-less fixture",
            )
            try:
                session_id = runtime.session_id
                first = await consume(runtime, "FAKE_CONTEXT first")
                second = await consume(runtime, "FAKE_CONTEXT second")
            finally:
                await runtime.aclose()

            assert session_id
            assert runtime.session_id == session_id
            assert [event.kind for event in first] == [
                "delivery_committed", "text", "done"]
            assert [event.kind for event in second] == [
                "delivery_committed", "text", "done"]
            assert "".join(e.text for e in first if e.kind == "text") \
                == "context-1"
            assert "".join(e.text for e in second if e.kind == "text") \
                == "context-2"
            posts = [
                item["json"] for item in server.requests
                if item["method"] == "POST"
            ]
            assert [item["role"] for item in posts[1]["messages"]] == [
                "system", "user", "assistant", "user"]
            assert all("tools" not in payload for payload in posts)

    asyncio.run(run())


def test_native_runtime_sessions_are_isolated_and_environment_factory_is_lazy() -> None:
    async def text(runtime: NativeAgentRuntime, prompt: str) -> str:
        parts = [
            event.text
            async for event in runtime.stream(prompt, "/tmp")
            if event.kind == "text"
        ]
        return "".join(parts)

    async def run() -> None:
        empty_runtime = create_native_host_runtime(environ={})
        try:
            try:
                await text(empty_runtime, "must fail before HTTP")
            except ModelProviderError as exc:
                assert "MYAGENTS_MODEL_ID" in str(exc)
            else:
                raise AssertionError("missing native model config must fail")
        finally:
            await empty_runtime.aclose()

        with FakeOpenAICompatibleServer() as server:
            environ = {
                "MYAGENTS_MODEL_BASE_URL": server.base_url,
                "MYAGENTS_MODEL_ID": "fake-model",
            }
            left = create_native_host_runtime(environ=environ)
            right = create_native_host_runtime(environ=environ)
            try:
                assert left.session_id != right.session_id
                assert await text(left, "FAKE_CONTEXT left-1") == "context-1"
                assert await text(left, "FAKE_CONTEXT left-2") == "context-2"
                assert await text(right, "FAKE_CONTEXT right-1") == "context-1"

                preparations = []
                events = [
                    event
                    async for event in right.stream_prepared(
                        lambda prep: (
                            preparations.append(prep)
                            or "FAKE_CONTEXT prepared"
                        ),
                        "/tmp",
                        right.session_id,
                    )
                ]
                assert [event.kind for event in events] == [
                    "delivery_committed", "text", "done"]
                assert len(preparations) == 1
                assert preparations[0].session_id == right.session_id
                assert preparations[0].restored is True
                assert preparations[0].fresh is False
                assert right.tool_policy == "none"
            finally:
                await left.aclose()
                await right.aclose()

    asyncio.run(run())


def test_native_runtime_timeout_after_commit_is_no_replay_uncertain() -> None:
    async def run() -> None:
        with FakeOpenAICompatibleServer() as server:
            runtime = NativeAgentRuntime(
                OpenAICompatibleProvider(server.base_url),
                model_id="fake-model",
                name="native-test",
                system_prompt="tool-less fixture",
                inactivity_timeout=0.05,
            )
            events = []
            started = time.monotonic()
            try:
                try:
                    async for event in runtime.stream("FAKE_SLOW", "/tmp"):
                        events.append(event)
                except AgentDeliveryUncertainError as exc:
                    assert "0.05" in str(exc)
                else:
                    raise AssertionError("committed silent stream must time out")
            finally:
                await runtime.aclose()
            assert time.monotonic() - started < 0.2
            assert [event.kind for event in events] == ["delivery_committed"]

    asyncio.run(run())


def test_native_runtime_post_attempt_timeout_before_headers_is_uncertain() -> None:
    async def run() -> None:
        with FakeOpenAICompatibleServer() as server:
            runtime = NativeAgentRuntime(
                OpenAICompatibleProvider(server.base_url),
                model_id="fake-model",
                name="native-test",
                system_prompt="tool-less fixture",
                inactivity_timeout=0.05,
            )
            events = []
            try:
                try:
                    async for event in runtime.stream(
                        "FAKE_PRE_RESPONSE_SLOW", "/tmp"
                    ):
                        events.append(event)
                except AgentDeliveryUncertainError:
                    pass
                else:
                    raise AssertionError(
                        "POST attempt without headers is still uncertain")
            finally:
                await runtime.aclose()
            assert [event.kind for event in events] == ["delivery_committed"]

    asyncio.run(run())


def test_native_runtime_rejects_bare_done_without_terminal_proof() -> None:
    async def run() -> None:
        runtime = NativeAgentRuntime(
            _BareDoneProvider(),
            model_id="fake-model",
            name="native-test",
            system_prompt="tool-less fixture",
        )
        events = []
        try:
            try:
                async for event in runtime.stream("hello", "/tmp"):
                    events.append(event)
            except AgentDeliveryUncertainError as exc:
                assert "权威终态" in str(exc)
            else:
                raise AssertionError("bare done must not commit context")
        finally:
            await runtime.aclose()
        assert [event.kind for event in events] == [
            "delivery_committed", "text"]

    asyncio.run(run())


def test_native_runtime_cancel_after_commit_poison_session_without_replay() -> None:
    async def run() -> None:
        with FakeOpenAICompatibleServer() as server:
            runtime = NativeAgentRuntime(
                OpenAICompatibleProvider(server.base_url),
                model_id="fake-model",
                name="native-test",
                system_prompt="tool-less fixture",
                inactivity_timeout=1,
            )
            original_session = runtime.session_id
            committed = asyncio.Event()

            async def consume() -> None:
                async for event in runtime.stream("FAKE_SLOW", "/tmp"):
                    if event.kind == "delivery_committed":
                        committed.set()

            task = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(committed.wait(), timeout=1)
                task.cancel()
                try:
                    await task
                except AgentDeliveryCancelledError:
                    pass
                else:
                    raise AssertionError("committed request cancellation must surface")
                assert runtime.session_id != original_session

                events = [
                    event
                    async for event in runtime.stream(
                        "FAKE_CONTEXT after cancel", "/tmp")
                ]
                assert "".join(
                    event.text for event in events if event.kind == "text"
                ) == "context-1"
            finally:
                await runtime.aclose()

    asyncio.run(run())


def test_native_provider_maps_model_mismatch_disconnect_and_redacts_secret() -> None:
    async def run() -> None:
        with FakeOpenAICompatibleServer(("actual-model",)) as server:
            mismatch = OpenAICompatibleProvider(server.base_url)
            try:
                try:
                    async for _ in mismatch.stream(
                        (ModelMessage("user", "hello"),),
                        model_id="configured-model",
                    ):
                        pass
                except ModelProviderError as exc:
                    assert "configured-model" in str(exc)
                    assert "actual-model" in str(exc)
                    assert "/v1/models" in str(exc)
                else:
                    raise AssertionError("unknown exact model id must fail")
                assert not any(
                    item["method"] == "POST" for item in server.requests)
            finally:
                await mismatch.aclose()

        with FakeOpenAICompatibleServer(("x" * 200000,)) as server:
            bounded = OpenAICompatibleProvider(server.base_url)
            try:
                try:
                    async for _ in bounded.stream(
                        (ModelMessage("user", "hello"),),
                        model_id="configured-model",
                    ):
                        pass
                except ModelProviderError as exc:
                    assert len(str(exc)) <= 2100
                else:
                    raise AssertionError("model mismatch must remain bounded")
            finally:
                await bounded.aclose()

        secret = "fixture-super-secret-key"
        with FakeOpenAICompatibleServer() as server:
            runtime = NativeAgentRuntime(
                OpenAICompatibleProvider(server.base_url, api_key=secret),
                model_id="fake-model",
                name="native-test",
                system_prompt="tool-less fixture",
            )
            try:
                try:
                    async for _ in runtime.stream("FAKE_ERROR_SECRET", "/tmp"):
                        pass
                except ModelProviderError as exc:
                    rendered = str(exc)
                    assert secret not in rendered
                    assert "Bearer [已隐藏]" in rendered
                else:
                    raise AssertionError("provider HTTP error must surface")
            finally:
                await runtime.aclose()

        with FakeOpenAICompatibleServer() as server:
            runtime = NativeAgentRuntime(
                OpenAICompatibleProvider(server.base_url),
                model_id="fake-model",
                name="native-test",
                system_prompt="tool-less fixture",
            )
            events = []
            try:
                try:
                    async for event in runtime.stream(
                        "FAKE_PARTIAL_EOF", "/tmp"
                    ):
                        events.append(event)
                except AgentDeliveryUncertainError:
                    pass
                else:
                    raise AssertionError("partial committed stream is uncertain")
            finally:
                await runtime.aclose()
            assert [event.kind for event in events] == [
                "delivery_committed", "text"]

    asyncio.run(run())


def test_native_provider_bounds_models_and_sse_frames() -> None:
    async def run() -> None:
        with FakeOpenAICompatibleServer(("x" * (1024 * 1024),)) as server:
            provider = OpenAICompatibleProvider(server.base_url)
            try:
                try:
                    await provider.list_models()
                except ModelProviderError as exc:
                    assert "1 MiB" in str(exc)
                else:
                    raise AssertionError("oversize /models must be rejected")
            finally:
                await provider.aclose()

        with FakeOpenAICompatibleServer() as server:
            runtime = NativeAgentRuntime(
                OpenAICompatibleProvider(server.base_url),
                model_id="fake-model",
                name="native-test",
                system_prompt="tool-less fixture",
            )
            try:
                try:
                    async for _ in runtime.stream("FAKE_HUGE_SSE", "/tmp"):
                        pass
                except AgentDeliveryUncertainError as exc:
                    assert "4 MiB" in str(exc)
                else:
                    raise AssertionError("oversize SSE line must be rejected")
            finally:
                await runtime.aclose()

    asyncio.run(run())


def test_orchestrator_production_host_is_native_and_missing_config_blocks_cleanly() -> None:
    orch = Orchestrator("/tmp", specs=(), persistent=False)
    try:
        assert isinstance(orch.host.adapter, NativeAgentRuntime)
        assert orch.host.adapter.tool_policy == "none"
        assert orch.adapters["host"] is orch.host
    finally:
        asyncio.run(orch.aclose())

    with tempfile.TemporaryDirectory(
        prefix="myagents-native-missing-config-",
    ) as raw, patch.dict(
        os.environ, {"XDG_CONFIG_HOME": raw}, clear=True,
    ):
        unavailable = Orchestrator(
            "/tmp",
            specs=(),
            persistent=False,
            discover_agents=True,
        )
        try:
            status = unavailable.agent_readiness_snapshot()[0]
            assert status.name == "host"
            assert status.state is ReadinessState.NOT_FOUND
            assert "myagents/config.toml" in status.setup_hint
            try:
                unavailable.require_message_agents("你好")
            except AgentUnavailableError as exc:
                assert "host" in str(exc)
                assert "myagents/config.toml" in str(exc)
            else:
                raise AssertionError("unconfigured native host must be blocked")
        finally:
            asyncio.run(unavailable.aclose())


def test_native_host_direct_route_discussion_and_explicit_mention() -> None:
    def specs_and_workers():
        workers = {name: _Worker(name) for name in ("alpha", "beta")}
        specs = tuple(
            AgentSpec(name, "jsonl", lambda worker=worker: worker)
            for name, worker in workers.items()
        )
        return specs, workers

    async def configured_orchestrator(server, specs):
        orch = Orchestrator(
            "/tmp",
            specs=specs,
            persistent=False,
            host_probe=_ready_host,
        )
        await orch.host.aclose()
        orch.host = HostAgent(
            create_native_host_runtime(environ={
                "MYAGENTS_MODEL_BASE_URL": server.base_url,
                "MYAGENTS_MODEL_ID": "fake-model",
            }),
            workers=[spec.name for spec in specs],
        )
        orch.adapters["host"] = orch.host
        return orch

    async def run() -> None:
        with FakeOpenAICompatibleServer() as server:
            specs, workers = specs_and_workers()
            orch = await configured_orchestrator(server, specs)
            try:
                direct = await orch.dispatch("FAKE_DIRECT", lambda *_: None)
                assert not direct.failures
                assert [(m.speaker, m.text) for m in orch.history] == [
                    ("user", "FAKE_DIRECT"),
                    ("host", "native direct answer"),
                ]
                assert not workers["alpha"].prompts
            finally:
                await orch.aclose()

        with FakeOpenAICompatibleServer() as server:
            specs, workers = specs_and_workers()
            orch = await configured_orchestrator(server, specs)
            try:
                routed = await orch.dispatch("FAKE_ROUTE", lambda *_: None)
                assert not routed.failures
                assert [m.speaker for m in orch.history] == ["user", "alpha"]
                assert "完整执行 fake 路由任务" in workers["alpha"].prompts[0]

                request_count = len(server.requests)
                explicit = await orch.dispatch(
                    "@beta FAKE_EXPLICIT", lambda *_: None)
                assert not explicit.failures
                assert len(server.requests) == request_count
                assert "FAKE_EXPLICIT" in workers["beta"].prompts[0]
            finally:
                await orch.aclose()

        with FakeOpenAICompatibleServer() as server:
            specs, _workers = specs_and_workers()
            orch = await configured_orchestrator(server, specs)
            try:
                outcome = await orch.dispatch(
                    "FAKE_DISCUSSION 请 alpha 和 beta 互相讨论",
                    lambda *_: None,
                )
                assert not outcome.failures
                assert [m.speaker for m in orch.history] == [
                    "user", "alpha", "beta", "host"]
                assert orch.history[-1].text == "native moderator summary"
            finally:
                await orch.aclose()

    asyncio.run(run())


def test_native_host_route_uncertain_turn_is_not_replayed() -> None:
    async def run() -> None:
        with FakeOpenAICompatibleServer() as server:
            orch = Orchestrator(
                "/tmp",
                specs=(),
                persistent=False,
                host_probe=_ready_host,
            )
            await orch.host.aclose()
            orch.host = HostAgent(create_native_host_runtime(environ={
                "MYAGENTS_MODEL_BASE_URL": server.base_url,
                "MYAGENTS_MODEL_ID": "fake-model",
            }))
            orch.adapters["host"] = orch.host
            try:
                failed = await orch.dispatch(
                    "FAKE_PARTIAL_EOF", lambda *_: None)
                assert failed.failures

                recovered = await orch.dispatch(
                    "FAKE_CONTEXT recovered", lambda *_: None)
                assert not recovered.failures
                assert orch.history[-1].text == "context-1"
                posts = [
                    item["json"] for item in server.requests
                    if item["method"] == "POST"
                ]
                assert len(posts) == 2
                second_prompt = posts[1]["messages"][-1]["content"]
                assert "FAKE_CONTEXT recovered" in second_prompt
                assert "FAKE_PARTIAL_EOF" not in second_prompt
            finally:
                await orch.aclose()

    asyncio.run(run())


def test_native_host_route_freezes_dispatch_snapshot_while_lock_is_busy() -> None:
    async def run() -> None:
        with FakeOpenAICompatibleServer() as server:
            orch = Orchestrator(
                "/tmp",
                specs=(),
                persistent=False,
                host_probe=_ready_host,
            )
            await orch.host.aclose()
            orch.host = HostAgent(create_native_host_runtime(environ={
                "MYAGENTS_MODEL_BASE_URL": server.base_url,
                "MYAGENTS_MODEL_ID": "fake-model",
            }))
            orch.adapters["host"] = orch.host
            lock = orch._delivery_lock("host")
            await lock.acquire()
            try:
                first = asyncio.create_task(orch.dispatch(
                    "FAKE_CONTEXT first", lambda *_: None))
                while len(orch.history) < 1:
                    await asyncio.sleep(0)
                await asyncio.sleep(0)
                second = asyncio.create_task(orch.dispatch(
                    "FAKE_CONTEXT second", lambda *_: None))
                while len(orch.history) < 2:
                    await asyncio.sleep(0)
                await asyncio.sleep(0)
            finally:
                lock.release()
            try:
                outcomes = await asyncio.gather(first, second)
                assert all(not outcome.failures for outcome in outcomes)
                posts = [
                    item["json"] for item in server.requests
                    if item["method"] == "POST"
                ]
                assert len(posts) == 2
                first_prompt = posts[0]["messages"][-1]["content"]
                second_prompt = posts[1]["messages"][-1]["content"]
                assert "FAKE_CONTEXT first" in first_prompt
                assert "FAKE_CONTEXT second" not in first_prompt
                assert "FAKE_CONTEXT second" in second_prompt
            finally:
                await orch.aclose()

    asyncio.run(run())


if __name__ == "__main__":
    test_native_host_readiness_requires_explicit_model_configuration()
    test_native_host_uses_private_xdg_config_with_environment_overrides()
    test_native_host_rejects_unsafe_or_broken_config_file()
    test_relative_xdg_config_home_falls_back_to_user_config_home()
    test_custom_config_path_is_preserved_in_runtime_setup_hint()
    test_openai_compatible_provider_lists_exact_models_and_streams_text()
    test_native_runtime_streams_and_preserves_isolated_session_context()
    test_native_runtime_sessions_are_isolated_and_environment_factory_is_lazy()
    test_native_runtime_timeout_after_commit_is_no_replay_uncertain()
    test_native_runtime_post_attempt_timeout_before_headers_is_uncertain()
    test_native_runtime_rejects_bare_done_without_terminal_proof()
    test_native_runtime_cancel_after_commit_poison_session_without_replay()
    test_native_provider_maps_model_mismatch_disconnect_and_redacts_secret()
    test_native_provider_bounds_models_and_sse_frames()
    test_orchestrator_production_host_is_native_and_missing_config_blocks_cleanly()
    test_native_host_direct_route_discussion_and_explicit_mention()
    test_native_host_route_uncertain_turn_is_not_replayed()
    test_native_host_route_freezes_dispatch_snapshot_while_lock_is_busy()
    print("ok  native host readiness requires explicit model configuration")

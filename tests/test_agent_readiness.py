"""M4.10 Agent 就绪探测与缺失安装体验。

所有场景只使用临时 fake executable/resolver，不启动或修改真实 agent。

运行：.venv/bin/python tests/test_agent_readiness.py
"""

from __future__ import annotations

import os
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_readiness import (
    AgentEnablementConfig,
    AgentEnablementError,
    AgentReadiness,
    AgentReadinessRegistry,
    AgentUnavailableError,
    ReadinessState,
    executable_probe,
    parse_agent_control_command,
)
from adapters.base import AgentEvent, AgentHostCapability
from host import HostAgent, HostDecision
from orchestrator import AgentSpec, Orchestrator
from session_manager import SessionManager
from storage.store import RoomStore


class FakeResolver:
    def __init__(self, values: dict[str, str | None]) -> None:
        self.values = values
        self.calls: list[str] = []

    def __call__(self, command: str) -> str | None:
        self.calls.append(command)
        return self.values.get(command)


def _fake_executable(root: Path, name: str, *, executable: bool = True) -> Path:
    target = root / name
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700 if executable else 0o600)
    return target


def test_snapshot_distinguishes_ready_missing_and_invalid() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-readiness-") as raw:
        root = Path(raw)
        kimi = _fake_executable(root, "kimi")
        qwen = _fake_executable(root, "qwen", executable=False)
        resolver = FakeResolver({
            "kimi": str(kimi),
            "qwen": str(qwen),
            "opencode": None,
        })
        registry = AgentReadinessRegistry({
            "kimi": executable_probe(
                "kimi", ("kimi",), "安装 Kimi", resolver=resolver),
            "qwen": executable_probe(
                "qwen", ("qwen",), "安装 Qwen", resolver=resolver),
            "opencode": executable_probe(
                "opencode", ("opencode",), "安装 OpenCode",
                resolver=resolver,
            ),
        })

        snapshot = registry.refresh()
        by_name = {item.name: item for item in snapshot}

        assert by_name["kimi"].state is ReadinessState.READY
        assert by_name["kimi"].executable == str(kimi.resolve())
        assert by_name["opencode"].state is ReadinessState.NOT_FOUND
        assert "当前进程 PATH" in by_name["opencode"].detail
        assert by_name["qwen"].state is ReadinessState.INVALID
        assert "不可执行" in by_name["qwen"].detail
        assert resolver.calls == ["kimi", "qwen", "opencode"]


def test_require_is_atomic_and_actionable() -> None:
    resolver = FakeResolver({"kimi": None, "qwen": None})
    registry = AgentReadinessRegistry({
        "kimi": executable_probe(
            "kimi", ("kimi",), "确认 kimi 在 PATH 中", resolver=resolver),
        "qwen": executable_probe(
            "qwen", ("qwen",), "确认 qwen 在 PATH 中", resolver=resolver),
    })
    registry.refresh()

    try:
        registry.require(("kimi", "qwen"), purpose="派发任务")
    except AgentUnavailableError as exc:
        assert exc.names == ("kimi", "qwen")
        assert "@kimi" in str(exc)
        assert "@qwen" in str(exc)
        assert "/agents rescan" in str(exc)
    else:
        raise AssertionError("任一目标未就绪时必须原子拒绝")


def test_refresh_observes_fake_install_without_restart() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-readiness-rescan-") as raw:
        root = Path(raw)
        resolver = FakeResolver({"qwen": None})
        registry = AgentReadinessRegistry({
            "qwen": executable_probe(
                "qwen", ("qwen",), "安装 Qwen", resolver=resolver),
        })

        assert registry.refresh()[0].state is ReadinessState.NOT_FOUND
        fake_qwen = _fake_executable(root, "qwen")
        resolver.values["qwen"] = str(fake_qwen)
        refreshed = registry.refresh()

        assert refreshed[0].state is ReadinessState.READY
        assert refreshed[0].executable == str(fake_qwen.resolve())
        registry.require(("qwen",), purpose="派发任务")


def test_probe_canonicalizes_symlink_without_executing_target() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-readiness-link-") as raw:
        root = Path(raw)
        marker = root / "must-not-exist"
        target = root / "real-agent"
        target.write_text(
            f'#!/bin/sh\ntouch "{marker}"\n',
            encoding="utf-8",
        )
        target.chmod(0o700)
        link = root / "agent-link"
        link.symlink_to(target)
        resolver = FakeResolver({"agent": str(link)})
        registry = AgentReadinessRegistry({
            "agent": executable_probe(
                "agent", ("agent",), "安装 agent", resolver=resolver),
        })

        status = registry.refresh()[0]

        assert status.state is ReadinessState.READY
        assert status.executable == str(target.resolve())
        assert os.path.exists(status.executable)
        assert not marker.exists(), "readiness probe 不得执行候选 CLI"


def test_global_switch_config_preserves_host_and_is_private() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-enable-config-") as raw:
        path = Path(raw) / "config.toml"
        path.write_text(
            "# keep this comment\n"
            "[host.model]\n"
            'provider = "openai-compatible"\n'
            'base_url = "http://127.0.0.1:1234/v1"\n'
            'model_id = "fixture-model"\n',
            encoding="utf-8",
        )
        path.chmod(0o600)
        config = AgentEnablementConfig(path)

        config.set_enabled("kimi", False)
        source = path.read_text(encoding="utf-8")
        assert "# keep this comment" in source
        assert 'model_id = "fixture-model"' in source
        assert "[agents.kimi]\nenabled = false" in source
        assert config.disabled_names(("kimi", "qwen")) == frozenset({"kimi"})
        assert path.stat().st_mode & 0o777 == 0o600

        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "enabled = false", "enabled = false # keep switch note"),
            encoding="utf-8",
        )
        config.set_enabled("kimi", True)
        assert config.disabled_names(("kimi",)) == frozenset()
        assert path.read_text(encoding="utf-8").count("[agents.kimi]") == 1
        assert "enabled = true # keep switch note" in path.read_text(
            encoding="utf-8")


def test_global_switch_config_fails_closed_on_unsafe_or_invalid_file() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-enable-invalid-") as raw:
        root = Path(raw)
        path = root / "config.toml"
        path.write_text("[agents.kimi]\nenabled = false\n", encoding="utf-8")
        path.chmod(0o644)
        config = AgentEnablementConfig(path)
        try:
            config.disabled_names(("kimi",))
        except AgentEnablementError as exc:
            assert "0600" in str(exc)
        else:
            raise AssertionError("宽松权限的开关配置必须 fail-closed")

        path.chmod(0o600)
        link = root / "linked.toml"
        link.symlink_to(path)
        try:
            AgentEnablementConfig(link).set_enabled("kimi", True)
        except AgentEnablementError as exc:
            assert "安全读取" in str(exc)
        else:
            raise AssertionError("不得跟随配置符号链接")


def test_agent_control_command_parser_is_strict() -> None:
    assert parse_agent_control_command("/agents").action == "show"
    assert parse_agent_control_command("/agents rescan").action == "rescan"
    command = parse_agent_control_command("/agents disable kimi")
    assert command is not None
    assert (command.action, command.name) == ("disable", "kimi")
    assert parse_agent_control_command("hello") is None
    try:
        parse_agent_control_command("/agents disable")
    except AgentEnablementError as exc:
        assert "/agents disable <agent>" in str(exc)
    else:
        raise AssertionError("不完整开关命令不得进入模型路由")


class FakeAdapter:
    stateful_session = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.session_id = None
        self.calls: list[str] = []

    async def stream(self, prompt: str, _workdir: str, **_kwargs):
        self.calls.append(prompt)
        yield AgentEvent("text", f"{self.name}-ok")
        yield AgentEvent("done")


def _fixed_probe(
    name: str,
    state: ReadinessState,
    detail: str | None = None,
):
    return lambda: AgentReadiness(
        name,
        state,
        detail or state.value,
        f"setup {name}",
        f"/fake/{name}" if state is ReadinessState.READY else None,
    )


class RecordingHost:
    name = "host"

    def __init__(self) -> None:
        self.choices: list[str] | None = None

    async def decide(
        self,
        _transcript: str,
        _workdir: str,
        _on_event=None,
        *,
        choices: list[str] | None = None,
    ) -> HostDecision:
        self.choices = choices
        assert choices
        return HostDecision(
            [choices[0]],
            "fake route",
            tasks={choices[0]: "执行 fake task"},
        )

    async def stream(self, _prompt: str, _workdir: str, **_kwargs):
        yield AgentEvent("done")


def test_explicit_missing_target_is_rejected_before_timeline() -> None:
    async def run() -> None:
        ready = FakeAdapter("ready")
        missing = FakeAdapter("missing")
        specs = (
            AgentSpec(
                "ready", "jsonl", lambda: ready,
                probe=_fixed_probe("ready", ReadinessState.READY),
            ),
            AgentSpec(
                "missing", "jsonl", lambda: missing,
                probe=_fixed_probe("missing", ReadinessState.NOT_FOUND),
            ),
        )
        orch = Orchestrator(
            ".",
            specs=specs,
            persistent=False,
            discover_agents=True,
            host_probe=_fixed_probe("host", ReadinessState.READY),
        )
        try:
            try:
                await orch.dispatch(
                    "@ready @missing 一起处理",
                    lambda _name, _event: None,
                )
            except AgentUnavailableError as exc:
                assert exc.names == ("missing",)
            else:
                raise AssertionError("显式多目标必须在 timeline 前原子拒绝")
            assert orch.history == []
            assert ready.calls == []
            assert missing.calls == []
        finally:
            await orch.aclose()

    asyncio.run(run())


def test_workflow_missing_role_is_rejected_before_git_baseline() -> None:
    class Inspector:
        def __init__(self) -> None:
            self.calls = 0

        async def capture_baseline(self, _workdir):
            self.calls += 1
            raise AssertionError("readiness 失败后不得读取 workflow baseline")

    async def run() -> None:
        inspector = Inspector()
        specs = tuple(
            AgentSpec(
                name,
                "jsonl",
                lambda name=name: FakeAdapter(name),
                probe=_fixed_probe(
                    name,
                    ReadinessState.NOT_FOUND
                    if name == "verify" else ReadinessState.READY,
                ),
            )
            for name in ("review", "write", "verify")
        )
        orch = Orchestrator(
            ".",
            specs=specs,
            persistent=False,
            discover_agents=True,
            host_probe=_fixed_probe("host", ReadinessState.READY),
            workspace_inspector=inspector,
        )
        try:
            message = (
                "/workflow --reviewer @review --implementer @write "
                "--verifier @verify -- 完成任务"
            )
            try:
                await orch.dispatch(message, lambda _name, _event: None)
            except AgentUnavailableError as exc:
                assert exc.names == ("verify",)
            else:
                raise AssertionError("workflow 缺少角色时必须拒绝")
            assert inspector.calls == 0
            assert orch.history == []
        finally:
            await orch.aclose()

    asyncio.run(run())


def test_discussion_missing_participant_is_rejected_atomically() -> None:
    async def run() -> None:
        specs = tuple(
            AgentSpec(
                name,
                "jsonl",
                lambda name=name: FakeAdapter(name),
                probe=_fixed_probe(
                    name,
                    ReadinessState.NOT_FOUND
                    if name == "missing" else ReadinessState.READY,
                ),
            )
            for name in ("ready", "missing")
        )
        orch = Orchestrator(
            ".",
            specs=specs,
            persistent=False,
            discover_agents=True,
            host_probe=_fixed_probe("host", ReadinessState.READY),
        )
        try:
            try:
                await orch.dispatch(
                    "/discuss @ready @missing -- 讨论主题",
                    lambda _name, _event: None,
                )
            except AgentUnavailableError as exc:
                assert exc.names == ("missing",)
            else:
                raise AssertionError("discussion 缺失参与者必须整条拒绝")
            assert orch.history == []
        finally:
            await orch.aclose()

    asyncio.run(run())


def test_missing_host_blocks_unmentioned_message_before_timeline() -> None:
    async def run() -> None:
        worker = FakeAdapter("worker")
        orch = Orchestrator(
            ".",
            specs=(AgentSpec(
                "worker", "jsonl", lambda: worker,
                probe=_fixed_probe("worker", ReadinessState.READY),
            ),),
            persistent=False,
            discover_agents=True,
            host_probe=_fixed_probe("host", ReadinessState.NOT_FOUND),
        )
        try:
            try:
                await orch.dispatch("没有显式点名", lambda _n, _e: None)
            except AgentUnavailableError as exc:
                assert exc.names == ("host",)
            else:
                raise AssertionError("host 未就绪时必须进入手动点名模式")
            assert orch.history == []
            assert worker.calls == []
        finally:
            await orch.aclose()

    asyncio.run(run())


def test_local_steering_does_not_require_host_readiness() -> None:
    orch = Orchestrator(
        ".",
        specs=(),
        persistent=False,
        discover_agents=True,
        host_probe=_fixed_probe("host", ReadinessState.NOT_FOUND),
    )
    try:
        orch.require_message_agents("/steer -- 增加一条验收标准")
    finally:
        asyncio.run(orch.aclose())


def test_host_routes_only_to_ready_workers() -> None:
    async def run() -> None:
        ready = FakeAdapter("ready")
        missing = FakeAdapter("missing")
        specs = (
            AgentSpec(
                "ready", "jsonl", lambda: ready,
                probe=_fixed_probe("ready", ReadinessState.READY),
            ),
            AgentSpec(
                "missing", "jsonl", lambda: missing,
                probe=_fixed_probe("missing", ReadinessState.NOT_FOUND),
            ),
        )
        orch = Orchestrator(
            ".",
            specs=specs,
            persistent=False,
            discover_agents=True,
            host_probe=_fixed_probe("host", ReadinessState.READY),
        )
        host = RecordingHost()
        orch.host = host
        orch.adapters["host"] = host
        try:
            outcome = await orch.dispatch(
                "请选择合适的人处理",
                lambda _name, _event: None,
            )
            assert not outcome.failures
            assert host.choices == ["ready"]
            assert len(ready.calls) == 1
            assert missing.calls == []
        finally:
            await orch.aclose()

    asyncio.run(run())


def test_global_switch_blocks_dispatch_and_host_routing_without_deleting_state() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="myagents-enable-routing-"
        ) as raw:
            path = Path(raw) / "config.toml"
            config = AgentEnablementConfig(path)
            worker = FakeAdapter("worker")
            orch = Orchestrator(
                ".",
                specs=(AgentSpec(
                    "worker", "jsonl", lambda: worker,
                    probe=_fixed_probe("worker", ReadinessState.READY),
                ),),
                persistent=False,
                discover_agents=True,
                host_probe=_fixed_probe("host", ReadinessState.READY),
                agent_enablement=config,
            )
            try:
                original_adapter = orch.adapters["worker"]
                orch.set_agent_enabled("worker", enabled=False)
                status = {
                    item.name: item
                    for item in orch.agent_readiness_snapshot()
                }["worker"]
                assert status.state is ReadinessState.DISABLED
                assert orch.adapters["worker"] is original_adapter
                assert orch.history == []
                try:
                    await orch.dispatch(
                        "@worker do it", lambda _name, _event: None)
                except AgentUnavailableError as exc:
                    assert exc.names == ("worker",)
                    assert "全局配置禁用" in str(exc)
                else:
                    raise AssertionError("已禁用 agent 不得接受新派发")
                assert worker.calls == []
                assert orch.history == []

                assert orch.ready_worker_names() == []

                orch.set_agent_enabled("worker", enabled=True)
                assert orch.ready_worker_names() == ["worker"]
                await orch.dispatch("@worker do it", lambda _name, _event: None)
                assert worker.calls
            finally:
                await orch.aclose()

    asyncio.run(run())


def test_disabled_agent_cannot_be_selected_as_host_backend() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="myagents-enable-host-"
        ) as raw:
            config = AgentEnablementConfig(Path(raw) / "config.toml")

            def worker_factory():
                return FakeAdapter("worker")

            worker_factory.host_capability = lambda: AgentHostCapability(
                "app-server", lambda: FakeAdapter("worker"))
            orch = Orchestrator(
                ".",
                specs=(AgentSpec(
                    "worker",
                    "app-server",
                    worker_factory,
                    probe=_fixed_probe("worker", ReadinessState.READY),
                ),),
                persistent=False,
                discover_agents=True,
                host_probe=_fixed_probe("host", ReadinessState.READY),
                host_model_factory=lambda _selection: FakeAdapter("host"),
                agent_enablement=config,
            )
            try:
                orch.set_agent_enabled("worker", enabled=False)
                try:
                    await orch.switch_host_backend("agent", "worker")
                except AgentUnavailableError as exc:
                    assert exc.names == ("host",)
                    assert "全局配置禁用" in str(exc)
                else:
                    raise AssertionError("已禁用 worker 不得成为 Host backend")
                assert orch.host_backend_selection.kind == "model"
            finally:
                await orch.aclose()

    asyncio.run(run())


def test_global_switch_applies_when_system_discovery_is_disabled() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-enable-embedded-") as raw:
        config = AgentEnablementConfig(Path(raw) / "config.toml")
        orch = Orchestrator(
            ".",
            specs=(AgentSpec(
                "worker", "jsonl", lambda: FakeAdapter("worker")),),
            persistent=False,
            discover_agents=False,
            agent_enablement=config,
        )
        try:
            assert orch.agent_readiness_snapshot()[0].ready
            orch.set_agent_enabled("worker", enabled=False)
            assert orch.agent_readiness_snapshot()[0].state \
                is ReadinessState.DISABLED
            try:
                orch.require_message_agents("@worker task")
            except AgentUnavailableError:
                pass
            else:
                raise AssertionError("关闭系统探测时仍必须应用全局禁用覆盖")
        finally:
            asyncio.run(orch.aclose())


def test_host_route_prompt_excludes_unready_workers() -> None:
    host = HostAgent(adapter=FakeAdapter("host"), workers=["ready", "missing"])
    prompt = host._build_route_prompt("[user] task", ["ready"])

    assert "ready" in prompt
    assert "missing" not in prompt
    no_worker_prompt = host._build_route_prompt("[user] task", [])
    assert "当前没有可派发的 worker" in no_worker_prompt
    assert '"targets"' not in no_worker_prompt


def test_rescan_constructs_newly_ready_adapter_without_restart() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-readiness-orch-") as raw:
        root = Path(raw)
        resolver = FakeResolver({"worker": None})
        created: list[FakeAdapter] = []

        def factory() -> FakeAdapter:
            adapter = FakeAdapter("worker")
            created.append(adapter)
            return adapter

        spec = AgentSpec(
            "worker",
            "jsonl",
            factory,
            probe=executable_probe(
                "worker", ("worker",), "安装 worker", resolver=resolver),
        )
        orch = Orchestrator(
            ".",
            specs=(spec,),
            persistent=False,
            discover_agents=True,
            host_probe=_fixed_probe("host", ReadinessState.READY),
        )
        try:
            assert created == []
            assert "worker" not in orch.adapters
            executable = _fake_executable(root, "worker")
            resolver.values["worker"] = str(executable)

            statuses = orch.refresh_agent_readiness()

            assert {item.name: item.state for item in statuses}["worker"] \
                is ReadinessState.READY
            assert len(created) == 1
            assert orch.adapters["worker"] is created[0]
        finally:
            asyncio.run(orch.aclose())


def test_rescan_does_not_publish_partially_restored_adapter() -> None:
    class StatefulAdapter(FakeAdapter):
        stateful_session = True

    with tempfile.TemporaryDirectory(prefix="myagents-readiness-state-") as raw:
        root = Path(raw)
        workdir = root / "work"
        workdir.mkdir()
        store = RoomStore(workdir, state_root=root / "state")
        store.set_agent_state("worker", cursor=5)
        state = {"value": ReadinessState.NOT_FOUND}
        spec = AgentSpec(
            "worker",
            "acp",
            lambda: StatefulAdapter("worker"),
            probe=lambda: AgentReadiness(
                "worker", state["value"], "fake", "setup worker"),
        )
        orch = Orchestrator(
            str(workdir),
            specs=(spec,),
            store=store,
            discover_agents=True,
            host_probe=_fixed_probe("host", ReadinessState.READY),
        )
        try:
            state["value"] = ReadinessState.READY
            try:
                orch.refresh_agent_readiness()
            except Exception as exc:
                assert "cursor=5" in str(exc)
            else:
                raise AssertionError("损坏 cursor 必须 fail loudly")
            assert "worker" not in orch.adapters
            assert "worker" not in orch._cursors
            statuses = {
                item.name: item for item in orch.agent_readiness_snapshot()
            }
            assert statuses["worker"].state is ReadinessState.INVALID
        finally:
            asyncio.run(orch.aclose())


def test_rescan_does_not_publish_adapter_when_setup_fails() -> None:
    class BrokenAdapter(FakeAdapter):
        def set_attachment_root(self, _root) -> None:
            raise RuntimeError("fake setup failed")

    state = {"value": ReadinessState.NOT_FOUND}
    spec = AgentSpec(
        "worker",
        "acp",
        lambda: BrokenAdapter("worker"),
        probe=lambda: AgentReadiness(
            "worker", state["value"], "fake", "setup worker"),
    )
    orch = Orchestrator(
        ".",
        specs=(spec,),
        persistent=False,
        discover_agents=True,
        host_probe=_fixed_probe("host", ReadinessState.READY),
    )
    try:
        state["value"] = ReadinessState.READY
        statuses = orch.refresh_agent_readiness()
        assert "worker" not in orch.adapters
        assert {item.name: item.state for item in statuses}["worker"] \
            is ReadinessState.INVALID
    finally:
        asyncio.run(orch.aclose())


def test_multisession_rescan_updates_all_loaded_rooms_and_reuses_probes() -> None:
    state = {"value": ReadinessState.NOT_FOUND}

    def worker_probe() -> AgentReadiness:
        return AgentReadiness(
            "worker", state["value"], "fake worker", "setup worker")

    host_probe = _fixed_probe("host", ReadinessState.NOT_FOUND)

    async def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="myagents-readiness-sessions-"
        ) as raw:
            root = Path(raw)
            workdir = root / "work"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            spec = AgentSpec(
                "worker", "jsonl", lambda: FakeAdapter("worker"),
                probe=worker_probe,
            )
            initial = Orchestrator(
                str(workdir),
                specs=(spec,),
                store=store,
                discover_agents=True,
                host_probe=host_probe,
            )
            manager = SessionManager(
                workdir,
                specs=(spec,),
                initial_orchestrator=initial,
                enable_control=False,
            )
            await manager.start()
            first_id = manager.active_session_id
            second = await manager.create_session(workdir)
            second_id = second.summary.room_id
            try:
                active_orch = manager.active_runtime.orch
                second_status = {
                    item.name: item.state
                    for item in active_orch.agent_readiness_snapshot()
                }
                assert second_status["host"] is ReadinessState.NOT_FOUND

                state["value"] = ReadinessState.READY
                manager.refresh_agent_readiness()

                for room_id in (first_id, second_id):
                    await manager.activate(room_id)
                    room_orch = manager.active_runtime.orch
                    statuses = {
                        item.name: item.state
                        for item in room_orch.agent_readiness_snapshot()
                    }
                    assert statuses["worker"] is ReadinessState.READY
            finally:
                await manager.aclose()

    asyncio.run(run())


def test_global_switch_syncs_all_loaded_rooms() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="myagents-enable-sessions-"
        ) as raw:
            root = Path(raw)
            workdir = root / "work"
            workdir.mkdir()
            config = AgentEnablementConfig(root / "config.toml")
            spec = AgentSpec(
                "worker", "jsonl", lambda: FakeAdapter("worker"),
                probe=_fixed_probe("worker", ReadinessState.READY),
            )
            initial = Orchestrator(
                str(workdir),
                specs=(spec,),
                store=RoomStore(workdir, state_root=root / "state"),
                discover_agents=True,
                host_probe=_fixed_probe("host", ReadinessState.READY),
                agent_enablement=config,
            )
            manager = SessionManager(
                workdir,
                specs=(spec,),
                initial_orchestrator=initial,
                enable_control=False,
            )
            await manager.start()
            first_id = manager.active_session_id
            second_id = (await manager.create_session(workdir)).summary.room_id
            try:
                manager.set_agent_enabled("worker", enabled=False)
                for room_id in (first_id, second_id):
                    await manager.activate(room_id)
                    status = manager.active_runtime.orch \
                        .agent_readiness_snapshot()[0]
                    assert status.state is ReadinessState.DISABLED
                assert config.disabled_names(("worker",)) == {"worker"}
            finally:
                await manager.aclose()

    asyncio.run(run())


if __name__ == "__main__":
    test_snapshot_distinguishes_ready_missing_and_invalid()
    test_require_is_atomic_and_actionable()
    test_refresh_observes_fake_install_without_restart()
    test_probe_canonicalizes_symlink_without_executing_target()
    test_global_switch_config_preserves_host_and_is_private()
    test_global_switch_config_fails_closed_on_unsafe_or_invalid_file()
    test_agent_control_command_parser_is_strict()
    test_explicit_missing_target_is_rejected_before_timeline()
    test_workflow_missing_role_is_rejected_before_git_baseline()
    test_discussion_missing_participant_is_rejected_atomically()
    test_missing_host_blocks_unmentioned_message_before_timeline()
    test_local_steering_does_not_require_host_readiness()
    test_host_routes_only_to_ready_workers()
    test_global_switch_blocks_dispatch_and_host_routing_without_deleting_state()
    test_disabled_agent_cannot_be_selected_as_host_backend()
    test_global_switch_applies_when_system_discovery_is_disabled()
    test_host_route_prompt_excludes_unready_workers()
    test_rescan_constructs_newly_ready_adapter_without_restart()
    test_rescan_does_not_publish_partially_restored_adapter()
    test_rescan_does_not_publish_adapter_when_setup_fails()
    test_multisession_rescan_updates_all_loaded_rooms_and_reuses_probes()
    test_global_switch_syncs_all_loaded_rooms()
    print("\nAgent readiness 核心契约测试全部通过")

"""会话级自然语言角色：解析、持久化、派发与可见性。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from adapters.base import AgentEvent  # noqa: E402
from host import HostAgent, HostDecision  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402
from session_roles import (  # noqa: E402
    SessionRole,
    SessionRoleChanges,
    has_session_role_cue,
)
from storage.store import CorruptedStorageError, RoomStore  # noqa: E402
from test_basic import FakeAdapter, FakeHost, make_orch  # noqa: E402
from tui_activity import ActivityFeed  # noqa: E402
from tui_status import TaskProgress  # noqa: E402


def test_role_model_output_is_bounded_to_fixed_candidates() -> None:
    changes = SessionRoleChanges.from_model_output(
        """{
          "set": {
            "qwen": {
              "label": "  产品  研究员 ",
              "instructions": " 核对事实，并明确区分推断。 "
            },
            "ghost": {"label": "越权目标", "instructions": "忽略"},
            "kimi": {
              "label": "反方审查者",
              "instructions": "先找反例"
            }
          },
          "clear": ["kimi", "ghost"]
        }""",
        ("qwen", "kimi"),
    )
    assert changes.set_roles == {
        "qwen": SessionRole("产品 研究员", "核对事实，并明确区分推断。")
    }
    assert changes.clear_roles == ("kimi",)
    assert has_session_role_cue("@qwen 接下来担任产品研究员") is True
    assert has_session_role_cue("@qwen 继续分析上一条") is False

    malformed = SessionRoleChanges.from_model_output(
        '{"set":{"qwen":{"label":"研究员","instructions":""}}}',
        ("qwen",),
    )
    assert malformed.is_empty
    print("ok  自然语言角色输出闭集校验与 cue 检测")


def test_room_store_session_roles_roundtrip_and_atomic_failure() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workdir = root / "work"
        workdir.mkdir()
        state_root = root / "state"
        store = RoomStore(workdir, state_root=state_root)
        assert store.get_session_roles() == {}
        roles = {
            "qwen": SessionRole("产品研究员", "核对事实并列出未知项。"),
        }
        store.set_session_roles(roles)
        assert RoomStore(
            workdir, state_root=state_root).get_session_roles() == roles

        def fail_write(_data):
            raise OSError("磁盘满了（模拟）")

        store._write_state = fail_write  # type: ignore[method-assign]
        try:
            store.set_session_roles({
                "qwen": SessionRole("新角色", "不应提交。")})
            raise AssertionError("角色状态写失败必须穿透")
        except OSError:
            pass
        assert store.get_session_roles() == roles
        assert RoomStore(
            workdir, state_root=state_root).get_session_roles() == roles
    print("ok  会话角色复用 RoomStore 且写失败不假提交")


def test_session_roles_are_isolated_by_named_room_and_fail_on_corruption() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workdir = root / "work"
        workdir.mkdir()
        state_root = root / "state"
        alpha = RoomStore(
            workdir, session_name="alpha", state_root=state_root)
        beta = RoomStore(
            workdir, session_name="beta", state_root=state_root)
        alpha.set_session_roles({
            "qwen": SessionRole("研究员", "只属于 alpha。")})
        assert RoomStore(
            workdir,
            session_name="alpha",
            state_root=state_root,
        ).get_session_roles()["qwen"].label == "研究员"
        assert RoomStore(
            workdir,
            session_name="beta",
            state_root=state_root,
        ).get_session_roles() == {}

        data = json.loads(alpha.state_path.read_text(encoding="utf-8"))
        data["session_roles"] = {
            "qwen": {"label": "研究员", "instructions": ""}}
        alpha.state_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            RoomStore(
                workdir,
                session_name="alpha",
                state_root=state_root,
            )
            raise AssertionError("损坏的 session_roles 必须 fail loudly")
        except CorruptedStorageError:
            pass
    print("ok  会话角色按命名房间隔离，损坏状态不静默覆盖")


class _RoleModelAdapter:
    session_id = None

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    async def stream(self, prompt: str, workdir: str):
        self.prompts.append(prompt)
        yield AgentEvent("status", "正在理解角色")
        yield AgentEvent("text", self.outputs.pop(0))
        yield AgentEvent("done")


def test_host_extracts_roles_without_changing_fixed_targets() -> None:
    adapter = _RoleModelAdapter([
        '{"set":{"qwen":{"label":"产品研究员",'
        '"instructions":"核对事实并列出未知项"},'
        '"ghost":{"label":"越权","instructions":"忽略"}},'
        '"clear":[]}',
    ])
    host = HostAgent(adapter=adapter, workers=["qwen", "opencode"])
    progress: list[AgentEvent] = []
    changes = asyncio.run(host.extract_session_roles(
        "@qwen 接下来担任产品研究员",
        ["qwen"],
        ".",
        progress.append,
    ))
    assert changes.set_roles == {
        "qwen": SessionRole("产品研究员", "核对事实并列出未知项")}
    assert changes.clear_roles == ()
    assert [event.kind for event in progress] == ["status"]
    assert "禁止调用工具" in adapter.prompts[0]
    assert "候选 agent：qwen" in adapter.prompts[0]
    print("ok  host 纯语义提取不改变固定 targets")


def test_unhashable_clear_does_not_turn_route_into_host_answer() -> None:
    adapter = _RoleModelAdapter([
        '{"targets":["qwen"],"reason":"需要研究",'
        '"tasks":{"qwen":"分析需求"},'
        '"role_changes":{"set":{},"clear":[{}]}}',
    ])
    host = HostAgent(adapter=adapter, workers=["qwen"])
    decision = asyncio.run(host.decide("让 qwen 分析需求", "."))
    assert decision.targets == ["qwen"]
    assert decision.answer is None
    assert decision.tasks == {"qwen": "分析需求"}
    assert decision.role_changes.is_empty
    print("ok  畸形 clear 不会吞掉无 mention 的原任务路由")


def test_explicit_role_survives_session_until_natural_language_clear() -> None:
    orch = make_orch()
    role_adapter = _RoleModelAdapter([
        '{"set":{"qwen":{"label":"产品研究员",'
        '"instructions":"核对事实并列出未知项"}},"clear":[]}',
        '{"set":{},"clear":["qwen"]}',
    ])
    orch.host = HostAgent(
        adapter=role_adapter,
        workers=[spec.name for spec in orch.specs],
    )
    orch.adapters["host"] = orch.host
    events: list[tuple[str, AgentEvent]] = []

    asyncio.run(orch.dispatch(
        "@qwen 接下来你担任产品研究员，先分析需求",
        lambda name, event: events.append((name, event)),
    ))
    qwen = orch.adapters["qwen"]
    assert orch.session_roles == {
        "qwen": SessionRole("产品研究员", "核对事实并列出未知项")}
    assert "当前聊天室会话中的临时角色：产品研究员" in qwen.last_prompt
    assert any(
        name == "qwen"
        and event.meta.get("session_role") == "产品研究员"
        for name, event in events
    )
    assert any(
        name == "host"
        and event.kind == "done"
        and event.meta.get("roleExtraction") is True
        for name, event in events
    )

    asyncio.run(orch.dispatch(
        "@qwen 继续分析第二个问题", lambda _name, _event: None))
    assert len(role_adapter.prompts) == 1
    assert "当前聊天室会话中的临时角色：产品研究员" in qwen.last_prompt

    asyncio.run(orch.dispatch(
        "@qwen 不再担任这个角色，恢复普通助手",
        lambda _name, _event: None,
    ))
    assert orch.session_roles == {}
    asyncio.run(orch.dispatch(
        "@qwen 分析第三个问题", lambda _name, _event: None))
    assert len(role_adapter.prompts) == 2
    assert "当前聊天室会话中的临时角色" not in qwen.last_prompt
    print("ok  显式角色在会话内持续，且可用自然语言清除")


def test_host_route_reuses_single_call_for_role_changes() -> None:
    orch = make_orch()
    orch.host.route = HostDecision(
        ["qwen"],
        "需要研究",
        tasks={"qwen": "分析这项需求"},
        role_changes=SessionRoleChanges(
            {"qwen": SessionRole("研究员", "先核对事实。")}, ()),
    )
    asyncio.run(orch.dispatch(
        "让 qwen 接下来担任研究员并分析需求",
        lambda _name, _event: None,
    ))
    assert orch.host.decide_calls == 1
    assert orch.session_roles["qwen"].label == "研究员"
    assert "当前聊天室会话中的临时角色：研究员" in \
        orch.adapters["qwen"].last_prompt
    print("ok  无 mention 路由在既有单次 host 调用中更新角色")


def test_persistent_orchestrator_restores_room_role() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workdir = root / "work"
        workdir.mkdir()
        state_root = root / "state"
        store = RoomStore(workdir, state_root=state_root)
        store.set_session_roles({
            "qwen": SessionRole("研究员", "恢复后仍核对事实。")})
        orch = Orchestrator(str(workdir), store=store)
        qwen = FakeAdapter("qwen")
        orch.adapters["qwen"] = qwen
        try:
            assert orch.session_roles["qwen"].label == "研究员"
            asyncio.run(orch.dispatch(
                "@qwen 继续", lambda _name, _event: None))
            assert "当前聊天室会话中的临时角色：研究员" in qwen.last_prompt
        finally:
            asyncio.run(orch.aclose())
    print("ok  重启恢复同一房间角色，不引入独立记忆系统")


def test_role_state_write_failure_prevents_worker_dispatch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workdir = root / "work"
        workdir.mkdir()
        state_root = root / "state"
        store = RoomStore(workdir, state_root=state_root)
        orch = Orchestrator(str(workdir), store=store)
        qwen = FakeAdapter("qwen")
        role_adapter = _RoleModelAdapter([
            '{"set":{"qwen":{"label":"研究员",'
            '"instructions":"核对事实。"}},"clear":[]}',
        ])
        orch.adapters["qwen"] = qwen
        orch.host = HostAgent(adapter=role_adapter, workers=["qwen"])
        orch.adapters["host"] = orch.host

        def fail_write(_data):
            raise OSError("磁盘满了（模拟）")

        store._write_state = fail_write  # type: ignore[method-assign]
        try:
            asyncio.run(orch.dispatch(
                "@qwen 接下来担任研究员并分析需求",
                lambda _name, _event: None,
            ))
            raise AssertionError("角色落盘失败必须阻止 worker 派发")
        except OSError:
            pass
        finally:
            asyncio.run(orch.aclose())
        assert orch.session_roles == {}
        assert qwen.last_prompt is None
        assert RoomStore(
            workdir, state_root=state_root).get_session_roles() == {}
    print("ok  角色落盘失败不更新内存，也不派发 worker")


def test_session_role_remains_visible_through_activity_updates() -> None:
    feed = ActivityFeed()
    feed.begin("command-role")
    feed.record_status(
        "command-role",
        "qwen",
        "准备执行",
        session_role="产品研究员",
    )
    feed.record_tool(
        "command-role",
        "qwen",
        "tool-1",
        "读取资料",
        status="completed",
    )
    feed.record_status(
        "command-role", "qwen", "本轮响应结束", state="completed")
    rendered_feed = feed.render("command-role", expanded=False)
    assert "qwen · 产品研究员（本会话）：本轮响应结束" in rendered_feed

    progress = TaskProgress("command-role")
    progress.set_agent(
        "qwen", "running", "准备执行", session_role="产品研究员")
    progress.set_agent("qwen", "completed", "本轮响应结束")
    rendered_progress = progress.render(2)
    assert "qwen · 产品研究员（本会话） 完成" in rendered_progress
    print("ok  活动卡与任务状态持续显示会话级角色")


if __name__ == "__main__":
    test_role_model_output_is_bounded_to_fixed_candidates()
    test_room_store_session_roles_roundtrip_and_atomic_failure()
    test_session_roles_are_isolated_by_named_room_and_fail_on_corruption()
    test_host_extracts_roles_without_changing_fixed_targets()
    test_unhashable_clear_does_not_turn_route_into_host_answer()
    test_explicit_role_survives_session_until_natural_language_clear()
    test_host_route_reuses_single_call_for_role_changes()
    test_persistent_orchestrator_restores_room_role()
    test_role_state_write_failure_prevents_worker_dispatch()
    test_session_role_remains_visible_through_activity_updates()

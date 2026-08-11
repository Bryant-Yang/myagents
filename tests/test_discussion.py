"""Bounded multi-agent discussion parser and orchestration tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import AgentEvent
from control import CommandBus, CommandStatus
from discussion import (
    DiscussionValidationError,
    parse_discussion_request,
)
from orchestrator import Orchestrator
from session_roles import SessionRole, SessionRoleChanges


WORKERS = ("kimi", "opencode", "codex")


def raises_validation(text: str) -> str:
    try:
        parse_discussion_request(text, WORKERS)
    except DiscussionValidationError as exc:
        return str(exc)
    raise AssertionError(f"expected DiscussionValidationError: {text!r}")


def test_parser_bounds_and_command_boundary() -> None:
    request = parse_discussion_request(
        "/discuss @kimi @opencode --rounds 2 --moderator codex -- "
        "新增 adapter 的协议选择",
        WORKERS,
    )
    assert request is not None
    assert request.participants == ("kimi", "opencode")
    assert request.rounds == 2
    assert request.moderator == "codex"
    assert request.topic == "新增 adapter 的协议选择"

    multiline = parse_discussion_request(
        "/discuss @kimi @opencode\n第一行\n第二行", WORKERS)
    assert multiline is not None
    assert multiline.rounds == 2 and multiline.moderator == "host"
    assert multiline.topic == "第一行\n第二行"

    assert parse_discussion_request("/discussion @kimi @opencode", WORKERS) \
        is None
    assert parse_discussion_request("请讨论 @kimi @opencode", WORKERS) is None
    assert "主题不能为空" in raises_validation(
        "/discuss @kimi @opencode --")
    assert "参与者数量" in raises_validation(
        "/discuss @kimi -- 主题")
    try:
        parse_discussion_request(
            "/discuss @kimi @opencode @codex @four -- 主题",
            WORKERS + ("four",),
        )
        raise AssertionError("four participants should fail")
    except DiscussionValidationError as exc:
        assert "参与者数量" in str(exc)
    assert "未知参与者" in raises_validation(
        "/discuss @kimi @ghost -- 主题")
    assert "参与者重复" in raises_validation(
        "/discuss @kimi @kimi -- 主题")
    assert "轮数必须" in raises_validation(
        "/discuss @kimi @opencode --rounds 0 -- 主题")
    assert "轮数必须" in raises_validation(
        "/discuss @kimi @opencode --rounds 4 -- 主题")
    assert "主题不能超过" in raises_validation(
        "/discuss @kimi @opencode -- " + "x" * 3001)
    assert "不能同时作为参与者" in raises_validation(
        "/discuss @kimi @opencode --moderator kimi -- 主题")
    assert "未知参数" in raises_validation(
        "/discuss @kimi @opencode --dynamic -- 主题")
    print("ok  /discuss parser（边界、上限、主题分隔、非命令不误判）")


class RoundBarrier:
    def __init__(self, participants: int) -> None:
        self.participants = participants
        self.arrivals: dict[int, int] = {}
        self.gates: dict[int, asyncio.Event] = {}

    async def arrive(self, round_number: int) -> None:
        gate = self.gates.setdefault(round_number, asyncio.Event())
        count = self.arrivals.get(round_number, 0) + 1
        self.arrivals[round_number] = count
        if count == self.participants:
            gate.set()
        await asyncio.wait_for(gate.wait(), timeout=1)


class RoundAdapter:
    stateful_session = True

    def __init__(self, name: str, barrier: RoundBarrier | None = None) -> None:
        self.name = name
        self.session_id = None
        self.prompts: list[str] = []
        self._barrier = barrier

    async def stream(self, prompt: str, workdir: str):
        self.prompts.append(prompt)
        round_number = len(self.prompts)
        if self._barrier is not None:
            await self._barrier.arrive(round_number)
        yield AgentEvent("text", f"{self.name}-round-{round_number}")
        yield AgentEvent("done")


class FailingAdapter(RoundAdapter):
    async def stream(self, prompt: str, workdir: str):
        self.prompts.append(prompt)
        yield AgentEvent("text", f"{self.name}-partial")
        raise RuntimeError("discussion boom")


class HangingAdapter(RoundAdapter):
    def __init__(
        self,
        name: str,
        arrivals: dict[str, int],
        started: asyncio.Event,
        expected: int,
    ) -> None:
        super().__init__(name)
        self._arrivals = arrivals
        self._started = started
        self._expected = expected

    async def stream(self, prompt: str, workdir: str):
        self.prompts.append(prompt)
        self._arrivals["n"] = self._arrivals.get("n", 0) + 1
        if self._arrivals["n"] >= self._expected:
            self._started.set()
        await asyncio.Event().wait()
        yield AgentEvent("text", "unreachable")
        yield AgentEvent("done")


class RoleDiscussionHost(RoundAdapter):
    def __init__(self) -> None:
        super().__init__("host")
        self.role_choices: list[tuple[str, ...]] = []

    async def extract_session_roles(
        self, text: str, choices: list[str], workdir: str, on_event=None
    ) -> SessionRoleChanges:
        self.role_choices.append(tuple(choices))
        return SessionRoleChanges({
            "kimi": SessionRole("正方", "提出可行路径并给出依据。"),
            "opencode": SessionRole("反方", "寻找反例与边界。"),
        }, ())


def make_orch() -> tuple[Orchestrator, dict[str, RoundAdapter]]:
    orch = Orchestrator(workdir=".", persistent=False)
    barrier = RoundBarrier(2)
    adapters = {
        "kimi": RoundAdapter("kimi", barrier),
        "opencode": RoundAdapter("opencode", barrier),
        "codex": RoundAdapter("codex"),
        "host": RoundAdapter("host"),
    }
    orch.adapters = adapters
    orch.host = adapters["host"]
    return orch, adapters


def test_bounded_rounds_share_timeline_and_finish_with_moderator() -> None:
    async def run() -> None:
        orch, adapters = make_orch()
        events: list[tuple[str, AgentEvent]] = []
        command_id = "discussion-command"
        outcome = await orch.dispatch(
            "/discuss @kimi @opencode --rounds 2 -- "
            "新增 adapter 的协议选择",
            lambda name, event: events.append((name, event)),
            command_id=command_id,
        )

        assert not outcome.failures
        assert len(adapters["kimi"].prompts) == 2
        assert len(adapters["opencode"].prompts) == 2
        assert len(adapters["host"].prompts) == 1
        assert adapters["kimi"]._barrier.arrivals == {1: 2, 2: 2}
        assert "当前是第 1/2 轮" in adapters["kimi"].prompts[0]
        assert "opencode-round-1" in adapters["kimi"].prompts[1]
        assert "kimi-round-1" in adapters["opencode"].prompts[1]
        assert "主持人本轮明确任务" in adapters["host"].prompts[0]
        assert "kimi-round-2" in adapters["host"].prompts[0]
        assert "opencode-round-2" in adapters["host"].prompts[0]
        assert "最终仲裁，不再安排任何 agent" in adapters["host"].prompts[0]

        assert orch.history[0].speaker == "user"
        assert orch.history[-1].speaker == "host"
        assert sum(message.speaker == "user" for message in orch.history) == 1
        assert sum(message.speaker == "kimi" for message in orch.history) == 2
        assert sum(message.speaker == "opencode" for message in orch.history) == 2
        assert all(message.command_id == command_id for message in orch.history)
        assert [event.kind for name, event in events if name == "kimi"].count(
            "done") == 1
        assert [event.kind for name, event in events if name == "opencode"].count(
            "done") == 1
        assert any(
            name == "system" and "讨论第 2/2 轮" in event.text
            for name, event in events
        )
        await orch.aclose()

    asyncio.run(run())
    print("ok  有界讨论（同轮并发、跨轮共享、单 user、最终 host）")


def test_failure_exits_later_rounds_but_moderator_runs_and_bus_fails() -> None:
    async def run() -> None:
        orch, adapters = make_orch()
        failing = FailingAdapter("kimi")
        good = RoundAdapter("opencode")
        adapters["kimi"] = failing
        adapters["opencode"] = good
        orch.adapters["kimi"] = failing
        orch.adapters["opencode"] = good
        bus = CommandBus(orch)
        bus.start()
        submitted = await bus.submit(
            "/discuss @kimi @opencode --rounds 3 -- 失败收口")
        terminal = await bus.wait(submitted.command_id, timeout=5)

        assert terminal["status"] == "failed"
        assert "kimi" in terminal["error"]
        assert "discussion boom" in terminal["error"]
        assert len(failing.prompts) == 1
        assert len(good.prompts) == 1
        assert len(adapters["host"].prompts) == 1
        assert "kimi-partial" in adapters["host"].prompts[0]
        assert "调用失败" in adapters["host"].prompts[0]
        assert all(
            message.command_id == submitted.command_id
            for message in orch.history
        )
        await bus.aclose()
        await orch.aclose()

    asyncio.run(run())
    print("ok  讨论失败收口（失败者不重试、host 总结、command failed）")


def test_cancel_stops_later_rounds_and_moderator() -> None:
    async def run() -> None:
        orch, adapters = make_orch()
        started = asyncio.Event()
        arrivals: dict[str, int] = {}
        hanging_a = HangingAdapter("kimi", arrivals, started, 2)
        hanging_b = HangingAdapter("opencode", arrivals, started, 2)
        adapters["kimi"] = hanging_a
        adapters["opencode"] = hanging_b
        orch.adapters["kimi"] = hanging_a
        orch.adapters["opencode"] = hanging_b
        bus = CommandBus(orch)
        bus.start()
        submitted = await bus.submit(
            "/discuss @kimi @opencode --rounds 3 -- 取消后不再继续")
        await asyncio.wait_for(started.wait(), timeout=2)
        cancelled = await bus.cancel(submitted.command_id)
        assert cancelled.status is CommandStatus.CANCELLED
        assert len(hanging_a.prompts) == 1
        assert len(hanging_b.prompts) == 1
        assert len(adapters["host"].prompts) == 0
        assert all(
            message.command_id == submitted.command_id
            for message in orch.history
        )
        assert sum(message.speaker == "user" for message in orch.history) == 1
        await bus.aclose()
        await orch.aclose()

    asyncio.run(run())
    print("ok  讨论取消收口（不进入后续轮次与 moderator）")


def test_registered_nonparticipant_can_moderate() -> None:
    async def run() -> None:
        orch, adapters = make_orch()
        outcome = await orch.dispatch(
            "/discuss @kimi @opencode --rounds 1 --moderator codex -- "
            "由第三个 worker 主持",
            lambda _name, _event: None,
            command_id="custom-moderator",
        )
        assert not outcome.failures
        assert len(adapters["codex"].prompts) == 1
        assert len(adapters["host"].prompts) == 0
        assert orch.history[-1].speaker == "codex"
        assert "最终仲裁，不再安排任何 agent" \
            not in adapters["kimi"].prompts[0]
        assert "最终仲裁，不再安排任何 agent" \
            in adapters["codex"].prompts[0]
        await orch.aclose()

    asyncio.run(run())
    print("ok  未参会的已注册 worker 可作为终局 moderator")


def test_discussion_roles_apply_to_all_rounds_without_changing_bounds() -> None:
    async def run() -> None:
        orch, adapters = make_orch()
        host = RoleDiscussionHost()
        orch.host = host
        orch.adapters["host"] = host
        adapters["host"] = host
        events: list[tuple[str, AgentEvent]] = []
        outcome = await orch.dispatch(
            "/discuss @kimi @opencode --rounds 2 -- "
            "让 kimi 担任正方、opencode 担任反方，讨论是否接入新协议",
            lambda name, event: events.append((name, event)),
            command_id="role-discussion",
        )

        assert not outcome.failures
        assert host.role_choices == [("kimi", "opencode", "host")]
        assert len(adapters["kimi"].prompts) == 2
        assert len(adapters["opencode"].prompts) == 2
        assert len(host.prompts) == 1
        assert all(
            "当前聊天室会话中的临时角色：正方" in prompt
            for prompt in adapters["kimi"].prompts
        )
        assert all(
            "当前聊天室会话中的临时角色：反方" in prompt
            for prompt in adapters["opencode"].prompts
        )
        assert "当前聊天室会话中的临时角色" not in host.prompts[0]
        assert orch.session_roles["kimi"].label == "正方"
        assert orch.session_roles["opencode"].label == "反方"
        assert any(
            name == "kimi"
            and event.meta.get("session_role") == "正方"
            for name, event in events
        )
        assert any(
            name == "opencode"
            and event.meta.get("session_role") == "反方"
            for name, event in events
        )
        await orch.aclose()

    asyncio.run(run())
    print("ok  讨论角色跨轮生效且不改变参与者、轮数和 moderator")


def test_invalid_discussion_does_not_enter_timeline() -> None:
    orch, _ = make_orch()
    try:
        asyncio.run(orch.dispatch(
            "/discuss @kimi -- 主题", lambda _name, _event: None))
        raise AssertionError("invalid discussion should fail")
    except DiscussionValidationError:
        pass
    assert orch.history == []
    asyncio.run(orch.aclose())
    print("ok  无效讨论在 timeline 写入前拒绝")


if __name__ == "__main__":
    test_parser_bounds_and_command_boundary()
    test_bounded_rounds_share_timeline_and_finish_with_moderator()
    test_failure_exits_later_rounds_but_moderator_runs_and_bus_fails()
    test_cancel_stops_later_rounds_and_moderator()
    test_registered_nonparticipant_can_moderate()
    test_discussion_roles_apply_to_all_rounds_without_changing_bounds()
    test_invalid_discussion_does_not_enter_timeline()
    print("\n/discuss 全部通过")

"""Natural-language ordered collaboration contract tests.

All tests use in-memory fake adapters.  No installed agent is started.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collaboration import (
    CollaborationPlan,
    CollaborationStep,
    CollaborationValidationError,
    has_ordered_collaboration_cue,
    parse_collaboration_payload,
)
from adapters.base import AgentEvent
from control import CommandBus, CommandStatus
from host import HostAgent, HostCollaboration, HostDecision
from orchestrator import Orchestrator
from session_roles import SessionRole, SessionRoleChanges


WORKERS = ("kimi", "opencode", "qwen")


def test_plan_parser_preserves_bounded_order() -> None:
    plan = parse_collaboration_payload(
        {
            "steps": [
                {"agent": "kimi", "assignment": "调查现状并列出证据"},
                {"agent": "opencode", "assignment": "基于调查结果提出方案"},
                {"agent": "qwen", "assignment": "复核方案并给出最终交付"},
            ]
        },
        WORKERS,
    )

    assert [step.agent for step in plan.steps] == [
        "kimi", "opencode", "qwen"]
    assert plan.steps[-1].assignment == "复核方案并给出最终交付"


def test_explicit_mentions_only_trigger_on_clear_order_cue() -> None:
    targets = ["kimi", "opencode"]
    assert has_ordered_collaboration_cue(
        "先让 @kimi 调研，再让 @opencode 根据结果给最终方案", targets)
    assert has_ordered_collaboration_cue(
        "@kimi 调研现状，然后 @opencode 基于结果复核", targets)
    assert not has_ordered_collaboration_cue(
        "@kimi @opencode 一起分析并分别回答", targets)
    assert not has_ordered_collaboration_cue(
        "@kimi @opencode 一起分析，先读代码再输出各自结论", targets)
    assert has_ordered_collaboration_cue(
        "先让 @kimi 提方案，再让 @opencode 分别从安全和性能复核并交付",
        targets,
    )
    assert not has_ordered_collaboration_cue("先让 @kimi 调研", ["kimi"])


def test_host_decide_returns_ordered_plan() -> None:
    class RouteAdapter:
        session_id = None

        async def stream(self, prompt: str, workdir: str):
            yield AgentEvent(
                "text",
                '{"collaboration":{"steps":['
                '{"agent":"kimi","assignment":"调查并给出证据"},'
                '{"agent":"opencode","assignment":"基于前序证据给最终方案"}'
                ']},"reason":"需要串行接力"}',
            )
            yield AgentEvent("done")

    host = HostAgent(RouteAdapter(), workers=["kimi", "opencode"])
    decision = asyncio.run(host.decide(
        "[user] 请先调查，再基于结果设计", ".",
        choices=["kimi", "opencode"],
    ))

    assert decision.answer is None and decision.targets == []
    assert decision.collaboration is not None
    assert decision.collaboration.participants == ("kimi", "opencode")
    assert "四种结果" in host._build_route_prompt(
        "[user] 做任务", ["kimi", "opencode"])


def test_explicit_plan_extraction_keeps_mention_closure() -> None:
    class ExtractionAdapter:
        session_id = None

        def __init__(self) -> None:
            self.prompt = ""

        async def stream(self, prompt: str, workdir: str):
            self.prompt = prompt
            yield AgentEvent(
                "text",
                '{"steps":['
                '{"agent":"opencode","assignment":"先检查现状"},'
                '{"agent":"kimi","assignment":"根据检查结果完成交付"}'
                ']}',
            )
            yield AgentEvent("done")

    adapter = ExtractionAdapter()
    host = HostAgent(adapter, workers=["kimi", "opencode", "qwen"])
    plan = asyncio.run(host.extract_collaboration_plan(
        "先让 @opencode 检查，再让 @kimi 完成",
        ["opencode", "kimi"],
        ".",
    ))

    assert plan.participants == ("opencode", "kimi")
    assert "只能使用全部候选 agent" in adapter.prompt
    assert "qwen" not in adapter.prompt


def test_plan_parser_rejects_untrusted_shapes() -> None:
    invalid = (
        {"steps": [{"agent": "kimi", "assignment": "只有一步"}]},
        {"steps": [
            {"agent": "kimi", "assignment": "1"},
            {"agent": "kimi", "assignment": "2"},
        ]},
        {"steps": [
            {"agent": "kimi", "assignment": "1"},
            {"agent": "ghost", "assignment": "2"},
        ]},
        {"steps": [
            {"agent": "kimi", "assignment": "1"},
            {"agent": "opencode", "assignment": "   "},
        ]},
        {"steps": [
            {"agent": "kimi", "assignment": "1"},
            {"agent": "opencode", "assignment": "2"},
            {"agent": "qwen", "assignment": "3"},
            {"agent": "kimi", "assignment": "4"},
            {"agent": "opencode", "assignment": "5"},
        ]},
    )
    for payload in invalid:
        try:
            parse_collaboration_payload(payload, WORKERS)
        except CollaborationValidationError:
            continue
        raise AssertionError(f"invalid plan was accepted: {payload!r}")

    try:
        parse_collaboration_payload(
            {"steps": [
                {"agent": "kimi", "assignment": "1"},
                {"agent": "opencode", "assignment": "2"},
            ]},
            WORKERS,
            require_all=("kimi", "opencode", "qwen"),
        )
    except CollaborationValidationError:
        pass
    else:
        raise AssertionError("explicit mention omission was accepted")


class RecordingAdapter:
    session_id = None

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls
        self.prompts: list[str] = []

    async def stream(self, prompt: str, workdir: str):
        self.calls.append(self.name)
        self.prompts.append(prompt)
        yield AgentEvent("text", f"{self.name}-result")
        yield AgentEvent("done")


class PlanHost(RecordingAdapter):
    def __init__(
        self,
        calls: list[str],
        plan: CollaborationPlan,
        role_changes: SessionRoleChanges | None = None,
    ) -> None:
        super().__init__("host", calls)
        self.plan = plan
        self.role_changes = role_changes or SessionRoleChanges.empty()
        self.decide_calls = 0
        self.extract_calls = 0

    async def decide(
        self, transcript: str, workdir: str, on_event=None, *, choices=None,
    ) -> HostDecision:
        self.decide_calls += 1
        return HostDecision([], "有序接力", collaboration=self.plan)

    async def extract_collaboration_plan(
        self, text: str, choices: list[str], workdir: str, on_event=None,
    ) -> CollaborationPlan:
        self.extract_calls += 1
        return self.plan

    async def extract_collaboration(
        self, text: str, choices: list[str], workdir: str, on_event=None,
    ) -> HostCollaboration:
        self.extract_calls += 1
        return HostCollaboration(self.plan, self.role_changes)


def make_plan_orchestrator(plan: CollaborationPlan):
    calls: list[str] = []
    orch = Orchestrator(workdir=".", persistent=False)
    adapters = {
        name: RecordingAdapter(name, calls)
        for name in (
            "kimi", "opencode", "qwen", "workbuddy", "dsh", "pi", "codex"
        )
    }
    host = PlanHost(calls, plan)
    adapters["host"] = host
    orch.adapters = adapters
    orch.host = host
    return orch, adapters, host, calls


def test_natural_language_plan_runs_strictly_in_order_with_shared_results() -> None:
    plan = CollaborationPlan((
        CollaborationStep("kimi", "调查并给出证据"),
        CollaborationStep("opencode", "基于前序证据设计"),
        CollaborationStep("qwen", "复核并给用户最终交付"),
    ))
    orch, adapters, host, calls = make_plan_orchestrator(plan)
    events: list[tuple[str, AgentEvent]] = []
    outcome = asyncio.run(orch.dispatch(
        "请先调查，再设计，最后复核交付",
        lambda name, event: events.append((name, event)),
        command_id="collab-1",
    ))

    assert outcome.failures == ()
    assert host.decide_calls == 1
    assert calls == ["kimi", "opencode", "qwen"]
    assert "kimi-result" in adapters["opencode"].prompts[0]
    assert "opencode-result" in adapters["qwen"].prompts[0]
    assert [message.speaker for message in orch.history] == [
        "user", "kimi", "opencode", "qwen"]
    assert {message.command_id for message in orch.history} == {"collab-1"}
    step_events = [
        event for _name, event in events
        if event.meta.get("collaboration_step")
    ]
    assert {event.meta["collaboration_step"] for event in step_events} == {
        1, 2, 3}


def test_explicit_order_uses_fixed_mentions_but_together_stays_fanout() -> None:
    plan = CollaborationPlan((
        CollaborationStep("kimi", "先调查"),
        CollaborationStep("opencode", "再完成最终交付"),
    ))
    orch, _adapters, host, calls = make_plan_orchestrator(plan)
    asyncio.run(orch.dispatch(
        "先让 @kimi 调查，再让 @opencode 基于结果交付",
        lambda _name, _event: None,
    ))
    assert host.decide_calls == 0 and host.extract_calls == 1
    assert calls == ["kimi", "opencode"]

    orch, _adapters, host, calls = make_plan_orchestrator(plan)
    asyncio.run(orch.dispatch(
        "先让 @kimi 提方案，再让 @opencode 讨论风险并复核交付",
        lambda _name, _event: None,
    ))
    assert host.decide_calls == 0 and host.extract_calls == 1
    assert calls == ["kimi", "opencode"]

    orch, _adapters, host, calls = make_plan_orchestrator(plan)
    asyncio.run(orch.dispatch(
        "@kimi @opencode 一起分析并分别回答",
        lambda _name, _event: None,
    ))
    assert host.decide_calls == 0 and host.extract_calls == 0
    assert set(calls) == {"kimi", "opencode"}


def test_collaboration_failure_stops_before_later_steps() -> None:
    class FailingAdapter(RecordingAdapter):
        async def stream(self, prompt: str, workdir: str):
            self.calls.append(self.name)
            self.prompts.append(prompt)
            raise RuntimeError("step failed")
            yield  # pragma: no cover

    plan = CollaborationPlan((
        CollaborationStep("kimi", "调查"),
        CollaborationStep("opencode", "设计"),
        CollaborationStep("kimi", "根据复核最终修订"),
    ))
    orch, adapters, _host, calls = make_plan_orchestrator(plan)
    adapters["opencode"] = FailingAdapter("opencode", calls)
    orch.adapters = adapters
    events: list[tuple[str, AgentEvent]] = []

    outcome = asyncio.run(orch.dispatch(
        "请按顺序协作",
        lambda name, event: events.append((name, event)),
    ))

    assert calls == ["kimi", "opencode"]
    assert len(outcome.failures) == 1
    assert outcome.failures[0].agent == "opencode"
    assert "step failed" in outcome.failures[0].error
    assert any(
        event.meta.get("phase") == "协作已停止"
        for _name, event in events
    )
    skipped = [
        (name, event) for name, event in events
        if event.meta.get("agent_state") == "skipped"
    ]
    assert [(name, event.meta.get("collaboration_step"))
            for name, event in skipped] == [("kimi", 3)]
    assert skipped[0][1].meta.get("phase") == "因前序失败未执行"
    assert skipped[0][1].meta.get(
        "collaboration_preserve_agent_state") is True


def test_ordered_collaboration_applies_explicit_session_roles_in_same_call() -> None:
    plan = CollaborationPlan((
        CollaborationStep("kimi", "先调查"),
        CollaborationStep("opencode", "再审查并交付"),
    ))
    orch, adapters, host, _calls = make_plan_orchestrator(plan)
    host.role_changes = SessionRoleChanges({
        "kimi": SessionRole("研究员", "只陈述有来源的事实。"),
    }, ())

    asyncio.run(orch.dispatch(
        "先让 @kimi 在本会话担任研究员并调查，再让 @opencode 审查",
        lambda _name, _event: None,
    ))

    assert host.extract_calls == 1
    assert orch.session_roles["kimi"].label == "研究员"
    assert "当前聊天室会话中的临时角色：研究员" \
        in adapters["kimi"].prompts[0]


def test_command_cancel_stops_current_and_future_collaboration_steps() -> None:
    class HangingAdapter(RecordingAdapter):
        def __init__(
            self, name: str, calls: list[str], started: asyncio.Event,
        ) -> None:
            super().__init__(name, calls)
            self.started = started

        async def stream(self, prompt: str, workdir: str):
            self.calls.append(self.name)
            self.prompts.append(prompt)
            self.started.set()
            await asyncio.Event().wait()
            yield AgentEvent("text", "unreachable")

    async def run() -> None:
        plan = CollaborationPlan((
            CollaborationStep("kimi", "调查"),
            CollaborationStep("opencode", "设计"),
            CollaborationStep("kimi", "最终修订"),
        ))
        orch, adapters, _host, calls = make_plan_orchestrator(plan)
        started = asyncio.Event()
        adapters["kimi"] = HangingAdapter("kimi", calls, started)
        orch.adapters = adapters
        events: list[tuple[str, AgentEvent]] = []
        bus = CommandBus(
            orch, lambda name, event: events.append((name, event)))
        bus.start()
        submitted = await bus.submit("请先调查，然后设计，最后复核")
        await asyncio.wait_for(started.wait(), timeout=1)

        cancelled = await bus.cancel(submitted.command_id)

        assert cancelled.status is CommandStatus.CANCELLED
        assert calls == ["kimi"]
        assert not adapters["opencode"].prompts
        assert sum(message.speaker == "user" for message in orch.history) == 1
        skipped = [
            (name, event) for name, event in events
            if event.meta.get("agent_state") == "skipped"
        ]
        assert [(name, event.meta.get("collaboration_step"))
                for name, event in skipped] == [
            ("opencode", 2), ("kimi", 3)]
        assert all(
            event.meta.get("phase") == "因任务取消未执行"
            for _name, event in skipped
        )
        assert any(
            name == "kimi"
            and event.meta.get("agent_state") == "cancelled"
            and event.meta.get("phase") == "当前步骤已取消"
            for name, event in events
        )
        assert skipped[1][1].meta.get(
            "collaboration_preserve_agent_state") is True
        await bus.aclose()
        await orch.aclose()

    asyncio.run(run())


def test_too_many_explicit_participants_fail_before_timeline() -> None:
    plan = CollaborationPlan((
        CollaborationStep("kimi", "unused"),
        CollaborationStep("opencode", "unused"),
    ))
    orch, _adapters, _host, _calls = make_plan_orchestrator(plan)
    try:
        asyncio.run(orch.dispatch(
            "先让 @kimi @opencode @qwen @workbuddy @codex 调查，再统一交付",
            lambda _name, _event: None,
        ))
    except CollaborationValidationError as exc:
        assert "最多点名 4 个" in str(exc)
    else:
        raise AssertionError("five explicit collaboration participants accepted")
    assert orch.history == []


def test_invalid_host_plan_fails_closed_without_worker_fallback() -> None:
    class InvalidPlanAdapter:
        session_id = None

        async def stream(self, prompt: str, workdir: str):
            yield AgentEvent(
                "text",
                '{"collaboration":{"steps":['
                '{"agent":"kimi","assignment":"one"},'
                '{"agent":"ghost","assignment":"two"}'
                ']}}',
            )

    plan = CollaborationPlan((
        CollaborationStep("kimi", "unused"),
        CollaborationStep("opencode", "unused"),
    ))
    orch, adapters, _host, calls = make_plan_orchestrator(plan)
    host = HostAgent(InvalidPlanAdapter(), workers=["kimi", "opencode"])
    orch.host = host
    adapters["host"] = host
    orch.adapters = adapters

    outcome = asyncio.run(orch.dispatch(
        "请安排多个智能体接力完成",
        lambda _name, _event: None,
    ))

    assert len(outcome.failures) == 1
    assert outcome.failures[0].agent == "host"
    assert calls == []
    assert [message.speaker for message in orch.history] == ["user"]


def test_malformed_collaboration_json_is_not_presented_as_host_answer() -> None:
    host = HostAgent(workers=["kimi", "opencode"])
    malformed = (
        '{"collaboration":',
        'prefix {"collaboration":{"steps":[}',
        '{"collaboration":{"steps":[],},"reason":"x"}',
        '{"collaboration"}',
        '{"collaboration"',
        "{'collaboration'}",
        '{"collaboration" {"steps":[]}}',
        '{collaboration:{steps:[]}}',
        'prefix { collaboration: {steps: []} }',
    )
    for raw in malformed:
        try:
            host._parse(raw, ["kimi", "opencode"])
        except CollaborationValidationError:
            continue
        raise AssertionError(f"malformed collaboration became answer: {raw!r}")


if __name__ == "__main__":
    test_plan_parser_preserves_bounded_order()
    test_explicit_mentions_only_trigger_on_clear_order_cue()
    test_host_decide_returns_ordered_plan()
    test_explicit_plan_extraction_keeps_mention_closure()
    test_plan_parser_rejects_untrusted_shapes()
    test_natural_language_plan_runs_strictly_in_order_with_shared_results()
    test_explicit_order_uses_fixed_mentions_but_together_stays_fanout()
    test_collaboration_failure_stops_before_later_steps()
    test_ordered_collaboration_applies_explicit_session_roles_in_same_call()
    test_command_cancel_stops_current_and_future_collaboration_steps()
    test_too_many_explicit_participants_fail_before_timeline()
    test_invalid_host_plan_fails_closed_without_worker_fallback()
    test_malformed_collaboration_json_is_not_presented_as_host_answer()
    print("ok  collaboration plan parser preserves order")

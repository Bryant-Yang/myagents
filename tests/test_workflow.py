"""ADR-0009 有界 workflow 合同验收。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import AgentEvent, ExecutionMode
from control import CommandBus
from orchestrator import AgentSpec, Orchestrator
from storage.store import RoomStore
from workflow import (
    MAX_STEERING_ITEMS,
    MilestoneWorkflow,
    StageDelivery,
    WorkflowValidationError,
    parse_stage_result,
    parse_steer_instruction,
    parse_workflow_request,
)
from workspace import (
    GitWorkspaceInspector,
    WorkspaceSnapshot,
    WorkspaceValidationError,
)


def envelope(stage: str, status: str, findings: list[str] | None = None) -> str:
    values = ",".join(f'"{item}"' for item in (findings or []))
    return (
        f"{stage} 正文\n"
        f'MYAGENTS_WORKFLOW {{"stage":"{stage}","status":"{status}",'
        f'"findings":[{values}]}}'
    )


def snapshot(fingerprint: str) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        "/repo", "a" * 40, "main", fingerprint, True)


class FakeInspector:
    def __init__(self) -> None:
        self.baseline = snapshot("baseline")
        self.current = self.baseline
        self.candidate_number = 0
        self.assertions: list[str] = []

    async def capture_baseline(self, workdir: str) -> WorkspaceSnapshot:
        return self.baseline

    async def capture_candidate(
        self, baseline: WorkspaceSnapshot,
    ) -> WorkspaceSnapshot:
        assert baseline == self.baseline
        self.candidate_number += 1
        self.current = snapshot(f"candidate-{self.candidate_number}")
        return self.current

    async def assert_unchanged(self, expected: WorkspaceSnapshot) -> None:
        self.assertions.append(expected.fingerprint)
        if self.current != expected:
            raise WorkspaceValidationError(
                f"drift: {self.current.fingerprint} != {expected.fingerprint}")


def test_parser_and_result_envelope() -> None:
    workers = ("kimi", "opencode", "codex")
    request = parse_workflow_request(
        "/workflow --implementer @kimi --reviewer @host "
        "--verifier @opencode -- 修复并验收",
        workers,
    )
    assert request is not None
    assert request.roles == {
        "reviewer": "host",
        "implementer": "kimi",
        "verifier": "opencode",
    }
    assert parse_workflow_request("/workflow-not-a-command", workers) is None
    assert parse_steer_instruction("/steer -- 加一条回归测试") == "加一条回归测试"
    result = parse_stage_result(
        envelope("verify", "changes_requested", ["F-1"]), "verify")
    assert result.findings == ("F-1",)

    invalid = (
        "/workflow --reviewer @kimi --implementer @kimi -- 目标",
        "/workflow --reviewer @ghost --implementer @kimi -- 目标",
        "/workflow --reviewer @host --implementer @kimi 目标",
    )
    for value in invalid:
        try:
            parse_workflow_request(value, workers)
        except WorkflowValidationError:
            pass
        else:
            raise AssertionError(value)
    for value in (
        "没有信封",
        envelope("verify", "pass") + "\n尾行",
        envelope("review", "ready"),
        'MYAGENTS_WORKFLOW {"stage":"verify","status":"pass",'
        '"findings":[],"extra":true}',
        'MYAGENTS_WORKFLOW {"stage":"verify","status":"blocked",'
        '"status":"pass","findings":[]}',
    ):
        try:
            parse_stage_result(value, "verify")
        except WorkflowValidationError:
            pass
        else:
            raise AssertionError(value)
    print("ok  workflow parser 与严格结果信封")


def test_state_machine_repair_and_boundary_steering() -> None:
    async def run() -> None:
        request = parse_workflow_request(
            "/workflow --reviewer @codex --implementer @kimi "
            "--verifier @opencode -- 完成小任务",
            ("kimi", "opencode", "codex"),
        )
        assert request is not None
        inspector = FakeInspector()
        calls: list[tuple[str, str, ExecutionMode, str]] = []
        review_started = asyncio.Event()
        release_review = asyncio.Event()
        statuses = {
            "review": "ready",
            "implement": "completed",
            "verify": "changes_requested",
            "repair": "completed",
            "reverify": "pass",
        }

        async def stage_runner(agent, stage, assignment, mode):
            calls.append((agent, stage, mode, assignment))
            if stage == "review":
                review_started.set()
                await release_review.wait()
            if stage == "final":
                return StageDelivery("最终汇总")
            return StageDelivery(envelope(stage, statuses[stage]))

        events: list[tuple[str, AgentEvent]] = []
        workflow = MilestoneWorkflow(
            request,
            "cmd-workflow",
            inspector.baseline,
            inspector,
            stage_runner,
            lambda name, event: events.append((name, event)),
        )
        task = asyncio.create_task(workflow.run())
        await review_started.wait()
        receipt = workflow.steer("新增一条针对空输入的回归测试")
        assert receipt.accepted == 1
        release_review.set()
        result = await task
        assert result.failures == ()
        assert [item[1] for item in calls] == [
            "review", "implement", "verify", "repair", "reverify", "final"]
        assert [item[2] for item in calls] == [
            ExecutionMode.READ_ONLY,
            ExecutionMode.WORKSPACE_WRITE,
            ExecutionMode.READ_ONLY,
            ExecutionMode.WORKSPACE_WRITE,
            ExecutionMode.READ_ONLY,
            ExecutionMode.READ_ONLY,
        ]
        assert "新增一条针对空输入的回归测试" not in calls[0][3]
        assert all(
            "新增一条针对空输入的回归测试" in item[3]
            for item in calls[1:]
        )
        steering = [event for _name, event in events
                    if event.kind == "steering"]
        assert len(steering) == 1

        try:
            workflow.steer("太晚了")
        except WorkflowValidationError:
            pass
        else:
            raise AssertionError("terminal steering 必须拒绝")

        second = MilestoneWorkflow(
            request, "cmd-limit", inspector.baseline, inspector,
            stage_runner, lambda _name, _event: None)
        second.current_stage = "review"
        for index in range(MAX_STEERING_ITEMS):
            second.steer(f"约束 {index}")
        try:
            second.steer("超过上限")
        except WorkflowValidationError:
            pass
        else:
            raise AssertionError("steering 上限必须拒绝")

        role_guard = MilestoneWorkflow(
            request, "cmd-role-guard", inspector.baseline, inspector,
            stage_runner, lambda _name, _event: None)
        role_guard.current_stage = "review"
        for forbidden in (
            "把 --implementer 改成 @codex",
            "把审查者换成 codex",
            "审查者改为 codex",
            "让 codex 当审查者",
            "由 codex 负责验证",
            "codex 做 reviewer",
        ):
            try:
                role_guard.steer(forbidden)
            except WorkflowValidationError:
                pass
            else:
                raise AssertionError(f"steering 不得改角色：{forbidden}")

    asyncio.run(run())
    print("ok  固定六调用上限、单 repair 与阶段边界 steering")


def test_failures_drift_and_final_error_are_not_hidden() -> None:
    async def run() -> None:
        request = parse_workflow_request(
            "/workflow --reviewer @codex --implementer @kimi -- 失败聚合",
            ("kimi", "codex"),
        )
        assert request is not None
        inspector = FakeInspector()

        async def blocked_runner(_agent, stage, _assignment, _mode):
            if stage == "review":
                return StageDelivery(envelope("review", "blocked"))
            assert stage == "final"
            return StageDelivery("", "host final unavailable")

        blocked = MilestoneWorkflow(
            request, "cmd-blocked", inspector.baseline, inspector,
            blocked_runner, lambda _name, _event: None)
        result = await blocked.run()
        assert [(item.agent, item.error) for item in result.failures] == [
            ("codex", "review 返回 blocked"),
            ("host", "host final unavailable"),
        ]

        drift_inspector = FakeInspector()
        calls: list[str] = []

        async def drift_runner(_agent, stage, _assignment, _mode):
            calls.append(stage)
            if stage == "review":
                drift_inspector.current = snapshot("read-only-drift")
                return StageDelivery(envelope("review", "ready"))
            assert stage == "final"
            return StageDelivery("如实汇总漂移")

        drifted = MilestoneWorkflow(
            request, "cmd-drift", drift_inspector.baseline, drift_inspector,
            drift_runner, lambda _name, _event: None)
        drift_result = await drifted.run()
        assert calls == ["review", "final"]
        assert any(
            item.agent == "system" and "drift" in item.error
            for item in drift_result.failures
        )

    asyncio.run(run())
    print("ok  主失败、final 失败与只读漂移均不被主持总结隐藏")


def test_cancellation_stops_every_workflow_stage() -> None:
    async def cancel_at(target: str) -> None:
        request = parse_workflow_request(
            "/workflow --reviewer @codex --implementer @kimi "
            "--verifier @opencode -- 取消验收",
            ("kimi", "codex", "opencode"),
        )
        assert request is not None
        inspector = FakeInspector()
        started = asyncio.Event()
        blocker = asyncio.Event()
        calls: list[str] = []

        async def stage_runner(_agent, stage, _assignment, _mode):
            calls.append(stage)
            if stage == target:
                started.set()
                await blocker.wait()
            if stage == "review":
                return StageDelivery(envelope(stage, "ready"))
            if stage in {"implement", "repair"}:
                return StageDelivery(envelope(stage, "completed"))
            if stage == "verify":
                status = (
                    "changes_requested"
                    if target in {"repair", "reverify"} else "pass"
                )
                return StageDelivery(envelope(stage, status))
            if stage == "reverify":
                return StageDelivery(envelope(stage, "pass"))
            return StageDelivery("final")

        workflow = MilestoneWorkflow(
            request, f"cmd-cancel-{target}", inspector.baseline,
            inspector, stage_runner, lambda _name, _event: None)
        task = asyncio.create_task(workflow.run())
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError(f"{target} 取消必须穿透")
        assert calls[-1] == target
        assert workflow.current_stage == target

    async def run() -> None:
        for stage in (
            "review", "implement", "verify", "repair", "reverify", "final",
        ):
            await cancel_at(stage)

    asyncio.run(run())
    print("ok  review/implement/verify/repair/reverify/final 均可有界取消")


def test_orchestrator_single_timeline_and_execution_modes() -> None:
    class Adapter:
        def __init__(self, name: str) -> None:
            self.name = name
            self.session_id = None
            self.calls: list[ExecutionMode] = []

        async def stream(
            self, prompt: str, workdir: str, *,
            execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
        ):
            self.calls.append(execution_mode)
            if "有界 workflow 阶段：review" in prompt:
                text = envelope("review", "ready")
            elif "有界 workflow 阶段：implement" in prompt:
                text = envelope("implement", "completed")
            elif "有界 workflow 阶段：verify" in prompt:
                text = envelope("verify", "pass")
            else:
                text = "host 最终汇总"
            yield AgentEvent("text", text)
            yield AgentEvent("done")

    async def run() -> None:
        adapters = {name: Adapter(name) for name in ("review", "write", "verify")}
        specs = tuple(
            AgentSpec(name, "jsonl", lambda item=item: item)
            for name, item in adapters.items()
        )
        inspector = FakeInspector()
        orch = Orchestrator(
            "/repo", specs=specs, persistent=False,
            workspace_inspector=inspector)
        host = Adapter("host")
        orch.host = host
        orch.adapters["host"] = host
        events: list[tuple[str, AgentEvent]] = []
        outcome = await orch.dispatch(
            "/workflow --reviewer @review --implementer @write "
            "--verifier @verify -- 验收单时间线",
            lambda name, event: events.append((name, event)),
            command_id="cmd-integration",
        )
        assert outcome.failures == ()
        users = [message for message in orch.history if message.speaker == "user"]
        assert len(users) == 1
        assert all(message.command_id == "cmd-integration" for message in orch.history)
        assert adapters["review"].calls == [ExecutionMode.READ_ONLY]
        assert adapters["write"].calls == [ExecutionMode.WORKSPACE_WRITE]
        assert adapters["verify"].calls == [ExecutionMode.READ_ONLY]
        assert host.calls == [ExecutionMode.READ_ONLY]
        assert any(event.meta.get("baseline_fingerprint") == "baseline"
                   for _name, event in events)
        assert any(
            event.kind == "status"
            and event.meta.get("workflow_stage") == "baseline"
            and "fingerprint=baseline" in event.text
            for _name, event in events
        )
        await orch.aclose()

    asyncio.run(run())
    print("ok  Orchestrator 单 user timeline 与 execution mode 透传")


def test_command_bus_persists_workflow_baseline_evidence() -> None:
    class Adapter:
        def __init__(self, name: str) -> None:
            self.name = name
            self.session_id = None

        async def stream(
            self, prompt: str, workdir: str, *,
            execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
        ):
            if "阶段：review" in prompt:
                text = envelope("review", "ready")
            elif "阶段：implement" in prompt:
                text = envelope("implement", "completed")
            elif "阶段：verify" in prompt:
                text = envelope("verify", "pass")
            else:
                text = "host 最终汇总"
            yield AgentEvent("text", text)
            yield AgentEvent("done")

        async def aclose(self) -> None:
            pass

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            adapters = {
                name: Adapter(name) for name in ("review", "write", "verify")
            }
            specs = tuple(
                AgentSpec(name, "jsonl", lambda item=item: item)
                for name, item in adapters.items()
            )
            store = RoomStore(workdir, state_root=root / "state")
            inspector = FakeInspector()
            orch = Orchestrator(
                str(workdir), specs=specs, store=store,
                workspace_inspector=inspector)
            host = Adapter("host")
            orch.host = host
            orch.adapters["host"] = host
            bus = CommandBus(orch)
            bus.start()
            submitted = await bus.submit(
                "/workflow --reviewer @review --implementer @write "
                "--verifier @verify -- 持久证据")
            result = await bus.wait(submitted.command_id, timeout=5)
            assert result["status"] == "completed", result
            persisted = store.read_events(0, 100)["items"]
            baseline_events = [
                item for item in persisted
                if item.command_id == submitted.command_id
                and item.agent == "system"
                and item.kind == "status"
                and item.text.startswith("workflow baseline")
            ]
            assert len(baseline_events) == 1
            assert "head=" + "a" * 40 in baseline_events[0].text
            assert "branch=main" in baseline_events[0].text
            assert "fingerprint=baseline" in baseline_events[0].text
            await bus.aclose()
            await orch.aclose()

    asyncio.run(run())
    print("ok  CommandBus 将 baseline HEAD/branch/fingerprint 独立持久化")


def test_orchestrator_cancel_removes_active_workflow() -> None:
    class BlockingAdapter:
        def __init__(self, name: str, started: asyncio.Event) -> None:
            self.name = name
            self.session_id = None
            self.started = started

        async def stream(
            self, prompt: str, workdir: str, *,
            execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
        ):
            self.started.set()
            await asyncio.Event().wait()
            yield AgentEvent("done")

        async def aclose(self) -> None:
            pass

    async def run() -> None:
        started = asyncio.Event()
        reviewer = BlockingAdapter("review", started)
        writer = BlockingAdapter("write", asyncio.Event())
        specs = (
            AgentSpec("review", "jsonl", lambda: reviewer),
            AgentSpec("write", "jsonl", lambda: writer),
        )
        orch = Orchestrator(
            "/repo", specs=specs, persistent=False,
            workspace_inspector=FakeInspector())
        task = asyncio.create_task(orch.dispatch(
            "/workflow --reviewer @review --implementer @write -- 取消清理",
            lambda _name, _event: None,
            command_id="cmd-orch-cancel",
        ))
        await asyncio.wait_for(started.wait(), timeout=2)
        assert "cmd-orch-cancel" in orch._active_workflows
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Orchestrator workflow 取消必须穿透")
        assert "cmd-orch-cancel" not in orch._active_workflows
        await orch.aclose()

    asyncio.run(run())
    print("ok  Orchestrator 取消后清理 active workflow owner")


def test_git_workspace_inspector_fixed_point() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(
                ["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=root, check=True)
            tracked = root / "value.txt"
            tracked.write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "value.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "baseline"], cwd=root, check=True)

            inspector = GitWorkspaceInspector()
            baseline = await inspector.capture_baseline(str(root))
            await inspector.assert_unchanged(baseline)
            tracked.write_text("two\n", encoding="utf-8")
            (root / "new.txt").write_text("new\n", encoding="utf-8")
            candidate = await inspector.capture_candidate(baseline)
            assert candidate.fingerprint != baseline.fingerprint
            await inspector.assert_unchanged(candidate)
            subprocess.run(["git", "add", "value.txt"], cwd=root, check=True)
            try:
                await inspector.capture_candidate(baseline)
            except WorkspaceValidationError as exc:
                assert "index" in str(exc)
            else:
                raise AssertionError("staged writer output 必须拒绝")

            subprocess.run(
                ["git", "reset", "-q", "HEAD", "--", "value.txt"],
                cwd=root, check=True)
            tracked.write_text("one\n", encoding="utf-8")
            (root / "new.txt").unlink()
            for flag in ("--assume-unchanged", "--skip-worktree"):
                subprocess.run(
                    ["git", "update-index", flag, "value.txt"],
                    cwd=root, check=True)
                try:
                    await inspector.capture_baseline(str(root))
                except WorkspaceValidationError as exc:
                    assert "index flags" in str(exc)
                else:
                    raise AssertionError(f"{flag} baseline 必须拒绝")
                clear = (
                    "--no-assume-unchanged"
                    if flag == "--assume-unchanged"
                    else "--no-skip-worktree"
                )
                subprocess.run(
                    ["git", "update-index", clear, "value.txt"],
                    cwd=root, check=True)

            clean = await inspector.capture_baseline(str(root))
            subprocess.run(
                ["git", "update-index", "--skip-worktree", "value.txt"],
                cwd=root, check=True)
            tracked.write_text("hidden\n", encoding="utf-8")
            try:
                await inspector.capture_candidate(clean)
            except WorkspaceValidationError as exc:
                assert "index" in str(exc)
            else:
                raise AssertionError("writer 隐藏改动必须拒绝")

        with tempfile.TemporaryDirectory() as tmp:
            try:
                await GitWorkspaceInspector().capture_baseline(tmp)
            except WorkspaceValidationError:
                pass
            else:
                raise AssertionError("非 Git 目录必须拒绝")

    asyncio.run(run())
    print("ok  Git fixed point 覆盖 tracked/untracked 并拒绝 staging")


def test_git_workspace_inspector_cancellation_reaps_process() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "pid"
            code = (
                "import os,sys,time; "
                "open(sys.argv[1], 'w').write(str(os.getpid())); "
                "time.sleep(300)"
            )
            inspector = GitWorkspaceInspector(
                git_command=(sys.executable, "-c", code, str(pid_file)),
                timeout_seconds=300,
            )
            task = asyncio.create_task(inspector.capture_baseline(str(root)))
            for _ in range(200):
                if pid_file.exists():
                    break
                await asyncio.sleep(0.01)
            assert pid_file.exists(), "fake git 未启动"
            pid = int(pid_file.read_text())
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("取消必须穿透")
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("取消后 git 进程仍存活")

    asyncio.run(run())
    print("ok  取消 baseline 会回收 Git 进程组")


if __name__ == "__main__":
    test_parser_and_result_envelope()
    test_state_machine_repair_and_boundary_steering()
    test_failures_drift_and_final_error_are_not_hidden()
    test_cancellation_stops_every_workflow_stage()
    test_orchestrator_single_timeline_and_execution_modes()
    test_command_bus_persists_workflow_baseline_evidence()
    test_orchestrator_cancel_removes_active_workflow()
    test_git_workspace_inspector_fixed_point()
    test_git_workspace_inspector_cancellation_reaps_process()
    print("\nworkflow 全部通过")

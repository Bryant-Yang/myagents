"""有界里程碑 workflow：解析、状态机、结果信封与 steering 限界。

调用方只负责提供一次 stage delivery；本模块决定角色、顺序、写权限、最多一次
repair 和最终 verdict，不递归创建 command，也不把内部 assignment 写成 user。
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from adapters.base import AgentEvent, ExecutionMode
from workspace import WorkspaceSnapshot, WorkspaceValidationError


WORKFLOW_USAGE = (
    "/workflow --reviewer @agent|@host --implementer @worker "
    "[--verifier @agent|@host] -- 任务目标"
)
STEER_USAGE = "/steer -- 给当前活动 workflow 的补充指令"
RESULT_PREFIX = "MYAGENTS_WORKFLOW "
MAX_GOAL_CHARS = 3000
MAX_RESULT_BYTES = 4096
MAX_FINDINGS = 32
MAX_FINDING_CHARS = 64
MAX_STEERING_ITEMS = 5
MAX_STEERING_CHARS = 1000
MAX_STEERING_TOTAL_CHARS = 4000
_FINDING_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ROLE_RE = re.compile(r"^@(\w+)$")
_FORBIDDEN_STEERING = re.compile(
    r"(?i)(?:--(?:reviewer|implementer|verifier)\b|"
    r"\b(?:change|replace|switch)\s+(?:reviewer|implementer|verifier)\b|"
    r"(?:把|将)?(?:审查者|审核者|reviewer|实现者|implementer|"
    r"验证者|复核者|verifier).{0,8}(?:换成|改成|改为|更换|替换|指定为|=)|"
    r"(?:让|由)\s*@?[A-Za-z0-9_-]+.{0,6}(?:当|做|担任|作为|负责)"
    r".{0,4}(?:审查|审核|review|实现|implement|修改|验证|verify|复核)|"
    r"@?[A-Za-z0-9_-]+.{0,6}(?:当|做|担任|作为)\s*"
    r"(?:审查者|审核者|reviewer|实现者|implementer|验证者|复核者|verifier)|"
    r"(?:扩大|放宽|绕过|自动批准|授权).{0,12}(?:权限|sandbox|permission)|"
    r"(?:跳过|取消).{0,8}(?:verify|复核|验证)|"
    r"(?:增加|再来|无限).{0,8}(?:repair|修复)|"
    r"no-replay)")


class WorkflowValidationError(ValueError):
    """命令、阶段结果或 steering 不满足冻结合同。"""


class WorkflowInspector(Protocol):
    async def capture_baseline(self, workdir: str) -> WorkspaceSnapshot: ...
    async def capture_candidate(
        self, baseline: WorkspaceSnapshot,
    ) -> WorkspaceSnapshot: ...
    async def assert_unchanged(
        self, expected: WorkspaceSnapshot,
    ) -> None: ...


@dataclass(frozen=True)
class WorkflowRequest:
    reviewer: str
    implementer: str
    verifier: str
    goal: str

    @property
    def roles(self) -> dict[str, str]:
        return {
            "reviewer": self.reviewer,
            "implementer": self.implementer,
            "verifier": self.verifier,
        }


@dataclass(frozen=True)
class StageDelivery:
    text: str
    error: str | None = None


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: str
    findings: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class WorkflowFailure:
    agent: str
    error: str


@dataclass(frozen=True)
class WorkflowRunResult:
    failures: tuple[WorkflowFailure, ...]
    final_stage: str


@dataclass(frozen=True)
class SteeringReceipt:
    command_id: str
    accepted: int
    total_chars: int
    applies_after: str

    def to_dict(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "accepted": self.accepted,
            "total_chars": self.total_chars,
            "applies_after": self.applies_after,
        }


@dataclass
class SteeringProposal:
    """已校验但尚未生效的 steering；由 CommandBus 持久化后提交。"""

    receipt: SteeringReceipt
    event: AgentEvent
    _commit: Callable[[], None]
    _committed: bool = False

    def commit(self) -> SteeringReceipt:
        if self._committed:
            raise WorkflowValidationError("steering proposal 已提交")
        self._commit()
        self._committed = True
        return self.receipt


StageRunner = Callable[
    [str, str, str, ExecutionMode], Awaitable[StageDelivery]
]
EventCallback = Callable[[str, AgentEvent], None]


def parse_workflow_request(
    text: str,
    workers: tuple[str, ...] | list[str],
    *,
    host_name: str = "host",
) -> WorkflowRequest | None:
    """解析精确 ``/workflow``；其他 slash 文本不误判。"""
    raw = text.strip()
    if raw == "/workflow":
        raise WorkflowValidationError(f"缺少参数；用法：{WORKFLOW_USAGE}")
    if not raw.startswith("/workflow "):
        return None
    header, separator, goal = raw.partition(" -- ")
    if not separator:
        raise WorkflowValidationError(f"缺少目标分隔符；用法：{WORKFLOW_USAGE}")
    goal = goal.strip()
    if not goal:
        raise WorkflowValidationError("workflow 任务目标不能为空")
    if len(goal) > MAX_GOAL_CHARS:
        raise WorkflowValidationError(
            f"workflow 任务目标超过 {MAX_GOAL_CHARS} 字符上限")
    try:
        tokens = shlex.split(header)
    except ValueError as exc:
        raise WorkflowValidationError(f"workflow 参数无法解析：{exc}") from exc
    if not tokens or tokens[0] != "/workflow":
        return None
    options: dict[str, str] = {}
    index = 1
    allowed = {"--reviewer", "--implementer", "--verifier"}
    while index < len(tokens):
        option = tokens[index]
        if option not in allowed:
            raise WorkflowValidationError(f"未知 workflow 参数：{option}")
        if option in options:
            raise WorkflowValidationError(f"workflow 参数重复：{option}")
        if index + 1 >= len(tokens):
            raise WorkflowValidationError(f"workflow 参数缺少值：{option}")
        options[option] = tokens[index + 1]
        index += 2
    missing = [name for name in ("--reviewer", "--implementer")
               if name not in options]
    if missing:
        raise WorkflowValidationError(
            f"缺少 workflow 参数：{', '.join(missing)}")

    worker_set = set(workers)
    all_roles = worker_set | {host_name}

    def role(option: str, allowed_names: set[str]) -> str:
        raw_value = options[option]
        match = _ROLE_RE.fullmatch(raw_value)
        if match is None or match.group(1) not in allowed_names:
            rendered = "、".join(f"@{name}" for name in sorted(allowed_names))
            raise WorkflowValidationError(
                f"{option} 必须是已注册角色：{rendered}")
        return match.group(1)

    reviewer = role("--reviewer", all_roles)
    implementer = role("--implementer", worker_set)
    verifier = (
        role("--verifier", all_roles)
        if "--verifier" in options else reviewer
    )
    if implementer in {reviewer, verifier}:
        raise WorkflowValidationError(
            "implementer 不得兼任 reviewer 或 verifier")
    return WorkflowRequest(reviewer, implementer, verifier, goal)


def parse_steer_instruction(text: str) -> str | None:
    """解析 TUI 本地 ``/steer -- ...``；其他文本不截获。"""
    raw = text.strip()
    if raw == "/steer":
        raise WorkflowValidationError(f"缺少 instruction；用法：{STEER_USAGE}")
    if not raw.startswith("/steer "):
        return None
    prefix = "/steer -- "
    if not raw.startswith(prefix):
        raise WorkflowValidationError(f"用法：{STEER_USAGE}")
    instruction = raw[len(prefix):].strip()
    if not instruction:
        raise WorkflowValidationError("steering 指令不能为空")
    return instruction


def parse_stage_result(text: str, expected_stage: str) -> StageResult:
    if text.count(RESULT_PREFIX) != 1:
        raise WorkflowValidationError(
            f"{expected_stage} 结果必须且只能包含一个 {RESULT_PREFIX.strip()} 信封")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or not lines[-1].startswith(RESULT_PREFIX):
        raise WorkflowValidationError(
            f"{expected_stage} 结果信封必须是最后一条非空行")
    envelope_line = lines[-1]
    envelope = envelope_line[len(RESULT_PREFIX):]
    if len(envelope_line.encode("utf-8")) > MAX_RESULT_BYTES:
        raise WorkflowValidationError("workflow 结果信封超过 4096 字节")
    def reject_duplicate_keys(pairs):
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise WorkflowValidationError(
                    f"workflow 结果信封包含重复字段：{key}")
            payload[key] = value
        return payload

    try:
        payload = json.loads(
            envelope, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise WorkflowValidationError(
            f"{expected_stage} 结果信封不是合法 JSON：{exc.msg}") from exc
    if not isinstance(payload, dict) or set(payload) != {
            "stage", "status", "findings"}:
        raise WorkflowValidationError(
            "workflow 结果信封必须且只能包含 stage/status/findings")
    stage = payload["stage"]
    status = payload["status"]
    findings = payload["findings"]
    if stage != expected_stage:
        raise WorkflowValidationError(
            f"结果 stage={stage!r} 与当前阶段 {expected_stage!r} 不一致")
    allowed_status = {
        "review": {"ready", "blocked"},
        "implement": {"completed", "blocked"},
        "repair": {"completed", "blocked"},
        "verify": {"pass", "changes_requested", "blocked"},
        "reverify": {"pass", "changes_requested", "blocked"},
    }[expected_stage]
    if not isinstance(status, str) or status not in allowed_status:
        raise WorkflowValidationError(
            f"{expected_stage} status 不合法：{status!r}")
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise WorkflowValidationError(
            f"findings 必须是最多 {MAX_FINDINGS} 项的数组")
    parsed: list[str] = []
    for finding in findings:
        if (not isinstance(finding, str)
                or not 1 <= len(finding) <= MAX_FINDING_CHARS
                or _FINDING_RE.fullmatch(finding) is None):
            raise WorkflowValidationError(f"finding id 不合法：{finding!r}")
        if finding in parsed:
            raise WorkflowValidationError(f"finding id 重复：{finding}")
        parsed.append(finding)
    return StageResult(stage, status, tuple(parsed), text)


class MilestoneWorkflow:
    """一个 command 的固定 workflow 实例。"""

    _STEERABLE = frozenset({"review", "implement", "repair"})

    def __init__(
        self,
        request: WorkflowRequest,
        command_id: str,
        baseline: WorkspaceSnapshot,
        inspector: WorkflowInspector,
        stage_runner: StageRunner,
        on_event: EventCallback,
    ) -> None:
        self.request = request
        self.command_id = command_id
        self.baseline = baseline
        self.inspector = inspector
        self.stage_runner = stage_runner
        self.on_event = on_event
        self.current_stage = "queued"
        self._steering: list[str] = []
        self._finished = False

    @property
    def steering_available(self) -> bool:
        return not self._finished and self.current_stage in self._STEERABLE

    def prepare_steering(self, instruction: str) -> SteeringProposal:
        if not self.steering_available:
            raise WorkflowValidationError(
                f"workflow 当前阶段 {self.current_stage} 不接受 steering")
        if not isinstance(instruction, str) or not instruction.strip():
            raise WorkflowValidationError("steering 指令不能为空")
        clean = instruction.strip()
        if len(clean) > MAX_STEERING_CHARS:
            raise WorkflowValidationError(
                f"steering 单条超过 {MAX_STEERING_CHARS} 字符上限")
        if len(self._steering) >= MAX_STEERING_ITEMS:
            raise WorkflowValidationError(
                f"steering 已达 {MAX_STEERING_ITEMS} 条上限")
        if sum(map(len, self._steering)) + len(clean) > MAX_STEERING_TOTAL_CHARS:
            raise WorkflowValidationError(
                f"steering 累计超过 {MAX_STEERING_TOTAL_CHARS} 字符上限")
        if _FORBIDDEN_STEERING.search(clean):
            raise WorkflowValidationError(
                "steering 只能补充目标、验收标准或实现约束，不能改角色/权限/流程")
        receipt = SteeringReceipt(
            self.command_id,
            len(self._steering) + 1,
            sum(map(len, self._steering)) + len(clean),
            self.current_stage,
        )
        event = AgentEvent(
            "steering",
            clean,
            {
                "workflow": True,
                "workflow_stage": self.current_stage,
                "workflow_roles": self.request.roles,
                "steering_available": True,
                **receipt.to_dict(),
            },
        )
        return SteeringProposal(
            receipt,
            event,
            lambda: self._steering.append(clean),
        )

    def steer(self, instruction: str) -> SteeringReceipt:
        """无持久层调用方的便捷入口；生产 CommandBus 使用 proposal 两阶段提交。"""
        proposal = self.prepare_steering(instruction)
        self.on_event("user", proposal.event)
        return proposal.commit()

    async def run(self) -> WorkflowRunResult:
        results: list[StageResult] = []
        failure: WorkflowFailure | None = None
        candidate = self.baseline
        verdict = "failed"
        try:
            review, failure = await self._read_stage(
                "review", self.request.reviewer, self.baseline, results)
            if failure is None and review is not None and review.status == "blocked":
                failure = WorkflowFailure(
                    self.request.reviewer, "review 返回 blocked")

            if failure is None:
                implement, failure = await self._write_stage(
                    "implement", self.request.implementer, results)
                if (failure is None and implement is not None
                        and implement.status == "blocked"):
                    failure = WorkflowFailure(
                        self.request.implementer, "implement 返回 blocked")
                if failure is None:
                    candidate = await self._inspect(
                        self.inspector.capture_candidate, self.baseline)

            verify: StageResult | None = None
            if failure is None:
                verify, failure = await self._read_stage(
                    "verify", self.request.verifier, candidate, results)
                if failure is None and verify is not None:
                    if verify.status == "pass":
                        verdict = "pass"
                    elif verify.status == "blocked":
                        failure = WorkflowFailure(
                            self.request.verifier, "verify 返回 blocked")

            if (failure is None and verify is not None
                    and verify.status == "changes_requested"):
                repair, failure = await self._write_stage(
                    "repair", self.request.implementer, results)
                if (failure is None and repair is not None
                        and repair.status == "blocked"):
                    failure = WorkflowFailure(
                        self.request.implementer, "repair 返回 blocked")
                if failure is None:
                    candidate = await self._inspect(
                        self.inspector.capture_candidate, self.baseline)
                    reverify, failure = await self._read_stage(
                        "reverify", self.request.verifier, candidate, results)
                    if failure is None and reverify is not None:
                        if reverify.status == "pass":
                            verdict = "pass"
                        else:
                            failure = WorkflowFailure(
                                self.request.verifier,
                                f"reverify 最终状态为 {reverify.status}",
                            )
            if failure is None and verdict != "pass":
                failure = WorkflowFailure(
                    self.request.verifier, "最终 verifier 未通过")
        except WorkspaceValidationError as exc:
            failure = WorkflowFailure("system", str(exc))
        except WorkflowValidationError as exc:
            failure = WorkflowFailure(
                self._agent_for_stage(self.current_stage), str(exc))

        # 取消直接穿透，不进行 final；其他失败也让 host 如实收口已有证据。
        failures: list[WorkflowFailure] = []
        if failure is not None:
            failures.append(failure)
        failures.extend(await self._final(
            results, candidate, tuple(failures), verdict))
        failures = list(dict.fromkeys(failures))
        self._finished = True
        self.current_stage = "completed" if not failures else "failed"
        self._emit_stage("system", self.current_stage, self.current_stage)
        for item in failures:
            self.on_event(item.agent, AgentEvent(
                "error",
                item.error,
                {
                    "workflow": True,
                    "workflow_stage": self.current_stage,
                    "workflow_roles": self.request.roles,
                    "workflow_agent": item.agent,
                    "steering_available": False,
                },
            ))
        for agent in dict.fromkeys(self.request.roles.values()):
            self.on_event(agent, AgentEvent(
                "status",
                "workflow 角色已收口",
                {
                    "workflow": True,
                    "workflow_stage": self.current_stage,
                    "workflow_roles": self.request.roles,
                    "workflow_agent": agent,
                    "steering_available": False,
                    "agent_state": "completed",
                    "phase": "workflow 已收口",
                },
            ))
        return WorkflowRunResult(tuple(failures), self.current_stage)

    async def _read_stage(
        self,
        stage: str,
        agent: str,
        expected: WorkspaceSnapshot,
        results: list[StageResult],
    ) -> tuple[StageResult | None, WorkflowFailure | None]:
        await self._inspect(self.inspector.assert_unchanged, expected)
        result, failure = await self._call_stage(
            stage, agent, ExecutionMode.READ_ONLY, results, expected)
        if failure is not None:
            return None, failure
        await self._inspect(self.inspector.assert_unchanged, expected)
        assert result is not None
        results.append(result)
        return result, None

    async def _write_stage(
        self,
        stage: str,
        agent: str,
        results: list[StageResult],
    ) -> tuple[StageResult | None, WorkflowFailure | None]:
        result, failure = await self._call_stage(
            stage, agent, ExecutionMode.WORKSPACE_WRITE, results, None)
        if failure is not None:
            return None, failure
        assert result is not None
        results.append(result)
        return result, None

    async def _call_stage(
        self,
        stage: str,
        agent: str,
        mode: ExecutionMode,
        results: list[StageResult],
        candidate: WorkspaceSnapshot | None,
    ) -> tuple[StageResult | None, WorkflowFailure | None]:
        self.current_stage = stage
        self._emit_stage(agent, stage, "running")
        assignment = self._assignment(stage, results, candidate)
        delivery = await self.stage_runner(agent, stage, assignment, mode)
        if delivery.error is not None:
            return None, WorkflowFailure(agent, delivery.error)
        try:
            result = parse_stage_result(delivery.text, stage)
        except WorkflowValidationError as exc:
            self.on_event(agent, AgentEvent(
                "error",
                str(exc),
                {
                    "workflow": True,
                    "workflow_stage": stage,
                    "workflow_roles": self.request.roles,
                    "workflow_agent": agent,
                    "steering_available": self.steering_available,
                },
            ))
            return None, WorkflowFailure(agent, str(exc))
        return result, None

    async def _final(
        self,
        results: list[StageResult],
        candidate: WorkspaceSnapshot,
        existing_failures: tuple[WorkflowFailure, ...],
        verdict: str,
    ) -> tuple[WorkflowFailure, ...]:
        self.current_stage = "final"
        self._emit_stage("host", "final", "running")
        failures: list[WorkflowFailure] = []
        try:
            await self._inspect(self.inspector.assert_unchanged, candidate)
        except WorkspaceValidationError as exc:
            failures.append(WorkflowFailure("system", str(exc)))
        all_before_final = (*existing_failures, *failures)
        assignment = self._final_assignment(
            results, all_before_final, verdict)
        delivery = await self.stage_runner(
            "host", "final", assignment, ExecutionMode.READ_ONLY)
        try:
            await self._inspect(self.inspector.assert_unchanged, candidate)
        except WorkspaceValidationError as exc:
            failures.append(WorkflowFailure("system", str(exc)))
        if delivery.error is not None:
            failures.append(WorkflowFailure("host", delivery.error))
        return tuple(dict.fromkeys(failures))

    def _assignment(
        self,
        stage: str,
        results: list[StageResult],
        candidate: WorkspaceSnapshot | None,
    ) -> str:
        previous = "\n\n".join(
            f"[{item.stage}]\n{item.text[:2000]}" for item in results)
        steering = "\n".join(
            f"- {item}" for item in self._steering) or "（无）"
        candidate_text = (
            f"{candidate.fingerprint}"
            if candidate is not None else "写阶段结束后由普通代码采集"
        )
        statuses = {
            "review": ("ready", "blocked"),
            "implement": ("completed", "blocked"),
            "repair": ("completed", "blocked"),
            "verify": ("pass", "changes_requested", "blocked"),
            "reverify": ("pass", "changes_requested", "blocked"),
        }[stage]
        role_instruction = {
            "review": "只读审查目标、代码和验证入口，给出稳定 finding id。",
            "implement": "作为唯一 writer 完成修改和必要验证，不 commit、不 staging。",
            "verify": "只读核对最终 diff、review findings 与实际测试证据。",
            "repair": "作为同一 writer 只修复 verifier 指出的 finding，并运行验证。",
            "reverify": "只读复核 repair 后最终 diff；这是最后一次复核。",
        }[stage]
        header = f"""有界 workflow 阶段：{stage}
任务目标：{self.request.goal}
Git baseline：{self.baseline.head} ({self.baseline.branch})
baseline fingerprint：{self.baseline.fingerprint}
candidate fingerprint：{candidate_text}
角色：reviewer={self.request.reviewer}, implementer={self.request.implementer}, verifier={self.request.verifier}

阶段要求：{role_instruction}
合法 status：{' | '.join(statuses)}
已接受、仅从本阶段开始生效的 steering：
{steering}

前序阶段证据：
"""
        footer = f"""
保留可读正文，并把下面的严格信封作为最后一条非空行；只出现一次：
{RESULT_PREFIX}{{"stage":"{stage}","status":"{statuses[0]}","findings":[]}}
status 必须从上面的合法值中选择；findings 只填稳定 id。"""
        evidence_budget = max(0, 11500 - len(header) - len(footer))
        evidence = (previous or "（无）")[:evidence_budget]
        return header + evidence + footer

    def _final_assignment(
        self,
        results: list[StageResult],
        failures: tuple[WorkflowFailure, ...],
        verdict: str,
    ) -> str:
        raw_evidence = "\n\n".join(
            f"[{item.stage}] status={item.status} findings={list(item.findings)}\n"
            f"{item.text[:1600]}"
            for item in results
        ) or "（没有成功解析的阶段结果）"
        actual = "pass" if not failures and verdict == "pass" else "failed"
        reason = "无" if not failures else "；".join(
            f"{item.agent}: {item.error}" for item in failures)
        steering = "\n".join(
            f"- {item}" for item in self._steering) or "（无）"
        header = f"""你是有界 workflow 的最终主持人，只做只读事实汇总。
任务目标：{self.request.goal}
固定角色：{self.request.roles}
普通代码裁定：{actual}
失败原因：{reason}
已接受 steering：
{steering}
阶段证据：
"""
        footer = """
请简洁总结实际完成阶段、Git/测试证据、未解决 finding 和最终结论。
不得把 failed、blocked 或 changes_requested 改写成通过。"""
        evidence_budget = max(0, 11500 - len(header) - len(footer))
        return header + raw_evidence[:evidence_budget] + footer

    def _emit_stage(self, agent: str, stage: str, state: str) -> None:
        self.on_event(agent, AgentEvent(
            "status",
            f"workflow {stage}",
            {
                "workflow": True,
                "workflow_stage": stage,
                "workflow_roles": self.request.roles,
                "workflow_agent": agent,
                "steering_available": self.steering_available,
                "agent_state": "running" if state == "running" else state,
                "phase": f"workflow {stage}",
            },
        ))

    def _agent_for_stage(self, stage: str) -> str:
        if stage in {"review"}:
            return self.request.reviewer
        if stage in {"implement", "repair"}:
            return self.request.implementer
        if stage in {"verify", "reverify"}:
            return self.request.verifier
        return "host" if stage == "final" else "system"

    @staticmethod
    async def _inspect(method: Callable, *args):
        return await method(*args)

"""Bounded, deterministic multi-agent discussion command.

``/discuss`` is parsed by ordinary code and executed by the Orchestrator.  The
models only produce each round's content; they never decide who speaks next or
whether another round should run.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Iterable


MIN_DISCUSSION_PARTICIPANTS = 2
MAX_DISCUSSION_PARTICIPANTS = 3
MIN_DISCUSSION_ROUNDS = 1
MAX_DISCUSSION_ROUNDS = 3
DEFAULT_DISCUSSION_ROUNDS = 2
DEFAULT_DISCUSSION_MODERATOR = "host"
MAX_DISCUSSION_TOPIC_CHARS = 3000

DISCUSSION_USAGE = (
    "/discuss @agent1 @agent2 [@agent3] "
    "[--rounds 1..3] [--moderator host|agent] -- 讨论主题\n"
    "（MCP/API 也可在首行参数后换行填写主题）"
)


class DiscussionValidationError(ValueError):
    """The user supplied a ``/discuss`` command with invalid structure."""


@dataclass(frozen=True)
class DiscussionRequest:
    participants: tuple[str, ...]
    rounds: int
    moderator: str
    topic: str


def parse_discussion_request(
        text: str,
        worker_names: Iterable[str],
        *,
        host_name: str = DEFAULT_DISCUSSION_MODERATOR,
) -> DiscussionRequest | None:
    """Parse one explicit bounded discussion, or return ``None``.

    Only an exact ``/discuss`` first token activates this grammar.  The first
    line contains participants/options and all following lines form the topic.
    Unknown slash text remains an ordinary chat message.
    """
    stripped = text.strip()
    if not stripped:
        return None
    first_line, separator, topic = stripped.partition("\n")
    first_token = first_line.strip().split(maxsplit=1)[0]
    if first_token != "/discuss":
        return None
    try:
        tokens = shlex.split(first_line, comments=False, posix=True)
    except ValueError as exc:
        raise DiscussionValidationError(
            f"/discuss 参数无法解析：{exc}；用法：{DISCUSSION_USAGE}"
        ) from exc
    if separator:
        header_tokens = tokens
    elif "--" in tokens:
        delimiter = tokens.index("--")
        header_tokens = tokens[:delimiter]
        topic = " ".join(tokens[delimiter + 1:])
    else:
        raise DiscussionValidationError(
            f"/discuss 必须用 -- 或换行分隔主题；用法：{DISCUSSION_USAGE}")
    if not topic.strip():
        raise DiscussionValidationError(
            f"/discuss 主题不能为空；用法：{DISCUSSION_USAGE}")
    if len(topic.strip()) > MAX_DISCUSSION_TOPIC_CHARS:
        raise DiscussionValidationError(
            f"/discuss 主题不能超过 {MAX_DISCUSSION_TOPIC_CHARS} 个字符")

    workers = tuple(dict.fromkeys(worker_names))
    valid_workers = set(workers)
    participants: list[str] = []
    rounds = DEFAULT_DISCUSSION_ROUNDS
    moderator = host_name
    seen_rounds = False
    seen_moderator = False
    index = 1
    while index < len(header_tokens):
        token = header_tokens[index]
        if token.startswith("@"):
            name = token[1:]
            if not name or name not in valid_workers:
                raise DiscussionValidationError(
                    f"/discuss 未知参与者：{token}；可选："
                    + " ".join(f"@{item}" for item in workers))
            if name in participants:
                raise DiscussionValidationError(
                    f"/discuss 参与者重复：@{name}")
            participants.append(name)
            index += 1
            continue
        if token == "--rounds":
            if seen_rounds or index + 1 >= len(header_tokens):
                raise DiscussionValidationError(
                    f"/discuss --rounds 必须且只能提供一次；用法："
                    f"{DISCUSSION_USAGE}")
            try:
                rounds = int(header_tokens[index + 1])
            except ValueError as exc:
                raise DiscussionValidationError(
                    "/discuss --rounds 必须是整数") from exc
            seen_rounds = True
            index += 2
            continue
        if token == "--moderator":
            if seen_moderator or index + 1 >= len(header_tokens):
                raise DiscussionValidationError(
                    f"/discuss --moderator 必须且只能提供一次；用法："
                    f"{DISCUSSION_USAGE}")
            moderator = header_tokens[index + 1].removeprefix("@")
            if moderator not in valid_workers | {host_name}:
                raise DiscussionValidationError(
                    f"/discuss 未知主持人：{header_tokens[index + 1]}")
            seen_moderator = True
            index += 2
            continue
        raise DiscussionValidationError(
            f"/discuss 未知参数：{token}；用法：{DISCUSSION_USAGE}")

    if not MIN_DISCUSSION_PARTICIPANTS <= len(participants) \
            <= MAX_DISCUSSION_PARTICIPANTS:
        raise DiscussionValidationError(
            "/discuss 参与者数量必须是 "
            f"{MIN_DISCUSSION_PARTICIPANTS}..{MAX_DISCUSSION_PARTICIPANTS}")
    if not MIN_DISCUSSION_ROUNDS <= rounds <= MAX_DISCUSSION_ROUNDS:
        raise DiscussionValidationError(
            "/discuss 轮数必须是 "
            f"{MIN_DISCUSSION_ROUNDS}..{MAX_DISCUSSION_ROUNDS}")
    if moderator in participants:
        raise DiscussionValidationError(
            f"/discuss 主持人 @{moderator} 不能同时作为参与者")
    return DiscussionRequest(
        participants=tuple(participants),
        rounds=rounds,
        moderator=moderator,
        topic=topic.strip(),
    )


def participant_assignment(
        request: DiscussionRequest,
        participant: str,
        round_number: int,
) -> str:
    """Build one deterministic participant instruction for a round."""
    if round_number == 1:
        stage = (
            "独立提案：独立给出你的判断、关键依据和一个可验收建议；"
            "不要假设其他参与者会同意。"
        )
    elif round_number == request.rounds:
        stage = (
            "交叉评议并收敛：阅读共享时间线里其他参与者此前的观点，"
            "指出最重要的共识或冲突，并据此修正你的建议。"
        )
    else:
        stage = (
            "交叉评议：阅读共享时间线里其他参与者此前的观点，"
            "提出一条有证据的质疑和一条可吸收的改进。"
        )
    participants = "、".join(request.participants)
    return (
        f"讨论主题：{request.topic}\n"
        f"你是参与者 {participant}；参与者为 {participants}。\n"
        f"当前是第 {round_number}/{request.rounds} 轮。{stage}\n"
        "这是只读讨论：只输出观点，不读取或修改文件，不运行命令，"
        "不调用工具、Skill 或子 agent。回复不超过 300 字。"
    )


def moderator_assignment(request: DiscussionRequest) -> str:
    """Build the terminal moderator instruction."""
    participants = "、".join(request.participants)
    return (
        f"主持一场已经结束的有界讨论。主题：{request.topic}\n"
        f"参与者：{participants}；已安排最多 {request.rounds} 轮。\n"
        "请仅依据共享时间线中本次讨论的发言，输出：共同点、关键分歧、"
        "最终建议、一个可执行验收项。若有参与者失败，明确标注证据缺口。\n"
        "这是最终仲裁，不再安排任何 agent 或新轮次；不读取或修改文件，"
        "不运行命令，不调用工具、Skill 或子 agent。回复不超过 400 字。"
    )

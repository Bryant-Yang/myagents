"""TUI 输入补全的纯数据与解析逻辑。

候选 UI 只消费这里生成的上下文；agent 名单由调用方传入当前
``Orchestrator.specs``，避免维护第二份注册表。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class LocalCommand:
    name: str
    description: str
    handler: str

    @property
    def token(self) -> str:
        return f"/{self.name}"


LOCAL_COMMANDS: tuple[LocalCommand, ...] = (
    LocalCommand("new", "新建独立会话", "action_new_session"),
    LocalCommand("sessions", "浏览、搜索和切换会话", "action_show_sessions"),
    LocalCommand("cancel", "取消当前任务", "action_cancel_active"),
    LocalCommand(
        "discuss", "查看有界多智能体讨论用法", "action_show_discuss_help"),
    LocalCommand(
        "workflow", "查看有界 review→实现→复核用法", "action_show_workflow_help"),
    LocalCommand(
        "steer", "查看运行中 workflow 补充指令用法", "action_show_steer_help"),
    LocalCommand(
        "details", "展开或收起当前活动卡", "action_toggle_details"),
    LocalCommand(
        "paste-image", "粘贴 macOS 剪贴板图片", "action_paste_image"),
    LocalCommand("agents", "查看已注册 agent", "action_show_agents"),
    LocalCommand("roles", "查看当前会话角色", "action_show_roles"),
    LocalCommand("roles clear", "清空当前会话角色", "action_clear_roles"),
    LocalCommand("help", "查看本地命令与快捷键", "action_show_help"),
)

_COMMANDS_BY_TOKEN = {command.token: command for command in LOCAL_COMMANDS}
_MENTION_AT_CURSOR_RE = re.compile(r"(?<!\w)@(\w*)$")
# 与 orchestrator.py 的路由 token 语义保持一致；例如 ``mail@kimi``
# 仍会被现有路由识别为显式 mention。
_MENTION_RE = re.compile(r"@(\w+)")
_TOKEN_TAIL_RE = re.compile(r"[\w-]*")
_SLASH_AT_CURSOR_RE = re.compile(r"^(\s*)/([\w-]*)$")


@dataclass(frozen=True)
class CompletionItem:
    value: str
    label: str
    description: str


@dataclass(frozen=True)
class CompletionContext:
    kind: str
    start: int
    end: int
    items: tuple[CompletionItem, ...]


def local_command_for(text: str) -> LocalCommand | None:
    """只识别完整注册命令；其他 slash 文本仍是普通消息。"""
    return _COMMANDS_BY_TOKEN.get(text.strip())


def unknown_mentions(text: str, valid_names: Iterable[str]) -> tuple[str, ...]:
    """返回保持输入顺序且去重的未知 ``@name``。"""
    valid = set(valid_names)
    unknown: list[str] = []
    for name in _MENTION_RE.findall(text):
        if name not in valid and name not in unknown:
            unknown.append(name)
    return tuple(unknown)


def completion_context(
        value: str,
        cursor: int,
        agents: Iterable[tuple[str, str]]) -> CompletionContext | None:
    """根据光标前的活动 token 生成 mention 或 slash 候选。"""
    cursor = max(0, min(cursor, len(value)))
    left = value[:cursor]
    tail_match = _TOKEN_TAIL_RE.match(value[cursor:])
    end = cursor + (len(tail_match.group(0)) if tail_match else 0)

    slash = _SLASH_AT_CURSOR_RE.match(left)
    if slash is not None:
        prefix = slash.group(2).lower()
        items = tuple(
            CompletionItem(
                command.token,
                command.token,
                command.description,
            )
            for command in LOCAL_COMMANDS
            if command.name.startswith(prefix)
        )
        if items:
            return CompletionContext(
                "command", len(slash.group(1)), end, items)
        return None

    mention = _MENTION_AT_CURSOR_RE.search(left)
    if mention is None:
        return None
    prefix = mention.group(1).lower()
    start = mention.start()
    outside_token = value[:start] + value[end:]
    already_selected = set(_MENTION_RE.findall(outside_token))
    items = tuple(
        CompletionItem(
            f"@{name}",
            f"@{name}",
            transport,
        )
        for name, transport in agents
        if name not in already_selected and name.lower().startswith(prefix)
    )
    if not items:
        return None
    return CompletionContext("mention", start, end, items)

"""myagents —— 终端多 agent 聊天室。

用法：
    .venv/bin/python main.py            # 在当前目录启动
    .venv/bin/python main.py /path/to/project   # 指定 agent 的工作目录

聊天室里 @kimi / @opencode / @qwen / @codebuddy / @dsh / @pi / @codex 把消息派发给对应 agent，支持一条消息
@多个（并发执行）。@host 叫当前会话选择的 HostBackend 出来总结/仲裁；不带 @
的消息由 host 用一次调用直接回答或决定派给谁。Host 可在直接模型与明确注册的
只读 agent backend 之间切换。
`/discuss` 可在一个 CommandBus command 内安排 2–3 个 worker 做 1–3 轮
有界讨论，再由指定 moderator 最终仲裁。

接入协议：可靠官方长连接优先。Kimi/OpenCode 走 ACP 长驻会话，Codex 走原生
app-server，Pi 走原生 RPC；Qwen Code/CodeBuddy 走 ACP-only。仅已证明安全的 ACP
prepare 失败才进入受限 JSONL。权限请求会弹窗交给用户决策。启动信息里
能看到每个 agent 的传输协议。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import shlex
from pathlib import Path
from typing import Callable, Iterable

from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.events import Blur, Resize
from textual.message import Message as TextualMessage
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    OptionList,
    RichLog,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from agent_readiness import (
    AgentEnablementConfig,
    AgentEnablementError,
    AgentUnavailableError,
    parse_agent_control_command,
)
from host_backend import (
    HostBackendValidationError,
    configured_default_host_backend,
    parse_host_command,
)
from context_lifecycle import (
    ContextCommandValidationError,
    ContextLifecycleError,
    parse_context_command,
)
from orchestrator import HOST_NAME, Orchestrator
from adapters.base import (
    AgentEvent,
    redact_sensitive_text,
)
from control import CommandBus, CommandNotFoundError, ControlServer
from clipboard_image import (
    ClipboardImageError,
    attachment_reference,
    capture_clipboard_png,
)
from discussion import DISCUSSION_USAGE, DiscussionValidationError
from workflow import (
    STEER_USAGE,
    WORKFLOW_USAGE,
    WorkflowValidationError,
    parse_steer_instruction,
)
from storage.store import (
    DEFAULT_SESSION_NAME,
    ExecutionEventRecord,
    RoomBusyError,
    RoomStore,
    WorkdirMismatchError,
    normalize_session_name,
    normalize_workdir,
)
from session_manager import (
    SessionManager,
    SessionNotice,
    SessionSnapshot,
    SessionTerminal,
)
from tui_completion import (
    LOCAL_COMMANDS,
    CompletionContext,
    completion_context,
    local_command_for,
    unknown_mentions,
)
from tui_status import TaskProgress, command_elapsed_seconds
from tui_markdown import render_chat_markdown
from tui_activity import ActivityDetailEvent, ActivityFeed

# 每个发言者的显示颜色
_COLORS = {"user": "yellow", "kimi": "cyan", "opencode": "green",
           "qwen": "bright_blue", "codebuddy": "bright_magenta",
           "dsh": "spring_green2",
           "pi": "deep_sky_blue1", "codex": "orange1", "host": "magenta",
           "activity": "bright_black"}

# 权限弹窗的固定应答：用户取消 / 退出 TUI 兜底
_CANCELLED = {"outcome": "cancelled"}


def _auto_approve_once(params: dict) -> dict:
    """只自动选择当次请求明确提供的 allow_once。

    不伪造 optionId，也不选 allow_always；这样模式退出后不会把
    远端 session 留在长期授权状态。
    """
    options = params.get("options")
    if not isinstance(options, list):
        return dict(_CANCELLED)
    for option in options:
        if not isinstance(option, dict) or option.get("kind") != "allow_once":
            continue
        option_id = option.get("optionId")
        if isinstance(option_id, str) and option_id:
            return {"outcome": "selected", "optionId": option_id}
    return dict(_CANCELLED)
_STREAM_RENDER_INTERVAL = 0.05
_SENSITIVE_DETAIL_KEYS = {
    "token", "secret", "password", "authorization", "api_key", "apikey",
    "credential", "private_key"}
_LEGACY_IMAGE_PREVIEW_RE = re.compile(
    r"\[图片附件：\s*[^\]\r\n]+\]"
)


def _activity_detail_events(
    records: Iterable[ExecutionEventRecord],
) -> tuple[ActivityDetailEvent, ...]:
    """把 storage record 复制成 UI 快照，避免详情层持有可变存储对象。"""
    return tuple(
        ActivityDetailEvent(
            seq=record.seq,
            agent=record.agent,
            kind=record.kind,
            text=record.text,
            created_at=record.created_at,
        )
        for record in records
    )


def _interrupted_event_summary(kind: str, text: str) -> str:
    """中断恢复不把最后一个 partial 正文复制到状态区或活动详情。"""
    if kind == "partial":
        return "上次响应在输出过程中中断"
    return redact_sensitive_text(" ".join(text.split()), limit=400)


def _safe_activity_agent(agent: str) -> str:
    return redact_sensitive_text(
        " ".join(agent.split()), limit=60) or "system"


def _tool_detail(tool: object) -> str:
    """把协议工具参数投影为可扫读字段；不直接展示原始 JSON。"""
    if not isinstance(tool, dict):
        return ""

    field_labels = {
        "path": "路径",
        "filepath": "文件",
        "file_path": "文件",
        "command": "命令",
        "cwd": "工作目录",
        "workdir": "工作目录",
        "url": "地址",
        "query": "查询",
        "pattern": "匹配",
        "content": "内容",
    }
    rows: list[str] = []

    def visit(value: object, key: str = "") -> None:
        if len(rows) >= 12:
            return
        lowered = key.lower()
        if any(marker in lowered for marker in _SENSITIVE_DETAIL_KEYS):
            rows.append(f"{key}=[已隐藏]")
            return
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                name = str(nested_key)
                if name not in {"title", "toolCallId"}:
                    visit(nested_value, name)
            return
        if isinstance(value, list):
            for item in value[:20]:
                visit(item, key)
            return
        rendered = redact_sensitive_text(str(value), limit=1200)
        if not rendered or rendered == "None":
            return
        label = field_labels.get(lowered, key or "参数")
        separator = "=" if label == key and key else "："
        rows.append(f"{label}{separator}{rendered}")

    visit(tool)
    return "\n".join(rows)[:4000]


_PERMISSION_OPTION_LABELS = {
    "allow_once": "允许一次",
    "allow_always": "始终允许（高风险）",
    "reject_once": "拒绝这次",
    "reject_always": "始终拒绝",
}


class PermissionScreen(ModalScreen):
    """ACP 权限请求弹窗：显示工具标题和 agent 给的 options，
    用户选 allow / reject / cancel，结果作为 ACP outcome 回给 agent。"""

    BINDINGS = [
        ("escape", "cancel_task", "取消任务"),
        ("ctrl+x", "cancel_task", "取消任务"),
    ]

    def __init__(
        self,
        agent_name: str,
        params: dict,
        *,
        session_id: str | None = None,
        session_label: str = "",
    ) -> None:
        super().__init__()
        self._agent_name = redact_sensitive_text(
            " ".join(agent_name.split()), limit=60) or "agent"
        self._session_id = session_id
        self._session_label = redact_sensitive_text(
            " ".join(session_label.split()), limit=200)
        tool = params.get("toolCall", {})
        if not isinstance(tool, dict):
            tool = {}
        self._title = redact_sensitive_text(
            str(tool.get("title") or "(未命名工具)"),
            limit=500,
        )
        self._detail = _tool_detail(tool)
        # optionId 不一定是合法 DOM id，用序号映射
        options = params.get("options", [])
        if not isinstance(options, list):
            options = []
        self._options = {
            f"perm-opt-{i}": opt
            for i, opt in enumerate(options)
            if isinstance(opt, dict)
        }

    def compose(self) -> ComposeResult:
        with Vertical(id="perm-dialog"):
            context = f"{self._session_label} · " if self._session_label else ""
            text = f"{context}@{self._agent_name} 请求执行\n{self._title}"
            if self._detail:
                text += f"\n{self._detail}"
            yield Label(text, id="permission-summary")
            for bid, opt in self._options.items():
                kind = str(opt.get("kind") or "")
                label = _PERMISSION_OPTION_LABELS.get(kind)
                if label is None:
                    label = redact_sensitive_text(
                        str(opt.get("name") or "使用此选项"), limit=120)
                variant = (
                    "success" if kind == "allow_once"
                    else "warning" if kind == "allow_always"
                    else "error" if kind.startswith("reject")
                    else "default"
                )
                yield Button(label, id=bid, variant=variant)
            yield Button("取消整个任务", id="perm-cancel", variant="error")
            yield Label(
                "Enter 选择 · Esc 取消整个任务",
                id="permission-help",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        opt = self._options.get(event.button.id or "")
        if opt is None:  # 取消按钮
            self.action_cancel_task()
        else:
            self.dismiss({"outcome": "selected",
                          "optionId": opt.get("optionId", "")})

    def action_cancel(self) -> None:  # Esc
        self.dismiss(dict(_CANCELLED))

    def action_cancel_task(self) -> None:
        self.app.action_cancel_session(self._session_id)
        with contextlib.suppress(Exception):
            self.dismiss(dict(_CANCELLED))


class SessionPickerScreen(ModalScreen[str | None]):
    """当前/全部项目的可搜索会话选择器。"""

    BINDINGS = [
        Binding("escape", "cancel", "关闭", priority=True),
        Binding("up", "previous", "上一个", priority=True),
        Binding("down", "next", "下一个", priority=True),
        Binding("enter", "select", "切换", priority=True),
        Binding("tab", "toggle_scope", "切换范围", priority=True),
        Binding("ctrl+n", "new", "新会话", priority=True),
        Binding("f2", "rename", "重命名", priority=True),
        Binding("ctrl+d", "delete", "删除", priority=True),
    ]

    _STATUS_LABELS = {
        "idle": "空闲",
        "queued": "排队",
        "waiting_resource": "等待资源",
        "running": "运行中",
        "waiting_permission": "等待权限",
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
    }
    _STATUS_STYLES = {
        "idle": "bright_black",
        "queued": "yellow",
        "waiting_resource": "yellow",
        "running": "bold green",
        "waiting_permission": "bold yellow",
        "completed": "green",
        "failed": "bold red",
        "cancelled": "bright_black",
    }

    def __init__(self, manager: SessionManager) -> None:
        super().__init__()
        self._manager = manager
        self._include_all = False
        self._items: tuple[SessionSnapshot, ...] = ()
        self._selected = 0
        self._selection_initialized = False
        self._option_indexes: dict[str, int] = {}
        self._item_indexes: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="session-picker"):
            with Horizontal(id="session-header"):
                yield Label("会话", id="session-heading")
                yield Label("当前项目", id="session-scope")
            yield Input(
                placeholder="搜索标题、消息或项目",
                id="session-search",
            )
            yield OptionList(id="session-options", compact=True)
            yield Label(
                "↑↓ 选择  Enter 打开  Tab 范围  Esc 关闭\n"
                "Ctrl+N 新建  F2 重命名  Ctrl+D 删除",
                id="session-help",
            )

    def on_mount(self) -> None:
        self._refresh()
        # on_mount 时 OptionList 尚可能没有最终宽度；布局完成后再按真实列宽
        # 截断两行内容，避免初次打开只显示极短摘要。
        self.call_after_refresh(self._refresh)
        self.query_one("#session-search", Input).focus()

    def on_resize(self, _: Resize) -> None:
        if self.is_mounted:
            self.call_after_refresh(self._refresh)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "session-search":
            self._selected = 0
            self._refresh()

    def _refresh(self) -> None:
        query = self.query_one("#session-search", Input).value
        items = self._manager.list_sessions(
            include_all=self._include_all,
            query=query,
        )
        if self._include_all:
            grouped: dict[str, list[SessionSnapshot]] = {}
            for item in items:
                grouped.setdefault(item.summary.workdir, []).append(item)
            items = tuple(
                item
                for workdir in sorted(grouped, key=str.casefold)
                for item in grouped[workdir]
            )
        self._items = items
        if self._items:
            if not self._selection_initialized:
                self._selected = next(
                    (
                        index for index, item in enumerate(self._items)
                        if item.summary.room_id
                        == self._manager.active_session_id
                    ),
                    0,
                )
                self._selection_initialized = True
            else:
                self._selected = max(
                    0,
                    min(self._selected, len(self._items) - 1),
                )
        else:
            self._selected = 0
        scope = "全部项目" if self._include_all else "当前项目"
        count = f"{len(self._items)} 个会话"
        self.query_one("#session-scope", Label).update(f"{scope}  ·  {count}")
        option_list = self.query_one("#session-options", OptionList)
        prompt_width = max(16, option_list.size.width - 6)
        option_list.set_options(self._render_options(prompt_width))
        self._sync_selection()

    @staticmethod
    def _activity_label(value: str | None) -> str:
        if not value:
            return "刚创建"
        compact = value[:16].replace("T", " ")
        if len(compact) == 16 and compact[4] == "-":
            return compact[5:]
        return compact

    @staticmethod
    def _preview(value: str) -> str:
        compact = _LEGACY_IMAGE_PREVIEW_RE.sub("[图片]", value)
        compact = " ".join(compact.split())
        return compact or "还没有消息"

    def _session_prompt(self, item: SessionSnapshot, width: int) -> Text:
        summary = item.summary
        current = summary.room_id == self._manager.active_session_id
        status = self._STATUS_LABELS.get(item.status, item.status)
        status_style = self._STATUS_STYLES.get(item.status, "bright_black")

        title = Text(no_wrap=True, overflow="ellipsis")
        title.append(summary.title, "bold")
        if current:
            title.append("  当前", "bold cyan")
        if item.unread:
            title.append("  ● 未读", "bold magenta")
        title.truncate(width, overflow="ellipsis")

        meta = Text(no_wrap=True, overflow="ellipsis")
        meta.append("●", status_style)
        meta.append(f" {status}", status_style)
        meta.append(
            f"  ·  {summary.message_count} 条  ·  "
            f"{self._activity_label(summary.last_active_at)}  ",
            "bright_black",
        )
        preview = self._preview(summary.last_user_message)
        preview_style = (
            "bright_black" if summary.last_user_message
            else "bright_black italic"
        )
        meta.append(preview, preview_style)
        meta.truncate(width, overflow="ellipsis")

        prompt = Text()
        prompt.append_text(title)
        prompt.append("\n")
        prompt.append_text(meta)
        return prompt

    def _render_options(self, prompt_width: int) -> list[Option | None]:
        self._option_indexes = {}
        self._item_indexes = {
            item.summary.room_id: index
            for index, item in enumerate(self._items)
        }
        if not self._items:
            return [Option("没有匹配的会话", id="empty", disabled=True)]
        options: list[Option | None] = []
        last_workdir = ""
        for item in self._items:
            summary = item.summary
            if self._include_all and summary.workdir != last_workdir:
                if options:
                    options.append(None)
                project = Text()
                project.append(summary.project_name, "bold")
                project.append(f"  {summary.workdir}", "bright_black")
                options.append(Option(
                    project,
                    id=f"project:{len(options)}",
                    disabled=True,
                ))
                last_workdir = summary.workdir
            self._option_indexes[summary.room_id] = len(options)
            options.append(Option(
                self._session_prompt(item, prompt_width),
                id=summary.room_id,
            ))
        return options

    def _sync_selection(self) -> None:
        option_list = self.query_one("#session-options", OptionList)
        if not self._items:
            option_list.highlighted = None
            return
        room_id = self._items[self._selected].summary.room_id
        option_list.highlighted = self._option_indexes[room_id]
        option_list.scroll_to_highlight()

    def selected(self) -> SessionSnapshot | None:
        if not self._items:
            return None
        return self._items[self._selected]

    def action_previous(self) -> None:
        if self._items:
            self._selected = (self._selected - 1) % len(self._items)
            self._sync_selection()

    def action_next(self) -> None:
        if self._items:
            self._selected = (self._selected + 1) % len(self._items)
            self._sync_selection()

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id != "session-options":
            return
        index = self._item_indexes.get(event.option_id or "")
        if index is not None:
            self._selected = index

    def on_option_list_option_selected(
            self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "session-options":
            return
        index = self._item_indexes.get(event.option_id or "")
        if index is not None:
            self._selected = index
            self.action_select()

    def action_select(self) -> None:
        selected = self.selected()
        if selected is not None:
            self.dismiss(selected.summary.room_id)

    def action_toggle_scope(self) -> None:
        self._include_all = not self._include_all
        self._selected = 0
        self._refresh()

    def action_new(self) -> None:
        self.dismiss("__new__")

    def action_rename(self) -> None:
        selected = self.selected()
        if selected is not None:
            self.dismiss(f"__rename__:{selected.summary.room_id}")

    def action_delete(self) -> None:
        selected = self.selected()
        if selected is not None:
            self.dismiss(f"__delete__:{selected.summary.room_id}")

    def action_cancel(self) -> None:
        self.dismiss(None)


class RenameSessionScreen(ModalScreen[str | None]):
    """只修改展示标题；稳定 session selector 不随标题变化。"""

    BINDINGS = [Binding("escape", "cancel", "取消", priority=True)]

    def __init__(self, snapshot: SessionSnapshot) -> None:
        super().__init__()
        self._snapshot = snapshot

    def compose(self) -> ComposeResult:
        with Vertical(id="session-dialog"):
            yield Label(
                f"重命名会话\n{self._snapshot.summary.project_name} · "
                f"{self._snapshot.summary.session_name}"
            )
            yield Input(
                value=self._snapshot.summary.title,
                placeholder="会话标题",
                id="session-title",
            )
            yield Button("保存", id="session-rename", variant="primary")
            yield Button("取消", id="session-rename-cancel")

    def on_mount(self) -> None:
        field = self.query_one("#session-title", Input)
        field.focus()
        field.select_all()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "session-rename":
            self.dismiss(self.query_one("#session-title", Input).value)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DeleteSessionScreen(ModalScreen[str | None]):
    """永久删除二次确认；必须逐字输入当前展示标题。"""

    BINDINGS = [Binding("escape", "cancel", "取消", priority=True)]

    def __init__(self, snapshot: SessionSnapshot) -> None:
        super().__init__()
        self._snapshot = snapshot

    def compose(self) -> ComposeResult:
        summary = self._snapshot.summary
        with Vertical(id="session-dialog"):
            yield Label(
                "永久删除会话（不可恢复）\n"
                f"{summary.project_name} / {summary.title}\n"
                f"{summary.message_count} 条消息 · "
                f"{summary.attachment_count} 个附件\n"
                f"请输入完整标题：{summary.title}"
            )
            yield Input(
                placeholder=summary.title,
                id="delete-confirmation",
            )
            yield Button("永久删除", id="session-delete", variant="error")
            yield Button("取消", id="session-delete-cancel")

    def on_mount(self) -> None:
        self.query_one("#delete-confirmation", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "session-delete":
            value = self.query_one("#delete-confirmation", Input).value
            self.dismiss(value)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ComposerInput(TextArea):
    """可增长的多行 composer：Enter 提交，Shift+Enter 换行。"""

    class Submitted(TextualMessage):
        def __init__(self, composer: "ComposerInput") -> None:
            super().__init__()
            self.input = composer
            self.value = composer.value

        @property
        def control(self) -> "ComposerInput":
            return self.input

    BINDINGS = [
        Binding("enter", "submit", "发送", show=False, priority=True),
        Binding(
            "shift+enter", "insert_newline", "换行",
            show=False, priority=True,
        ),
        Binding("up", "completion_previous", show=False, priority=True),
        Binding("down", "completion_next", show=False, priority=True),
        Binding("tab", "completion_accept", show=False, priority=True),
        Binding("alt+up", "interject", "提升排队", priority=True),
        Binding("ctrl+x", "cancel_active", "取消当前任务", priority=True),
        Binding("escape", "completion_close", show=False, priority=True),
        *TextArea.BINDINGS,
    ]

    def __init__(self, text: str = "", **kwargs) -> None:
        super().__init__(
            text,
            soft_wrap=True,
            show_line_numbers=False,
            highlight_cursor_line=False,
            tab_behavior="focus",
            **kwargs,
        )

    @property
    def value(self) -> str:
        """保留原 composer 的字符串接口，避免会话草稿层感知 TextArea。"""
        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.load_text(value)

    @property
    def cursor_position(self) -> int:
        return self.document.get_index_from_location(self.cursor_location)

    @cursor_position.setter
    def cursor_position(self, value: int) -> None:
        index = max(0, min(int(value), len(self.text)))
        self.cursor_location = self.document.get_location_from_index(index)

    @property
    def selection_offsets(self) -> tuple[int, int]:
        return (
            self.document.get_index_from_location(self.selection.start),
            self.document.get_index_from_location(self.selection.end),
        )

    def replace_offsets(self, text: str, start: int, end: int) -> None:
        start_location = self.document.get_location_from_index(start)
        end_location = self.document.get_location_from_index(end)
        self.replace(text, start_location, end_location)

    def action_completion_previous(self) -> None:
        if not self.app.move_completion(-1):
            self.action_cursor_up()

    def action_completion_next(self) -> None:
        if not self.app.move_completion(1):
            self.action_cursor_down()

    def action_completion_accept(self) -> None:
        if not self.app.accept_completion():
            self.app.action_focus_next()

    def action_completion_close(self) -> None:
        if not self.app.close_completion():
            self.app.action_cancel_active()

    def action_interject(self) -> None:
        self.app.action_interject()

    def action_cancel_active(self) -> None:
        self.app.action_cancel_active()

    def action_submit(self) -> None:
        if not self.app.accept_completion():
            self.post_message(self.Submitted(self))

    def action_insert_newline(self) -> None:
        self.insert("\n")

    def action_paste(self) -> None:
        """文本剪贴板沿用 Textual；空文本时尝试读取系统图片。"""
        if self.app.clipboard:
            super().action_paste()
        else:
            self.app.action_paste_image()


class ActivityPanel(Static):
    """独立任务过程区；聊天主线只保留用户、回复和错误。"""

    can_focus = True

    BINDINGS = [
        Binding("up", "activity_previous", show=False, priority=True),
        Binding("down", "activity_next", show=False, priority=True),
        Binding("enter", "activity_toggle", show=False, priority=True),
        Binding("escape", "activity_close", show=False, priority=True),
    ]

    def action_activity_previous(self) -> None:
        self.app.move_activity_selection(-1)

    def action_activity_next(self) -> None:
        self.app.move_activity_selection(1)

    def action_activity_toggle(self) -> None:
        self.app.action_toggle_details()

    def action_activity_close(self) -> None:
        self.app.close_activity_navigation()

    def on_blur(self, _event: Blur) -> None:
        self.app.compact_activity_navigation()


class ChatApp(App):
    BINDINGS = [
        ("ctrl+n", "new_session", "新会话"),
        ("ctrl+o", "show_sessions", "会话"),
        ("ctrl+g", "focus_activities", "活动"),
        ("ctrl+x", "cancel_active", "取消当前任务"),
        ("escape", "cancel_active", "取消当前任务"),
    ]
    CSS = """
    RichLog { border: round $primary; }
    Input { border: round $secondary; }
    #composer {
        height: 3;
        min-height: 3;
        max-height: 8;
        border: round $secondary;
        padding: 0 1;
    }
    #composer-hint {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #task-status {
        height: auto;
        min-height: 3;
        padding: 0 1;
        border: round $secondary;
        color: $text-muted;
    }
    #notice-strip {
        display: none;
        height: auto;
        max-height: 4;
        padding: 0 1;
        border-left: thick $error;
        background: $error 12%;
        color: $text;
    }
    #activity-panel {
        display: none;
        height: auto;
        max-height: 14;
        overflow-y: auto;
        padding: 0 1;
        border: round $primary-darken-1;
        color: $text-muted;
    }
    #activity-panel:focus {
        border: round $accent;
        color: $text;
    }
    #task-status.auto-approve {
        border: round $error;
        color: $warning;
    }
    #completion-list {
        display: none;
        height: auto;
        max-height: 10;
        overflow-x: hidden;
        overflow-y: auto;
        text-wrap: nowrap;
        padding: 0 1;
        border: round $accent;
        background: $surface;
    }
    PermissionScreen { align: center middle; }
    SessionPickerScreen {
        align: center middle;
        background: $background 70%;
    }
    #perm-dialog {
        width: 72; max-width: 92%; height: auto; padding: 1 2;
        border: round $warning; background: $surface;
    }
    #perm-dialog Button { width: 100%; margin-top: 1; }
    #permission-summary { margin-bottom: 1; }
    #permission-help {
        margin-top: 1;
        color: $text-muted;
        text-align: center;
    }
    #session-dialog {
        width: 60; height: auto; padding: 1 2;
        border: round $primary; background: $surface;
    }
    #session-dialog Button { width: 100%; margin-top: 1; }
    #session-picker {
        width: 94%; max-width: 104;
        height: 82%; min-height: 24;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #session-header {
        height: 1;
        margin-bottom: 1;
    }
    #session-heading {
        width: 1fr;
        text-style: bold;
    }
    #session-scope {
        width: auto;
        color: $accent;
        text-style: bold;
    }
    #session-search {
        margin-bottom: 1;
    }
    #session-options {
        height: 1fr;
        padding: 0;
        border: none;
        background: $surface-darken-1;
        overflow-y: auto;
    }
    #session-options > .option-list--option {
        padding: 0 2;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #session-options > .option-list--option-highlighted {
        background: $accent 22%;
        color: $text;
        text-style: none;
    }
    #session-options > .option-list--option-disabled {
        padding: 0 2;
        color: $text-muted;
    }
    #session-options > .option-list--separator {
        color: $surface-lighten-2;
    }
    #session-help {
        color: $text-muted;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(self, workdir: str, persistent: bool = True, *,
                 orchestrator: Orchestrator | None = None,
                 session_name: str | None = None,
                 clipboard_image_capture: Callable[
                     [Path], Path] = capture_clipboard_png) -> None:
        super().__init__()
        if orchestrator is not None:
            # 注入的 orchestrator 必须属于同一房间，不能静默换房
            if normalize_workdir(orchestrator.workdir) \
                    != normalize_workdir(workdir):
                raise WorkdirMismatchError(
                    f"orchestrator 属于 {orchestrator.workdir!r}，"
                    f"与 workdir {workdir!r} 不一致")
            requested_session = (
                orchestrator.session_name
                if session_name is None
                else normalize_session_name(session_name)
            )
            if orchestrator.session_name != requested_session:
                raise WorkdirMismatchError(
                    f"orchestrator 属于会话 {orchestrator.session_name!r}，"
                    f"与请求会话 {requested_session!r} 不一致")
            self.orch = orchestrator
        else:
            requested_session = normalize_session_name(
                session_name or DEFAULT_SESSION_NAME)
            default_host_backend = (
                configured_default_host_backend()
                if persistent else None
            )
            self.orch = Orchestrator(
                workdir,
                persistent=persistent,
                session_name=requested_session,
                discover_agents=True,
                agent_enablement=AgentEnablementConfig(),
                default_host_backend=default_host_backend,
            )
        self.session_name = self.orch.session_name
        # 持久模式由 SessionManager 集中拥有多个隔离 runtime；非持久测试仍沿用
        # 单 Orchestrator/CommandBus，不伪造可切换会话。
        self.session_manager: SessionManager | None = None
        if self.orch.store is not None:
            self.session_manager = SessionManager(
                self.orch.workdir,
                specs=self.orch.specs,
                event_sink=self._on_session_event,
                permission_handler=self._session_permission,
                notification_sink=self._on_session_notice,
                terminal_sink=self._on_session_terminal,
                initial_orchestrator=self.orch,
            )
            initial_runtime = self.session_manager.active_runtime
            self.bus = initial_runtime.bus
            self.control_server = initial_runtime.control
        else:
            self.bus = CommandBus(self.orch, self._on_agent_event)
            self.control_server = None
        # 等待用户决策的权限 Future：退出时必须全部按 cancelled 收尾，
        # 不留挂起的 Future（adapter 侧的 prompt 才不会傻等）。
        self._permission_futures: set[asyncio.Future] = set()
        self._permission_future_sessions: dict[
            asyncio.Future, str | None
        ] = {}
        # RichLog 只能 append，直接写 ACP token/chunk 会变成“一词一行”。
        # 这里保留逻辑时间线，并按最多 20fps 把同一回复更新到同一条记录。
        self._display_lines: list[tuple[str, str, str]] = []
        self._rendered_display_lines: list[Text | None] = []
        self._stream_text: dict[str, str] = {}
        self._stream_line_index: dict[str, int] = {}
        self._stream_flush_handles: dict[str, asyncio.TimerHandle] = {}
        activity_room_id = (
            self.orch.store.room_id
            if self.orch.store is not None else "__nonpersistent__"
        )
        self._activity_feeds = {activity_room_id: ActivityFeed()}
        self._activity_feed = self._activity_feeds[activity_room_id]
        self._activity_room_id = activity_room_id
        self._error_notices: dict[str, str] = {}
        # /yolo 是当前进程内、按 room 隔离的显式授权。不得写入 room state，
        # 因此重启后一定恢复默认逐次询问。
        self._auto_approve_rooms: set[str] = set()
        self._expanded_activity_ids = {activity_room_id: set()}
        self._expanded_activity = self._expanded_activity_ids[activity_room_id]
        self._selected_activity_id: str | None = None
        self._clipboard_image_capture = clipboard_image_capture
        self._image_paste_in_progress = False
        self._task_progresses: dict[str, TaskProgress] = {}
        self._latest_task_id: str | None = None
        self._completion: CompletionContext | None = None
        self._completion_index = 0
        self._suppress_completion_value: str | None = None

    def compose(self) -> ComposeResult:
        yield RichLog(wrap=True, id="chat-log")
        yield Static(id="notice-strip")
        yield ActivityPanel(id="activity-panel")
        yield Static(id="task-status")
        yield OptionList(id="completion-list", compact=True)
        yield Static(id="composer-hint")
        yield ComposerInput(
            placeholder=(
                "输入消息，@ 选择 Agent，/ 查看命令"
            ),
            id="composer",
        )

    async def on_mount(self) -> None:
        if self.session_manager is not None:
            await self.session_manager.start()
            self._bind_active_runtime()
        else:
            # 非持久单会话仍由当前 TUI 注入权限；未注入时 ACP 一律 deny。
            self.orch.set_permission_handler(self._acp_permission)
            self.bus.start()
        self.set_interval(1.0, self._render_task_status)
        if self.session_manager is not None:
            self.set_interval(30.0, self._reap_idle_sessions)
        self._render_task_status()
        if self.session_manager is None and self.control_server is not None:
            await self.control_server.start()
        # 恢复显示：按 seq 顺序渲染持久化历史（只渲染，不 append）
        restored = sorted(self.orch.history, key=lambda m: m.seq)
        for message in restored:
            self._write(message.speaker, message.text)
        if restored:
            self._system(f"已恢复 {len(restored)} 条历史消息")
        self._restore_interrupted_executions()
        self._restore_latest_collaboration_task()
        statuses = self.orch.agent_readiness_snapshot()
        ready = [item.name for item in statuses if item.ready]
        pending = [item.name for item in statuses if not item.ready]
        summary = f"聊天室已就绪：已就绪 {len(ready)}/{len(statuses)}"
        if pending:
            summary += f" · {len(pending)} 个待处理"
        summary += " · /agents 管理 · Ctrl+O 会话"
        self._system(summary)
        self._update_permission_mode_ui()
        composer = self.query_one("#composer", ComposerInput)
        self._resize_composer(composer)
        self._render_composer_hint()
        composer.focus()

    async def on_unmount(self) -> None:
        # 先放行等待中的权限请求（cancelled），再停 bus worker（取消进行
        # 中的 dispatch），最后统一回收 adapter——顺序反过来会死锁：
        # aclose 等的锁可能被等权限的 prompt 持有。bus 不拥有 orch。
        for handle in self._stream_flush_handles.values():
            handle.cancel()
        self._stream_flush_handles.clear()
        self._cancel_pending_permissions()
        try:
            if self.session_manager is not None:
                await self.session_manager.aclose()
            else:
                try:
                    if self.control_server is not None:
                        await self.control_server.aclose()
                    await self.bus.aclose()
                finally:
                    # 控制层或事件日志收尾失败，也不能跳过 adapter/进程组回收。
                    await self.orch.aclose()
        finally:
            # 退出即撤销全部高风险临时授权，即使其他收尾步骤失败也不保留。
            self._auto_approve_rooms.clear()

    def _bind_active_runtime(self) -> None:
        if self.session_manager is None:
            return
        runtime = self.session_manager.active_runtime
        previous_room_id = self._activity_room_id
        self.orch = runtime.orch
        self.bus = runtime.bus
        self.control_server = runtime.control
        self.session_name = runtime.summary.session_name
        self._activity_room_id = runtime.summary.room_id
        if previous_room_id != self._activity_room_id:
            self._selected_activity_id = None
        self._activity_feed = self._activity_feeds.setdefault(
            runtime.summary.room_id, ActivityFeed())
        self._expanded_activity = self._expanded_activity_ids.setdefault(
            runtime.summary.room_id, set())
        # 非活动房间没有可同步的 RichLog 行；只保留 feed 内仍可展开的近期卡。
        for command_id, _snapshot in self._activity_feed.take_evicted():
            self._expanded_activity.discard(command_id)
        self._discard_stale_activity_selection()
        self._update_permission_mode_ui()
        self._render_error_notice()

    def _on_session_event(
        self,
        room_id: str,
        name: str,
        event: AgentEvent,
    ) -> None:
        if self.session_manager is None:
            return
        if event.kind == "cancel_requested":
            self._cancel_permissions_for(room_id)
        if room_id == self.session_manager.active_session_id:
            self._on_agent_event(name, event)
            if event.kind == "committed" and name == "user":
                self._bind_active_runtime()
        else:
            self._record_background_activity(room_id, name, event)

    def _on_session_notice(self, notice: SessionNotice) -> None:
        if not self.is_mounted:
            return
        label = "已完成" if notice.status == "completed" else "失败"
        severity = "information" if notice.status == "completed" else "error"
        self.notify(
            f"{notice.project_name} / {notice.title}：{label}",
            severity=severity,
            timeout=5,
        )

    def _on_session_terminal(self, terminal: SessionTerminal) -> None:
        feed = self._activity_feeds.setdefault(
            terminal.session_id, ActivityFeed())
        # control 可在 agent 产生 committed 事件前取消 queued command；仍要
        # 为这条已发生的命令留下终态卡，而不是因为尚未建卡而静默丢失。
        feed.begin(terminal.command_id, terminal.status)
        elapsed = command_elapsed_seconds(
            terminal.created_at,
            terminal.finished_at,
            allow_open_interval=False,
        )
        is_active = (
            self.is_mounted
            and self.session_manager is not None
            and terminal.session_id == self.session_manager.active_session_id
            and feed is self._activity_feed
        )
        if is_active:
            self._set_command_status(
                terminal.command_id,
                terminal.status,
                terminal.error,
                elapsed_seconds=elapsed,
            )
        else:
            feed.set_command_state(
                terminal.command_id,
                terminal.status,
                terminal.error,
                elapsed_seconds=elapsed,
            )
        if terminal.status == "failed" and terminal.error:
            self._show_error_notice(
                terminal.error, room_id=terminal.session_id)

    def _record_background_activity(
        self, room_id: str, name: str, ev: AgentEvent
    ) -> None:
        """更新非活动房间的摘要模型，不把过程行写进当前 RichLog。"""
        command_value = ev.meta.get("command_id")
        if not command_value:
            return
        command_id = str(command_value)
        feed = self._activity_feeds.setdefault(room_id, ActivityFeed())
        if ev.kind == "error":
            self._show_error_notice(ev.text, room_id=room_id)
        if ev.kind == "committed" and name == "user":
            feed.mark_request_committed(command_id)
            feed.begin(command_id)
            feed.set_command_state(command_id, "running")
            roles = ev.meta.get("workflow_roles")
            if ev.meta.get("workflow") is True and isinstance(roles, dict):
                reviewer = str(roles.get("reviewer") or "")
                implementer = str(roles.get("implementer") or "")
                verifier = str(roles.get("verifier") or "")
                if implementer:
                    feed.record_status(
                        command_id, implementer, "等待实现", state="queued")
                if verifier and verifier != reviewer:
                    feed.record_status(
                        command_id, verifier, "等待复核", state="queued")
                if reviewer:
                    feed.record_status(
                        command_id, reviewer, "审查中", state="running")
                feed.record_note(
                    command_id,
                    "workflow",
                    "workflow",
                    "workflow 已创建："
                    f"审 {reviewer} → 写 {implementer} → 验 {verifier}",
                )
            else:
                targets = self.orch.parse_mentions(ev.text)
                if targets:
                    for target in targets:
                        feed.record_status(
                            command_id, target, "思考中…", state="queued")
                else:
                    feed.record_status(
                        command_id, HOST_NAME, "处理中…", state="running")
            return
        if ev.kind == "text":
            feed.record_status(command_id, name, "回复中", state="running")
        elif ev.kind == "plan":
            feed.record_plan_event(command_id, ev.text)
        elif ev.kind == "info":
            targets = ev.meta.get("route_targets")
            if (ev.meta.get("collaboration") is True
                    and isinstance(targets, list)):
                for index, target in enumerate(targets):
                    feed.record_status(
                        command_id,
                        str(target),
                        "准备执行" if index == 0 else "等待前序步骤",
                        state="running" if index == 0 else "queued",
                    )
            feed.record_note(
                command_id,
                name,
                "info",
                ev.text,
                state=(
                    "completed"
                    if name == HOST_NAME and isinstance(targets, list)
                    else "running"
                ),
            )
        elif ev.kind == "status":
            if ev.meta.get("collaboration_preserve_agent_state") is True:
                feed.record_note(
                    command_id,
                    name,
                    "collaboration",
                    ev.text,
                )
                return
            heartbeat = ev.meta.get("heartbeat") is True
            activity_agent = (
                str(ev.meta.get("phase") or "任务")
                if heartbeat and name == "system" else name
            )
            feed.record_status(
                command_id,
                activity_agent,
                ev.text,
                state=str(ev.meta.get("agent_state") or "running"),
                heartbeat=heartbeat,
                session_role=(
                    str(ev.meta["session_role"])
                    if isinstance(ev.meta.get("session_role"), str)
                    else None
                ),
            )
        elif ev.kind == "tool":
            command = ev.meta.get("command")
            tool_call_id = ev.meta.get("tool_call_id")
            detail = (
                redact_sensitive_text(command, limit=4000)
                if isinstance(command, str) else ""
            )
            feed.record_tool(
                command_id,
                name,
                str(tool_call_id or ev.text or "anonymous"),
                ev.text,
                status=str(ev.meta.get("status") or ""),
                detail=detail,
                identity_is_fallback=not bool(tool_call_id),
            )
        elif ev.kind == "permission":
            state = (
                "waiting_permission"
                if "等待权限" in ev.text else "running"
            )
            feed.record_note(
                command_id, name, "permission", ev.text, state=state)
        elif ev.kind == "cancel_requested":
            feed.set_command_state(command_id, "cancelling")
            feed.record_note(
                command_id, "system", "cancel", ev.text)
        elif ev.kind == "steering":
            feed.record_note(command_id, name, "steering", ev.text)
        elif ev.kind == "interjection":
            feed.record_note(command_id, name, "interjection", ev.text)
        elif ev.kind == "error":
            feed.record_status(command_id, name, ev.text, state="failed")
        elif ev.kind == "done":
            feed.record_status(
                command_id, name, "本轮响应结束", state="completed")

    async def _reap_idle_sessions(self) -> None:
        if self.session_manager is not None:
            # runtime 空闲回收不等于 room 被删除；这里只释放 UI runtime
            # 资源，保留当前 App 进程内的 /yolo room 状态供重新激活。
            for room_id in await self.session_manager.reap_idle():
                self._activity_feeds.pop(room_id, None)
                self._expanded_activity_ids.pop(room_id, None)

    # ---- ACP 权限决策 ----

    async def _acp_permission(self, agent_name: str, params: dict) -> dict:
        """ACP adapter 的权限决策回调：弹窗等用户选，返回 ACP outcome。"""
        return await self._permission_dialog(agent_name, params)

    async def _session_permission(
        self,
        room_id: str,
        agent_name: str,
        params: dict,
    ) -> dict:
        assert self.session_manager is not None
        summary = self.session_manager.snapshot(room_id).summary
        return await self._permission_dialog(
            agent_name,
            params,
            room_id=room_id,
            session_label=f"{summary.project_name} / {summary.title}",
            record_events=False,
        )

    async def _permission_dialog(
        self,
        agent_name: str,
        params: dict,
        *,
        room_id: str | None = None,
        session_label: str = "",
        record_events: bool = True,
    ) -> dict:
        if room_id is not None and self.session_manager is not None:
            command_id = self.session_manager.active_command_id(room_id)
        else:
            active = self.bus.active()
            command_id = active.command_id if active is not None else None
        tool = params.get("toolCall", {})
        if not isinstance(tool, dict):
            tool = {}
        title = redact_sensitive_text(
            str(tool.get("title") or "(未命名工具)"),
            limit=500,
        )
        mirrors_events = (
            params.get("_myagents_mirrors_permission_events") is True
        )
        if self._auto_approve_for(room_id):
            outcome = _auto_approve_once(params)
            selected = outcome.get("outcome") == "selected"
            detail = (
                f"自动批准权限：{outcome.get('optionId', '')}"
                if selected else "自动批准失败：请求未提供 allow_once，已拒绝"
            )
            if record_events and not mirrors_events:
                self._record_permission_event(
                    command_id, agent_name, f"{detail} · {title}")
            return outcome
        if record_events and not mirrors_events:
            self._record_permission_event(
                command_id, agent_name, f"等待权限：{title}")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._permission_futures.add(fut)
        self._permission_future_sessions[fut] = room_id
        self.push_screen(
            PermissionScreen(
                agent_name,
                params,
                session_id=room_id,
                session_label=session_label,
            ),
            lambda outcome: self._resolve_permission(fut, outcome),
        )
        try:
            outcome = await fut
            if outcome.get("outcome") == "selected":
                option_id = outcome.get("optionId", "")
                text = f"权限已选择：{option_id}"
            else:
                text = "权限已取消或拒绝"
            if record_events and not mirrors_events:
                self._record_permission_event(
                    command_id, agent_name, text)
            return outcome
        finally:
            self._permission_futures.discard(fut)
            self._permission_future_sessions.pop(fut, None)
            if not fut.done():
                fut.cancel()

    def _resolve_permission(self, fut: asyncio.Future, outcome: dict | None) -> None:
        if not fut.done():
            fut.set_result(outcome or dict(_CANCELLED))

    def _cancel_pending_permissions(self) -> None:
        for fut in list(self._permission_futures):
            if not fut.done():
                fut.set_result(dict(_CANCELLED))
        for screen in list(self.screen_stack):
            if isinstance(screen, PermissionScreen):
                with contextlib.suppress(Exception):
                    screen.dismiss(dict(_CANCELLED))

    def _cancel_permissions_for(self, room_id: str | None) -> None:
        for fut, owner in list(self._permission_future_sessions.items()):
            if owner == room_id and not fut.done():
                fut.set_result(dict(_CANCELLED))
        for screen in list(self.screen_stack):
            if isinstance(screen, PermissionScreen) \
                    and screen._session_id == room_id:
                with contextlib.suppress(Exception):
                    screen.dismiss(dict(_CANCELLED))

    def _record_permission_event(
            self, command_id: str | None, agent_name: str, text: str) -> None:
        if command_id is not None:
            state = (
                "waiting_permission"
                if text.startswith("等待权限：")
                else "running"
            )
            self._set_agent_status(command_id, agent_name, state, text)
            if self._activity_feed.record_note(
                command_id,
                agent_name,
                "permission",
                text,
                state=state,
            ):
                self._upsert_activity_card(command_id)
        else:
            self._system(f"{agent_name}: {text}")
        store = self.orch.store
        if command_id is not None and store is not None:
            store.append_event(
                command_id=command_id, agent=agent_name,
                kind="permission", text=text)

    def _restore_interrupted_executions(self) -> None:
        """恢复执行状态；本进程仍拥有的活任务绝不能标成上次中断。"""
        store = self.orch.store
        if store is None or not hasattr(store, "read_events"):
            return
        terminal = {"completed", "failed", "cancelled"}
        for item in store.latest_execution_events():
            if item.kind in terminal:
                continue
            summary = _interrupted_event_summary(item.kind, item.text)
            safe_agent = _safe_activity_agent(item.agent)
            try:
                live = self.bus.get(item.command_id)
            except CommandNotFoundError:
                live = None
            if live is not None:
                state = live.status.value
                progress = self._update_command_progress(
                    item.command_id,
                    state,
                    live.error,
                    elapsed_seconds=command_elapsed_seconds(
                        live.created_at,
                        live.finished_at,
                        allow_open_interval=state in {"queued", "running"},
                    ),
                )
                progress.set_agent(
                    safe_agent,
                    "queued" if state == "queued" else "running",
                    summary,
                )
                if hasattr(store, "read_command_events"):
                    self._restore_task_plan(
                        progress,
                        store.read_command_events(item.command_id)["items"],
                    )
                continue
            progress = self._update_command_progress(
                item.command_id, "interrupted", summary)
            progress.set_agent(safe_agent, "interrupted", summary)
            if hasattr(store, "read_command_events"):
                self._restore_task_plan(
                    progress,
                    store.read_command_events(item.command_id)["items"],
                )
            if self._activity_feed.set_command_state(
                item.command_id, "interrupted", summary
            ):
                self._upsert_activity_card(item.command_id)
            self._system(
                f"上次任务 {item.command_id[:8]} 已中断；"
                f"最后状态：{safe_agent} {summary}")
        self._render_task_status()

    def _restore_latest_collaboration_task(self) -> None:
        """恢复当前房间最后一个协作任务的固定状态投影。"""
        store = self.orch.store
        if store is None or not hasattr(store, "read_latest_command_events"):
            return
        page = store.read_latest_command_events()
        if page is None:
            return
        items = page["items"]
        if not items or not any(item.kind == "plan" for item in items):
            return
        last = items[-1]
        try:
            live = self.bus.get(last.command_id)
        except CommandNotFoundError:
            live = None
        state = (
            live.status.value
            if live is not None
            else last.kind
            if last.kind in {"completed", "failed", "cancelled"}
            else "interrupted"
        )
        error = (
            live.error
            if live is not None
            else _interrupted_event_summary(last.kind, last.text)
            if state in {"failed", "interrupted"}
            else None
        )
        elapsed = (
            command_elapsed_seconds(
                live.created_at,
                live.finished_at,
                allow_open_interval=state in {"queued", "running"},
            )
            if live is not None
            else command_elapsed_seconds(
                items[0].created_at,
                items[-1].created_at,
                allow_open_interval=False,
            )
        )
        progress = self._update_command_progress(
            last.command_id,
            state,
            error,
            elapsed_seconds=elapsed,
        )
        self._restore_task_plan(progress, items)
        self._render_task_status()

    @staticmethod
    def _restore_task_plan(
        progress: TaskProgress,
        records: Iterable[ExecutionEventRecord],
    ) -> bool:
        """把持久计划单调投影到固定任务区，不参与任何执行决策。"""
        changed = False
        for record in records:
            if record.kind == "plan":
                changed = progress.record_collaboration_plan(
                    record.text) or changed
        return changed

    # ---- 时间线输出 ----

    @staticmethod
    def _line(speaker: str, text: str, style: str = "") -> Text:
        color = _COLORS.get(speaker, "white")
        prefix = Text(f"[{speaker}] ", style=f"bold {color}")
        body = (
            Text(text, style=style)
            if speaker in {"user", "system", "activity"}
            else render_chat_markdown(text, style)
        )
        return Text.assemble(prefix, body)

    def _write(self, speaker: str, text: str, style: str = "") -> None:
        if "red" in style:
            text = redact_sensitive_text(text, limit=2000)
        self._display_lines.append((speaker, text, style))
        rendered = self._line(speaker, text, style)
        self._rendered_display_lines.append(rendered)
        self.query_one(RichLog).write(rendered)
        if "red" in style:
            self._show_error_notice(text)

    def _show_error_notice(
        self, text: str, *, room_id: str | None = None
    ) -> None:
        """按 room 保存最新可操作错误，避免切会话时串屏。"""
        target_room = room_id or self._activity_room_id
        compact = redact_sensitive_text(" ".join(text.split()), limit=500)
        self._error_notices[target_room] = compact
        if target_room == self._activity_room_id:
            self._render_error_notice()

    def _render_error_notice(self) -> None:
        try:
            notice = self.query_one("#notice-strip", Static)
        except NoMatches:
            return
        compact = self._error_notices.get(self._activity_room_id, "")
        if compact:
            notice.update(f"错误 · {compact}")
            notice.styles.display = "block"
        else:
            notice.update("")
            notice.styles.display = "none"

    def _clear_error_notice(self, room_id: str | None = None) -> None:
        target_room = room_id or self._activity_room_id
        self._error_notices.pop(target_room, None)
        if target_room == self._activity_room_id:
            self._render_error_notice()

    def _render_display_lines(self) -> None:
        """重绘逻辑时间线，让正在流式增长的回复仍只占一条记录。"""
        log = self.query_one(RichLog)
        log.clear()
        if len(self._rendered_display_lines) > len(self._display_lines):
            del self._rendered_display_lines[len(self._display_lines):]
        while len(self._rendered_display_lines) < len(self._display_lines):
            self._rendered_display_lines.append(None)
        for index, (speaker, text, style) in enumerate(self._display_lines):
            rendered = self._rendered_display_lines[index]
            if rendered is None:
                rendered = self._line(speaker, text, style)
                self._rendered_display_lines[index] = rendered
            log.write(rendered)

    def _buffer_stream_text(self, name: str, text: str) -> None:
        self._stream_text[name] = self._stream_text.get(name, "") + text
        if name not in self._stream_flush_handles:
            loop = asyncio.get_running_loop()
            self._stream_flush_handles[name] = loop.call_later(
                _STREAM_RENDER_INTERVAL, self._flush_stream_text, name)

    def _flush_stream_text(self, name: str) -> None:
        handle = self._stream_flush_handles.pop(name, None)
        if handle is not None:
            handle.cancel()
        text = self._stream_text.get(name, "")
        if not text:
            return
        index = self._stream_line_index.get(name)
        if index is None:
            self._stream_line_index[name] = len(self._display_lines)
            self._display_lines.append((name, text, ""))
            self._rendered_display_lines.append(None)
        else:
            self._display_lines[index] = (name, text, "")
            self._rendered_display_lines[index] = None
        self._render_display_lines()

    def _finish_stream_text(self, name: str) -> None:
        """同步刷完尾部 chunk，并结束这一段逻辑回复。"""
        self._flush_stream_text(name)
        self._stream_text.pop(name, None)
        self._stream_line_index.pop(name, None)

    def _upsert_activity_card(self, command_id: str) -> None:
        """同一 command 的过程事件始终只重绘独立任务面板。"""
        del command_id
        self._refresh_activity_cards()

    def _apply_activity_evictions(self) -> bool:
        """近期可展开窗口之外从任务面板移除；持久事件仍是事实源。"""
        changed = False
        for command_id, _snapshot in self._activity_feed.take_evicted():
            self._expanded_activity.discard(command_id)
            if self._selected_activity_id == command_id:
                self._selected_activity_id = None
            changed = True
        self._discard_stale_activity_selection()
        return changed

    def _discard_stale_activity_selection(self) -> None:
        """以当前 feed 为准收口选择状态，不依赖可能被截断的淘汰通知。"""
        command_ids = set(self._activity_feed.command_ids())
        self._expanded_activity.intersection_update(command_ids)
        if self._selected_activity_id not in command_ids:
            self._selected_activity_id = None

    def _render_activity_card(self, command_id: str) -> Text:
        expanded = command_id in self._expanded_activity
        text = self._activity_feed.render(command_id, expanded=expanded)
        style = "dim"
        if command_id == self._selected_activity_id:
            text = "▶ " + text.replace(
                f"/details {'收起' if expanded else '展开'}",
                f"Enter {'收起' if expanded else '展开'} · Esc 返回",
                1,
            )
            style = "bold cyan"
        return Text(text, style=style)

    def _refresh_activity_cards(self) -> None:
        """切换展开态时只重建任务面板，不重绘聊天主线。"""
        self._apply_activity_evictions()
        try:
            panel = self.query_one("#activity-panel", ActivityPanel)
        except NoMatches:
            # Session watcher 可能恰好在 Textual 已卸载子节点后完成收尾。
            return
        all_command_ids = self._activity_feed.command_ids()
        if not all_command_ids:
            panel.update("")
            panel.styles.display = "none"
            return
        browsing = self._selected_activity_id is not None
        command_ids = (
            all_command_ids
            if browsing else self._activity_feed.compact_command_ids()
        )
        hidden_count = len(all_command_ids) - len(command_ids)
        rendered = Text()
        rendered.append("任务活动", "bold")
        if browsing:
            rendered.append(
                "  ·  ↑↓ 选择  ·  Enter 展开/收起  ·  Esc 返回", "dim")
        else:
            rendered.append(
                "  ·  当前与最近  ·  Ctrl+G 浏览全部  ·  /details 展开",
                "dim",
            )
        if hidden_count:
            rendered.append(
                f"\n已收起 {hidden_count} 个任务  ·  Ctrl+G 浏览全部",
                "dim",
            )
        for command_id in command_ids:
            rendered.append("\n")
            rendered.append_text(self._render_activity_card(command_id))
        panel.update(rendered)
        visual_lines = rendered.plain.count("\n") + 1
        expanded = any(
            command_id in self._expanded_activity
            for command_id in command_ids
        )
        height_limit = 14 if browsing or expanded else 7
        panel.styles.height = min(
            height_limit, max(3, visual_lines + 2))
        panel.styles.display = "block"

    def _system(self, text: str) -> None:
        self._write("system", text, "dim")

    # ---- 消息处理 ----

    def _completion_agents(self) -> tuple[tuple[str, str], ...]:
        status_by_name = {
            item.name: item
            for item in self.orch.agent_readiness_snapshot()
        }
        agents = []
        for spec in self.orch.specs:
            if spec.display_name or spec.purpose:
                identity = spec.display_name or spec.name
                purpose = f" · {spec.purpose}" if spec.purpose else ""
                description = (
                    f"{identity}{purpose} · "
                    f"{status_by_name[spec.name].state.label}"
                )
            else:
                description = (
                    f"{spec.transport.upper()} · "
                    f"{status_by_name[spec.name].state.label}"
                )
            agents.append((spec.name, description))
        if all(name != HOST_NAME for name, _ in agents):
            host = status_by_name[HOST_NAME]
            backend = self.orch.host_backend_status()
            target = (
                f"@{backend.selection.target}"
                if backend.selection.kind == "agent"
                else backend.resolved_target
            )
            agents.append((
                HOST_NAME,
                f"主持、路由与总结 · {target} · {host.state.label}",
            ))
        agents.sort(
            key=lambda item: not status_by_name[item[0]].ready)
        return tuple(agents)

    def _update_composer_completion(self, box: ComposerInput) -> None:
        value = box.value
        if value == self._suppress_completion_value:
            self._suppress_completion_value = None
            self.close_completion()
            return
        self._suppress_completion_value = None
        context = completion_context(
            value,
            box.cursor_position,
            self._completion_agents(),
        )
        self._completion = context
        self._completion_index = 0
        self._render_completion()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "composer":
            return
        box = event.text_area
        if not isinstance(box, ComposerInput):
            return
        self._resize_composer(box)
        if not self.is_mounted:
            return
        self._update_composer_completion(box)

    def _resize_composer(self, box: ComposerInput) -> None:
        """按显式换行和软换行近似增长，避免长任务只剩一条横向窗口。"""
        content_width = max(20, box.content_size.width or self.size.width - 4)
        visual_lines = 0
        for line in box.value.split("\n"):
            width = cell_len(line.expandtabs(4))
            visual_lines += max(
                1, (width + content_width - 1) // content_width)
        box.styles.height = min(8, max(3, visual_lines + 2))

    def _render_completion(self) -> None:
        popup = self.query_one("#completion-list", OptionList)
        context = self._completion
        if context is None:
            popup.styles.display = "none"
            popup.clear_options()
            return
        options: list[Option] = []
        for item in context.items:
            prompt = Text(no_wrap=True, overflow="ellipsis")
            prompt.append("  ")
            prompt.append(f"{item.label:<14}", "bold")
            prompt.append(f" {item.description}", "bright_black")
            options.append(Option(prompt, id=item.value))
        popup.clear_options()
        popup.add_options(options)
        # OptionList 的 auto height 不计边框；候选刚好达到上限时会裁掉末项，
        # 同时又不会生成可滚动区域。显式把两行边框计入实际高度。
        popup.styles.height = min(len(options) + 2, 10)
        popup.styles.display = "block"
        popup.highlighted = self._completion_index
        popup.scroll_to_highlight()

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id != "completion-list":
            return
        context = self._completion
        if context is not None and event.option_index < len(context.items):
            self._completion_index = event.option_index

    def on_option_list_option_selected(
            self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "completion-list":
            return
        context = self._completion
        if context is None or event.option_index >= len(context.items):
            return
        self._completion_index = event.option_index
        self.accept_completion()

    def move_completion(self, delta: int) -> bool:
        context = self._completion
        if context is None:
            return False
        self._completion_index = (
            self._completion_index + delta) % len(context.items)
        self._render_completion()
        return True

    def accept_completion(self) -> bool:
        context = self._completion
        if context is None:
            return False
        box = self.query_one("#composer", ComposerInput)
        current = completion_context(
            box.value,
            box.cursor_position,
            self._completion_agents(),
        )
        if current != context:
            self.close_completion()
            return False
        item = context.items[self._completion_index]
        suffix = ""
        if context.kind == "mention" and (
                context.end == len(box.value)
                or not box.value[context.end].isspace()):
            suffix = " "
        completed_value = (
            box.value[:context.start]
            + item.value
            + suffix
            + box.value[context.end:]
        )
        # 已经完整输入本地命令时，Enter 应直接提交；不能被候选层吞掉，
        # 迫使用户再按一次 Enter。
        if context.kind in {"command", "agent_control"} \
                and completed_value == box.value:
            self.close_completion()
            return False
        self._suppress_completion_value = completed_value
        box.value = completed_value
        box.cursor_position = context.start + len(item.value) + len(suffix)
        self.close_completion()
        box.focus()
        return True

    def close_completion(self) -> bool:
        was_open = self._completion is not None
        self._completion = None
        self._completion_index = 0
        if self.is_mounted:
            self._render_completion()
        return was_open

    async def on_composer_input_submitted(
        self,
        event: ComposerInput.Submitted,
    ) -> None:
        text = event.value.strip()
        if not text:
            return
        original_cursor = event.input.cursor_position

        valid_agents = [spec.name for spec in self.orch.specs]
        valid_agents.append(HOST_NAME)
        unknown = unknown_mentions(text, valid_agents)
        if unknown:
            rendered = "、".join(f"@{name}" for name in unknown)
            self._write(
                "system",
                f"未知 agent：{rendered}；请从 @ 候选中选择",
                "bold red",
            )
            event.input.value = event.value
            event.input.cursor_position = original_cursor
            event.input.focus()
            return

        try:
            agent_command = parse_agent_control_command(text)
        except AgentEnablementError as exc:
            self._write("system", str(exc), "bold red")
            event.input.value = event.value
            event.input.cursor_position = original_cursor
            event.input.focus()
            return
        if agent_command is not None:
            event.input.value = ""
            self.close_completion()
            if agent_command.action == "show":
                self.action_show_agents()
            elif agent_command.action == "rescan":
                self.action_rescan_agents()
            else:
                self.action_set_agent_enabled(
                    agent_command.name or "",
                    enabled=agent_command.action == "enable",
                )
            return

        try:
            context_command = parse_context_command(text)
        except ContextCommandValidationError as exc:
            self._write("system", str(exc), "bold red")
            event.input.value = event.value
            event.input.cursor_position = original_cursor
            event.input.focus()
            return
        if context_command is not None:
            event.input.value = ""
            self.close_completion()
            if context_command.action == "show":
                self.action_show_context()
            else:
                await self._compact_context(
                    context_command.target or HOST_NAME)
            return

        command = local_command_for(text)
        try:
            host_command = parse_host_command(text)
        except HostBackendValidationError as exc:
            self._write("system", str(exc), "bold red")
            event.input.value = event.value
            event.input.cursor_position = original_cursor
            event.input.focus()
            return
        if host_command is not None:
            event.input.value = ""
            self.close_completion()
            if host_command.action == "show":
                self.action_show_host()
            else:
                await self._switch_host_backend(
                    host_command.kind or "", host_command.target or "")
            return
        if command is not None:
            # 本地命令必须在持久化/路由之前截获；未注册的 slash 文本仍按
            # 普通消息提交，避免猜测用户语义。
            event.input.value = ""
            self.close_completion()
            getattr(self, command.handler)()
            return
        try:
            self.orch.require_message_agents(text)
        except (
            AgentUnavailableError,
            DiscussionValidationError,
            WorkflowValidationError,
        ) as exc:
            self._write("system", str(exc), "bold red")
            event.input.value = event.value
            event.input.cursor_position = original_cursor
            event.input.focus()
            return
        event.input.value = ""
        self.close_completion()
        # 不预先显示用户文本/思考状态：等 orchestrator 持久确认
        # （committed 事件）后再显示；append 失败时什么都不出现。
        self._dispatch_from_ui(text)

    def action_show_agents(self) -> None:
        transport_by_name = {
            spec.name: spec.transport.upper() for spec in self.orch.specs
        }
        transport_by_name[HOST_NAME] = self.orch.host_backend_status().transport
        rows = ["Agent 就绪状态："]
        for status in self.orch.agent_readiness_snapshot():
            rows.append(
                f"@{status.name} · {status.state.label} · "
                f"{transport_by_name[status.name]}")
            rows.append(f"  {status.detail}")
            if not status.ready:
                rows.append(f"  建议：{status.setup_hint}")
        rows.append("重新检测：/agents rescan（不会安装或改动任何 agent）")
        rows.append(
            "全局开关：/agents disable <agent> · /agents enable <agent>")
        self._system("\n".join(rows))

    def action_show_host(self) -> None:
        status = self.orch.host_backend_status()
        selection = status.selection
        kind = "直接模型" if selection.kind == "model" else "完整 Agent"
        reference = (
            f"{selection.reference}={selection.target}"
            if selection.kind == "model" else f"agent={selection.target}"
        )
        self._system(
            "当前会话 Host：\n"
            f"类型：{kind}\n"
            f"选择：{reference}\n"
            f"目标：{status.resolved_target}\n"
            f"状态：{status.readiness.state.label} · {status.transport}\n"
            f"详情：{status.readiness.detail}\n"
            "切换：/host model <profile-or-exact-model-id> 或 "
            "/host agent <agent>"
        )

    def action_show_context(self) -> None:
        rows = [
            "当前会话上下文：",
            (
                "策略：达到 "
                f"{self.orch.context_policy.trigger_characters:,} 字符后"
                + ("自动压缩" if self.orch.context_policy.auto_compact
                   else "仅手动压缩")
            ),
        ]
        for status in self.orch.context_status_snapshot():
            counters = ""
            if (
                status.message_count is not None
                and status.character_count is not None
            ):
                counters = (
                    f" · {status.message_count} 条内部消息"
                    f" · {status.character_count:,} 字符"
                )
            checkpoint = ""
            if status.checkpoint_generation is not None:
                checkpoint = (
                    f" · checkpoint #{status.checkpoint_generation}"
                    f"（至 seq {status.checkpoint_boundary}）"
                )
            rows.append(
                f"@{status.name} · {status.state}{counters}{checkpoint}")
            rows.append(f"  {status.detail}")
        rows.append("手动压缩：/compact [@agent]；省略目标时使用 @host")
        self._system("\n".join(rows))

    def action_compact_context(self) -> None:
        """Local command handler used by exact /compact completion."""
        self.run_worker(self._compact_context(HOST_NAME))

    async def _compact_context(self, name: str) -> None:
        if self.bus.has_pending():
            self._write(
                "system",
                "当前会话有任务正在运行或排队；请在回合结束后压缩上下文",
                "bold red",
            )
            return
        self.notify(f"正在压缩 @{name} 上下文…", timeout=3)
        try:
            result = await self.orch.compact_context(name)
        except ContextLifecycleError as exc:
            self._write("system", f"压缩失败：{exc}", "bold red")
            return
        except Exception:
            self._write(
                "system", "压缩失败：发生未预期的内部错误", "bold red")
            return
        if not result.changed:
            self._system(
                f"@{name} 近期上下文还不足以压缩；现有内容保持不变")
            return
        self._system(
            f"@{name} 上下文已压缩："
            f"{result.before_characters:,} → "
            f"{result.after_characters:,} 字符；"
            "完整聊天时间线未删除"
        )

    async def _switch_host_backend(self, kind: str, target: str) -> None:
        if self.bus.has_pending():
            self._write(
                "system",
                "当前会话有任务正在运行或排队；结束后再切换 Host",
                "bold red",
            )
            return
        try:
            status = await self.orch.switch_host_backend(kind, target)
        except Exception as exc:
            self._write("system", f"切换 Host 失败：{exc}", "bold red")
            return
        self._system(
            f"Host 已切换：{status.selection.label} · "
            f"{status.resolved_target} · {status.transport}"
        )

    def action_rescan_agents(self) -> None:
        try:
            if self.session_manager is not None:
                self.session_manager.refresh_agent_readiness()
            else:
                self.orch.refresh_agent_readiness()
        except Exception as exc:
            self._write("system", f"重新检测失败：{exc}", "bold red")
            return
        self._system("已重新检测；未安装或改动任何 agent")
        self.action_show_agents()

    def action_set_agent_enabled(self, name: str, *, enabled: bool) -> None:
        try:
            if self.session_manager is not None:
                self.session_manager.set_agent_enabled(name, enabled=enabled)
            else:
                self.orch.set_agent_enabled(name, enabled=enabled)
        except Exception as exc:
            verb = "启用" if enabled else "禁用"
            self._write("system", f"{verb} @{name} 失败：{exc}", "bold red")
            return
        verb = "启用" if enabled else "禁用"
        self._system(
            f"已全局{verb} @{name}；当前进程内所有已加载会话已同步，"
            "其他正在运行的 myagents 进程请执行 /agents rescan。"
            "已在执行的任务不会被中断。"
        )
        self.action_show_agents()

    def action_show_roles(self) -> None:
        roles = self.orch.session_roles
        if not roles:
            self._system(
                "当前会话没有设置角色；可直接说“让 @agent 担任……”")
            return
        rows = ["当前会话角色："]
        for name, role in roles.items():
            rows.append(f"@{name} · {role.label}")
            rows.append(f"  {role.instructions}")
        rows.append("清空全部：/roles clear")
        self._system("\n".join(rows))

    def action_clear_roles(self) -> None:
        if self.bus.has_pending():
            self._system("当前会话有任务正在运行或排队，结束后再清空角色")
            return
        try:
            cleared = self.orch.clear_session_roles()
        except Exception as exc:
            self._write("system", f"清空会话角色失败：{exc}", "bold red")
            return
        if not cleared:
            self._system("当前会话没有角色，无需清空")
            return
        rendered = "、".join(f"@{name}" for name in cleared)
        self._system(f"已清空当前会话角色：{rendered}")

    @property
    def auto_approve(self) -> bool:
        """当前活动会话是否由 /yolo 临时自动批准。"""
        return self._activity_room_id in self._auto_approve_rooms

    def _auto_approve_for(self, room_id: str | None) -> bool:
        key = self._activity_room_id if room_id is None else room_id
        return key in self._auto_approve_rooms

    def _update_permission_mode_ui(self) -> None:
        """让当前会话的危险状态同时出现在标题与固定任务区。"""
        if self.session_manager is None:
            title = "myagents"
        else:
            title = f"myagents · {self.session_manager.active_runtime.summary.title}"
        self.title = f"{title} · YOLO" if self.auto_approve else title
        if not self.is_mounted:
            return
        panel = self.query_one("#task-status", Static)
        panel.set_class(self.auto_approve, "auto-approve")
        self._render_task_status()

    def action_toggle_yolo(self) -> None:
        """切换当前会话的进程内自动批准；不持久化、不越过只读 profile。"""
        room_id = self._activity_room_id
        if room_id in self._auto_approve_rooms:
            self._auto_approve_rooms.remove(room_id)
            self._system("YOLO 已关闭：当前会话恢复逐次权限确认")
        else:
            self._auto_approve_rooms.add(room_id)
            self._system(
                "YOLO 已开启：当前会话将自动批准普通/写入轮次的 "
                "allow_once；只读阶段仍硬拒绝，退出后自动失效"
            )
        self._update_permission_mode_ui()

    def action_show_help(self) -> None:
        commands = "\n".join(
            f"{command.token:<10} {command.description}"
            for command in LOCAL_COMMANDS
        )
        self._system(
            "本地命令：\n"
            f"{commands}\n"
            "候选：输入 @ 或 /，↑↓ 选择，Tab/Enter 补全，Esc 关闭")

    def action_show_discuss_help(self) -> None:
        self._system(
            "有界讨论用法：\n"
            f"{DISCUSSION_USAGE}\n"
            "参与者 2–3 个，轮数 1–3；默认两轮并由 host 最终仲裁。")

    def action_show_workflow_help(self) -> None:
        self._system(
            "有界里程碑 workflow 用法：\n"
            f"{WORKFLOW_USAGE}\n"
            "固定 review → 单 writer 实现 → 独立复核；最多一次 repair。")

    def action_show_steer_help(self) -> None:
        self._system(
            "运行中 workflow steering 用法：\n"
            f"{STEER_USAGE}\n"
            "只在 review/implement/repair 阶段接受，并从下一阶段边界生效。")

    def action_toggle_details(self) -> None:
        """展开或收起当前选中、否则最近一张执行活动卡。"""
        command_ids = self._activity_feed.command_ids()
        if not command_ids:
            store = self.orch.store
            if (
                store is None
                or not hasattr(store, "read_latest_command_events")
            ):
                self._system("当前会话还没有可展开的执行活动")
                return
            self.notify("正在读取最近任务详情…", timeout=2)
            self._load_latest_persisted_activity(store)
            return
        command_id = (
            self._selected_activity_id
            if self._selected_activity_id in command_ids
            else command_ids[-1]
        )
        if command_id in self._expanded_activity:
            self._expanded_activity.remove(command_id)
        else:
            self._expanded_activity.add(command_id)
            store = self.orch.store
            if store is not None and hasattr(store, "read_command_events"):
                generation = self._activity_feed.start_details_loading(
                    command_id)
                self._load_persisted_activity_details(
                    store, command_id, self._activity_feed,
                    self._activity_room_id,
                    generation,
                )
        self._upsert_activity_card(command_id)

    def _load_latest_persisted_activity(self, store: RoomStore) -> None:
        """没有内存卡时，按需恢复当前 room 最近一个持久任务。"""
        target_room_id = self._activity_room_id
        target_feed = self._activity_feed

        async def runner() -> None:
            try:
                page = await asyncio.to_thread(
                    store.read_latest_command_events)
                if page is None:
                    if self._activity_room_id == target_room_id:
                        self._system("当前会话还没有可展开的执行活动")
                    return
                items = page["items"]
                last = items[-1]
            except Exception as exc:
                if self._activity_room_id == target_room_id:
                    self._write(
                        "system",
                        "详情加载失败："
                        + redact_sensitive_text(str(exc), limit=400),
                        "bold red",
                    )
                return
            if self._activity_feeds.get(target_room_id) is not target_feed:
                return
            if target_feed.command_ids():
                # 等待磁盘期间若已有新任务进入当前 room，不用旧历史抢占焦点。
                return
            state = (
                last.kind
                if last.kind in {"completed", "failed", "cancelled"}
                else "interrupted"
            )
            safe_last_text = _interrupted_event_summary(
                last.kind, last.text)
            safe_agent = _safe_activity_agent(last.agent)
            target_feed.begin(last.command_id, state)
            target_feed.record_status(
                last.command_id,
                safe_agent,
                safe_last_text,
                state=state,
            )
            elapsed = (
                command_elapsed_seconds(
                    items[0].created_at,
                    items[-1].created_at,
                    allow_open_interval=False,
                )
                if items else None
            )
            target_feed.set_command_state(
                last.command_id,
                state,
                safe_last_text
                if state in {"failed", "interrupted"} else None,
                elapsed_seconds=elapsed,
            )
            target_feed.set_persisted_details(
                last.command_id,
                _activity_detail_events(items),
                total_count=page["total_count"],
                omitted_count=page["omitted_count"],
                kind_counts=page["kind_counts"],
                agents=page["agents"],
                partial_char_count=page["partial_char_count"],
            )
            if self._activity_room_id == target_room_id:
                progress = self._update_command_progress(
                    last.command_id,
                    state,
                    safe_last_text
                    if state in {"failed", "interrupted"} else None,
                    elapsed_seconds=elapsed,
                )
                self._restore_task_plan(progress, items)
                self._render_task_status()
            self._expanded_activity_ids.setdefault(
                target_room_id, set()).add(last.command_id)
            if self._activity_room_id == target_room_id:
                self._upsert_activity_card(last.command_id)

        self.run_worker(runner())

    def _load_persisted_activity_details(
        self,
        store: RoomStore,
        command_id: str,
        target_feed: ActivityFeed,
        target_room_id: str,
        generation: int,
    ) -> None:
        """异步读取一个任务的持久详情；切会话不会串写当前 RichLog。"""
        async def runner() -> None:
            try:
                page = await asyncio.to_thread(
                    store.read_command_events, command_id)
            except Exception as exc:
                if target_feed.has(command_id):
                    target_feed.set_details_error(
                        command_id, str(exc), generation=generation)
            else:
                if target_feed.has(command_id):
                    target_feed.set_persisted_details(
                        command_id,
                        _activity_detail_events(page["items"]),
                        total_count=page["total_count"],
                        omitted_count=page["omitted_count"],
                        kind_counts=page["kind_counts"],
                        agents=page["agents"],
                        partial_char_count=page["partial_char_count"],
                        generation=generation,
                    )
            if (
                self._activity_room_id == target_room_id
                and self._activity_feed is target_feed
                and target_feed.has(command_id)
            ):
                self._upsert_activity_card(command_id)

        self.run_worker(runner())

    def _refresh_expanded_persisted_detail(
        self,
        command_id: str,
    ) -> None:
        """终态或会话切回时，把运行中旧快照刷新为完整持久过程。"""
        store = self.orch.store
        if (
            store is None
            or not hasattr(store, "read_command_events")
            or command_id not in self._expanded_activity
            or not self._activity_feed.has(command_id)
            or self._activity_feed.has_terminal_persisted_details(command_id)
        ):
            return
        generation = self._activity_feed.start_details_loading(command_id)
        self._load_persisted_activity_details(
            store,
            command_id,
            self._activity_feed,
            self._activity_room_id,
            generation,
        )

    def action_focus_activities(self) -> None:
        """进入活动卡键盘导航，并默认选中最近一张。"""
        command_ids = self._activity_feed.command_ids()
        if not command_ids:
            self._system("当前没有可浏览的执行活动")
            return
        self.close_completion()
        self._selected_activity_id = command_ids[-1]
        self._refresh_activity_cards()
        self.query_one(ActivityPanel).focus()
        self.call_after_refresh(self._scroll_selected_activity_into_view)

    def move_activity_selection(self, delta: int) -> None:
        command_ids = self._activity_feed.command_ids()
        if not command_ids:
            return
        if self._selected_activity_id not in command_ids:
            index = len(command_ids) - 1
        else:
            index = command_ids.index(self._selected_activity_id)
            index = (index + delta) % len(command_ids)
        self._selected_activity_id = command_ids[index]
        self._refresh_activity_cards()
        self.call_after_refresh(self._scroll_selected_activity_into_view)

    def _scroll_selected_activity_into_view(self) -> None:
        command_id = self._selected_activity_id
        if command_id is None:
            return
        panel = self.query_one(ActivityPanel)
        offset = 1
        for item in self._activity_feed.command_ids():
            if item == command_id:
                panel.scroll_to(y=offset, immediate=True, force=True)
                return
            offset += self._activity_feed.render(
                item,
                expanded=item in self._expanded_activity,
            ).count("\n") + 1

    def close_activity_navigation(self) -> None:
        """退出活动卡导航，清除选中标记并把焦点还给输入框。"""
        self.compact_activity_navigation()
        self.query_one("#composer", ComposerInput).focus()

    def compact_activity_navigation(self) -> None:
        """活动区失焦后恢复紧凑投影，不强行改变新的焦点目标。"""
        if self._selected_activity_id is None:
            return
        self._selected_activity_id = None
        self._refresh_activity_cards()

    def action_paste_image(self) -> None:
        """保存系统剪贴板图片，并把本地附件引用插入当前草稿。"""
        if self._image_paste_in_progress:
            self._system("正在读取剪贴板图片…")
            return
        store = self.orch.store
        if store is None:
            self._write(
                "system", "图片粘贴需要持久会话", "bold red")
            return
        self._image_paste_in_progress = True
        destination = store.room_dir / "attachments"

        async def runner() -> None:
            try:
                path = await asyncio.to_thread(
                    self._clipboard_image_capture, destination)
            except ClipboardImageError as exc:
                self._write("system", str(exc), "bold red")
                return
            except Exception as exc:
                self._write(
                    "system", f"粘贴图片失败：{exc}", "bold red")
                return
            finally:
                self._image_paste_in_progress = False
            box = self.query_one("#composer", ComposerInput)
            try:
                reference = attachment_reference(path)
            except ClipboardImageError as exc:
                self._write("system", str(exc), "bold red")
                return
            start, end = box.selection_offsets
            prefix = "" if start == 0 or box.value[start - 1].isspace() else " "
            suffix = "" if end == len(box.value) or (
                end < len(box.value) and box.value[end].isspace()
            ) else " "
            box.replace_offsets(
                f"{prefix}{reference}{suffix}", start, end)
            box.focus()
            self._system(f"已粘贴图片：{path.name}")

        self.run_worker(runner())

    def _dispatch_from_ui(self, text: str) -> None:
        """经 CommandBus 提交（唯一入口，不再直接 orch.dispatch）。

        提交/容量/closed 异常与执行期失败（如持久化错误 → 命令 failed）
        都显示为红色 system 错误；bus FIFO 串行执行，事件经 event_sink
        回到 _on_agent_event。
        """
        try:
            instruction = parse_steer_instruction(text)
        except WorkflowValidationError as exc:
            self._write("system", str(exc), "bold red")
            return
        if instruction is not None:
            active = self.bus.active()
            if active is None:
                self._write("system", "当前没有 running workflow", "bold red")
                return
            try:
                receipt = self.bus.steer(active.command_id, instruction)
            except Exception as exc:
                self._write("system", f"steering 失败：{exc}", "bold red")
            else:
                self._system(
                    f"steering 已接受（第 {receipt['accepted']} 条），"
                    "将在下一阶段边界生效")
            return

        async def runner() -> None:
            if self.session_manager is not None:
                try:
                    managed = await self.session_manager.submit(text)
                except Exception as exc:
                    self._write("system", f"派发失败：{exc}", "bold red")
                    return
                self._clear_error_notice(managed.session_id)
                if managed.session_id == self.session_manager.active_session_id:
                    self._set_queued_command(managed.command_id, text)
                while True:
                    result = await self.session_manager.wait(managed)
                    if not result["timed_out"]:
                        break
                if managed.session_id != self.session_manager.active_session_id:
                    return
                self._set_command_status(
                    managed.command_id,
                    result["status"],
                    result.get("error"),
                    elapsed_seconds=command_elapsed_seconds(
                        result.get("created_at"),
                        result.get("finished_at"),
                        allow_open_interval=False,
                    ),
                )
                if result["status"] == "failed":
                    self._write(
                        "system", f"派发失败：{result['error']}", "bold red"
                    )
                return

            bus = self.bus
            try:
                snap = await bus.submit(text)
            except Exception as exc:
                self._write("system", f"派发失败：{exc}", "bold red")
                return
            self._clear_error_notice()
            self._set_queued_command(snap.command_id, text)
            while True:  # agent 长任务可能超过单次 wait 上限，等到 terminal
                result = await bus.wait(snap.command_id)
                if not result["timed_out"]:
                    break
            self._set_command_status(
                snap.command_id,
                result["status"],
                result.get("error"),
                elapsed_seconds=command_elapsed_seconds(
                    result.get("created_at"),
                    result.get("finished_at"),
                    allow_open_interval=False,
                ),
            )
            if result["status"] == "failed":
                self._write("system", f"派发失败：{result['error']}", "bold red")
        # run_worker：agent 调用是长任务，不能阻塞 UI
        self.run_worker(runner())

    # ---- 固定任务状态区 ----

    def _ensure_task(self, command_id: str) -> TaskProgress:
        progress = self._task_progresses.get(command_id)
        if progress is None:
            progress = TaskProgress(command_id)
            self._task_progresses[command_id] = progress
            self._activity_feed.begin(command_id)
        self._latest_task_id = command_id
        return progress

    def _update_command_progress(
        self,
        command_id: str,
        status: str,
        error: str | None = None,
        *,
        elapsed_seconds: float | None = None,
    ) -> TaskProgress:
        progress = self._ensure_task(command_id)
        progress.set_command(
            status, error, elapsed_seconds=elapsed_seconds)
        return progress

    def _set_command_status(
            self, command_id: str, status: str,
            error: str | None = None, *,
            elapsed_seconds: float | None = None) -> None:
        progress = self._update_command_progress(
            command_id,
            status,
            error,
            elapsed_seconds=elapsed_seconds,
        )
        activity_changed = self._activity_feed.set_command_state(
            command_id,
            status,
            error,
            elapsed_seconds=(
                progress.elapsed_seconds
                if progress.is_terminal else None
            ),
        )
        if self.is_mounted and progress.is_terminal:
            self._refresh_expanded_persisted_detail(command_id)
        if self.is_mounted and (
                activity_changed or self._activity_feed.has(command_id)):
            # queued 序号取决于同 room 其余卡片的状态；任一 command 出队或
            # 终止时一起刷新，避免后续排队项停留在过期序号。
            self._refresh_activity_cards()
        self._render_task_status()

    def _set_queued_command(self, command_id: str, text: str) -> None:
        """显示已入 CommandBus、但尚未 committed 到 timeline 的用户输入。"""
        self._activity_feed.begin(command_id, "queued")
        self._activity_feed.record_pending_request(command_id, text)
        self._set_command_status(command_id, "queued")

    def _set_agent_status(
            self, command_id: str, name: str,
            state: str, phase: str = "",
            *, session_role: str | None = None,
            allow_reentry: bool = False) -> None:
        self._ensure_task(command_id).set_agent(
            name,
            state,
            phase,
            session_role=session_role,
            allow_reentry=allow_reentry,
        )
        self._render_task_status()

    def _set_workflow_status(self, command_id: str, meta: dict) -> None:
        self._ensure_task(command_id).set_workflow(
            meta.get("workflow_stage"),
            meta.get("workflow_roles"),
            meta.get("steering_available"),
        )
        self._render_task_status()

    def _selected_task(self) -> TaskProgress | None:
        active = self.bus.active()
        if active is not None:
            return self._ensure_task(active.command_id)
        unfinished = [
            progress for progress in self._task_progresses.values()
            if progress.status in {"queued", "running"}
        ]
        if unfinished:
            return unfinished[-1]
        if self._latest_task_id is not None:
            return self._task_progresses.get(self._latest_task_id)
        return None

    def _render_task_status(self) -> None:
        if not self.is_mounted:
            return
        self._render_composer_hint()
        panel = self.query_one("#task-status", Static)
        warning = (
            "YOLO：当前会话自动完全授权已开启（只读阶段仍硬拒绝）\n"
            if self.auto_approve else ""
        )
        workspace = self._workspace_status_line()
        progress = self._selected_task()
        if progress is None:
            panel.update(
                warning + workspace + "\n空闲 · 输入 @ 选择 Agent，输入 / 查看命令")
            return
        with contextlib.suppress(Exception):
            snapshot = self.bus.get(progress.command_id)
            elapsed = command_elapsed_seconds(
                snapshot.created_at,
                snapshot.finished_at,
                allow_open_interval=(
                    snapshot.status.value in {"queued", "running"}
                ),
            )
            progress = self._update_command_progress(
                progress.command_id,
                snapshot.status.value,
                snapshot.error,
                elapsed_seconds=elapsed,
            )
            if self._activity_feed.set_command_state(
                progress.command_id,
                snapshot.status.value,
                snapshot.error,
                elapsed_seconds=(
                    progress.elapsed_seconds
                    if progress.is_terminal else None
                ),
            ):
                self._upsert_activity_card(progress.command_id)
        panel.update(warning + workspace + "\n" + progress.render())

    def _workspace_status_line(self) -> str:
        """固定展示当前主持、Agent 可用度和后台提醒。"""
        backend = self.orch.host_backend_status()
        selection = backend.selection
        if selection.kind == "agent":
            host = f"主持 @{selection.target}（只读）"
        else:
            target = backend.resolved_target or selection.target
            safe_target = redact_sensitive_text(
                " ".join(target.split()), limit=80)
            host = f"主持 模型 {safe_target}"
        statuses = tuple(
            item for item in self.orch.agent_readiness_snapshot()
            if item.name != HOST_NAME
        )
        ready = sum(1 for item in statuses if item.ready)
        parts = [host, f"Agent {ready}/{len(statuses)}"]
        if self.session_manager is not None:
            with contextlib.suppress(RuntimeError):
                summary = self.session_manager.workspace_activity_summary()
                if summary.background_running:
                    parts.append(f"后台 {summary.background_running} 运行")
                if summary.unread:
                    parts.append(f"{summary.unread} 未读")
        return " · ".join(parts)

    def _render_composer_hint(self) -> None:
        if not self.is_mounted:
            return
        active = self.bus.active()
        if active is None and not self.bus.has_pending():
            text = (
                "Enter 发送 · Shift+Enter 换行 · @ 选择 Agent · "
                "/ 查看命令 · Ctrl+O 会话"
            )
        else:
            text = (
                "Enter 排队 · Shift+Enter 换行 · Alt+↑ 插入最早排队 · "
                "Esc 取消当前任务"
            )
        self.query_one("#composer-hint", Static).update(text)

    def action_cancel_active(self) -> None:
        if self.session_manager is not None:
            self.action_cancel_session(
                self.session_manager.active_session_id
            )
            return
        active = self.bus.active()
        if active is None:
            self._system("当前没有可取消的任务")
            return

        async def runner() -> None:
            try:
                result = await self.bus.cancel(active.command_id)
            except Exception as exc:
                self._write("system", f"取消失败：{exc}", "bold red")
            else:
                if result.status.value == "cancelled":
                    self._system(
                        f"任务 {result.command_id[:8]} 已取消")
                else:
                    self._system(
                        f"任务 {result.command_id[:8]} 已是 "
                        f"{result.status.value}，无需取消")
        self.run_worker(runner())

    def action_interject(self) -> None:
        """把当前会话 FIFO 中最早的排队输入提升为运行中插话。"""
        box = self.query_one("#composer", ComposerInput)
        active = self.bus.active()
        if active is None:
            self._write(
                "system", "当前没有运行中的任务，无法提升排队输入", "bold red")
            return
        target_bus = self.bus
        target_room_id = self._activity_room_id

        async def runner() -> None:
            try:
                await target_bus.interject_next(active.command_id)
            except Exception as exc:
                if self._activity_room_id == target_room_id:
                    self._write("system", f"插话失败：{exc}", "bold red")
                    box.focus()
                else:
                    self.notify(
                        f"后台会话插话失败：{exc}",
                        severity="error",
                        timeout=5,
                    )
                return
            if self._activity_room_id == target_room_id:
                self.close_completion()
                box.focus()

        self.run_worker(runner())

    def action_cancel_session(self, session_id: str | None) -> None:
        """取消指定会话任务；权限弹窗只影响其所属会话。"""
        if self.session_manager is None:
            self.action_cancel_active()
            return
        target = session_id or self.session_manager.active_session_id
        self._cancel_permissions_for(target)

        async def runner() -> None:
            assert self.session_manager is not None
            try:
                result = await self.session_manager.cancel(target)
            except Exception as exc:
                self._write("system", f"取消失败：{exc}", "bold red")
                return
            if result is None:
                self._system("该会话没有可取消的任务")
            elif result.status.value == "cancelled":
                self._system(f"任务 {result.command_id[:8]} 已取消")
            else:
                self._system(
                    f"任务 {result.command_id[:8]} 已是 "
                    f"{result.status.value}，无需取消"
                )

        self.run_worker(runner())

    def action_show_sessions(self) -> None:
        if self.session_manager is None:
            self._system("非持久模式不支持会话选择器")
            return
        self._save_active_draft()
        self.push_screen(
            SessionPickerScreen(self.session_manager),
            self._accept_session_picker_action,
        )

    def _save_active_draft(self) -> None:
        if self.session_manager is None or not self.is_mounted:
            return
        box = self.query_one("#composer", ComposerInput)
        self.session_manager.save_draft(
            box.value,
            cursor_position=box.cursor_position,
        )

    def _accept_session_picker_action(self, value: str | None) -> None:
        if value is None:
            return
        if value == "__new__":
            self.action_new_session()
            return
        if value.startswith("__rename__:"):
            room_id = value.removeprefix("__rename__:")
            snapshot = self._session_picker_snapshot(room_id)
            if snapshot is None:
                self._write("system", "会话已不存在", "bold red")
                return
            self.push_screen(
                RenameSessionScreen(snapshot),
                lambda title: self._accept_session_rename(room_id, title),
            )
            return
        if value.startswith("__delete__:"):
            room_id = value.removeprefix("__delete__:")
            snapshot = self._session_picker_snapshot(room_id)
            if snapshot is None:
                self._write("system", "会话已不存在", "bold red")
                return
            if room_id == self.session_manager.active_session_id:
                self._write(
                    "system", "当前会话不能永久删除；请先切换", "bold red"
                )
                return
            if snapshot.status in {
                "queued", "waiting_resource", "running", "waiting_permission"
            }:
                self._write(
                    "system", "运行中或等待权限的会话不能永久删除", "bold red"
                )
                return
            self.push_screen(
                DeleteSessionScreen(snapshot),
                lambda confirmation: self._accept_session_delete(
                    room_id, confirmation
                ),
            )
            return

        async def runner() -> None:
            assert self.session_manager is not None
            try:
                await self.session_manager.activate(value)
            except Exception as exc:
                self._write("system", f"切换会话失败：{exc}", "bold red")
                return
            self._bind_active_runtime()
            self._render_active_session()

        self.run_worker(runner())

    def _session_picker_snapshot(
        self,
        room_id: str,
    ) -> SessionSnapshot | None:
        if self.session_manager is None:
            return None
        return next(
            (
                item
                for item in self.session_manager.list_sessions(
                    include_all=True
                )
                if item.summary.room_id == room_id
            ),
            None,
        )

    def _accept_session_rename(
        self,
        room_id: str,
        title: str | None,
    ) -> None:
        if title is None:
            return

        async def runner() -> None:
            assert self.session_manager is not None
            try:
                renamed = await self.session_manager.rename_session(
                    room_id, title
                )
            except Exception as exc:
                self._write("system", f"重命名失败：{exc}", "bold red")
                return
            if room_id == self.session_manager.active_session_id:
                self._bind_active_runtime()
            self._system(f"会话已重命名：{renamed.summary.title}")

        self.run_worker(runner())

    def _accept_session_delete(
        self,
        room_id: str,
        confirmation: str | None,
    ) -> None:
        if confirmation is None:
            return

        async def runner() -> None:
            assert self.session_manager is not None
            try:
                await self.session_manager.delete_session(
                    room_id,
                    confirmation=confirmation,
                )
            except Exception as exc:
                self._write("system", f"永久删除失败：{exc}", "bold red")
                return
            self._activity_feeds.pop(room_id, None)
            self._expanded_activity_ids.pop(room_id, None)
            self._error_notices.pop(room_id, None)
            self._auto_approve_rooms.discard(room_id)
            self._system("会话已永久删除")

        self.run_worker(runner())

    def _render_active_session(self) -> None:
        """从持久 history 重建当前可见时间线，不混入其他会话的临时日志。"""
        if self.session_manager is None:
            return
        for handle in self._stream_flush_handles.values():
            handle.cancel()
        self._stream_flush_handles.clear()
        self._display_lines.clear()
        self._rendered_display_lines.clear()
        self._stream_text.clear()
        self._stream_line_index.clear()
        self._selected_activity_id = None
        self._task_progresses.clear()
        self._latest_task_id = None
        self.query_one(RichLog).clear()
        snapshot = self.session_manager.snapshot()
        for message in sorted(snapshot.history, key=lambda item: item.seq):
            self._write(message.speaker, message.text)
        for command_id in tuple(self._expanded_activity):
            self._refresh_expanded_persisted_detail(command_id)
        self._refresh_activity_cards()
        self._render_error_notice()
        self._restore_interrupted_executions()
        self._restore_latest_collaboration_task()
        self._system(
            f"已切换：{snapshot.summary.project_name} / "
            f"{snapshot.summary.title}；工作目录：{snapshot.summary.workdir}"
        )
        box = self.query_one("#composer", ComposerInput)
        box.value = snapshot.draft
        box.cursor_position = min(snapshot.cursor_position, len(box.value))
        box.focus()
        self._render_task_status()

    def action_new_session(self) -> None:
        """立即创建并切换到空白会话；原会话任务继续在后台运行。"""
        if self.session_manager is None:
            self._system("非持久模式不支持新建会话")
            return
        self._save_active_draft()

        async def runner() -> None:
            assert self.session_manager is not None
            try:
                await self.session_manager.create_session()
            except Exception as exc:
                self._write("system", f"新建会话失败：{exc}", "bold red")
                return
            self._bind_active_runtime()
            self._render_active_session()

        self.run_worker(runner())

    def _on_agent_event(self, name: str, ev: AgentEvent) -> None:
        command_value = ev.meta.get("command_id")
        command_id = str(command_value) if command_value else None
        if command_id is not None and ev.meta.get("workflow") is True:
            self._set_workflow_status(command_id, ev.meta)
        if ev.kind == "committed" and name == "user":
            # 用户消息已持久确认：此时才显示文本和派发状态
            if command_id is not None:
                self._activity_feed.mark_request_committed(command_id)
            self._write("user", ev.text)
            if command_id is not None:
                self._set_command_status(command_id, "running")
            roles = ev.meta.get("workflow_roles")
            if ev.meta.get("workflow") is True and isinstance(roles, dict):
                reviewer = str(roles.get("reviewer") or "")
                implementer = str(roles.get("implementer") or "")
                verifier = str(roles.get("verifier") or "")
                if command_id is not None:
                    reviewer_phase = (
                        "审查中（后续复核）"
                        if reviewer and reviewer == verifier else "审查中"
                    )
                    if reviewer:
                        self._set_agent_status(
                            command_id, reviewer, "running", reviewer_phase)
                    if implementer:
                        self._set_agent_status(
                            command_id, implementer, "queued", "等待实现")
                        self._activity_feed.record_status(
                            command_id, implementer, "等待实现", state="queued")
                    if verifier and verifier != reviewer:
                        self._set_agent_status(
                            command_id, verifier, "queued", "等待复核")
                        self._activity_feed.record_status(
                            command_id, verifier, "等待复核", state="queued")
                    # 排队角色先入模型，真正已开始的 reviewer 最后成为当前焦点。
                    if reviewer:
                        self._activity_feed.record_status(
                            command_id,
                            reviewer,
                            reviewer_phase,
                            state="running",
                        )
                workflow_text = (
                    "workflow 已创建："
                    f"审 {reviewer} → 写 {implementer} → 验 {verifier}"
                )
                if command_id is not None:
                    self._activity_feed.record_note(
                        command_id, "workflow", "workflow", workflow_text)
                else:
                    self._system(workflow_text)
            else:
                targets = self.orch.parse_mentions(ev.text)
                if targets:
                    for target in targets:
                        if command_id is not None:
                            self._set_agent_status(
                                command_id, target, "queued", "等待派发")
                            self._activity_feed.record_status(
                                command_id,
                                target,
                                "思考中…",
                                state="queued",
                            )
                        else:
                            self._system(f"{target} 思考中…")
                else:
                    if command_id is not None:
                        self._set_agent_status(
                            command_id, HOST_NAME, "running", "路由中")
                        self._activity_feed.record_status(
                            command_id,
                            HOST_NAME,
                            "处理中…",
                            state="running",
                        )
                    else:
                        self._system("host 处理中…")
            if command_id is not None:
                self._upsert_activity_card(command_id)
        elif ev.kind == "plan":
            self._finish_stream_text(name)
            if command_id is not None:
                progress_changed = self._ensure_task(
                    command_id).record_collaboration_plan(ev.text)
                activity_changed = self._activity_feed.record_plan_event(
                    command_id, ev.text)
                if progress_changed:
                    self._render_task_status()
                if activity_changed:
                    self._upsert_activity_card(command_id)
            else:
                self._system("收到缺少任务标识的协作计划事件")
        elif ev.kind == "text":
            if command_id is not None:
                self._set_agent_status(
                    command_id, name, "running", "回复中")
                if self._activity_feed.record_status(
                    command_id, name, "回复中", state="running"
                ):
                    self._upsert_activity_card(command_id)
            self._buffer_stream_text(name, ev.text)
        elif ev.kind == "info":
            self._finish_stream_text(name)
            if command_id is not None:
                targets = ev.meta.get("route_targets")
                if name == HOST_NAME and isinstance(targets, list):
                    self._set_agent_status(
                        command_id, HOST_NAME, "completed", "路由完成")
                    collaboration = ev.meta.get("collaboration") is True
                    for index, target in enumerate(targets):
                        state = (
                            "running"
                            if not collaboration or index == 0 else "queued"
                        )
                        phase = (
                            "准备执行"
                            if not collaboration or index == 0
                            else "等待前序步骤"
                        )
                        self._set_agent_status(
                            command_id, str(target), state, phase)
                        if collaboration:
                            self._activity_feed.record_status(
                                command_id,
                                str(target),
                                phase,
                                state=state,
                            )
                else:
                    self._set_agent_status(
                        command_id, name, "running",
                        str(ev.meta.get("phase") or ev.text))
                if self._activity_feed.record_note(
                    command_id,
                    name,
                    "info",
                    ev.text,
                    state=(
                        "completed"
                        if name == HOST_NAME and isinstance(targets, list)
                        else "running"
                    ),
                ):
                    self._upsert_activity_card(command_id)
            else:
                self._system(f"{name}: {ev.text}")
        elif ev.kind == "status":
            self._finish_stream_text(name)
            preserve_agent_state = (
                ev.meta.get("collaboration_preserve_agent_state") is True)
            if (command_id is not None
                    and not ev.meta.get("heartbeat")
                    and not preserve_agent_state):
                self._set_agent_status(
                    command_id,
                    name,
                    str(ev.meta.get("agent_state") or "running"),
                    str(ev.meta.get("phase") or ev.text),
                    session_role=(
                        str(ev.meta["session_role"])
                        if isinstance(ev.meta.get("session_role"), str)
                        else None
                    ),
                    allow_reentry=ev.meta.get("collaboration") is True,
                )
            if command_id is not None:
                if preserve_agent_state:
                    if self._activity_feed.record_note(
                        command_id,
                        name,
                        "collaboration",
                        ev.text,
                    ):
                        self._upsert_activity_card(command_id)
                    return
                heartbeat = ev.meta.get("heartbeat") is True
                activity_agent = (
                    str(ev.meta.get("phase") or "任务")
                    if heartbeat and name == "system"
                    else name
                )
                if self._activity_feed.record_status(
                    command_id,
                    activity_agent,
                    ev.text,
                    state=str(ev.meta.get("agent_state") or "running"),
                    heartbeat=heartbeat,
                    session_role=(
                        str(ev.meta["session_role"])
                        if isinstance(ev.meta.get("session_role"), str)
                        else None
                    ),
                ):
                    self._upsert_activity_card(command_id)
            else:
                self._system(f"{name}: {ev.text}")
        elif ev.kind == "tool":
            self._finish_stream_text(name)
            if command_id is not None:
                self._set_agent_status(
                    command_id, name, "running", f"工具：{ev.text}")
                command = ev.meta.get("command")
                tool_call_id = ev.meta.get("tool_call_id")
                detail = (
                    redact_sensitive_text(command, limit=4000)
                    if isinstance(command, str) else ""
                )
                if self._activity_feed.record_tool(
                    command_id,
                    name,
                    str(tool_call_id or ev.text or "anonymous"),
                    ev.text,
                    status=str(ev.meta.get("status") or ""),
                    detail=detail,
                    identity_is_fallback=not bool(tool_call_id),
                ):
                    self._upsert_activity_card(command_id)
            else:
                self._system(f"{name} 使用工具：{ev.text}")
        elif ev.kind == "permission":
            self._finish_stream_text(name)
            if command_id is not None:
                state = (
                    "waiting_permission"
                    if "等待权限" in ev.text
                    else "running"
                )
                self._set_agent_status(
                    command_id, name, state, ev.text)
                if self._activity_feed.record_note(
                    command_id,
                    name,
                    "permission",
                    ev.text,
                    state=state,
                ):
                    self._upsert_activity_card(command_id)
            else:
                self._system(f"{name}: {ev.text}")
        elif ev.kind == "cancel_requested":
            self._finish_stream_text(name)
            if self.session_manager is None:
                self._cancel_pending_permissions()
            else:
                self._cancel_permissions_for(
                    self.session_manager.active_session_id
                )
            # 这里只是请求已发出；真正 terminal 由 CommandBus wait 结果确认。
            # 保留工具状态和运行态，避免取消超时期间短暂显示假成功。
            if command_id is not None:
                self._activity_feed.set_command_state(
                    command_id, "cancelling")
                self._activity_feed.record_note(
                    command_id, "system", "cancel", ev.text)
                self._upsert_activity_card(command_id)
            else:
                self._system(ev.text)
        elif ev.kind == "steering":
            if command_id is not None:
                self._activity_feed.record_note(
                    command_id, name, "steering", ev.text)
                self._upsert_activity_card(command_id)
            self._system(f"steering 已记录：{ev.text}")
        elif ev.kind == "interjection":
            if command_id is not None:
                self._activity_feed.record_note(
                    command_id, name, "interjection", ev.text)
                self._upsert_activity_card(command_id)
            target = str(ev.meta.get("agent") or "agent")
            self._system(f"插话已接受：{target} · {ev.text}")
        elif ev.kind == "error":
            self._finish_stream_text(name)
            if command_id is not None:
                self._set_agent_status(
                    command_id, name, "failed", ev.text)
                if self._activity_feed.record_status(
                    command_id, name, ev.text, state="failed"
                ):
                    self._upsert_activity_card(command_id)
            self._write(name, f"出错：{ev.text}", "bold red")
        elif ev.kind == "done":
            self._finish_stream_text(name)
            if command_id is not None:
                self._set_agent_status(
                    command_id, name, "completed", "本轮响应结束")
                if self._activity_feed.record_status(
                    command_id,
                    name,
                    "本轮响应结束",
                    state="completed",
                ):
                    self._upsert_activity_card(command_id)
            else:
                self._system(f"{name} 本轮响应结束")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local multi-agent TUI")
    parser.add_argument(
        "workdir", nargs="?", default=".",
        help="Agent working directory (default: current directory)")
    parser.add_argument(
        "--session", default=DEFAULT_SESSION_NAME,
        help="Create or resume one named conversation session")
    return parser.parse_args(argv)


def run_chat_loop(
        workdir: str, session_name: str, *, app_factory=ChatApp) -> None:
    """启动一个 App；其内部 SessionManager 管理全部会话 runtime。"""
    app_factory(
        workdir,
        session_name=normalize_session_name(session_name),
    ).run()


def run_cli(
        argv: list[str] | None = None, *,
        runner=run_chat_loop) -> None:
    """解析 CLI 并把可恢复的启动冲突转换为无 traceback 的操作提示。"""
    args = parse_args(argv)
    try:
        runner(
            args.workdir,
            args.session,
        )
    except RoomBusyError as exc:
        session = normalize_session_name(args.session)
        workdir = shlex.quote(str(args.workdir))
        raise SystemExit(
            f"myagents: 会话 {session!r} 已在另一个 TUI 中运行。\n"
            f"{exc}\n"
            "请关闭已有 TUI，或打开一个新会话：\n"
            f"  uv run main.py --session <新名称> {workdir}"
        ) from None


def main() -> None:
    run_cli()


if __name__ == "__main__":
    main()

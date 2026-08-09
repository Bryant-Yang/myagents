"""myagents —— 终端多 agent 聊天室。

用法：
    .venv/bin/python main.py            # 在当前目录启动
    .venv/bin/python main.py /path/to/project   # 指定 agent 的工作目录

聊天室里 @kimi / @opencode / @codex 把消息派发给对应 agent，支持一条消息
@多个（并发执行）。@host 叫主持人（由 codex 扮演）出来总结/仲裁；不带 @
的消息由 host 用一次调用直接回答或决定派给谁。
`/discuss` 可在一个 CommandBus command 内安排 2–3 个 worker 做 1–3 轮
有界讨论，再由指定 moderator 最终仲裁。

接入协议：可靠官方长连接优先。Kimi/OpenCode 走 ACP 长驻会话，Codex 走原生
app-server；ACP prepare 失败才进入受限 JSONL。权限请求会弹窗交给用户决策。启动信息里
能看到每个 agent 的传输协议。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shlex
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, RichLog, Static

from orchestrator import HOST_NAME, Orchestrator
from adapters.base import (
    AgentEvent,
    redact_sensitive_text,
    tool_status_label,
)
from control import CommandBus, ControlServer
from clipboard_image import ClipboardImageError, capture_clipboard_png
from discussion import DISCUSSION_USAGE
from workflow import (
    STEER_USAGE,
    WORKFLOW_USAGE,
    WorkflowValidationError,
    parse_steer_instruction,
)
from storage.store import (
    DEFAULT_SESSION_NAME,
    RoomBusyError,
    RoomStore,
    WorkdirMismatchError,
    normalize_session_name,
    normalize_workdir,
)
from tui_completion import (
    LOCAL_COMMANDS,
    CompletionContext,
    completion_context,
    local_command_for,
    unknown_mentions,
)
from tui_status import TaskProgress

# 每个发言者的显示颜色
_COLORS = {"user": "yellow", "kimi": "cyan", "opencode": "green",
           "codex": "orange1", "host": "magenta"}

# 权限弹窗的固定应答：用户取消 / 退出 TUI 兜底
_CANCELLED = {"outcome": "cancelled"}
_STREAM_RENDER_INTERVAL = 0.05
_SENSITIVE_DETAIL_KEYS = {
    "token", "secret", "password", "authorization", "api_key", "apikey",
    "credential", "private_key"}


def _tool_detail(tool: object) -> str:
    """提取适合给用户看的工具上下文；隐藏常见凭据字段并限制长度。"""
    if not isinstance(tool, dict):
        return ""

    def scrub(value: object, key: str = "") -> object:
        lowered = key.lower()
        if any(marker in lowered for marker in _SENSITIVE_DETAIL_KEYS):
            return "[已隐藏]"
        if isinstance(value, dict):
            return {str(k): scrub(v, str(k)) for k, v in value.items()
                    if str(k) not in {"title", "toolCallId"}}
        if isinstance(value, list):
            return [scrub(item) for item in value[:20]]
        if isinstance(value, str):
            return redact_sensitive_text(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:2000]

    cleaned = scrub(tool)
    if not isinstance(cleaned, dict) or not cleaned:
        return ""
    rendered = json.dumps(cleaned, ensure_ascii=False, indent=2)
    return rendered[:4000]


class PermissionScreen(ModalScreen):
    """ACP 权限请求弹窗：显示工具标题和 agent 给的 options，
    用户选 allow / reject / cancel，结果作为 ACP outcome 回给 agent。"""

    BINDINGS = [
        ("escape", "cancel", "取消权限"),
        ("ctrl+x", "cancel_task", "取消任务"),
    ]

    def __init__(self, agent_name: str, params: dict) -> None:
        super().__init__()
        self._agent_name = agent_name
        tool = params.get("toolCall", {})
        if not isinstance(tool, dict):
            tool = {}
        self._title = redact_sensitive_text(
            str(tool.get("title") or "(未命名工具)"),
            limit=500,
        )
        self._detail = _tool_detail(tool)
        # optionId 不一定是合法 DOM id，用序号映射
        self._options = {f"perm-opt-{i}": opt
                         for i, opt in enumerate(params.get("options", []))}

    def compose(self) -> ComposeResult:
        with Vertical(id="perm-dialog"):
            text = f"{self._agent_name} 请求权限：{self._title}"
            if self._detail:
                text += f"\n{self._detail}"
            yield Label(text)
            for bid, opt in self._options.items():
                yield Button(opt.get("name") or opt.get("optionId", "?"), id=bid)
            yield Button("取消", id="perm-cancel", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        opt = self._options.get(event.button.id or "")
        if opt is None:  # 取消按钮
            self.dismiss(dict(_CANCELLED))
        else:
            self.dismiss({"outcome": "selected",
                          "optionId": opt.get("optionId", "")})

    def action_cancel(self) -> None:  # Esc
        self.dismiss(dict(_CANCELLED))

    def action_cancel_task(self) -> None:
        self.app.action_cancel_active()


@dataclass(frozen=True)
class NewSessionRequest:
    """ChatApp 正常退出后，由顶层循环打开的新会话。"""

    session_name: str


class NewSessionScreen(ModalScreen[str | None]):
    """新会话命名弹窗；空名称由 ChatApp 生成稳定可见的默认名。"""

    BINDINGS = [("escape", "cancel", "取消")]

    def compose(self) -> ComposeResult:
        with Vertical(id="session-dialog"):
            yield Label("新会话名称（留空自动生成）")
            yield Input(
                placeholder="例如：game-review",
                id="session-name",
            )
            yield Button("创建", id="session-create", variant="primary")
            yield Button("取消", id="session-cancel")

    def on_mount(self) -> None:
        self.query_one("#session-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "session-create":
            value = self.query_one("#session-name", Input).value
            self.dismiss(value)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ComposerInput(Input):
    """输入框保持焦点时处理候选导航，不让 Enter 提前提交消息。"""

    BINDINGS = [
        Binding("up", "completion_previous", show=False),
        Binding("down", "completion_next", show=False),
        Binding("tab", "completion_accept", show=False),
        Binding("escape", "completion_close", show=False),
        *Input.BINDINGS,
    ]

    def action_completion_previous(self) -> None:
        self.app.move_completion(-1)

    def action_completion_next(self) -> None:
        self.app.move_completion(1)

    def action_completion_accept(self) -> None:
        if not self.app.accept_completion():
            self.app.action_focus_next()

    def action_completion_close(self) -> None:
        self.app.close_completion()

    async def action_submit(self) -> None:
        if not self.app.accept_completion():
            await super().action_submit()

    def action_paste(self) -> None:
        """文本剪贴板沿用 Textual；空文本时尝试读取系统图片。"""
        if self.app.clipboard:
            super().action_paste()
        else:
            self.app.action_paste_image()


class ChatApp(App):
    BINDINGS = [
        ("ctrl+n", "new_session", "新会话"),
        ("ctrl+x", "cancel_active", "取消当前任务"),
    ]
    CSS = """
    RichLog { border: round $primary; }
    Input { border: round $secondary; }
    #task-status {
        height: auto;
        min-height: 3;
        padding: 0 1;
        border: round $secondary;
        color: $text-muted;
    }
    #completion-list {
        display: none;
        height: auto;
        max-height: 8;
        padding: 0 1;
        border: round $accent;
        background: $surface;
    }
    PermissionScreen { align: center middle; }
    #perm-dialog {
        width: 60; height: auto; padding: 1 2;
        border: round $warning; background: $surface;
    }
    #perm-dialog Button { width: 100%; margin-top: 1; }
    #session-dialog {
        width: 60; height: auto; padding: 1 2;
        border: round $primary; background: $surface;
    }
    #session-dialog Button { width: 100%; margin-top: 1; }
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
            self.orch = Orchestrator(
                workdir,
                persistent=persistent,
                session_name=requested_session,
            )
        self.session_name = self.orch.session_name
        # 唯一命令入口：TUI 输入和未来外部控制统一经 CommandBus FIFO 派发；
        # bus 不拥有 orch（aclose 只停自己的 worker）
        self.bus = CommandBus(self.orch, self._on_agent_event)
        self.control_server = (
            ControlServer(self.orch, self.bus)
            if self.orch.store is not None else None
        )
        # 等待用户决策的权限 Future：退出时必须全部按 cancelled 收尾，
        # 不留挂起的 Future（adapter 侧的 prompt 才不会傻等）。
        self._permission_futures: set[asyncio.Future] = set()
        # RichLog 只能 append，直接写 ACP token/chunk 会变成“一词一行”。
        # 这里保留逻辑时间线，并按最多 20fps 把同一回复更新到同一条记录。
        self._display_lines: list[tuple[str, str, str]] = []
        self._stream_text: dict[str, str] = {}
        self._stream_line_index: dict[str, int] = {}
        self._stream_flush_handles: dict[str, asyncio.TimerHandle] = {}
        self._heartbeat_line_index: dict[str, int] = {}
        self._tool_line_index: dict[tuple[str, str, str], int] = {}
        self._tool_line_fingerprint: dict[
            tuple[str, str, str], tuple[str, str, str]
        ] = {}
        self._tool_latest: dict[
            tuple[str, str, str], tuple[str, AgentEvent]
        ] = {}
        self._show_tool_details = False
        self._clipboard_image_capture = clipboard_image_capture
        self._image_paste_in_progress = False
        self._task_progresses: dict[str, TaskProgress] = {}
        self._task_started_at: dict[str, float] = {}
        self._latest_task_id: str | None = None
        self._completion: CompletionContext | None = None
        self._completion_index = 0
        self._suppress_completion_value: str | None = None

    def compose(self) -> ComposeResult:
        yield RichLog(wrap=True)
        yield Static(id="task-status")
        yield Static(id="completion-list")
        yield ComposerInput(
            placeholder=(
                "@kimi @opencode 点名派发；/new 或 Ctrl+N 新会话；"
                "Ctrl+X 取消当前任务；Ctrl+C 退出"
            ),
            id="composer",
        )
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "myagents"
        # TUI 权限决策器注入所有 ACP adapter；未注入时 ACP 一律 deny
        self.orch.set_permission_handler(self._acp_permission)
        self.bus.start()
        self.set_interval(1.0, self._render_task_status)
        self._render_task_status()
        if self.control_server is not None:
            await self.control_server.start()
        # 恢复显示：按 seq 顺序渲染持久化历史（只渲染，不 append）
        restored = sorted(self.orch.history, key=lambda m: m.seq)
        for message in restored:
            self._write(message.speaker, message.text)
        if restored:
            self._system(f"已恢复 {len(restored)} 条历史消息")
        self._restore_interrupted_executions()
        agents = " ".join(f"@{s.name}({s.transport.upper()})" for s in self.orch.specs)
        self._system(f"聊天室已就绪：{agents} @host；不带 @ 的消息由 host 处理；"
                     f"会话：{self.session_name}；工作目录：{self.orch.workdir}")
        self.query_one("#composer", ComposerInput).focus()

    async def on_unmount(self) -> None:
        # 先放行等待中的权限请求（cancelled），再停 bus worker（取消进行
        # 中的 dispatch），最后统一回收 adapter——顺序反过来会死锁：
        # aclose 等的锁可能被等权限的 prompt 持有。bus 不拥有 orch。
        for handle in self._stream_flush_handles.values():
            handle.cancel()
        self._stream_flush_handles.clear()
        self._cancel_pending_permissions()
        try:
            if self.control_server is not None:
                await self.control_server.aclose()
            await self.bus.aclose()
        finally:
            # 控制层或事件日志收尾失败，也不能跳过 adapter/进程组回收。
            await self.orch.aclose()

    # ---- ACP 权限决策 ----

    async def _acp_permission(self, agent_name: str, params: dict) -> dict:
        """ACP adapter 的权限决策回调：弹窗等用户选，返回 ACP outcome。"""
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
        if not mirrors_events:
            self._record_permission_event(
                command_id, agent_name, f"等待权限：{title}")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._permission_futures.add(fut)
        self.push_screen(
            PermissionScreen(agent_name, params),
            lambda outcome: self._resolve_permission(fut, outcome),
        )
        try:
            outcome = await fut
            if outcome.get("outcome") == "selected":
                option_id = outcome.get("optionId", "")
                text = f"权限已选择：{option_id}"
            else:
                text = "权限已取消或拒绝"
            if not mirrors_events:
                self._record_permission_event(
                    command_id, agent_name, text)
            return outcome
        finally:
            self._permission_futures.discard(fut)
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

    def _record_permission_event(
            self, command_id: str | None, agent_name: str, text: str) -> None:
        self._system(f"{agent_name}: {text}")
        if command_id is not None:
            state = (
                "waiting_permission"
                if text.startswith("等待权限：")
                else "running"
            )
            self._set_agent_status(command_id, agent_name, state, text)
        store = self.orch.store
        if command_id is not None and store is not None:
            store.append_event(
                command_id=command_id, agent=agent_name,
                kind="permission", text=text)

    def _restore_interrupted_executions(self) -> None:
        """启动时诚实显示上轮未留下 terminal 事件的命令。"""
        store = self.orch.store
        if store is None or not hasattr(store, "read_events"):
            return
        terminal = {"completed", "failed", "cancelled"}
        interrupted = [
            item for item in store.latest_execution_events()
            if item.kind not in terminal]
        for item in interrupted:
            progress = self._ensure_task(item.command_id)
            progress.set_command("interrupted", item.text)
            progress.set_agent(item.agent, "interrupted", item.text)
            self._system(
                f"上次任务 {item.command_id[:8]} 已中断；"
                f"最后状态：{item.agent} {item.text}")
        self._render_task_status()

    # ---- 时间线输出 ----

    @staticmethod
    def _line(speaker: str, text: str, style: str = "") -> Text:
        color = _COLORS.get(speaker, "white")
        return Text.assemble((f"[{speaker}] ", f"bold {color}"), (text, style))

    def _write(self, speaker: str, text: str, style: str = "") -> None:
        self._display_lines.append((speaker, text, style))
        self.query_one(RichLog).write(self._line(speaker, text, style))

    def _render_display_lines(self) -> None:
        """重绘逻辑时间线，让正在流式增长的回复仍只占一条记录。"""
        log = self.query_one(RichLog)
        log.clear()
        for speaker, text, style in self._display_lines:
            log.write(self._line(speaker, text, style))

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
        else:
            self._display_lines[index] = (name, text, "")
        self._render_display_lines()

    def _finish_stream_text(self, name: str) -> None:
        """同步刷完尾部 chunk，并结束这一段逻辑回复。"""
        self._flush_stream_text(name)
        self._stream_text.pop(name, None)
        self._stream_line_index.pop(name, None)

    def _upsert_tool_line(self, name: str, ev: AgentEvent) -> None:
        """每个 command/tool 只占一条逻辑记录，状态变化原位更新。"""
        command_id = str(ev.meta.get("command_id") or "current")
        tool_identity = str(
            ev.meta.get("tool_call_id") or ev.text or "anonymous")
        key = (command_id, name, tool_identity)
        self._tool_latest[key] = (name, ev)
        status = tool_status_label(ev.meta.get("status"))
        line = f"{name} 使用工具：{ev.text}"
        if status:
            line += f" · {status}"
        command = ev.meta.get("command")
        if isinstance(command, str) and command:
            if self._show_tool_details:
                line += f"\n  {command}"
            else:
                line += " · /details 查看"
        index = self._tool_line_index.get(key)
        rendered = ("system", line, "dim")
        if self._tool_line_fingerprint.get(key) == rendered:
            return
        self._tool_line_fingerprint[key] = rendered
        if index is None:
            self._tool_line_index[key] = len(self._display_lines)
            self._display_lines.append(rendered)
        else:
            self._display_lines[index] = rendered
        self._render_display_lines()

    def _clear_tool_state(
            self, command_id: object, name: str | None = None) -> None:
        if not command_id:
            return
        target = str(command_id)
        keys = [
            key for key in self._tool_line_index
            if key[0] == target and (name is None or key[1] == name)
        ]
        for key in keys:
            self._tool_line_index.pop(key, None)
            self._tool_line_fingerprint.pop(key, None)
            self._tool_latest.pop(key, None)

    def _system(self, text: str) -> None:
        self._write("system", text, "dim")

    # ---- 消息处理 ----

    def _completion_agents(self) -> tuple[tuple[str, str], ...]:
        agents = [(spec.name, spec.transport.upper())
                  for spec in self.orch.specs]
        if all(name != HOST_NAME for name, _ in agents):
            agents.append((HOST_NAME, "MODERATOR"))
        return tuple(agents)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "composer":
            return
        if event.value == self._suppress_completion_value:
            self._suppress_completion_value = None
            self.close_completion()
            return
        self._suppress_completion_value = None
        context = completion_context(
            event.value,
            event.input.cursor_position,
            self._completion_agents(),
        )
        self._completion = context
        self._completion_index = 0
        self._render_completion()

    def _render_completion(self) -> None:
        popup = self.query_one("#completion-list", Static)
        context = self._completion
        if context is None:
            popup.styles.display = "none"
            popup.update("")
            return
        lines = Text()
        for index, item in enumerate(context.items):
            prefix = "› " if index == self._completion_index else "  "
            style = "reverse bold" if index == self._completion_index else ""
            lines.append(
                f"{prefix}{item.label:<14} {item.description}\n",
                style=style,
            )
        popup.update(lines)
        popup.styles.display = "block"

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
        if context.kind == "command" and completed_value == box.value:
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

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

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
            event.input.cursor_position = len(event.value)
            event.input.focus()
            return

        event.input.value = ""
        self.close_completion()
        command = local_command_for(text)
        if command is not None:
            # 本地命令必须在持久化/路由之前截获；未知或带参数的 slash
            # 文本仍按普通消息提交，避免猜测用户语义。
            getattr(self, command.handler)()
            return
        # 不预先显示用户文本/思考状态：等 orchestrator 持久确认
        # （committed 事件）后再显示；append 失败时什么都不出现。
        self._dispatch_from_ui(text)

    def action_show_agents(self) -> None:
        agents = " ".join(
            f"@{name}({transport})"
            for name, transport in self._completion_agents()
        )
        self._system(f"可用 agent：{agents}")

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
        """切换工具命令详情，并原位重绘现有工具记录。"""
        self._show_tool_details = not self._show_tool_details
        latest = list(self._tool_latest.values())
        for name, event in latest:
            self._upsert_tool_line(name, event)

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
            reference = f"[图片附件：{path}]"
            start, end = box.selection
            prefix = "" if start == 0 or box.value[start - 1].isspace() else " "
            suffix = "" if end == len(box.value) or (
                end < len(box.value) and box.value[end].isspace()
            ) else " "
            box.replace(f"{prefix}{reference}{suffix}", start, end)
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
            try:
                snap = await self.bus.submit(text)
            except Exception as exc:
                self._write("system", f"派发失败：{exc}", "bold red")
                return
            self._set_command_status(snap.command_id, snap.status.value)
            while True:  # agent 长任务可能超过单次 wait 上限，等到 terminal
                result = await self.bus.wait(snap.command_id)
                if not result["timed_out"]:
                    break
            self._set_command_status(
                snap.command_id, result["status"], result.get("error"))
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
            self._task_started_at[command_id] = time.monotonic()
        self._latest_task_id = command_id
        return progress

    def _set_command_status(
            self, command_id: str, status: str,
            error: str | None = None) -> None:
        self._ensure_task(command_id).set_command(status, error)
        self._render_task_status()

    def _set_agent_status(
            self, command_id: str, name: str,
            state: str, phase: str = "") -> None:
        self._ensure_task(command_id).set_agent(name, state, phase)
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
        panel = self.query_one("#task-status", Static)
        progress = self._selected_task()
        if progress is None:
            panel.update("空闲 · 输入 @ 选择 agent，输入 / 查看命令")
            return
        with contextlib.suppress(Exception):
            snapshot = self.bus.get(progress.command_id)
            progress.set_command(snapshot.status.value, snapshot.error)
        started = self._task_started_at.get(
            progress.command_id, time.monotonic())
        panel.update(progress.render(time.monotonic() - started))

    def action_cancel_active(self) -> None:
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

    def action_new_session(self) -> None:
        """请求创建全新持久会话；切换由顶层 App 循环在 unmount 后完成。"""
        if self.bus.has_pending() or self._permission_futures:
            self._system("先完成或取消当前任务，再新建会话")
            return
        if self.orch.store is None:
            self._system("非持久模式不支持新建会话")
            return
        self.push_screen(
            NewSessionScreen(),
            self._accept_new_session_name,
        )

    @staticmethod
    def _generated_session_name() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"chat-{stamp}-{uuid.uuid4().hex[:6]}"

    def _accept_new_session_name(self, value: str | None) -> None:
        if value is None:
            return
        if self.bus.has_pending() or self._permission_futures:
            self._system("当前状态已变化；先完成或取消当前任务")
            return
        store = self.orch.store
        if store is None:
            self._system("非持久模式不支持新建会话")
            return
        candidate = value if value.strip() else self._generated_session_name()
        try:
            name = normalize_session_name(candidate)
        except ValueError as exc:
            self._write("system", f"新会话名称无效：{exc}", "bold red")
            return
        if RoomStore.session_exists(
                self.orch.workdir,
                name,
                state_root=store.state_root):
            self._write(
                "system",
                f"会话已存在：{name}；请换一个名称，"
                f"恢复该会话请使用 --session {name}",
                "bold red",
            )
            return
        self.exit(NewSessionRequest(name))

    def _on_agent_event(self, name: str, ev: AgentEvent) -> None:
        command_value = ev.meta.get("command_id")
        command_id = str(command_value) if command_value else None
        if command_id is not None and ev.meta.get("workflow") is True:
            self._set_workflow_status(command_id, ev.meta)
        if ev.kind == "committed" and name == "user":
            # 用户消息已持久确认：此时才显示文本和派发状态
            self._write("user", ev.text)
            if command_id is not None:
                self._set_command_status(command_id, "running")
            roles = ev.meta.get("workflow_roles")
            if ev.meta.get("workflow") is True and isinstance(roles, dict):
                reviewer = str(roles.get("reviewer") or "")
                implementer = str(roles.get("implementer") or "")
                verifier = str(roles.get("verifier") or "")
                if command_id is not None:
                    if reviewer:
                        phase = (
                            "审查中（后续复核）"
                            if reviewer == verifier else "审查中"
                        )
                        self._set_agent_status(
                            command_id, reviewer, "running", phase)
                    if implementer:
                        self._set_agent_status(
                            command_id, implementer, "queued", "等待实现")
                    if verifier and verifier != reviewer:
                        self._set_agent_status(
                            command_id, verifier, "queued", "等待复核")
                self._system(
                    "workflow 已创建："
                    f"审 {reviewer} → 写 {implementer} → 验 {verifier}"
                )
            else:
                targets = self.orch.parse_mentions(ev.text)
                if targets:
                    for target in targets:
                        if command_id is not None:
                            self._set_agent_status(
                                command_id, target, "queued", "等待派发")
                        self._system(f"{target} 思考中…")
                else:
                    if command_id is not None:
                        self._set_agent_status(
                            command_id, HOST_NAME, "running", "路由中")
                    self._system("host 处理中…")
        elif ev.kind == "text":
            if command_id is not None:
                self._set_agent_status(
                    command_id, name, "running", "回复中")
            self._buffer_stream_text(name, ev.text)
        elif ev.kind == "info":
            self._finish_stream_text(name)
            if command_id is not None:
                targets = ev.meta.get("route_targets")
                if name == HOST_NAME and isinstance(targets, list):
                    self._set_agent_status(
                        command_id, HOST_NAME, "completed", "路由完成")
                    for target in targets:
                        self._set_agent_status(
                            command_id, str(target), "running", "准备执行")
                else:
                    self._set_agent_status(
                        command_id, name, "running",
                        str(ev.meta.get("phase") or ev.text))
            self._system(f"{name}: {ev.text}")
        elif ev.kind == "status":
            self._finish_stream_text(name)
            if command_id is not None and not ev.meta.get("heartbeat"):
                self._set_agent_status(
                    command_id,
                    name,
                    str(ev.meta.get("agent_state") or "running"),
                    str(ev.meta.get("phase") or ev.text),
                )
            if ev.meta.get("heartbeat") is True and command_id:
                index = self._heartbeat_line_index.get(command_id)
                line = (ev.text if name == "system"
                        else f"{name}: {ev.text}")
                if index is None:
                    self._heartbeat_line_index[command_id] = len(
                        self._display_lines)
                    self._display_lines.append(("system", line, "dim"))
                else:
                    self._display_lines[index] = ("system", line, "dim")
                self._render_display_lines()
            else:
                self._system(f"{name}: {ev.text}")
        elif ev.kind == "tool":
            self._finish_stream_text(name)
            if command_id is not None:
                self._set_agent_status(
                    command_id, name, "running", f"工具：{ev.text}")
            self._upsert_tool_line(name, ev)
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
            self._system(f"{name}: {ev.text}")
        elif ev.kind == "cancel_requested":
            self._finish_stream_text(name)
            self._cancel_pending_permissions()
            # 这里只是请求已发出；真正 terminal 由 CommandBus wait 结果确认。
            # 保留工具状态和运行态，避免取消超时期间短暂显示假成功。
            self._system(ev.text)
        elif ev.kind == "steering":
            self._system(f"steering 已记录：{ev.text}")
        elif ev.kind == "error":
            self._finish_stream_text(name)
            self._clear_tool_state(ev.meta.get("command_id"), name)
            if command_id is not None:
                self._set_agent_status(
                    command_id, name, "failed", ev.text)
            self._write(name, f"出错：{ev.text}", "bold red")
        elif ev.kind == "done":
            self._finish_stream_text(name)
            self._clear_tool_state(ev.meta.get("command_id"), name)
            if command_id is not None:
                self._set_agent_status(
                    command_id, name, "completed", "本轮响应结束")
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
        workdir: str, session_name: str, *,
        app_factory=ChatApp) -> None:
    """同进程切换：旧 App 完整 unmount 后才构造下一会话。"""
    current = normalize_session_name(session_name)
    while True:
        result = app_factory(
            workdir,
            session_name=current,
        ).run()
        if not isinstance(result, NewSessionRequest):
            return
        current = result.session_name


def run_cli(
        argv: list[str] | None = None, *,
        runner=run_chat_loop) -> None:
    """解析 CLI 并把可恢复的启动冲突转换为无 traceback 的操作提示。"""
    args = parse_args(argv)
    try:
        runner(args.workdir, args.session)
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

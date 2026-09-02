"""连接 daemon owner 的轻量 Textual 客户端。

AttachedChatApp 不创建 RoomStore、Orchestrator、CommandBus 或 agent 进程；退出
只断开界面。所有读写都经过受信 ControlClient endpoint。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Static

from control import ControlClient, ControlClientError
from tui_markdown import render_chat_markdown


_SPEAKER_STYLES = {
    "user": "bold yellow",
    "host": "bold magenta",
    "kimi": "bold cyan",
    "opencode": "bold green",
    "qwen": "bold bright_blue",
    "codebuddy": "bold bright_magenta",
    "dsh": "bold spring_green2",
    "pi": "bold deep_sky_blue1",
    "codex": "bold orange1",
}


def _bounded_text(value: object, *, limit: int) -> str:
    """控制面已脱敏；客户端再做长度与控制字符收口。"""
    text = str(value).replace("\x1b", "").replace("\x00", "")
    return text[:limit]


class AttachedPermissionScreen(ModalScreen[dict[str, str] | None]):
    """只解决 daemon 已登记的本次 option 闭集。"""

    BINDINGS = [("escape", "leave_pending", "稍后处理")]

    def __init__(self, request: dict[str, Any]) -> None:
        super().__init__()
        self.request = request
        options = request.get("options")
        if not isinstance(options, list):
            options = []
        self._options = {
            f"attached-permission-{index}": option
            for index, option in enumerate(options)
            if isinstance(option, dict)
            and isinstance(option.get("option_id"), str)
        }

    def compose(self) -> ComposeResult:
        tool_call = self.request.get("tool_call")
        if not isinstance(tool_call, dict):
            tool_call = {}
        agent = _bounded_text(
            self.request.get("agent") or "agent", limit=80)
        title = _bounded_text(
            tool_call.get("title") or "(未命名工具)", limit=500)
        with Vertical(id="attached-permission-dialog"):
            yield Label(
                f"daemon 中的 @{agent} 等待权限\n{title}",
                id="attached-permission-summary",
            )
            for button_id, option in self._options.items():
                label = _bounded_text(
                    option.get("name") or option.get("kind") or "使用此选项",
                    limit=160,
                )
                yield Button(label, id=button_id)
            yield Button(
                "拒绝本次请求",
                id="attached-permission-deny",
                variant="error",
            )
            yield Label("Esc 暂不处理；任务继续等待本机确认")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        option = self._options.get(event.button.id or "")
        if option is None:
            self.dismiss({"outcome": "cancelled"})
            return
        self.dismiss({
            "outcome": "selected",
            "option_id": str(option["option_id"]),
        })

    def action_leave_pending(self) -> None:
        self.dismiss(None)


class AttachedChatApp(App):
    """可关闭、可重新打开的 daemon companion TUI。"""

    BINDINGS = [
        Binding("escape", "cancel_active", "取消当前任务"),
        Binding("ctrl+x", "cancel_active", "取消当前任务"),
    ]

    CSS = """
    #attached-chat { border: round $primary; }
    #attached-status {
        height: auto;
        min-height: 2;
        padding: 0 1;
        color: $text-muted;
        border: round $secondary;
    }
    #attached-composer { border: round $secondary; }
    AttachedPermissionScreen { align: center middle; }
    #attached-permission-dialog {
        width: 72;
        max-width: 92%;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #attached-permission-dialog Button { width: 100%; margin-top: 1; }
    #attached-permission-summary { margin-bottom: 1; }
    """

    def __init__(
        self,
        client: ControlClient,
        *,
        poll_interval: float = 0.5,
    ) -> None:
        super().__init__()
        if poll_interval <= 0:
            raise ValueError("poll_interval 必须为正数")
        self.client = client
        self.poll_interval = poll_interval
        self._last_seq = 0
        self._polling = False
        self._detaching = False
        self._permission_request_id: str | None = None
        self._owner: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        yield RichLog(wrap=True, id="attached-chat")
        yield Static("正在连接 daemon…", id="attached-status")
        yield Input(
            placeholder="输入消息；关闭此界面不会停止后台任务",
            id="attached-composer",
        )

    async def on_mount(self) -> None:
        owner = await self.client.get_room()
        if owner.get("owner_kind") != "daemon":
            raise RuntimeError("attach 只连接 daemon owner")
        self._owner = owner
        self._set_status("已附着")
        await self._poll_once()
        self.set_interval(self.poll_interval, self._schedule_poll)
        self.query_one("#attached-composer", Input).focus()

    def _schedule_poll(self) -> None:
        if self._polling or self._detaching or not self.is_mounted:
            return
        self.run_worker(self._poll_once())

    async def _poll_once(self) -> None:
        if self._polling or self._detaching or not self.is_mounted:
            return
        self._polling = True
        try:
            page = await self.client.read_timeline(
                after_seq=self._last_seq, limit=200
            )
            if self._detaching or not self.is_mounted:
                return
            try:
                log = self.query_one("#attached-chat", RichLog)
            except NoMatches:
                # Screen 树可能已进入卸载窗口，但 on_unmount 尚未来得及执行。
                return
            for item in page["items"]:
                speaker = str(item.get("speaker") or "system")
                text = str(item.get("text") or "")
                line = Text(f"[{speaker}] ", style=_SPEAKER_STYLES.get(
                    speaker, "bold bright_black"
                ))
                if speaker in {"user", "system", "activity"}:
                    line.append(text)
                else:
                    line.append_text(render_chat_markdown(text))
                log.write(line)
                seq = item.get("seq")
                if isinstance(seq, int) and not isinstance(seq, bool):
                    self._last_seq = max(self._last_seq, seq)
            await self._poll_permission()
            if self._detaching or not self.is_mounted:
                return
            commands = await self.client.list_commands(limit=200)
            items = commands.get("items")
            if not isinstance(items, list):
                items = []
            running = next(
                (item for item in items
                 if isinstance(item, dict)
                 and item.get("status") == "running"),
                None,
            )
            queued = sum(
                1 for item in items
                if isinstance(item, dict) and item.get("status") == "queued"
            )
            if running is None:
                activity = f"空闲 · 排队 {queued}" if queued else "空闲"
            else:
                command_id = str(running.get("command_id") or "")[:8]
                activity = f"运行 {command_id} · 排队 {queued}"
            self._set_status(f"已附着 · {activity}")
        except ControlClientError as exc:
            self._set_status(f"连接中断：{exc}", error=True)
        finally:
            self._polling = False

    async def _poll_permission(self) -> None:
        if (self._permission_request_id is not None
                or self._detaching or not self.is_mounted):
            return
        pending = await self.client.list_permissions()
        if self._detaching or not self.is_mounted:
            return
        items = pending.get("items")
        if not isinstance(items, list) or not items:
            return
        request = items[0]
        if not isinstance(request, dict):
            return
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return
        self._permission_request_id = request_id
        self.push_screen(
            AttachedPermissionScreen(request),
            lambda result: self._accept_permission(request_id, result),
        )

    def _accept_permission(
        self,
        request_id: str,
        result: dict[str, str] | None,
    ) -> None:
        self._permission_request_id = None
        if result is None:
            self._set_status("权限仍在 daemon 中等待本机确认")
            return

        async def resolve() -> None:
            try:
                await self.client.resolve_permission(
                    request_id,
                    outcome=result["outcome"],
                    option_id=result.get("option_id"),
                )
                self._set_status("权限选择已提交")
            except ControlClientError as exc:
                self._set_status(f"权限提交失败：{exc}", error=True)

        self.run_worker(resolve())

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value.strip()
        if not message:
            return
        event.input.value = ""
        request_id = str(uuid.uuid4())
        try:
            command = await self.client.submit(
                message, request_id=request_id
            )
        except ControlClientError as exc:
            event.input.value = message
            self._set_status(f"发送失败：{exc}", error=True)
            return
        short_id = str(command.get("command_id") or "")[:8]
        self._set_status(f"已排队：{short_id}")
        await self._poll_once()

    async def action_cancel_active(self) -> None:
        """取消 daemon 当前任务；没有 running 时取消最早 queued。"""
        try:
            page = await self.client.list_commands(limit=200)
            items = page.get("items")
            if not isinstance(items, list):
                items = []
            target = next(
                (item for item in items
                 if isinstance(item, dict)
                 and item.get("status") == "running"),
                None,
            )
            if target is None:
                target = next(
                    (item for item in items
                     if isinstance(item, dict)
                     and item.get("status") == "queued"),
                    None,
                )
            if target is None:
                self._set_status("当前没有可取消的任务")
                return
            command_id = target.get("command_id")
            if not isinstance(command_id, str) or not command_id:
                self._set_status("任务状态异常，未执行取消", error=True)
                return
            await self.client.cancel_command(command_id)
            self._set_status(f"已取消：{command_id[:8]}")
        except ControlClientError as exc:
            self._set_status(f"取消失败：{exc}", error=True)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        if self._detaching or not self.is_mounted:
            return
        owner = self._owner or {}
        prefix = (
            f"daemon PID {owner.get('pid')} · "
            f"会话 {owner.get('session_name', self.client.session_name)}"
        )
        rendered = Text(f"{prefix}\n{text}")
        if error:
            rendered.stylize("bold red")
        with contextlib.suppress(NoMatches):
            self.query_one("#attached-status", Static).update(rendered)

    async def on_unmount(self) -> None:
        # 客户端没有 owner 资源；故意不发送 shutdown/cancel/permission resolve。
        self._detaching = True
        await asyncio.sleep(0)

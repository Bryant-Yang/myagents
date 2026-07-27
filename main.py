"""myagents —— 终端多 agent 聊天室。

用法：
    .venv/bin/python main.py            # 在当前目录启动
    .venv/bin/python main.py /path/to/project   # 指定 agent 的工作目录

聊天室里 @kimi / @opencode / @codex 把消息派发给对应 agent，支持一条消息
@多个（并发执行）。@host 叫主持人（由 codex 扮演）出来总结/仲裁；不带 @
的消息由 host 用一次调用直接回答或决定派给谁。

接入协议：可靠官方长连接优先。Kimi 走 ACP 长驻会话，Codex 走原生
app-server，OpenCode 走 JSONL；权限请求会弹窗交给用户决策。启动信息里
能看到每个 agent 的传输协议。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, RichLog

from orchestrator import Orchestrator
from adapters.base import AgentEvent, redact_sensitive_text
from control import CommandBus, ControlServer
from storage.store import WorkdirMismatchError, normalize_workdir

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


class ChatApp(App):
    BINDINGS = [
        ("ctrl+x", "cancel_active", "取消当前任务"),
    ]
    CSS = """
    RichLog { border: round $primary; }
    Input { border: round $secondary; }
    PermissionScreen { align: center middle; }
    #perm-dialog {
        width: 60; height: auto; padding: 1 2;
        border: round $warning; background: $surface;
    }
    #perm-dialog Button { width: 100%; margin-top: 1; }
    """

    def __init__(self, workdir: str, persistent: bool = True, *,
                 orchestrator: Orchestrator | None = None) -> None:
        super().__init__()
        if orchestrator is not None:
            # 注入的 orchestrator 必须属于同一房间，不能静默换房
            if normalize_workdir(orchestrator.workdir) \
                    != normalize_workdir(workdir):
                raise WorkdirMismatchError(
                    f"orchestrator 属于 {orchestrator.workdir!r}，"
                    f"与 workdir {workdir!r} 不一致")
            self.orch = orchestrator
        else:
            self.orch = Orchestrator(workdir, persistent=persistent)
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

    def compose(self) -> ComposeResult:
        yield RichLog(wrap=True)
        yield Input(placeholder="@kimi @opencode 点名派发；Ctrl+X 取消当前任务；Ctrl+C 退出")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "myagents"
        # TUI 权限决策器注入所有 ACP adapter；未注入时 ACP 一律 deny
        self.orch.set_permission_handler(self._acp_permission)
        self.bus.start()
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
                     f"工作目录：{self.orch.workdir}")
        self.query_one(Input).focus()

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
            self._system(
                f"上次任务 {item.command_id[:8]} 已中断；"
                f"最后状态：{item.agent} {item.text}")

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

    def _system(self, text: str) -> None:
        self._write("system", text, "dim")

    # ---- 消息处理 ----

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        # 不预先显示用户文本/思考状态：等 orchestrator 持久确认
        # （committed 事件）后再显示；append 失败时什么都不出现。
        self._dispatch_from_ui(text)

    def _dispatch_from_ui(self, text: str) -> None:
        """经 CommandBus 提交（唯一入口，不再直接 orch.dispatch）。

        提交/容量/closed 异常与执行期失败（如持久化错误 → 命令 failed）
        都显示为红色 system 错误；bus FIFO 串行执行，事件经 event_sink
        回到 _on_agent_event。
        """
        async def runner() -> None:
            try:
                snap = await self.bus.submit(text)
            except Exception as exc:
                self._write("system", f"派发失败：{exc}", "bold red")
                return
            while True:  # agent 长任务可能超过单次 wait 上限，等到 terminal
                result = await self.bus.wait(snap.command_id)
                if not result["timed_out"]:
                    break
            if result["status"] == "failed":
                self._write("system", f"派发失败：{result['error']}", "bold red")
        # run_worker：agent 调用是长任务，不能阻塞 UI
        self.run_worker(runner())

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

    def _on_agent_event(self, name: str, ev: AgentEvent) -> None:
        if ev.kind == "committed" and name == "user":
            # 用户消息已持久确认：此时才显示文本和派发状态
            self._write("user", ev.text)
            targets = self.orch.parse_mentions(ev.text)
            if targets:
                for target in targets:
                    self._system(f"{target} 思考中…")
            else:
                self._system("host 处理中…")
        elif ev.kind == "text":
            self._buffer_stream_text(name, ev.text)
        elif ev.kind == "info":
            self._finish_stream_text(name)
            self._system(f"{name}: {ev.text}")
        elif ev.kind == "status":
            self._finish_stream_text(name)
            self._system(f"{name}: {ev.text}")
        elif ev.kind == "tool":
            self._finish_stream_text(name)
            command = ev.meta.get("command")
            suffix = f"\n  {command}" if command else ""
            self._system(f"{name} 使用工具：{ev.text}{suffix}")
        elif ev.kind == "permission":
            self._finish_stream_text(name)
            self._system(f"{name}: {ev.text}")
        elif ev.kind == "cancel_requested":
            self._finish_stream_text(name)
            self._cancel_pending_permissions()
            self._system(ev.text)
        elif ev.kind == "error":
            self._finish_stream_text(name)
            self._write(name, f"出错：{ev.text}", "bold red")
        elif ev.kind == "done":
            self._finish_stream_text(name)
            self._system(f"{name} 完成")


def main() -> None:
    workdir = sys.argv[1] if len(sys.argv) > 1 else "."
    ChatApp(workdir).run()


if __name__ == "__main__":
    main()

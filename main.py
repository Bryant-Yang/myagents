"""myagents —— 终端多 agent 聊天室。

用法：
    .venv/bin/python main.py            # 在当前目录启动
    .venv/bin/python main.py /path/to/project   # 指定 agent 的工作目录

聊天室里 @kimi / @opencode / @codex 把消息派发给对应 agent，支持一条消息
@多个（并发执行）。@host 叫主持人（由 codex 扮演）出来总结/仲裁；不带 @
的消息由 host 做 LLM 路由，自动决定派给谁（也可能它自己答）。

接入协议：ACP-first。kimi 走 ACP 长驻会话（权限请求会弹窗交给用户决策），
codex / opencode 走 JSONL 无头 fallback。启动信息里能看到每个 agent 的
传输协议。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, RichLog

from orchestrator import Orchestrator
from adapters.base import AgentEvent
from control import CommandBus, ControlServer
from storage.store import WorkdirMismatchError, normalize_workdir

# 每个发言者的显示颜色
_COLORS = {"user": "yellow", "kimi": "cyan", "opencode": "green",
           "codex": "orange1", "host": "magenta"}

# 权限弹窗的固定应答：用户取消 / 退出 TUI 兜底
_CANCELLED = {"outcome": "cancelled"}


class PermissionScreen(ModalScreen):
    """ACP 权限请求弹窗：显示工具标题和 agent 给的 options，
    用户选 allow / reject / cancel，结果作为 ACP outcome 回给 agent。"""

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, agent_name: str, params: dict) -> None:
        super().__init__()
        self._agent_name = agent_name
        tool = params.get("toolCall", {})
        self._title = tool.get("title") or "(未命名工具)"
        # optionId 不一定是合法 DOM id，用序号映射
        self._options = {f"perm-opt-{i}": opt
                         for i, opt in enumerate(params.get("options", []))}

    def compose(self) -> ComposeResult:
        with Vertical(id="perm-dialog"):
            yield Label(f"{self._agent_name} 请求权限：{self._title}")
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


class ChatApp(App):
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

    def compose(self) -> ComposeResult:
        yield RichLog(wrap=True)
        yield Input(placeholder="@kimi @opencode 点名派发；不带 @ 由 host 路由；Ctrl+C 退出")
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
        agents = " ".join(f"@{s.name}({s.transport.upper()})" for s in self.orch.specs)
        self._system(f"聊天室已就绪：{agents} @host；不带 @ 的消息由 host 路由；"
                     f"工作目录：{self.orch.workdir}")
        self.query_one(Input).focus()

    async def on_unmount(self) -> None:
        # 先放行等待中的权限请求（cancelled），再停 bus worker（取消进行
        # 中的 dispatch），最后统一回收 adapter——顺序反过来会死锁：
        # aclose 等的锁可能被等权限的 prompt 持有。bus 不拥有 orch。
        self._cancel_pending_permissions()
        if self.control_server is not None:
            await self.control_server.aclose()
        await self.bus.aclose()
        await self.orch.aclose()

    # ---- ACP 权限决策 ----

    async def _acp_permission(self, agent_name: str, params: dict) -> dict:
        """ACP adapter 的权限决策回调：弹窗等用户选，返回 ACP outcome。"""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._permission_futures.add(fut)
        self.push_screen(
            PermissionScreen(agent_name, params),
            lambda outcome: self._resolve_permission(fut, outcome),
        )
        try:
            return await fut
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

    # ---- 时间线输出 ----

    def _write(self, speaker: str, text: str, style: str = "") -> None:
        color = _COLORS.get(speaker, "white")
        line = Text.assemble((f"[{speaker}] ", f"bold {color}"), (text, style))
        self.query_one(RichLog).write(line)

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

    def _on_agent_event(self, name: str, ev: AgentEvent) -> None:
        if ev.kind == "committed" and name == "user":
            # 用户消息已持久确认：此时才显示文本和派发状态
            self._write("user", ev.text)
            targets = self.orch.parse_mentions(ev.text)
            if targets:
                for target in targets:
                    self._system(f"{target} 思考中…")
            else:
                self._system("host 路由中…")
        elif ev.kind == "text":
            self._write(name, ev.text)
        elif ev.kind == "info":
            self._system(f"{name}: {ev.text}")
        elif ev.kind == "error":
            self._write(name, f"出错：{ev.text}", "bold red")
        elif ev.kind == "done":
            self._system(f"{name} 完成")


def main() -> None:
    workdir = sys.argv[1] if len(sys.argv) > 1 else "."
    ChatApp(workdir).run()


if __name__ == "__main__":
    main()

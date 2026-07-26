"""ACP → AgentAdapter 桥接：让 ACP agent 无缝接入现有编排器。

与 JSONL adapter 的本质区别：**会话在 agent 侧保持**。
- JSONL adapter：每次 stream() 一个新进程、新会话，靠 transcript 转发上下文。
- ACP adapter：一个长驻进程、一个 session，每次 stream() 只是 session/prompt
  追加一轮。编排器据此（`stateful_session = True`）改发增量上下文——
  只发自上次派发以来的新消息，不再重复完整 transcript。

两条安全契约（Codex review 立的）：
1. **权限默认 deny**：未注入 TUI 权限决策器时，session/request_permission
   一律 cancelled，auto 放行必须显式 opt-in。TUI 通过
   `set_permission_handler` 注入异步决策回调，把选择权交给用户。
2. **取消必须等确认**：流被取消时，先发 session/cancel，然后**等待**
   原 prompt 以 cancelled 结束（有限超时）；超时说明连接不可信，关闭
   并标记必须重建。锁只在确认停止或连接关闭后才释放——
   下一轮 prompt 绝不与仍在执行的上一轮重叠。

会话所有权：一个 AcpAdapter 实例是它 session 的唯一 writer。
不要把这个 session id 交给别的进程（普通 kimi TUI、另一个 adapter）并发写。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator, Awaitable, Callable

from adapters.base import AgentEvent

from .client import AcpClient, PermissionHandler

_CANCEL_TIMEOUT = 10  # 等 agent 确认 cancelled 的有限超时（秒）

# 通用 agent-aware 权限决策器：async (agent_name, params) -> outcome。
# orchestrator 用它把 TUI 决策注入所有 ACP adapter（通用 runtime 里弹窗必须
# 知道权限来自哪个 agent）；名字在 adapter 内绑定，client 层仍只见
# params-only 的 PermissionHandler。
AgentPermissionHandler = Callable[[str, dict], Awaitable[dict]]


class AcpAdapter:
    """通用 ACP adapter：name + 启动命令即可定义一个 agent。"""

    # 编排器据此识别"会话在 agent 侧保持"：走增量上下文而非完整 transcript
    stateful_session = True

    def __init__(
        self,
        name: str,
        cmd: list[str],
        permission: str = "deny",  # 默认拒绝；auto 必须显式 opt-in
        cancel_timeout: float = _CANCEL_TIMEOUT,
        permission_handler: PermissionHandler | None = None,
    ) -> None:
        self.name = name
        self.session_id: str | None = None
        self._cmd = cmd
        self._permission = permission
        self._permission_handler = permission_handler
        self._cancel_timeout = cancel_timeout
        self._client = AcpClient(cmd, permission=permission,
                                 permission_handler=permission_handler)
        self._started = False
        self._lock = asyncio.Lock()  # session 唯一 writer：串行化本 adapter 的轮次

    def set_permission_handler(self, handler: AgentPermissionHandler | None) -> None:
        """TUI 挂载后注入权限决策器；之后的连接重建也会带上它。

        handler 签名：async (agent_name, params) -> outcome——通用 runtime
        里弹窗必须知道权限来自哪个 agent。client 层保持 params-only，
        名字在这里绑定。"""
        if handler is None:
            self._permission_handler = None
        else:
            self._permission_handler = lambda params: handler(self.name, params)
        self._client.set_permission_handler(self._permission_handler)

    async def _ensure_started(self, workdir: str) -> bool:
        """确保进程和 session 就绪；返回本轮是否新建了 session。"""
        if not self._started:
            try:
                await self._client.start()
                self.session_id = await self._client.session_new(workdir)
            except BaseException:
                # 原子初始化：start 成功但 session_new 失败时，进程和
                # reader/stderr tasks 必须回收，否则下次 stream 会对
                # 同一个 client 重复 start，覆盖活进程句柄留下孤儿
                await self._reset()
                raise
            self._started = True
            return True
        return False

    async def _reset(self) -> None:
        """关闭当前连接并标记必须重建（下轮 stream 重新 start + session/new）。"""
        with contextlib.suppress(Exception):
            await self._client.close()
        self._client = AcpClient(self._cmd, permission=self._permission,
                                 permission_handler=self._permission_handler)
        self._started = False
        self.session_id = None

    async def stream(self, prompt: str, workdir: str) -> AsyncIterator[AgentEvent]:
        async with self._lock:
            new_session = await self._ensure_started(workdir)
            if new_session:
                # session id 只展示一次（每次建立时），不刷屏
                yield AgentEvent("info", f"ACP session 已建立：{self.session_id}")
            updates: asyncio.Queue = asyncio.Queue()

            def on_notify(method: str, params: dict) -> None:
                if params.get("sessionId") == self.session_id:
                    updates.put_nowait((method, params))

            self._client.on_notification = on_notify
            task = asyncio.create_task(self._client.prompt(self.session_id, prompt))
            task.add_done_callback(lambda t: updates.put_nowait(("__done__", t)))
            try:
                while True:
                    method, payload = await updates.get()
                    if method == "__done__":
                        result = payload.result()  # 失败在此抛出，交给编排器兜底
                        yield AgentEvent("done", meta={
                            "stopReason": result.get("stopReason", "")})
                        break
                    if method != "session/update":
                        continue
                    update = payload.get("update", {})
                    kind = update.get("sessionUpdate")
                    if kind == "agent_message_chunk":
                        text = update.get("content", {}).get("text", "")
                        if text:
                            yield AgentEvent("text", text)
                    elif kind == "tool_call":
                        yield AgentEvent("info", f"tool: {update.get('title', '')}")
                    # thought/plan/commands 等事件 MVP 阶段不展示
            finally:
                if not task.done():
                    # 取消契约：先通知中断，再等确认；锁在整个 finally
                    # 期间一直持有，下一轮不会提前开始。
                    with contextlib.suppress(Exception):
                        await self._client.cancel(self.session_id)
                    try:
                        await asyncio.wait_for(task, timeout=self._cancel_timeout)
                    except Exception:
                        # 超时或出错：连接状态不可信，关闭并标记重建
                        await self._reset()

    async def aclose(self) -> None:
        # 与 stream 同一把锁：close 不会与进行中的 session/new/prompt
        # 竞态（否则可能在 stream 眼皮底下杀掉进程）
        async with self._lock:
            await self._client.close()
            self._started = False
            self.session_id = None


class AcpKimiAdapter(AcpAdapter):
    def __init__(self, permission: str = "deny") -> None:
        super().__init__("kimi", ["kimi", "acp"], permission=permission)

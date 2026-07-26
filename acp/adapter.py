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

session restore 原语：`stream_prepared(make_prompt, workdir, resume_session_id)`
在 writer lock 的同一生命周期内完成 prepare（start → session/load 或
session/new）和 prompt 流，并返回不可歧义的 SessionPreparation；上层据此
安全地续接旧 session，而不会碰到 prepare 与 prompt 之间连接被重建的窗口。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

from adapters.base import AgentEvent

from .client import AcpClient, AcpError, PermissionHandler

_CANCEL_TIMEOUT = 10  # 等 agent 确认 cancelled 的有限超时（秒）

# 通用 agent-aware 权限决策器：async (agent_name, params) -> outcome。
# orchestrator 用它把 TUI 决策注入所有 ACP adapter（通用 runtime 里弹窗必须
# 知道权限来自哪个 agent）；名字在 adapter 内绑定，client 层仍只见
# params-only 的 PermissionHandler。
AgentPermissionHandler = Callable[[str, dict], Awaitable[dict]]


@dataclass(frozen=True)
class SessionPreparation:
    """一轮 prompt 前 session 准备的不可歧义结果。

    - session_id：本轮实际使用的 session（load 成功时是 resume id，
      回退/新建时是 session/new 返回的 id，复用活跃 session 时是现有 id）。
    - restored：True 表示本轮命中调用方请求的 resume session
      （load 成功，或活跃 session 正好就是它）；False 表示是新 session。
    - load_failed：尝试过 session/load 但被 agent 拒绝（已回退 new）。
      capability 不支持时不算失败（根本没尝试）。
    - fresh：本轮是否建立了连接级 session（new 或 load）。复用活跃
      session 时为 False——据此保证 session info 只 emit 一次。
    """
    session_id: str
    restored: bool
    load_failed: bool
    fresh: bool


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

    async def _prepare_locked(self, workdir: str,
                              resume_session_id: str | None = None
                              ) -> SessionPreparation:
        """在 writer lock 持有期间准备 session；返回不可歧义的结果。

        - 已有活跃 session：resume id 为 None 或正好等于活跃 id 时复用；
          不同 id 直接报错——绝不把活跃 session 偷换成另一个。
        - 未启动：start 后有 resume id 且 agent 声明 loadSession 才尝试
          session/load；只有 load 本身的 AcpError 被吞并回退 session/new。
          start（initialize）、session/new 的错误照常传播并原子回收。
        """
        if self._started:
            assert self.session_id is not None
            if (resume_session_id is not None
                    and resume_session_id != self.session_id):
                raise AcpError(
                    f"resume_session_id={resume_session_id} 与活跃 session "
                    f"{self.session_id} 不一致：拒绝偷换，请先 aclose()")
            return SessionPreparation(
                session_id=self.session_id,
                restored=resume_session_id is not None,
                load_failed=False, fresh=False)
        try:
            await self._client.start()
            restored = False
            load_failed = False
            if (resume_session_id
                    and self._client.capabilities.get("loadSession")):
                try:
                    await self._client.session_load(resume_session_id, workdir)
                    self.session_id = resume_session_id
                    restored = True
                except AcpError:
                    # 只吞 load 本身的失败：回退 new，结果里如实标记
                    load_failed = True
            if not restored:
                self.session_id = await self._client.session_new(workdir)
        except BaseException:
            # 原子初始化：start 成功但 session/new 失败时，进程和
            # reader/stderr tasks 必须回收，否则下次 stream 会对
            # 同一个 client 重复 start，覆盖活进程句柄留下孤儿
            await self._reset()
            raise
        self._started = True
        return SessionPreparation(
            session_id=self.session_id, restored=restored,
            load_failed=load_failed, fresh=True)

    async def _reset(self) -> None:
        """关闭当前连接并标记必须重建（下轮 stream 重新 start + session/new）。"""
        with contextlib.suppress(Exception):
            await self._client.close()
        self._client = AcpClient(self._cmd, permission=self._permission,
                                 permission_handler=self._permission_handler)
        self._started = False
        self.session_id = None

    def stream(self, prompt: str, workdir: str) -> AsyncIterator[AgentEvent]:
        # 直接返回底层 async generator（不再包一层）：调用方 aclose() 时
        # GeneratorExit 一次性打进持有锁的 stream_prepared，取消契约
        # 同步走完；多层 async for 包装会把关闭推迟到 GC finalizer。
        return self.stream_prepared(lambda _prep: prompt, workdir)

    async def stream_prepared(
        self,
        make_prompt: Callable[[SessionPreparation], str],
        workdir: str,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """restore 原语：prepare（start/load/new）与 prompt 在同一锁生命周期。

        writer lock 从 prepare 一直持到 prompt 流结束（含取消契约），
        aclose()/下一轮 stream 无法插在 prepare 与 prompt 之间——调用方
        在 make_prompt 里拿到的 SessionPreparation 就是本轮实际生效的
        session，不存在"prepare 后连接被重建、session 换了但上层仍沿用
        旧 cursor"的窗口。make_prompt 是惰性回调：在判定（load 成功/
        回退 new/复用）之后、锁内执行，可以安全地用 prep.session_id
        构造增量上下文。

        原子性契约：make_prompt 在 prepare 之后、任何 info/prompt 之前
        同步执行——上层可以把它当作 checkpoint hook（持久化 session/cursor
        后再让本轮生效）。factory 抛异常时不 emit info、不发 prompt；
        若本轮新建/恢复了 session（prep.fresh），连接被 reset 回收，
        不留下未提交的活跃 session，下轮可用同一 resume id 重试。
        复用旧活跃 session（非 fresh）时的 factory 异常不销毁现有 session。
        """
        async with self._lock:
            prep = await self._prepare_locked(workdir, resume_session_id)
            try:
                prompt = make_prompt(prep)
            except BaseException:
                if prep.fresh:
                    # 上层 checkpoint 未提交：这个 fresh session 没人认领，
                    # 必须回收，否则下轮拿旧 resume id 会撞"拒绝偷换"
                    await self._reset()
                raise
            if prep.fresh:
                # session id 只展示一次（每次建立/恢复时），不刷屏；
                # close/重建后下轮 fresh 又为 True，会再次如实报告
                verb = "已恢复" if prep.restored else "已建立"
                yield AgentEvent("info", f"ACP session {verb}：{prep.session_id}")
            inner = self._prompt_locked(prep.session_id, prompt)
            try:
                async for ev in inner:
                    yield ev
            finally:
                # async for 委托不会级联关闭：外层被 aclose 时必须显式关
                # 内层，取消契约（cancel → 等确认 → 必要时重建）才会在
                # 锁内同步执行，而不是拖到 GC 的 asyncgen finalizer。
                await inner.aclose()

    async def _prompt_locked(self, session_id: str,
                             prompt: str) -> AsyncIterator[AgentEvent]:
        """锁内执行一轮 prompt 并映射事件；取消契约见 stream_prepared。"""
        updates: asyncio.Queue = asyncio.Queue()

        def on_notify(method: str, params: dict) -> None:
            if params.get("sessionId") == session_id:
                updates.put_nowait((method, params))

        self._client.on_notification = on_notify
        task = asyncio.create_task(self._client.prompt(session_id, prompt))
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
                    await self._client.cancel(session_id)
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

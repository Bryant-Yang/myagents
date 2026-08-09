"""ACP → AgentAdapter 桥接：让 ACP agent 无缝接入现有编排器。

与 JSONL adapter 的本质区别：**会话在 agent 侧保持**。
- JSONL adapter：每次 stream() 一个新进程、新会话，靠 transcript 转发上下文。
- ACP adapter：一个长驻进程、一个 session，每次 stream() 只是 session/prompt
  追加一轮。编排器据此（`stateful_session = True`）改发增量上下文——
  只发自上次派发以来的新消息，不再重复完整 transcript。

三条安全契约（Codex review 立的）：
1. **权限默认 deny**：未注入 TUI 权限决策器时，session/request_permission
   一律 cancelled，auto 放行必须显式 opt-in。TUI 通过
   `set_permission_handler` 注入异步决策回调，把选择权交给用户。
2. **取消必须等确认**：流被取消时，先发 session/cancel，然后**等待**
   原 prompt 以 cancelled 结束（有限超时）；超时说明连接不可信，关闭
   并标记必须重建。锁只在确认停止或连接关闭后才释放——
   下一轮 prompt 绝不与仍在执行的上一轮重叠。
3. **无活动必须回收**：prompt 连续一段时间没有任何 ACP 通知或终止响应，
   且当前不在等待人类权限时，视为上游卡死，自动取消本轮；取消也不确认
   时重建连接，避免永久堵住串行 CommandBus。prompt 已提交后的超时属于
   结果不确定，必须形成 no-replay 边界。

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
import json
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Mapping, Sequence

from adapters.base import (
    AgentAdapter,
    AgentDeliveryUncertainError,
    AgentEvent,
    ExecutionMode,
    ReadOnlyFallbackError,
    redact_sensitive_text,
)
from clipboard_image import prompt_images

from .client import (
    AcpClient,
    AcpError,
    AcpRemoteError,
    AcpRequestNotSentError,
    PermissionHandler,
)

_CANCEL_TIMEOUT = 10  # 等 agent 确认 cancelled 的有限超时（秒）
_INACTIVITY_TIMEOUT = 120  # prompt 连续无任何 ACP 事件的上限（秒）
_TOOL_INACTIVITY_TIMEOUT = 900  # 活跃工具允许更长的无输出窗口（秒）
_FALLBACK_SESSION_PREFIX = "fallback:jsonl:"
_TERMINAL_TOOL_STATUSES = {
    "completed", "succeeded", "success", "failed", "error",
    "declined", "cancelled", "canceled",
}

# OpenCode 默认允许大部分工具。ACP 生产路径必须把未知工具、写入、命令、
# 网络、Skill、子 agent 与外部目录统一变成 ask，才能由 myagents TUI
# fail-closed 决策；读取/搜索和本地只读索引保持无弹窗。
OPENCODE_ACP_PERMISSION_POLICY = {
    "*": "ask",
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "lsp": "allow",
    "todowrite": "allow",
    "edit": "ask",
    "bash": "ask",
    "task": "ask",
    "skill": "ask",
    "webfetch": "ask",
    "websearch": "ask",
    "external_directory": "ask",
}

# read_only 阶段不能把 ask 简单回成 cancelled：OpenCode 1.18.14–1.18.15
# 在工具权限被取消后会直接 end_turn 且不产生正文。把有副作用工具在 runtime
# 层硬拒绝，模型会收到失败的工具结果并继续用只读工具完成报告；未知工具仍
# fail-closed。
OPENCODE_ACP_READ_ONLY_PERMISSION_POLICY = {
    "*": "deny",
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "lsp": "allow",
    "todowrite": "allow",
}

# Qwen 会从用户配置继承 approval mode；若 native TUI 正处于 auto/yolo，
# ACP runtime 可能直接执行工具而不发 requestPermission。生产普通轮强制 default，
# 让写入/命令回到 ACP 权限仲裁；workflow 只读轮强制 plan，在 runtime 层阻断
# 文件修改和有副作用命令，不能只依赖 client cancelled。
QWEN_ACP_DEFAULT_CMD = (
    "qwen", "--acp", "--approval-mode", "default",
)
QWEN_ACP_READ_ONLY_CMD = (
    "qwen", "--acp", "--approval-mode", "plan",
)

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
        cmd: Sequence[str],
        permission: str = "deny",  # 默认拒绝；auto 必须显式 opt-in
        cancel_timeout: float = _CANCEL_TIMEOUT,
        inactivity_timeout: float = _INACTIVITY_TIMEOUT,
        tool_inactivity_timeout: float = _TOOL_INACTIVITY_TIMEOUT,
        permission_handler: PermissionHandler | None = None,
        fallback_adapter: AgentAdapter | None = None,
        env_overrides: Mapping[str, str] | None = None,
        execution_env_overrides: Mapping[
            ExecutionMode, Mapping[str, str]
        ] | None = None,
        execution_cmd_overrides: Mapping[
            ExecutionMode, Sequence[str]
        ] | None = None,
    ) -> None:
        if cancel_timeout <= 0:
            raise ValueError("cancel_timeout 必须大于 0")
        if inactivity_timeout <= 0:
            raise ValueError("inactivity_timeout 必须大于 0")
        if tool_inactivity_timeout <= 0:
            raise ValueError("tool_inactivity_timeout 必须大于 0")
        self.name = name
        self.session_id: str | None = None
        self._cmd = list(cmd)
        self._permission = permission
        self._permission_handler = permission_handler
        self._fallback = fallback_adapter
        self._env_overrides = dict(env_overrides or {})
        self._execution_env_overrides = {
            mode: dict(overrides)
            for mode, overrides in (execution_env_overrides or {}).items()
        }
        self._execution_cmd_overrides = {
            mode: list(command)
            for mode, command in (execution_cmd_overrides or {}).items()
        }
        self._active_env_overrides = self._env_for_mode(
            ExecutionMode.DEFAULT)
        self._active_cmd = self._cmd_for_mode(ExecutionMode.DEFAULT)
        self._attachment_root: Path | None = None
        self._cancel_timeout = cancel_timeout
        self._inactivity_timeout = inactivity_timeout
        self._tool_inactivity_timeout = tool_inactivity_timeout
        self._client = AcpClient(self._active_cmd, permission=permission,
                                 permission_handler=permission_handler,
                                 env_overrides=self._active_env_overrides)
        self._started = False
        # 当前活跃 session 已经运行过的最近 execution mode。read_only 只能
        # 复用同为 read_only 的 session；普通轮次可能持有 allow_always，
        # 必须连进程一起隔离，不能只拒绝新的 permission request。
        self._session_execution_mode: ExecutionMode | None = None
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

    def set_attachment_root(self, root: Path | None) -> None:
        """限制可作为 ACP image block 发送的本地附件目录。"""
        self._attachment_root = None if root is None else Path(root).absolute()

    def _env_for_mode(self, execution_mode: ExecutionMode) -> dict[str, str]:
        """合并进程级基础环境与 execution-mode 专用收口。"""
        return {
            **self._env_overrides,
            **self._execution_env_overrides.get(execution_mode, {}),
        }

    def _cmd_for_mode(self, execution_mode: ExecutionMode) -> list[str]:
        """选择 execution mode 对应的进程级命令。"""
        return list(self._execution_cmd_overrides.get(
            execution_mode, self._cmd))

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
        if (resume_session_id is not None
                and resume_session_id.startswith(_FALLBACK_SESSION_PREFIX)):
            # JSONL 降级轮只是持久化 checkpoint 占位，不是可被 ACP
            # session/load 的真实 session。下一轮恢复为新 ACP session，
            # 让 Orchestrator 按 fresh-session 契约有界 bootstrap。
            resume_session_id = None
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

    async def _reset(
        self,
        env_overrides: Mapping[str, str] | None = None,
        cmd: Sequence[str] | None = None,
    ) -> None:
        """关闭当前连接并标记必须重建（下轮 stream 重新 start + session/new）。"""
        with contextlib.suppress(Exception):
            await self._client.close()
        if env_overrides is not None:
            self._active_env_overrides = dict(env_overrides)
        if cmd is not None:
            self._active_cmd = list(cmd)
        self._client = AcpClient(self._active_cmd, permission=self._permission,
                                 permission_handler=self._permission_handler,
                                 env_overrides=self._active_env_overrides)
        self._started = False
        self.session_id = None
        self._session_execution_mode = None

    def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        # 直接返回底层 async generator（不再包一层）：调用方 aclose() 时
        # GeneratorExit 一次性打进持有锁的 stream_prepared，取消契约
        # 同步走完；多层 async for 包装会把关闭推迟到 GC finalizer。
        return self.stream_prepared(
            lambda _prep: prompt,
            workdir,
            execution_mode=execution_mode,
        )

    async def stream_prepared(
        self,
        make_prompt: Callable[[SessionPreparation], str],
        workdir: str,
        resume_session_id: str | None = None,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
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
            target_env = self._env_for_mode(execution_mode)
            target_cmd = self._cmd_for_mode(execution_mode)
            if (target_env != self._active_env_overrides
                    or target_cmd != self._active_cmd):
                # 环境与 CLI profile 都属于 ACP 子进程，不可能在活进程内
                # 安全切换。
                # profile 变化时重建进程并禁止 load 旧 session，避免权限缓存
                # 或上一 mode 的 runtime policy 穿透本轮。
                await self._reset(target_env, target_cmd)
                resume_session_id = None
            elif (execution_mode is ExecutionMode.READ_ONLY
                    and self._session_execution_mode
                    is not ExecutionMode.READ_ONLY):
                # 非只读 session 可能在先前轮次获得 allow_always/session 级
                # 写入授权。fresh session 还不够保守：runtime 也可能在进程
                # 内缓存授权，所以关闭整个 ACP client，并禁止 load 持久化
                # resume id。新连接的 prepare 失败仍可走既有只读 fallback。
                if self._started:
                    await self._reset()
                resume_session_id = None
            was_started = self._started
            try:
                prep = await self._prepare_locked(workdir, resume_session_id)
            except Exception:
                # 只有新连接的 start/initialize/session prepare 失败才能
                # 降级。活跃 session 冲突等内部错误不能被伪装成
                # JSONL 成功；prompt 已发送后的异常也不经过此分支。
                if was_started or self._fallback is None:
                    raise
                async for event in self._stream_fallback_locked(
                        make_prompt, workdir, resume_session_id,
                        execution_mode):
                    yield event
                return
            try:
                prompt = make_prompt(prep)
            except BaseException:
                if prep.fresh:
                    # 上层 checkpoint 未提交：这个 fresh session 没人认领，
                    # 必须回收，否则下轮拿旧 resume id 会撞"拒绝偷换"
                    await self._reset()
                raise
            self._session_execution_mode = execution_mode
            if prep.fresh:
                # session id 只展示一次（每次建立/恢复时），不刷屏；
                # close/重建后下轮 fresh 又为 True，会再次如实报告
                verb = "已恢复" if prep.restored else "已建立"
                yield AgentEvent("info", f"ACP session {verb}：{prep.session_id}")
            inner = self._prompt_locked(
                prep.session_id, prompt, execution_mode)
            try:
                async for ev in inner:
                    yield ev
            finally:
                # async for 委托不会级联关闭：外层被 aclose 时必须显式关
                # 内层，取消契约（cancel → 等确认 → 必要时重建）才会在
                # 锁内同步执行，而不是拖到 GC 的 asyncgen finalizer。
                await inner.aclose()

    async def _stream_fallback_locked(
        self,
        make_prompt: Callable[[SessionPreparation], str],
        workdir: str,
        resume_session_id: str | None,
        execution_mode: ExecutionMode,
    ) -> AsyncIterator[AgentEvent]:
        """ACP prepare 失败后的唯一安全 JSONL 降级点。

        该方法只由 ``stream_prepared`` 的 prepare 异常分支调用，
        仍持有同一 writer lock。先调 make_prompt 完成伪 session
        checkpoint，再启动一次性 adapter；checkpoint 失败直接穿透，
        不得用降级路径掩盖持久化错误。
        """
        assert self._fallback is not None
        if execution_mode is ExecutionMode.WORKSPACE_WRITE:
            raise ReadOnlyFallbackError(
                f"{self.name} ACP prepare 失败；只读 JSONL fallback "
                "不能承担 workspace_write 阶段")
        prep = SessionPreparation(
            session_id=f"{_FALLBACK_SESSION_PREFIX}{self.name}",
            restored=False,
            load_failed=resume_session_id is not None,
            fresh=True,
        )
        prompt = make_prompt(prep)
        yield AgentEvent(
            "info",
            f"{self.name} ACP 启动失败，使用只读 JSONL fallback",
            meta={
                "primary_transport": "acp",
                "fallback_transport": "jsonl",
                "fallback_scope": "prepare-only",
            },
        )
        if execution_mode is ExecutionMode.DEFAULT:
            inner = self._fallback.stream(prompt, workdir)
        else:
            inner = self._fallback.stream(
                prompt, workdir, execution_mode=execution_mode)
        try:
            async for event in inner:
                yield event
        finally:
            closer = getattr(inner, "aclose", None)
            if closer is not None:
                with contextlib.suppress(RuntimeError):
                    await closer()

    async def _prompt_locked(
        self,
        session_id: str,
        prompt: str,
        execution_mode: ExecutionMode,
    ) -> AsyncIterator[AgentEvent]:
        """锁内执行一轮 prompt 并映射事件；取消契约见 stream_prepared。"""
        updates: asyncio.Queue = asyncio.Queue()
        seen_status: set[str] = set()
        pending_permissions: set[int] = set()
        tool_contexts: dict[str, dict[str, str]] = {}
        tool_fingerprints: dict[str, tuple[str, str, str, str]] = {}
        active_tools: set[str] = set()
        last_tool_key: str | None = None
        delivery_committed = False

        def on_notify(method: str, params: dict) -> None:
            if params.get("sessionId") == session_id:
                updates.put_nowait((method, params))

        def on_permission_activity(
                phase: str, request_id: int, params: dict) -> None:
            if params.get("sessionId") == session_id:
                updates.put_nowait((
                    "__permission_activity__",
                    {"phase": phase, "request_id": request_id},
                ))

        self._client.on_notification = on_notify
        self._client.on_permission_activity = on_permission_activity
        if execution_mode is ExecutionMode.READ_ONLY:
            self._client.set_permission_handler(
                lambda _params: {"outcome": "cancelled"})
        else:
            self._client.set_permission_handler(self._permission_handler)
        # Agents that omit or disable image capability must still get a
        # defined images tuple; never leave it unbound across the prompt call.
        images: tuple = ()
        if self._client.capabilities.get(
                "promptCapabilities", {}).get("image") is True:
            images = prompt_images(prompt, self._attachment_root)
        task = asyncio.create_task(
            self._client.prompt(session_id, prompt, images))
        task.add_done_callback(lambda t: updates.put_nowait(("__done__", t)))
        try:
            while True:
                try:
                    if pending_permissions:
                        # 人类审批时长不属于 agent 静默。取消/关闭仍会直接
                        # cancel 本协程，不会因无 timeout 而失去退出能力。
                        method, payload = await updates.get()
                    else:
                        timeout = (
                            self._tool_inactivity_timeout
                            if active_tools else self._inactivity_timeout
                        )
                        method, payload = await asyncio.wait_for(
                            updates.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    timeout = (
                        self._tool_inactivity_timeout
                        if active_tools else self._inactivity_timeout
                    )
                    scope = "活跃工具" if active_tools else "会话"
                    raise AgentDeliveryUncertainError(
                        f"ACP {scope}连续 {timeout:g} 秒无活动，"
                        "已取消本轮请求") from None
                if method == "__permission_activity__":
                    if not delivery_committed:
                        delivery_committed = True
                        yield AgentEvent("delivery_committed", meta={
                            "sessionId": session_id,
                            "transport": "acp",
                        })
                    request_id = payload["request_id"]
                    if payload["phase"] == "requested":
                        pending_permissions.add(request_id)
                    else:
                        pending_permissions.discard(request_id)
                    continue
                if method == "__done__":
                    try:
                        result = payload.result()
                    except AcpRequestNotSentError:
                        # drain 前失败：服务端不可能看到该 prompt。
                        raise
                    except AcpRemoteError as exc:
                        if not delivery_committed:
                            # 还没有任何 session 活动且服务端显式回错：
                            # 按未接受处理，下轮可以继续增量补发。
                            raise
                        raise AgentDeliveryUncertainError(
                            f"ACP prompt 已产生会话活动后失败：{exc}"
                        ) from exc
                    except Exception as exc:
                        # request 已经完成 stdio drain，但没有得到可信的
                        # 显式拒绝；断线/协议错误可能发生在 agent 已
                        # 开始执行之后，必须走 no-replay。
                        raise AgentDeliveryUncertainError(
                            f"ACP prompt 已发送但结果不确定：{exc}"
                        ) from exc
                    if not delivery_committed:
                        delivery_committed = True
                        yield AgentEvent("delivery_committed", meta={
                            "sessionId": session_id,
                            "transport": "acp",
                        })
                    yield AgentEvent("done", meta={
                        "stopReason": result.get("stopReason", "")})
                    break
                if method != "session/update":
                    continue
                if not delivery_committed:
                    # 在任何正文/工具/状态事件进入外部 sink 前，
                    # 让 Orchestrator 先持久化 no-replay cursor。
                    delivery_committed = True
                    yield AgentEvent("delivery_committed", meta={
                        "sessionId": session_id,
                        "transport": "acp",
                    })
                update = payload.get("update", {})
                kind = update.get("sessionUpdate")
                if kind == "agent_message_chunk":
                    text = update.get("content", {}).get("text", "")
                    if text:
                        yield AgentEvent("text", text)
                elif kind == "agent_thought_chunk":
                    # 不展示 chain-of-thought 正文，只公开安全的阶段状态。
                    status = "agent 正在分析…"
                    if status not in seen_status:
                        seen_status.add(status)
                        yield AgentEvent("status", status)
                elif kind == "tool_call":
                    title = redact_sensitive_text(
                        str(update.get("title") or "(未命名工具)"),
                        limit=500,
                    )
                    raw_input = update.get("rawInput")
                    command = ""
                    if isinstance(raw_input, dict):
                        value = raw_input.get("command")
                        if isinstance(value, str):
                            command = redact_sensitive_text(value)
                    tool_call_id = update.get("toolCallId")
                    tool_key = str(tool_call_id or title)
                    last_tool_key = tool_key
                    tool_kind = str(update.get("kind") or "")
                    status_value = str(update.get("status") or "")
                    if status_value.lower() in _TERMINAL_TOOL_STATUSES:
                        active_tools.discard(tool_key)
                    else:
                        active_tools.add(tool_key)
                    tool_contexts[tool_key] = {
                        "title": title,
                        "command": command,
                        "kind": tool_kind,
                    }
                    tool_fingerprints[tool_key] = (
                        title, status_value, command, tool_kind)
                    yield AgentEvent("tool", title, meta={
                        "tool_call_id": tool_call_id,
                        "tool_kind": tool_kind or None,
                        "status": update.get("status"),
                        "command": command,
                    })
                elif kind == "tool_call_update":
                    tool_call_id = update.get("toolCallId")
                    fallback_key = str(
                        tool_call_id or update.get("title")
                        or last_tool_key or "工具调用")
                    previous = tool_contexts.get(fallback_key, {})
                    title = redact_sensitive_text(
                        str(update.get("title")
                            or previous.get("title")
                            or "工具调用"),
                        limit=500,
                    )
                    command = previous.get("command", "")
                    raw_input = update.get("rawInput")
                    if isinstance(raw_input, dict):
                        value = raw_input.get("command")
                        if isinstance(value, str):
                            command = redact_sensitive_text(value)
                    tool_kind = str(
                        update.get("kind") or previous.get("kind") or "")
                    status_value = redact_sensitive_text(
                        str(update.get("status") or "updated"),
                        limit=200,
                    )
                    if status_value.lower() in _TERMINAL_TOOL_STATUSES:
                        active_tools.discard(fallback_key)
                    else:
                        active_tools.add(fallback_key)
                    fingerprint = (
                        title, status_value, command, tool_kind)
                    # Kimi 可能在一次工具调用中高频发送完全相同的
                    # in_progress update。它们证明连接活着，但对消费者没有
                    # 新信息；只保留可见状态迁移。
                    if tool_fingerprints.get(fallback_key) == fingerprint:
                        yield AgentEvent("activity", meta={
                            "tool_call_id": tool_call_id,
                            "phase": "tool",
                        })
                        continue
                    tool_fingerprints[fallback_key] = fingerprint
                    tool_contexts[fallback_key] = {
                        "title": title,
                        "command": command,
                        "kind": tool_kind,
                    }
                    yield AgentEvent("tool", title, meta={
                        "tool_call_id": tool_call_id,
                        "tool_kind": tool_kind or None,
                        "status": update.get("status") or "updated",
                        "command": command,
                        "update": True,
                    })
                elif kind in {
                        "plan", "available_commands_update",
                        "current_mode_update", "config_option_update"}:
                    status = "agent 已更新执行计划"
                    if status not in seen_status:
                        seen_status.add(status)
                        yield AgentEvent("status", status)
        finally:
            self._client.set_permission_handler(self._permission_handler)
            if self._client.on_permission_activity is on_permission_activity:
                self._client.on_permission_activity = None
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
            self._session_execution_mode = None


class AcpKimiAdapter(AcpAdapter):
    def __init__(
        self,
        permission: str = "deny",
        *,
        fallback_jsonl: bool = True,
        fallback_adapter: AgentAdapter | None = None,
    ) -> None:
        if fallback_jsonl and fallback_adapter is None:
            from adapters.kimi_adapter import KimiAdapter

            fallback_adapter = KimiAdapter.readonly_fallback()
        super().__init__(
            "kimi",
            ["kimi", "acp"],
            permission=permission,
            fallback_adapter=(fallback_adapter if fallback_jsonl else None),
        )


class AcpOpenCodeAdapter(AcpAdapter):
    def __init__(
        self,
        permission: str = "deny",
        *,
        fallback_jsonl: bool = True,
        fallback_adapter: AgentAdapter | None = None,
        cmd: list[str] | None = None,
    ) -> None:
        if fallback_jsonl and fallback_adapter is None:
            from adapters.opencode_adapter import OpenCodeAdapter

            fallback_adapter = OpenCodeAdapter.readonly_fallback()
        super().__init__(
            "opencode",
            cmd or ["opencode", "acp"],
            permission=permission,
            fallback_adapter=(fallback_adapter if fallback_jsonl else None),
            env_overrides={
                "OPENCODE_PERMISSION": json.dumps(
                    OPENCODE_ACP_PERMISSION_POLICY,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
            execution_env_overrides={
                ExecutionMode.READ_ONLY: {
                    "OPENCODE_PERMISSION": json.dumps(
                        OPENCODE_ACP_READ_ONLY_PERMISSION_POLICY,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
            },
        )


class AcpQwenAdapter(AcpAdapter):
    """Qwen Code 的 ACP-only 生产 adapter。"""

    def __init__(self, permission: str = "deny") -> None:
        super().__init__(
            "qwen",
            QWEN_ACP_DEFAULT_CMD,
            permission=permission,
            execution_cmd_overrides={
                ExecutionMode.READ_ONLY: QWEN_ACP_READ_ONLY_CMD,
            },
        )

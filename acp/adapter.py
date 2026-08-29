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
import inspect
import json
import os
import signal
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from adapters.base import (
    DEFAULT_AGENT_INACTIVITY_TIMEOUT,
    AgentAdapter,
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    AgentEvent,
    AgentHostCapability,
    ExecutionMode,
    ReadOnlyFallbackError,
    redact_sensitive_text,
)
from agent_readiness import (
    AgentReadiness,
    ReadinessState,
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
_SESSION_PREPARE_TIMEOUT = 30  # initialize 后 new/load 不得无界挂起
_SESSION_CLOSE_TIMEOUT = 2  # 有 close capability 时的有界 session 收尾（秒）
_TOOL_INACTIVITY_TIMEOUT = 900  # 活跃工具允许更长的无输出窗口（秒）
_AUTH_TIMEOUT = 300  # 浏览器登录的有限等待上限（秒）
_AUTH_NOTIFICATION_QUEUE_LIMIT = 64  # 认证期通知有界；溢出即协议失败
_AUTH_NOTIFICATION_QUEUE_BYTE_LIMIT = 1024 * 1024
_PROMPT_UPDATE_QUEUE_LIMIT = 256  # prompt 通知有界；溢出关闭连接
_PROMPT_UPDATE_QUEUE_BYTE_LIMIT = 64 * 1024 * 1024
_ACP_INBOUND_FRAME_BYTE_LIMIT = 32 * 1024 * 1024
_PROMPT_TOOL_TRACKING_LIMIT = 64  # 同轮并发未终止工具的硬上限
_TOOL_CALL_ID_BYTE_LIMIT = 256
_TOOL_TITLE_BYTE_LIMIT = 500
_TOOL_KIND_BYTE_LIMIT = 100
_TOOL_STATUS_BYTE_LIMIT = 200
_TOOL_COMMAND_BYTE_LIMIT = 2000
_BROWSER_OPEN_TIMEOUT = 10  # 系统 URL launcher 自身的有限等待上限（秒）
_FALLBACK_SESSION_PREFIX = "fallback:jsonl:"
_TERMINAL_TOOL_STATUSES = {
    "completed", "succeeded", "success", "failed", "error",
    "declined", "cancelled", "canceled",
}


def _bounded_tool_text(
    value: object,
    *,
    byte_limit: int,
    default: str = "",
) -> str:
    """Sanitize one protocol tool field before tracking or publication."""
    if value is None:
        return default
    if not isinstance(value, (str, int, float, bool)):
        return default
    # Limit before regex redaction so a single field cannot manufacture an
    # additional frame-sized temporary string while being normalized.
    prefix = str(value)[:byte_limit]
    redacted = redact_sensitive_text(prefix, limit=byte_limit)
    bounded = redacted.encode("utf-8")[:byte_limit].decode(
        "utf-8", errors="ignore")
    return bounded or default

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
_CODEBUDDY_CLI_ENV = "MYAGENTS_CODEBUDDY_CLI"
CODEBUDDY_ACP_DEFAULT_ARGS = (
    "--acp", "--acp-transport", "stdio",
    "--permission-mode", "default",
    "--subagent-permission-mode", "dontAsk",
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--setting-sources", "",
)
CODEBUDDY_ACP_READ_ONLY_ARGS = (
    "--acp", "--acp-transport", "stdio",
    "--permission-mode", "dontAsk",
    "--subagent-permission-mode", "dontAsk",
    "--tools", "Read,Glob,Grep",
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--setting-sources", "",
)


def _validated_codebuddy_cli(candidate: str, source: str) -> str:
    """解析并校验独立 CLI；拒绝任何 App bundle 内私有可执行文件。"""
    path = Path(candidate).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AcpError(f"{source} 指定的 CodeBuddy CLI 不存在：{path}") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise AcpError(f"{source} 指定的 CodeBuddy CLI 不可执行：{resolved}")
    parts = resolved.parts
    if any(
        part.lower().endswith(".app")
        and index + 1 < len(parts)
        and parts[index + 1].lower() == "contents"
        for index, part in enumerate(parts)
    ):
        raise AcpError(
            f"{source} 指向 App 包内私有 CLI，已拒绝：{resolved}；"
            "请安装官方独立 CodeBuddy CLI"
        )
    return str(resolved)


def _find_codebuddy_cli() -> str | None:
    """被动查找独立 CLI；不启动进程，也不借用 App 包内私有文件。"""
    explicit = os.environ.get(_CODEBUDDY_CLI_ENV, "").strip()
    if explicit:
        return _validated_codebuddy_cli(explicit, _CODEBUDDY_CLI_ENV)
    for name in ("codebuddy", "cbc"):
        resolved = shutil.which(name)
        if resolved:
            return _validated_codebuddy_cli(resolved, f"PATH 中的 {name}")
    return None


def codebuddy_readiness_probe() -> AgentReadiness:
    """供 AgentSpec 使用的 CodeBuddy 被动就绪探测。"""
    setup_hint = (
        "安装官方独立 CodeBuddy CLI，或用 MYAGENTS_CODEBUDDY_CLI "
        "指向该可执行文件"
    )
    try:
        executable = _find_codebuddy_cli()
    except AcpError as exc:
        return AgentReadiness(
            "codebuddy",
            ReadinessState.INVALID,
            str(exc),
            setup_hint,
        )
    if executable is None:
        return AgentReadiness(
            "codebuddy",
            ReadinessState.NOT_FOUND,
            "当前进程 PATH 未检测到 codebuddy / cbc",
            setup_hint,
        )
    return AgentReadiness(
        "codebuddy",
        ReadinessState.READY,
        f"已检测到 CLI：{executable}",
        setup_hint,
        executable,
    )


def _resolve_codebuddy_cli() -> str:
    """仅使用可独立运行的官方 CLI，不借用 App 包内私有进程。"""
    executable = _find_codebuddy_cli()
    if executable is not None:
        return executable
    # 保持其他可选 agent 的惰性启动语义：TUI 可以正常打开，用户实际点名
    # CodeBuddy 时由 transport 报出标准的 executable-not-found 错误。
    return "codebuddy"


async def _open_browser(url: str) -> bool:
    """用独立、可取消的系统 launcher 打开认证页，不阻塞 ACP read loop。"""
    mac_launcher = Path("/usr/bin/open")
    launcher = (
        str(mac_launcher) if mac_launcher.is_file()
        else shutil.which("xdg-open")
    )
    if launcher is None:
        return False
    process = await asyncio.create_subprocess_exec(
        launcher,
        url,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return await asyncio.wait_for(
            process.wait(), timeout=_BROWSER_OPEN_TIMEOUT) == 0
    except BaseException:
        # `open`/`xdg-open` 可能派生 helper 后先行退出。因此不能用
        # leader.returncode 推断整个进程组已回收：宽限期后始终对
        # 启动时拥有的 pgid 做一次 best-effort KILL。
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        if process.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=1)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise


def _codebuddy_auth_notification(
    opener: Callable[[str], Awaitable[bool]],
) -> Callable[[str, dict], Awaitable[None]]:
    """只处理 CodeBuddy ACP 的登录 URL 通知，并拒绝非官方地址。"""
    trusted_domains = ("tencent.com", "codebuddy.cn", "codebuddy.ai")

    async def handle(method: str, params: dict) -> None:
        if method != "_codebuddy.ai/authUrl":
            return
        url = params.get("authUrl") if isinstance(params, dict) else None
        parsed = urlparse(url) if isinstance(url, str) else None
        hostname = parsed.hostname.lower() if parsed and parsed.hostname else ""
        trusted = (
            parsed is not None
            and parsed.scheme == "https"
            and any(
                hostname == domain or hostname.endswith("." + domain)
                for domain in trusted_domains
            )
        )
        if not trusted:
            raise AcpError("CodeBuddy 返回了不可信的认证地址，已拒绝打开")
        try:
            opened = await opener(url)
        except Exception as exc:
            raise AcpError("CodeBuddy 认证页面打开失败") from exc
        if opened is not True:
            raise AcpError("CodeBuddy 认证页面打开失败")

    return handle


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
    - load_failed：session/load 返回可确定 fresh 的标准/获证拒绝
      （已回退 new）。
      capability 不支持时不算失败（根本没尝试）。
    - fresh：本轮是否建立了连接级 session（new 或 load）。复用活跃
      session 时为 False——据此保证 session info 只 emit 一次。
    """
    session_id: str
    restored: bool
    load_failed: bool
    fresh: bool


class _PrepareFallbackEligibleError(Exception):
    """An explicitly classified pre-session failure may use read-only JSONL."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(str(cause))


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
        session_prepare_timeout: float = _SESSION_PREPARE_TIMEOUT,
        inactivity_timeout: float = DEFAULT_AGENT_INACTIVITY_TIMEOUT,
        tool_inactivity_timeout: float = _TOOL_INACTIVITY_TIMEOUT,
        inbound_frame_byte_limit: int = _ACP_INBOUND_FRAME_BYTE_LIMIT,
        prompt_update_queue_byte_limit: int = (
            _PROMPT_UPDATE_QUEUE_BYTE_LIMIT),
        permission_handler: PermissionHandler | None = None,
        fallback_adapter: AgentAdapter | None = None,
        env_overrides: Mapping[str, str] | None = None,
        env_removals: Iterable[str] | None = None,
        execution_env_overrides: Mapping[
            ExecutionMode, Mapping[str, str]
        ] | None = None,
        execution_cmd_overrides: Mapping[
            ExecutionMode, Sequence[str]
        ] | None = None,
        auth_method: str | None = None,
        auth_timeout: float = _AUTH_TIMEOUT,
        auth_notification_queue_byte_limit: int = (
            _AUTH_NOTIFICATION_QUEUE_BYTE_LIMIT),
        auth_notification_handler: Callable[
            [str, dict], Awaitable[None]
        ] | None = None,
        auth_required: bool = False,
        authenticate_on_demand: bool = False,
    ) -> None:
        if cancel_timeout <= 0:
            raise ValueError("cancel_timeout 必须大于 0")
        if session_prepare_timeout <= 0:
            raise ValueError("session_prepare_timeout 必须大于 0")
        if inactivity_timeout <= 0:
            raise ValueError("inactivity_timeout 必须大于 0")
        if tool_inactivity_timeout <= 0:
            raise ValueError("tool_inactivity_timeout 必须大于 0")
        if inbound_frame_byte_limit <= 0:
            raise ValueError("inbound_frame_byte_limit 必须大于 0")
        if prompt_update_queue_byte_limit <= 0:
            raise ValueError("prompt_update_queue_byte_limit 必须大于 0")
        if auth_timeout <= 0:
            raise ValueError("auth_timeout 必须大于 0")
        if auth_notification_queue_byte_limit <= 0:
            raise ValueError(
                "auth_notification_queue_byte_limit 必须大于 0")
        if auth_required and not auth_method:
            raise ValueError("auth_required=True 时必须提供 auth_method")
        if authenticate_on_demand and not auth_method:
            raise ValueError("authenticate_on_demand=True 时必须提供 auth_method")
        self.name = name
        self.session_id: str | None = None
        self._cmd = list(cmd)
        self._permission = permission
        self._permission_handler = permission_handler
        self._fallback = fallback_adapter
        self._env_overrides = dict(env_overrides or {})
        self._env_removals = frozenset(env_removals or ())
        self._execution_env_overrides = {
            mode: dict(overrides)
            for mode, overrides in (execution_env_overrides or {}).items()
        }
        self._execution_cmd_overrides = {
            mode: list(command)
            for mode, command in (execution_cmd_overrides or {}).items()
        }
        self._auth_method = auth_method
        self._auth_timeout = auth_timeout
        self._auth_notification_queue_byte_limit = (
            auth_notification_queue_byte_limit)
        self._auth_notification_handler = auth_notification_handler
        self._auth_required = auth_required
        self._authenticate_on_demand = authenticate_on_demand
        self._active_env_overrides = self._env_for_mode(
            ExecutionMode.DEFAULT)
        self._active_cmd = self._cmd_for_mode(ExecutionMode.DEFAULT)
        self._active_cwd: str | None = None
        self._attachment_root: Path | None = None
        self._cancel_timeout = cancel_timeout
        self._session_prepare_timeout = session_prepare_timeout
        self._inactivity_timeout = inactivity_timeout
        self._tool_inactivity_timeout = tool_inactivity_timeout
        self._inbound_frame_byte_limit = inbound_frame_byte_limit
        self._prompt_update_queue_byte_limit = prompt_update_queue_byte_limit
        self._client = AcpClient(self._active_cmd, permission=permission,
                                 permission_handler=permission_handler,
                                 env_overrides=self._active_env_overrides,
                                 env_removals=self._env_removals,
                                 inbound_frame_byte_limit=(
                                     self._inbound_frame_byte_limit))
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

    @staticmethod
    def _normalize_workdir(workdir: str) -> str:
        try:
            resolved = Path(workdir).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AcpError(f"ACP workdir 不存在或无法解析：{workdir}") from exc
        if not resolved.is_dir():
            raise AcpError(f"ACP workdir 不是目录：{resolved}")
        return str(resolved)

    async def _bind_workdir_locked(
        self,
        workdir: str,
        resume_session_id: str | None,
    ) -> str:
        """Bind child process cwd before initialize; never retarget a live session."""
        normalized = self._normalize_workdir(workdir)
        if self._active_cwd == normalized:
            return normalized
        if self._started:
            raise AcpError(
                f"ACP 活跃 session 绑定在 {self._active_cwd}；"
                f"拒绝切换到 {normalized}，请先 aclose()")
        if self._active_cwd is not None and resume_session_id is not None:
            raise AcpError(
                f"ACP resume session 属于 {self._active_cwd}；"
                f"拒绝在异目录 {normalized} 重放")
        # No request has been submitted on this connection.  The dormant
        # client has no process/reader yet, so binding its cwd in place avoids
        # manufacturing a spurious lifecycle reset before the first start.
        self._active_cwd = normalized
        self._client.cwd = normalized
        return normalized

    def _validate_initialized_client(self, client: AcpClient) -> None:
        """Adapter-specific initialize gate; base ACP accepts standard peers."""

    @staticmethod
    def _prepare_failure_allows_cross_protocol_fallback(
        exc: Exception,
        *,
        phase: str,
    ) -> bool:
        """Classify the narrow prepare failures safe for JSONL fallback.

        Fallback is not a retry transport.  It is allowed only when no session
        request could have been accepted: a local executable cannot start, or
        initialize/session-new explicitly reports that the ACP method is not
        implemented.  Timeout, EOF/transport uncertainty, authentication,
        policy/quota/backend rejection and every session/load error remain on
        the ACP failure path.
        """
        if phase == "initialize" and isinstance(
                exc, (FileNotFoundError, PermissionError)):
            return True
        return (
            phase in {"initialize", "session_new"}
            and isinstance(exc, AcpRemoteError)
            and type(exc.code) is int
            and exc.code == -32601
        )

    def _images_for_prompt(self, prompt: str) -> tuple:
        """Resolve trusted images only when the peer advertises support."""
        if self._client.capabilities.get(
                "promptCapabilities", {}).get("image") is True:
            return prompt_images(prompt, self._attachment_root)
        return ()

    async def _close_active_session(self) -> None:
        """Use standard session/close when the peer advertises it."""
        if not self._started or self.session_id is None:
            return
        session_capabilities = self._client.capabilities.get(
            "sessionCapabilities", {})
        if not isinstance(session_capabilities, dict):
            return
        if not isinstance(session_capabilities.get("close"), dict):
            return
        await self._client.session_close(
            self.session_id, timeout=_SESSION_CLOSE_TIMEOUT)

    async def _prepare_locked(self, workdir: str,
                              resume_session_id: str | None = None
                              ) -> SessionPreparation:
        """在 writer lock 持有期间准备 session；返回不可歧义的结果。

        - 已有活跃 session：resume id 为 None 或正好等于活跃 id 时复用；
          不同 id 直接报错——绝不把活跃 session 偷换成另一个。
        - 未启动：start 后有 resume id 且 agent 声明 loadSession 才尝试
          session/load；只有服务端明确表达“无法加载该 session”的
          AcpRemoteError 可回退 session/new。认证、权限/策略拒绝、内部错误、
          协议参数错误、超时、断线与队列污染都不能继续 new。
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
        fallback_eligible_cause: Exception | None = None
        try:
            client = self._client
            previous_notification_handler = client.on_notification
            auth_notifications: asyncio.Queue[
                tuple[str, dict, int]
            ] | None = None
            queued_auth_notification_bytes = 0

            def release_auth_notification_bytes(size: int) -> None:
                nonlocal queued_auth_notification_bytes
                queued_auth_notification_bytes = max(
                    0, queued_auth_notification_bytes - size)

            if self._auth_method is not None:
                auth_notifications = asyncio.Queue(maxsize=_AUTH_NOTIFICATION_QUEUE_LIMIT)

                def collect_auth_notification(
                    method: str,
                    params: dict,
                ) -> None:
                    nonlocal queued_auth_notification_bytes
                    try:
                        item_bytes = len(json.dumps(
                            {"method": method, "params": params},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8"))
                    except (TypeError, UnicodeError, ValueError) as exc:
                        raise AcpError(
                            "ACP 认证通知无法进行有界字节计量；连接已关闭"
                        ) from exc
                    if (queued_auth_notification_bytes + item_bytes
                            > self._auth_notification_queue_byte_limit):
                        raise AcpError(
                            "ACP 认证通知超过 "
                            f"{self._auth_notification_queue_byte_limit} "
                            "字节上限；连接已关闭")
                    try:
                        auth_notifications.put_nowait(
                            (method, params, item_bytes))
                        queued_auth_notification_bytes += item_bytes
                    except asyncio.QueueFull as exc:
                        # 认证通知可能触发浏览器等安全动作，不能静默丢一条
                        # 后继续。让 read loop fail，prepare 原子回收连接。
                        raise AcpError(
                            "ACP 认证通知超过 64 条上限；连接已关闭"
                        ) from exc

                client.on_notification = collect_auth_notification
            else:
                # session/load 可以重放大量历史通知。prepare 阶段还没有
                # prompt-scoped sink；无认证 adapter 必须直接丢弃，既不积压
                # 内存，也不把旧 session 输出泄漏成当前轮事件。
                client.on_notification = None
            try:
                try:
                    await client.start()
                except Exception as exc:
                    if self._prepare_failure_allows_cross_protocol_fallback(
                            exc, phase="initialize"):
                        fallback_eligible_cause = exc
                    raise
                self._validate_initialized_client(client)
                if (not self._authenticate_on_demand
                        and self._auth_method is not None
                        and (client.auth_methods or self._auth_required)):
                    assert auth_notifications is not None
                    await self._authenticate_bounded(
                        client, auth_notifications,
                        lambda size: release_auth_notification_bytes(size))

                async def prepare_session() -> tuple[bool, bool]:
                    nonlocal fallback_eligible_cause
                    restored = False
                    load_failed = False
                    if (resume_session_id
                            and client.capabilities.get("loadSession")):
                        try:
                            await client.session_load(
                                resume_session_id,
                                workdir,
                                timeout=self._session_prepare_timeout,
                            )
                            self.session_id = resume_session_id
                            restored = True
                        except AcpRemoteError as exc:
                            if not self._load_rejection_allows_fresh(exc):
                                raise
                            # 只吞“该 session 无法 load”的明确拒绝。
                            load_failed = True
                    if not restored:
                        try:
                            self.session_id = await client.session_new(
                                workdir,
                                timeout=self._session_prepare_timeout,
                            )
                        except Exception as exc:
                            if self._prepare_failure_allows_cross_protocol_fallback(
                                    exc, phase="session_new"):
                                fallback_eligible_cause = exc
                            raise
                    return restored, load_failed

                try:
                    restored, load_failed = await prepare_session()
                except AcpRemoteError as exc:
                    if not (
                        self._authenticate_on_demand
                        and self._is_authentication_required(exc)
                    ):
                        raise
                    # CodeBuddy 的独立 CLI 会复用已有登录；只有在
                    # session prepare 明确返回 Authentication required 时
                    # 才打开登录页，避免每次启动强制注销已有会话。
                    assert auth_notifications is not None
                    await self._authenticate_bounded(
                        client, auth_notifications,
                        lambda size: release_auth_notification_bytes(size))
                    restored, load_failed = await prepare_session()
            finally:
                client.on_notification = previous_notification_handler
        except BaseException as exc:
            # 原子初始化：start 成功但 session/new 失败时，进程和
            # reader/stderr tasks 必须回收，否则下次 stream 会对
            # 同一个 client 重复 start，覆盖活进程句柄留下孤儿
            await self._reset()
            if fallback_eligible_cause is exc:
                raise _PrepareFallbackEligibleError(exc) from exc
            raise
        self._started = True
        return SessionPreparation(
            session_id=self.session_id, restored=restored,
            load_failed=load_failed, fresh=True)

    async def _authenticate_bounded(
        self,
        client: AcpClient,
        notifications: asyncio.Queue[tuple[str, dict, int]],
        release_notification_bytes: Callable[[int], None],
    ) -> None:
        try:
            await asyncio.wait_for(
                self._authenticate_with_notifications(
                    client, notifications, release_notification_bytes),
                timeout=self._auth_timeout,
            )
        except TimeoutError as exc:
            raise AcpError(
                f"ACP 认证在 {self._auth_timeout:g} 秒内未完成；"
                "连接已关闭，可重试"
            ) from exc

    @staticmethod
    def _is_authentication_required(exc: AcpRemoteError) -> bool:
        return (
            type(exc.code) is int
            and exc.code == -32000
            and exc.remote_message == "Authentication required"
        )

    @staticmethod
    def _load_rejection_allows_fresh(exc: AcpRemoteError) -> bool:
        """Only deterministic inability to load permits a fresh session.

        ACP defines -32002 Resource not found.  -32601 is also deterministic
        when a peer advertised loadSession but then rejects the method.
        Generic server errors (notably -32000), authentication, policy/quota
        denials, malformed requests, internal failures and cancellation cannot
        be bypassed with session/new.  Any evidenced vendor-specific mapping
        belongs in that concrete adapter, never in this common classifier.
        """
        return type(exc.code) is int and exc.code in {-32002, -32601}

    async def _authenticate_with_notifications(
        self,
        client: AcpClient,
        notifications: asyncio.Queue[tuple[str, dict, int]],
        release_notification_bytes: Callable[[int], None],
    ) -> None:
        """认证与通知并发推进；耗时 UI 动作永远不在 ACP read loop 执行。"""
        assert self._auth_method is not None
        auth_task = asyncio.create_task(
            client.authenticate(self._auth_method))
        notification_task: asyncio.Task | None = None
        try:
            while True:
                notification_task = asyncio.create_task(notifications.get())
                done, _pending = await asyncio.wait(
                    {auth_task, notification_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if notification_task in done:
                    method, params, item_bytes = notification_task.result()
                    release_notification_bytes(item_bytes)
                    if self._auth_notification_handler is not None:
                        await self._auth_notification_handler(method, params)
                else:
                    notification_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await notification_task
                notification_task = None
                if auth_task in done:
                    await auth_task
                    return
        finally:
            if notification_task is not None and not notification_task.done():
                notification_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await notification_task
            if not auth_task.done():
                auth_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await auth_task

    async def _reset(
        self,
        env_overrides: Mapping[str, str] | None = None,
        cmd: Sequence[str] | None = None,
        cwd: str | None = None,
        *,
        graceful: bool = True,
    ) -> None:
        """关闭当前连接并标记必须重建（下轮 stream 重新 start + session/new）。"""
        if graceful:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._close_active_session(),
                    timeout=_SESSION_CLOSE_TIMEOUT,
                )
        with contextlib.suppress(Exception):
            await self._client.close()
        if env_overrides is not None:
            self._active_env_overrides = dict(env_overrides)
        if cmd is not None:
            self._active_cmd = list(cmd)
        if cwd is not None:
            self._active_cwd = cwd
        self._client = AcpClient(self._active_cmd, permission=self._permission,
                                 permission_handler=self._permission_handler,
                                 env_overrides=self._active_env_overrides,
                                 env_removals=self._env_removals,
                                 cwd=self._active_cwd or ".",
                                 inbound_frame_byte_limit=(
                                     self._inbound_frame_byte_limit))
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
            fallback_workdir = workdir
            workdir = await self._bind_workdir_locked(
                workdir, resume_session_id)
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
            except _PrepareFallbackEligibleError as exc:
                # Only the explicit pre-session classifier above may cross
                # protocols.  Fatal load/auth/policy/quota/transport/timeout
                # errors never enter this branch.
                if was_started or self._fallback is None:
                    raise exc.cause from exc
                async for event in self._stream_fallback_locked(
                        make_prompt, fallback_workdir, resume_session_id,
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
        updates: asyncio.Queue = asyncio.Queue(
            maxsize=_PROMPT_UPDATE_QUEUE_LIMIT)
        queued_update_bytes = 0
        seen_status: set[str] = set()
        pending_permissions: set[int | float | str] = set()
        tool_contexts: dict[str, dict[str, str]] = {}
        tool_fingerprints: dict[str, tuple[str, str, str, str]] = {}
        active_tools: set[str] = set()
        last_tool_key: str | None = None
        delivery_committed = False
        capacity_error: AcpError | None = None
        connection_poisoned = False
        caller_cancelled: asyncio.CancelledError | None = None
        cancel_sent = False
        cancel_prompt_not_sent = False
        cancel_result: object | None = None
        cancel_result_observed = False
        client = self._client

        def poison_update_queue(error: AcpError) -> None:
            """Publish a bounded wake-up when an item itself cannot be queued."""
            nonlocal capacity_error
            if capacity_error is not None:
                return
            capacity_error = error
            try:
                # If the queue is full, an existing item already guarantees the
                # consumer will wake.  If it is empty (for example one giant
                # first notification), this zero-byte sentinel prevents waiting
                # until the inactivity watchdog despite a known fatal error.
                updates.put_nowait(("__capacity_error__", None, 0))
            except asyncio.QueueFull:
                pass

        def enqueue_update(item: tuple[str, object]) -> None:
            nonlocal queued_update_bytes
            if capacity_error is not None:
                return
            method, payload = item
            if method == "__done__":
                item_bytes = 1
            else:
                try:
                    item_bytes = len(json.dumps(
                        {"method": method, "payload": payload},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8"))
                except (TypeError, UnicodeError, ValueError):
                    poison_update_queue(AcpError(
                        "ACP prompt 通知无法进行有界字节计量；"
                        "连接已关闭"))
                    return
            if (queued_update_bytes + item_bytes
                    > self._prompt_update_queue_byte_limit):
                poison_update_queue(AcpError(
                    "ACP prompt 更新队列超过 "
                    f"{self._prompt_update_queue_byte_limit} 字节上限；"
                    "连接已关闭"))
                return
            try:
                updates.put_nowait((method, payload, item_bytes))
                queued_update_bytes += item_bytes
            except asyncio.QueueFull:
                # The read loop must never accumulate prompt notifications
                # without bound. Dropping after this point is safe only because
                # the entire connection is poisoned and rebuilt in finally.
                poison_update_queue(AcpError(
                    "ACP prompt 更新队列超过 "
                    f"{_PROMPT_UPDATE_QUEUE_LIMIT} 条上限；连接已关闭"))

        def clear_tool_tracking(tool_key: str) -> None:
            active_tools.discard(tool_key)
            tool_contexts.pop(tool_key, None)
            tool_fingerprints.pop(tool_key, None)

        def reserve_tool_tracking(tool_key: str) -> None:
            nonlocal capacity_error
            if tool_key in tool_contexts:
                return
            if len(tool_contexts) >= _PROMPT_TOOL_TRACKING_LIMIT:
                capacity_error = AcpError(
                    "ACP prompt 工具跟踪超过 "
                    f"{_PROMPT_TOOL_TRACKING_LIMIT} 个上限；连接已关闭")
                raise AgentDeliveryUncertainError(
                    str(capacity_error)) from capacity_error

        def on_notify(method: str, params: dict) -> None:
            if params.get("sessionId") == session_id:
                enqueue_update((method, params))

        def on_permission_activity(
                phase: str, request_id: int | float | str,
                params: dict) -> None:
            if params.get("sessionId") == session_id:
                enqueue_update((
                    "__permission_activity__",
                    {"phase": phase, "request_id": request_id},
                ))

        # Adapter-specific image capability checks happen before callbacks are
        # installed, so a local image validation error cannot retain prompt
        # state on the long-lived client.
        images = self._images_for_prompt(prompt)
        client.on_notification = on_notify
        client.on_permission_activity = on_permission_activity
        if execution_mode is ExecutionMode.READ_ONLY:
            client.set_permission_handler(
                lambda _params: {"outcome": "cancelled"})
        else:
            client.set_permission_handler(self._permission_handler)
        task = asyncio.create_task(
            client.prompt(session_id, prompt, images))

        def on_task_done(done_task: asyncio.Task) -> None:
            enqueue_update(("__done__", done_task))

        task.add_done_callback(on_task_done)
        try:
            while True:
                if capacity_error is not None:
                    if not delivery_committed:
                        delivery_committed = True
                        yield AgentEvent("delivery_committed", meta={
                            "sessionId": session_id,
                            "transport": "acp",
                        })
                    raise AgentDeliveryUncertainError(
                        str(capacity_error)) from capacity_error
                try:
                    if pending_permissions:
                        # 人类审批时长不属于 agent 静默。取消/关闭仍会直接
                        # cancel 本协程，不会因无 timeout 而失去退出能力。
                        method, payload, item_bytes = await updates.get()
                    else:
                        timeout = (
                            self._tool_inactivity_timeout
                            if active_tools else self._inactivity_timeout
                        )
                        method, payload, item_bytes = await asyncio.wait_for(
                            updates.get(), timeout=timeout)
                    queued_update_bytes -= item_bytes
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
                            delivery_committed = True
                            yield AgentEvent("delivery_committed", meta={
                                "sessionId": session_id,
                                "transport": "acp",
                            })
                        # stdio drain already completed before request() can
                        # receive this response.  A remote error is a
                        # deterministic failure, but not proof that tools did
                        # not run, so the no-replay boundary is mandatory.
                        raise
                    except Exception as exc:
                        # request 已经完成 stdio drain，但没有得到可信的
                        # 显式拒绝；断线/协议错误可能发生在 agent 已
                        # 开始执行之后，必须走 no-replay。
                        if not delivery_committed:
                            delivery_committed = True
                            yield AgentEvent("delivery_committed", meta={
                                "sessionId": session_id,
                                "transport": "acp",
                            })
                        connection_poisoned = True
                        raise AgentDeliveryUncertainError(
                            f"ACP prompt 已发送但结果不确定：{exc}"
                        ) from exc
                    if not delivery_committed:
                        delivery_committed = True
                        yield AgentEvent("delivery_committed", meta={
                            "sessionId": session_id,
                            "transport": "acp",
                        })
                    stop_reason = (
                        result.get("stopReason")
                        if isinstance(result, dict) else None
                    )
                    if stop_reason != "end_turn":
                        # ACP response 只证明 prompt 已确定终止，不代表任务成功。
                        # delivery_committed 已先发布，所以上层会保留 no-replay
                        # cursor；max-token/refusal/cancel/缺失/未来未知值都不得
                        # 伪装成 done，也无需用“不确定交付”掩盖明确终局。
                        raise AcpError(
                            "ACP prompt 返回非成功 stopReason："
                            f"{stop_reason!r}")
                    yield AgentEvent("done", meta={
                        "stopReason": stop_reason})
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
                    title = _bounded_tool_text(
                        update.get("title"),
                        byte_limit=_TOOL_TITLE_BYTE_LIMIT,
                        default="(未命名工具)",
                    )
                    raw_input = update.get("rawInput")
                    command = ""
                    if isinstance(raw_input, dict):
                        value = raw_input.get("command")
                        if isinstance(value, str):
                            command = _bounded_tool_text(
                                value,
                                byte_limit=_TOOL_COMMAND_BYTE_LIMIT,
                            )
                    tool_call_id = _bounded_tool_text(
                        update.get("toolCallId"),
                        byte_limit=_TOOL_CALL_ID_BYTE_LIMIT,
                    ) or None
                    tool_key = str(tool_call_id or title)
                    last_tool_key = tool_key
                    tool_kind = _bounded_tool_text(
                        update.get("kind"),
                        byte_limit=_TOOL_KIND_BYTE_LIMIT,
                    )
                    status_value = _bounded_tool_text(
                        update.get("status"),
                        byte_limit=_TOOL_STATUS_BYTE_LIMIT,
                    )
                    if status_value.lower() in _TERMINAL_TOOL_STATUSES:
                        clear_tool_tracking(tool_key)
                    else:
                        reserve_tool_tracking(tool_key)
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
                        "status": status_value or None,
                        "command": command,
                    })
                elif kind == "tool_call_update":
                    tool_call_id = _bounded_tool_text(
                        update.get("toolCallId"),
                        byte_limit=_TOOL_CALL_ID_BYTE_LIMIT,
                    ) or None
                    update_title = _bounded_tool_text(
                        update.get("title"),
                        byte_limit=_TOOL_TITLE_BYTE_LIMIT,
                    )
                    fallback_key = str(
                        tool_call_id or update_title
                        or last_tool_key or "工具调用")
                    previous = tool_contexts.get(fallback_key, {})
                    title = (
                        update_title
                        or previous.get("title")
                        or "工具调用")
                    command = previous.get("command", "")
                    raw_input = update.get("rawInput")
                    if isinstance(raw_input, dict):
                        value = raw_input.get("command")
                        if isinstance(value, str):
                            command = _bounded_tool_text(
                                value,
                                byte_limit=_TOOL_COMMAND_BYTE_LIMIT,
                            )
                    tool_kind = (
                        _bounded_tool_text(
                            update.get("kind"),
                            byte_limit=_TOOL_KIND_BYTE_LIMIT,
                        )
                        or previous.get("kind")
                        or "")
                    status_value = _bounded_tool_text(
                        update.get("status"),
                        byte_limit=_TOOL_STATUS_BYTE_LIMIT,
                        default="updated",
                    )
                    fingerprint = (
                        title, status_value, command, tool_kind)
                    terminal = (
                        status_value.lower() in _TERMINAL_TOOL_STATUSES)
                    if terminal:
                        clear_tool_tracking(fallback_key)
                    else:
                        reserve_tool_tracking(fallback_key)
                        active_tools.add(fallback_key)
                    # Kimi 可能在一次工具调用中高频发送完全相同的
                    # in_progress update。它们证明连接活着，但对消费者没有
                    # 新信息；只保留可见状态迁移。
                    if (not terminal
                            and tool_fingerprints.get(
                                fallback_key) == fingerprint):
                        yield AgentEvent("activity", meta={
                            "tool_call_id": tool_call_id,
                            "phase": "tool",
                        })
                        continue
                    if not terminal:
                        tool_fingerprints[fallback_key] = fingerprint
                        tool_contexts[fallback_key] = {
                            "title": title,
                            "command": command,
                            "kind": tool_kind,
                        }
                    yield AgentEvent("tool", title, meta={
                        "tool_call_id": tool_call_id,
                        "tool_kind": tool_kind or None,
                        "status": status_value,
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
        except asyncio.CancelledError as exc:
            # A CommandBus/TUI cancellation can arrive after session/prompt was
            # drained but before the first ACP update.  Preserve the exception
            # until the finite cancel handshake below establishes whether this
            # was a pre-send cancellation or a committed no-replay boundary.
            caller_cancelled = exc
        finally:
            task.remove_done_callback(on_task_done)
            client.set_permission_handler(self._permission_handler)
            if client.on_notification is on_notify:
                client.on_notification = None
            if client.on_permission_activity is on_permission_activity:
                client.on_permission_activity = None
            needs_reset = capacity_error is not None or connection_poisoned
            if not task.done():
                # 取消契约：先通知中断，再等确认；锁在整个 finally
                # 期间一直持有，下一轮不会提前开始。
                try:
                    await asyncio.wait_for(
                        client.cancel(
                            session_id,
                            permission_reap_timeout=self._cancel_timeout,
                        ),
                        timeout=self._cancel_timeout,
                    )
                    cancel_sent = True
                except (TimeoutError, Exception):
                    needs_reset = True
                try:
                    cancel_result = await asyncio.wait_for(
                        task, timeout=self._cancel_timeout)
                    cancel_result_observed = True
                except AcpRequestNotSentError:
                    cancel_prompt_not_sent = True
                except (asyncio.CancelledError, Exception):
                    # 超时或出错：连接状态不可信，关闭并标记重建
                    needs_reset = True
            else:
                # Overflow may drop the __done__ sentinel. Retrieve the task
                # result here so its exception cannot escape as an unhandled
                # background-task warning after the poisoned connection closes.
                try:
                    cancel_result = task.result()
                    cancel_result_observed = True
                except AcpRequestNotSentError:
                    cancel_prompt_not_sent = True
                except (asyncio.CancelledError, Exception):
                    if caller_cancelled is not None:
                        needs_reset = True

            if ((cancel_sent or caller_cancelled is not None)
                    and not cancel_prompt_not_sent):
                # Only an explicit pre-send failure may retain the old cursor.
                # Every actual session/cancel (stream.aclose, inactivity,
                # overflow or outer task cancellation), plus a caller-cancel
                # race whose prompt task already completed, crossed a possibly
                # accepted prompt.  The connection is reusable only after the
                # exact ACP terminal confirms cancellation; end_turn/empty/
                # future values are not an interrupt acknowledgement.
                stop_reason = (
                    cancel_result.get("stopReason")
                    if cancel_result_observed
                    and isinstance(cancel_result, dict)
                    else None
                )
                if stop_reason != "cancelled":
                    needs_reset = True
            if needs_reset:
                # A poisoned connection (overflow, transport/read-loop failure,
                # or bounded cancel failure) must not attempt another polite
                # write before process teardown: its stdin may be backpressured.
                await self._reset(graceful=False)

        if caller_cancelled is not None:
            if cancel_prompt_not_sent:
                raise caller_cancelled
            raise AgentDeliveryCancelledError(
                "ACP prompt 已发送后被取消，已固化 no-replay 边界"
            ) from caller_cancelled

    async def aclose(self) -> None:
        # 与 stream 同一把锁：close 不会与进行中的 session/new/prompt
        # 竞态（否则可能在 stream 眼皮底下杀掉进程）
        async with self._lock:
            with contextlib.suppress(Exception):
                # session_close() 会传 request timeout，外层仍要有
                # 独立上限：即使未来的 client 实现在 write_lock/drain
                # 之前卡住，用户退出也必须继续到 force teardown。
                await asyncio.wait_for(
                    self._close_active_session(),
                    timeout=_SESSION_CLOSE_TIMEOUT,
                )
            await self._client.close()
            self._started = False
            self.session_id = None
            self._session_execution_mode = None


class AcpKimiAdapter(AcpAdapter):
    @classmethod
    def host_capability(cls) -> AgentHostCapability:
        # Kimi ACP currently has no independently proven runtime hard-deny
        # profile.  Its checked-in JSONL agent is an actual Read/Grep/Glob
        # tool closure, so Host uses that safe transport rather than pretending
        # that an upper-layer READ_ONLY enum constrains ACP tools.
        from adapters.kimi_adapter import KimiAdapter

        return AgentHostCapability(
            "jsonl", KimiAdapter.readonly_fallback)

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
    @classmethod
    def host_capability(cls) -> AgentHostCapability:
        return AgentHostCapability(
            "acp", lambda: cls(fallback_jsonl=False))

    @staticmethod
    def _load_rejection_allows_fresh(exc: AcpRemoteError) -> bool:
        if AcpAdapter._load_rejection_allows_fresh(exc):
            return True
        # OpenCode maps its concrete ACPSessionNotFoundError to JSON-RPC
        # invalidParams (-32602).  Keep this compatibility evidence local to
        # the OpenCode adapter; generic -32602 errors must remain fatal.
        message = exc.remote_message.strip()
        prefix = "session not found:"
        return (
            type(exc.code) is int
            and exc.code == -32602
            and message.casefold().startswith(prefix)
            and bool(message[len(prefix):].strip())
        )

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

    @classmethod
    def host_capability(cls) -> AgentHostCapability:
        return AgentHostCapability("acp", cls)

    def __init__(self, permission: str = "deny") -> None:
        super().__init__(
            "qwen",
            QWEN_ACP_DEFAULT_CMD,
            permission=permission,
            execution_cmd_overrides={
                ExecutionMode.READ_ONLY: QWEN_ACP_READ_ONLY_CMD,
            },
        )


class AcpCodeBuddyAdapter(AcpAdapter):
    """CodeBuddy 的 ACP-only 生产 adapter。"""

    @classmethod
    def host_capability(cls) -> AgentHostCapability:
        return AgentHostCapability("acp", cls)

    def __init__(
        self,
        permission: str = "deny",
        *,
        auth_timeout: float = _AUTH_TIMEOUT,
        auth_url_opener: Callable[
            [str], Awaitable[bool]
        ] = _open_browser,
    ) -> None:
        if not (
            inspect.iscoroutinefunction(auth_url_opener)
            or inspect.iscoroutinefunction(
                getattr(auth_url_opener, "__call__", None))
        ):
            raise TypeError("auth_url_opener 必须是 async callable")
        executable = _resolve_codebuddy_cli()
        auth_method = (
            os.environ.get("MYAGENTS_CODEBUDDY_AUTH_METHOD", "internal").strip()
            or "internal"
        )
        default_cmd = (executable, *CODEBUDDY_ACP_DEFAULT_ARGS)
        # plan 会继承进入前的权限基线，不是硬只读。dontAsk 配合工具闭集
        # 让 write/edit/bash/network/subagent 在 runtime 层根本不可调用。
        read_only_cmd = (executable, *CODEBUDDY_ACP_READ_ONLY_ARGS)
        super().__init__(
            "codebuddy",
            default_cmd,
            permission=permission,
            env_overrides={
                "CODEBUDDY_DISABLE_COMPILE_CACHE": "1",
                "CODEBUDDY_INTERNET_ENVIRONMENT": "internal",
            },
            execution_cmd_overrides={
                ExecutionMode.READ_ONLY: read_only_cmd,
            },
            auth_method=auth_method,
            auth_timeout=auth_timeout,
            auth_notification_handler=_codebuddy_auth_notification(
                auth_url_opener),
            auth_required=True,
            authenticate_on_demand=True,
        )

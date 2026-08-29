"""Orchestrator：整个系统的大脑。

关键概念（学习要点）：
- **Hub-and-spoke（中心辐射）**：agent 之间从不直接对话。所有消息先进
  共享时间线（history），@谁就把上下文打包发给谁，回复再贴回时间线。
  这样不会有"两个 agent 互相@死循环"，也只有一个地方需要管上下文。
- **多种传输，两种上下文策略**（AgentSpec.transport）：
  - JSONL adapter：无头调用每次是全新会话，agent 看不到聊天记录，
    把最近 N 条对话记录塞进 prompt 一起发（transcript 快照）。
  - 有状态 adapter（`stateful_session`，ACP、原生 RPC 或 app-server）：原生会话
    在 agent 侧保持，编排器
    只发**增量**——每个 agent 一个 history cursor，记录已交付到哪儿；
    每轮只发 cursor 之后的新消息（跳过 agent 自己的回复，那些本来就在
    它的 ACP session 里；首次 bootstrap 限发最近 N 条）。读 cursor →
    选增量 → stream → 推进 cursor 在每-agent delivery lock 内原子完成，
    同一 agent 的并发 dispatch 严格串行。成功交付后推进 cursor；提交前
    或服务端明确拒绝的失败不推进，下一轮补发；提交后结果不确定的失败先
    建立 no-replay cursor，防止重复执行工具任务。
- **并发扇出（fan-out）**：一条消息 @多个 agent 时并行派发，互不等待。
- **有序协作（pipeline）**：自然语言表达明确依赖时，host 只提取 2–4 步
  固定计划，编排器串行推进并把前序真实回复交给后续步骤。
- **Supervisor（中心协调者）**：注册表里的 host 是一个由 LLM 扮演的
  主持人。显式 @ 永远优先；用户没点名时，host 用一次调用直接回答或
  决定派发给谁（见 host.py）。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from acp.adapter import (
    AcpKimiAdapter,
    AcpOpenCodeAdapter,
    AcpQwenAdapter,
    AcpCodeBuddyAdapter,
    AgentPermissionHandler,
    codebuddy_readiness_probe,
)
from agent_readiness import (
    AgentEnablementConfig,
    AgentEnablementError,
    AgentReadiness,
    AgentReadinessRegistry,
    AgentUnavailableError,
    ReadinessProbe,
    ReadinessState,
    executable_probe,
)
from adapters.base import (
    AgentAdapter,
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    AgentEvent,
    AgentHostCapability,
    ExecutionMode,
)
from codex_app_server.adapter import CodexAppServerAdapter
from clipboard_image import prompt_images
from context_lifecycle import (
    AdapterContextSnapshot,
    ContextCheckpoint,
    ContextCompactionResult,
    ContextLifecycleError,
    ContextPolicy,
    ContextStatus,
)
from dsh_acp import AcpDshAdapter, dsh_readiness_probe
from pi_rpc.adapter import PiRpcAdapter
from collaboration import (
    CollaborationPlan,
    CollaborationPlanEvent,
    CollaborationValidationError,
    MAX_COLLABORATION_STEPS,
    has_ordered_collaboration_cue,
)
from discussion import (
    MIN_DISCUSSION_PARTICIPANTS,
    DiscussionRequest,
    DiscussionValidationError,
    moderator_assignment,
    parse_natural_discussion_request,
    parse_discussion_request,
    participant_assignment,
)
from host import MODERATOR_TEMPLATE, HostAgent
from host_backend import (
    HostBackendSelection,
    HostBackendStatus,
    HostBackendValidationError,
)
from native_agent import (
    NativeModelConfigurationError,
    create_native_host_runtime,
    native_model_profile_names,
    native_host_readiness_probe,
    resolve_native_model_config,
)
from session_roles import (
    SessionRole,
    SessionRoleChanges,
    apply_session_role_changes,
    has_session_role_cue,
    session_role_assignment,
)
from workflow import (
    MilestoneWorkflow,
    StageDelivery,
    WorkflowValidationError,
    parse_steer_instruction,
    parse_workflow_request,
)
from workspace import GitWorkspaceInspector
from storage.store import (
    DEFAULT_SESSION_NAME,
    MAX_READ_LIMIT,
    CorruptedStorageError,
    RoomLease,
    RoomStore,
    WorkdirMismatchError,
    normalize_session_name,
    normalize_workdir,
)

HOST_NAME = "host"

_MENTION_RE = re.compile(r"@(\w+)")


@dataclass(frozen=True)
class AgentSpec:
    """一个工人 agent 的注册信息：名字 + 传输协议 + adapter 工厂。

    transport:
      - "acp"        ACP 有状态会话，编排器发增量上下文
      - "acp+jsonl" ACP 为主，仅 prepare 失败时受限 JSONL 降级
      - "app-server" Codex 原生有状态 thread，编排器发增量上下文
      - "rpc"        厂商原生有状态 RPC，编排器发增量上下文
      - "jsonl"      无头一次性调用，编排器发完整 transcript 快照

    新增 agent = 在这里加一行（写一个 adapter 类）。协议判断只看
    transport / adapter 能力声明，不散落 `if name == ...`。
    """

    name: str
    transport: str  # "acp" | "acp+jsonl" | "app-server" | "rpc" | "jsonl"
    factory: Callable[[], AgentAdapter]
    probe: ReadinessProbe | None = None
    display_name: str = ""
    purpose: str = ""

    def agent_host_capability(self) -> AgentHostCapability | None:
        """Resolve an adapter-owned Host declaration without name branching."""
        declaration = getattr(self.factory, "host_capability", None)
        if not callable(declaration):
            return None
        capability = declaration()
        if not isinstance(capability, AgentHostCapability):
            raise TypeError("adapter host_capability 返回了未知契约")
        return capability


@dataclass(frozen=True)
class AgentFailure:
    """一轮 fan-out 中某个 worker 的终态失败。"""

    agent: str
    error: str


@dataclass(frozen=True)
class DispatchOutcome:
    """Orchestrator 调度结果；所有 worker 都收尾后再汇总失败。"""

    failures: tuple[AgentFailure, ...] = ()

    def error_summary(self) -> str:
        return "；".join(
            f"{failure.agent}: {failure.error}"
            for failure in self.failures)


@dataclass(frozen=True)
class _PreparedStreamResult:
    """Raw result of one stateful prepared stream before timeline policy."""

    text: str
    done_meta: dict
    failed: str | None
    delivered_upto: int


@dataclass
class _RuntimeInterjectionProposal:
    """One validated native in-flight write, committed only by CommandBus."""

    event: AgentEvent
    receipt: dict[str, object]
    _commit: Callable[[], Awaitable[None]]
    _committed: bool = False

    async def commit(self) -> dict[str, object]:
        if self._committed:
            raise RuntimeError("插话 proposal 已提交")
        await self._commit()
        self._committed = True
        return dict(self.receipt)


# 工人 agent 注册表：长连接协议优先，JSONL 只作受约束降级。
# Kimi 主路径是 ACP（命令 ["kimi", "acp"]），只允许在
# ACP prepare 失败前使用只读 JSONL fallback；OpenCode 使用同一 ACP seam，
# 但由专用 adapter 注入 ask-by-default 权限策略和 OpenCode 只读配置；
# Qwen Code 使用 ACP-only；Codex 使用官方 app-server 长连接；旧 Codex JSONL
# adapter 保留为 fallback。
AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        "kimi", "acp+jsonl", AcpKimiAdapter,
        executable_probe("kimi", ("kimi",), "安装 Kimi Code CLI"),
        display_name="Kimi Code", purpose="代码分析与实现",
    ),
    AgentSpec(
        "opencode", "acp+jsonl", AcpOpenCodeAdapter,
        executable_probe("opencode", ("opencode",), "安装 OpenCode CLI"),
        display_name="OpenCode", purpose="代码实现与审查",
    ),
    AgentSpec(
        "qwen", "acp", AcpQwenAdapter,
        executable_probe("qwen", ("qwen",), "安装 Qwen Code CLI"),
        display_name="Qwen Code", purpose="软件工程协作",
    ),
    AgentSpec(
        "codebuddy", "acp", AcpCodeBuddyAdapter,
        codebuddy_readiness_probe,
        display_name="CodeBuddy", purpose="代码分析与实现",
    ),
    AgentSpec(
        "dsh", "acp", AcpDshAdapter,
        dsh_readiness_probe,
        display_name="DeepSeek Harness", purpose="工程任务执行",
    ),
    AgentSpec(
        "pi", "rpc", PiRpcAdapter,
        executable_probe("pi", ("pi",), "安装 Pi coding agent CLI"),
        display_name="Pi", purpose="轻量工程协作",
    ),
    AgentSpec(
        "codex", "app-server", CodexAppServerAdapter,
        executable_probe("codex", ("codex",), "安装 Codex CLI"),
        display_name="Codex", purpose="代码实现、审查与工具执行",
    ),
)
AGENTS: dict[str, AgentSpec] = {spec.name: spec for spec in AGENT_SPECS}

_HOST_READINESS_PROBE = native_host_readiness_probe
_HOST_CLOSE_TIMEOUT = 10.0


class _UnavailableHostAdapter:
    """Fail-closed placeholder for a persisted but unavailable selection."""

    name = HOST_NAME
    session_id = None

    def __init__(self, detail: str) -> None:
        self.detail = detail

    async def stream(self, *_args, **_kwargs):
        raise RuntimeError(self.detail)
        yield AgentEvent("done")  # pragma: no cover


async def _close_host_bounded(host: HostAgent) -> None:
    await asyncio.wait_for(host.aclose(), timeout=_HOST_CLOSE_TIMEOUT)


async def _close_host_quietly(host: HostAgent) -> None:
    """Best-effort candidate cleanup without hiding the primary switch error."""
    try:
        await _close_host_bounded(host)
    except (asyncio.CancelledError, Exception):
        pass


def _ready_probe(name: str) -> ReadinessProbe:
    """嵌入/测试模式的兼容 probe：注册即视为可用。"""

    def probe() -> AgentReadiness:
        return AgentReadiness(
            name,
            ReadinessState.READY,
            "调用方已提供 adapter",
            "无需本机 CLI 探测",
        )

    return probe

# 发给 JSONL agent 的 prompt 模板：身份 + 对话记录 + 工作目录约定
_PROMPT_TEMPLATE = """\
你在一个名叫 myagents 的多 agent 聊天室里，身份是 "{name}"。
房间里有一个人类用户，可能还有其他 AI agent。
以下是最近的对话记录（格式 [发言者] 内容）：

{transcript}

{assignment}
请作为 {name}，针对用户最新的消息给出回复或执行其中的任务。
要求：直接输出内容，不要自我介绍，不要复述上面的记录。
遇到 `[图片 N]` 或兼容格式 `[图片附件：绝对路径]` 时，先使用可用的图像或文件读取能力查看图片。
如需读写文件、运行命令，都在当前目录内进行。
"""

# 发给 ACP agent 的 prompt 模板：session 原生记忆完整上下文，
# 只补"你上次被派发之后"的新消息（含其他 agent 的发言）
_ACP_PROMPT_TEMPLATE = """\
你在一个名叫 myagents 的多 agent 聊天室里，身份是 "{name}"。
房间里有一个人类用户，可能还有其他 AI agent。你的会话保持着完整上下文，
以下是自上次派发给你之后的新消息（格式 [发言者] 内容）：

{transcript}

{assignment}
请作为 {name}，针对最新的消息给出回复或执行其中的任务。
要求：直接输出内容，不要自我介绍，不要复述上面的记录。
遇到 `[图片 N]` 或兼容格式 `[图片附件：绝对路径]` 时，先使用可用的图像或文件读取能力查看图片。
如需读写文件、运行命令，都在当前目录内进行。
"""


@dataclass
class Message:
    speaker: str  # "user" 或 agent 名
    text: str
    # 持久化/内存序号：有 store 时来自 timeline 的单调 seq；无 store 时
    # 由 _append_message 单调分配。直接构造（兼容旧用法）时保持 0。
    seq: int = 0
    created_at: str = ""  # UTC ISO-8601；内存模式留空
    command_id: str | None = None


# on_event(agent_name, event) —— UI 层传进来的回调
EventCallback = Callable[[str, AgentEvent], None]


class _CheckpointError(Exception):
    """cursor/session_id checkpoint 落盘失败。

    必须穿透 _deliver_prepared 向 dispatch 传播：store 已不可信，不能把
    本轮伪装成普通 agent 失败（那会去 append timeline）。adapter 侧按
    stream_prepared 契约回收未提交的 fresh session。
    """


class _PostSubmitCheckpointError(_CheckpointError):
    """A durable no-replay checkpoint failed after prompt acceptance.

    Unlike a pre-submit checkpoint failure, retrying this room is unsafe: the
    agent may already have performed side effects while the durable cursor
    still points before the accepted turn.  ``cause`` is re-raised unchanged
    after the active stream is closed and the orchestrator is poisoned.
    """

    def __init__(self, name: str, cause: Exception) -> None:
        self.cause = cause
        super().__init__(
            f"agent {name!r} post-submit checkpoint 写失败：{cause}；"
            "Orchestrator 已停用以防止重放")


class _EventCallbackError(Exception):
    """区分 event sink 与 agent 异常；sink 失败不得被当成 agent 失败吞掉。"""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class OrchestratorClosedError(Exception):
    """Orchestrator 已关闭：拒绝新的 dispatch 和排队中的 delivery。"""


async def _gather_structured(*awaitables):
    """Gather peers without abandoning siblings after one branch fails.

    ``asyncio.gather`` propagates the first exception while other awaitables
    keep running.  Room writers need a stronger scope: after a fatal
    no-replay checkpoint error poisons the Orchestrator, no sibling may append
    history in the background.  Outer cancellation follows the same cleanup
    contract.
    """
    tasks = [asyncio.create_task(awaitable) for awaitable in awaitables]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _close_async_stream(stream) -> None:
    """Best-effort producer cleanup after the consumer aborts iteration.

    Python's ``async for`` does not close an arbitrary async iterator when its
    body raises.  Stateful adapters commonly hold their single-writer lock for
    the iterator lifetime, so every consumer-side failure must explicitly
    finish it before propagating the primary error.
    """
    closer = getattr(stream, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except (asyncio.CancelledError, Exception):
        # Cleanup is secondary to the callback/checkpoint error that caused
        # iteration to stop.  Adapters retain their own bounded reset path.
        pass


class Orchestrator:
    def __init__(self, workdir: str, history_limit: int = 12,
                 specs: tuple[AgentSpec, ...] = AGENT_SPECS, *,
                 store: RoomStore | None = None,
                 persistent: bool = True,
                 session_name: str = DEFAULT_SESSION_NAME,
                 workspace_inspector=None,
                 discover_agents: bool = False,
                 host_probe: ReadinessProbe | None = None,
                 host_model_factory: Callable[
                     [HostBackendSelection], AgentAdapter] | None = None,
                 agent_enablement: AgentEnablementConfig | None = None,
                 context_policy: ContextPolicy | None = None,
                 default_host_backend: HostBackendSelection | None = None,
                 ) -> None:
        self.workdir = workdir
        self.session_name = normalize_session_name(session_name)
        self.history_limit = history_limit
        self.specs = specs
        self.discover_agents = discover_agents
        self._host_model_factory = host_model_factory
        self._agent_enablement = agent_enablement
        self.context_policy = context_policy or ContextPolicy()
        self.default_host_backend = (
            default_host_backend
            or (
                store.default_host_backend
                if store is not None else HostBackendSelection.default()
            )
        ).validated()
        self._registered_names = tuple(
            dict.fromkeys((*[spec.name for spec in specs], HOST_NAME)))
        probes: dict[str, ReadinessProbe] = {
            spec.name: (
                spec.probe
                if discover_agents and spec.probe is not None
                else _ready_probe(spec.name)
            )
            for spec in specs
        }
        self._host_readiness_probe = (
            (host_probe or _HOST_READINESS_PROBE)
            if discover_agents else _ready_probe(HOST_NAME)
        )
        probes[HOST_NAME] = self._host_readiness_probe
        self._readiness = AgentReadinessRegistry(probes)
        readiness = {
            item.name: item for item in self._refresh_registered_readiness()
        }
        self.workspace_inspector = (
            workspace_inspector or GitWorkspaceInspector())
        self._permission_handler: AgentPermissionHandler | None = None
        self.adapters: dict[str, AgentAdapter] = {}
        for spec in specs:
            if not readiness[spec.name].ready:
                continue
            try:
                self.adapters[spec.name] = spec.factory()
            except Exception as exc:
                if not discover_agents:
                    raise
                self._readiness.mark_invalid(
                    spec.name,
                    f"adapter 初始化失败：{exc}",
                )
        # 持久化：persistent=True 默认按 workdir/session 打开 RoomStore；
        # 调用方也可注入自己的 store（必须属于同一房间，fail loudly）。
        # persistent=False 用于测试/一次性场景，不触碰任何磁盘状态。
        if not persistent and store is not None:
            raise ValueError("persistent=False 时不接受 store 参数")
        self.store: RoomStore | None = None
        if persistent:
            if store is None:
                store = RoomStore(
                    workdir,
                    session_name=self.session_name,
                    default_host_backend=self.default_host_backend,
                )
            elif store.workdir != normalize_workdir(workdir):
                raise WorkdirMismatchError(
                    f"store 属于 {store.workdir!r}，与 workdir {workdir!r} 不一致")
            elif store.session_name != self.session_name:
                raise WorkdirMismatchError(
                    f"store 属于会话 {store.session_name!r}，"
                    f"与请求会话 {self.session_name!r} 不一致")
            self.store = store
        self._host_backend = (
            self.store.get_host_backend()
            if self.store is not None
            else HostBackendSelection.default()
        )
        host_status = self._probe_host_backend(self._host_backend)
        self._readiness.set_status(host_status)
        host_adapter: AgentAdapter
        if host_status.ready:
            try:
                host_adapter = self._create_host_adapter(self._host_backend)
            except Exception as exc:
                host_status = AgentReadiness(
                    HOST_NAME,
                    ReadinessState.INVALID,
                    f"host backend 初始化失败：{exc}",
                    host_status.setup_hint,
                )
                self._readiness.set_status(host_status)
                host_adapter = _UnavailableHostAdapter(host_status.detail)
        else:
            host_adapter = _UnavailableHostAdapter(host_status.detail)
        # Host is always a separate instance, even when its backend target is
        # also registered as a worker (for example @codex).
        self.host = HostAgent(
            host_adapter,
            workers=[spec.name for spec in specs],
            backend_kind=self._host_backend.kind,
            fresh_replay_floor=self._host_fresh_replay_floor(),
        )
        self.adapters[HOST_NAME] = self.host
        (
            self._host_transport,
            self._host_resolved_target,
        ) = self._resolve_host_status_display(self._host_backend)
        self._session_roles: dict[str, SessionRole] = (
            self.store.get_session_roles()
            if self.store is not None else {}
        )
        self._attachment_root = (
            self.store.room_dir / "attachments"
            if self.store is not None else None
        )
        for name, adapter in self.adapters.items():
            if name == HOST_NAME:
                continue
            setter = getattr(adapter, "set_attachment_root", None)
            if setter is not None:
                setter(self._attachment_root)
        self.history: list[Message] = []
        # 无 store 时的内存 seq 分配起点；有 store 时 seq 来自 timeline。
        self._next_memory_seq = 1
        # 从 store 分页加载全部历史（read 单次上限 MAX_READ_LIMIT）。
        if self.store is not None:
            after_seq = 0
            while True:
                page = self.store.read(after_seq, limit=MAX_READ_LIMIT)
                for rec in page["items"]:
                    self.history.append(Message(
                        rec.speaker, rec.text, seq=rec.seq,
                        created_at=rec.created_at, command_id=rec.command_id))
                after_seq = page["next_after_seq"]
                if not page["has_more"]:
                    break
        # 每个有状态（ACP）agent 的 history cursor：不应自动重放到 timeline
        # 的哪个 seq（持久化序号，不是 list 下标）。只增不减；成功交付或
        # post-submit 结果不确定时推进，明确未提交的失败保持旧值。
        # 有 store 时从 state.json 恢复；cursor 超出当前 timeline 最大 seq
        # 说明状态与历史脱节，fail loudly——静默跳过会丢上下文。
        self._cursors: dict[str, int] = {}
        max_seq = self.history[-1].seq if self.history else 0
        for name, adapter in self.adapters.items():
            if not getattr(adapter, "stateful_session", False):
                continue
            cursor = 0
            if self.store is not None:
                cursor = self.store.get_agent_state(name)["cursor"]
                if cursor > max_seq:
                    raise CorruptedStorageError(
                        f"agent {name!r} 的持久化 cursor={cursor} 超出当前 "
                        f"timeline 最大 seq={max_seq}")
            self._cursors[name] = cursor
        if self.store is not None:
            boundary = self._host_fresh_replay_floor()
            host_cursor = self.store.get_agent_state(HOST_NAME)["cursor"]
            if boundary > max_seq or boundary > host_cursor:
                raise CorruptedStorageError(
                    "host backend replay boundary 超出 timeline/cursor")
            # An unavailable Host uses a stateless fail-closed placeholder, but
            # its durable cursor remains valid and must survive until a later
            # readiness rescan can construct the selected backend.
            self._cursors[HOST_NAME] = host_cursor
        self._context_checkpoints: dict[str, ContextCheckpoint] = (
            self.store.get_context_checkpoints()
            if self.store is not None else {}
        )
        for name, checkpoint in self._context_checkpoints.items():
            if name not in self._registered_names:
                raise CorruptedStorageError(
                    f"未知 agent {name!r} 存在 context checkpoint")
            cursor = self._cursors.get(name)
            if cursor is None:
                raise CorruptedStorageError(
                    f"无状态 agent {name!r} 不能持有 context checkpoint")
            if checkpoint.boundary_seq > cursor or checkpoint.boundary_seq > max_seq:
                raise CorruptedStorageError(
                    f"agent {name!r} 的 context checkpoint boundary="
                    f"{checkpoint.boundary_seq} 超出 cursor/timeline")
        # 单写者 lease：只读加载/校验全部通过后、返回给调用方前获取；
        # 之后本房间的所有写入都受 lease 保护。获取失败（RoomBusyError）
        # 时构造失败，此时尚无 lease 需要释放。
        self._lease: RoomLease | None = None
        if self.store is not None:
            self._lease = self.store.acquire_owner()
        # 每-agent delivery lock：读 cursor → 选增量 → stream 完整执行 →
        # 推进 cursor 是一个原子单元。没有它，两个针对同一 agent 的并发
        # dispatch 会都按旧 cursor 构造 prompt，重复/乱序投递。
        self._delivery_locks: dict[str, asyncio.Lock] = {}
        self._active_workflows: dict[str, MilestoneWorkflow] = {}
        self._active_deliveries: dict[
            str, dict[object, tuple[str, AgentAdapter]]
        ] = {}
        self._active_dispatches = 0
        # A timed-out/failed Host close may leave the old runtime alive. Keep
        # the room fail-closed until restart; a passive rescan cannot prove the
        # process was reaped and therefore must not revive readiness.
        self._host_close_failure: str | None = None
        # 关闭标志：aclose() 原子置位；dispatch 与排队中的 _run_one 据此
        # 拒绝新工作，closed 后不再写 timeline、不再 start/load/prompt。
        self._closed = False

    def _append_message(self, speaker: str, text: str,
                        command_id: str | None = None) -> Message:
        """往 history 追加一条消息的唯一入口（含持久化）。

        有 store：先落盘（append 内部 flush+fsync），成功后才进 history；
        落盘失败抛异常，history 和 seq 都不动。无 store：分配单调内存 seq。
        """
        if self.store is not None:
            record = self.store.append(speaker, text, command_id=command_id)
            message = Message(speaker, text, seq=record.seq,
                              created_at=record.created_at,
                              command_id=record.command_id)
        else:
            message = Message(speaker, text, seq=self._next_memory_seq,
                              command_id=command_id)
            self._next_memory_seq += 1
        self.history.append(message)
        return message

    # ---- 注册信息 / 生命周期 ----

    @property
    def session_roles(self) -> dict[str, SessionRole]:
        """当前房间角色快照；调用方不能修改内部状态。"""
        return dict(self._session_roles)

    def agent_readiness_snapshot(self) -> tuple[AgentReadiness, ...]:
        """返回注册顺序稳定的本机就绪状态快照。"""
        return self._readiness.snapshot()

    @property
    def agent_enablement(self) -> AgentEnablementConfig | None:
        return self._agent_enablement

    def _refresh_registered_readiness(self) -> tuple[AgentReadiness, ...]:
        disabled: frozenset[str] = frozenset()
        config_error: AgentEnablementError | None = None
        if self._agent_enablement is not None:
            try:
                disabled = self._agent_enablement.disabled_names(
                    spec.name for spec in self.specs)
            except AgentEnablementError as exc:
                config_error = exc
        statuses = self._readiness.refresh(disabled_names=disabled)
        if config_error is not None:
            for spec in self.specs:
                self._readiness.mark_invalid(
                    spec.name,
                    f"全局 Agent 开关配置无效：{config_error}",
                    setup_hint=(
                        f"修复 {self._agent_enablement.path} 后执行 "
                        "/agents rescan"
                    ),
                )
            statuses = self._readiness.snapshot()
        return statuses

    def set_agent_enabled(
        self,
        name: str,
        *,
        enabled: bool,
        write_config: bool = True,
    ) -> tuple[AgentReadiness, ...]:
        if name not in {spec.name for spec in self.specs}:
            raise AgentEnablementError(
                f"未知 worker agent：{name!r}；请从 /agents 中选择")
        if self._agent_enablement is None:
            raise AgentEnablementError("当前运行未启用全局 Agent 开关配置")
        if write_config:
            self._agent_enablement.set_enabled(name, enabled)
        return self.refresh_agent_readiness()

    @property
    def host_readiness_probe(self) -> ReadinessProbe:
        """供同一 TUI 创建的新 room 复用完全相同的 host 探测契约。"""
        return self._host_readiness_probe

    @property
    def host_model_factory(
        self,
    ) -> Callable[[HostBackendSelection], AgentAdapter] | None:
        """Propagate an injected model factory to sibling room runtimes."""
        return self._host_model_factory

    @property
    def host_backend_selection(self) -> HostBackendSelection:
        return self._host_backend

    def host_backend_status(self) -> HostBackendStatus:
        """返回内存快照；状态栏刷新不得重新读取模型配置文件。"""
        readiness = next(
            item for item in self._readiness.snapshot()
            if item.name == HOST_NAME)
        return HostBackendStatus(
            self._host_backend,
            readiness,
            self._host_transport,
            self._host_resolved_target,
        )

    def _resolve_host_status_display(
        self, selection: HostBackendSelection
    ) -> tuple[str, str]:
        """只在初始化、显式切换或 rescan 边界解析可见 Host 身份。"""
        resolved = selection.target
        if selection.kind == "agent":
            transport = "HOST-AGENT/UNAVAILABLE"
            spec = self._host_agent_spec(selection.target)
            if spec is not None:
                try:
                    capability = spec.agent_host_capability()
                except Exception:
                    capability = None
                if capability is not None:
                    transport = f"HOST-AGENT/{capability.transport.upper()}"
        else:
            transport = "NATIVE-MODEL"
            try:
                config = resolve_native_model_config(
                    selection.target,
                    reference=selection.reference or "profile",
                )
                resolved = config.model_id
            except NativeModelConfigurationError:
                pass
        return transport, resolved

    def _host_agent_spec(self, name: str) -> AgentSpec | None:
        return next((spec for spec in self.specs if spec.name == name), None)

    def _probe_host_backend(
        self,
        selection: HostBackendSelection,
    ) -> AgentReadiness:
        if selection.kind == "model":
            if not self.discover_agents:
                return self._host_readiness_probe()
            if selection.reference == "model":
                return native_host_readiness_probe(
                    exact_model_id=selection.target)
            if selection.target == "default" \
                    and self._host_readiness_probe is not _HOST_READINESS_PROBE:
                return self._host_readiness_probe()
            return native_host_readiness_probe(profile=selection.target)
        spec = self._host_agent_spec(selection.target)
        try:
            capability = (
                spec.agent_host_capability() if spec is not None else None
            )
        except Exception as exc:
            return AgentReadiness(
                HOST_NAME,
                ReadinessState.INVALID,
                f"agent host {selection.target} capability 无效：{exc}",
                "修复该 adapter 的 host_capability 声明后执行 /agents rescan",
            )
        if spec is None or capability is None:
            return AgentReadiness(
                HOST_NAME,
                ReadinessState.INVALID,
                f"agent {selection.target!r} 未声明 host-safe read-only capability",
                "使用 /host agent <支持的 agent> 或 /host model <profile>",
            )
        worker_status = next(
            (
                item for item in self._readiness.snapshot()
                if item.name == selection.target
            ),
            None,
        )
        if worker_status is not None and not worker_status.ready:
            return AgentReadiness(
                HOST_NAME,
                worker_status.state,
                f"agent host {selection.target}：{worker_status.detail}",
                worker_status.setup_hint,
                worker_status.executable,
            )
        probe = spec.probe
        if not self.discover_agents or probe is None:
            return AgentReadiness(
                HOST_NAME,
                ReadinessState.READY,
                f"agent host {selection.target} 已注册独立只读实例",
                "无需本机 CLI 探测",
            )
        try:
            source = probe()
        except Exception as exc:
            return AgentReadiness(
                HOST_NAME,
                ReadinessState.INVALID,
                f"agent host {selection.target} 探测失败：{exc}",
                "执行 /agents rescan",
            )
        return AgentReadiness(
            HOST_NAME,
            source.state,
            f"agent host {selection.target}：{source.detail}",
            source.setup_hint,
            source.executable,
        )

    def _create_host_adapter(
        self,
        selection: HostBackendSelection,
    ) -> AgentAdapter:
        if selection.kind == "model":
            if self._host_model_factory is not None:
                return self._host_model_factory(selection)
            return create_native_host_runtime(
                target=selection.target,
                reference=selection.reference or "profile",
            )
        spec = self._host_agent_spec(selection.target)
        capability = (
            spec.agent_host_capability() if spec is not None else None
        )
        if spec is None or capability is None:
            raise HostBackendValidationError(
                f"agent {selection.target!r} 不能作为 host")
        adapter = capability.factory()
        if getattr(adapter, "name", None) != spec.name:
            raise HostBackendValidationError(
                f"agent {selection.target!r} 的 host factory 返回身份不匹配 adapter")
        return adapter

    def _host_fresh_replay_floor(self) -> int:
        """Return the durable no-replay floor for fresh Host sessions."""
        if self.store is None:
            return 0
        return self.store.get_host_replay_floor()

    def _create_host(
        self,
        selection: HostBackendSelection,
        *,
        fresh_replay_floor: int,
    ) -> HostAgent:
        return HostAgent(
            self._create_host_adapter(selection),
            workers=[spec.name for spec in self.specs],
            backend_kind=selection.kind,
            fresh_replay_floor=fresh_replay_floor,
        )

    def _selection_for_model_target(
        self,
        target: str,
    ) -> HostBackendSelection:
        if target in native_model_profile_names():
            return HostBackendSelection.model_profile(target)
        if target == "default":
            return HostBackendSelection.model_profile(target)
        return HostBackendSelection.exact_model(target)

    async def switch_host_backend(
        self,
        kind: str,
        target: str,
    ) -> HostBackendStatus:
        """Atomically switch this room to a fresh, independently owned host."""
        if self._closed:
            raise OrchestratorClosedError("Orchestrator 已关闭，拒绝切换 host")
        if self._host_close_failure is not None:
            raise RuntimeError(
                "旧 host runtime 未确认关闭；为避免双 writer，"
                "请重启当前会话后再切换")
        if self._active_dispatches:
            raise RuntimeError("当前仍有运行或已进入派发的任务；结束后再切换 host")
        selection = (
            self._selection_for_model_target(target)
            if kind == "model"
            else HostBackendSelection.agent(target)
        )
        status = self._probe_host_backend(selection)
        if not status.ready:
            raise AgentUnavailableError([status], purpose="切换 host")
        lock = self._delivery_lock(HOST_NAME)
        if lock.locked():
            raise RuntimeError("host 正在运行；请在当前阶段结束后再切换")
        boundary = self.history[-1].seq if self.history else 0
        candidate = self._create_host(
            selection, fresh_replay_floor=boundary)

        await lock.acquire()
        old_host = self.host
        old_selection = self._host_backend
        try:
            try:
                await _close_host_bounded(old_host)
            except BaseException as exc:
                await _close_host_quietly(candidate)
                self._host_close_failure = str(exc)
                old_status = self._probe_host_backend(old_selection)
                self._readiness.set_status(AgentReadiness(
                    HOST_NAME,
                    ReadinessState.INVALID,
                    f"旧 host runtime 关闭失败：{exc}",
                    old_status.setup_hint,
                    old_status.executable,
                ))
                # Keep the original handle so final room shutdown can retry its
                # bounded close. Publishing a replacement here could create a
                # second writer while the old runtime is still alive.
                raise
            try:
                if self.store is not None:
                    self.store.set_host_backend(selection, cursor=boundary)
            except BaseException:
                await _close_host_quietly(candidate)
                replacement = self._create_host(
                    old_selection,
                    fresh_replay_floor=self._host_fresh_replay_floor(),
                )
                self.host = replacement
                self.adapters[HOST_NAME] = replacement
                raise
            self._host_backend = selection
            (
                self._host_transport,
                self._host_resolved_target,
            ) = self._resolve_host_status_display(selection)
            self.host = candidate
            self.adapters[HOST_NAME] = candidate
            self._cursors[HOST_NAME] = boundary
            self._context_checkpoints.pop(HOST_NAME, None)
            self._readiness.set_status(status)
        finally:
            lock.release()
        return self.host_backend_status()

    def context_status_snapshot(self) -> tuple[ContextStatus, ...]:
        """Describe context ownership honestly for every registered target."""
        statuses: list[ContextStatus] = []
        ordered_names = (HOST_NAME, *[spec.name for spec in self.specs])
        readiness = {
            item.name: item for item in self.agent_readiness_snapshot()
        }
        for name in ordered_names:
            adapter = self.adapters.get(name)
            checkpoint = self._context_checkpoints.get(name)
            if adapter is None:
                ready = readiness.get(name)
                detail = (
                    ready.detail if ready is not None
                    else "adapter 尚未构造"
                )
                statuses.append(ContextStatus(
                    name=name,
                    strategy="unavailable",
                    compactable=False,
                    state="不可用",
                    detail=detail,
                    checkpoint_generation=(
                        checkpoint.generation if checkpoint else None),
                    checkpoint_boundary=(
                        checkpoint.boundary_seq if checkpoint else None),
                ))
                continue
            inspect = getattr(adapter, "context_snapshot", None)
            if callable(inspect):
                try:
                    snapshot = inspect()
                except (AttributeError, ContextLifecycleError):
                    snapshot = None
                except Exception:
                    statuses.append(ContextStatus(
                        name=name,
                        strategy="capability-error",
                        compactable=False,
                        state="状态异常",
                        detail="adapter 上下文状态读取失败；已停止自动压缩",
                        checkpoint_generation=(
                            checkpoint.generation if checkpoint else None),
                        checkpoint_boundary=(
                            checkpoint.boundary_seq if checkpoint else None),
                    ))
                    continue
                if isinstance(snapshot, AdapterContextSnapshot):
                    detail = (
                        "myagents 可在安全回合边界生成摘要并保留近期原文"
                        if snapshot.compactable
                        else "adapter 仅公开状态，未声明安全压缩"
                    )
                    statuses.append(ContextStatus(
                        name=name,
                        strategy=snapshot.strategy,
                        compactable=snapshot.compactable,
                        state=("可压缩" if snapshot.compactable else "只读状态"),
                        detail=detail,
                        message_count=snapshot.message_count,
                        character_count=snapshot.character_count,
                        checkpoint_generation=(
                            checkpoint.generation if checkpoint else None),
                        checkpoint_boundary=(
                            checkpoint.boundary_seq if checkpoint else None),
                    ))
                    continue
            if getattr(adapter, "stateful_session", False):
                statuses.append(ContextStatus(
                    name=name,
                    strategy="transport-managed",
                    compactable=False,
                    state="由 transport 管理",
                    detail=(
                        "保留原生 session 增量上下文；当前没有已验收的安全压缩能力"
                    ),
                    checkpoint_generation=(
                        checkpoint.generation if checkpoint else None),
                    checkpoint_boundary=(
                        checkpoint.boundary_seq if checkpoint else None),
                ))
            else:
                statuses.append(ContextStatus(
                    name=name,
                    strategy="bounded-snapshot",
                    compactable=False,
                    state="无长期 session",
                    detail=f"每轮只发送最近 {self.history_limit} 条共享记录",
                ))
        return tuple(statuses)

    async def compact_context(
        self,
        name: str = HOST_NAME,
        *,
        on_event: EventCallback | None = None,
    ) -> ContextCompactionResult:
        """Manually compact one capability-bearing adapter at an idle boundary."""
        if self._closed:
            raise OrchestratorClosedError("Orchestrator 已关闭，拒绝压缩上下文")
        if name not in self._registered_names:
            raise ContextLifecycleError(f"未知 agent：@{name}")
        if self._active_dispatches:
            raise ContextLifecycleError(
                "当前有任务正在派发；请在回合或 workflow 阶段结束后压缩")
        adapter = self.adapters.get(name)
        if adapter is None:
            raise ContextLifecycleError(f"@{name} 当前不可用，无法压缩上下文")
        lock = self._delivery_lock(name)
        if lock.locked():
            raise ContextLifecycleError(
                f"@{name} 正在运行；请在当前回合结束后压缩")
        async with lock:
            # HostBackend/readiness may have changed while the caller waited.
            adapter = self.adapters.get(name)
            if adapter is None:
                raise ContextLifecycleError(
                    f"@{name} 当前不可用，无法压缩上下文")
            return await self._compact_context_locked(
                name, adapter, on_event=on_event, automatic=False)

    async def _compact_context_locked(
        self,
        name: str,
        adapter: AgentAdapter,
        *,
        on_event: EventCallback | None,
        automatic: bool,
    ) -> ContextCompactionResult:
        inspect = getattr(adapter, "context_snapshot", None)
        compact = getattr(adapter, "compact_context", None)
        if not callable(inspect) or not callable(compact):
            raise ContextLifecycleError(
                f"@{name} 的 transport 未声明已验收的安全压缩能力")
        try:
            snapshot = inspect()
        except AttributeError as exc:
            raise ContextLifecycleError(
                f"@{name} 的当前 backend 不支持安全压缩") from exc
        except ContextLifecycleError:
            raise
        except Exception as exc:
            raise ContextLifecycleError(
                f"@{name} 上下文状态读取失败；未执行压缩") from exc
        if (
            not isinstance(snapshot, AdapterContextSnapshot)
            or not snapshot.compactable
        ):
            raise ContextLifecycleError(
                f"@{name} 的当前 backend 不支持安全压缩")
        if automatic and on_event is not None:
            self._emit_adapter_event(
                on_event,
                name,
                AgentEvent("status", "上下文接近预算，正在安全压缩…"),
            )
        try:
            result = await compact(self.context_policy)
        except asyncio.CancelledError:
            raise
        except ContextLifecycleError:
            raise
        except Exception as exc:
            raise ContextLifecycleError(
                f"@{name} 上下文压缩失败；未暴露 adapter 内部细节") from exc
        if not isinstance(result, ContextCompactionResult):
            raise ContextLifecycleError(
                f"@{name} 返回了无效的上下文压缩结果")
        if not result.changed:
            return result
        previous = self._context_checkpoints.get(name)
        checkpoint = ContextCheckpoint.create(
            boundary_seq=self._cursors.get(name, 0),
            summary=result.summary,
            generation=(previous.generation + 1 if previous else 1),
            source_messages=result.source_messages,
            retained_messages=result.retained_messages,
        )
        try:
            if self.store is not None:
                self.store.set_context_checkpoint(name, checkpoint)
        except Exception as exc:
            # The live runtime has already compacted while durable recovery did
            # not.  Continuing could lose or duplicate context after restart.
            self._closed = True
            raise ContextLifecycleError(
                "上下文已在 runtime 内压缩，但 checkpoint 持久化失败；"
                "当前会话已 fail-closed，请重启后检查存储"
            ) from exc
        self._context_checkpoints[name] = checkpoint
        if automatic and on_event is not None:
            self._emit_adapter_event(
                on_event,
                name,
                AgentEvent(
                    "info",
                    "上下文已自动压缩",
                    {
                        "before_characters": result.before_characters,
                        "after_characters": result.after_characters,
                        "checkpoint_generation": checkpoint.generation,
                    },
                ),
            )
        return result

    async def _maybe_auto_compact_locked(
        self,
        name: str,
        adapter: AgentAdapter,
        on_event: EventCallback,
    ) -> None:
        if not self.context_policy.auto_compact:
            return
        inspect = getattr(adapter, "context_snapshot", None)
        if not callable(inspect):
            return
        try:
            snapshot = inspect()
        except (AttributeError, ContextLifecycleError):
            return
        except Exception as exc:
            raise ContextLifecycleError(
                f"@{name} 上下文状态读取失败；已停止本轮派发") from exc
        if (
            not isinstance(snapshot, AdapterContextSnapshot)
            or not snapshot.compactable
            or snapshot.character_count < self.context_policy.trigger_characters
        ):
            return
        await self._compact_context_locked(
            name, adapter, on_event=on_event, automatic=True)

    def ready_worker_names(self) -> list[str]:
        """返回可安全派发且 adapter 已构造的 worker，保持注册顺序。"""
        ready = {
            item.name for item in self._readiness.snapshot()
            if item.ready
        }
        return [
            spec.name for spec in self.specs
            if spec.name in ready and spec.name in self.adapters
        ]

    def refresh_agent_readiness(self) -> tuple[AgentReadiness, ...]:
        """重新执行被动探测，并为新就绪的 agent 惰性构造 adapter。"""
        if self._closed:
            raise OrchestratorClosedError("Orchestrator 已关闭，拒绝重新探测")
        statuses = self._refresh_registered_readiness()
        by_name = {item.name: item for item in statuses}
        for spec in self.specs:
            if not by_name[spec.name].ready or spec.name in self.adapters:
                continue
            try:
                adapter = spec.factory()
                cursor: int | None = None
                if getattr(adapter, "stateful_session", False):
                    cursor = 0
                    if self.store is not None:
                        cursor = self.store.get_agent_state(
                            spec.name)["cursor"]
                        max_seq = self.history[-1].seq if self.history else 0
                        if cursor > max_seq:
                            raise CorruptedStorageError(
                                f"agent {spec.name!r} 的持久化 cursor={cursor} "
                                f"超出当前 timeline 最大 seq={max_seq}")
                permission_setter = getattr(
                    adapter, "set_permission_handler", None)
                if permission_setter is not None:
                    permission_setter(self._permission_handler)
                attachment_setter = getattr(
                    adapter, "set_attachment_root", None)
                if attachment_setter is not None:
                    attachment_setter(self._attachment_root)
            except CorruptedStorageError as exc:
                self._readiness.mark_invalid(
                    spec.name,
                    f"会话状态恢复失败：{exc}",
                    setup_hint="检查当前会话状态或新建会话",
                )
                raise
            except Exception as exc:
                self._readiness.mark_invalid(
                    spec.name,
                    f"adapter 初始化失败：{exc}",
                )
                continue
            # adapter 构造、注入和 cursor 校验全部成功后才公开；这些构造器
            # 按项目契约是惰性的，此前不会启动真实 agent 进程。
            self.adapters[spec.name] = adapter
            if cursor is not None:
                self._cursors[spec.name] = cursor
        host_status = self._probe_host_backend(self._host_backend)
        if self._host_close_failure is not None:
            host_status = AgentReadiness(
                HOST_NAME,
                ReadinessState.INVALID,
                f"旧 host runtime 关闭失败：{self._host_close_failure}",
                "重启当前会话，确认旧 runtime 已回收后再试",
            )
        self._readiness.set_status(host_status)
        (
            self._host_transport,
            self._host_resolved_target,
        ) = self._resolve_host_status_display(self._host_backend)
        if host_status.ready and isinstance(
                self.host.adapter, _UnavailableHostAdapter):
            try:
                host = self._create_host(
                    self._host_backend,
                    fresh_replay_floor=self._host_fresh_replay_floor(),
                )
                self.host = host
                self.adapters[HOST_NAME] = host
                if host.stateful_session:
                    cursor = 0
                    if self.store is not None:
                        cursor = self.store.get_agent_state(HOST_NAME)["cursor"]
                    self._cursors[HOST_NAME] = cursor
            except Exception as exc:
                self._readiness.mark_invalid(
                    HOST_NAME, f"host backend 初始化失败：{exc}")
        return self._readiness.snapshot()

    def require_message_agents(self, text: str) -> None:
        """在任何 timeline/workspace 副作用前原子校验本轮所需 agent。"""
        if parse_steer_instruction(text) is not None:
            return
        workers = tuple(spec.name for spec in self.specs)
        workflow_request = parse_workflow_request(
            text, workers, host_name=HOST_NAME)
        if workflow_request is not None:
            self._readiness.require(
                (*workflow_request.roles.values(), HOST_NAME),
                purpose="workflow",
            )
            return
        discussion_request = parse_discussion_request(
            text, workers, host_name=HOST_NAME)
        if discussion_request is not None:
            self._readiness.require(
                (*discussion_request.participants,
                 discussion_request.moderator),
                purpose="讨论",
            )
            return
        targets = self.parse_mentions(text)
        if self._is_explicit_collaboration_candidate(text, targets):
            if len(targets) > MAX_COLLABORATION_STEPS:
                raise CollaborationValidationError(
                    f"有序协作最多点名 {MAX_COLLABORATION_STEPS} 个 agent")
            self._readiness.require(
                (*targets, HOST_NAME),
                purpose="有序协作",
            )
            return
        natural_discussion = parse_natural_discussion_request(text, targets)
        if natural_discussion is not None:
            self._readiness.require(
                (*natural_discussion.participants,
                 natural_discussion.moderator),
                purpose="讨论",
            )
            return
        self._readiness.require(
            targets or [HOST_NAME],
            purpose="任务",
        )

    def clear_session_roles(self) -> tuple[str, ...]:
        """原子清空当前房间角色，返回实际清除的 agent 名。"""
        cleared = tuple(self._session_roles)
        if not cleared:
            return ()
        self._apply_session_role_changes(SessionRoleChanges({}, cleared))
        return cleared

    def _apply_session_role_changes(
        self,
        changes: SessionRoleChanges,
    ) -> None:
        if changes.is_empty:
            return
        updated = apply_session_role_changes(self._session_roles, changes)
        if self.store is not None:
            self.store.set_session_roles(updated)
        self._session_roles = updated

    async def _extract_session_role_changes(
        self,
        text: str,
        targets: list[str],
        on_event: EventCallback,
        *,
        host_will_continue: bool = False,
        command_id: str | None = None,
        max_seq: int,
    ) -> SessionRoleChanges:
        if not targets or not has_session_role_cue(text):
            return SessionRoleChanges.empty()
        on_event(HOST_NAME, AgentEvent(
            "status",
            "正在识别会话角色",
            {"agent_state": "running", "phase": "角色识别"},
        ))
        try:
            if getattr(self.host, "prepared_semantic_session", False):
                raw = await self._run_host_semantic(
                    lambda _transcript: self.host.build_session_roles_prompt(
                        text, targets),
                    on_event,
                    command_id,
                    max_seq=max_seq,
                )
                changes = self.host.parse_session_roles(raw, targets)
            else:
                changes = await self.host.extract_session_roles(
                    text,
                    targets,
                    self.workdir,
                    lambda event: self._emit_adapter_event(
                        on_event, HOST_NAME, event),
                )
        except _EventCallbackError as exc:
            raise exc.cause from exc
        except Exception as exc:
            on_event(HOST_NAME, AgentEvent(
                "info",
                f"会话角色识别失败，原任务继续：{exc}",
                {"phase": "角色识别失败"},
            ))
            changes = SessionRoleChanges.empty()
        if not host_will_continue:
            on_event(HOST_NAME, AgentEvent(
                "done", meta={"roleExtraction": True}))
        return changes

    def _session_role_meta(self, name: str) -> dict[str, str]:
        role = self._session_roles.get(name)
        return {"session_role": role.label} if role is not None else {}

    def set_permission_handler(self, handler: AgentPermissionHandler | None) -> None:
        """把 TUI 的权限决策器注入所有支持它的 adapter（ACP）。
        handler 签名：async (agent_name, params) -> outcome。
        未注入时 ACP 一律 deny——这是安全契约，不是默认值偷懒。"""
        self._permission_handler = handler
        for adapter in self.adapters.values():
            setter = getattr(adapter, "set_permission_handler", None)
            if setter is not None:
                setter(handler)

    async def aclose(self) -> None:
        """统一关闭所有支持 aclose() 的 adapter（ACP 长驻进程在此回收）。
        adapter 内部用同一把锁保证 close 不与进行中的 prompt 竞态。

        幂等：先原子置 closed——之后的新 dispatch 直接拒绝，排队等
        delivery lock 的 _run_one 拿到锁后也会看到并放弃，绝不
        start/load/prompt agent。无论 adapter close 结果如何，
        最后都释放房间单写者 lease。"""
        self._closed = True
        try:
            await asyncio.gather(
                *(adapter.aclose() for adapter in self.adapters.values()
                  if hasattr(adapter, "aclose")),
                return_exceptions=True,  # 一个关不掉不耽误其他的回收
            )
        finally:
            if self._lease is not None:
                self._lease.release()
                self._lease = None

    # ---- 路由 ----

    def parse_mentions(self, text: str) -> list[str]:
        """从消息里提取 @agent，按出现顺序去重，忽略未注册的名字。"""
        seen: list[str] = []
        for name in _MENTION_RE.findall(text):
            if name in self._registered_names and name not in seen:
                seen.append(name)
        return seen

    @staticmethod
    def _is_explicit_collaboration_candidate(
        text: str,
        targets: list[str],
    ) -> bool:
        """Only workers may enter ordered collaboration; host stays a router."""
        return HOST_NAME not in targets and has_ordered_collaboration_cue(
            text, targets)

    # ---- 上下文组装 ----

    def _delivery_lock(self, name: str) -> asyncio.Lock:
        lock = self._delivery_locks.get(name)
        if lock is None:
            lock = self._delivery_locks[name] = asyncio.Lock()
        return lock

    @staticmethod
    def _format(messages: list[Message]) -> str:
        if not messages:
            return "（暂无记录）"
        return "\n".join(f"[{m.speaker}] {m.text[:2000]}" for m in messages)

    def _messages_for(
        self,
        name: str,
        *,
        max_seq: int | None = None,
    ) -> tuple[list[Message], int]:
        """有状态 agent 本轮的增量消息 + 交付目标 seq（快照末尾的 seq）。

        必须在 delivery lock 内调用：cursor 读取、增量选择、推进是一个
        原子单元，锁外读到的 cursor 可能已被并发轮次推进。

        cursor 是持久化的 timeline seq，不是 list 下标；``max_seq`` 为 host
        语义调用冻结本次 dispatch 的消息上界，避免等待单写锁时串入后续消息：
        - cursor>0：选 seq 严格大于 cursor 的新消息，跳过 agent 自己的
          回复（那些已经在它的 ACP session 里，重发就是重复上下文）。
        - cursor=0（bootstrap）：不发全部历史，只发最近 history_limit
          条，避免长聊天后第一次 @ 就无界发送。
        """
        snapshot = [
            message for message in self.history
            if max_seq is None or message.seq <= max_seq
        ]
        snapshot_end_seq = snapshot[-1].seq if snapshot else 0
        cursor = self._cursors.get(name, 0)
        if cursor > 0:
            msgs = [m for m in snapshot
                    if m.seq > cursor and m.speaker != name]
        else:
            msgs = [m for m in snapshot[-self.history_limit:]
                    if m.speaker != name]
        if not msgs and cursor == 0:
            # 没有新消息也要给出触发点（快照窗口里可能只剩自己的发言）
            msgs = [m for m in snapshot if m.speaker != name][-1:]
        return msgs, snapshot_end_seq

    def _build_prompt(
            self, agent_name: str, messages: list[Message],
            assignment: str | None = None) -> str:
        transcript = self._format(messages)
        if agent_name == HOST_NAME:
            assignment_block = ""
            if assignment:
                assignment_block = (
                    "主持人本轮明确任务：\n"
                    f"{assignment.strip()[:12000]}\n"
                )
            return MODERATOR_TEMPLATE.format(
                name=agent_name,
                transcript=transcript,
                assignment=assignment_block,
            )
        elif getattr(self.adapters[agent_name], "stateful_session", False):
            template = _ACP_PROMPT_TEMPLATE
        else:
            template = _PROMPT_TEMPLATE
        assignment_block = ""
        if assignment:
            assignment_block = (
                "主持人本轮明确委托：\n"
                f"{assignment.strip()[:12000]}\n"
                "请直接执行完整任务；除非存在无法自行解决的真实阻塞，"
                "不要把任务退回给主持人或用户，也不要只给候选方案等待确认。\n"
            )
        return template.format(
            name=agent_name,
            transcript=transcript,
            assignment=assignment_block,
        )

    @staticmethod
    def _fallback_assignment(user_text: str) -> str:
        """host 旧格式/畸形 tasks 的安全退化：仍给 worker 明确执行语义。"""
        return (
            "直接完成用户最新请求。若请求同时包含构思、规划、实现或验证，"
            "这些步骤都由你完成，不要等待主持人另发方案。\n"
            f"用户原始请求：{user_text[:3000]}"
        )

    # ---- 派发 ----

    async def dispatch(self, user_text: str, on_event: EventCallback,
                       command_id: str | None = None) -> DispatchOutcome:
        """Gate HostBackend switching across the whole accepted dispatch."""
        if self._closed:
            raise OrchestratorClosedError("Orchestrator 已关闭，拒绝 dispatch")
        self._active_dispatches += 1
        try:
            return await self._dispatch(user_text, on_event, command_id)
        finally:
            self._active_dispatches -= 1

    async def _dispatch(self, user_text: str, on_event: EventCallback,
                        command_id: str | None = None) -> DispatchOutcome:
        """处理一条用户消息：记录 → 路由/计划 → 派发 → 收回回复。

        路由规则：显式 @ 普通情况下直接 fan-out；明确先后关系时，host 只在
        mention 闭集内提取计划。用户没点名时交给 host 一次处理（直接回答、
        并行路由或返回有序协作计划）。

        command_id：调用方（CommandBus）的命令 id；本轮 user 消息和所有
        agent 最终 timeline 记录（正常回复、失败占位、无文本占位）都带它，
        并随 committed 事件的 meta 一起下发。

        持久确认：用户消息落盘（或分配内存 seq）成功后才回调
        `("user", AgentEvent("committed", text,
        meta={"seq": ..., "command_id": ...}))`——
        UI 据此再显示用户文本，append 失败时什么都不显示。
        closed 后拒绝 dispatch，不写 timeline。

        返回所有 target 收尾后的 DispatchOutcome；单个 worker 失败不取消
        其他 target，由调用方据 failures 决定 command 终态。
        """
        if self._closed:
            raise OrchestratorClosedError("Orchestrator 已关闭，拒绝 dispatch")
        self.require_message_agents(user_text)
        workflow_request = parse_workflow_request(
            user_text,
            tuple(spec.name for spec in self.specs),
            host_name=HOST_NAME,
        )
        if workflow_request is not None:
            baseline = await self.workspace_inspector.capture_baseline(
                self.workdir)
            return await self._dispatch_workflow(
                workflow_request,
                baseline,
                user_text,
                on_event,
                command_id,
            )
        discussion = parse_discussion_request(
            user_text, (spec.name for spec in self.specs),
            host_name=HOST_NAME,
        )
        if discussion is not None:
            return await self._dispatch_discussion(
                discussion, user_text, on_event, command_id)
        targets = self.parse_mentions(user_text)
        image_routed_from_host = False
        if HOST_NAME in targets and prompt_images(
                user_text, self._attachment_root):
            worker_targets = [name for name in targets if name != HOST_NAME]
            if not worker_targets:
                worker_targets = self.ready_worker_names()[:1]
            if not worker_targets:
                raise RuntimeError(
                    "host 没有图片能力，且当前没有可用 worker 可接收附件")
            self._readiness.require(worker_targets, purpose="图片任务")
            targets = worker_targets
            image_routed_from_host = True
        explicit_collaboration = self._is_explicit_collaboration_candidate(
            user_text, targets)
        natural_discussion = (
            None
            if explicit_collaboration
            else parse_natural_discussion_request(
                user_text,
                targets,
                host_name=HOST_NAME,
            )
        )
        if natural_discussion is not None:
            return await self._dispatch_discussion(
                natural_discussion,
                user_text,
                on_event,
                command_id,
                recognized_naturally=True,
            )
        committed = self._append_message("user", user_text,
                                         command_id=command_id)
        on_event("user", AgentEvent("committed", user_text,
                                    meta={"seq": committed.seq,
                                          "command_id": command_id}))
        if image_routed_from_host:
            on_event(HOST_NAME, AgentEvent(
                "info",
                f"图片路由 → {'、'.join(targets)}",
                meta={
                    "route_targets": list(targets),
                    "phase": "图片路由完成",
                },
            ))
        # 快照（在第一个 await 之前）：等待 host 处理或 agent 启动期间，
        # 用户可能又提交了新消息进 history。JSONL agent 和 host 路由的
        # prompt 必须用快照拼接，否则并发消息会串话（agent 执行错任务）。
        # worker 的有状态 adapter 不走这个快照——它的增量起点（cursor）在
        # delivery lock 内读取。原生 host 仍以 committed.seq 为上界，防止
        # 等待 host 单写锁时混入稍后提交的并发消息。
        snapshot = list(self.history)
        snapshot_text = self._format(snapshot[-self.history_limit:])
        assignments: dict[str, str] = {}
        role_changes = SessionRoleChanges.empty()
        collaboration: CollaborationPlan | None = None
        routed_by_host = not targets
        if routed_by_host:
            # Host backend failure is terminal for this command. It must never
            # silently execute the same request through a different worker or
            # backend because the original model/agent may already have seen it.
            on_event(HOST_NAME, AgentEvent(
                "status",
                "正在路由",
                meta={"agent_state": "running", "phase": "路由"},
            ))
            try:
                ready_workers = self.ready_worker_names()
                if getattr(self.host, "prepared_semantic_session", False):
                    raw = await self._run_host_semantic(
                        lambda transcript: self.host.build_route_prompt(
                            transcript, ready_workers),
                        on_event,
                        command_id,
                        max_seq=committed.seq,
                    )
                    decision = self.host.parse_decision(raw, ready_workers)
                else:
                    decision = await self.host.decide(
                        snapshot_text,
                        self.workdir,
                        lambda event: self._emit_adapter_event(
                            on_event, HOST_NAME, event),
                        choices=ready_workers,
                    )
            except _EventCallbackError as exc:
                # host 进度事件与 worker 事件使用同一持久化失败契约：
                # sink 失败必须穿透，不能伪装成路由失败后继续派发。
                raise exc.cause from exc
            except (CollaborationValidationError,
                    DiscussionValidationError) as exc:
                on_event(HOST_NAME, AgentEvent(
                    "error", f"路由计划无效：{exc}",
                    meta={"phase": "路由计划无效"},
                ))
                return DispatchOutcome((AgentFailure(HOST_NAME, str(exc)),))
            except Exception as exc:
                on_event(HOST_NAME, AgentEvent("error", f"host 处理失败：{exc}"))
                return DispatchOutcome((AgentFailure(HOST_NAME, str(exc)),))
            else:
                if decision.answer is not None:
                    # host 在“判断是否路由”的同一次 LLM 调用里已经给出最终
                    # 回答，直接落时间线；不再二次调用 host adapter。
                    on_event(HOST_NAME, AgentEvent(
                        "info",
                        "host 直接回答（单次调用）",
                        meta={"phase": "直接回答"},
                    ))
                    on_event(HOST_NAME, AgentEvent("text", decision.answer))
                    self._append_message(
                        HOST_NAME, decision.answer, command_id=command_id)
                    on_event(HOST_NAME, AgentEvent(
                        "done", meta={"hostAnswered": True}))
                    return DispatchOutcome()
                if decision.discussion is not None:
                    discussion = DiscussionRequest(
                        decision.discussion.participants,
                        decision.discussion.rounds,
                        decision.discussion.moderator,
                        user_text.strip(),
                    )
                    on_event(HOST_NAME, AgentEvent(
                        "info",
                        "讨论路由 → "
                        f"{'、'.join(discussion.participants)} · "
                        f"{discussion.rounds} 轮 · "
                        f"{discussion.moderator} 总结",
                        meta={
                            "route_targets": list(discussion.participants),
                            "phase": "讨论路由完成",
                            "discussion": True,
                            "discussion_rounds": discussion.rounds,
                            "discussion_moderator": discussion.moderator,
                        },
                    ))
                    return await self._dispatch_discussion(
                        discussion,
                        user_text,
                        on_event,
                        command_id,
                        recognized_naturally=True,
                        user_already_committed=True,
                        user_seq=committed.seq,
                    )
                collaboration = decision.collaboration
                targets, reason = decision.targets, decision.reason
                role_changes = decision.role_changes
                if collaboration is None:
                    assignments = {
                        name: decision.tasks.get(name)
                        or self._fallback_assignment(user_text)
                        for name in targets
                    }
            if collaboration is not None:
                on_event(HOST_NAME, AgentEvent(
                    "info",
                    "协作计划 → " + " → ".join(
                        step.agent for step in collaboration.steps)
                    + f"（{reason}）",
                    meta={
                        "route_targets": list(collaboration.participants),
                        "collaboration_steps": [
                            step.agent for step in collaboration.steps],
                        "phase": "协作计划就绪",
                        "collaboration": True,
                        "collaboration_total": len(collaboration.steps),
                    },
                ))
            else:
                on_event(HOST_NAME, AgentEvent(
                    "info",
                    f"路由 → {'、'.join(targets)}（{reason}）",
                    meta={
                        "route_targets": list(targets),
                        "phase": "路由完成",
                    },
                ))
                for name in targets:
                    assignments.setdefault(
                        name, self._fallback_assignment(user_text))
        else:
            if explicit_collaboration:
                on_event(HOST_NAME, AgentEvent(
                    "status",
                    "正在提取有序协作计划",
                    meta={
                        "agent_state": "running",
                        "phase": "协作计划识别",
                        "collaboration": True,
                    },
                ))
                try:
                    if getattr(self.host, "prepared_semantic_session", False):
                        raw = await self._run_host_semantic(
                            lambda _transcript:
                                self.host.build_collaboration_prompt(
                                    user_text, targets),
                            on_event,
                            command_id,
                            max_seq=committed.seq,
                        )
                        extraction = self.host.parse_collaboration(
                            raw, targets)
                    else:
                        extraction = await self.host.extract_collaboration(
                            user_text,
                            targets,
                            self.workdir,
                            lambda event: self._emit_adapter_event(
                                on_event, HOST_NAME, event),
                        )
                except _EventCallbackError as exc:
                    raise exc.cause from exc
                except CollaborationValidationError as exc:
                    on_event(HOST_NAME, AgentEvent(
                        "error", f"协作计划无效：{exc}",
                        meta={"phase": "协作计划无效"},
                    ))
                    return DispatchOutcome((
                        AgentFailure(HOST_NAME, str(exc)),
                    ))
                collaboration = extraction.plan
                role_changes = extraction.role_changes
                on_event(HOST_NAME, AgentEvent(
                    "info",
                    "协作计划 → " + " → ".join(
                        step.agent for step in collaboration.steps),
                    meta={
                        "route_targets": list(collaboration.participants),
                        "collaboration_steps": [
                            step.agent for step in collaboration.steps],
                        "phase": "协作计划就绪",
                        "collaboration": True,
                        "collaboration_total": len(collaboration.steps),
                    },
                ))
            else:
                role_changes = await self._extract_session_role_changes(
                    user_text,
                    targets,
                    on_event,
                    host_will_continue=HOST_NAME in targets,
                    command_id=command_id,
                    max_seq=committed.seq,
                )
        self._apply_session_role_changes(role_changes)
        if collaboration is not None:
            on_event(HOST_NAME, AgentEvent(
                "plan",
                CollaborationPlanEvent.created(collaboration).encode(),
                meta={
                    "collaboration": True,
                    "phase": "协作计划已冻结",
                },
            ))
            return await self._dispatch_collaboration(
                collaboration,
                on_event,
                command_id,
            )
        for name in targets:
            on_event(name, AgentEvent(
                "status",
                "已接收任务，准备执行",
                meta={
                    "agent_state": "running",
                    "phase": "准备执行",
                    **self._session_role_meta(name),
                },
            ))
        results = await _gather_structured(
            *(self._run_one(
                name, on_event, snapshot, command_id,
                assignments.get(name))
              for name in targets)
        )
        failures = tuple(
            AgentFailure(name, error)
            for name, error in zip(targets, results)
            if error is not None
        )
        return DispatchOutcome(failures)

    async def _dispatch_collaboration(
        self,
        plan: CollaborationPlan,
        on_event: EventCallback,
        command_id: str | None,
    ) -> DispatchOutcome:
        """Run a validated plan serially; later steps see earlier replies."""
        total = len(plan.steps)
        for index, step in enumerate(plan.steps, start=1):
            on_event(step.agent, AgentEvent(
                "plan",
                CollaborationPlanEvent.transition(
                    plan, index, "running").encode(),
                meta={"collaboration": True},
            ))
            meta = {
                "agent_state": "running",
                "phase": f"协作 {index}/{total}",
                "collaboration": True,
                "collaboration_step": index,
                "collaboration_total": total,
                "collaboration_agent": step.agent,
                **self._session_role_meta(step.agent),
            }
            on_event(step.agent, AgentEvent(
                "status",
                f"协作第 {index}/{total} 步：已接收任务，准备执行",
                meta=meta,
            ))

            def step_event(
                name: str,
                event: AgentEvent,
                *,
                _meta: dict = meta,
            ) -> None:
                on_event(name, AgentEvent(
                    event.kind,
                    event.text,
                    {**event.meta, **_meta},
                ))

            try:
                error = await self._run_one(
                    step.agent,
                    step_event,
                    list(self.history),
                    command_id,
                    step.assignment,
                )
            except asyncio.CancelledError:
                on_event(step.agent, AgentEvent(
                    "plan",
                    CollaborationPlanEvent.transition(
                        plan, index, "cancelled").encode(),
                    meta={"collaboration": True},
                ))
                on_event(step.agent, AgentEvent(
                    "status",
                    f"协作第 {index}/{total} 步已取消",
                    meta={
                        **meta,
                        "agent_state": "cancelled",
                        "phase": "当前步骤已取消",
                    },
                ))
                self._emit_skipped_collaboration_steps(
                    plan,
                    index,
                    on_event,
                    phase="因任务取消未执行",
                    reason="任务已取消",
                )
                raise
            if error is not None:
                on_event(step.agent, AgentEvent(
                    "plan",
                    CollaborationPlanEvent.transition(
                        plan, index, "failed").encode(),
                    meta={"collaboration": True},
                ))
                on_event(step.agent, AgentEvent(
                    "status",
                    f"协作在第 {index}/{total} 步停止，后续步骤未执行",
                    meta={
                        **meta,
                        "agent_state": "failed",
                        "phase": "协作已停止",
                    },
                ))
                self._emit_skipped_collaboration_steps(
                    plan,
                    index,
                    on_event,
                    phase="因前序失败未执行",
                    reason="前序步骤失败",
                )
                return DispatchOutcome((AgentFailure(step.agent, error),))
            on_event(step.agent, AgentEvent(
                "plan",
                CollaborationPlanEvent.transition(
                    plan, index, "completed").encode(),
                meta={"collaboration": True},
            ))
        return DispatchOutcome()

    def _emit_skipped_collaboration_steps(
        self,
        plan: CollaborationPlan,
        completed_count: int,
        on_event: EventCallback,
        *,
        phase: str,
        reason: str,
    ) -> None:
        """Publish honest terminal state for steps that will never start."""
        total = len(plan.steps)
        prior_agents = {
            step.agent for step in plan.steps[:completed_count]
        }
        for step_index, step in enumerate(
            plan.steps[completed_count:],
            start=completed_count + 1,
        ):
            on_event(step.agent, AgentEvent(
                "plan",
                CollaborationPlanEvent.transition(
                    plan, step_index, "skipped").encode(),
                meta={"collaboration": True},
            ))
            on_event(step.agent, AgentEvent(
                "status",
                f"协作第 {step_index}/{total} 步未执行：{reason}",
                meta={
                    "agent_state": "skipped",
                    "phase": phase,
                    "collaboration": True,
                    "collaboration_step": step_index,
                    "collaboration_total": total,
                    "collaboration_agent": step.agent,
                    "collaboration_preserve_agent_state": (
                        step.agent in prior_agents),
                    **self._session_role_meta(step.agent),
                },
            ))

    async def _dispatch_workflow(
        self,
        request,
        baseline,
        user_text: str,
        on_event: EventCallback,
        command_id: str | None,
    ) -> DispatchOutcome:
        """把一条命令委托给 workflow 深模块；内部阶段不递归 dispatch。"""

        def workflow_event(agent_name: str, event: AgentEvent) -> None:
            role_meta = self._session_role_meta(agent_name)
            if role_meta:
                event = AgentEvent(
                    event.kind,
                    event.text,
                    {**event.meta, **role_meta},
                )
            on_event(agent_name, event)

        committed = self._append_message(
            "user", user_text, command_id=command_id)
        workflow_event("user", AgentEvent(
            "committed",
            user_text,
            {
                "seq": committed.seq,
                "command_id": command_id,
                "workflow": True,
                "workflow_stage": "queued",
                "workflow_roles": request.roles,
                "baseline_head": baseline.head,
                "baseline_branch": baseline.branch,
                "baseline_fingerprint": baseline.fingerprint,
                "steering_available": False,
            },
        ))
        workflow_event("system", AgentEvent(
            "status",
            "workflow baseline · "
            f"head={baseline.head} · branch={baseline.branch} · "
            f"fingerprint={baseline.fingerprint}",
            {
                "command_id": command_id,
                "workflow": True,
                "workflow_stage": "baseline",
                "workflow_roles": request.roles,
                "baseline_head": baseline.head,
                "baseline_branch": baseline.branch,
                "baseline_fingerprint": baseline.fingerprint,
                "steering_available": False,
                "agent_state": "running",
                "phase": "workflow baseline 已持久化",
            },
        ))
        workflow_id = command_id or f"direct-{committed.seq}"

        async def stage_runner(
            name: str,
            stage: str,
            assignment: str,
            execution_mode: ExecutionMode,
        ) -> StageDelivery:
            before_seq = self.history[-1].seq if self.history else 0
            snapshot = list(self.history)

            def stage_event(agent_name: str, event: AgentEvent) -> None:
                # 一个角色可能在 repair/reverify 再次出现；阶段间 done 不得
                # 让固定 TUI 状态提前锁成 terminal。
                if event.kind == "done" and stage != "final":
                    workflow_event(agent_name, AgentEvent(
                        "status",
                        f"workflow {stage} 响应已落盘",
                        {
                            **event.meta,
                            "workflow": True,
                            "workflow_stage": stage,
                            "workflow_roles": request.roles,
                            "workflow_agent": agent_name,
                            "steering_available": (
                                workflow.steering_available),
                            "agent_state": "running",
                            "phase": f"workflow {stage} 已完成",
                        },
                    ))
                    return
                workflow_event(agent_name, event)

            error = await self._run_one(
                name,
                stage_event,
                snapshot,
                command_id,
                assignment,
                execution_mode,
            )
            replies = [
                message.text for message in self.history
                if (message.seq > before_seq
                    and message.speaker == name
                    and message.command_id == command_id)
            ]
            return StageDelivery(replies[-1] if replies else "", error)

        workflow = MilestoneWorkflow(
            request,
            workflow_id,
            baseline,
            self.workspace_inspector,
            stage_runner,
            workflow_event,
        )
        if command_id is not None:
            self._active_workflows[command_id] = workflow
        try:
            result = await workflow.run()
        finally:
            if command_id is not None:
                self._active_workflows.pop(command_id, None)
        return DispatchOutcome(tuple(
            AgentFailure(item.agent, item.error)
            for item in result.failures
        ))

    def prepare_workflow_steering(self, command_id: str, instruction: str):
        workflow = self._active_workflows.get(command_id)
        if workflow is None:
            raise WorkflowValidationError(
                f"命令 {command_id} 不是活动 workflow")
        return workflow.prepare_steering(instruction)

    def prepare_interjection(self, command_id: str, instruction: str):
        """Resolve one bounded interjection without protocol/name branching."""
        workflow = self._active_workflows.get(command_id)
        if workflow is not None:
            # Workflow owns stricter phase/role/permission bounds. A Pi stage
            # must not bypass those bounds by falling through to native RPC.
            return workflow.prepare_steering(instruction)

        active = self._active_deliveries.get(command_id, {})
        if not active:
            raise RuntimeError("当前任务尚未进入可插话的 agent 运行段")
        if len(active) != 1:
            raise RuntimeError(
                "当前任务有多个 agent 同时运行，无法确定唯一插话目标")
        token, (name, adapter) = next(iter(active.items()))
        submit = getattr(adapter, "interject", None)
        if not callable(submit):
            raise RuntimeError(
                f"当前 {name} transport 不支持安全的运行中插话；"
                "该输入仍在队首，可等待正常出队，或按 Esc 取消当前任务")
        clean = instruction.strip()
        event = AgentEvent(
            "interjection",
            clean,
            {
                "agent": name,
                "mode": "in_flight",
                "applies_after": "current_turn",
            },
        )

        async def commit() -> None:
            current = self._active_deliveries.get(command_id, {})
            if current.get(token) != (name, adapter):
                raise RuntimeError("插话目标已结束或切换，未发送")
            await submit(clean)

        return _RuntimeInterjectionProposal(
            event,
            {
                "command_id": command_id,
                "accepted": 1,
                "agent": name,
                "mode": "in_flight",
                "applies_after": "current_turn",
            },
            commit,
        )

    def steer_workflow(self, command_id: str, instruction: str):
        """无持久层调用方的兼容入口；CommandBus 使用两阶段 prepare/commit。"""
        workflow = self._active_workflows.get(command_id)
        if workflow is None:
            raise WorkflowValidationError(
                f"命令 {command_id} 不是活动 workflow")
        return workflow.steer(instruction)

    def _activate_delivery(
        self,
        command_id: str | None,
        name: str,
        adapter: AgentAdapter,
    ) -> object | None:
        if command_id is None:
            return None
        token = object()
        self._active_deliveries.setdefault(command_id, {})[token] = (
            name, adapter)
        return token

    def _deactivate_delivery(
        self,
        command_id: str | None,
        token: object | None,
    ) -> None:
        if command_id is None or token is None:
            return
        active = self._active_deliveries.get(command_id)
        if active is None:
            return
        active.pop(token, None)
        if not active:
            self._active_deliveries.pop(command_id, None)

    async def _dispatch_discussion(
            self,
            request: DiscussionRequest,
            user_text: str,
            on_event: EventCallback,
            command_id: str | None,
            *,
            recognized_naturally: bool = False,
            user_already_committed: bool = False,
            user_seq: int | None = None,
    ) -> DispatchOutcome:
        """Execute one bounded discussion without recursively dispatching.

        The raw command is the only user record.  Participant instructions are
        per-turn assignments, so internal rounds never impersonate the user or
        create nested CommandBus commands.
        """
        if not user_already_committed:
            committed = self._append_message(
                "user", user_text, command_id=command_id)
            user_seq = committed.seq
            on_event("user", AgentEvent(
                "committed", user_text,
                meta={"seq": committed.seq, "command_id": command_id},
            ))
        elif user_seq is None:
            raise ValueError("已提交的讨论必须提供 user_seq")
        if recognized_naturally:
            on_event("system", AgentEvent(
                "status",
                "已识别为讨论 · "
                f"{len(request.participants)} 人 × {request.rounds} 轮 · "
                f"{request.moderator} 总结",
                meta={
                    "phase": "讨论意图识别",
                    "discussion": True,
                    "discussion_rounds": request.rounds,
                    "discussion_participants": list(request.participants),
                    "discussion_moderator": request.moderator,
                },
            ))
        role_targets = list(dict.fromkeys(
            (*request.participants, request.moderator)))
        role_changes = await self._extract_session_role_changes(
            request.topic,
            role_targets,
            on_event,
            host_will_continue=request.moderator == HOST_NAME,
            command_id=command_id,
            max_seq=user_seq,
        )
        self._apply_session_role_changes(role_changes)
        active = list(request.participants)
        failures: list[AgentFailure] = []

        for round_number in range(1, request.rounds + 1):
            if round_number > 1 \
                    and len(active) < MIN_DISCUSSION_PARTICIPANTS:
                on_event("system", AgentEvent(
                    "status",
                    "存活参与者不足两人，停止后续交叉轮并进入主持总结",
                    meta={"phase": "讨论提前收口"},
                ))
                break
            on_event("system", AgentEvent(
                "status",
                f"讨论第 {round_number}/{request.rounds} 轮："
                f"{'、'.join(active)}",
                meta={"phase": f"讨论 {round_number}/{request.rounds}"},
            ))
            snapshot = list(self.history)
            for name in active:
                on_event(name, AgentEvent(
                    "status",
                    f"准备讨论第 {round_number}/{request.rounds} 轮",
                    meta={
                        "agent_state": "running",
                        "phase": f"讨论 {round_number}/{request.rounds}",
                        **self._session_role_meta(name),
                    },
                ))

            # Intermediate done would make the TUI regard an agent as terminal
            # before its next round.  Preserve the event as a running status;
            # after gather we emit a real done only for participants that will
            # not receive another turn.
            has_possible_next_round = round_number < request.rounds

            def round_event(name: str, event: AgentEvent) -> None:
                if has_possible_next_round and event.kind == "done":
                    on_event(name, AgentEvent(
                        "status",
                        f"第 {round_number} 轮回复已落盘",
                        meta={
                            **event.meta,
                            "agent_state": "running",
                            "phase": "等待下一轮",
                        },
                    ))
                    return
                on_event(name, event)

            results = await _gather_structured(
                *(self._run_one(
                    name,
                    round_event,
                    snapshot,
                    command_id,
                    participant_assignment(request, name, round_number),
                ) for name in active)
            )
            next_active: list[str] = []
            for name, error in zip(active, results):
                if error is None:
                    next_active.append(name)
                else:
                    failures.append(AgentFailure(name, error))

            will_continue = (
                round_number < request.rounds
                and len(next_active) >= MIN_DISCUSSION_PARTICIPANTS
            )
            if has_possible_next_round and not will_continue:
                for name in next_active:
                    on_event(name, AgentEvent(
                        "done", meta={"discussionStoppedEarly": True}))
            active = next_active

        on_event(request.moderator, AgentEvent(
            "status",
            "正在汇总讨论",
            meta={
                "agent_state": "running",
                "phase": "主持仲裁",
                **self._session_role_meta(request.moderator),
            },
        ))
        moderator_error = await self._run_one(
            request.moderator,
            on_event,
            list(self.history),
            command_id,
            moderator_assignment(request),
        )
        if moderator_error is not None:
            failures.append(AgentFailure(request.moderator, moderator_error))
        return DispatchOutcome(tuple(failures))

    async def _run_one(self, name: str, on_event: EventCallback,
                       snapshot: list[Message],
                       command_id: str | None = None,
                       assignment: str | None = None,
                       execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
                       ) -> str | None:
        assignment = session_role_assignment(
            self._session_roles.get(name), assignment)
        adapter = self.adapters[name]
        if getattr(adapter, "stateful_session", False):
            # P1 契约：读 cursor → 选增量 → stream 完整执行 → 推进 cursor
            # 必须在该 agent 的 delivery lock 内完成，prompt 拿到锁之后才
            # 构造——否则并发的两个 dispatch 都按旧 cursor 构造 prompt，
            # 第二轮会重复/乱序投递。不同 agent 持不同的锁，并行扇出不受影响。
            async with self._delivery_lock(name):
                # HostBackend 可能在本轮等锁时完成切换。
                # adapter 必须在锁内重新解析，否则会对已关闭的旧
                # runtime 发起请求。worker 的 adapter 不会被切换，
                # 但复用这一统一规则避免通用层按名分支。
                adapter = self.adapters[name]
                if self._closed:
                    # 用户消息已提交、但本轮排队期间 orchestrator 已关闭：
                    # 绝不 start/load/prompt agent
                    raise OrchestratorClosedError(
                        f"Orchestrator 已关闭，放弃对 {name!r} 的排队派发")
                delivery_token = self._activate_delivery(
                    command_id, name, adapter)
                try:
                    if getattr(adapter, "stream_prepared", None) is not None:
                        # ACP restore 原语：prepare 与 prompt 同一锁生命周期，
                        # checkpoint（cursor/session_id）在 prompt 前原子提交。
                        failed, delivered_upto = await self._deliver_prepared(
                            name, adapter, on_event, command_id, assignment,
                            execution_mode)
                    else:
                        # 无 stream_prepared 的 stateful fake：纯内存 seq 路径
                        messages, delivered_upto = self._messages_for(name)
                        failed = await self._deliver(
                            name, adapter, messages, on_event, command_id,
                            assignment, execution_mode)
                finally:
                    self._deactivate_delivery(command_id, delivery_token)
                if failed is None:
                    # 成功交付后才推进 cursor，且只推进到本轮构造时的快照
                    # 末尾 seq：1) 明确未提交的失败不推进——下轮从旧 cursor
                    # 补发，不丢上下文；2) 本轮进行期间到达的新消息没发出去，必须留给
                    # 下一轮；3) 自己的回复靠 speaker 过滤跳过。
                    # post-submit no-replay 例外已在 _deliver_prepared 的
                    # 专门异常分支里先持久化，不会走到这里。
                    # 有 store：先落盘（原子写），成功后才更新内存 cursor；
                    # 写失败向 dispatch 传播，内存/磁盘 cursor 都不动，
                    # 下一轮按旧 cursor 补发。
                    self._commit_cursor(name, delivered_upto)
                return failed
        else:
            # 无状态（JSONL）agent：dispatch 瞬间的 transcript 快照
            delivery_token = self._activate_delivery(
                command_id, name, adapter)
            try:
                return await self._deliver(
                    name, adapter, snapshot[-self.history_limit:], on_event,
                    command_id, assignment, execution_mode)
            finally:
                self._deactivate_delivery(command_id, delivery_token)

    async def _deliver_prepared(self, name: str, adapter: AgentAdapter,
                                on_event: EventCallback,
                                command_id: str | None = None,
                                assignment: str | None = None,
                                execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
                                ) -> tuple[str | None, int]:
        result = await self._consume_prepared(
            name,
            adapter,
            on_event,
            command_id,
            lambda messages: self._build_prompt(
                name, messages, assignment),
            execution_mode,
        )
        # history 必须诚实：有回复记回复，失败了记失败，
        # "无文本回复"只留给调用成功但真没说话的情况。
        reply = result.text.strip()
        if reply:
            if result.failed is not None:
                reply += f"\n\n（调用失败：{result.failed[:120]}）"
            self._append_message(name, reply, command_id=command_id)
        elif result.failed is not None:
            self._append_message(
                name,
                f"（调用失败：{result.failed[:120]}）",
                command_id=command_id,
            )
        else:
            self._append_message(name, "（无文本回复）", command_id=command_id)
        if result.failed is None:
            # 只有成功轮（append 也已成功，否则上面已抛出）才发 done。
            on_event(name, AgentEvent("done", meta=result.done_meta))
        return result.failed, result.delivered_upto

    async def _consume_prepared(
        self,
        name: str,
        adapter: AgentAdapter,
        on_event: EventCallback,
        command_id: str | None,
        prompt_builder: Callable[[list[Message]], str],
        execution_mode: ExecutionMode,
        *,
        emit_text: bool = True,
        max_seq: int | None = None,
    ) -> _PreparedStreamResult:
        """Prepare/checkpoint/stream once without imposing timeline policy.

        ``make_prompt`` remains the atomic checkpoint hook. Worker delivery
        records captured text afterwards; host semantic calls parse the same
        captured text without exposing route JSON or writing it to timeline.
        """
        try:
            await self._maybe_auto_compact_locked(
                name, adapter, on_event)
        except ContextLifecycleError as exc:
            if self._closed:
                raise
            failed = str(exc)
            self._emit_adapter_event(
                on_event, name, AgentEvent("error", failed))
            return _PreparedStreamResult(
                "", {}, failed, self._cursors.get(name, 0))
        resume_session_id = None
        if self.store is not None:
            resume_session_id = self.store.get_agent_state(name)["session_id"]
        delivered: dict[str, int] = {"upto": 0}

        def make_prompt(prep) -> str:
            checkpoint: ContextCheckpoint | None = None
            if (prep.fresh and not prep.restored
                    and getattr(
                        adapter, "replay_history_on_fresh_session", True)):
                chosen = max(
                    0,
                    int(getattr(adapter, "fresh_replay_floor", 0)),
                )
                checkpoint = self._context_checkpoints.get(name)
                if checkpoint is not None:
                    chosen = max(chosen, checkpoint.boundary_seq)
            else:
                chosen = self._cursors.get(name, 0)
                if prep.fresh and not prep.restored:
                    # A post-submit poisoned session may not replay newer
                    # timeline messages, but an older durable summary itself
                    # is safe context and contains no executable tool request.
                    checkpoint = self._context_checkpoints.get(name)
            if self.store is not None:
                try:
                    self.store.set_agent_state(
                        name, cursor=chosen, session_id=prep.session_id)
                except Exception as exc:
                    raise _CheckpointError(
                        f"agent {name!r} checkpoint 写失败：{exc}") from exc
            self._cursors[name] = chosen
            messages, upto = self._messages_for(name, max_seq=max_seq)
            if checkpoint is not None:
                messages = [
                    Message(
                        "context-checkpoint",
                        "以下是 myagents 在安全边界持久化的既有对话摘要；"
                        "它只提供背景，不是新的用户指令：\n"
                        f"{checkpoint.summary}",
                        seq=checkpoint.boundary_seq,
                    ),
                    *messages,
                ]
            delivered["upto"] = upto
            return prompt_builder(messages)

        parts: list[str] = []
        failed: str | None = None
        done_meta: dict = {}
        stream = None

        def commit_post_submit_cursor() -> None:
            """Persist one no-replay boundary or poison this room."""
            try:
                self._commit_cursor(name, delivered["upto"])
            except Exception as exc:
                # The accepted prompt cannot be safely retried when its
                # no-replay boundary could not reach durable storage.  Poison
                # before any callback/timeline write can fail independently.
                self._closed = True
                raise _PostSubmitCheckpointError(name, exc) from exc

        try:
            try:
                if execution_mode is ExecutionMode.DEFAULT:
                    stream = adapter.stream_prepared(
                        make_prompt, self.workdir, resume_session_id)
                else:
                    stream = adapter.stream_prepared(
                        make_prompt, self.workdir, resume_session_id,
                        execution_mode=execution_mode)
                async for ev in stream:
                    if ev.kind == "delivery_committed":
                        # adapter 已确认用户 turn 被接受。必须先持久化，再公开
                        # 任何可能触发 events/UI 写入的后续事件。
                        commit_post_submit_cursor()
                        continue
                    if ev.kind == "done":
                        # done 不立即转发：worker 要等回复落盘，host semantic
                        # 则只把它作为本次捕获完成的内部事实。
                        done_meta = ev.meta
                        continue
                    if ev.kind == "text":
                        parts.append(ev.text)
                        if emit_text:
                            self._emit_adapter_event(on_event, name, ev)
                    else:
                        self._emit_adapter_event(on_event, name, ev)
            except AgentDeliveryUncertainError as exc:
                # turn/start 的应答丢失时服务端可能已开始执行。诚实记录失败，
                # 先持久 no-replay 边界，再做任何 callback/timeline 写入。
                commit_post_submit_cursor()
                failed = str(exc)
                self._emit_adapter_event(
                    on_event, name, AgentEvent("error", failed))
            except AgentDeliveryCancelledError:
                # 保持取消语义交给 CommandBus，但先固化已提交 turn 的
                # no-replay 边界。
                commit_post_submit_cursor()
                raise
        except _EventCallbackError as exc:
            await _close_async_stream(stream)
            raise exc.cause from exc
        except _PostSubmitCheckpointError as exc:
            # async-for does not guarantee that a producer is closed when its
            # consumer body raises.  Explicitly finish the adapter generator
            # so an accepted prompt cannot remain active behind the poisoned
            # room.  Teardown errors are secondary to the storage root cause.
            await _close_async_stream(stream)
            try:
                on_event(name, AgentEvent("error", str(exc)))
            except Exception:
                # Preserve the fatal checkpoint failure as the primary error;
                # the room is already poisoned and cannot continue dispatch.
                pass
            raise exc.cause from exc
        except _CheckpointError as exc:
            # checkpoint 失败：发 error event 后原样抛出，不进 history
            on_event(name, AgentEvent("error", str(exc)))
            raise
        except Exception as exc:  # agent 崩溃不拖垮整个聊天室
            failed = str(exc)
            on_event(name, AgentEvent("error", failed))
        return _PreparedStreamResult(
            "".join(parts), done_meta, failed, delivered["upto"])

    async def _run_host_semantic(
        self,
        prompt_builder: Callable[[str], str],
        on_event: EventCallback,
        command_id: str | None = None,
        *,
        max_seq: int,
    ) -> str:
        """Run one model-only host call through the durable prepared seam."""
        async with self._delivery_lock(HOST_NAME):
            # A backend switch may finish while this semantic call waits for
            # the Host writer lock. Resolve the published Host only after the
            # lock is acquired, exactly like the explicit @host path.
            adapter = self.host
            if self._closed:
                raise OrchestratorClosedError(
                    "Orchestrator 已关闭，放弃 host 语义调用")
            delivery_token = self._activate_delivery(
                command_id, HOST_NAME, adapter)
            try:
                result = await self._consume_prepared(
                    HOST_NAME,
                    adapter,
                    on_event,
                    command_id,
                    lambda messages: prompt_builder(self._format(messages)),
                    ExecutionMode.READ_ONLY,
                    emit_text=False,
                    max_seq=max_seq,
                )
            finally:
                self._deactivate_delivery(command_id, delivery_token)
            if result.failed is not None:
                raise RuntimeError(result.failed)
            self._commit_cursor(HOST_NAME, result.delivered_upto)
            return result.text

    async def _deliver(self, name: str, adapter: AgentAdapter,
                       messages: list[Message],
                       on_event: EventCallback,
                       command_id: str | None = None,
                       assignment: str | None = None,
                       execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
                       ) -> str | None:
        """执行一轮并把结果写回 history；返回失败信息（None = 成功）。"""
        prompt = self._build_prompt(name, messages, assignment)
        parts: list[str] = []
        failed: str | None = None
        done_meta: dict = {}
        stream = None
        try:
            if execution_mode is ExecutionMode.DEFAULT:
                stream = adapter.stream(prompt, self.workdir)
            else:
                stream = adapter.stream(
                    prompt, self.workdir, execution_mode=execution_mode)
            async for ev in stream:
                if ev.kind == "done":
                    # done 不立即转发：等回复落盘成功后才发（见方法尾）
                    done_meta = ev.meta
                    continue
                self._emit_adapter_event(on_event, name, ev)
                if ev.kind == "text":
                    parts.append(ev.text)
        except _EventCallbackError as exc:
            await _close_async_stream(stream)
            raise exc.cause from exc
        except Exception as exc:  # agent 崩溃不拖垮整个聊天室
            failed = str(exc)
            on_event(name, AgentEvent("error", failed))
        # history 必须诚实：有回复记回复，失败了记失败，
        # "无文本回复"只留给调用成功但真没说话的情况。
        reply = "".join(parts).strip()
        if reply:
            if failed is not None:
                reply += f"\n\n（调用失败：{failed[:120]}）"
            self._append_message(name, reply, command_id=command_id)
        elif failed is not None:
            self._append_message(name, f"（调用失败：{failed[:120]}）",
                                 command_id=command_id)
        else:
            self._append_message(name, "（无文本回复）", command_id=command_id)
        if failed is None:
            # 只有成功轮（append 也已成功，否则上面已抛出）才发 done
            on_event(name, AgentEvent("done", meta=done_meta))
        return failed

    @staticmethod
    def _emit_adapter_event(
            on_event: EventCallback, name: str, event: AgentEvent) -> None:
        try:
            on_event(name, event)
        except Exception as exc:
            raise _EventCallbackError(exc) from exc

    def _commit_cursor(self, name: str, delivered_upto: int) -> None:
        """先落盘再更新内存；成功轮与 post-submit no-replay 共用。"""
        committed = max(self._cursors.get(name, 0), delivered_upto)
        if self.store is not None:
            if name == HOST_NAME:
                self.store.commit_host_cursor(committed)
            else:
                self.store.set_agent_state(name, cursor=committed)
        self._cursors[name] = committed

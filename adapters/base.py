"""Adapter 基类：把各家 agent transport 统一成一个接口。

关键概念（学习要点）：
- **Adapter 模式**：kimi / opencode / qwen / claude / codex 各自的命令行参数和
  输出格式都不同，这里定义统一契约，上层 orchestrator 不需要关心差异。
- **流式事件（streaming events）**：agent 干活是长时间的（几十秒到几分钟），
  不能等它跑完才显示。具体 transport 把 ACP 或 JSONL 事件统一成
  `AgentEvent`，逐条吐给 UI。
- **会话恢复（session resume）**：每次无头调用默认是全新会话。CLI 会返回
  session id，下次调用带上它可以延续上下文。MVP 默认不用（用 transcript
  转发代替），但接口里留好了位置。
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Callable, Mapping, Protocol


# Stateful transports may be silent while a local or remote model pre-fills a
# long context. Keep one product-wide bounded default; protocol adapters may
# accept shorter injected values for deterministic tests.
DEFAULT_AGENT_INACTIVITY_TIMEOUT = 300.0


class AgentDeliveryUncertainError(RuntimeError):
    """请求可能已被 agent 接受，调用方不得自动重投同一批消息。"""


class AgentDeliveryCancelledError(asyncio.CancelledError):
    """请求已提交后被取消；保持取消语义，同时禁止自动重投。"""


class ReadOnlyFallbackError(RuntimeError):
    """写阶段只能进入只读 fallback；调用方必须 fail-closed。"""


class LineFrameTooLargeError(RuntimeError):
    """A newline-delimited transport frame exceeded its configured limit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"line frame exceeds {limit} bytes")


class ExecutionMode(str, Enum):
    """一轮 agent 调用允许的工作区能力。"""

    DEFAULT = "default"
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


_SENSITIVE_KEY_VALUE = re.compile(
    r"(?i)(?<![A-Z0-9_])"
    r"([\"']?[A-Z0-9_]{0,32}(?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|"
    r"AUTHORIZATION|CREDENTIAL|PRIVATE_KEY)[A-Z0-9_]{0,32}"
    r"[\"']?\s*[:=]\s*)"
    r"(?!\[已隐藏\])(?:\"[^\"]*\"|'[^']*'|[^\s,}\]、，；;]+)")
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)(\bAuthorization\s*[:=]\s*)(?:Bearer|Basic)\s+"
    r"[^\s,}\]\"']+")
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[^\s'\"]+")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_TOOL_STATUS_LABELS = {
    "pending": "等待中",
    "queued": "等待中",
    "in_progress": "进行中",
    "running": "进行中",
    "completed": "已完成",
    "succeeded": "已完成",
    "success": "已完成",
    "failed": "失败",
    "error": "失败",
    "declined": "已拒绝",
    "cancelled": "已取消",
    "canceled": "已取消",
}


def redact_sensitive_text(value: str, *, limit: int = 2000) -> str:
    """隐藏命令/工具文本中的常见凭据形态并做长度限制。"""
    # 先截取 UI/事件实际会保留的前缀，既满足输出上限，也避免针对模型返回的
    # 多 MiB 文本做无意义的全量正则扫描。
    text = value[:limit]
    folded = text.casefold()
    if "authorization" in folded:
        text = _AUTHORIZATION_HEADER.sub(r"\1[已隐藏]", text)
    if any(marker in folded for marker in (
        "token", "secret", "password", "api_key", "apikey",
        "authorization", "credential", "private_key",
    )):
        text = _SENSITIVE_KEY_VALUE.sub(r"\1[已隐藏]", text)
    if "bearer " in folded:
        text = _BEARER_VALUE.sub("Bearer [已隐藏]", text)
    if "sk-" in text:
        text = _OPENAI_KEY.sub("[已隐藏]", text)
    return text[:limit]


def tool_status_label(value: object) -> str:
    """把常见协议状态转为稳定、简短的用户文案；未知值保留原意。"""
    if value is None:
        return ""
    status = str(value).strip()
    if not status:
        return ""
    return _TOOL_STATUS_LABELS.get(
        status.lower(), status.replace("_", " "))


@dataclass
class AgentEvent:
    """agent 执行过程中吐出的一个事件。

    kind:
      - "text"  : agent 的正文输出（可能分多段到达）
      - "info"  : 元信息（token 用量、session id 等），UI 里灰色显示
      - "status": 安全的阶段/心跳摘要，不含 chain-of-thought 正文
      - "tool"  : 工具标题与已脱敏、有界的 meta 上下文；同一
        tool_call_id 的生命周期通过 meta.status 原位更新
      - "permission": 权限请求/结果摘要
      - "plan"  : 编排器冻结的有界协作计划或步骤迁移；JSON 正文只进入
        执行事件与 TUI，不进入聊天或 agent history
      - "activity": 无可见增量的协议活动；只刷新 CommandBus 静默时钟，
        不进入 UI 或持久事件
      - "cancel_requested": 控制层请求取消，UI 应先结束权限等待
      - "steering": workflow 已接受的阶段边界补充指令；只进 execution event
      - "delivery_committed": 内部投递确认；Orchestrator 在公开任何后续
        事件前持久化 no-replay cursor，不转发给 UI
      - "error" : 出错（非零退出、stderr 内容）
      - "done"  : 本次调用结束
    """

    kind: str
    text: str = ""
    meta: dict = field(default_factory=dict)


class AgentAdapter(Protocol):
    """所有 agent adapter 遵守的契约。

    新增一个 agent（比如 claude）只需要：
    1. 写一个类，实现 name / stream()
    2. 在 orchestrator.AGENT_SPECS 里注册
    """

    name: str
    session_id: str | None  # 上一次调用拿到的会话 id，用于 resume

    def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        """异步生成器：执行一轮对话，边执行边吐事件。"""
        ...


@dataclass(frozen=True)
class AgentHostCapability:
    """Adapter-owned proof and constructor for an independent read-only Host.

    Merely accepting ``ExecutionMode.READ_ONLY`` is not enough: declaring this
    capability means the concrete adapter enforces that mode in its runtime
    policy and that ``factory`` creates a fresh writer/session.  The generic
    Host layer never infers safety from an agent name or transport.
    """

    transport: str
    factory: Callable[[], AgentAdapter]

    def __post_init__(self) -> None:
        transport = self.transport.strip()
        if not transport or any(ch in transport for ch in "\r\n\0"):
            raise ValueError("Host capability transport 无效")
        if not callable(self.factory):
            raise TypeError("Host capability factory 必须可调用")
        object.__setattr__(self, "transport", transport)


class BoundedLog:
    """有界日志：只保留首段 + 尾段，中间丢弃（记录省略量）。

    用于子进程 stderr：管道要持续排空（防背压死锁），但内容不能无限
    累积（防长任务把 TUI 内存吃到 OOM）。失败报错时首尾往往最有用：
    头部是原因，尾部是现状。
    """

    def __init__(self, limit: int = 2048) -> None:
        self.limit = limit
        self.head = ""
        self.tail = ""
        self.total = 0

    def append(self, s: str) -> None:
        self.total += len(s)
        if len(self.head) < self.limit:
            take = self.limit - len(self.head)
            self.head += s[:take]
            s = s[take:]
        if s:
            self.tail = (self.tail + s)[-self.limit:]

    def render(self) -> str:
        # head + tail 可能恰好覆盖全部内容（limit < total <= 2*limit），
        # 此时 omitted == 0 但 tail 非空——必须拼上 tail，否则静默丢尾部。
        if not self.tail:
            return self.head
        omitted = self.total - len(self.head) - len(self.tail)
        if omitted > 0:
            return f"{self.head}\n…（省略 {omitted} 字符）…\n{self.tail}"
        return self.head + self.tail


async def read_lines(
    stream: asyncio.StreamReader,
    *,
    max_frame_bytes: int | None = None,
) -> AsyncIterator[str]:
    """从 StreamReader 逐行产出文本：读定长块、按 \n 手动切。

    不用 `async for line in stream`（底层 readline 有 64KB 单行上限，
    超长 JSON 行会直接抛 LimitOverrunError）——与 pi 的 rpc-process
    同一做法。stream_jsonl 和 acp/client 共用。
    """
    if max_frame_bytes is not None and max_frame_bytes <= 0:
        raise ValueError("max_frame_bytes 必须大于 0")
    buf = b""
    while chunk := await stream.read(65536):
        buf += chunk
        while True:
            newline = buf.find(b"\n")
            if newline < 0:
                if (max_frame_bytes is not None
                        and len(buf) > max_frame_bytes):
                    raise LineFrameTooLargeError(max_frame_bytes)
                break
            raw, buf = buf[:newline], buf[newline + 1:]
            if max_frame_bytes is not None and len(raw) > max_frame_bytes:
                raise LineFrameTooLargeError(max_frame_bytes)
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                yield line
    tail = buf.decode("utf-8", errors="replace").strip()
    if tail:
        yield tail


async def stream_jsonl(
    cmd: list[str],
    workdir: str,
    *,
    env_overrides: Mapping[str, str] | None = None,
) -> AsyncIterator[str]:
    """公共助手：spawn 子进程，逐行产出 stdout。

    各家 CLI 的无头模式都是"命令 + JSONL 输出"，差异只在参数和 JSON 结构，
    所以子进程管理抽到这里共用。非零退出时把 stderr 作为异常抛出。

    健壮性四条（都是真实踩过的坑）：
    - **stderr 并发排空 + 有界保留**：只读 stdout、等进程退出再读 stderr
      会死锁——子进程 stderr 写满 64KB 管道缓冲后阻塞在 write 上，永不
      退出。所以起一个 drainer task 并发排空；内容用 BoundedLog 只留
      首段+尾段，防长任务把内存吃到 OOM。
    - **手动分帧**：不用 `async for line in proc.stdout`（底层 readline
      有 64KB 单行上限，超长 JSON 行会直接抛 LimitOverrunError），
      改为读定长块、按 \\n 手动切（与 pi 的 rpc-process 同一做法）。
    - **取消即杀整个进程组**：start_new_session 让子进程自立进程组，
      取消时 killpg SIGTERM → 5s 超时 → SIGKILL。只 terminate 直接
      子进程不够——CLI 启动的 shell/tool 后代会继承管道，既卡死读取，
      又可能在 TUI 退出后继续改工作区。
    """
    process_env = None
    if env_overrides:
        process_env = os.environ.copy()
        process_env.update(env_overrides)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workdir,
        env=process_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # 自立进程组，pgid = 子进程 pid
    )
    stderr_log = BoundedLog()

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(8192):
            stderr_log.append(chunk.decode("utf-8", errors="replace"))

    drain = asyncio.create_task(drain_stderr())
    try:
        assert proc.stdout is not None
        async for line in read_lines(proc.stdout):
            yield line
        await proc.wait()
    finally:
        if proc.returncode is None:  # 取消/异常时进程组还活着，负责到底
            try:
                os.killpg(proc.pid, signal.SIGTERM)  # pgid == proc.pid
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
        await drain  # 进程退出后 stderr 必 EOF，在此收齐
    if proc.returncode != 0:
        # render() 本身已有界（首段+尾段），不要再截断——会把尾段切掉
        raise RuntimeError(f"exit {proc.returncode}: {stderr_log.render().strip()}")

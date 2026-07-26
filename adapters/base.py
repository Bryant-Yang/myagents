"""Adapter 基类：把各家 agent CLI 的无头模式统一成一个接口。

关键概念（学习要点）：
- **Adapter 模式**：kimi / opencode / claude / codex 各自的命令行参数和
  输出格式都不同，这里定义统一契约，上层 orchestrator 不需要关心差异。
- **流式事件（streaming events）**：agent 干活是长时间的（几十秒到几分钟），
  不能等它跑完才显示。各家 CLI 都以 JSONL（每行一个 JSON）输出事件，
  我们逐行解析、逐条吐给 UI。
- **会话恢复（session resume）**：每次无头调用默认是全新会话。CLI 会返回
  session id，下次调用带上它可以延续上下文。MVP 默认不用（用 transcript
  转发代替），但接口里留好了位置。
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


@dataclass
class AgentEvent:
    """agent 执行过程中吐出的一个事件。

    kind:
      - "text"  : agent 的正文输出（可能分多段到达）
      - "info"  : 元信息（token 用量、session id 等），UI 里灰色显示
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

    def stream(self, prompt: str, workdir: str) -> AsyncIterator[AgentEvent]:
        """异步生成器：执行一轮对话，边执行边吐事件。"""
        ...


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


async def read_lines(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """从 StreamReader 逐行产出文本：读定长块、按 \n 手动切。

    不用 `async for line in stream`（底层 readline 有 64KB 单行上限，
    超长 JSON 行会直接抛 LimitOverrunError）——与 pi 的 rpc-process
    同一做法。stream_jsonl 和 acp/client 共用。
    """
    buf = b""
    while chunk := await stream.read(65536):
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                yield line
    tail = buf.decode("utf-8", errors="replace").strip()
    if tail:
        yield tail


async def stream_jsonl(cmd: list[str], workdir: str) -> AsyncIterator[str]:
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
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workdir,
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

"""Kimi Code CLI adapter.

无头调用方式（已实测）：
    kimi -p "<prompt>" --output-format stream-json

注意：-p 无头模式与 --auto / --yolo 互斥（实测报 "Cannot combine"），
但它本身就会直接执行工具调用（实测可写文件、无权限询问）。
意味着它可以在 workdir 里自由操作——务必在 git 仓库里用，
方便 git diff 验收与回滚。

输出是 JSONL，实测样例：
    {"role":"assistant","content":"PONG"}
    {"role":"meta","type":"session.resume_hint","session_id":"session_xxx",
     "command":"kimi -r session_xxx", "content":"..."}

- role=assistant → 正文
- role=meta, type=session.resume_hint → 记录 session_id，可用于恢复会话
  （kimi -r <id> 或 kimi -S <id>；本 MVP 暂不使用，见 README「上下文共享」）
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from .base import AgentEvent, stream_jsonl


class KimiAdapter:
    name = "kimi"

    def __init__(self, use_resume: bool = False) -> None:
        self.session_id: str | None = None
        self.use_resume = use_resume  # True 时后续调用带上 -S <id> 延续会话

    async def stream(self, prompt: str, workdir: str) -> AsyncIterator[AgentEvent]:
        cmd = ["kimi", "-p", prompt, "--output-format", "stream-json"]
        if self.use_resume and self.session_id:
            cmd += ["--session", self.session_id]

        async for line in stream_jsonl(cmd, workdir):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # 非 JSON 行（进度提示等）直接忽略

            role = ev.get("role")
            if role == "assistant":
                yield AgentEvent("text", ev.get("content", ""))
            elif role == "meta" and ev.get("type") == "session.resume_hint":
                self.session_id = ev.get("session_id")
            # 其他 role（tool 调用等）MVP 阶段不展示

        yield AgentEvent("done")

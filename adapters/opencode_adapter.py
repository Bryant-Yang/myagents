"""OpenCode CLI adapter.

无头调用方式（已实测）：
    opencode run "<prompt>" --format json --dir <workdir> [--session <id>]

输出是 JSONL 事件流，实测样例：
    {"type":"step_start", "sessionID":"ses_xxx", ...}
    {"type":"text", "sessionID":"ses_xxx",
     "part":{"type":"text","text":"PONG", ...}}
    {"type":"step_finish", ...
     "part":{"reason":"stop","tokens":{"total":15826,"input":15798,...}}}

- type=text      → 正文（part.text）
- type=step_finish → 一轮结束，带 token 用量，作为 info 事件展示
- 每个事件都带 sessionID，记录下来可用 --session <id> 续会话

注意：opencode 内部会起一个本地 server 再执行任务，启动比 kimi 慢一点，
这是正常现象。
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from .base import AgentEvent, stream_jsonl


class OpenCodeAdapter:
    name = "opencode"

    def __init__(self, use_resume: bool = False) -> None:
        self.session_id: str | None = None
        self.use_resume = use_resume

    async def stream(self, prompt: str, workdir: str) -> AsyncIterator[AgentEvent]:
        cmd = ["opencode", "run", prompt, "--format", "json", "--dir", workdir]
        if self.use_resume and self.session_id:
            cmd += ["--session", self.session_id]

        async for line in stream_jsonl(cmd, workdir):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            if sid := ev.get("sessionID"):
                self.session_id = sid

            etype = ev.get("type")
            if etype == "text":
                text = ev.get("part", {}).get("text", "")
                if text:
                    yield AgentEvent("text", text)
            elif etype == "step_finish":
                tokens = ev.get("part", {}).get("tokens", {})
                total = tokens.get("total")
                if total is not None:
                    yield AgentEvent("info", f"tokens: {total}")
            # step_start / tool 事件等 MVP 阶段不展示

        yield AgentEvent("done")

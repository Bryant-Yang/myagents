"""Codex CLI adapter.

无头调用方式（已实测）：
    codex exec --cd <workdir> --sandbox workspace-write \
        --skip-git-repo-check --json "<prompt>"

输出是 JSONL 事件流，实测样例：
    {"type":"thread.started","thread_id":"019f..."}
    {"type":"turn.started"}
    {"type":"item.completed","item":{"type":"agent_message","text":"PONG"}}
    {"type":"turn.completed","usage":{"input_tokens":21440,...}}

- thread.started        → 记录 thread_id（可用 codex exec resume <id> 续会话）
- item.completed/agent_message → 正文
- item.completed/error  → 警告级信息（如 skills 预算提示），按 info 展示
- turn.completed        → token 用量，作为 info 事件

sandbox 三档：read-only（评审）/ workspace-write（干活）/ danger-full-access。
主持人用 read-only 实例（只读就够，也是 sandbox 概念的实战示范），
工人用默认的 workspace-write。
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from .base import AgentEvent, stream_jsonl


class CodexAdapter:
    name = "codex"

    def __init__(
        self,
        use_resume: bool = False,
        sandbox: str = "workspace-write",
        *,
        ephemeral: bool = False,
    ) -> None:
        if use_resume and ephemeral:
            raise ValueError("ephemeral Codex exec 不支持 resume")
        self.session_id: str | None = None
        self.use_resume = use_resume
        self.sandbox = sandbox
        self.ephemeral = ephemeral

    async def stream(self, prompt: str, workdir: str) -> AsyncIterator[AgentEvent]:
        if self.use_resume and self.session_id:
            cmd = ["codex", "exec", "resume", self.session_id, prompt,
                   "--json", "--skip-git-repo-check", "--sandbox", self.sandbox]
        else:
            cmd = ["codex", "exec"]
            if self.ephemeral:
                cmd.append("--ephemeral")
            cmd.extend([
                "--cd", workdir,
                "--sandbox", self.sandbox,
                "--skip-git-repo-check",
                "--json",
                prompt,
            ])

        async for line in stream_jsonl(cmd, workdir):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = ev.get("type")
            if etype == "thread.started":
                self.session_id = ev.get("thread_id")
            elif etype == "item.completed":
                item = ev.get("item", {})
                itype = item.get("type")
                if itype == "agent_message":
                    text = item.get("text", "")
                    if text:
                        yield AgentEvent("text", text)
                elif itype == "error":
                    msg = item.get("message", "")
                    # codex 每次调用都会带的 skills 预算提示，过滤掉避免刷屏
                    if msg and "skills context budget" not in msg:
                        yield AgentEvent("info", msg[:120])
                # reasoning / command_execution 等 MVP 阶段不展示
            elif etype == "turn.completed":
                usage = ev.get("usage", {})
                total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                if total:
                    yield AgentEvent("info", f"tokens: {total}")

        yield AgentEvent("done")

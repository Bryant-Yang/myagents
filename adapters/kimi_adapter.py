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
from pathlib import Path
from typing import AsyncIterator

from .base import AgentEvent, stream_jsonl


KIMI_READONLY_AGENT_FILE = Path(__file__).with_name(
    "kimi_readonly_fallback.md").resolve()


class KimiAdapter:
    name = "kimi"

    def __init__(
        self,
        use_resume: bool = False,
        *,
        agent_file: str | Path | None = None,
    ) -> None:
        self.session_id: str | None = None
        self.use_resume = use_resume  # True 时后续调用带上 -S <id> 延续会话
        self.agent_file = (
            None if agent_file is None else Path(agent_file).resolve())

    @classmethod
    def readonly_fallback(cls) -> "KimiAdapter":
        """构造 ACP prepare 失败时的受限 JSONL adapter。

        Kimi ``-p`` 会自动处理工具权限，所以安全边界不能只靠
        prompt。显式 agent file 用工具白名单把降级路径限制为代码
        阅读，禁止写入、命令、网络、Skill 和子 agent。
        """
        return cls(agent_file=KIMI_READONLY_AGENT_FILE)

    async def stream(self, prompt: str, workdir: str) -> AsyncIterator[AgentEvent]:
        cmd = ["kimi", "-p", prompt, "--output-format", "stream-json"]
        if self.use_resume and self.session_id:
            cmd += ["--session", self.session_id]
        elif self.agent_file is not None:
            if not self.agent_file.is_file():
                raise RuntimeError(
                    f"Kimi agent file 不存在：{self.agent_file}")
            cmd += ["--agent-file", str(self.agent_file)]

        async for line in stream_jsonl(cmd, workdir):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # 非 JSON 行（进度提示等）直接忽略

            role = ev.get("role")
            if role == "assistant":
                content = ev.get("content")
                if isinstance(content, str) and content:
                    yield AgentEvent("text", content)
            elif role == "meta" and ev.get("type") == "session.resume_hint":
                session_id = ev.get("session_id")
                if isinstance(session_id, str) and session_id:
                    self.session_id = session_id
            # 其他 role（tool 调用等）MVP 阶段不展示

        yield AgentEvent("done")

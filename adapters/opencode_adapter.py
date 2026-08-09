"""OpenCode CLI JSONL adapter。

生产 OpenCode 主路径在 ``AcpOpenCodeAdapter``；本 adapter 仅作为
ACP prepare 失败前的隔离只读 fallback，以及显式兼容调用。

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
from pathlib import Path
from typing import AsyncIterator

from .base import (
    AgentEvent,
    ExecutionMode,
    ReadOnlyFallbackError,
    stream_jsonl,
)


OPENCODE_READONLY_AGENT = "myagents-readonly-fallback"
OPENCODE_READONLY_CONFIG_FILE = Path(__file__).with_name(
    "opencode_readonly_fallback.json").resolve()
OPENCODE_READONLY_PERMISSION = {
    "*": "deny",
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
}


class OpenCodeAdapter:
    name = "opencode"

    def __init__(
        self,
        use_resume: bool = False,
        *,
        readonly_config_file: str | Path | None = None,
    ) -> None:
        self.session_id: str | None = None
        self.use_resume = use_resume
        self.readonly_config_file = (
            None if readonly_config_file is None
            else Path(readonly_config_file).resolve()
        )

    @classmethod
    def readonly_fallback(cls) -> "OpenCodeAdapter":
        """构造 ACP prepare 失败后的只读 JSONL adapter。

        OpenCode 无头模式默认允许大部分工具，因此不能仅靠 prompt 声明
        “只读”。该模式通过 inline config 选择专用 agent，并禁用项目配置、
        Claude 兼容层、外部 plugin 与自动升级；专用 agent 的 permission
        白名单只开放 read/glob/grep/list。
        """
        return cls(readonly_config_file=OPENCODE_READONLY_CONFIG_FILE)

    async def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        if (execution_mode is ExecutionMode.WORKSPACE_WRITE
                and self.readonly_config_file is not None):
            raise ReadOnlyFallbackError(
                "OpenCode 只读 JSONL fallback 不能承担 workspace_write 阶段")
        cmd = ["opencode", "run", prompt, "--format", "json", "--dir", workdir]
        env_overrides = None
        if execution_mode is ExecutionMode.READ_ONLY:
            readonly_config_file = OPENCODE_READONLY_CONFIG_FILE
            if not readonly_config_file.is_file():
                raise RuntimeError(
                    f"OpenCode 只读配置不存在：{readonly_config_file}")
            config_content = readonly_config_file.read_text(encoding="utf-8")
            try:
                json.loads(config_content)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"OpenCode 只读配置不是合法 JSON：{exc}") from exc
            cmd = ["opencode", "--pure", *cmd[1:]]
            cmd += ["--agent", OPENCODE_READONLY_AGENT]
            env_overrides = {
                "OPENCODE_CONFIG_CONTENT": config_content,
                "OPENCODE_PERMISSION": json.dumps(
                    OPENCODE_READONLY_PERMISSION,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                "OPENCODE_DISABLE_AUTOUPDATE": "1",
            }
        elif self.use_resume and self.session_id:
            cmd += ["--session", self.session_id]
        else:
            readonly_config_file = self.readonly_config_file
            if (readonly_config_file is not None
                    and not readonly_config_file.is_file()):
                raise RuntimeError(
                    f"OpenCode 只读配置不存在：{readonly_config_file}")
            if readonly_config_file is None:
                config_content = None
            else:
                config_content = readonly_config_file.read_text(
                encoding="utf-8")
            if config_content is not None:
                try:
                    json.loads(config_content)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"OpenCode 只读配置不是合法 JSON：{exc}") from exc
                cmd = ["opencode", "--pure", *cmd[1:]]
                cmd += ["--agent", OPENCODE_READONLY_AGENT]
                env_overrides = {
                    "OPENCODE_CONFIG_CONTENT": config_content,
                    "OPENCODE_PERMISSION": json.dumps(
                        OPENCODE_READONLY_PERMISSION,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                    "OPENCODE_DISABLE_AUTOUPDATE": "1",
                }

        async for line in stream_jsonl(
                cmd, workdir, env_overrides=env_overrides):
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

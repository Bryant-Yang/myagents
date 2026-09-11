"""Claude Code headless stream-json -> AgentAdapter bridge.

Claude Code 的官方编程化入口是 ``claude -p`` 的 stream-json 长连接，而不是
ACP。本 adapter 保持该厂商协议在深层模块内，同时暴露与其他 adapter 一致的
stateful session、权限、执行模式、取消与 no-replay 契约。权限决策经
``--permission-prompt-tool`` 指向的 stdio MCP 桥（permission_server.py）回到
TUI；未文档化的 ``control_request`` 线格式被明确禁用。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import stat
import shutil
import sys
import uuid as uuid_module
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Sequence

from adapters.base import (
    DEFAULT_AGENT_INACTIVITY_TIMEOUT,
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    AgentEvent,
    AgentHostCapability,
    ExecutionMode,
    redact_sensitive_text,
)
from agent_readiness import AgentReadiness, ReadinessState
from clipboard_image import MAX_CLIPBOARD_IMAGE_BYTES, prompt_images
from storage.store import default_state_root

from .client import (
    ClaudeResumeNotFoundError,
    ClaudeStreamClient,
    ClaudeStreamError,
)


CLAUDE_STREAM_TRANSPORT = "stream-json"
CLAUDE_PERMISSION_SERVER = "myagents-claude-permission"
CLAUDE_PERMISSION_TOOL = "request_permission"
CLAUDE_PERMISSION_SCRIPT = "permission_server.py"
# 必须与 permission_server.py 的 _SOCKET_ENV/_TOKEN_ENV 保持一致；红线 gate
# 会同时断言两侧字面量。
CLAUDE_PERMISSION_SOCKET_ENV = "MYAGENTS_CLAUDE_PERMISSION_SOCKET"
CLAUDE_PERMISSION_TOKEN_ENV = "MYAGENTS_CLAUDE_PERMISSION_TOKEN"
# 只读轮的内置工具闭集：--restricted 已移除代码执行工具与 WebFetch，这里再
# 把文件工具收口为只读三项。
CLAUDE_READ_ONLY_TOOLS = "Read,Glob,Grep"
CLAUDE_HARDENED_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
}

_CLAUDE_CLI_ENV = "MYAGENTS_CLAUDE_CLI"
_SESSION_TOKEN_PREFIX = "claude:v1:"
_SESSION_TOKEN_VERSION = "myagents.claude.session-token/v1"
_PROFILE_DEFAULT = "default"
_PROFILE_READ_ONLY = "read_only"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MAX_ENVELOPE_BYTES = 16 * 1024
_MAX_SOCKET_PAYLOAD_BYTES = 64 * 1024
_MAX_PERMISSION_INPUT_CHARS = 64 * 1024
_MAX_USER_SETTINGS_BYTES = 2 * 1024 * 1024
_MAX_TOOL_NAME_CHARS = 256
_MAX_PROMPT_IMAGES = 16
_PERMISSION_EVENT_QUEUE_LIMIT = 256
# 成功终态只有 result subtype == "success"（对齐 DSH 仅 end_turn 的规则）。
CLAUDE_SUCCESS_SUBTYPE = "success"


class ClaudeAdapterError(RuntimeError):
    """Claude transport, policy, or session contract failed."""


AgentPermissionHandler = Callable[[str, dict], Awaitable[dict]]
ClaudeClientFactory = Callable[..., ClaudeStreamClient]


@dataclass(frozen=True)
class ClaudeSessionPreparation:
    """Compatibility shape consumed by Orchestrator.stream_prepared."""

    session_id: str | None
    restored: bool
    load_failed: bool
    fresh: bool


def _b64decode_json(encoded: str) -> dict:
    if not encoded or len(encoded) > _MAX_ENVELOPE_BYTES * 2:
        raise ClaudeAdapterError("Claude session envelope 长度非法")
    padding = "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        if len(raw) > _MAX_ENVELOPE_BYTES:
            raise ClaudeAdapterError("Claude session envelope 超过大小上限")
        value = json.loads(raw.decode("utf-8"))
    except ClaudeAdapterError:
        raise
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClaudeAdapterError("Claude session envelope 不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ClaudeAdapterError("Claude session envelope 顶层必须是对象")
    return value


def _b64encode_json(value: dict) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _validate_permission_outcome(outcome: object, params: dict) -> dict:
    if not isinstance(outcome, dict):
        return {"outcome": "cancelled"}
    if outcome.get("outcome") == "cancelled":
        return {"outcome": "cancelled"}
    if outcome.get("outcome") != "selected":
        return {"outcome": "cancelled"}
    option_id = outcome.get("optionId")
    valid_ids = {
        item.get("optionId")
        for item in params.get("options", [])
        if isinstance(item, dict)
    }
    if isinstance(option_id, str) and option_id and option_id in valid_ids:
        return {"outcome": "selected", "optionId": option_id}
    return {"outcome": "cancelled"}


class ClaudeCodeAdapter:
    """Stateful adapter for ``claude -p`` stream-json with a fail-closed
    permission bridge.

    v1 明确不声明 ``interject``：bare CLI 没有文档化的 mid-turn steer 线格式，
    能力缺失时 orchestrator 按 ADR-0018 fail-closed 拒绝运行中插话。
    """

    name = "claude"
    stateful_session = True
    # A missing native session must not cause the orchestrator to replay old
    # tool-bearing assignments into a fresh Claude process.
    replay_history_on_fresh_session = False

    @classmethod
    def host_capability(cls) -> AgentHostCapability:
        # Host 专用构造器硬编码只读闭集；与 Codex 的最严谨形态一致。
        return AgentHostCapability(
            CLAUDE_STREAM_TRANSPORT, lambda: cls(host_read_only=True))

    def __init__(
        self,
        cmd: Sequence[str] = ("claude",),
        *,
        client_factory: ClaudeClientFactory = ClaudeStreamClient,
        state_root: Path | None = None,
        permission_handler: AgentPermissionHandler | None = None,
        cancel_timeout: float = 10.0,
        request_timeout: float = 30.0,
        inactivity_timeout: float = DEFAULT_AGENT_INACTIVITY_TIMEOUT,
        tool_inactivity_timeout: float = 900.0,
        host_read_only: bool = False,
    ) -> None:
        if not cmd:
            raise ValueError("Claude command 不能为空")
        if min(
            cancel_timeout,
            request_timeout,
            inactivity_timeout,
            tool_inactivity_timeout,
        ) <= 0:
            raise ValueError("Claude timeout 必须大于 0")
        self.session_id: str | None = None
        self._base_cmd = list(cmd)
        self._client_factory = client_factory
        configured_root = (
            state_root
            if state_root is not None
            else Path(os.environ.get(
                "MYAGENTS_CLAUDE_STATE_ROOT",
                str(default_state_root() / "claude-code"),
            ))
        )
        self._state_root = Path(configured_root).expanduser().resolve()
        self._permission_script = (
            Path(__file__).parent / CLAUDE_PERMISSION_SCRIPT).resolve()
        self._permission_handler = permission_handler
        self._attachment_root: Path | None = None
        self._cancel_timeout = cancel_timeout
        self._request_timeout = request_timeout
        self._inactivity_timeout = inactivity_timeout
        self._tool_inactivity_timeout = tool_inactivity_timeout
        self._host_read_only = bool(host_read_only)
        self._client: ClaudeStreamClient | None = None
        self._started = False
        self._closed = False
        self._active_profile: str | None = None
        self._active_workdir: str | None = None
        self._active_uuid: str | None = None
        self._lock = asyncio.Lock()
        self._active_event_queue: asyncio.Queue[AgentEvent] | None = None
        self._pending_permissions: set[str] = set()
        self._prompt_permission_gate: asyncio.Event | None = None
        self._permission_server: asyncio.AbstractServer | None = None
        self._permission_socket_path: Path | None = None
        self._permission_token = ""
        self._mcp_config_path: Path | None = None
        self._auth_settings_path: Path | None = None
        self._pending_resume_fallback = False

    @property
    def pid(self) -> int | None:
        return None if self._client is None else self._client.pid

    def set_permission_handler(
        self,
        handler: AgentPermissionHandler | None,
    ) -> None:
        self._permission_handler = handler

    def set_attachment_root(self, root: Path | None) -> None:
        self._attachment_root = None if root is None else Path(root).absolute()

    def _profile_for(self, mode: ExecutionMode) -> str:
        if mode is ExecutionMode.READ_ONLY or self._host_read_only:
            return _PROFILE_READ_ONLY
        # DEFAULT 与 WORKSPACE_WRITE 共用同一普通 profile：写权限由 Claude
        # Code 的 permission-mode default 加上 TUI 逐次弹窗收口。
        return _PROFILE_DEFAULT

    def _session_dir(self, workdir: str, profile: str) -> Path:
        # 短 key 与短 socket 名：macOS AF_UNIX 路径上限约 104 字节，权限桥
        # socket 必须在默认状态根下也放得下（与 control.sock 同一约束）。
        workspace_key = hashlib.sha256(
            workdir.encode("utf-8")
        ).hexdigest()[:16]
        target = self._state_root / workspace_key / profile
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(target, 0o700)
        return target.resolve()

    @staticmethod
    def _session_token(
        native_uuid: str,
        *,
        workdir: str,
        profile: str,
    ) -> str:
        return _SESSION_TOKEN_PREFIX + _b64encode_json({
            "version": _SESSION_TOKEN_VERSION,
            "uuid": native_uuid,
            "profile": profile,
            "workspace": hashlib.sha256(
                workdir.encode("utf-8")
            ).hexdigest(),
        })

    def _decode_resume_token(
        self,
        token: str | None,
        *,
        workdir: str,
        profile: str,
    ) -> str | None:
        """Return the native session UUID to resume, or ``None`` for fresh.

        工作区不匹配或格式损坏是硬错误；只有 execution profile 不匹配按
        跨 profile 规则回退 fresh（持久 session 不跨 profile 复用）。
        """
        if token is None:
            return None
        if not token.startswith(_SESSION_TOKEN_PREFIX):
            raise ClaudeAdapterError("Claude session checkpoint 格式不受支持")
        payload = _b64decode_json(token[len(_SESSION_TOKEN_PREFIX):])
        if set(payload) != {"version", "uuid", "profile", "workspace"}:
            raise ClaudeAdapterError("Claude session checkpoint 字段闭集不匹配")
        if payload.get("version") != _SESSION_TOKEN_VERSION:
            raise ClaudeAdapterError("Claude session checkpoint 版本不受支持")
        if payload.get("workspace") != hashlib.sha256(
                workdir.encode("utf-8")).hexdigest():
            raise ClaudeAdapterError("Claude session checkpoint 属于其他工作区")
        native_uuid = payload.get("uuid")
        if (not isinstance(native_uuid, str)
                or _UUID_RE.fullmatch(native_uuid) is None):
            raise ClaudeAdapterError("Claude session checkpoint 缺少合法 uuid")
        if payload.get("profile") != profile:
            # Execution profiles never reuse one another's native session.
            return None
        return native_uuid

    def _command(
        self,
        *,
        profile: str,
        mcp_config: Path | None,
        session_args: tuple[str, ...],
        auth_settings: Path | None = None,
    ) -> list[str]:
        command = [
            *self._base_cmd,
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--setting-sources", "",
            "--strict-mcp-config",
        ]
        if auth_settings is not None:
            command.extend(("--settings", str(auth_settings)))
        if profile == _PROFILE_READ_ONLY:
            command.extend((
                "--restricted",
                "--tools", CLAUDE_READ_ONLY_TOOLS,
                "--permission-prompts", "none",
                "--no-session-persistence",
            ))
        else:
            assert mcp_config is not None
            command.extend((
                "--mcp-config", str(mcp_config),
                "--permission-prompt-tool",
                f"mcp__{CLAUDE_PERMISSION_SERVER}__{CLAUDE_PERMISSION_TOOL}",
                "--permission-mode", "default",
            ))
        command.extend(session_args)
        command.append("--replay-user-messages")
        return command

    def _user_settings_auth(self) -> dict:
        """Cherry-pick auth keys from the user's settings.json.

        ``--setting-sources ""`` 会连同认证一起屏蔽用户 settings（GLM 等
        代理用户的 ANTHROPIC_BASE_URL/AUTH_TOKEN 就在 settings 的 env 块
        里，2026-09-11 真实探针实测）。这里只挑拣认证相关的三个键写进
        我们自己的 ``--settings`` 文件；permissions/hooks/enabledPlugins
        等永远不会加载，TUI 弹窗边界不变。
        """
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        base = Path(config_dir).expanduser() if config_dir else (
            Path.home() / ".claude")
        path = base / "settings.json"
        try:
            info = path.lstat()
        except OSError:
            return {}
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            return {}
        if info.st_size > _MAX_USER_SETTINGS_BYTES:
            return {}
        try:
            payload = path.read_bytes().decode("utf-8")
            value = json.loads(payload)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        auth: dict = {}
        env = value.get("env")
        if isinstance(env, dict) and env:
            cleaned = {
                key: item for key, item in env.items()
                if isinstance(key, str) and isinstance(item, str)
            }
            if cleaned:
                auth["env"] = cleaned
        for key in ("apiKeyHelper", "model"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                auth[key] = item
        return auth

    def _write_auth_settings(self, session_dir: Path) -> Path | None:
        auth = self._user_settings_auth()
        if not auth:
            return None
        payload = json.dumps(
            auth, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        target = session_dir / "auth-settings.json"
        temporary = target.with_name(
            f".{target.name}.{secrets.token_hex(6)}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        try:
            fd = os.open(temporary, flags, 0o600)
            os.write(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, target)
        except BaseException:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        with contextlib.suppress(OSError):
            os.chmod(target, 0o600)
        return target

    def _write_mcp_config(self, session_dir: Path, socket_path: Path) -> Path:
        """Write the bridge config as a 0600 file.

        token 不能内联在 ``--mcp-config <json>`` 上：argv 对本机其他进程
        可见（ps）。文件放在 myagents 私有 state 目录内，权限 0600。
        """
        payload = json.dumps({
            "mcpServers": {
                CLAUDE_PERMISSION_SERVER: {
                    "command": sys.executable,
                    "args": [str(self._permission_script)],
                    "env": {
                        CLAUDE_PERMISSION_SOCKET_ENV: str(socket_path),
                        CLAUDE_PERMISSION_TOKEN_ENV: self._permission_token,
                    },
                },
            },
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        target = session_dir / "mcp-config.json"
        temporary = target.with_name(
            f".{target.name}.{secrets.token_hex(6)}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        try:
            fd = os.open(temporary, flags, 0o600)
            os.write(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, target)
        except BaseException:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        with contextlib.suppress(OSError):
            os.chmod(target, 0o600)
        return target

    async def _start_permission_socket_locked(self, session_dir: Path) -> None:
        if self._permission_server is not None:
            return
        socket_path = session_dir / (
            "p-" + secrets.token_hex(4) + ".sock")
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()
        token = secrets.token_urlsafe(24)

        async def _handle(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await self._handle_permission_connection(reader, writer, token)

        try:
            server = await asyncio.start_unix_server(
                _handle, str(socket_path))
        except OSError as exc:
            raise ClaudeAdapterError(
                f"Claude 权限桥 socket 无法监听：{exc}") from exc
        self._permission_server = server
        self._permission_socket_path = socket_path
        self._permission_token = token
        with contextlib.suppress(OSError):
            os.chmod(socket_path, 0o600)

    async def _stop_permission_socket_locked(self) -> None:
        server = self._permission_server
        self._permission_server = None
        socket_path = self._permission_socket_path
        self._permission_socket_path = None
        self._permission_token = ""
        if server is not None:
            server.close()
            with contextlib.suppress(BaseException):
                await server.wait_closed()
        if socket_path is not None:
            with contextlib.suppress(OSError):
                socket_path.unlink()

    async def _handle_permission_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        token: str,
    ) -> None:
        call_id = "c" + secrets.token_hex(8)
        tool_name = ""
        reply: dict = {
            "behavior": "deny",
            "message": "myagents 权限桥请求处理失败",
        }
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            except TimeoutError:
                line = b""
            if (
                not line
                or len(line) > _MAX_SOCKET_PAYLOAD_BYTES
                or not line.endswith(b"\n")
            ):
                reply = {
                    "behavior": "deny",
                    "message": "myagents 权限桥请求帧非法",
                }
            else:
                parsed = json.loads(line.decode("utf-8", errors="strict"))
                input_value = self._validated_permission_request(
                    parsed, token)
                if input_value is None:
                    reply = {
                        "behavior": "deny",
                        "message": "myagents 权限桥请求未通过凭证或格式校验",
                    }
                else:
                    tool_name = parsed["toolName"]
                    reply = await self._answer_permission(
                        call_id, tool_name, input_value)
        except (UnicodeError, json.JSONDecodeError):
            reply = {
                "behavior": "deny",
                "message": "myagents 权限桥请求不是合法 JSON",
            }
        except BaseException:
            reply = {
                "behavior": "deny",
                "message": "myagents 权限桥请求处理失败",
            }
        finally:
            encoded = json.dumps(
                reply, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            try:
                writer.write(encoded)
                await writer.drain()
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            with contextlib.suppress(BaseException):
                writer.close()
                await writer.wait_closed()

    def _validated_permission_request(
        self,
        parsed: object,
        token: str,
    ) -> dict | None:
        if not isinstance(parsed, dict):
            return None
        if set(parsed) != {"version", "token", "toolName", "input"}:
            return None
        if parsed.get("version") != 1:
            return None
        supplied = parsed.get("token")
        if not isinstance(supplied, str):
            return None
        if not hmac.compare_digest(supplied, token):
            return None
        tool_name = parsed.get("toolName")
        if (
            not isinstance(tool_name, str)
            or not tool_name
            or len(tool_name) > _MAX_TOOL_NAME_CHARS
        ):
            return None
        input_value = parsed.get("input")
        if not isinstance(input_value, dict):
            return None
        if len(json.dumps(
                input_value, ensure_ascii=False)) > _MAX_PERMISSION_INPUT_CHARS:
            return None
        return input_value

    async def _answer_permission(
        self,
        call_id: str,
        tool_name: str,
        input_value: dict,
    ) -> dict:
        allow_id = f"allow_once:{call_id}"
        reject_id = f"reject_once:{call_id}"
        params = {
            "toolCall": {
                "toolCallId": call_id,
                "title": tool_name,
                "rawInput": input_value,
            },
            "options": [
                {
                    "optionId": allow_id,
                    "name": "允许一次",
                    "kind": "allow_once",
                },
                {
                    "optionId": reject_id,
                    "name": "拒绝",
                    "kind": "reject_once",
                },
            ],
            "_myagents_mirrors_permission_events": True,
        }
        gate = self._prompt_permission_gate
        if gate is None:
            return {
                "behavior": "deny",
                "message": "本轮尚未提交到模型，权限请求已被拒绝",
            }
        try:
            await asyncio.wait_for(
                gate.wait(), timeout=self._request_timeout)
        except (TimeoutError, asyncio.CancelledError):
            return {
                "behavior": "deny",
                "message": "权限请求等待超时，已按默认策略拒绝",
            }
        if self._prompt_permission_gate is not gate:
            return {
                "behavior": "deny",
                "message": "权限请求属于上一轮任务，已被拒绝",
            }

        self._pending_permissions.add(call_id)
        if not self._queue_permission_event(
                f"等待权限：{tool_name}", call_id):
            self._pending_permissions.discard(call_id)
            return {
                "behavior": "deny",
                "message": "权限事件通道不可用，已按默认策略拒绝",
            }
        try:
            if self._permission_handler is None:
                outcome: object = {"outcome": "cancelled"}
            else:
                outcome = self._permission_handler(self.name, params)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
            selected = _validate_permission_outcome(outcome, params)
        except BaseException:
            selected = {"outcome": "cancelled"}
        finally:
            self._pending_permissions.discard(call_id)

        if (selected.get("outcome") == "selected"
                and selected.get("optionId") == allow_id):
            self._queue_permission_event(
                f"权限已允许一次：{tool_name}", call_id)
            return {"behavior": "allow", "updatedInput": input_value}
        if (selected.get("outcome") == "selected"
                and selected.get("optionId") == reject_id):
            self._queue_permission_event(
                f"权限已拒绝：{tool_name}", call_id)
            return {
                "behavior": "deny",
                "message": f"用户拒绝了此次 {tool_name} 调用",
            }
        self._queue_permission_event(
            f"权限已取消或拒绝：{tool_name}", call_id)
        return {
            "behavior": "deny",
            "message": f"此次 {tool_name} 调用未获批准，已按默认策略拒绝",
        }

    def _queue_permission_event(self, text: str, tool_call_id: str) -> bool:
        queue = self._active_event_queue
        if queue is None:
            return False
        try:
            queue.put_nowait(AgentEvent(
                "permission",
                text,
                meta={"tool_call_id": tool_call_id},
            ))
        except asyncio.QueueFull:
            return False
        return True

    async def _prepare_locked(
        self,
        workdir: str,
        resume_session_id: str | None,
        mode: ExecutionMode,
    ) -> ClaudeSessionPreparation:
        if self._closed:
            raise ClaudeAdapterError("Claude adapter 已关闭")
        canonical_workdir = str(Path(workdir).expanduser().resolve(strict=True))
        if not Path(canonical_workdir).is_dir():
            raise ClaudeAdapterError("Claude workdir 不是目录")
        profile = self._profile_for(mode)

        if self._started:
            client = self._client
            if (self._active_profile == profile
                    and self._active_workdir == canonical_workdir
                    and client is not None and client.running):
                if profile == _PROFILE_DEFAULT:
                    assert self.session_id is not None
                    if (resume_session_id is not None
                            and resume_session_id != self.session_id):
                        raise ClaudeAdapterError(
                            "Claude 活跃 session 与 checkpoint 不一致")
                return ClaudeSessionPreparation(
                    self.session_id,
                    restored=resume_session_id is not None,
                    load_failed=False,
                    fresh=False,
                )
            # profile/workspace 边界，或上一轮遗留的死连接：一律重建。
            await self._reset_locked()
            if self._closed:
                raise ClaudeAdapterError("Claude adapter 已关闭")
            # A profile/workspace boundary always starts a new process.

        if profile == _PROFILE_DEFAULT:
            resume_uuid = self._decode_resume_token(
                resume_session_id,
                workdir=canonical_workdir,
                profile=profile,
            )
            durable_token = resume_session_id
        else:
            # 只读轮使用一次性进程与临时会话，但保留持久 checkpoint 原样，
            # 使后续普通轮仍能 --resume 原 durable session。
            self._decode_resume_token(
                resume_session_id,
                workdir=canonical_workdir,
                profile=_PROFILE_READ_ONLY,
            )
            resume_uuid = None
            durable_token = resume_session_id

        session_dir = self._session_dir(canonical_workdir, profile)
        # 认证对两个 profile 都必需（只读轮也要调模型）。
        auth_settings = self._write_auth_settings(session_dir)
        self._auth_settings_path = auth_settings
        if profile == _PROFILE_DEFAULT:
            if self._permission_script.is_symlink() or not (
                    self._permission_script.is_file()):
                raise ClaudeAdapterError("Claude 权限桥脚本缺失")
            await self._start_permission_socket_locked(session_dir)
        else:
            await self._stop_permission_socket_locked()

        if resume_uuid is not None:
            session_args = ("--resume", resume_uuid)
        elif profile == _PROFILE_DEFAULT:
            session_args = ("--session-id", str(uuid_module.uuid4()))
        else:
            session_args = ()
        mcp_config_path: Path | None = None
        if profile == _PROFILE_DEFAULT and self._permission_socket_path:
            mcp_config_path = self._write_mcp_config(
                session_dir, self._permission_socket_path)
            self._mcp_config_path = mcp_config_path
        try:
            if profile == _PROFILE_DEFAULT and mcp_config_path is None:
                raise ClaudeAdapterError("Claude 权限桥 socket 未就绪")
            command = self._command(
                profile=profile,
                mcp_config=mcp_config_path,
                session_args=session_args,
                auth_settings=auth_settings,
            )
            client = self._client_factory(
                command,
                cwd=canonical_workdir,
                env_overrides=dict(CLAUDE_HARDENED_ENV),
                interrupt_timeout=self._cancel_timeout,
            )
            self._client = client
            await client.start()
        except BaseException:
            await self._reset_locked()
            raise
        # Claude 是输入驱动启动：非法 flag / resume 未命中等启动失败都在
        # 首个 turn 的回执等待中浮出（见 stream_prepared 的回退逻辑）。
        native_uuid = resume_uuid if resume_uuid is not None else (
            session_args[1] if session_args else None)

        assert native_uuid is not None or profile == _PROFILE_READ_ONLY
        if profile == _PROFILE_DEFAULT:
            assert native_uuid is not None
            self.session_id = self._session_token(
                native_uuid,
                workdir=canonical_workdir,
                profile=profile,
            )
            self._active_uuid = native_uuid
        else:
            self.session_id = durable_token
            self._active_uuid = None
        self._active_profile = profile
        self._active_workdir = canonical_workdir
        # resume 未命中要到首个 turn 才从 CLI 浮出（输入驱动启动）；记录
        # 本次 prepare 是否带 --resume，供 stream_prepared 做一次回退。
        self._pending_resume_fallback = (
            profile == _PROFILE_DEFAULT and resume_uuid is not None
        )
        self._started = True
        return ClaudeSessionPreparation(
            self.session_id,
            restored=resume_uuid is not None,
            load_failed=False,
            fresh=True,
        )

    def _validate_init(self, init: object, profile: str) -> None:
        if not isinstance(init, dict):
            raise ClaudeAdapterError("Claude system/init 不是对象")
        servers = init.get("mcp_servers")
        server_names: set[str] = set()
        if isinstance(servers, list):
            for item in servers:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    server_names.add(item["name"])
                elif isinstance(item, str):
                    server_names.add(item)
        errors = init.get("mcp_server_errors")
        if profile == _PROFILE_DEFAULT:
            if CLAUDE_PERMISSION_SERVER not in server_names:
                raise ClaudeAdapterError(
                    "Claude 权限桥未在 system/init 中注册；"
                    "普通轮拒绝在无权限桥模式下启动")
            if errors:
                raise ClaudeAdapterError(
                    "Claude 权限桥连接失败："
                    + redact_sensitive_text(json.dumps(errors), limit=2000))
        else:
            if server_names:
                raise ClaudeAdapterError(
                    "Claude 只读轮出现了未预期的 MCP server")
            if errors:
                raise ClaudeAdapterError(
                    "Claude 只读轮报告了 MCP 错误："
                    + redact_sensitive_text(json.dumps(errors), limit=2000))

    def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        return self.stream_prepared(
            lambda _prep: prompt,
            workdir,
            execution_mode=execution_mode,
        )

    async def stream_prepared(
        self,
        make_prompt: Callable[[ClaudeSessionPreparation], str],
        workdir: str,
        resume_session_id: str | None = None,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        async with self._lock:
            prep = await self._prepare_locked(
                workdir,
                resume_session_id,
                execution_mode,
            )
            try:
                prompt = make_prompt(prep)
            except BaseException:
                if prep.fresh:
                    await self._reset_locked()
                raise
            session_info = None
            if prep.fresh:
                if self._active_profile == _PROFILE_READ_ONLY:
                    session_info = AgentEvent(
                        "info",
                        "Claude 只读轮已建立（独立临时会话，不写入历史）",
                        meta={
                            "sessionId": prep.session_id,
                            "transport": CLAUDE_STREAM_TRANSPORT,
                        },
                    )
                else:
                    verb = "已恢复" if prep.restored else "已建立"
                    session_info = AgentEvent(
                        "info",
                        f"Claude session {verb}",
                        meta={
                            "sessionId": prep.session_id,
                            "transport": CLAUDE_STREAM_TRANSPORT,
                        },
                    )
            inner = self._prompt_locked(prompt, prep.session_id)
            try:
                async for event in inner:
                    yield event
                    if event.kind == "delivery_committed":
                        permission_gate = self._prompt_permission_gate
                        if permission_gate is not None:
                            # Resuming after this internal event means the
                            # Orchestrator has durably advanced its no-replay
                            # cursor.  Only now may a waiting tool approval be
                            # shown or granted.
                            permission_gate.set()
                        if session_info is not None:
                            yield session_info
                            session_info = None
            except ClaudeResumeNotFoundError:
                # CLI 在读取 stdin 之前拒绝 --resume：本轮必然未执行，允许
                # 一次性回退 fresh session（cursor 增量仍生效，无历史重放）。
                if not self._pending_resume_fallback:
                    raise
                self._pending_resume_fallback = False
                await self._reset_locked()
                if self._closed:
                    raise ClaudeAdapterError("Claude adapter 已关闭")
                retry_prep = await self._prepare_locked(
                    workdir, None, execution_mode)
                try:
                    prompt = make_prompt(retry_prep)
                except BaseException:
                    if retry_prep.fresh:
                        await self._reset_locked()
                    raise
                if retry_prep.fresh:
                    yield AgentEvent(
                        "info",
                        "Claude resume 未命中，已回退新 session",
                        meta={
                            "sessionId": retry_prep.session_id,
                            "transport": CLAUDE_STREAM_TRANSPORT,
                        },
                    )
                retry_inner = self._prompt_locked(
                    prompt, retry_prep.session_id)
                try:
                    async for event in retry_inner:
                        yield event
                        if event.kind == "delivery_committed":
                            retry_gate = self._prompt_permission_gate
                            if retry_gate is not None:
                                retry_gate.set()
                finally:
                    with contextlib.suppress(RuntimeError):
                        await retry_inner.aclose()
            finally:
                with contextlib.suppress(RuntimeError):
                    await inner.aclose()

    async def _prompt_locked(
        self,
        prompt: str,
        session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        if self._client is None:
            raise ClaudeAdapterError("Claude client 尚未启动")
        images = prompt_images(
            prompt,
            self._attachment_root,
            max_total_bytes=MAX_CLIPBOARD_IMAGE_BYTES,
            max_images=_MAX_PROMPT_IMAGES,
        )
        if self._prompt_permission_gate is not None:
            raise ClaudeAdapterError("Claude permission checkpoint gate 已被占用")
        permission_gate = asyncio.Event()
        self._prompt_permission_gate = permission_gate
        permission_events: asyncio.Queue[AgentEvent] = asyncio.Queue(
            maxsize=_PERMISSION_EVENT_QUEUE_LIMIT,
        )
        self._active_event_queue = permission_events
        raw_stream = self._client.send_turn(prompt, images)
        raw_task = asyncio.create_task(anext(raw_stream))
        permission_task = asyncio.create_task(permission_events.get())
        committed = False
        delivery_maybe_sent = False
        settled = False
        buffered: list[AgentEvent] = []
        seen_thinking = False
        active_tool_ids: set[str] = set()
        try:
            while not settled:
                timeout = None
                if not self._pending_permissions:
                    timeout = (
                        self._tool_inactivity_timeout
                        if active_tool_ids
                        else self._inactivity_timeout
                    )
                done, _pending = await asyncio.wait(
                    {raw_task, permission_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    assert timeout is not None
                    phase = "活跃工具" if active_tool_ids else "stream"
                    raise AgentDeliveryUncertainError(
                        f"Claude {phase} 连续 {timeout:g} 秒无活动，"
                        "本轮结果不确定"
                    )
                if permission_task in done:
                    permission_event = permission_task.result()
                    permission_task = asyncio.create_task(
                        permission_events.get())
                    # Drain every permission event that is already ready so a
                    # fast-moving raw stream can never strand dialog updates
                    # behind the result frame.
                    ready_events = [permission_event]
                    while True:
                        try:
                            ready_events.append(
                                permission_events.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                    for event in ready_events:
                        if committed:
                            yield event
                        else:
                            buffered.append(event)
                    if raw_task not in done:
                        continue
                try:
                    payload = raw_task.result()
                except StopAsyncIteration as exc:
                    raise AgentDeliveryUncertainError(
                        "Claude stream 在 result 前结束") from exc
                raw_task = asyncio.create_task(anext(raw_stream))
                if not isinstance(payload, dict):
                    raise ClaudeAdapterError("Claude event 不是对象")
                raw_type = payload.get("type")
                if raw_type == "delivery_committed":
                    if committed:
                        raise ClaudeAdapterError("Claude 重复 delivery_committed")
                    try:
                        # Claude 的 init 在首条输入后才输出；此时必须已到，
                        # 普通轮在这里做无桥 fail-closed。
                        self._validate_init(
                            self._client.init_message
                            if self._client is not None else None,
                            self._active_profile or _PROFILE_DEFAULT,
                        )
                    except ClaudeAdapterError as exc:
                        # 回执已被消费：本轮在传输层已交付，模型可能已开始
                        # 执行。必须走 uncertain 让 orchestrator 推进
                        # no-replay cursor，否则同一条消息可被重投。
                        delivery_maybe_sent = True  # 该进程不得复用
                        raise AgentDeliveryUncertainError(
                            f"Claude 已交付但权限桥校验失败：{exc}") from exc
                    committed = True
                    yield AgentEvent("delivery_committed", meta={
                        "sessionId": session_id,
                        "transport": CLAUDE_STREAM_TRANSPORT,
                    })
                    for event in buffered:
                        yield event
                    buffered.clear()
                    continue
                if not committed:
                    # Client promises to buffer vendor events until the
                    # replay echo.  Reject a broken client rather than expose
                    # a pre-checkpoint side effect to the outer event sink.
                    raise ClaudeAdapterError(
                        "Claude 在 delivery_committed 前泄露了事件")
                if raw_type == "stream_event":
                    event = self._stream_event(payload)
                    if event is None:
                        continue
                    if event.kind == "text":
                        yield event
                    elif event.kind == "status":
                        if not seen_thinking:
                            seen_thinking = True
                            yield event
                    else:
                        yield event
                elif raw_type == "assistant":
                    synthetic_error = self._synthetic_assistant_error(payload)
                    if synthetic_error is not None:
                        # CLI 在 API 错误/未登录时本地合成一条 assistant
                        # 消息（model == "<synthetic>"）并仍返回 success
                        # result——若不拦截就会记成"成功但无正文"。
                        delivery_maybe_sent = True
                        raise AgentDeliveryUncertainError(
                            f"Claude 未完成模型调用：{synthetic_error}")
                    tool_events = self._assistant_tool_events(
                        payload, active_tool_ids)
                    for event in tool_events:
                        yield event
                    if not tool_events:
                        yield AgentEvent(
                            "activity", meta={"phase": "assistant"})
                elif raw_type == "user":
                    tool_events = self._tool_result_events(
                        payload, active_tool_ids)
                    for event in tool_events:
                        yield event
                elif raw_type == "system":
                    event = self._system_event(payload)
                    if event is not None:
                        yield event
                elif raw_type == "result":
                    if payload.get("subtype") != CLAUDE_SUCCESS_SUBTYPE:
                        detail = payload.get("result") or payload.get("subtype")
                        raise AgentDeliveryUncertainError(
                            "Claude 请求已提交但执行失败："
                            + redact_sensitive_text(
                                str(detail or "unknown"), limit=2000))
                    result_session = payload.get("session_id")
                    if (self._active_uuid is not None
                            and result_session != self._active_uuid):
                        raise AgentDeliveryUncertainError(
                            "Claude result 的 session 与当前 checkpoint 不一致")
                    settled = True
                    meta: dict = {
                        "sessionId": session_id,
                        "claudePid": self.pid,
                    }
                    if isinstance(payload.get("usage"), dict):
                        meta["usage"] = payload["usage"]
                    if isinstance(payload.get("total_cost_usd"), (int, float)):
                        meta["totalCostUsd"] = payload["total_cost_usd"]
                    yield AgentEvent("done", meta=meta)
                # 其他未知帧保持静默：Claude 的 stream-json 会随版本新增
                # 事件类型，未识别帧不能打断已提交轮次。
        except AgentDeliveryCancelledError:
            delivery_maybe_sent = True
            raise
        except asyncio.CancelledError as exc:
            delivery_maybe_sent = True
            # Once the native turn iterator is running, cancellation can race
            # with stdin drain before the replay echo reaches this adapter.
            # Treat the boundary conservatively: a cancelled user task must
            # never cause an automatic replay of a Claude turn that may
            # already be executing.
            raise AgentDeliveryCancelledError(
                "Claude turn 发送后被取消，禁止自动重投"
            ) from exc
        except AgentDeliveryUncertainError:
            delivery_maybe_sent = True
            raise
        except GeneratorExit:
            # The frame may already sit in Claude's stdin buffer even though
            # no echo was consumed; never reuse this process for the next
            # turn.
            delivery_maybe_sent = True
            raise
        except BaseException as exc:
            if committed:
                raise AgentDeliveryUncertainError(
                    f"Claude turn 已提交但未成功完成：{exc}"
                ) from exc
            raise
        finally:
            self._active_event_queue = None
            if self._prompt_permission_gate is permission_gate:
                self._prompt_permission_gate = None
            for task in (raw_task, permission_task):
                if not task.done():
                    task.cancel()
            for task in (raw_task, permission_task):
                with contextlib.suppress(BaseException):
                    await task
            with contextlib.suppress(BaseException):
                await raw_stream.aclose()
            if (committed or delivery_maybe_sent) and not settled:
                # A turn without a trustworthy result must never share a
                # process with the next turn.  Client-side cancellation
                # already attempts SIGINT; process rebuild is the final
                # single-writer/no-overlap guarantee.
                await self._reset_locked()

    def _stream_event(self, payload: dict) -> AgentEvent | None:
        """Map one ``stream_event`` frame to a text/status/activity event."""
        parent_tool_use_id = payload.get("parent_tool_use_id")
        event = payload.get("event")
        if not isinstance(event, dict):
            return None
        if parent_tool_use_id is not None:
            # Subagent partials stay out of the main reply body.
            return AgentEvent("activity", meta={"phase": "subagent"})
        event_type = event.get("type")
        if event_type == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                return None
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    return AgentEvent("text", text)
                return None
            if isinstance(delta_type, str) and delta_type.startswith("thinking"):
                return AgentEvent("status", "Claude 正在思考…")
            return None
        return AgentEvent("activity", meta={"phase": event_type})

    @staticmethod
    def _synthetic_assistant_error(payload: dict) -> str | None:
        """Return redacted error text when the CLI synthesized the message."""
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        if message.get("model") != "<synthetic>":
            return None
        content = message.get("content")
        chunks: list[str] = []
        if isinstance(content, list):
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "text"
                        and isinstance(block.get("text"), str)):
                    chunks.append(block["text"])
        detail = " ".join(chunk for chunk in chunks if chunk) or (
            "CLI 合成了错误消息")
        return redact_sensitive_text(detail, limit=400)

    def _assistant_tool_events(
        self,
        payload: dict,
        active_tool_ids: set[str],
    ) -> list[AgentEvent]:
        if payload.get("parent_tool_use_id") is not None:
            return []
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        events: list[AgentEvent] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_call_id = block.get("id")
            tool_name = block.get("name")
            if not isinstance(tool_call_id, str) or not isinstance(
                    tool_name, str):
                continue
            active_tool_ids.add(tool_call_id)
            args = block.get("input")
            command = ""
            if isinstance(args, dict) and isinstance(args.get("command"), str):
                command = redact_sensitive_text(args["command"])
            events.append(AgentEvent(
                "tool",
                tool_name,
                meta={
                    "tool_call_id": tool_call_id,
                    "status": "in_progress",
                    "command": command,
                },
            ))
        return events

    def _tool_result_events(
        self,
        payload: dict,
        active_tool_ids: set[str],
    ) -> list[AgentEvent]:
        if payload.get("parent_tool_use_id") is not None:
            return []
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        events: list[AgentEvent] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_call_id = block.get("tool_use_id")
            if not isinstance(tool_call_id, str):
                continue
            active_tool_ids.discard(tool_call_id)
            status = "failed" if block.get("is_error") else "completed"
            events.append(AgentEvent(
                "tool",
                "tool_result",
                meta={
                    "tool_call_id": tool_call_id,
                    "status": status,
                    "command": "",
                },
            ))
        return events

    def _system_event(self, payload: dict) -> AgentEvent | None:
        subtype = payload.get("subtype")
        if subtype == "api_retry":
            attempt = payload.get("attempt")
            detail = f"第 {attempt} 次" if isinstance(attempt, int) else ""
            return AgentEvent(
                "status",
                f"Claude 正在重试{detail}…",
                meta={"phase": "api_retry"},
            )
        if subtype == "permission_denied":
            return AgentEvent(
                "status",
                "Claude 权限已被拒绝（只读轮自动拒绝）",
                meta={"phase": subtype},
            )
        return AgentEvent("activity", meta={"phase": subtype})

    async def _reset_locked(self) -> None:
        client = self._client
        self._client = None
        permission_gate = self._prompt_permission_gate
        self._prompt_permission_gate = None
        if permission_gate is not None:
            permission_gate.set()
        if client is not None:
            with contextlib.suppress(BaseException):
                await client.close()
        await self._stop_permission_socket_locked()
        if self._mcp_config_path is not None:
            with contextlib.suppress(OSError):
                self._mcp_config_path.unlink()
        self._mcp_config_path = None
        if self._auth_settings_path is not None:
            with contextlib.suppress(OSError):
                self._auth_settings_path.unlink()
        self._auth_settings_path = None
        self._started = False
        self.session_id = None
        self._active_profile = None
        self._active_workdir = None
        self._active_uuid = None
        self._active_event_queue = None
        self._pending_permissions.clear()
        self._pending_resume_fallback = False

    async def aclose(self) -> None:
        self._closed = True
        client = self._client
        if client is not None:
            with contextlib.suppress(BaseException):
                await client.close()
        acquired = False
        try:
            await asyncio.wait_for(
                self._lock.acquire(), timeout=min(self._cancel_timeout, 1.0))
            acquired = True
            await self._reset_locked()
        except TimeoutError:
            pass
        finally:
            if acquired:
                self._lock.release()


def _find_claude_cli(
    *,
    environ: Mapping[str, str] | None = None,
    resolver: Callable[[str], str | None] | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    find = resolver or shutil.which
    explicit = env.get(_CLAUDE_CLI_ENV, "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ClaudeAdapterError(
                f"{_CLAUDE_CLI_ENV} 指向的路径无效：{explicit}") from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ClaudeAdapterError(
                f"{_CLAUDE_CLI_ENV} 指向的路径不可执行：{explicit}")
        return str(resolved)
    return find("claude")


def claude_readiness_probe(
    *,
    environ: Mapping[str, str] | None = None,
    resolver: Callable[[str], str | None] | None = None,
) -> AgentReadiness:
    """Pure-read probe: resolve the CLI path only, never execute it."""
    setup_hint = (
        "安装 Claude Code CLI 并完成登录，或用 MYAGENTS_CLAUDE_CLI 指向"
        "该可执行文件"
    )
    try:
        executable = _find_claude_cli(environ=environ, resolver=resolver)
    except ClaudeAdapterError as exc:
        return AgentReadiness(
            "claude", ReadinessState.INVALID, str(exc), setup_hint)
    if executable is None:
        return AgentReadiness(
            "claude",
            ReadinessState.NOT_FOUND,
            "当前进程 PATH 未检测到 claude",
            setup_hint,
        )
    return AgentReadiness(
        "claude",
        ReadinessState.READY,
        f"已检测到 CLI：{executable}",
        setup_hint,
        executable,
    )

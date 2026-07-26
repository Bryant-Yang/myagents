"""Client for the TUI-owned local control socket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import stat
import uuid
from pathlib import Path
from typing import Any

from control.server import (
    ENDPOINT_MODE,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
)
from storage.store import default_state_root, normalize_workdir, room_id_for


class ControlClientError(Exception):
    """Control client or discovery error."""


class ControlUnavailableError(ControlClientError):
    """No verified TUI control endpoint is available."""


class ControlRemoteError(ControlClientError):
    """The control server returned a stable protocol error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ControlClient:
    def __init__(self, workdir: str | Path,
                 state_root: str | Path | None = None) -> None:
        self.workdir = normalize_workdir(workdir)
        self.state_root = (
            Path(state_root).expanduser().resolve()
            if state_root is not None else default_state_root()
        )
        self.room_id = room_id_for(self.workdir)
        self.room_dir = self.state_root / "rooms" / self.room_id
        self.endpoint_path = self.room_dir / "endpoint.json"
        self.socket_path = self.room_dir / "control.sock"

    def _hint(self) -> str:
        return (
            "请先启动该房间 TUI："
            f".venv/bin/python main.py {self.workdir}"
        )

    def _read_endpoint(self) -> dict[str, Any]:
        try:
            info = self.endpoint_path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ControlUnavailableError(
                    f"endpoint 不是普通文件：{self.endpoint_path}；{self._hint()}")
            if stat.S_IMODE(info.st_mode) != ENDPOINT_MODE:
                raise ControlUnavailableError(
                    f"endpoint 权限不是 0600：{self.endpoint_path}；{self._hint()}")
            raw = self.endpoint_path.read_bytes()
        except FileNotFoundError as exc:
            raise ControlUnavailableError(
                f"未发现活跃 TUI endpoint：{self.endpoint_path}；{self._hint()}"
            ) from exc
        except OSError as exc:
            raise ControlUnavailableError(
                f"无法读取 TUI endpoint：{exc}；{self._hint()}") from exc
        if len(raw) > 64 * 1024:
            raise ControlUnavailableError(
                f"endpoint 文件异常过大；{self._hint()}")
        try:
            endpoint = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlUnavailableError(
                f"endpoint 内容损坏：{exc}；{self._hint()}") from exc
        expected_keys = {
            "protocol_version", "pid", "room_id", "workdir", "socket_path"}
        if not isinstance(endpoint, dict) or set(endpoint) != expected_keys:
            raise ControlUnavailableError(
                f"endpoint 字段不合法；{self._hint()}")
        if (endpoint["protocol_version"] != PROTOCOL_VERSION
                or not isinstance(endpoint["pid"], int)
                or isinstance(endpoint["pid"], bool)
                or endpoint["pid"] <= 0
                or endpoint["room_id"] != self.room_id
                or endpoint["workdir"] != self.workdir
                or endpoint["socket_path"] != str(self.socket_path)):
            raise ControlUnavailableError(
                f"endpoint 与目标房间不匹配；{self._hint()}")
        return endpoint

    async def call(self, method: str,
                   params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._read_endpoint()
        request_id = str(uuid.uuid4())
        request = {
            "id": request_id,
            "method": method,
            "params": {} if params is None else params,
        }
        raw = (json.dumps(
            request, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(raw) > MAX_REQUEST_BYTES:
            raise ControlClientError(
                f"control 请求超过 {MAX_REQUEST_BYTES} 字节上限")
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self.socket_path), limit=MAX_RESPONSE_BYTES + 1)
        except OSError as exc:
            raise ControlUnavailableError(
                f"无法连接活跃 TUI：{exc}；{self._hint()}") from exc
        try:
            writer.write(raw)
            await writer.drain()
            try:
                response_raw = await reader.readuntil(b"\n")
            except (asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError) as exc:
                raise ControlUnavailableError(
                    f"control 响应不完整或超过上限；{self._hint()}") from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if len(response_raw) > MAX_RESPONSE_BYTES:
            raise ControlUnavailableError("control 响应超过大小上限")
        try:
            response = json.loads(response_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlUnavailableError(
                f"control 响应不是合法 JSON：{exc}") from exc
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise ControlUnavailableError("control 响应 id/形状不合法")
        if set(response) == {"id", "error"}:
            error = response["error"]
            if (not isinstance(error, dict)
                    or set(error) != {"code", "message"}
                    or not isinstance(error["code"], str)
                    or not isinstance(error["message"], str)):
                raise ControlUnavailableError("control error 响应形状不合法")
            raise ControlRemoteError(error["code"], error["message"])
        if set(response) != {"id", "result"} \
                or not isinstance(response["result"], dict):
            raise ControlUnavailableError("control result 响应形状不合法")
        return response["result"]

    async def get_room(self) -> dict[str, Any]:
        return await self.call("room.get")

    async def read_timeline(self, after_seq: int = 0,
                            limit: int = 50) -> dict[str, Any]:
        return await self.call(
            "timeline.read", {"after_seq": after_seq, "limit": limit})

    async def submit(self, message: str,
                     request_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"message": message}
        if request_id is not None:
            params["request_id"] = request_id
        return await self.call("command.submit", params)

    async def get_command(self, command_id: str) -> dict[str, Any]:
        return await self.call("command.get", {"command_id": command_id})

    async def wait_command(self, command_id: str,
                           timeout: float = 30.0) -> dict[str, Any]:
        return await self.call(
            "command.wait", {"command_id": command_id, "timeout": timeout})

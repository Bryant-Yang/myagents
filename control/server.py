"""TUI-owned local control server.

The Unix socket is a private transport for the running TUI.  It only exposes
the existing RoomStore and CommandBus; it never creates an Orchestrator or
touches an agent session directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from control.command_bus import (
    MAX_WAIT_TIMEOUT,
    CommandBus,
    CommandBusClosedError,
    CommandBusError,
    CommandCapacityError,
    CommandNotFoundError,
    CommandValidationError,
)
from storage.store import DEFAULT_READ_LIMIT, MAX_READ_LIMIT

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
ENDPOINT_MODE = 0o600
SOCKET_MODE = 0o600


class ControlServerError(Exception):
    """Control server lifecycle error."""


class ControlBusyError(ControlServerError):
    """The room already has a live control listener."""


class _InvalidParams(ValueError):
    pass


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_keys(params: dict[str, Any], *, required: set[str],
                   optional: set[str] = frozenset()) -> None:
    missing = required - params.keys()
    extra = params.keys() - required - optional
    if missing:
        raise _InvalidParams(f"缺少参数：{', '.join(sorted(missing))}")
    if extra:
        raise _InvalidParams(f"未知参数：{', '.join(sorted(extra))}")


class ControlServer:
    """JSON-Lines control endpoint owned by one persistent ChatApp."""

    def __init__(self, orchestrator: Any, command_bus: CommandBus) -> None:
        store = getattr(orchestrator, "store", None)
        if store is None:
            raise ControlServerError("ControlServer 只支持 persistent room")
        self._orch = orchestrator
        self._store = store
        self._bus = command_bus
        self.socket_path = store.room_dir / "control.sock"
        self.endpoint_path = store.room_dir / "endpoint.json"
        self._server: asyncio.AbstractServer | None = None
        self._client_tasks: set[asyncio.Task[Any]] = set()
        self._owns_files = False
        self._socket_identity: tuple[int, int] | None = None
        self._endpoint_identity: tuple[int, int] | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        await self._clear_stale_files()
        try:
            try:
                server = await asyncio.start_unix_server(
                    self._handle_client,
                    path=str(self.socket_path),
                    limit=MAX_REQUEST_BYTES + 1,
                )
            except OSError as exc:
                if (exc.errno == errno.ENAMETOOLONG
                        or "path too long" in str(exc)):
                    raise ControlServerError(
                        f"control socket 路径过长（"
                        f"{len(os.fsencode(str(self.socket_path)))} 字节，"
                        "超出本机 AF_UNIX 路径上限，macOS 约 104 字节）："
                        f"{self.socket_path}；请通过 XDG_STATE_HOME 使用更短"
                        "的状态根目录，或注入更短的 state_root") from exc
                raise
            # 先登记再 chmod/write_endpoint：两者之间任一步失败都必须
            # 走下面的清理路径，绝不泄漏仍在监听的 server 或残留 socket
            self._server = server
            self._owns_files = True
            socket_stat = os.lstat(self.socket_path)
            self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            os.chmod(self.socket_path, SOCKET_MODE)
            self._write_endpoint()
        except BaseException:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
            if self._owns_files:
                self._unlink_if_owned(
                    self.endpoint_path, self._endpoint_identity)
                self._unlink_if_owned(
                    self.socket_path, self._socket_identity)
                self._owns_files = False
                self._socket_identity = None
                self._endpoint_identity = None
            raise

    async def aclose(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        current = asyncio.current_task()
        tasks = [task for task in self._client_tasks
                 if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_files:
            self._owns_files = False
            self._unlink_if_owned(
                self.endpoint_path, self._endpoint_identity)
            self._unlink_if_owned(
                self.socket_path, self._socket_identity)
            self._socket_identity = None
            self._endpoint_identity = None
            _fsync_dir(self._store.room_dir)

    async def _clear_stale_files(self) -> None:
        if os.path.lexists(self.socket_path):
            try:
                _, writer = await asyncio.open_unix_connection(
                    str(self.socket_path))
            except OSError as exc:
                if exc.errno not in {
                    errno.ENOENT, errno.ECONNREFUSED, errno.ENOTSOCK,
                }:
                    raise ControlServerError(
                        f"无法验证 control socket：{exc}") from exc
            else:
                writer.close()
                await writer.wait_closed()
                raise ControlBusyError(
                    f"房间已有活跃 control server：{self.socket_path}")
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.unlink()
            with contextlib.suppress(FileNotFoundError):
                self.endpoint_path.unlink()
            _fsync_dir(self._store.room_dir)
        elif self.endpoint_path.exists():
            # No socket can be listening at the advertised fixed path.
            self.endpoint_path.unlink()
            _fsync_dir(self._store.room_dir)

    def _write_endpoint(self) -> None:
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "room_id": self._store.room_id,
            "workdir": self._store.workdir,
            "socket_path": str(self.socket_path),
        }
        fd, tmp = tempfile.mkstemp(
            dir=self._store.room_dir, prefix=".endpoint.", suffix=".tmp")
        try:
            os.fchmod(fd, ENDPOINT_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.endpoint_path)
            os.chmod(self.endpoint_path, ENDPOINT_MODE)
            endpoint_stat = os.lstat(self.endpoint_path)
            self._endpoint_identity = (
                endpoint_stat.st_dev, endpoint_stat.st_ino)
            _fsync_dir(self._store.room_dir)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    @staticmethod
    def _unlink_if_owned(path: Path,
                         identity: tuple[int, int] | None) -> None:
        if identity is None:
            return
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError as exc:
                    line = exc.partial
                    if not line:
                        break
                except asyncio.LimitOverrunError:
                    await self._send(writer, self._error(
                        None, "INVALID_REQUEST",
                        f"请求超过 {MAX_REQUEST_BYTES} 字节上限"))
                    break
                if len(line) > MAX_REQUEST_BYTES:
                    await self._send(writer, self._error(
                        None, "INVALID_REQUEST",
                        f"请求超过 {MAX_REQUEST_BYTES} 字节上限"))
                    break
                response = await self._process_line(line)
                await self._send(writer, response)
                if reader.at_eof():
                    break
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if task is not None:
                self._client_tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _process_line(self, line: bytes) -> dict[str, Any]:
        request_id: str | int | None = None
        try:
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return self._error(
                    None, "INVALID_REQUEST", f"请求不是合法 UTF-8 JSON：{exc}")
            if not isinstance(request, dict):
                return self._error(None, "INVALID_REQUEST", "请求必须是 JSON object")
            if set(request) != {"id", "method", "params"}:
                return self._error(
                    request.get("id"), "INVALID_REQUEST",
                    "请求字段必须且只能是 id、method、params")
            request_id = request["id"]
            if (isinstance(request_id, bool)
                    or not isinstance(request_id, (str, int, type(None)))):
                return self._error(
                    None, "INVALID_REQUEST", "id 必须是 string、int 或 null")
            method = request["method"]
            params = request["params"]
            if not isinstance(method, str) or not method:
                return self._error(
                    request_id, "INVALID_REQUEST", "method 必须是非空字符串")
            if not isinstance(params, dict):
                return self._error(
                    request_id, "INVALID_PARAMS", "params 必须是 JSON object")
            result = await self._dispatch(method, params)
            return {"id": request_id, "result": result}
        except _InvalidParamsMethod as exc:
            return self._error(request_id, "METHOD_NOT_FOUND", str(exc))
        except _InvalidParams as exc:
            return self._error(request_id, "INVALID_PARAMS", str(exc))
        except CommandNotFoundError as exc:
            return self._error(request_id, "NOT_FOUND", str(exc))
        except CommandCapacityError as exc:
            return self._error(request_id, "CAPACITY", str(exc))
        except CommandBusClosedError as exc:
            return self._error(request_id, "BUS_CLOSED", str(exc))
        except (CommandValidationError, ValueError) as exc:
            return self._error(request_id, "INVALID_PARAMS", str(exc))
        except CommandBusError as exc:
            return self._error(request_id, "BUS_CLOSED", str(exc))
        except Exception:
            return self._error(
                request_id, "INTERNAL", "control server 内部错误")

    async def _dispatch(self, method: str,
                        params: dict[str, Any]) -> dict[str, Any]:
        if method == "room.get":
            _validate_keys(params, required=set())
            return {
                "protocol_version": PROTOCOL_VERSION,
                "active": True,
                "pid": os.getpid(),
                "room_id": self._store.room_id,
                "room_name": self._store.room_name,
                "session_name": self._store.session_name,
                "workdir": self._store.workdir,
                "agents": [
                    {"name": spec.name, "transport": spec.transport}
                    for spec in self._orch.specs
                ],
            }
        if method == "timeline.read":
            _validate_keys(
                params, required=set(), optional={"after_seq", "limit"})
            after_seq = params.get("after_seq", 0)
            limit = params.get("limit", DEFAULT_READ_LIMIT)
            if (isinstance(after_seq, bool)
                    or not isinstance(after_seq, int) or after_seq < 0):
                raise _InvalidParams("after_seq 必须是非负整数")
            if (isinstance(limit, bool) or not isinstance(limit, int)
                    or not 1 <= limit <= MAX_READ_LIMIT):
                raise _InvalidParams(
                    f"limit 必须是 1..{MAX_READ_LIMIT} 的整数")
            page = self._store.read(after_seq=after_seq, limit=limit)
            return {
                "items": [item.to_dict() for item in page["items"]],
                "has_more": page["has_more"],
                "next_after_seq": page["next_after_seq"],
            }
        if method == "events.read":
            _validate_keys(
                params, required=set(), optional={"after_seq", "limit"})
            after_seq = params.get("after_seq", 0)
            limit = params.get("limit", DEFAULT_READ_LIMIT)
            if (isinstance(after_seq, bool)
                    or not isinstance(after_seq, int) or after_seq < 0):
                raise _InvalidParams("after_seq 必须是非负整数")
            if (isinstance(limit, bool) or not isinstance(limit, int)
                    or not 1 <= limit <= MAX_READ_LIMIT):
                raise _InvalidParams(
                    f"limit 必须是 1..{MAX_READ_LIMIT} 的整数")
            page = self._store.read_events(
                after_seq=after_seq, limit=limit)
            return {
                "items": [item.to_dict() for item in page["items"]],
                "has_more": page["has_more"],
                "next_after_seq": page["next_after_seq"],
            }
        if method == "command.submit":
            _validate_keys(
                params, required={"message"}, optional={"request_id"})
            snapshot = await self._bus.submit(
                params["message"], request_id=params.get("request_id"))
            return snapshot.to_dict()
        if method == "command.get":
            _validate_keys(params, required={"command_id"})
            command_id = params["command_id"]
            if not isinstance(command_id, str) or not command_id:
                raise _InvalidParams("command_id 必须是非空字符串")
            return self._bus.get(command_id).to_dict()
        if method == "command.wait":
            _validate_keys(
                params, required={"command_id"}, optional={"timeout"})
            command_id = params["command_id"]
            timeout = params.get("timeout", MAX_WAIT_TIMEOUT)
            if not isinstance(command_id, str) or not command_id:
                raise _InvalidParams("command_id 必须是非空字符串")
            return await self._bus.wait(command_id, timeout=timeout)
        if method == "command.cancel":
            _validate_keys(params, required={"command_id"})
            command_id = params["command_id"]
            if not isinstance(command_id, str) or not command_id:
                raise _InvalidParams("command_id 必须是非空字符串")
            return (await self._bus.cancel(command_id)).to_dict()
        raise _InvalidParamsMethod(method)

    async def _send(self, writer: asyncio.StreamWriter,
                    response: dict[str, Any]) -> None:
        raw = (json.dumps(
            response, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(raw) > MAX_RESPONSE_BYTES:
            raw = (json.dumps(self._error(
                response.get("id"), "INTERNAL", "响应超过大小上限"),
                ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        writer.write(raw)
        await writer.drain()

    @staticmethod
    def _error(request_id: Any, code: str,
               message: str) -> dict[str, Any]:
        return {
            "id": request_id,
            "error": {"code": code, "message": message},
        }


class _InvalidParamsMethod(_InvalidParams):
    """Internal marker converted to METHOD_NOT_FOUND before the response."""

    def __init__(self, method: str) -> None:
        super().__init__(f"未知 method：{method}")

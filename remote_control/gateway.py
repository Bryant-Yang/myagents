"""本机 daemon 的最小远程伴侣网关。

网关本身不持有 room、Orchestrator、CommandBus 或 agent。它只把经过 Bearer
鉴权的有限 HTTP 操作翻译为 ControlClient 请求；权限只允许远端拒绝，批准必须
回到本机 attach TUI。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from control import ControlClient, ControlClientError

MAX_REMOTE_BODY_BYTES = 128 * 1024
_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1"})
_DEFAULT_HOST_HEADERS = frozenset({"127.0.0.1", "::1", "localhost"})


class RemoteGatewayError(ValueError):
    """Remote companion 配置或私有凭据错误。"""


def validate_remote_bind(host: str) -> str:
    clean = host.strip().lower() if isinstance(host, str) else ""
    if clean not in _LOOPBACK_BINDS:
        raise RemoteGatewayError(
            "remote 只允许绑定 127.0.0.1/::1；外部访问请使用 "
            "Tailscale Serve 等受信反向代理"
        )
    return clean


def _host_without_port(value: str) -> str:
    clean = value.strip().lower()
    if clean.startswith("["):
        end = clean.find("]")
        return clean[1:end] if end > 0 else ""
    if clean.count(":") > 1:
        return clean
    return clean.rsplit(":", 1)[0] if ":" in clean else clean


def _validate_allowed_hosts(values: tuple[str, ...]) -> frozenset[str]:
    hosts: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise RemoteGatewayError("allowed host 必须是非空主机名")
        host = _host_without_port(value)
        if not host or any(char in host for char in "/?#@*"):
            raise RemoteGatewayError(f"allowed host 不合法：{value!r}")
        hosts.add(host)
    return frozenset(hosts)


def _validate_token(token: str) -> str:
    if (not isinstance(token, str) or len(token) < 32
            or len(token) > 1024 or token.strip() != token
            or any(char.isspace() for char in token)):
        raise RemoteGatewayError("remote token 必须是 32..1024 位无空白字符串")
    return token


def load_or_create_remote_token(path: str | Path) -> str:
    """原子创建或读取 0600 token；拒绝 symlink 与异常文件。"""
    requested = Path(path).expanduser()
    parent = requested.parent.absolute()
    token_path = parent / requested.name
    created_parent = False
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        created_parent = True
    except FileExistsError:
        pass
    parent_info = os.lstat(parent)
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        raise RemoteGatewayError("remote token 目录必须是真实目录")
    if created_parent:
        os.chmod(parent, 0o700)
    elif stat.S_IMODE(parent_info.st_mode) != 0o700:
        raise RemoteGatewayError(
            "remote token 目录权限必须是 0700；不会自动修改已有目录")
    no_follow = getattr(os, "O_NOFOLLOW", 0)

    try:
        fd = os.open(
            token_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow,
        )
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        try:
            fd = os.open(
                token_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | no_follow,
                0o600,
            )
        except FileExistsError:
            return load_or_create_remote_token(token_path)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (token + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return token
    except OSError as exc:
        raise RemoteGatewayError(f"无法安全读取 remote token：{exc}") from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RemoteGatewayError("remote token 必须是普通文件")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise RemoteGatewayError("remote token 权限必须是 0600")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    if len(raw) > 4096:
        raise RemoteGatewayError("remote token 文件异常过大")
    try:
        return _validate_token(raw.decode("utf-8").strip())
    except UnicodeDecodeError as exc:
        raise RemoteGatewayError("remote token 不是合法 UTF-8") from exc


async def _read_json(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("application/json"):
        raise HTTPException(415, "请求必须使用 application/json")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_REMOTE_BODY_BYTES:
            raise HTTPException(413, "请求体超过 128 KiB")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"请求不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "请求必须是 JSON object")
    return value


def _exact_fields(
    value: dict[str, Any], *, required: set[str], optional: set[str] = frozenset()
) -> None:
    missing = required - value.keys()
    extra = value.keys() - required - optional
    if missing:
        raise HTTPException(400, f"缺少字段：{', '.join(sorted(missing))}")
    if extra:
        raise HTTPException(400, f"未知字段：{', '.join(sorted(extra))}")


def _query_int(
    request: Request, name: str, default: int, *, minimum: int, maximum: int
) -> int:
    raw = request.query_params.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise HTTPException(400, f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise HTTPException(400, f"{name} 必须在 {minimum}..{maximum} 之间")
    return value


def create_remote_app(
    client: Any,
    *,
    token: str,
    allowed_hosts: tuple[str, ...] = (),
) -> Starlette:
    """构造无状态 ASGI companion；client 必须是 ControlClient 兼容对象。"""
    secret = _validate_token(token)
    trusted_hosts = _DEFAULT_HOST_HEADERS | _validate_allowed_hosts(allowed_hosts)

    async def index(_request: Request) -> Response:
        return FileResponse(_STATIC_ROOT / "index.html", media_type="text/html")

    async def script(_request: Request) -> Response:
        return FileResponse(
            _STATIC_ROOT / "app.js", media_type="text/javascript")

    async def room(_request: Request) -> Response:
        return JSONResponse(await client.get_room())

    async def timeline(request: Request) -> Response:
        after_seq = _query_int(
            request, "after_seq", 0, minimum=0, maximum=2**63 - 1)
        limit = _query_int(request, "limit", 100, minimum=1, maximum=200)
        return JSONResponse(await client.read_timeline(after_seq, limit))

    async def events(request: Request) -> Response:
        after_seq = _query_int(
            request, "after_seq", 0, minimum=0, maximum=2**63 - 1)
        limit = _query_int(request, "limit", 100, minimum=1, maximum=200)
        return JSONResponse(await client.read_events(after_seq, limit))

    async def commands(request: Request) -> Response:
        limit = _query_int(request, "limit", 50, minimum=1, maximum=200)
        return JSONResponse(await client.list_commands(limit))

    async def submit(request: Request) -> Response:
        value = await _read_json(request)
        _exact_fields(value, required={"message", "request_id"})
        message = value["message"]
        request_id = value["request_id"]
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(400, "message 必须是非空字符串")
        if (not isinstance(request_id, str) or not request_id
                or len(request_id) > 128):
            raise HTTPException(400, "request_id 必须是 1..128 字符字符串")
        return JSONResponse(await client.submit(message, request_id=request_id))

    async def cancel(request: Request) -> Response:
        return JSONResponse(await client.cancel_command(
            request.path_params["command_id"]))

    async def steer(request: Request) -> Response:
        value = await _read_json(request)
        _exact_fields(value, required={"instruction"})
        instruction = value["instruction"]
        if not isinstance(instruction, str) or not instruction.strip():
            raise HTTPException(400, "instruction 必须是非空字符串")
        return JSONResponse(await client.steer_command(
            request.path_params["command_id"], instruction))

    async def permissions(_request: Request) -> Response:
        return JSONResponse(await client.list_permissions())

    async def deny_permission(request: Request) -> Response:
        return JSONResponse(await client.resolve_permission(
            request.path_params["request_id"], outcome="cancelled"))

    app = Starlette(routes=[
        Route("/", index, methods=["GET"]),
        Route("/app.js", script, methods=["GET"]),
        Route("/api/room", room, methods=["GET"]),
        Route("/api/timeline", timeline, methods=["GET"]),
        Route("/api/events", events, methods=["GET"]),
        Route("/api/commands", commands, methods=["GET"]),
        Route("/api/commands", submit, methods=["POST"]),
        Route("/api/commands/{command_id:str}/cancel", cancel, methods=["POST"]),
        Route("/api/commands/{command_id:str}/steer", steer, methods=["POST"]),
        Route("/api/permissions", permissions, methods=["GET"]),
        Route("/api/permissions/{request_id:str}/deny", deny_permission,
              methods=["POST"]),
    ])

    async def security_boundary(request: Request, call_next):
        host = _host_without_port(request.headers.get("host", ""))
        if host not in trusted_hosts:
            return JSONResponse({"error": "untrusted host"}, status_code=400)
        if request.url.path.startswith("/api/"):
            authorization = request.headers.get("authorization", "")
            expected = f"Bearer {secret}"
            if not hmac.compare_digest(authorization, expected):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            response = await call_next(request)
        except ControlClientError as exc:
            response = JSONResponse(
                {"error": str(exc)}, status_code=503)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'none'; frame-ancestors 'none'"
        )
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=security_boundary)
    return app


def run_remote_gateway(
    workdir: str | Path,
    *,
    session_name: str = "default",
    state_root: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    token_file: str | Path | None = None,
    allowed_hosts: tuple[str, ...] = (),
) -> None:
    """验证 daemon owner 后，在 loopback 启动 remote companion。"""
    bind = validate_remote_bind(host)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RemoteGatewayError("port 必须是 1..65535 的整数")
    client = ControlClient(
        workdir, state_root=state_root, session_name=session_name)

    import asyncio

    room = asyncio.run(client.get_room())
    if room.get("owner_kind") != "daemon":
        raise RemoteGatewayError("remote 只连接 daemon owner；请先启动 daemon")
    root = client.state_root
    path = (
        Path(token_file) if token_file is not None
        else root / "remote" / f"{client.room_id}.token"
    )
    token = load_or_create_remote_token(path)
    app = create_remote_app(client, token=token, allowed_hosts=allowed_hosts)
    display_host = f"[{bind}]" if ":" in bind else bind
    print(f"remote companion：http://{display_host}:{port}/#token={token}")
    print("仅监听本机；跨设备请通过 Tailscale Serve，并显式设置 --allowed-host")
    uvicorn.run(app, host=bind, port=port, access_log=False, log_level="warning")

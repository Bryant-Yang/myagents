"""持有一个持久 room 的后台运行 owner。

DaemonRuntime 是 M8 的进程内深模块：调用方只负责 start/wait/aclose；它内部
唯一拥有 Orchestrator、CommandBus、ControlServer、room lease 与权限等待。
TUI、Web 和其他客户端都只能经过 ControlClient 访问，不能创建第二个 owner。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from agent_readiness import AgentEnablementConfig
from control import (
    CommandBus,
    ControlClient,
    ControlUnavailableError,
    ControlServer,
    PermissionBroker,
)
from host_backend import HostBackendSelection, configured_default_host_backend
from orchestrator import AGENT_SPECS, AgentSpec, Orchestrator
from storage.store import (
    DEFAULT_SESSION_NAME,
    RoomStore,
    default_state_root,
    normalize_session_name,
    normalize_workdir,
    room_id_for,
)

_DAEMON_START_TIMEOUT = 8.0
_DAEMON_STOP_TIMEOUT = 8.0


class DaemonRuntime:
    """一个 room 的唯一后台 owner；客户端断开不改变其生命周期。"""

    def __init__(
        self,
        workdir: str | Path,
        *,
        session_name: str = DEFAULT_SESSION_NAME,
        state_root: str | Path | None = None,
        specs: tuple[AgentSpec, ...] = AGENT_SPECS,
        discover_agents: bool = True,
        default_host_backend: HostBackendSelection | None = None,
    ) -> None:
        selected_backend = (
            default_host_backend
            or (
                configured_default_host_backend()
                if discover_agents else HostBackendSelection.default()
            )
        )
        store = RoomStore(
            workdir,
            state_root=state_root,
            session_name=session_name,
            default_host_backend=selected_backend,
        )
        self.orch = Orchestrator(
            str(workdir),
            specs=specs,
            store=store,
            session_name=session_name,
            discover_agents=discover_agents,
            agent_enablement=(
                AgentEnablementConfig() if discover_agents else None
            ),
            default_host_backend=selected_backend,
        )
        self.permissions = PermissionBroker()
        self.orch.set_permission_handler(self.permissions.request)
        self.bus = CommandBus(self.orch)
        self._shutdown_requested = asyncio.Event()
        self.control = ControlServer(
            self.orch,
            self.bus,
            owner_kind="daemon",
            permission_broker=self.permissions,
            shutdown_sink=self.request_shutdown,
        )
        self._started = False
        self._closed = False

    @property
    def store(self) -> RoomStore:
        assert self.orch.store is not None
        return self.orch.store

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("DaemonRuntime 已关闭")
        if self._started:
            return
        self.bus.start()
        try:
            await self.control.start()
        except BaseException:
            await self.bus.aclose()
            await self.permissions.aclose()
            await self.orch.aclose()
            self._closed = True
            raise
        self._started = True

    def request_shutdown(self) -> None:
        self._shutdown_requested.set()

    async def wait_shutdown(self) -> None:
        if not self._started:
            raise RuntimeError("DaemonRuntime 尚未启动")
        await self._shutdown_requested.wait()

    async def serve(self) -> None:
        await self.start()
        try:
            await self.wait_shutdown()
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._shutdown_requested.set()
        errors: list[BaseException] = []
        # 先取消权限等待，确保 adapter prompt 能退出，再停接收和 bus。
        for closer in (
            self.permissions.aclose,
            self.control.aclose,
            self.bus.aclose,
            self.orch.aclose,
        ):
            try:
                await closer()
            except BaseException as exc:
                errors.append(exc)
        self._started = False
        if errors:
            cancellation = next(
                (item for item in errors
                 if isinstance(item, asyncio.CancelledError)),
                None,
            )
            if cancellation is not None:
                raise cancellation
            raise BaseExceptionGroup("DaemonRuntime 关闭失败", errors)


async def serve_daemon(
    workdir: str | Path,
    *,
    session_name: str = DEFAULT_SESSION_NAME,
    state_root: str | Path | None = None,
) -> None:
    """前台运行 daemon，并把 SIGINT/SIGTERM 映射到同一有界关闭路径。"""
    runtime = DaemonRuntime(
        workdir,
        session_name=session_name,
        state_root=state_root,
    )
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, runtime.request_shutdown)
            installed.append(signum)
    try:
        await runtime.serve()
    finally:
        for signum in installed:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(signum)


def _resolved_state_root(state_root: str | Path | None) -> Path:
    return (
        Path(state_root).expanduser().resolve()
        if state_root is not None else default_state_root()
    )


def _private_log_fd(
    workdir: str | Path,
    session_name: str,
    state_root: str | Path | None,
) -> tuple[int, Path]:
    normalized_workdir = normalize_workdir(workdir)
    root = _resolved_state_root(state_root)
    work_path = Path(normalized_workdir)
    if root == work_path or root.is_relative_to(work_path) \
            or work_path.is_relative_to(root):
        raise ValueError("daemon 状态根不得与工作目录重叠")
    log_dir = root / "daemon-logs"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(log_dir, 0o700)
    log_path = log_dir / (
        room_id_for(normalized_workdir, session_name) + ".log"
    )
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(
        log_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0) | no_follow,
        0o600,
    )
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(fd)
        raise ValueError("daemon 日志路径必须是普通文件")
    os.fchmod(fd, 0o600)
    return fd, log_path


def _daemon_command(
    workdir: str | Path,
    session_name: str,
    state_root: str | Path | None,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        str(normalize_workdir(workdir)),
        "--session",
        normalize_session_name(session_name),
    ]
    if state_root is not None:
        command.extend(["--state-root", str(_resolved_state_root(state_root))])
    return command


async def _wait_for_daemon(
    client: ControlClient,
    process: subprocess.Popen[bytes],
    timeout: float,
) -> dict:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            room = await client.get_room()
        except ControlUnavailableError as exc:
            last_error = exc
            await asyncio.sleep(0.05)
            continue
        if room.get("owner_kind") != "daemon":
            raise RuntimeError("目标 endpoint 不是 daemon owner")
        return room
    detail = f"：{last_error}" if last_error is not None else ""
    raise RuntimeError(f"daemon 未在 {timeout:g} 秒内就绪{detail}")


def _terminate_spawned_process(process: subprocess.Popen[bytes]) -> None:
    """只回收本次刚创建的 daemon child，不触碰 endpoint 中的原 owner。"""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def start_daemon_detached(
    workdir: str | Path,
    *,
    session_name: str = DEFAULT_SESSION_NAME,
    state_root: str | Path | None = None,
    timeout: float = _DAEMON_START_TIMEOUT,
) -> dict:
    """启动独立 process group，并只在受信 control endpoint 就绪后返回。"""
    if timeout <= 0:
        raise ValueError("timeout 必须为正数")
    normalized_session = normalize_session_name(session_name)
    fd, log_path = _private_log_fd(
        workdir, normalized_session, state_root
    )
    try:
        process = subprocess.Popen(
            _daemon_command(workdir, normalized_session, state_root),
            cwd=normalize_workdir(workdir),
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(fd)
    client = ControlClient(
        workdir,
        state_root=state_root,
        session_name=normalized_session,
    )
    try:
        room = asyncio.run(_wait_for_daemon(client, process, timeout))
    except BaseException:
        _terminate_spawned_process(process)
        raise RuntimeError(
            f"daemon 启动失败；详情见私有日志 {log_path}"
        ) from None
    if room.get("pid") != process.pid:
        _terminate_spawned_process(process)
        raise RuntimeError("daemon 启动失败：endpoint PID 与新进程不匹配")
    return {**room, "log_path": str(log_path)}


def daemon_status(
    workdir: str | Path,
    *,
    session_name: str = DEFAULT_SESSION_NAME,
    state_root: str | Path | None = None,
) -> dict:
    client = ControlClient(
        workdir, state_root=state_root, session_name=session_name
    )
    return asyncio.run(client.get_room())


async def _stop_daemon(
    client: ControlClient,
    *,
    timeout: float,
) -> dict:
    room = await client.get_room()
    if room.get("owner_kind") != "daemon":
        raise RuntimeError("目标房间由 TUI 持有，拒绝当作 daemon 停止")
    await client.shutdown_runtime()
    pid = room.get("pid")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        endpoint_gone = False
        try:
            await client.get_room()
        except ControlUnavailableError:
            endpoint_gone = True
        process_gone = True
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                process_gone = False
            else:
                process_gone = False
        if endpoint_gone and process_gone:
            return room
        await asyncio.sleep(0.05)
    raise RuntimeError("daemon 未在限定时间内完成关闭")


def stop_daemon(
    workdir: str | Path,
    *,
    session_name: str = DEFAULT_SESSION_NAME,
    state_root: str | Path | None = None,
    timeout: float = _DAEMON_STOP_TIMEOUT,
) -> dict:
    if timeout <= 0:
        raise ValueError("timeout 必须为正数")
    client = ControlClient(
        workdir, state_root=state_root, session_name=session_name
    )
    return asyncio.run(_stop_daemon(client, timeout=timeout))


def _module_main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="myagents room daemon")
    parser.add_argument("workdir")
    parser.add_argument("--session", default=DEFAULT_SESSION_NAME)
    parser.add_argument("--state-root")
    args = parser.parse_args()
    asyncio.run(serve_daemon(
        args.workdir,
        session_name=args.session,
        state_root=args.state_root,
    ))


if __name__ == "__main__":
    _module_main()

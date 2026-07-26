"""M3 local control socket tests.

Run: .venv/bin/python tests/test_m3_control.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.base import AgentEvent
from acp.adapter import AcpAdapter
from control import (
    CommandBus,
    ControlBusyError,
    ControlClient,
    ControlRemoteError,
    ControlServer,
    ControlServerError,
    ControlUnavailableError,
)
from control.server import MAX_REQUEST_BYTES
from orchestrator import Orchestrator
from storage.store import RoomStore

FAKE_ACP_SERVER = str(Path(__file__).parent / "fake_acp_server.py")


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def stream(self, prompt: str, workdir: str):
        self.calls.append(prompt)
        yield AgentEvent("text", f"reply-{len(self.calls)}")
        yield AgentEvent("done")


class Room:
    def __init__(self) -> None:
        # macOS AF_UNIX path is short (about 104 bytes); keep the injected
        # state_root under /tmp so room_dir/control.sock is representable.
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        root = Path(self.tmp.name)
        self.workdir = root / "work"
        self.state_root = root / "state"
        self.workdir.mkdir()
        self.store = RoomStore(self.workdir, state_root=self.state_root)
        self.orch = Orchestrator(str(self.workdir), store=self.store)
        self.agent = FakeAgent()
        self.orch.adapters["kimi"] = self.agent
        self.bus = CommandBus(self.orch)
        self.server = ControlServer(self.orch, self.bus)
        self.client = ControlClient(self.workdir, state_root=self.state_root)

    async def start(self) -> None:
        self.bus.start()
        await self.server.start()

    async def close(self) -> None:
        await self.server.aclose()
        await self.bus.aclose()
        await self.orch.aclose()

    def cleanup(self) -> None:
        self.tmp.cleanup()


async def raw_call(path: Path, payload: bytes) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(payload)
        await writer.drain()
        return json.loads((await reader.readline()).decode("utf-8"))
    finally:
        writer.close()
        await writer.wait_closed()


def test_protocol_roundtrip() -> None:
    async def run() -> None:
        room = Room()
        try:
            await room.start()
            info = await room.client.get_room()
            assert info["active"] is True
            assert info["room_id"] == room.store.room_id
            assert info["workdir"] == str(room.workdir.resolve())
            assert {"name": "kimi", "transport": "acp"} in info["agents"]

            first = await room.client.submit("@kimi first", request_id="same")
            duplicate = await room.client.submit(
                "@kimi must-not-run", request_id="same")
            second = await room.client.submit("@kimi second")
            assert duplicate["command_id"] == first["command_id"]
            assert second["command_id"] != first["command_id"]

            r1 = await room.client.wait_command(first["command_id"], timeout=5)
            r2 = await room.client.wait_command(second["command_id"], timeout=5)
            assert r1["status"] == r2["status"] == "completed"
            assert (await room.client.get_command(
                first["command_id"]))["status"] == "completed"
            assert len(room.agent.calls) == 2

            page1 = await room.client.read_timeline(limit=2)
            assert len(page1["items"]) == 2 and page1["has_more"] is True
            page2 = await room.client.read_timeline(
                after_seq=page1["next_after_seq"], limit=2)
            items = page1["items"] + page2["items"]
            assert [item["speaker"] for item in items] == [
                "user", "kimi", "user", "kimi"]
            assert [item["command_id"] for item in items] == [
                first["command_id"], first["command_id"],
                second["command_id"], second["command_id"],
            ]
        finally:
            await room.close()
            room.cleanup()

    asyncio.run(run())
    print("ok  control 五方法 + FIFO/idempotency + timeline pagination")


def test_permissions_cleanup_and_unavailable() -> None:
    async def run() -> None:
        room = Room()
        endpoint = room.server.endpoint_path
        sock = room.server.socket_path
        try:
            await room.start()
            assert stat.S_IMODE(endpoint.stat().st_mode) == 0o600
            assert stat.S_IMODE(sock.stat().st_mode) == 0o600
            await room.server.aclose()
            assert not endpoint.exists() and not sock.exists()
            try:
                await room.client.get_room()
                raise AssertionError("closed server 应不可用")
            except ControlUnavailableError as exc:
                assert "main.py" in str(exc)
            assert not room.server._client_tasks
        finally:
            await room.bus.aclose()
            await room.orch.aclose()
            room.cleanup()

    asyncio.run(run())
    print("ok  endpoint/socket 0600 + close 清理 + actionable unavailable")


def test_stale_recovery_and_active_refusal() -> None:
    async def run() -> None:
        room = Room()
        stale_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stale_listener.bind(str(room.server.socket_path))
            stale_listener.close()  # pathname remains, but no listener
            room.server.endpoint_path.write_text("{}", encoding="utf-8")
            os.chmod(room.server.endpoint_path, 0o600)
            await room.start()
            assert (await room.client.get_room())["active"] is True

            other = ControlServer(room.orch, room.bus)
            try:
                await other.start()
                raise AssertionError("active server 不应被抢占")
            except ControlBusyError:
                pass
            await other.aclose()  # failed starter must not delete owner files
            assert room.server.socket_path.exists()
            assert room.server.endpoint_path.exists()
            assert (await room.client.get_room())["active"] is True
        finally:
            stale_listener.close()
            await room.close()
            room.cleanup()

    asyncio.run(run())
    print("ok  stale 恢复 + active socket 不被第二 server 删除/抢占")


def test_protocol_errors_and_recovery() -> None:
    async def run() -> None:
        room = Room()
        try:
            await room.start()
            path = room.server.socket_path
            bad_json = await raw_call(path, b"{bad json}\n")
            assert bad_json["error"]["code"] == "INVALID_REQUEST"
            unknown = await raw_call(path, json.dumps({
                "id": 1, "method": "wat", "params": {},
            }).encode() + b"\n")
            assert unknown["error"]["code"] == "METHOD_NOT_FOUND"
            extra = await raw_call(path, json.dumps({
                "id": 2, "method": "room.get", "params": {"extra": 1},
            }).encode() + b"\n")
            assert extra["error"]["code"] == "INVALID_PARAMS"
            too_large = await raw_call(
                path, b"x" * (MAX_REQUEST_BYTES + 1) + b"\n")
            assert too_large["error"]["code"] == "INVALID_REQUEST"
            try:
                await room.client.get_command("does-not-exist")
                raise AssertionError("missing command 应返回 remote error")
            except ControlRemoteError as exc:
                assert exc.code == "NOT_FOUND"
            # Every malformed request above used its own connection; server remains live.
            assert (await room.client.get_room())["active"] is True
        finally:
            await room.close()
            room.cleanup()

    asyncio.run(run())
    print("ok  malformed/unknown/strict/oversize 稳定错误 + server 可恢复")


def test_discovery_does_not_create_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / "work"
        workdir.mkdir()
        state_root = Path(tmp) / "absent-state"
        client = ControlClient(workdir, state_root=state_root)
        assert not state_root.exists()

        async def run() -> None:
            try:
                await client.get_room()
                raise AssertionError("不存在的房间不应可用")
            except ControlUnavailableError as exc:
                assert "main.py" in str(exc)

        asyncio.run(run())
        assert not state_root.exists()
    print("ok  ControlClient 发现路径不创建状态目录")


def test_start_failure_leaves_no_listener_or_files() -> None:
    async def run() -> None:
        room = Room()
        real_chmod = os.chmod
        try:
            def boom(path, mode, *args, **kwargs):
                if str(path) == str(room.server.socket_path):
                    raise OSError("注入的 chmod 失败")
                return real_chmod(path, mode, *args, **kwargs)
            os.chmod = boom
            try:
                try:
                    await room.server.start()
                    raise AssertionError("chmod 失败应让 start 抛错")
                except OSError:
                    pass
            finally:
                os.chmod = real_chmod
            # 无残留：listening server 已关闭，socket/endpoint 文件已删
            assert room.server._server is None
            assert not room.server.socket_path.exists()
            assert not room.server.endpoint_path.exists()
            assert room.server._socket_identity is None
            # 没泄漏 listener 占位：立即重试可以干净启动
            await room.start()
            assert (await room.client.get_room())["active"] is True
        finally:
            os.chmod = real_chmod
            await room.close()
            room.cleanup()

    asyncio.run(run())
    print("ok  start 中途失败不泄漏 listener/socket，可干净重试")


def test_endpoint_commit_failure_cleans_owned_files() -> None:
    import control.server as server_module

    async def run() -> None:
        room = Room()
        real_fsync_dir = server_module._fsync_dir
        try:
            # _write_endpoint calls this after os.replace, so both endpoint and
            # listening socket already exist when the injected failure occurs.
            server_module._fsync_dir = lambda path: (
                (_ for _ in ()).throw(OSError("注入 endpoint dir fsync 失败")))
            try:
                await room.server.start()
                raise AssertionError("endpoint commit 失败应让 start 抛错")
            except OSError:
                pass
            assert room.server._server is None
            assert not room.server.socket_path.exists()
            assert not room.server.endpoint_path.exists()
            assert room.server._socket_identity is None
            assert room.server._endpoint_identity is None
        finally:
            server_module._fsync_dir = real_fsync_dir
            await room.close()
            room.cleanup()

    asyncio.run(run())
    print("ok  endpoint replace 后提交失败仍按 inode 清理 endpoint/socket")


def test_long_socket_path_actionable_error() -> None:
    async def run() -> None:
        # macOS sun_path 上限约 104 字节：构造超出上限的 room_dir
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="l" * 55) as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            orch = Orchestrator(str(workdir), store=store)
            bus = CommandBus(orch)
            server = ControlServer(orch, bus)
            assert len(os.fsencode(str(server.socket_path))) > 103
            try:
                try:
                    await server.start()
                    raise AssertionError("超长 socket 路径应 fail closed")
                except ControlServerError as exc:
                    assert "路径过长" in str(exc)
                    assert "XDG_STATE_HOME" in str(exc)
                assert not server.socket_path.exists()
                assert not server.endpoint_path.exists()
            finally:
                await server.aclose()
                await bus.aclose()
                await orch.aclose()

    asyncio.run(run())
    print("ok  AF_UNIX 超长路径给出可操作错误且不创建任何文件")


def test_textual_external_command_visibility() -> None:
    from main import ChatApp
    from textual.widgets import RichLog

    async def run() -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            state_root = root / "state"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=state_root)
            orch = Orchestrator(str(workdir), store=store)
            agent = FakeAgent()
            orch.adapters["kimi"] = agent
            app = ChatApp(str(workdir), orchestrator=orch)
            client = ControlClient(workdir, state_root=state_root)
            endpoint = store.room_dir / "endpoint.json"
            sock = store.room_dir / "control.sock"

            async with app.run_test() as pilot:
                await pilot.pause()
                first = await client.submit("@kimi external-1", request_id="ext")
                duplicate = await client.submit(
                    "@kimi ignored", request_id="ext")
                second = await client.submit("@kimi external-2")
                assert duplicate["command_id"] == first["command_id"]
                assert (await client.wait_command(
                    first["command_id"], timeout=5))["status"] == "completed"
                assert (await client.wait_command(
                    second["command_id"], timeout=5))["status"] == "completed"
                await pilot.pause()
                assert app.bus.get(
                    first["command_id"]).status.value == "completed"
                rendered = "\n".join(
                    str(line.text) for line in app.query_one(RichLog).lines)
                assert "@kimi external-1" in rendered
                assert "@kimi external-2" in rendered
                assert "reply-1" in rendered and "reply-2" in rendered
                assert endpoint.exists() and sock.exists()

            assert not endpoint.exists() and not sock.exists()
            assert app.bus._closed is True and orch._closed is True

    asyncio.run(run())
    print("ok  external control command 实时进入同一 Textual UI/bus 并随退出清理")


def test_external_permission_stays_in_tui() -> None:
    from main import ChatApp, PermissionScreen

    async def wait_until(pilot, predicate, timeout: float = 10) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() >= deadline:
                raise AssertionError("等待 TUI 权限弹窗超时")
            await pilot.pause()
            await asyncio.sleep(0.02)

    async def run() -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            state_root = root / "state"
            fake_state = root / "fake-acp-state"
            workdir.mkdir()
            old_state = os.environ.get("FAKE_ACP_STATE")
            os.environ["FAKE_ACP_STATE"] = str(fake_state)
            try:
                store = RoomStore(workdir, state_root=state_root)
                orch = Orchestrator(str(workdir), store=store)
                adapter = AcpAdapter(
                    "kimi", [sys.executable, FAKE_ACP_SERVER])
                orch.adapters["kimi"] = adapter
                app = ChatApp(str(workdir), orchestrator=orch)
                client = ControlClient(workdir, state_root=state_root)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    command = await client.submit("@kimi 需要 perm 一下")
                    await wait_until(
                        pilot, lambda: isinstance(app.screen, PermissionScreen))
                    # The external bridge has no permission bypass: the active
                    # TUI owns the decision and returns the selected option.
                    await pilot.click("#perm-opt-0")
                    result = await client.wait_command(
                        command["command_id"], timeout=10)
                    assert result["status"] == "completed"
                    await wait_until(
                        pilot,
                        lambda: not isinstance(app.screen, PermissionScreen))
                raw = fake_state.read_text(encoding="utf-8")
                assert "permission:" in raw
                assert '"outcome": "selected"' in raw
                assert adapter._started is False
            finally:
                if old_state is None:
                    os.environ.pop("FAKE_ACP_STATE", None)
                else:
                    os.environ["FAKE_ACP_STATE"] = old_state

    asyncio.run(run())
    print("ok  external command 的 ACP permission 仍由当前 TUI 弹窗决策")


if __name__ == "__main__":
    test_protocol_roundtrip()
    test_permissions_cleanup_and_unavailable()
    test_stale_recovery_and_active_refusal()
    test_protocol_errors_and_recovery()
    test_discovery_does_not_create_state()
    test_start_failure_leaves_no_listener_or_files()
    test_endpoint_commit_failure_cleans_owned_files()
    test_long_socket_path_actionable_error()
    test_textual_external_command_visibility()
    test_external_permission_stays_in_tui()
    print("\nM3 control core 全部通过")

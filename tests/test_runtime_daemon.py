"""M8 后台 owner 与可重新附着控制验收。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import AgentEvent, ExecutionMode
from control import ControlClient, ControlUnavailableError
from orchestrator import AgentSpec
from attached_tui import AttachedChatApp, AttachedPermissionScreen
from runtime_daemon import DaemonRuntime
from storage.store import room_id_for


class BlockingAdapter:
    name = "worker"
    session_id = None
    stateful_session = False
    started: asyncio.Event
    release: asyncio.Event

    async def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ):
        del prompt, workdir, execution_mode
        type(self).started.set()
        await type(self).release.wait()
        yield AgentEvent("text", "后台任务完成")
        yield AgentEvent("done")


class PermissionAdapter:
    name = "worker"
    session_id = None
    stateful_session = False
    requested: asyncio.Event

    def __init__(self) -> None:
        self._handler = None

    def set_permission_handler(self, handler) -> None:
        self._handler = handler

    async def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ):
        del prompt, workdir, execution_mode
        assert self._handler is not None
        type(self).requested.set()
        outcome = await self._handler(self.name, {
            "toolCall": {"title": "写入验收结果"},
            "options": [
                {
                    "optionId": "allow-once",
                    "kind": "allow_once",
                    "name": "允许一次",
                },
                {
                    "optionId": "reject-once",
                    "kind": "reject_once",
                    "name": "拒绝",
                },
            ],
        })
        yield AgentEvent("text", f"permission:{outcome['outcome']}")
        yield AgentEvent("done")


def test_daemon_keeps_command_alive_between_control_clients() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            state_root = root / "state"
            BlockingAdapter.started = asyncio.Event()
            BlockingAdapter.release = asyncio.Event()
            runtime = DaemonRuntime(
                workdir,
                state_root=state_root,
                specs=(AgentSpec("worker", "fake", BlockingAdapter),),
                discover_agents=False,
            )
            await runtime.start()
            first_client = ControlClient(workdir, state_root=state_root)
            room = await first_client.get_room()
            assert room["owner_kind"] == "daemon"
            submitted = await first_client.submit(
                "@worker 继续执行", request_id="detach-once")
            await asyncio.wait_for(BlockingAdapter.started.wait(), 1)

            # ControlClient 无持久连接；丢弃前一个客户端等同 UI detach，
            # daemon 仍是唯一 room owner。
            del first_client
            BlockingAdapter.release.set()
            attached_again = ControlClient(workdir, state_root=state_root)
            result = await attached_again.wait_command(
                submitted["command_id"], timeout=3)
            assert result["status"] == "completed"
            timeline = await attached_again.read_timeline()
            assert [item["speaker"] for item in timeline["items"]] == [
                "user", "worker"]
            assert timeline["items"][-1]["text"] == "后台任务完成"

            await runtime.aclose()
            try:
                await attached_again.get_room()
                raise AssertionError("daemon 关闭后 endpoint 必须不可用")
            except ControlUnavailableError:
                pass

    asyncio.run(run())


def test_daemon_cli_detaches_reports_status_and_stops() -> None:
    with TemporaryDirectory(dir="/tmp") as tmp:
        root = Path(tmp)
        workdir = root / "work"
        workdir.mkdir()
        state_root = root / "state"
        room_id = room_id_for(str(workdir.resolve()))
        endpoint = state_root / "rooms" / room_id / "endpoint.json"
        base = [
            sys.executable,
            str(ROOT / "main.py"),
            "daemon",
        ]
        pid: int | None = None
        try:
            started = subprocess.run(
                [
                    *base,
                    "start",
                    str(workdir),
                    "--state-root",
                    str(state_root),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            assert started.returncode == 0, started.stderr or started.stdout
            assert "daemon 已启动" in started.stdout
            payload = json.loads(endpoint.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            os.kill(pid, 0)

            duplicate = subprocess.run(
                [
                    *base,
                    "start",
                    str(workdir),
                    "--state-root",
                    str(state_root),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            assert duplicate.returncode != 0
            assert "daemon 启动失败" in duplicate.stderr
            assert json.loads(endpoint.read_text("utf-8"))["pid"] == pid
            os.kill(pid, 0)

            status = subprocess.run(
                [
                    *base,
                    "status",
                    str(workdir),
                    "--state-root",
                    str(state_root),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            assert status.returncode == 0, status.stderr or status.stdout
            assert f"PID {pid}" in status.stdout
            assert "后台运行中" in status.stdout

            stopped = subprocess.run(
                [
                    *base,
                    "stop",
                    str(workdir),
                    "--state-root",
                    str(state_root),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            assert stopped.returncode == 0, stopped.stderr or stopped.stdout
            assert "daemon 已停止" in stopped.stdout
            for _ in range(100):
                if not endpoint.exists():
                    break
                time.sleep(0.02)
            assert not endpoint.exists()
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pid = None
            else:
                raise AssertionError("daemon stop 后进程仍存在")
        finally:
            if pid is not None:
                try:
                    os.kill(pid, 15)
                except ProcessLookupError:
                    pass


def test_attached_tui_detaches_without_stopping_daemon() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            state_root = root / "state"
            BlockingAdapter.started = asyncio.Event()
            BlockingAdapter.release = asyncio.Event()
            runtime = DaemonRuntime(
                workdir,
                state_root=state_root,
                specs=(AgentSpec("worker", "fake", BlockingAdapter),),
                discover_agents=False,
            )
            await runtime.start()
            client = ControlClient(workdir, state_root=state_root)
            app = AttachedChatApp(client, poll_interval=0.02)
            async with app.run_test(size=(90, 24)) as pilot:
                composer = app.query_one("#attached-composer")
                composer.value = "@worker 后台继续"
                await pilot.press("enter")
                await asyncio.wait_for(BlockingAdapter.started.wait(), 1)
                status = str(app.query_one("#attached-status").render())
                assert "已附着" in status

            # Textual context 已退出，但 daemon endpoint 和任务仍活着。
            assert (await client.get_room())["owner_kind"] == "daemon"
            BlockingAdapter.release.set()
            for _ in range(100):
                page = await client.read_timeline()
                if any(item["speaker"] == "worker" for item in page["items"]):
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("后台任务未完成")

            restored = AttachedChatApp(client, poll_interval=0.02)
            async with restored.run_test(size=(90, 24)) as pilot:
                for _ in range(100):
                    rendered = "\n".join(
                        str(line.text)
                        for line in restored.query_one("#attached-chat").lines
                    )
                    if "后台任务完成" in rendered:
                        break
                    await pilot.pause(0.02)
                assert "后台任务完成" in rendered
            await runtime.aclose()

    asyncio.run(run())


def test_permission_wait_survives_detach_and_resolves_after_reattach() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            state_root = root / "state"
            PermissionAdapter.requested = asyncio.Event()
            runtime = DaemonRuntime(
                workdir,
                state_root=state_root,
                specs=(AgentSpec("worker", "fake", PermissionAdapter),),
                discover_agents=False,
            )
            await runtime.start()
            client = ControlClient(workdir, state_root=state_root)
            first = AttachedChatApp(client, poll_interval=0.02)
            async with first.run_test(size=(90, 24)) as pilot:
                composer = first.query_one("#attached-composer")
                composer.value = "@worker 请求权限"
                await pilot.press("enter")
                await asyncio.wait_for(PermissionAdapter.requested.wait(), 1)
                for _ in range(100):
                    if isinstance(first.screen, AttachedPermissionScreen):
                        break
                    await pilot.pause(0.02)
                assert isinstance(first.screen, AttachedPermissionScreen)

            pending = await client.list_permissions()
            assert len(pending["items"]) == 1

            second = AttachedChatApp(client, poll_interval=0.02)
            async with second.run_test(size=(90, 24)) as pilot:
                for _ in range(100):
                    if isinstance(second.screen, AttachedPermissionScreen):
                        break
                    await pilot.pause(0.02)
                assert isinstance(second.screen, AttachedPermissionScreen)
                await pilot.click("#attached-permission-0")
                for _ in range(100):
                    page = await client.read_timeline()
                    if any("permission:selected" in item["text"]
                           for item in page["items"]):
                        break
                    await pilot.pause(0.02)
                else:
                    raise AssertionError("权限解决后任务未完成")
            assert (await client.list_permissions())["items"] == []
            await runtime.aclose()

    asyncio.run(run())


def test_attached_tui_escape_cancels_active_without_stopping_daemon() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            state_root = root / "state"
            BlockingAdapter.started = asyncio.Event()
            BlockingAdapter.release = asyncio.Event()
            runtime = DaemonRuntime(
                workdir,
                state_root=state_root,
                specs=(AgentSpec("worker", "fake", BlockingAdapter),),
                discover_agents=False,
            )
            await runtime.start()
            client = ControlClient(workdir, state_root=state_root)
            app = AttachedChatApp(client, poll_interval=0.02)
            async with app.run_test(size=(90, 24)) as pilot:
                composer = app.query_one("#attached-composer")
                composer.value = "@worker 长任务"
                await pilot.press("enter")
                await asyncio.wait_for(BlockingAdapter.started.wait(), 1)
                await pilot.press("escape")
                for _ in range(100):
                    commands = await client.list_commands()
                    if commands["items"][-1]["status"] == "cancelled":
                        break
                    await pilot.pause(0.02)
                else:
                    raise AssertionError("Esc 未取消 daemon 中的当前任务")
            assert (await client.get_room())["owner_kind"] == "daemon"
            await runtime.aclose()

    asyncio.run(run())


if __name__ == "__main__":
    test_daemon_keeps_command_alive_between_control_clients()
    test_daemon_cli_detaches_reports_status_and_stops()
    test_attached_tui_detaches_without_stopping_daemon()
    test_permission_wait_survives_detach_and_resolves_after_reattach()
    test_attached_tui_escape_cancels_active_without_stopping_daemon()
    print("ok  daemon detach/reconnect 保持同一任务与时间线")

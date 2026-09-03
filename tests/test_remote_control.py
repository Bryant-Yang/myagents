"""M8 loopback remote companion 安全边界验收。"""

from __future__ import annotations

import asyncio
import subprocess
import stat
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TOKEN = "s" * 32

from remote_control.gateway import (
    RemoteGatewayError,
    create_remote_app,
    load_or_create_remote_token,
    validate_remote_bind,
)
from adapters.base import AgentEvent
from control import ControlClient
from orchestrator import AgentSpec
from runtime_daemon import DaemonRuntime


class FakeControlClient:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str | None]] = []
        self.cancelled: list[str] = []
        self.steered: list[tuple[str, str]] = []
        self.resolved: list[tuple[str, str, str | None]] = []

    async def get_room(self):
        return {"owner_kind": "daemon", "room_id": "room-1",
                "session_name": "default", "workdir": "/tmp/work",
                "agents": [
                    {"name": "kimi", "transport": "acp+jsonl",
                     "ready": True, "state": "ready"},
                    {"name": "qwen", "transport": "acp",
                     "ready": False, "state": "not_found"},
                    {"name": "host", "transport": "NATIVE-MODEL",
                     "ready": False, "state": "invalid"},
                ]}

    async def read_timeline(self, after_seq=0, limit=50):
        return {"items": [{"seq": 1, "speaker": "host", "text": "你好"}],
                "has_more": False, "next_after_seq": 1}

    async def read_events(self, after_seq=0, limit=50):
        return {"items": [], "has_more": False, "next_after_seq": after_seq}

    async def list_commands(self, limit=50):
        return {"items": [{"command_id": "cmd-1", "message": "hi",
                            "status": "running"}]}

    async def read_command_events(self, command_id, limit=80):
        assert command_id == "cmd-1"
        return {
            "command_id": command_id,
            "items": [
                {
                    "seq": 1,
                    "command_id": command_id,
                    "agent": "worker",
                    "kind": "tool",
                    "text": "读取 README.md · 已完成",
                    "created_at": "2026-09-02T01:02:03Z",
                },
                {
                    "seq": 2,
                    "command_id": command_id,
                    "agent": "worker",
                    "kind": "partial",
                    "text": "过程正文不在详情重复显示",
                    "created_at": "2026-09-02T01:02:04Z",
                },
            ],
            "total_count": 2,
            "omitted_count": 0,
            "kind_counts": {"tool": 1, "partial": 1},
            "agents": ["worker"],
            "partial_char_count": 42,
        }

    async def submit(self, message, request_id=None):
        self.submitted.append((message, request_id))
        return {"command_id": "cmd-2", "status": "queued"}

    async def cancel_command(self, command_id):
        self.cancelled.append(command_id)
        return {"command_id": command_id, "status": "cancelled"}

    async def steer_command(self, command_id, instruction):
        self.steered.append((command_id, instruction))
        return {"command_id": command_id, "accepted": 1}

    async def list_permissions(self):
        return {"items": [{"request_id": "perm-1", "agent": "kimi",
                            "tool_call": {"title": "写文件"},
                            "options": [{"option_id": "allow-1",
                                         "kind": "allow_once",
                                         "name": "允许一次"}]}]}

    async def resolve_permission(self, request_id, *, outcome, option_id=None):
        self.resolved.append((request_id, outcome, option_id))
        return {"request_id": request_id, "outcome": outcome}


class ReplyAdapter:
    name = "worker"
    session_id = None
    stateful_session = False

    async def stream(self, prompt, workdir, *, execution_mode=None):
        del prompt, workdir, execution_mode
        yield AgentEvent("text", "REMOTE_DAEMON_OK")
        yield AgentEvent("done")


def test_remote_api_requires_bearer_and_never_exposes_approval() -> None:
    async def run() -> None:
        client = FakeControlClient()
        app = create_remote_app(client, token=TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as http:
            index = await http.get("/")
            script = await http.get("/app.js")
            assert index.status_code == script.status_code == 200
            assert TOKEN not in index.text and TOKEN not in script.text
            assert "default-src 'self'" in index.headers[
                "content-security-policy"]
            assert (await http.get("/api/room")).status_code == 401
            headers = {"Authorization": f"Bearer {TOKEN}"}
            room = await http.get("/api/room", headers=headers)
            assert room.status_code == 200
            assert room.json()["owner_kind"] == "daemon"
            assert [item["name"] for item in room.json()["agents"]] == [
                "kimi", "qwen", "host"]
            assert room.json()["agents"][-1]["ready"] is False
            timeline = await http.get(
                "/api/timeline?after_seq=0&limit=20", headers=headers)
            assert timeline.json()["items"][0]["text"] == "你好"
            details = await http.get(
                "/api/commands/cmd-1/events?limit=80", headers=headers)
            assert details.status_code == 200
            assert details.json()["items"][0]["kind"] == "tool"
            submitted = await http.post(
                "/api/commands",
                headers=headers,
                json={"message": "继续", "request_id": "browser-uuid"},
            )
            assert submitted.status_code == 200
            assert client.submitted == [("继续", "browser-uuid")]
            assert (await http.post(
                "/api/commands/cmd-1/cancel", headers=headers
            )).status_code == 200
            assert client.cancelled == ["cmd-1"]
            assert (await http.post(
                "/api/commands/cmd-1/steer",
                headers=headers,
                json={"instruction": "先说明风险"},
            )).status_code == 200
            assert client.steered == [("cmd-1", "先说明风险")]

            permissions = await http.get("/api/permissions", headers=headers)
            assert permissions.json()["items"][0]["request_id"] == "perm-1"
            assert (await http.post(
                "/api/permissions/perm-1/approve", headers=headers
            )).status_code == 404
            denied = await http.post(
                "/api/permissions/perm-1/deny", headers=headers)
            assert denied.status_code == 200
            assert client.resolved == [("perm-1", "cancelled", None)]
            assert (await http.post(
                "/api/runtime/shutdown", headers=headers
            )).status_code == 404

    asyncio.run(run())


def test_remote_rejects_untrusted_hosts_and_oversize_json() -> None:
    async def run() -> None:
        client = FakeControlClient()
        app = create_remote_app(client, token=TOKEN)
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://evil.example"
        ) as http:
            assert (await http.get("/api/room", headers=headers)).status_code == 400
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as http:
            response = await http.post(
                "/api/commands",
                headers={**headers, "Content-Type": "application/json"},
                content=b'{"message":"' + b"x" * (129 * 1024) + b'"}',
            )
            assert response.status_code == 413

    asyncio.run(run())
    assert validate_remote_bind("127.0.0.1") == "127.0.0.1"
    assert validate_remote_bind("::1") == "::1"
    try:
        validate_remote_bind("0.0.0.0")
        raise AssertionError("remote gateway 禁止直接监听所有网卡")
    except RemoteGatewayError:
        pass


def test_remote_token_is_stable_private_and_not_a_symlink() -> None:
    with TemporaryDirectory(dir="/tmp") as tmp:
        root = Path(tmp)
        token_path = root / "remote" / "token"
        first = load_or_create_remote_token(token_path)
        second = load_or_create_remote_token(token_path)
        assert first == second and len(first) >= 32
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700

        token_path.unlink()
        token_path.symlink_to(root / "elsewhere")
        try:
            load_or_create_remote_token(token_path)
            raise AssertionError("token symlink 必须拒绝")
        except RemoteGatewayError:
            pass

        shared_parent = root / "shared"
        shared_parent.mkdir(mode=0o755)
        try:
            load_or_create_remote_token(shared_parent / "token")
            raise AssertionError("不得静默收紧用户已有目录权限")
        except RemoteGatewayError:
            pass
        assert stat.S_IMODE(shared_parent.stat().st_mode) == 0o755


def test_remote_static_client_never_interprets_agent_html() -> None:
    source = (ROOT / "remote_control/static/app.js").read_text("utf-8")
    assert "textContent" in source
    assert "innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "document.write" not in source
    assert "eval(" not in source
    assert "location.hash" in source and "history.replaceState" in source
    assert "localStorage" not in source
    assert "let refreshing" in source
    assert "/steer" in source and "/cancel" in source and "/deny" in source
    assert "/approve" not in source and "/runtime/shutdown" not in source
    assert "远程端只能拒绝" in source


def test_remote_client_state_model_behaviour() -> None:
    completed = subprocess.run(
        ["node", str(ROOT / "tests/remote_client_state_test.js")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_remote_api_controls_the_existing_daemon_owner() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            state_root = root / "state"
            runtime = DaemonRuntime(
                workdir,
                state_root=state_root,
                specs=(AgentSpec("worker", "fake", ReplyAdapter),),
                discover_agents=False,
            )
            await runtime.start()
            client = ControlClient(workdir, state_root=state_root)
            app = create_remote_app(client, token=TOKEN)
            transport = httpx.ASGITransport(app=app)
            headers = {"Authorization": f"Bearer {TOKEN}"}
            try:
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://127.0.0.1"
                ) as http:
                    submitted = await http.post(
                        "/api/commands",
                        headers=headers,
                        json={"message": "@worker 真实控制路径",
                              "request_id": "remote-daemon-once"},
                    )
                    assert submitted.status_code == 200, submitted.text
                    command_id = submitted.json()["command_id"]
                    result = await client.wait_command(command_id, timeout=3)
                    assert result["status"] == "completed"
                    timeline = await http.get(
                        "/api/timeline?limit=20", headers=headers)
                    assert [item["speaker"]
                            for item in timeline.json()["items"]] == [
                                "user", "worker"]
                    assert timeline.json()["items"][-1]["text"] \
                        == "REMOTE_DAEMON_OK"
            finally:
                await runtime.aclose()

    asyncio.run(run())


if __name__ == "__main__":
    test_remote_api_requires_bearer_and_never_exposes_approval()
    test_remote_rejects_untrusted_hosts_and_oversize_json()
    test_remote_token_is_stable_private_and_not_a_symlink()
    test_remote_static_client_never_interprets_agent_html()
    test_remote_client_state_model_behaviour()
    test_remote_api_controls_the_existing_daemon_owner()
    print("ok  remote bearer/loopback/deny-only/token 安全边界")

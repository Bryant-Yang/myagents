#!/usr/bin/env python3
"""Manual release evidence: real Kimi ACP through the stdio MCP bridge.

This is intentionally not part of the fast harness gate.  It uses the locally
installed ``kimi`` agent and may consume model quota.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from main import ChatApp
from orchestrator import Orchestrator
from storage.store import RoomStore

BRIDGE = str(ROOT / "myagents_mcp.py")


def kimi_acp_pids() -> set[int]:
    result = subprocess.run(
        ["pgrep", "-f", r"(^|/)kimi acp($| )"],
        capture_output=True, text=True, check=False)
    return {
        int(line) for line in result.stdout.splitlines() if line.strip().isdigit()
    }


async def wait_terminal(session: ClientSession, command_id: str,
                        total_timeout: float = 180) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"command {command_id} 未在 {total_timeout}s 内完成")
        result = await session.call_tool(
            "myagents_wait_command",
            {"command_id": command_id, "timeout": min(30, remaining)})
        if result.isError:
            raise RuntimeError(result.content[0].text)
        payload = result.structuredContent
        if not payload["timed_out"]:
            return payload


async def run_round(workdir: Path, state_root: Path, marker: str,
                    request_id: str) -> tuple[str, str]:
    store = RoomStore(workdir, state_root=state_root)
    orch = Orchestrator(str(workdir), store=store)
    app = ChatApp(str(workdir), orchestrator=orch)
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            BRIDGE,
            "--workdir", str(workdir),
            "--state-root", str(state_root),
        ],
        cwd=str(ROOT),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                submitted = await session.call_tool(
                    "myagents_send_message",
                    {
                        "message": (
                            f"@kimi 只回复 {marker}，不要调用任何工具，"
                            "不要添加解释。"
                        ),
                        "request_id": request_id,
                    },
                )
                if submitted.isError:
                    raise RuntimeError(submitted.content[0].text)
                command_id = submitted.structuredContent["command_id"]
                terminal = await wait_terminal(session, command_id)
                if terminal["status"] != "completed":
                    raise RuntimeError(
                        f"command {command_id} terminal={terminal}")
                timeline = await session.call_tool(
                    "myagents_read_timeline",
                    {"after_seq": 0, "limit": 200})
                if timeline.isError:
                    raise RuntimeError(timeline.content[0].text)
                records = timeline.structuredContent["items"]
                replies = [
                    item["text"] for item in records
                    if item["command_id"] == command_id
                    and item["speaker"] == "kimi"
                ]
                if len(replies) != 1 or marker not in replies[0]:
                    raise AssertionError(
                        f"真实 Kimi 回复不符合预期：{replies!r}")
        session_id = store.get_agent_state("kimi")["session_id"]
        if not session_id:
            raise AssertionError("真实 Kimi ACP session_id 未持久化")
        assert orch.adapters["kimi"]._started is True
    assert orch.adapters["kimi"]._started is False
    assert not (store.room_dir / "endpoint.json").exists()
    assert not (store.room_dir / "control.sock").exists()
    return session_id, command_id


async def main() -> None:
    before = kimi_acp_pids()
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="myagents-e2e-") as tmp:
        root = Path(tmp)
        workdir = root / "work"
        state_root = root / "state"
        workdir.mkdir()

        session1, command1 = await run_round(
            workdir, state_root, "M3_REAL_ONE", "real-round-1")
        # Reconstruct RoomStore, Orchestrator, TUI, ACP process, and MCP bridge.
        # The persisted session id must be loaded rather than replaced.
        session2, command2 = await run_round(
            workdir, state_root, "M3_REAL_TWO", "real-round-2")
        if session2 != session1:
            raise AssertionError(
                f"session/load 未复用：first={session1}, second={session2}")
        if command2 == command1:
            raise AssertionError("两轮 command_id 不应相同")

        final_store = RoomStore(workdir, state_root=state_root)
        records = final_store.read(limit=200)["items"]
        if [record.seq for record in records] != list(
                range(1, len(records) + 1)):
            raise AssertionError("timeline seq 不连续")
        if [record.speaker for record in records] != [
                "user", "kimi", "user", "kimi"]:
            raise AssertionError(
                f"重启后 timeline 重复或丢失："
                f"{[(r.seq, r.speaker) for r in records]}")
        if [record.command_id for record in records] != [
                command1, command1, command2, command2]:
            raise AssertionError("timeline command_id 关联错误")

    await asyncio.sleep(0.2)
    leaked = kimi_acp_pids() - before
    if leaked:
        raise AssertionError(f"退出后残留 kimi acp PID：{sorted(leaked)}")
    print(
        "REAL M3 E2E OK "
        f"session={session1} commands={command1},{command2} records=4")


if __name__ == "__main__":
    asyncio.run(main())

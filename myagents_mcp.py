#!/usr/bin/env python3
"""Local stdio MCP bridge for one running myagents TUI room.

This process is deliberately thin: it discovers the TUI-owned Unix control
socket and translates MCP tools to that private protocol.  It never creates an
Orchestrator, starts the TUI, or grants agent permissions.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from control import ControlClient, ControlClientError


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
SEND_MESSAGE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)
CANCEL_COMMAND = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)
STEER_COMMAND = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


def build_server(client: ControlClient) -> FastMCP:
    server = FastMCP(
        "myagents",
        instructions=(
            "Connect to the already-running local myagents TUI room. "
            "Messages use the TUI's existing FIFO command bus and agent session."
        ),
        log_level="WARNING",
    )

    async def call(method: str,
                   params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await client.call(method, params)
        except ControlClientError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="myagents_get_room",
        description=(
            "Get the active local room, normalized workdir, PID, and configured "
            "agent transports. The TUI must already be running."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_room() -> dict[str, Any]:
        return await call("room.get")

    @server.tool(
        name="myagents_read_timeline",
        description=(
            "Read persisted room timeline records after a sequence cursor. "
            "Returns at most 200 records and a next_after_seq cursor."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def read_timeline(
        after_seq: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await call(
            "timeline.read", {"after_seq": after_seq, "limit": limit})

    @server.tool(
        name="myagents_read_events",
        description=(
            "Read persisted execution events such as running status, tool use, "
            "permission waits, heartbeats, partial output, and terminal state."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def read_events(
        after_seq: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await call(
            "events.read", {"after_seq": after_seq, "limit": limit})

    @server.tool(
        name="myagents_send_message",
        description=(
            "Submit a user message to the running TUI's FIFO command bus. "
            "Returns immediately with command_id. An optional request_id makes "
            "retries idempotent for this TUI process."
        ),
        annotations=SEND_MESSAGE,
        structured_output=True,
    )
    async def send_message(
        message: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"message": message}
        if request_id is not None:
            params["request_id"] = request_id
        return await call("command.submit", params)

    @server.tool(
        name="myagents_get_command",
        description=(
            "Get one submitted command's queued, running, completed, failed, "
            "or cancelled status."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def get_command(command_id: str) -> dict[str, Any]:
        return await call("command.get", {"command_id": command_id})

    @server.tool(
        name="myagents_wait_command",
        description=(
            "Wait up to 30 seconds for a command to become terminal. A timeout "
            "does not cancel the command."
        ),
        annotations=READ_ONLY,
        structured_output=True,
    )
    async def wait_command(
        command_id: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        return await call(
            "command.wait", {"command_id": command_id, "timeout": timeout})

    @server.tool(
        name="myagents_cancel_command",
        description=(
            "Cancel one queued or running command without stopping the room. "
            "Cancelling an already-terminal command is idempotent."
        ),
        annotations=CANCEL_COMMAND,
        structured_output=True,
    )
    async def cancel_command(command_id: str) -> dict[str, Any]:
        return await call(
            "command.cancel", {"command_id": command_id})

    @server.tool(
        name="myagents_steer_command",
        description=(
            "Add one bounded instruction to the active milestone workflow. "
            "It takes effect only at the next stage boundary and cannot "
            "change roles, permissions, or workflow bounds."
        ),
        annotations=STEER_COMMAND,
        structured_output=True,
    )
    async def steer_command(
        command_id: str,
        instruction: str,
    ) -> dict[str, Any]:
        return await call("command.steer", {
            "command_id": command_id,
            "instruction": instruction,
        })

    return server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect MCP stdio to one already-running myagents TUI room")
    parser.add_argument(
        "--workdir", required=True,
        help="Exact workdir used to start the target myagents TUI")
    parser.add_argument(
        "--session", default="default",
        help="Named myagents conversation session (default: default)")
    parser.add_argument(
        "--state-root", type=Path,
        help="Override myagents state root (primarily for isolated tests)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = ControlClient(
        args.workdir,
        state_root=args.state_root,
        session_name=args.session,
    )
    build_server(client).run(transport="stdio")


if __name__ == "__main__":
    main()

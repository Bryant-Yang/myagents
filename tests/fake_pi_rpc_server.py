"""Fake Pi RPC server used by the Python transport contract tests.

The real Pi RPC mode speaks LF-delimited JSON objects without JSON-RPC
headers.  This fixture intentionally exposes only the command shapes consumed
by ``PiRpcClient`` and records every inbound frame in ``FAKE_PI_RPC_STATE``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


STATE = os.environ.get("FAKE_PI_RPC_STATE", "/tmp/fake_pi_rpc_state")


def log(value: str) -> None:
    with open(STATE, "a", encoding="utf-8") as handle:
        handle.write(value + "\n")


def send(value: dict, *, newline: bool = True) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False))
    if newline:
        sys.stdout.write("\n")
    sys.stdout.flush()


def response(request: dict, *, data=None, success: bool = True,
             error: str = "") -> None:
    frame = {
        "id": request.get("id"),
        "type": "response",
        "command": request.get("type"),
        "success": success,
    }
    if success and data is not None:
        frame["data"] = data
    if not success:
        frame["error"] = error
    send(frame)


def text_events(text: str = "PONG") -> None:
    send({"type": "agent_start"})
    send({
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "text_delta",
            "delta": text,
        },
    })


def main() -> None:
    active_slow = False
    ignore_abort = False
    pending_extension_id: str | None = None
    extension_reuse_responses = 0

    for raw in sys.stdin:
        request = json.loads(raw)
        command = request.get("type")
        log("in:" + json.dumps(request, ensure_ascii=False, sort_keys=True))

        if command == "get_state":
            response(request, data={
                "thinkingLevel": "medium",
                "isStreaming": active_slow,
                "isCompacting": False,
                "steeringMode": "all",
                "followUpMode": "all",
                "sessionFile": "/tmp/fake-pi-session.jsonl",
                "sessionId": "fake-pi-session",
                "autoCompactionEnabled": True,
                "messageCount": 2,
                "pendingMessageCount": 0,
            })
            continue

        if command == "get_commands":
            response(request, data={"commands": [{
                "name": "myagents-policy",
                "description": "fake attestation command",
                "source": "extension",
                "sourceInfo": {"path": "/tmp/fake-policy.js"},
            }]})
            continue

        if command == "prompt":
            message = request.get("message", "")
            if message == "reject":
                response(request, success=False, error="preflight rejected")
                continue
            if message == "die-before-response":
                sys.stderr.write("before-response\n")
                sys.stderr.flush()
                os._exit(7)
            if message == "unterminated":
                send({"type": "agent_start"}, newline=False)
                return
            if message == "oversized":
                response(request)
                send({"type": "message_update", "payload": "x" * 4096})
                continue
            if message == "bad-json":
                response(request)
                sys.stdout.write("{not-json}\n")
                sys.stdout.flush()
                continue
            if message == "mismatched-response":
                send({
                    "id": request.get("id"),
                    "type": "response",
                    "command": "get_state",
                    "success": True,
                })
                continue
            if message == "event-byte-overflow":
                for index in range(2):
                    send({
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "text_delta",
                            "delta": f"{index}:" + "x" * 400,
                        },
                    })
                response(request)
                continue
            if message == "event-byte-release":
                response(request)
                for index in range(3):
                    send({
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "text_delta",
                            "delta": f"{index}:" + "x" * 400,
                        },
                    })
                    time.sleep(0.05)
                send({"type": "agent_settled"})
                continue
            if message == "large-user-echo":
                response(request)
                send({
                    "type": "message_start",
                    "message": {
                        "role": "user",
                        "content": [{
                            "type": "image",
                            "data": "x" * (2 * 1024 * 1024),
                        }],
                    },
                })
                send({"type": "agent_settled"})
                continue
            if message == "before-response":
                text_events("EARLY")
                send({"type": "agent_end"})
                response(request)
                send({"type": "turn_tail", "value": "AFTER_AGENT_END"})
                send({"type": "agent_settled"})
                continue
            if message in {"slow", "slow-no-abort"}:
                active_slow = True
                ignore_abort = message == "slow-no-abort"
                response(request)
                text_events("PARTIAL")
                continue
            if message == "extension":
                pending_extension_id = "ext-1"
                send({
                    "type": "extension_ui_request",
                    "id": pending_extension_id,
                    "method": "confirm",
                    "title": "Run edit",
                    "message": "Allow once?",
                    "timeout": 5000,
                })
                response(request)
                text_events("WHILE_WAITING")
                continue
            if message == "extension-overflow":
                for index in range(33):
                    send({
                        "type": "extension_ui_request",
                        "id": f"ext-overflow-{index}",
                        "method": "confirm",
                        "title": "Run edit",
                        "message": "Allow once?",
                        "timeout": 5000,
                    })
                response(request)
                continue
            if message == "extension-duplicate":
                for _index in range(2):
                    send({
                        "type": "extension_ui_request",
                        "id": "ext-duplicate",
                        "method": "confirm",
                        "title": "Run edit",
                        "message": "Allow once?",
                        "timeout": 5000,
                    })
                response(request)
                continue
            if message == "extension-id-reuse":
                pending_extension_id = "ext-reuse"
                extension_reuse_responses = 0
                send({
                    "type": "extension_ui_request",
                    "id": pending_extension_id,
                    "method": "confirm",
                    "title": "Run edit",
                    "message": "Allow once?",
                    "timeout": 5000,
                })
                response(request)
                text_events("REUSE")
                continue
            if message == "image":
                log("images:" + json.dumps(
                    request.get("images"), ensure_ascii=False,
                    sort_keys=True))
            response(request)
            text_events()
            send({"type": "agent_end"})
            send({"type": "agent_settled"})
            continue

        if command == "abort":
            if ignore_abort:
                continue
            response(request)
            if active_slow:
                active_slow = False
                send({"type": "agent_settled"})
            continue

        if command == "steer":
            if not active_slow:
                response(request, success=False, error="no active prompt")
                continue
            response(request)
            send({
                "type": "queue_update",
                "steering": [request.get("message", "")],
                "followUp": [],
            })
            text_events("STEERED")
            send({
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop"},
            })
            send({"type": "agent_end", "willRetry": False})
            active_slow = False
            send({"type": "agent_settled"})
            continue

        if command == "extension_ui_response":
            if request.get("id") == pending_extension_id:
                if (
                    pending_extension_id == "ext-reuse"
                    and extension_reuse_responses == 0
                ):
                    extension_reuse_responses = 1
                    time.sleep(0.05)
                    send({
                        "type": "extension_ui_request",
                        "id": pending_extension_id,
                        "method": "confirm",
                        "title": "Run edit again",
                        "message": "Allow once?",
                        "timeout": 5000,
                    })
                else:
                    pending_extension_id = None
                    send({"type": "agent_settled"})
            continue

        if command == "spawn_child":
            # Test-only command-line mode: this is never an RPC command emitted
            # by the client.  Kept here unreachable as a protocol tripwire.
            response(request, success=False, error="unsupported")
            continue

        response(request, success=False, error=f"unsupported command: {command}")


if __name__ == "__main__":
    child: subprocess.Popen | None = None
    if "--spawn-child" in sys.argv:
        child = subprocess.Popen([
            sys.executable,
            "-c",
            (
                "import os,signal,time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                "open(os.environ['FAKE_PI_CHILD'], 'w').write(str(os.getpid()));"
                "time.sleep(120)"
            ),
        ], start_new_session=True)
        log(f"child:{child.pid}")
    if "--ignore-term" in sys.argv:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif child is not None:
        def reap_tracked_child(_signum, _frame) -> None:
            # Real Pi tracks detached Bash processes and kills their process
            # trees from its SIGTERM/SIGHUP handler.  Mirror that topology
            # instead of relying on the parent's process-group kill.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, reap_tracked_child)
    if "--stderr-flood" in sys.argv:
        sys.stderr.write("HEAD" + "x" * 20000 + "TAIL")
        sys.stderr.flush()
    main()

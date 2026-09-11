"""Fake Claude Code stream-json process for transport contract tests.

The real headless mode speaks one NDJSON object per line on both stdin and
stdout, replays every accepted user frame back as an acknowledgment, and ends
each turn with exactly one ``result`` frame.  This fixture exposes only the
shapes consumed by ``ClaudeStreamClient`` and records every inbound frame in
``FAKE_CLAUDE_STREAM_STATE``.

Modes (argv[1]):
  normal            init + echo + text/tool events + success result
  no_echo           skip the replay echo (lost acceptance)
  events_before_echo  emit an event frame before the echo
  crash_before_init exit before system/init
  resume_not_found  exit with the exact documented resume-miss stderr
  slow_init         delay before system/init
  slow_turn         delay after the echo (for SIGINT tests)
  api_retry         emit an api_retry system frame mid-turn
  error_result      end the turn with error_during_execution
  wrong_session     result carries a different session id
  huge_frame        emit an oversized line
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time


STATE = os.environ.get("FAKE_CLAUDE_STREAM_STATE",
                       "/tmp/fake_claude_stream_state")
SESSION_ID = "2f0c8a52-1111-4222-8333-444455556666"
OTHER_SESSION_ID = "aaaa1111-2222-4333-8444-555566667777"


def log(value: str) -> None:
    with open(STATE, "a", encoding="utf-8") as handle:
        handle.write(value + "\n")


def send(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def text_of(request: dict) -> str:
    content = request.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
    return ""


def stream_events() -> None:
    send({"type": "stream_event", "event": {"type": "message_start"},
          "parent_tool_use_id": None, "session_id": SESSION_ID})
    send({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "FAKE "},
        },
        "parent_tool_use_id": None,
        "session_id": SESSION_ID,
    })
    send({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "toolu_1",
                "name": "Bash",
                "input": {"command": "echo hi"},
            }],
        },
        "parent_tool_use_id": None,
        "session_id": SESSION_ID,
    })
    send({
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "hi",
            }],
        },
        "parent_tool_use_id": None,
        "session_id": SESSION_ID,
    })
    send({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "CLAUDE"},
        },
        "parent_tool_use_id": None,
        "session_id": SESSION_ID,
    })


def result(subtype: str = "success", session_id: str = SESSION_ID) -> None:
    send({
        "type": "result",
        "subtype": subtype,
        "is_error": subtype != "success",
        "result": "done" if subtype == "success" else "boom",
        "session_id": session_id,
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 3, "output_tokens": 5},
    })


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    log("mode:" + mode)
    if mode == "crash_before_init":
        sys.stderr.write("boom\n")
        sys.exit(3)
    if mode == "resume_not_found":
        sys.stderr.write(
            "No conversation found with session ID: whatever\n")
        sys.exit(1)
    if mode == "slow_init":
        time.sleep(3)

    def on_sigint(_signum, _frame) -> None:
        log("sigint")
        result()

    signal.signal(signal.SIGINT, on_sigint)

    # Claude 的 stream-json 输入模式是输入驱动的：首条 stdin 之前完全静默，
    # init 在第一条消息之后、replay 回执之前输出（2026-09-10 实测）。
    first_input = True
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        log("in:" + line)
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if first_input:
            first_input = False
            send({
                "type": "system",
                "subtype": "init",
                "model": "fake-model",
                "mcp_servers": [{"name": "myagents-claude-permission"}],
            })
            # 实测 init 与 replay 回执之间还有启动杂音帧。
            send({"type": "system", "subtype": "status", "status": "booting"})
        if mode == "events_before_echo":
            send({"type": "stream_event", "event": {"type": "message_start"},
                  "parent_tool_use_id": None, "session_id": SESSION_ID})
        if mode == "no_echo":
            # True lost acceptance: stay completely silent for this turn.
            continue
        if mode != "events_before_echo":
            echo = dict(request)
            echo["session_id"] = SESSION_ID
            send(echo)
        if mode == "slow_turn":
            # Sleep in short slices so the SIGINT handler runs promptly.
            for _ in range(600):
                time.sleep(0.05)
        stream_events()
        if mode == "api_retry":
            send({
                "type": "system",
                "subtype": "api_retry",
                "attempt": 1,
                "max_retries": 2,
                "retry_delay_ms": 100,
                "error": "overloaded",
            })
        if mode == "huge_frame":
            sys.stdout.write("x" * (3 * 1024 * 1024) + "\n")
            sys.stdout.flush()
        if mode == "error_result":
            result("error_during_execution")
        elif mode == "wrong_session":
            result("success", OTHER_SESSION_ID)
        else:
            result()
    sys.exit(0)


if __name__ == "__main__":
    main()

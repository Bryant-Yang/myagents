"""Local OpenAI-compatible fixture used by native-agent contract tests."""

from __future__ import annotations

import json
import threading
import time
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeOpenAICompatibleServer(AbstractContextManager):
    def __init__(self, models: tuple[str, ...] = ("fake-model",), *,
                 models_discovery: bool = True) -> None:
        self.models = models
        self.models_discovery = models_discovery
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="fake-openai-compatible",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> "FakeOpenAICompatibleServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def _handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self) -> None:
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError):
                    # Timeout/cancel tests intentionally close the client side
                    # while the fixture is still preparing a response.
                    return

            def log_message(self, _format: str, *_args) -> None:
                return

            def do_GET(self) -> None:
                if self.path != "/v1/models" or not fixture.models_discovery:
                    self._json(404, {"error": {"message": "not found"}})
                    return
                fixture.requests.append({
                    "method": "GET",
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                })
                self._json(200, {
                    "object": "list",
                    "data": [
                        {"id": model_id, "object": "model"}
                        for model_id in fixture.models
                    ],
                })

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    self._json(400, {"error": {"message": "invalid json"}})
                    return
                fixture.requests.append({
                    "method": "POST",
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "json": payload,
                })
                if self.path != "/v1/chat/completions":
                    self._json(404, {"error": {"message": "not found"}})
                    return
                if payload.get("model") not in fixture.models:
                    self._json(400, {
                        "error": {"message": "unknown exact model id"},
                    })
                    return
                messages = [
                    item for item in payload.get("messages", [])
                    if isinstance(item, dict)
                ]
                prompt = "\n".join(
                    str(item.get("content", "")) for item in messages)
                latest_prompt = next((
                    str(item.get("content", ""))
                    for item in reversed(messages)
                    if item.get("role") == "user"
                ), "")
                if "FAKE_SLOW" in prompt:
                    self._start_sse()
                    time.sleep(0.25)
                    self._send_completion(("late",))
                    return
                if "FAKE_PRE_RESPONSE_SLOW" in prompt:
                    time.sleep(0.25)
                    self._start_sse()
                    self._send_completion(("late",))
                    return
                if "FAKE_PARTIAL_EOF" in prompt:
                    self._start_sse()
                    self._send_data({
                        "choices": [{
                            "delta": {"content": "partial"},
                            "finish_reason": None,
                        }],
                    })
                    self.close_connection = True
                    return
                if "FAKE_HUGE_SSE" in prompt:
                    self._start_sse()
                    self.wfile.write(b"data: " + b"x" * (4 * 1024 * 1024 + 1))
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if "FAKE_ERROR_SECRET" in prompt:
                    secret = self.headers.get("Authorization", "")
                    self._json(500, {
                        "error": {"message": f"provider echoed {secret}"},
                    })
                    return
                self._start_sse()
                if "FAKE_DIRECT" in latest_prompt:
                    self._send_completion(("native direct answer",))
                    return
                if ("FAKE_ROUTE" in latest_prompt
                        and "请直接处理用户最新" in latest_prompt):
                    self._send_completion((json.dumps({
                        "targets": ["alpha"],
                        "reason": "fixture route",
                        "tasks": {
                            "alpha": "完整执行 fake 路由任务",
                        },
                        "role_changes": {"set": {}, "clear": []},
                    }, ensure_ascii=False),))
                    return
                if ("FAKE_DISCUSSION" in latest_prompt
                        and "请直接处理用户最新" in latest_prompt):
                    self._send_completion((json.dumps({
                        "discussion": {
                            "participants": ["alpha", "beta"],
                            "rounds": 1,
                            "moderator": "host",
                            "topic": "fixture bounded discussion",
                        },
                        "reason": "fixture discussion",
                    }, ensure_ascii=False),))
                    return
                if ("FAKE_DISCUSSION" in latest_prompt
                        and "主持人本轮明确任务" in latest_prompt):
                    self._send_completion(("native moderator summary",))
                    return
                if "FAKE_CONTEXT" in prompt:
                    user_turns = sum(
                        1 for item in payload.get("messages", [])
                        if isinstance(item, dict) and item.get("role") == "user"
                    )
                    self._send_completion((f"context-{user_turns}",))
                    return
                self._send_completion(("hello ", "native"))

            def _start_sse(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

            def _send_completion(self, chunks: tuple[str, ...]) -> None:
                for chunk in chunks:
                    self._send_data({
                        "choices": [{
                            "delta": {"content": chunk},
                            "finish_reason": None,
                        }],
                    })
                self._send_data({
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                })
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def _send_data(self, payload: dict[str, Any]) -> None:
                line = "data: " + json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":"))
                self.wfile.write(line.encode("utf-8") + b"\n\n")
                self.wfile.flush()

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)

        return Handler

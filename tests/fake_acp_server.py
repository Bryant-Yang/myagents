"""Fake ACP server：测试用的假 agent，stdio 上讲 NDJSON JSON-RPC。

行为约定（配合 tests/test_acp.py 的断言）：
- initialize：固定响应；若环境变量 FAKE_ACP_FAIL_INIT=1，回 error
- session/new / session/list / session/load：固定响应，记入状态文件
- session/prompt：
  - 有上一轮未完成（pending）→ 记 "VIOLATION:overlap"（配合串行化测试）
  - 含 "perm"：发 session/request_permission 反向请求，同步等应答并记录
  - 含 "slow-never"：发一个 chunk 后挂起，收到 cancel 也只记录、永不完成
  - 含 "slow"：发一个 chunk 后挂起；收到 cancel 后延迟
    FAKE_ACP_CANCEL_DELAY 秒（默认 0.3）回 stopReason=cancelled，
    记 "cancel-complete"（配合"下一轮不提前开始"测试）
  - 其他：发 thought + message chunk，再回 stopReason=end_turn
- session/cancel（通知）：记入状态文件
- 未知带 id 方法：-32601

状态文件路径由环境变量 FAKE_ACP_STATE 指定，测试据此断言 server 行为。
"""

import json
import os
import sys
import time

STATE = os.environ.get("FAKE_ACP_STATE", "/tmp/fake_acp_state")
SESSION_ID = "fake-session-1"
CANCEL_DELAY = float(os.environ.get("FAKE_ACP_CANCEL_DELAY", "0.3"))
FAIL_INIT = os.environ.get("FAKE_ACP_FAIL_INIT") == "1"
FAIL_NEW = os.environ.get("FAKE_ACP_FAIL_NEW") == "1"

# 挂起的 prompt：{"rid": 请求 id, "mode": "slow" | "never"}；同时最多一轮
PENDING: dict = {"rid": None, "mode": None}


def log(event: str) -> None:
    with open(STATE, "a") as f:
        f.write(event + "\n")


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def chunk(sid: str, text: str) -> None:
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": sid, "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text}}}})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)

        # 客户端对 server 反向请求的应答（权限以外的兜底；perm 走同步读）
        if "method" not in msg:
            continue

        method, params, rid = msg["method"], msg.get("params", {}), msg.get("id")

        if method == "initialize":
            if FAIL_INIT:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000, "message": "init boom"}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {"loadSession": True,
                                          "sessionCapabilities": {"list": {}}},
                    "authMethods": [],
                    "agentInfo": {"name": "fake-acp", "version": "0.1"},
                }})
        elif method == "session/new":
            log("new:" + params.get("cwd", ""))
            if FAIL_NEW:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000, "message": "new boom"}})
            else:
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"sessionId": SESSION_ID}})
        elif method == "session/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "sessions": [{"sessionId": SESSION_ID, "cwd": "/tmp"}]}})
        elif method == "session/load":
            log("load:" + params.get("sessionId", ""))
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/prompt":
            if PENDING["rid"] is not None:
                log("VIOLATION:overlap")
            text = params["prompt"][0]["text"]
            log("prompt:" + text)
            sid = params["sessionId"]
            if "perm" in text:
                send({"jsonrpc": "2.0", "id": 900,
                      "method": "session/request_permission",
                      "params": {"sessionId": sid,
                                 "toolCall": {"title": "写文件"},
                                 "options": [
                                     {"optionId": "allow", "kind": "allow_once",
                                      "name": "允许一次"},
                                     {"optionId": "deny", "kind": "reject_once",
                                      "name": "拒绝"},
                                 ]}})
                # 同步等客户端应答后再继续（测试 server，要确定性时序）
                resp = json.loads(sys.stdin.readline())
                log("permission:" + json.dumps(
                    resp.get("result", resp.get("error")), ensure_ascii=False))
            if "slow" in text:
                chunk(sid, "开始了")
                PENDING["rid"] = rid
                PENDING["mode"] = "never" if "never" in text else "slow"
                continue  # 挂起，等 session/cancel
            if "perm" not in text:
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": sid, "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "想想"}}}})
            chunk(sid, "PO")
            chunk(sid, "NG")
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            sid = params.get("sessionId", "")
            log("cancel:" + sid)
            if PENDING["rid"] is not None and PENDING["mode"] == "slow":
                # 延迟完成 cancel：模拟 agent 需要时间停下工具调用
                time.sleep(CANCEL_DELAY)
                send({"jsonrpc": "2.0", "id": PENDING["rid"],
                      "result": {"stopReason": "cancelled"}})
                log("cancel-complete")
                PENDING["rid"] = None
                PENDING["mode"] = None
            # mode == "never"：只记录，永不完成（配合超时重建测试）
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()

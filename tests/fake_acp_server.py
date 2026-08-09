"""Fake ACP server：测试用的假 agent，stdio 上讲 NDJSON JSON-RPC。

行为约定（配合 tests/test_acp.py 的断言）：
- initialize：固定响应；若环境变量 FAKE_ACP_FAIL_INIT=1，回 error；
  FAKE_ACP_NO_LOAD_CAP=1 时不声明 loadSession capability
- session/new / session/list / session/load：固定响应，记入状态文件；
  FAKE_ACP_FAIL_NEW=1 / FAKE_ACP_FAIL_LOAD=1 时各自回 error
- session/prompt：
  - 有上一轮未完成（pending）→ 记 "VIOLATION:overlap"（配合串行化测试）
  - FAKE_ACP_FAIL_PROMPT=1：直接回 error
  - 含 "disconnect"：收到请求后直接退出，模拟已发送但响应前断线
  - 含 "perm"：发 session/request_permission 反向请求，同步等应答并记录
  - 含 "slow-never"：发一个 chunk 后挂起，收到 cancel 也只记录、永不完成
  - 含 "slow"：发一个 chunk 后挂起；收到 cancel 后延迟
    FAKE_ACP_CANCEL_DELAY 秒（默认 0.3）回 stopReason=cancelled，
    记 "cancel-complete"（配合"下一轮不提前开始"测试）
  - 其他：发 thought + message chunk，再回 stopReason=end_turn
- session/cancel（通知）：记入状态文件
- 未知带 id 方法：-32601

状态文件按行记录 new:/load:/prompt:/cancel: 等事件，行序即调用次序，
测试据此断言 load/new/prompt 的先后关系。

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
FAIL_LOAD = os.environ.get("FAKE_ACP_FAIL_LOAD") == "1"
FAIL_PROMPT = os.environ.get("FAKE_ACP_FAIL_PROMPT") == "1"
# initialize 不声明 loadSession capability（模拟不支持 session/load 的 agent）
NO_LOAD_CAP = os.environ.get("FAKE_ACP_NO_LOAD_CAP") == "1"
# initialize 将 promptCapabilities.image 置为 False（无原生图片能力）
NO_IMAGE_CAP = os.environ.get("FAKE_ACP_NO_IMAGE_CAP") == "1"
try:
    OPENCODE_PERMISSION = json.loads(
        os.environ.get("OPENCODE_PERMISSION", "{}"))
except json.JSONDecodeError:
    OPENCODE_PERMISSION = {}

# 挂起的 prompt：{"rid": 请求 id, "mode": "slow" | "never"}；同时最多一轮
PENDING: dict = {"rid": None, "mode": None}
# 仅存在于当前 fake server 进程，模拟 allow_always/session 授权缓存。
ALWAYS_ALLOWED_SESSIONS: set[str] = set()


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
                caps = {
                    "sessionCapabilities": {"list": {}},
                    "promptCapabilities": {
                        "image": not NO_IMAGE_CAP,
                        "audio": False,
                        "embeddedContext": True,
                    },
                }
                if not NO_LOAD_CAP:
                    caps["loadSession"] = True
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": caps,
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
            if FAIL_LOAD:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000, "message": "load boom"}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/prompt":
            if PENDING["rid"] is not None:
                log("VIOLATION:overlap")
            text = params["prompt"][0]["text"]
            log("prompt:" + text)
            log("prompt-types:" + ",".join(
                str(block.get("type", ""))
                for block in params.get("prompt", [])
                if isinstance(block, dict)
            ))
            sid = params["sessionId"]
            if "disconnect" in text:
                log("disconnect:" + text)
                return
            if FAIL_PROMPT:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000, "message": "prompt boom"}})
                continue
            if "opencode-readonly-tool-flow" in text:
                action = OPENCODE_PERMISSION.get(
                    "bash", OPENCODE_PERMISSION.get("*", "ask"))
                log("opencode-bash-policy:" + action)
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": sid, "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "opencode-bash",
                        "title": "bash",
                        "kind": "execute",
                        "status": "pending",
                    }}})
                if action == "ask":
                    send({"jsonrpc": "2.0", "id": 901,
                          "method": "session/request_permission",
                          "params": {
                              "sessionId": sid,
                              "toolCall": {"title": "bash"},
                              "options": [
                                  {"optionId": "allow", "kind": "allow_once",
                                   "name": "允许一次"},
                                  {"optionId": "deny", "kind": "reject_once",
                                   "name": "拒绝"},
                              ],
                          }})
                    response = json.loads(sys.stdin.readline())
                    outcome = response.get("result", {}).get("outcome", {})
                    log("opencode-permission:" + json.dumps(
                        outcome, ensure_ascii=False))
                    if outcome.get("outcome") == "cancelled":
                        send({"jsonrpc": "2.0", "id": rid,
                              "result": {"stopReason": "end_turn"}})
                        continue
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": sid, "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "opencode-bash",
                        "title": "bash",
                        "kind": "execute",
                        "status": "failed" if action == "deny" else "completed",
                    }}})
                chunk(
                    sid,
                    'MYAGENTS_WORKFLOW {"stage":"verify",'
                    '"status":"pass","findings":[]}',
                )
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"stopReason": "end_turn"}})
                continue
            needs_permission = "perm" in text or "requires-write" in text
            if needs_permission and sid in ALWAYS_ALLOWED_SESSIONS:
                log("permission-bypassed:" + sid)
            elif needs_permission:
                permission_title = (
                    "API_TOKEN=secret-value 写文件"
                    if "secret-title" in text else "写文件"
                )
                send({"jsonrpc": "2.0", "id": 900,
                      "method": "session/request_permission",
                      "params": {"sessionId": sid,
                                 "toolCall": {
                                     "title": permission_title,
                                     "rawInput": {
                                         "path": "examples/demo.txt",
                                         "command": "printf demo > examples/demo.txt",
                                     }},
                                 "options": [
                                     {"optionId": "allow", "kind": (
                                         "allow_always"
                                         if "perm-always" in text
                                         else "allow_once"),
                                      "name": (
                                          "始终允许"
                                          if "perm-always" in text
                                          else "允许一次")},
                                     {"optionId": "deny", "kind": "reject_once",
                                      "name": "拒绝"},
                                 ]}})
                # 同步等客户端应答后再继续（测试 server，要确定性时序）
                resp = json.loads(sys.stdin.readline())
                permission_result = resp.get("result", resp.get("error"))
                rendered_permission = json.dumps(
                    permission_result, ensure_ascii=False)
                log("permission:" + rendered_permission)
                if ("perm-always" in text
                        and '"outcome": "selected"' in rendered_permission
                        and '"optionId": "allow"' in rendered_permission):
                    ALWAYS_ALLOWED_SESSIONS.add(sid)
            if "slow" in text:
                chunk(sid, "开始了")
                PENDING["rid"] = rid
                PENDING["mode"] = "never" if "never" in text else "slow"
                continue  # 挂起，等 session/cancel
            if "observe" in text:
                tool_title = (
                    "API_TOKEN=secret-value 检查 JavaScript"
                    if "secret-title" in text else "检查 JavaScript"
                )
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": sid, "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tool-1",
                        "title": tool_title,
                        "kind": "execute",
                        "rawInput": {
                            "command": "API_TOKEN=secret-value node --check demo.js"
                        }}}})
                repeat = 200 if "tool-spam" in text else 1
                for _ in range(repeat):
                    update = {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tool-1",
                        "status": "in_progress",
                    }
                    # 真实 Kimi 的高频 update 经常不重复 title；adapter 应从
                    # tool_call 初始事件继承，而不是退化成“工具调用”。
                    if "tool-spam" not in text:
                        update["title"] = tool_title
                    send({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {"sessionId": sid, "update": update},
                    })
                if "tool-pause" in text:
                    # 模拟真实 Kimi 启动工程子代理后，父 ACP 会话长时间没有
                    # token/update，但工具本身仍然处于活跃状态。
                    time.sleep(0.15)
                if "tool-spam" in text or "tool-pause" in text:
                    send({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": sid,
                            "update": {
                                "sessionUpdate": "tool_call_update",
                                "toolCallId": "tool-1",
                                "status": "completed",
                            },
                        },
                    })
            if not needs_permission:
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

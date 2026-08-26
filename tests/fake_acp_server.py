"""Fake ACP server：测试用的假 agent，stdio 上讲 NDJSON JSON-RPC。

行为约定（配合 tests/test_acp.py 的断言）：
- initialize：固定响应；若环境变量 FAKE_ACP_FAIL_INIT=1，回 error；
  FAKE_ACP_NO_LOAD_CAP=1 时不声明 loadSession capability
- session/new / session/list / session/load：固定响应，记入状态文件；
  FAKE_ACP_FAIL_NEW=1 / FAKE_ACP_FAIL_LOAD=1 时各自回 error
  FAKE_ACP_HANG_NEW=1 / FAKE_ACP_HANG_LOAD=1 时不回响；
  FAKE_ACP_DISCONNECT_LOAD=1 时在收到 load 后退出
- session/prompt：
  - 有上一轮未完成（pending）→ 记 "VIOLATION:overlap"（配合串行化测试）
  - FAKE_ACP_FAIL_PROMPT=1：直接回 error
  - 含 "disconnect"：收到请求后直接退出，模拟已发送但响应前断线
  - 含 "perm"：发 session/request_permission 反向请求，同步等应答并记录
  - 含 "slow-never"：发一个 chunk 后挂起，收到 cancel 也只记录、永不完成
  - 含 "slow"：挂起（`silent-slow` 不发 chunk）；收到 cancel 后延迟
    FAKE_ACP_CANCEL_DELAY 秒（默认 0.3），再回
    FAKE_ACP_CANCEL_STOP_REASON（默认 cancelled，`__missing__` 表示缺失），
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
CANCEL_STOP_REASON = os.environ.get(
    "FAKE_ACP_CANCEL_STOP_REASON", "cancelled")
FAIL_INIT = os.environ.get("FAKE_ACP_FAIL_INIT") == "1"
FAIL_NEW = os.environ.get("FAKE_ACP_FAIL_NEW") == "1"
FAIL_LOAD = os.environ.get("FAKE_ACP_FAIL_LOAD") == "1"
HANG_NEW = os.environ.get("FAKE_ACP_HANG_NEW") == "1"
HANG_LOAD = os.environ.get("FAKE_ACP_HANG_LOAD") == "1"
DISCONNECT_LOAD = os.environ.get("FAKE_ACP_DISCONNECT_LOAD") == "1"
try:
    FAIL_LOAD_CODE = int(os.environ.get("FAKE_ACP_FAIL_LOAD_CODE", "-32002"))
except ValueError:
    FAIL_LOAD_CODE = -32603
FAIL_LOAD_MESSAGE = os.environ.get(
    "FAKE_ACP_FAIL_LOAD_MESSAGE", "session resource not found")
LOAD_NOTIFICATION_COUNT = int(
    os.environ.get("FAKE_ACP_LOAD_NOTIFICATION_COUNT", "0"))
FAIL_PROMPT = os.environ.get("FAKE_ACP_FAIL_PROMPT") == "1"
STOP_REASON = os.environ.get("FAKE_ACP_STOP_REASON", "end_turn")
PROMPT_UPDATE_FLOOD = int(
    os.environ.get("FAKE_ACP_PROMPT_UPDATE_FLOOD", "0"))
FRAME_PAYLOAD_BYTES = int(
    os.environ.get("FAKE_ACP_FRAME_PAYLOAD_BYTES", "2048"))
MEDIUM_UPDATE_COUNT = int(
    os.environ.get("FAKE_ACP_MEDIUM_UPDATE_COUNT", "0"))
MEDIUM_UPDATE_BYTES = int(
    os.environ.get("FAKE_ACP_MEDIUM_UPDATE_BYTES", "256"))
UNIQUE_TOOL_FLOOD = int(
    os.environ.get("FAKE_ACP_UNIQUE_TOOL_FLOOD", "0"))
TERMINAL_TOOL_FLOOD = int(
    os.environ.get("FAKE_ACP_TERMINAL_TOOL_FLOOD", "0"))
# initialize 不声明 loadSession capability（模拟不支持 session/load 的 agent）
NO_LOAD_CAP = os.environ.get("FAKE_ACP_NO_LOAD_CAP") == "1"
# initialize 不声明 sessionCapabilities.close（模拟生命周期能力不完整）
NO_CLOSE_CAP = os.environ.get("FAKE_ACP_NO_CLOSE_CAP") == "1"
# initialize 将 promptCapabilities.image 置为 False（无原生图片能力）
NO_IMAGE_CAP = os.environ.get("FAKE_ACP_NO_IMAGE_CAP") == "1"
AGENT_NAME = os.environ.get("FAKE_ACP_AGENT_NAME", "fake-acp")
AGENT_VERSION = os.environ.get("FAKE_ACP_AGENT_VERSION", "0.1")
AGENT_PROFILE = os.environ.get("FAKE_ACP_AGENT_PROFILE", "")
AGENT_RUNTIME_VERSION = os.environ.get(
    "FAKE_ACP_AGENT_RUNTIME_VERSION", "0.1.1-rc.2")
_compatibility_revision_raw = os.environ.get(
    "FAKE_ACP_AGENT_COMPATIBILITY_REVISION", "1")
try:
    # JSON parsing lets tests distinguish integer 1 from bool true.  An
    # unquoted invalid value is intentionally preserved as a string so the
    # adapter can exercise its exact-type rejection path.
    AGENT_COMPATIBILITY_REVISION = json.loads(_compatibility_revision_raw)
except json.JSONDecodeError:
    AGENT_COMPATIBILITY_REVISION = _compatibility_revision_raw
try:
    AGENT_POLICY_REVISION = json.loads(
        os.environ.get("FAKE_ACP_AGENT_POLICY_REVISION", "1"))
except json.JSONDecodeError:
    AGENT_POLICY_REVISION = None
try:
    AGENT_READ_ONLY_TOOLS = json.loads(os.environ.get(
        "FAKE_ACP_AGENT_READ_ONLY_TOOLS", '["read","glob","grep"]'))
except json.JSONDecodeError:
    AGENT_READ_ONLY_TOOLS = None
REQUIRE_AUTH = os.environ.get("FAKE_ACP_REQUIRE_AUTH") == "1"
AUTH_AT_NEW = os.environ.get("FAKE_ACP_AUTH_AT_NEW") == "1"
PREAUTHENTICATED = os.environ.get("FAKE_ACP_PREAUTHENTICATED") == "1"
AUTH_URL = os.environ.get("FAKE_ACP_AUTH_URL") == "1"
AUTH_HANG = os.environ.get("FAKE_ACP_AUTH_HANG") == "1"
AUTH_NOTIFICATION_COUNT = int(
    os.environ.get("FAKE_ACP_AUTH_NOTIFICATION_COUNT", "0"))
AUTH_NOTIFICATION_BYTES = int(
    os.environ.get("FAKE_ACP_AUTH_NOTIFICATION_BYTES", "1024"))
STOP_READING_AFTER_NEW = (
    os.environ.get("FAKE_ACP_STOP_READING_AFTER_NEW") == "1")
STOP_READING_AFTER_INIT = (
    os.environ.get("FAKE_ACP_STOP_READING_AFTER_INIT") == "1")
EXIT_AFTER_NEW = os.environ.get("FAKE_ACP_EXIT_AFTER_NEW") == "1"
BOOL_NEW_RESPONSE_ID = os.environ.get("FAKE_ACP_BOOL_NEW_RESPONSE_ID") == "1"
AUTH_URL_VALUE = os.environ.get(
    "FAKE_ACP_AUTH_URL_VALUE",
    "https://copilot.tencent.com/fake-auth",
)
try:
    OPENCODE_PERMISSION = json.loads(
        os.environ.get("OPENCODE_PERMISSION", "{}"))
except json.JSONDecodeError:
    OPENCODE_PERMISSION = {}
ENV_PROBE_KEYS = tuple(filter(
    None, os.environ.get("FAKE_ACP_ENV_PROBE_KEYS", "").split(",")))

# 挂起的 prompt：{"rid": 请求 id, "mode": "slow" | "never"}；同时最多一轮
PENDING: dict = {"rid": None, "mode": None}
# 仅存在于当前 fake server 进程，模拟 allow_always/session 授权缓存。
ALWAYS_ALLOWED_SESSIONS: set[str] = set()


def log(event: str) -> None:
    with open(STATE, "a") as f:
        f.write(event + "\n")


def has_logged(prefix: str) -> bool:
    try:
        with open(STATE) as f:
            return any(line.startswith(prefix) for line in f)
    except FileNotFoundError:
        return False


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def send_many(objects: list[dict]) -> None:
    """One write makes notification-flood scheduling deterministic."""
    sys.stdout.write("".join(json.dumps(obj) + "\n" for obj in objects))
    sys.stdout.flush()


def message_chunk(sid: str, text: str) -> dict:
    return {"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": sid, "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text}}}}


def chunk(sid: str, text: str) -> None:
    send(message_chunk(sid, text))


def permission_request(rid: object, sid: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "session/request_permission",
        "params": {
            "sessionId": sid,
            "toolCall": {"title": "写文件"},
            "options": [
                {"optionId": "allow", "kind": "allow_once",
                 "name": "允许一次"},
                {"optionId": "deny", "kind": "reject_once",
                 "name": "拒绝"},
            ],
        },
    }


def main() -> None:
    authenticated = PREAUTHENTICATED
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)

        # 客户端对 server 反向请求的应答（权限以外的兜底；perm 走同步读）
        if "method" not in msg:
            response_id = msg.get("id")
            if response_id in {902, 903, 904}:
                log("async-permission:" + str(msg.get("id")) + ":" +
                    json.dumps(msg.get("result", msg.get("error")),
                               ensure_ascii=False))
            elif (type(response_id) is int
                  and 1000 <= response_id <= 1016):
                log("permission-flood-response:" + str(response_id) + ":" +
                    json.dumps(msg.get("result", msg.get("error")),
                               ensure_ascii=False))
            elif response_id in {1100, 1101}:
                log("permission-byte-response:" + str(response_id) + ":" +
                    json.dumps(msg.get("result", msg.get("error")),
                               ensure_ascii=False))
            elif response_id == 950:
                log("duplicate-permission-response:950:" +
                    json.dumps(msg.get("result", msg.get("error")),
                               ensure_ascii=False))
            continue

        method, params, rid = msg["method"], msg.get("params", {}), msg.get("id")

        if method == "initialize":
            log("process-cwd:" + os.getcwd())
            if ENV_PROBE_KEYS:
                log("process-env:" + json.dumps(
                    {key: os.environ.get(key) for key in ENV_PROBE_KEYS},
                    sort_keys=True,
                ))
            if FAIL_INIT:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32601, "message": "init boom"}})
            else:
                caps = {
                    "sessionCapabilities": {
                        "list": {},
                        **({} if NO_CLOSE_CAP else {"close": {}}),
                    },
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
                    "authMethods": ([{
                        "id": "internal",
                        "name": "Login with WeChat",
                    }] if REQUIRE_AUTH else []),
                    "agentInfo": {
                        "name": AGENT_NAME,
                        "version": AGENT_VERSION,
                        "_meta": {
                            "deepseek.ai/dsh-myagents-profile": AGENT_PROFILE,
                            "deepseek.ai/dsh-myagents-policy-revision": (
                                AGENT_POLICY_REVISION),
                            "deepseek.ai/dsh-myagents-read-only-tools": (
                                AGENT_READ_ONLY_TOOLS),
                            "deepseek.ai/dsh-runtime-version": (
                                AGENT_RUNTIME_VERSION),
                            "deepseek.ai/dsh-compatibility-revision": (
                                AGENT_COMPATIBILITY_REVISION),
                        },
                    },
                }})
                if STOP_READING_AFTER_INIT:
                    log("stop-reading-after-init")
                    while True:
                        time.sleep(1)
        elif method == "authenticate":
            method_id = params.get("methodId", "")
            log("authenticate:" + method_id)
            if not REQUIRE_AUTH or method_id == "internal":
                authenticated = True
                if AUTH_URL:
                    send({
                        "jsonrpc": "2.0",
                        "method": "_codebuddy.ai/authUrl",
                        "params": {
                            "authUrl": AUTH_URL_VALUE,
                            "provider": "internal",
                        },
                    })
                if AUTH_NOTIFICATION_COUNT:
                    send_many([
                        {
                            "jsonrpc": "2.0",
                            "method": "_fake/authProgress",
                            "params": {
                                "text": "A" * AUTH_NOTIFICATION_BYTES,
                                "index": index,
                            },
                        }
                        for index in range(AUTH_NOTIFICATION_COUNT)
                    ])
                if AUTH_HANG:
                    continue
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
            else:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000,
                                "message": "unknown auth method"}})
        elif method == "session/new":
            log("new:" + params.get("cwd", ""))
            if HANG_NEW:
                continue
            if AUTH_AT_NEW and not authenticated:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000,
                                "message": "Authentication required"}})
            elif FAIL_NEW:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000, "message": "new boom"}})
            else:
                send({"jsonrpc": "2.0",
                      "id": True if BOOL_NEW_RESPONSE_ID else rid,
                      "result": {"sessionId": SESSION_ID}})
                if (STOP_READING_AFTER_NEW
                        and not has_logged("stop-reading-after-new")):
                    log("stop-reading-after-new")
                    while True:
                        time.sleep(1)
                if (EXIT_AFTER_NEW
                        and not has_logged("exit-after-new")):
                    log("exit-after-new")
                    time.sleep(0.05)
                    return
        elif method == "session/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "sessions": [{"sessionId": SESSION_ID, "cwd": "/tmp"}]}})
        elif method == "session/load":
            log("load:" + params.get("sessionId", ""))
            if DISCONNECT_LOAD:
                return
            if HANG_LOAD:
                continue
            if FAIL_LOAD:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": FAIL_LOAD_CODE,
                                "message": FAIL_LOAD_MESSAGE}})
            else:
                for index in range(LOAD_NOTIFICATION_COUNT):
                    chunk(
                        params.get("sessionId", SESSION_ID),
                        f"LOAD-HISTORY-{index}",
                    )
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/close":
            log("close:" + params.get("sessionId", ""))
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/prompt":
            if REQUIRE_AUTH and not authenticated:
                send({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000,
                                "message": "auth_required"}})
                continue
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
            if "giant-frame" in text:
                chunk(sid, "X" * FRAME_PAYLOAD_BYTES)
                continue
            if "unterminated-frame" in text:
                sys.stdout.write("X" * FRAME_PAYLOAD_BYTES)
                sys.stdout.flush()
                continue
            if MEDIUM_UPDATE_COUNT:
                log(f"medium-update-flood:{MEDIUM_UPDATE_COUNT}")
                send_many([
                    *[
                        message_chunk(sid, "M" * MEDIUM_UPDATE_BYTES)
                        for _index in range(MEDIUM_UPDATE_COUNT)
                    ],
                    {"jsonrpc": "2.0", "id": rid,
                     "result": {"stopReason": "end_turn"}},
                ])
                continue
            if PROMPT_UPDATE_FLOOD:
                log(f"prompt-update-flood:{PROMPT_UPDATE_FLOOD}")
                send_many([
                    *[
                        message_chunk(sid, f"FLOOD-{index}")
                        for index in range(PROMPT_UPDATE_FLOOD)
                    ],
                    {"jsonrpc": "2.0", "id": rid,
                     "result": {"stopReason": "end_turn"}},
                ])
                continue
            if UNIQUE_TOOL_FLOOD:
                log(f"unique-tool-flood:{UNIQUE_TOOL_FLOOD}")
                send_many([
                    *[
                        {"jsonrpc": "2.0", "method": "session/update",
                         "params": {"sessionId": sid, "update": {
                             "sessionUpdate": "tool_call",
                             "toolCallId": f"unique-tool-{index}",
                             "title": f"unique tool {index}",
                             "kind": "execute",
                             "status": "pending",
                         }}}
                        for index in range(UNIQUE_TOOL_FLOOD)
                    ],
                    {"jsonrpc": "2.0", "id": rid,
                     "result": {"stopReason": "end_turn"}},
                ])
                continue
            if TERMINAL_TOOL_FLOOD:
                log(f"terminal-tool-flood:{TERMINAL_TOOL_FLOOD}")
                notifications: list[dict] = []
                for index in range(TERMINAL_TOOL_FLOOD):
                    tool_call_id = f"terminal-tool-{index}"
                    notifications.extend((
                        {"jsonrpc": "2.0", "method": "session/update",
                         "params": {"sessionId": sid, "update": {
                             "sessionUpdate": "tool_call",
                             "toolCallId": tool_call_id,
                             "title": f"terminal tool {index}",
                             "kind": "execute",
                             "status": "pending",
                         }}},
                        {"jsonrpc": "2.0", "method": "session/update",
                         "params": {"sessionId": sid, "update": {
                             "sessionUpdate": "tool_call_update",
                             "toolCallId": tool_call_id,
                             "status": "completed",
                         }}},
                    ))
                send_many([
                    *notifications,
                    {"jsonrpc": "2.0", "id": rid,
                     "result": {"stopReason": "end_turn"}},
                ])
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
            if "perm-after-terminal" in text:
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"stopReason": "end_turn"}})
                time.sleep(0.1)
                send(permission_request(903, sid))
                continue
            if "perm-terminal-first" in text:
                send(permission_request(902, sid))
                time.sleep(0.05)
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"stopReason": "end_turn"}})
                continue
            if "perm-overflow" in text:
                send(permission_request(904, sid))
                send_many([
                    message_chunk(sid, f"PERM-FLOOD-{index}")
                    for index in range(300)
                ])
                PENDING["rid"] = rid
                PENDING["mode"] = "slow"
                continue
            if "permission-flood-17" in text:
                send_many([
                    permission_request(1000 + index, sid)
                    for index in range(17)
                ])
                # Give the client-side permission handlers time to enter their
                # waits, so all sixteen slots are observably active before the
                # prompt terminal closes the scope.
                time.sleep(0.1)
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"stopReason": "end_turn"}})
                continue
            if "permission-byte-flood" in text:
                requests = []
                for request_id in (1100, 1101):
                    request = permission_request(request_id, sid)
                    request["params"]["detail"] = "B" * 512
                    requests.append(request)
                send_many(requests)
                time.sleep(0.1)
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"stopReason": "end_turn"}})
                continue
            if "perm-duplicate-active-id" in text:
                send(permission_request(950, sid))
                time.sleep(0.05)
                send(permission_request(950, sid))
                continue
            if "perm-invalid-request-id" in text:
                invalid = permission_request({"invalid": True}, sid)
                send(invalid)
                continue
            if "perm-response-backpressure" in text:
                request = permission_request(1200, sid)
                request["params"]["options"][0]["optionId"] = "A" * (1024 * 1024)
                send(request)
                time.sleep(0.05)
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"stopReason": "end_turn"}})
                log("permission-response-backpressure")
                while True:
                    time.sleep(1)
            if "oversized-tool-fields" in text:
                giant = "Z" * 5000
                tool_call_id = "tool-" + giant
                send_many([
                    {"jsonrpc": "2.0", "method": "session/update",
                     "params": {"sessionId": sid, "update": {
                         "sessionUpdate": "tool_call",
                         "toolCallId": tool_call_id,
                         "title": "title-" + giant,
                         "kind": "kind-" + giant,
                         "status": "pending-" + giant,
                         "rawInput": {"command": "command-" + giant},
                     }}},
                    {"jsonrpc": "2.0", "method": "session/update",
                     "params": {"sessionId": sid, "update": {
                         "sessionUpdate": "tool_call_update",
                         "toolCallId": tool_call_id,
                         "status": "completed",
                     }}},
                    {"jsonrpc": "2.0", "id": rid,
                     "result": {"stopReason": "end_turn"}},
                ])
                continue
            needs_permission = "perm" in text or "requires-write" in text
            if needs_permission and sid in ALWAYS_ALLOWED_SESSIONS:
                log("permission-bypassed:" + sid)
            elif needs_permission:
                permission_title = (
                    "API_TOKEN=secret-value 写文件"
                    if "secret-title" in text else "写文件"
                )
                permission_sid = (
                    "foreign-session" if "perm-wrong-session" in text else sid)
                permission_rid: object = (
                    ("permission-" + ("x" * 1024))
                    if "perm-giant-string-id" in text
                    else ("permission-900"
                          if "perm-string-id" in text else 900))
                send({"jsonrpc": "2.0", "id": permission_rid,
                      "method": "session/request_permission",
                      "params": {"sessionId": permission_sid,
                                 "toolCall": {
                                     "title": permission_title,
                                     "rawInput": {
                                         "path": "examples/demo.txt",
                                         "command": "printf demo > examples/demo.txt",
                                     }},
                                 "options": [
                                     {"optionId": (
                                         "duplicate" if "perm-duplicate" in text
                                         else "allow"), "kind": (
                                         "allow_always"
                                         if "perm-always" in text
                                         else "allow_once"),
                                      "name": (
                                          "始终允许"
                                          if "perm-always" in text
                                          else "允许一次")},
                                     {"optionId": (
                                         "duplicate" if "perm-duplicate" in text
                                         else "deny"), "kind": "reject_once",
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
                if "silent-slow" not in text:
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
            result = (
                {} if STOP_REASON == "__missing__"
                else {"stopReason": STOP_REASON}
            )
            send({"jsonrpc": "2.0", "id": rid, "result": result})
        elif method == "session/cancel":
            sid = params.get("sessionId", "")
            log("cancel:" + sid)
            if PENDING["rid"] is not None and PENDING["mode"] == "slow":
                # 延迟完成 cancel：模拟 agent 需要时间停下工具调用
                time.sleep(CANCEL_DELAY)
                result = (
                    {} if CANCEL_STOP_REASON == "__missing__"
                    else {"stopReason": CANCEL_STOP_REASON}
                )
                log("cancel-complete")
                send({"jsonrpc": "2.0", "id": PENDING["rid"],
                      "result": result})
                PENDING["rid"] = None
                PENDING["mode"] = None
            # mode == "never"：只记录，永不完成（配合超时重建测试）
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()

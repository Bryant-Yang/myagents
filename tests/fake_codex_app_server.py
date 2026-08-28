"""Fake Codex app-server：测试用的假 agent，stdio 上讲 V2 JSONL JSON-RPC。

协议形状以本机官方 schema（codex app-server generate-json-schema，V2）和
官方手册 "Codex App Server" 章节为准：
- 线上是换行分隔 JSON（JSONL），消息里**没有** "jsonrpc" 头；收到带
  "jsonrpc" 键的消息记 "VIOLATION:jsonrpc"
- 请求：{"id", "method", "params"}；响应：{"id", "result"|"error"}；
  通知：{"method", "params"}（无 id）
- 生命周期：initialize → initialized（通知）→ thread/start → turn/start，
  之后流式通知（turn/started、item/started、item/agentMessage/delta、
  item/completed、turn/completed）

行为约定（配合 tests/test_codex_app_server.py 的断言；prompt 文本触发分支）：
- 含 "die"：不回响应，直接退出进程（模拟 server 崩溃时 pending 请求）
- 含 "approval"：发反向请求 item/commandExecution/requestApproval，
  同步等客户端应答并记 "approval:<result|error>"，然后正常完成本轮
- 含 "slow"：发一个 delta 后挂起不完成；收到 turn/interrupt 后回空
  result 并发 turn/completed(status=interrupted)，记 "interrupt:<turnId>"
- 活跃 slow turn 收到 turn/steer：校验 threadId/expectedTurnId，回同一 turnId，
  发追加 delta 后完成；不会创建第二 turn
- 含 "tools"：发 reasoning item + item/reasoning/textDelta（正文含标记
  REASONING-SECRET-MARKER）、commandExecution / fileChange / mcpToolCall
  item（command 含 API_TOKEN=secret-value，配合脱敏断言），再发
  agentMessage delta "PO"/"NG" 并完成
- 其他：item/started(agentMessage) → delta "PO"/"NG" → item/completed →
  turn/completed(status=completed)

同一时刻只允许一个活跃 turn；turn/start 在上一轮未 terminal 时到达记
"VIOLATION:overlap"（配合"取消后不重叠"测试）。

状态文件按行记录 initialize:<pid> / initialized / thread:<tid>:<pid> /
turn:<tid>:<turnId>:<pid> / approval:... / interrupt:... 等事件，行序即
调用次序；测试据此独立证明两轮复用同一 thread 与同一进程（pid）。
路径由环境变量 FAKE_CODEX_STATE 指定。
"""

import json
import os
import sys
import time

STATE = os.environ.get("FAKE_CODEX_STATE", "/tmp/fake_codex_app_state")
THREAD_ID = "thr_fake_1"
REASONING_MARKER = "REASONING-SECRET-MARKER"
SECRET_COMMAND = "API_TOKEN=secret-value rm -rf build"

PID = os.getpid()
_turn_seq = 0
# 当前活跃 turn：{"rid": turn/start 请求 id, "thread_id": str,
# "turn_id": str, "slow": bool}
_active: dict = {
    "rid": None,
    "thread_id": None,
    "turn_id": None,
    "slow": False,
}


def log(event: str) -> None:
    with open(STATE, "a") as f:
        f.write(event + "\n")


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def notify(method: str, params: dict) -> None:
    send({"method": method, "params": params})


def turn_obj(turn_id: str, status: str) -> dict:
    # Turn 必填：id / items / status（v2 TurnCompletedNotification schema）
    return {"id": turn_id, "items": [], "status": status}


def thread_obj(cwd: str = "/tmp", *, ephemeral: bool = False) -> dict:
    return {
        "id": THREAD_ID,
        "cliVersion": "0.0.0-fake",
        "createdAt": 1,
        "updatedAt": 1,
        "cwd": cwd,
        "ephemeral": ephemeral,
        "modelProvider": "openai",
        "preview": "",
        "sessionId": "sess_fake_1",
        "source": "appServer",
        "status": {"type": "idle"},
        "turns": [],
    }


def send_thread_result(rid, params: dict) -> None:
    thread = thread_obj(
        params.get("cwd") or "/tmp",
        ephemeral=params.get("ephemeral") is True,
    )
    send({"id": rid, "result": {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": thread["cwd"],
        "model": "gpt-5.4-fake",
        "modelProvider": "openai",
        "sandbox": {"type": "readOnly"},
        "thread": thread}})


def emit_text_and_complete(thread_id: str, turn_id: str, rid) -> None:
    item_id = turn_id + "_msg"
    notify("item/started", {"threadId": thread_id, "turnId": turn_id,
                            "startedAtMs": 1,
                            "item": {"id": item_id, "type": "agentMessage",
                                     "text": ""}})
    for delta in ("PO", "NG"):
        notify("item/agentMessage/delta", {"threadId": thread_id,
                                           "turnId": turn_id,
                                           "itemId": item_id,
                                           "delta": delta})
    notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                              "completedAtMs": 2,
                              "item": {"id": item_id, "type": "agentMessage",
                                       "text": "PONG"}})
    notify("turn/completed", {"threadId": thread_id,
                              "turn": turn_obj(turn_id, "completed")})


def emit_tool_items(thread_id: str, turn_id: str) -> None:
    # reasoning：正文只能通过 delta 出现，adapter 不得暴露
    notify("item/started", {"threadId": thread_id, "turnId": turn_id,
                            "startedAtMs": 1,
                            "item": {"id": turn_id + "_rs", "type": "reasoning",
                                     "summary": [], "content": []}})
    notify("item/reasoning/textDelta", {"threadId": thread_id,
                                        "turnId": turn_id,
                                        "itemId": turn_id + "_rs",
                                        "contentIndex": 0,
                                        "delta": REASONING_MARKER})
    notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                              "completedAtMs": 2,
                              "item": {"id": turn_id + "_rs", "type": "reasoning",
                                       "summary": [], "content": []}})
    # commandExecution：命令里带凭据形态，adapter 必须脱敏
    cmd_item = {"id": turn_id + "_cmd", "type": "commandExecution",
                "command": SECRET_COMMAND, "commandActions": [],
                "cwd": "/tmp", "status": "inProgress"}
    notify("item/started", {"threadId": thread_id, "turnId": turn_id,
                            "startedAtMs": 3, "item": cmd_item})
    done_cmd = dict(cmd_item, status="completed", exitCode=0,
                    aggregatedOutput="")
    notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                              "completedAtMs": 4, "item": done_cmd})
    # fileChange
    fc_item = {"id": turn_id + "_fc", "type": "fileChange",
               "changes": [{"path": "demo.txt",
                            "kind": {"type": "update"},
                            "diff": "@@ demo"}],
               "status": "completed"}
    notify("item/started", {"threadId": thread_id, "turnId": turn_id,
                            "startedAtMs": 5, "item": fc_item})
    notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                              "completedAtMs": 6, "item": fc_item})
    # mcpToolCall
    mcp_item = {"id": turn_id + "_mcp", "type": "mcpToolCall",
                "server": "fs", "tool": "read_file",
                "arguments": {"path": "demo.txt"}, "status": "completed"}
    notify("item/started", {"threadId": thread_id, "turnId": turn_id,
                            "startedAtMs": 7, "item": mcp_item})
    notify("item/completed", {"threadId": thread_id, "turnId": turn_id,
                              "completedAtMs": 8, "item": mcp_item})


def main() -> None:
    global _turn_seq
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "jsonrpc" in msg:
            log("VIOLATION:jsonrpc")

        # 客户端对 server 反向请求的应答：approval 走同步读，其余兜底忽略
        if "method" not in msg:
            continue

        method, params, rid = msg["method"], msg.get("params") or {}, msg.get("id")

        if method == "initialize":
            log("initialize:%d" % PID)
            if "--slow-initialize" in sys.argv:
                time.sleep(2)
            send({"id": rid, "result": {
                "codexHome": "/tmp/fake-codex-home",
                "platformFamily": "unix",
                "platformOs": "macos",
                "userAgent": "fake-codex-app-server/0.1"}})
        elif method == "initialized":
            log("initialized")
            if "--event-flood" in sys.argv:
                log("event-flood-start")
                for index in range(6000):
                    notify("item/started", {
                        "threadId": THREAD_ID,
                        "turnId": f"flood-{index}",
                        "startedAtMs": index,
                        "item": {
                            "id": f"flood-item-{index}",
                            "type": "reasoning",
                            "summary": [],
                            "content": [],
                        },
                    })
                log("event-flood-done")
        elif method == "thread/start":
            log("thread:%s:%d" % (THREAD_ID, PID))
            log("thread-params:" + json.dumps(params, sort_keys=True))
            send_thread_result(rid, params)
        elif method == "thread/resume":
            if params.get("threadId") != THREAD_ID:
                send({"id": rid, "error": {
                    "code": -32602, "message": "unknown thread"}})
                continue
            log("resume:%s:%d" % (THREAD_ID, PID))
            log("resume-params:" + json.dumps(params, sort_keys=True))
            send_thread_result(rid, params)
        elif method == "turn/start":
            if _active["rid"] is not None:
                log("VIOLATION:overlap")
            _turn_seq += 1
            turn_id = "turn_%d" % _turn_seq
            thread_id = params.get("threadId", "")
            log("turn:%s:%s:%d" % (thread_id, turn_id, PID))
            log("turn-params:" + json.dumps(params, sort_keys=True))
            text = ""
            for part in params.get("input") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    text += part.get("text", "")
            if "reject-turn" in text:
                send({"id": rid, "error": {
                    "code": -32602,
                    "message": "turn rejected before acceptance",
                }})
                continue
            if "die" in text:
                # 模拟崩溃：pending 的 turn/start 永远等不到响应
                os._exit(1)
            _active.update({"rid": rid, "thread_id": thread_id,
                            "turn_id": turn_id,
                            "slow": "slow" in text})
            send({"id": rid, "result": {
                "turn": turn_obj(turn_id, "inProgress")}})
            notify("turn/started", {"threadId": thread_id,
                                    "turn": turn_obj(turn_id, "inProgress")})
            if "badframe" in text:
                sys.stdout.write("{invalid-json\n")
                sys.stdout.flush()
                continue
            if "serverinterrupt" in text:
                notify("item/agentMessage/delta", {
                    "threadId": thread_id, "turnId": turn_id,
                    "itemId": turn_id + "_msg", "delta": "half"})
                notify("turn/completed", {
                    "threadId": thread_id,
                    "turn": turn_obj(turn_id, "interrupted")})
                _active.update({"rid": None, "thread_id": None,
                                "turn_id": None, "slow": False})
                continue
            if "approval" in text:
                send({"id": "srv-approval-1",
                      "method": "item/commandExecution/requestApproval",
                      "params": {
                          "threadId": thread_id,
                          "turnId": turn_id,
                          "itemId": turn_id + "_cmd",
                          "startedAtMs": int(time.time() * 1000),
                          "command": SECRET_COMMAND,
                          "commandActions": [],
                          "cwd": "/tmp",
                          "reason": "需要执行 shell 命令",
                          "availableDecisions": ["accept", "decline", "cancel"]}})
                # 同步等客户端应答后再继续（测试 server，要确定性时序）
                resp = json.loads(sys.stdin.readline())
                if "jsonrpc" in resp:
                    log("VIOLATION:jsonrpc")
                log("approval:" + json.dumps(
                    resp.get("result", resp.get("error")), ensure_ascii=False))
                notify("item/completed", {
                    "threadId": thread_id, "turnId": turn_id,
                    "completedAtMs": 2,
                    "item": {"id": turn_id + "_cmd", "type": "commandExecution",
                             "command": SECRET_COMMAND, "commandActions": [],
                             "cwd": "/tmp", "status": "declined"}})
            if "permissions" in text:
                send({"id": "srv-permissions-1",
                      "method": "item/permissions/requestApproval",
                      "params": {
                          "threadId": thread_id,
                          "turnId": turn_id,
                          "itemId": turn_id + "_perm",
                          "startedAtMs": int(time.time() * 1000),
                          "cwd": "/tmp",
                          "reason": "需要额外网络权限",
                          "permissions": {"network": {"enabled": True}}}})
                resp = json.loads(sys.stdin.readline())
                if "jsonrpc" in resp:
                    log("VIOLATION:jsonrpc")
                log("permissions:" + json.dumps(
                    resp.get("result", resp.get("error")), ensure_ascii=False))
            if _active["slow"]:
                notify("item/agentMessage/delta", {
                    "threadId": thread_id, "turnId": turn_id,
                    "itemId": turn_id + "_msg", "delta": "开始了"})
                continue  # 挂起，等 turn/interrupt
            if "badstatus" in text:
                notify("turn/completed", {
                    "threadId": thread_id,
                    "turn": turn_obj(turn_id, "inProgress")})
                _active.update({"rid": None, "thread_id": None,
                                "turn_id": None, "slow": False})
                continue
            if "tools" in text:
                emit_tool_items(thread_id, turn_id)
            emit_text_and_complete(thread_id, turn_id, rid)
            _active.update({"rid": None, "thread_id": None,
                            "turn_id": None, "slow": False})
        elif method == "turn/steer":
            log("steer-params:" + json.dumps(params, sort_keys=True))
            if (
                _active["rid"] is None
                or params.get("threadId") != _active["thread_id"]
                or params.get("expectedTurnId") != _active["turn_id"]
            ):
                send({"id": rid, "error": {
                    "code": -32602,
                    "message": "active turn does not match expected turn",
                }})
                continue
            text = "".join(
                part.get("text", "")
                for part in params.get("input") or []
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if "reject-steer" in text:
                send({"id": rid, "error": {
                    "code": -32602,
                    "message": "turn cannot accept steering now",
                }})
                continue
            if "die-steer" in text:
                os._exit(1)
            thread_id = _active["thread_id"]
            turn_id = _active["turn_id"]
            send({"id": rid, "result": {"turnId": turn_id}})
            notify("item/agentMessage/delta", {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": turn_id + "_msg",
                "delta": "已收到插话",
            })
            notify("turn/completed", {
                "threadId": thread_id,
                "turn": turn_obj(turn_id, "completed"),
            })
            _active.update({"rid": None, "thread_id": None,
                            "turn_id": None, "slow": False})
        elif method == "turn/interrupt":
            log("interrupt:%s" % params.get("turnId", ""))
            send({"id": rid, "result": {}})
            if _active["rid"] is not None:
                thread_id = ""
                # 从最近一条 turn 日志恢复 threadId，保证通知形状完整
                try:
                    with open(STATE) as f:
                        for ev in reversed(f.read().splitlines()):
                            if ev.startswith("turn:"):
                                thread_id = ev.split(":")[1]
                                break
                except OSError:
                    pass
                notify("turn/completed", {
                    "threadId": thread_id,
                    "turn": turn_obj(_active["turn_id"], "interrupted")})
                log("interrupt-complete:%s" % _active["turn_id"])
                _active.update({"rid": None, "thread_id": None,
                                "turn_id": None, "slow": False})
        elif rid is not None:
            send({"id": rid,
                  "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()

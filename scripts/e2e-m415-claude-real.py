"""M4.15 真实 Claude Code stream-json 探针（发布前手工证据）。

本脚本调用真实模型与真实 `claude` CLI，产生真实费用，**不进入默认
Harness gate**；执行前必须获得用户明确授权（对照 ADR-0022 §6）。

前置：
- 本机 `claude` CLI 已安装并完成登录（≥2.1.259）；
- 当前进程 PATH 能解析 `claude`。

运行：
    .venv/bin/python scripts/e2e-m415-claude-real.py

验收面（ADR-0022 §6 的自动化可执行子集）：
1. 冷/热两轮复用同一 CLI 进程与 session，流式正文 + delivery_committed 时序；
2. 真实权限桥 deny：写文件被拒后原文件不变；
3. 真实权限桥 allow：一次 allow_once 后文件被改写；
4. 只读轮：进程重建、argv 含 --restricted 闭集、写文件被自动拒绝；
5. 重启 --resume：新进程续接 durable session 并记得此前交付内容；
6. 图片：受信 PNG 经 base64 内容块被正确识别；
7. 提交后取消：no-replay、进程组回收、无残留。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import struct
import sys
import uuid as uuid_module
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import (  # noqa: E402
    AgentDeliveryCancelledError,
    ExecutionMode,
)
from claude_code.adapter import ClaudeCodeAdapter  # noqa: E402


TURN_TIMEOUT = 300.0


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _solid_png(rgb: tuple[int, int, int], size: int = 8) -> bytes:
    width, height = size, size
    raw = b"".join(
        b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(
            ">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _reply_text(events: list) -> str:
    return "".join(event.text for event in events if event.kind == "text")


async def _turn(adapter: ClaudeCodeAdapter, prompt: str, workdir: str,
                *, mode: ExecutionMode = ExecutionMode.DEFAULT,
                resume: str | None = None) -> tuple[list, str | None]:
    events: list = []
    stream = adapter.stream_prepared(
        lambda _prep: prompt, workdir, resume, execution_mode=mode)
    async for event in stream:
        events.append(event)
    return events, adapter.session_id


async def main() -> int:
    state_root = Path("/tmp/myagents-m415-real") / (
        uuid_module.uuid4().hex[:8])
    state_root.mkdir(mode=0o700, parents=True)
    evidence: list[str] = []
    with TemporaryDirectory(prefix="m415-") as raw_tmp:
        workdir = str(Path(raw_tmp).resolve())
        out_file = Path(workdir) / "out.txt"
        out_file.write_text("ORIGINAL", encoding="utf-8")

        permission_mode = {"allow": False}

        async def handler(_name: str, params: dict) -> dict:
            kind = "allow_once" if permission_mode["allow"] else "reject_once"
            ids = [item["optionId"] for item in params["options"]
                   if item["kind"] == kind]
            return {"outcome": "selected", "optionId": ids[0]}

        adapter = ClaudeCodeAdapter(
            state_root=state_root, permission_handler=handler,
        )

        # -- 1 冷轮：delivery_committed 时序 + 流式正文 -------------------
        started = asyncio.get_running_loop().time()
        events, token = await asyncio.wait_for(
            _turn(adapter, "只回复两个字符：OK", workdir),
            timeout=TURN_TIMEOUT)
        assert events[0].kind == "delivery_committed", events[0]
        reply = _reply_text(events)
        assert "OK" in reply, reply
        cold_pid = adapter.pid
        assert events[-1].kind == "done" and events[-1].meta.get(
            "claudePid") == cold_pid
        assert token and token.startswith("claude:v1:")
        evidence.append(
            f"冷轮 {(asyncio.get_running_loop().time() - started):.1f}s "
            f"pid={cold_pid} 回复={reply!r} commit 先于全部可见事件")

        # -- 2 热轮：同进程复用 ------------------------------------------
        events, token2 = await asyncio.wait_for(
            _turn(adapter, "再回复：PONG", workdir), timeout=TURN_TIMEOUT)
        assert adapter.pid == cold_pid, (adapter.pid, cold_pid)
        assert "PONG" in _reply_text(events), _reply_text(events)
        assert token2 == token
        evidence.append(f"热轮复用同 pid={adapter.pid}，checkpoint 未变")

        # -- 3 权限桥 deny：真实 MCP 桥 → TUI handler 拒绝 ---------------
        permission_mode["allow"] = False
        events, _ = await asyncio.wait_for(
            _turn(adapter, "把 out.txt 的内容改成 TAMPERED", workdir),
            timeout=TURN_TIMEOUT)
        permission_texts = [event.text for event in events
                            if event.kind == "permission"]
        assert any("等待权限" in text for text in permission_texts), (
            permission_texts)
        assert any("拒绝" in text for text in permission_texts), (
            permission_texts)
        assert out_file.read_text(encoding="utf-8") == "ORIGINAL"
        evidence.append("deny 路径：写请求被拒，out.txt 保持 ORIGINAL")

        # -- 4 权限桥 allow：一次 allow_once 放行 ------------------------
        permission_mode["allow"] = True
        events, _ = await asyncio.wait_for(
            _turn(adapter, "把 out.txt 的内容改成 ALLOWED", workdir),
            timeout=TURN_TIMEOUT)
        assert any("权限已允许一次" in event.text for event in events
                   if event.kind == "permission"), [
            event.text for event in events if event.kind == "permission"]
        # Claude 的 Write/Edit 可能带尾换行，按内容语义比较。
        assert out_file.read_text(encoding="utf-8").strip() == "ALLOWED", (
            out_file.read_text(encoding="utf-8"))
        evidence.append("allow 路径：一次 allow_once 后 out.txt == ALLOWED")

        # -- 5 只读轮：进程重建 + restricted 闭集 + 写被自动拒绝 ---------
        events, readonly_token = await asyncio.wait_for(
            _turn(adapter, "读取 out.txt 并只复述其内容单词", workdir,
                  mode=ExecutionMode.READ_ONLY),
            timeout=TURN_TIMEOUT)
        readonly_pid = adapter.pid
        assert readonly_pid != cold_pid
        client = adapter._client
        assert client is not None and "--restricted" in client.command
        assert "--mcp-config" not in client.command
        assert "ALLOWED" in _reply_text(events), _reply_text(events)
        evidence.append(
            f"只读轮重建 pid={readonly_pid}，argv 含 --restricted，"
            "读取成功")
        events, _ = await asyncio.wait_for(
            _turn(adapter, "创建 blocked.txt 内容 NOPE", workdir,
                  mode=ExecutionMode.READ_ONLY),
            timeout=TURN_TIMEOUT)
        assert not (Path(workdir) / "blocked.txt").exists()
        evidence.append("只读轮写入被工具闭集自动拒绝（blocked.txt 不存在）")

        # -- 6 重启 --resume：新进程记得此前交付 -------------------------
        await adapter.aclose()
        resumed = ClaudeCodeAdapter(
            state_root=state_root, permission_handler=handler,
        )
        events, resumed_token = await asyncio.wait_for(
            _turn(resumed, "我上一轮让你把 out.txt 改成了什么？只回答那个单词",
                  workdir, resume=token),
            timeout=TURN_TIMEOUT)
        assert resumed.pid not in (None, cold_pid, readonly_pid)
        assert resumed_token == token, (resumed_token, token)
        assert "ALLOWED" in _reply_text(events).upper(), _reply_text(events)
        evidence.append(
            f"重启 --resume：新 pid={resumed.pid} 续接同一 checkpoint 并"
            "记得 ALLOWED")

        # -- 7 图片：受信 PNG 识别 ---------------------------------------
        attachment_root = Path(workdir) / "attachments"
        attachment_root.mkdir(mode=0o700)
        image_path = attachment_root / "img-0001.png"
        image_path.write_bytes(_solid_png((0, 200, 0)))
        image_path.chmod(0o600)
        resumed.set_attachment_root(attachment_root)
        events, _ = await asyncio.wait_for(
            _turn(resumed, "[图片 1] 图中的纯色是什么颜色？只回答颜色英文名",
                  workdir),
            timeout=TURN_TIMEOUT)
        reply = _reply_text(events).lower()
        assert "green" in reply, reply
        evidence.append(f"图片识别：绿色 PNG → {reply.strip()[:40]!r}")

        # -- 8 提交后取消：no-replay + 回收 ------------------------------
        stream = resumed.stream(
            "从 1 数到 30，每个数字单独一行，不要省略任何数字", workdir)

        async def drive() -> None:
            async for _event in stream:
                pass

        task = asyncio.create_task(drive())
        while resumed._prompt_permission_gate is None:
            await asyncio.sleep(0.02)
        await asyncio.sleep(2.0)
        task.cancel()
        cancelled = False
        with contextlib.suppress(asyncio.CancelledError,
                                 AgentDeliveryCancelledError):
            await task
        cancelled = True
        assert cancelled and resumed.session_id is None
        evidence.append("提交后取消：no-replay 生效，进程已回收待重建")
        await resumed.aclose()

        for pid in {cold_pid, readonly_pid, resumed.pid}:
            if pid is None:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError(f"进程 {pid} 未被回收")

    print("== M4.15 Claude 真实探针通过 ==")
    for line in evidence:
        print("  -", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

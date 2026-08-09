#!/usr/bin/env python3
"""授权手工探针：在临时 Git repo 运行真实 M5 多角色 workflow。

不进入默认 Harness；会调用本机真实 Kimi/Codex，并只允许 writer 在
自动创建的临时 repo 中修改文件。退出时整个临时目录自动删除。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.base import AgentEvent
from orchestrator import Orchestrator


def run_checked(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        list(args), cwd=cwd, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return completed.stdout.strip()


def prepare_repo(root: Path) -> str:
    run_checked(root, "git", "init", "-q", "-b", "main")
    run_checked(root, "git", "config", "user.name", "M5 Acceptance")
    run_checked(root, "git", "config", "user.email", "m5@example.invalid")
    (root / "calc.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (root / "test_calc.py").write_text(
        "import unittest\n\n"
        "from calc import add\n\n\n"
        "class CalcTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    run_checked(root, "git", "add", "calc.py", "test_calc.py")
    run_checked(root, "git", "commit", "-qm", "baseline")
    return run_checked(root, "git", "rev-parse", "HEAD")


async def exercise(root: Path) -> dict[str, object]:
    command_id = str(uuid.uuid4())
    events: list[dict[str, object]] = []
    orch = Orchestrator(str(root), persistent=False)

    def on_event(agent: str, event: AgentEvent) -> None:
        if event.kind in {"status", "info", "error", "done"}:
            record = {
                "agent": agent,
                "kind": event.kind,
                "text": event.text[:500],
                "stage": event.meta.get("workflow_stage"),
                "mode": event.meta.get("execution_mode"),
            }
            events.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)

    command = (
        "/workflow --reviewer @kimi --implementer @codex "
        "-- 在 calc.py 新增 subtract(a: int, b: int) -> int，并在 test_calc.py "
        "新增至少两个覆盖正数和负数的 unittest；运行完整 unittest。"
    )
    try:
        outcome = await asyncio.wait_for(
            orch.dispatch(command, on_event, command_id=command_id),
            timeout=900,
        )
        history = [
            {
                "speaker": item.speaker,
                "text": item.text,
                "command_id": item.command_id,
            }
            for item in orch.history
        ]
    finally:
        await orch.aclose()
    return {
        "command_id": command_id,
        "failures": [
            {"agent": item.agent, "error": item.error}
            for item in outcome.failures
        ],
        "events": events,
        "history": history,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--keep", action="store_true",
        help="成功后保留临时 repo 并打印路径")
    args = parser.parse_args()
    missing = [name for name in ("kimi", "codex")
               if shutil.which(name) is None]
    if missing:
        raise SystemExit(f"缺少真实 agent CLI：{', '.join(missing)}")

    root = Path(tempfile.mkdtemp(prefix="myagents-m5-"))
    try:
        baseline = prepare_repo(root)
        evidence = asyncio.run(exercise(root))
        failures = evidence["failures"]
        if failures:
            print(json.dumps(evidence, ensure_ascii=False, indent=2))
            raise RuntimeError(f"workflow failed: {failures}")
        assert run_checked(root, "git", "rev-parse", "HEAD") == baseline
        assert run_checked(root, "git", "branch", "--show-current") == "main"
        assert not run_checked(root, "git", "diff", "--cached", "--name-only")
        diff = run_checked(root, "git", "diff", "--", "calc.py", "test_calc.py")
        if "def subtract" not in diff:
            raise RuntimeError("最终 diff 缺少 subtract")
        tests = run_checked(
            root, sys.executable, "-m", "unittest", "discover", "-v")
        print(json.dumps({
            "result": "PASS",
            "repo": str(root),
            "baseline": baseline,
            "git_diff": diff,
            "tests": tests,
            "command_id": evidence["command_id"],
            "timeline_speakers": [
                item["speaker"] for item in evidence["history"]],
        }, ensure_ascii=False, indent=2))
        if args.keep:
            print(f"保留临时 repo：{root}")
    finally:
        if not args.keep:
            shutil.rmtree(root)


if __name__ == "__main__":
    main()

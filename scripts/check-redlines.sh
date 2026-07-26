#!/usr/bin/env bash
# myagents 红线守门：与 HARNESS.md §8 一一对应。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path.cwd()
errors: list[str] = []


def fail(rule: str, path: pathlib.Path, node: ast.AST, message: str) -> None:
    rel = path.relative_to(ROOT)
    line = getattr(node, "lineno", 1)
    errors.append(f"[{rule}] {rel}:{line}: {message}")


def parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


production = [
    path for path in ROOT.rglob("*.py")
    if not any(part in {".venv", "tests", "scripts", "__pycache__"}
               for part in path.relative_to(ROOT).parts)
]

# R1: production call sites may not opt into auto permission. Function defaults
# for the three production ACP constructors must remain deny.
for path in production:
    tree = parse(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (keyword.arg == "permission"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == "auto"):
                    fail("R1", path, node,
                         '生产构造显式使用 permission="auto"；改为 deny，'
                         "需要 auto 时由明确授权的外部调用临时注入")

required_deny_defaults = {
    ROOT / "acp/client.py": {"AcpClient"},
    ROOT / "acp/adapter.py": {"AcpAdapter", "AcpKimiAdapter"},
}
for path, classes in required_deny_defaults.items():
    tree = parse(path)
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in classes:
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name != "__init__":
                continue
            args = item.args.args
            defaults = [None] * (len(args) - len(item.args.defaults)) + list(
                item.args.defaults)
            for arg, default in zip(args, defaults):
                if arg.arg == "permission":
                    found.add(node.name)
                    if not (isinstance(default, ast.Constant)
                            and default.value == "deny"):
                        fail("R1", path, item,
                             f"{node.name} permission 默认值必须是 deny")
    missing = classes - found
    for name in sorted(missing):
        errors.append(f"[R1] {path.relative_to(ROOT)}: "
                      f"未找到 {name}.__init__ 的 permission=deny 契约")

# R2: generic orchestration/runtime may register names, but may not branch on
# specific worker literals.
worker_names = {"kimi", "codex", "opencode", "claude"}
for path in [ROOT / "orchestrator.py", ROOT / "acp/client.py"]:
    tree = parse(path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        literals = {
            value.value for value in ast.walk(node)
            if isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value in worker_names
        }
        if literals:
            fail("R2", path, node,
                 "通用层按具体 agent 名做条件判断；改用 AgentSpec、"
                 "transport 或 adapter 能力")

# R3: UI/orchestration/host may not spawn shell or subprocesses directly.
for path in [ROOT / "main.py", ROOT / "orchestrator.py", ROOT / "host.py"]:
    tree = parse(path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([alias.name for alias in node.names]
                     if isinstance(node, ast.Import)
                     else [node.module or ""])
            if any(name == "subprocess" or name.startswith("subprocess.")
                   for name in names):
                fail("R3", path, node,
                     "上层不得 import subprocess；进程启动下沉到 transport")
        if isinstance(node, ast.Call):
            target = node.func
            attr = target.attr if isinstance(target, ast.Attribute) else ""
            if attr in {"create_subprocess_exec", "create_subprocess_shell",
                        "system", "popen", "Popen"}:
                fail("R3", path, node,
                     f"上层直接调用 {attr}；改由 ACP/JSONL adapter 执行")

# R4: production Kimi registration remains ACP-first.
orchestrator = ROOT / "orchestrator.py"
source = orchestrator.read_text(encoding="utf-8")
tree = parse(orchestrator)
for node in ast.walk(tree):
    if isinstance(node, ast.ImportFrom) and node.module == "adapters.kimi_adapter":
        fail("R4", orchestrator, node,
             "生产 orchestrator 不得导入旧 KimiAdapter JSONL 路径")
if 'AgentSpec("kimi", "acp", AcpKimiAdapter)' not in source:
    errors.append("[R4] orchestrator.py: Kimi 生产注册必须是 "
                  'AgentSpec("kimi", "acp", AcpKimiAdapter)')

if errors:
    print("红线检查失败：")
    for error in errors:
        print(f"  - {error}")
    print("请读取 HARNESS.md §8 与 docs/workflow.md 对应场景后修复。")
    sys.exit(1)

print("✓ R1 权限默认 fail-closed")
print("✓ R2 通用层无 agent-name 协议分支")
print("✓ R3 子进程仅由 transport 层启动")
print("✓ R4 Kimi 生产路径保持 ACP-first")
PY

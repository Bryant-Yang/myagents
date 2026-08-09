#!/usr/bin/env bash
# myagents 红线守门：与 HARNESS.md §8 一一对应。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import ast
import json
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
    ROOT / "acp/adapter.py": {
        "AcpAdapter", "AcpKimiAdapter", "AcpOpenCodeAdapter"},
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

# R4: production Kimi/OpenCode remain ACP-first. JSONL is allowed only behind
# the prepare-only seam and must use each runtime's checked read-only profile.
orchestrator = ROOT / "orchestrator.py"
source = orchestrator.read_text(encoding="utf-8")
tree = parse(orchestrator)
for node in ast.walk(tree):
    if (isinstance(node, ast.ImportFrom)
            and node.module in {
                "adapters.kimi_adapter", "adapters.opencode_adapter"}):
        fail("R4", orchestrator, node,
             "生产 orchestrator 不得直接导入 worker JSONL adapter")
if 'AgentSpec("kimi", "acp+jsonl", AcpKimiAdapter)' not in source:
    errors.append("[R4] orchestrator.py: Kimi 生产注册必须是 "
                  'AgentSpec("kimi", "acp+jsonl", AcpKimiAdapter)')
if 'AgentSpec("opencode", "acp+jsonl", AcpOpenCodeAdapter)' not in source:
    errors.append("[R4] orchestrator.py: OpenCode 生产注册必须是 "
                  'AgentSpec("opencode", "acp+jsonl", '
                  "AcpOpenCodeAdapter)")

acp_adapter = ROOT / "acp/adapter.py"
acp_source = acp_adapter.read_text(encoding="utf-8")
if "KimiAdapter.readonly_fallback()" not in acp_source:
    errors.append(
        "[R4] acp/adapter.py: AcpKimiAdapter 必须通过 "
        "KimiAdapter.readonly_fallback() 构造受限降级")
if "OpenCodeAdapter.readonly_fallback()" not in acp_source:
    errors.append(
        "[R4] acp/adapter.py: AcpOpenCodeAdapter 必须通过 "
        "OpenCodeAdapter.readonly_fallback() 构造受限降级")

expected_opencode_policy = {
    "*": "ask",
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "lsp": "allow",
    "todowrite": "allow",
    "edit": "ask",
    "bash": "ask",
    "task": "ask",
    "skill": "ask",
    "webfetch": "ask",
    "websearch": "ask",
    "external_directory": "ask",
}
opencode_policy = None
for node in parse(acp_adapter).body:
    if not isinstance(node, ast.Assign):
        continue
    if any(isinstance(target, ast.Name)
           and target.id == "OPENCODE_ACP_PERMISSION_POLICY"
           for target in node.targets):
        try:
            opencode_policy = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            opencode_policy = None
if opencode_policy != expected_opencode_policy:
    errors.append(
        "[R4] acp/adapter.py: OPENCODE_ACP_PERMISSION_POLICY 必须保持 "
        "unknown/risky=ask、read/search=allow")

profile = ROOT / "adapters/kimi_readonly_fallback.md"
if not profile.is_file():
    errors.append(
        "[R4] adapters/kimi_readonly_fallback.md: 缺少 Kimi 只读 fallback profile")
else:
    profile_source = profile.read_text(encoding="utf-8")
    parts = profile_source.split("---", 2)
    if len(parts) < 3:
        errors.append(
            "[R4] adapters/kimi_readonly_fallback.md: 缺少 YAML frontmatter")
    else:
        frontmatter = parts[1].splitlines()
        tools: list[str] = []
        reading_tools = False
        subagents_disabled = False
        for raw in frontmatter:
            stripped = raw.strip()
            if stripped == "tools:":
                reading_tools = True
                continue
            if reading_tools and raw.startswith("  - "):
                tools.append(raw[4:].strip())
                continue
            if reading_tools and stripped:
                reading_tools = False
            if stripped == "subagents: []":
                subagents_disabled = True
        if tools != ["Read", "Grep", "Glob"]:
            errors.append(
                "[R4] adapters/kimi_readonly_fallback.md: tools 必须精确为 "
                "[Read, Grep, Glob]；禁止 Bash/Write/Edit/Skill/Agent/MCP")
        if not subagents_disabled:
            errors.append(
                "[R4] adapters/kimi_readonly_fallback.md: 必须设置 subagents: []")

opencode_profile = ROOT / "adapters/opencode_readonly_fallback.json"
opencode_jsonl = ROOT / "adapters/opencode_adapter.py"
opencode_jsonl_source = opencode_jsonl.read_text(encoding="utf-8")
expected_readonly_permission = {
    "*": "deny",
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
}
if '"OPENCODE_PERMISSION": json.dumps(' not in opencode_jsonl_source:
    errors.append(
        "[R4] adapters/opencode_adapter.py: fallback 必须通过 "
        "OPENCODE_PERMISSION 运行时覆盖执行 deny-all 白名单")
if not opencode_profile.is_file():
    errors.append(
        "[R4] adapters/opencode_readonly_fallback.json: 缺少 OpenCode "
        "只读 fallback profile")
else:
    try:
        data = json.loads(opencode_profile.read_text(encoding="utf-8"))
        readonly = data["agent"]["myagents-readonly-fallback"]
    except (json.JSONDecodeError, KeyError, TypeError):
        errors.append(
            "[R4] adapters/opencode_readonly_fallback.json: profile 结构无效")
    else:
        if readonly.get("mode") != "primary":
            errors.append(
                "[R4] adapters/opencode_readonly_fallback.json: agent mode "
                "必须是 primary")
        if readonly.get("permission") != expected_readonly_permission:
            errors.append(
                "[R4] adapters/opencode_readonly_fallback.json: permission "
                "必须精确为 deny-all + read/glob/grep/list allow")

# R5: discussion scheduling is explicit and hard-bounded.  Models produce
# content only; the orchestrator must not recursively dispatch another command.
discussion = ROOT / "discussion.py"
if not discussion.is_file():
    errors.append("[R5] discussion.py: 缺少有界讨论状态定义")
else:
    discussion_tree = parse(discussion)
    expected_bounds = {
        "MIN_DISCUSSION_PARTICIPANTS": 2,
        "MAX_DISCUSSION_PARTICIPANTS": 3,
        "MIN_DISCUSSION_ROUNDS": 1,
        "MAX_DISCUSSION_ROUNDS": 3,
        "MAX_DISCUSSION_TOPIC_CHARS": 3000,
    }
    found_bounds: dict[str, object] = {}
    for node in discussion_tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in expected_bounds:
                try:
                    found_bounds[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    found_bounds[target.id] = None
    if found_bounds != expected_bounds:
        errors.append(
            "[R5] discussion.py: 讨论边界必须保持 2..3 个参与者、1..3 轮")

discussion_dispatch = None
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef) \
            and node.name == "_dispatch_discussion":
        discussion_dispatch = node
        break
if discussion_dispatch is None:
    errors.append(
        "[R5] orchestrator.py: 缺少 _dispatch_discussion 有界状态机")
else:
    for node in ast.walk(discussion_dispatch):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if (isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == "dispatch"):
            fail("R5", orchestrator, node,
                 "讨论状态机不得递归 dispatch；轮次必须在单 command 内推进")
    called_names = {
        node.func.id for node in ast.walk(discussion_dispatch)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    if not {"participant_assignment", "moderator_assignment"} \
            <= called_names:
        errors.append(
            "[R5] orchestrator.py: 讨论必须由确定性参与者轮次和终局主持收口")

# R6: milestone workflow keeps fixed roles/stages, one writer, one repair,
# strict steering bounds, and execution modes at the adapter seam.
workflow = ROOT / "workflow.py"
if not workflow.is_file():
    errors.append("[R6] workflow.py: 缺少有界里程碑 workflow 深模块")
else:
    workflow_tree = parse(workflow)
    workflow_source = workflow.read_text(encoding="utf-8")
    expected_bounds = {
        "MAX_GOAL_CHARS": 3000,
        "MAX_RESULT_BYTES": 4096,
        "MAX_FINDINGS": 32,
        "MAX_FINDING_CHARS": 64,
        "MAX_STEERING_ITEMS": 5,
        "MAX_STEERING_CHARS": 1000,
        "MAX_STEERING_TOTAL_CHARS": 4000,
    }
    found_bounds: dict[str, object] = {}
    for node in workflow_tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in expected_bounds:
                try:
                    found_bounds[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    found_bounds[target.id] = None
    if found_bounds != expected_bounds:
        errors.append(
            "[R6] workflow.py: goal/result/finding/steering 有界常量被放宽")
    if '_STEERABLE = frozenset({"review", "implement", "repair"})' \
            not in workflow_source:
        errors.append(
            "[R6] workflow.py: steering 只能在 review/implement/repair 开放")

    run_method = next((
        item for node in workflow_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MilestoneWorkflow"
        for item in node.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "run"
    ), None)
    if run_method is None:
        errors.append("[R6] workflow.py: 缺少 MilestoneWorkflow.run")
    else:
        calls = [
            node.func.attr for node in ast.walk(run_method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ]
        if calls.count("_write_stage") != 2 \
                or calls.count("_read_stage") != 3 \
                or calls.count("_final") != 1:
            errors.append(
                "[R6] workflow.py: 必须保持 review/implement/verify、"
                "最多一次 repair/reverify 和一次 final")

    required_mode_source = (
        "ExecutionMode.READ_ONLY",
        "ExecutionMode.WORKSPACE_WRITE",
    )
    if not all(value in workflow_source for value in required_mode_source):
        errors.append(
            "[R6] workflow.py: 读写阶段必须显式传递 adapter execution mode")

workflow_dispatch = next((
    node for node in ast.walk(tree)
    if isinstance(node, ast.AsyncFunctionDef)
    and node.name == "_dispatch_workflow"
), None)
if workflow_dispatch is None:
    errors.append("[R6] orchestrator.py: 缺少 _dispatch_workflow")
else:
    for node in ast.walk(workflow_dispatch):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr == "dispatch"):
            fail("R6", orchestrator, node,
                 "workflow 不得递归 dispatch 或创建嵌套 command")

for path in [ROOT / "workflow.py"]:
    if path.is_file():
        for node in ast.walk(parse(path)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([alias.name for alias in node.names]
                         if isinstance(node, ast.Import)
                         else [node.module or ""])
                if any(name == "subprocess" or name.startswith("subprocess.")
                       for name in names):
                    fail("R6", path, node,
                         "workflow 不得启动 git；必须注入 WorkspaceInspector")

if errors:
    print("红线检查失败：")
    for error in errors:
        print(f"  - {error}")
    print("请读取 HARNESS.md §8 与 docs/workflow.md 对应场景后修复。")
    sys.exit(1)

print("✓ R1 权限默认 fail-closed")
print("✓ R2 通用层无 agent-name 协议分支")
print("✓ R3 子进程仅由 transport 层启动")
print("✓ R4 Kimi/OpenCode ACP-first + prepare-only 只读 JSONL fallback")
print("✓ R5 /discuss 参与者/轮次有界且不递归 dispatch")
print("✓ R6 /workflow 固定阶段/单 writer/一次 repair/steering 有界")
PY

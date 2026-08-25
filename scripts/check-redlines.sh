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
import re
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
# for the production ACP constructors must remain deny.
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
        "AcpAdapter", "AcpKimiAdapter", "AcpOpenCodeAdapter",
        "AcpQwenAdapter", "AcpWorkBuddyAdapter"},
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
worker_names = {
    "kimi", "codex", "opencode", "qwen", "workbuddy", "pi", "claude"}
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
for path in [
        ROOT / "main.py", ROOT / "orchestrator.py", ROOT / "host.py",
        ROOT / "agent_readiness.py"]:
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

# R4: production Kimi/OpenCode remain constrained ACP-first; Qwen/WorkBuddy
# remain ACP-only; Pi remains attested RPC-only. JSONL is allowed only behind
# each verified prepare-only seam.
orchestrator = ROOT / "orchestrator.py"
source = orchestrator.read_text(encoding="utf-8")
tree = parse(orchestrator)
for node in ast.walk(tree):
    if (isinstance(node, ast.ImportFrom)
            and node.module in {
                "adapters.kimi_adapter", "adapters.opencode_adapter"}):
        fail("R4", orchestrator, node,
             "生产 orchestrator 不得直接导入 worker JSONL adapter")
registered_specs: set[tuple[str, str, str]] = set()
for node in ast.walk(tree):
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AgentSpec"
        and len(node.args) >= 3
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[2], ast.Name)
    ):
        continue
    registered_specs.add((
        str(node.args[0].value),
        str(node.args[1].value),
        node.args[2].id,
    ))
expected_specs = {
    ("kimi", "acp+jsonl", "AcpKimiAdapter"),
    ("opencode", "acp+jsonl", "AcpOpenCodeAdapter"),
    ("qwen", "acp", "AcpQwenAdapter"),
    ("workbuddy", "acp", "AcpWorkBuddyAdapter"),
    ("pi", "rpc", "PiRpcAdapter"),
}
for name, transport, factory in sorted(expected_specs - registered_specs):
    errors.append(
        f"[R4] orchestrator.py: {name} 生产注册必须是 "
        f'AgentSpec("{name}", "{transport}", {factory}, ...)')

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
expected_opencode_readonly_policy = {
    "*": "deny",
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "lsp": "allow",
    "todowrite": "allow",
}
opencode_policies = {}
for node in parse(acp_adapter).body:
    if not isinstance(node, ast.Assign):
        continue
    names = {
        target.id for target in node.targets if isinstance(target, ast.Name)
    }
    matched = names & {
        "OPENCODE_ACP_PERMISSION_POLICY",
        "OPENCODE_ACP_READ_ONLY_PERMISSION_POLICY",
    }
    for name in matched:
        try:
            opencode_policies[name] = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            opencode_policies[name] = None
if (opencode_policies.get("OPENCODE_ACP_PERMISSION_POLICY")
        != expected_opencode_policy):
    errors.append(
        "[R4] acp/adapter.py: OPENCODE_ACP_PERMISSION_POLICY 必须保持 "
        "unknown/risky=ask、read/search=allow")
if (opencode_policies.get("OPENCODE_ACP_READ_ONLY_PERMISSION_POLICY")
        != expected_opencode_readonly_policy):
    errors.append(
        "[R4] acp/adapter.py: OPENCODE_ACP_READ_ONLY_PERMISSION_POLICY "
        "必须保持 unknown/risky=deny、read/search=allow")
opencode_adapter_class = next(
    (node for node in parse(acp_adapter).body
     if isinstance(node, ast.ClassDef) and node.name == "AcpOpenCodeAdapter"),
    None,
)
opencode_adapter_source = (
    ast.get_source_segment(acp_source, opencode_adapter_class)
    if opencode_adapter_class is not None else ""
)
if ("execution_env_overrides" not in opencode_adapter_source
        or "ExecutionMode.READ_ONLY" not in opencode_adapter_source
        or "OPENCODE_ACP_READ_ONLY_PERMISSION_POLICY"
        not in opencode_adapter_source):
    errors.append(
        "[R4] acp/adapter.py: OpenCode read_only 必须注入独立 runtime policy")

expected_qwen_commands = {
    "QWEN_ACP_DEFAULT_CMD": (
        "qwen", "--acp", "--approval-mode", "default"),
    "QWEN_ACP_READ_ONLY_CMD": (
        "qwen", "--acp", "--approval-mode", "plan"),
}
qwen_commands = {}
for node in parse(acp_adapter).body:
    if not isinstance(node, ast.Assign):
        continue
    for target in node.targets:
        if (isinstance(target, ast.Name)
                and target.id in expected_qwen_commands):
            try:
                qwen_commands[target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                qwen_commands[target.id] = None
for name, expected in expected_qwen_commands.items():
    if qwen_commands.get(name) != expected:
        errors.append(
            f"[R4] acp/adapter.py: {name} 必须固定 Qwen runtime approval profile")
qwen_adapter_class = next(
    (node for node in parse(acp_adapter).body
     if isinstance(node, ast.ClassDef) and node.name == "AcpQwenAdapter"),
    None,
)
qwen_adapter_source = (
    ast.get_source_segment(acp_source, qwen_adapter_class)
    if qwen_adapter_class is not None else ""
)
if ("execution_cmd_overrides" not in qwen_adapter_source
        or "ExecutionMode.READ_ONLY" not in qwen_adapter_source
        or "QWEN_ACP_DEFAULT_CMD" not in qwen_adapter_source
        or "QWEN_ACP_READ_ONLY_CMD" not in qwen_adapter_source):
    errors.append(
        "[R4] acp/adapter.py: Qwen 必须普通轮 default、read_only 轮 plan")

expected_workbuddy_args = {
    "WORKBUDDY_ACP_DEFAULT_ARGS": (
        "--acp", "--acp-transport", "stdio",
        "--permission-mode", "default",
        "--subagent-permission-mode", "dontAsk",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "",
    ),
    "WORKBUDDY_ACP_READ_ONLY_ARGS": (
        "--acp", "--acp-transport", "stdio",
        "--permission-mode", "dontAsk",
        "--subagent-permission-mode", "dontAsk",
        "--tools", "Read,Glob,Grep",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "",
    ),
}
workbuddy_args = {}
for node in parse(acp_adapter).body:
    if not isinstance(node, ast.Assign):
        continue
    for target in node.targets:
        if (isinstance(target, ast.Name)
                and target.id in expected_workbuddy_args):
            try:
                workbuddy_args[target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                workbuddy_args[target.id] = None
for name, expected in expected_workbuddy_args.items():
    if workbuddy_args.get(name) != expected:
        errors.append(
            f"[R4] acp/adapter.py: {name} 必须固定 WorkBuddy runtime profile")
workbuddy_adapter_class = next(
    (node for node in parse(acp_adapter).body
     if isinstance(node, ast.ClassDef) and node.name == "AcpWorkBuddyAdapter"),
    None,
)
workbuddy_adapter_source = (
    ast.get_source_segment(acp_source, workbuddy_adapter_class)
    if workbuddy_adapter_class is not None else ""
)
if ("execution_cmd_overrides" not in workbuddy_adapter_source
        or "ExecutionMode.READ_ONLY" not in workbuddy_adapter_source
        or "WORKBUDDY_ACP_DEFAULT_ARGS" not in workbuddy_adapter_source
        or "WORKBUDDY_ACP_READ_ONLY_ARGS" not in workbuddy_adapter_source
        or "MYAGENTS_WORKBUDDY_AUTH_METHOD" not in workbuddy_adapter_source
        or "_workbuddy_auth_notification" not in workbuddy_adapter_source
        or "auth_timeout" not in workbuddy_adapter_source
        or "auth_required=True" not in workbuddy_adapter_source
        or "authenticate_on_demand=True" not in workbuddy_adapter_source
        or "CODEBUDDY_INTERNET_ENVIRONMENT" not in workbuddy_adapter_source
        or '"internal"' not in workbuddy_adapter_source):
    errors.append(
        "[R4] acp/adapter.py: WorkBuddy 必须普通轮 default、read_only 轮 "
        "dontAsk + 只读工具闭集，固定中国区环境、按需有界认证并通过进程级 "
        "profile 隔离")
acp_base_class = next(
    (node for node in parse(acp_adapter).body
     if isinstance(node, ast.ClassDef) and node.name == "AcpAdapter"),
    None,
)
auth_required_guard = next(
    (node for node in (acp_base_class.body if acp_base_class else [])
     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
     and node.name == "_is_authentication_required"),
    None,
)
auth_required_guard_source = (
    ast.get_source_segment(acp_source, auth_required_guard)
    if auth_required_guard is not None else ""
)
if ("type(exc.code) is int" not in auth_required_guard_source
        or 'exc.remote_message == "Authentication required"'
        not in auth_required_guard_source):
    errors.append(
        "[R4] acp/adapter.py: WorkBuddy 按需认证只允许原生整数 -32000 与"
        "精确 Authentication required 消息触发")
if ("_WORKBUDDY_APP_CLI" in acp_source
        or "/Applications/WorkBuddy.app" in acp_source):
    errors.append(
        "[R4] acp/adapter.py: WorkBuddy 只能使用可独立运行的官方 CLI，"
        "不得回退到 App 包内私有二进制")
workbuddy_resolver = next(
    (node for node in parse(acp_adapter).body
     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
     and node.name == "_resolve_workbuddy_cli"),
    None,
)
workbuddy_finder = next(
    (node for node in parse(acp_adapter).body
     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
     and node.name == "_find_workbuddy_cli"),
    None,
)
workbuddy_validator = next(
    (node for node in parse(acp_adapter).body
     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
     and node.name == "_validated_workbuddy_cli"),
    None,
)
workbuddy_resolver_source = (
    ast.get_source_segment(acp_source, workbuddy_resolver)
    if workbuddy_resolver is not None else ""
)
workbuddy_finder_source = (
    ast.get_source_segment(acp_source, workbuddy_finder)
    if workbuddy_finder is not None else ""
)
workbuddy_validator_source = (
    ast.get_source_segment(acp_source, workbuddy_validator)
    if workbuddy_validator is not None else ""
)
if workbuddy_finder_source.count("_validated_workbuddy_cli") < 2:
    errors.append(
        "[R4] acp/adapter.py: WorkBuddy 显式 CLI 与 PATH 候选都必须经过"
        "独立可执行文件校验")
if ("resolve(strict=True)" not in workbuddy_validator_source
        or "os.access" not in workbuddy_validator_source
        or "os.X_OK" not in workbuddy_validator_source
        or 'endswith(".app")' not in workbuddy_validator_source
        or '== "contents"' not in workbuddy_validator_source):
    errors.append(
        "[R4] acp/adapter.py: WorkBuddy CLI 校验必须 canonicalize、要求可执行，"
        "并拒绝 App bundle 内目标（含符号链接）")

# Pi is a separate native RPC transport.  Its process is safe to expose only
# when the fixed extension, exact wrapper-tool closure, startup attestation,
# and three execution profiles stay inseparable.  There is no Pi JSONL/ACP
# fallback and the deliberately small client may never expose raw RPC bash.
pi_adapter = ROOT / "pi_rpc/adapter.py"
pi_client = ROOT / "pi_rpc/client.py"
pi_bridge = ROOT / "pi_rpc/extensions/myagents_permission_bridge.ts"
for required_path in (pi_adapter, pi_client, pi_bridge):
    if not required_path.is_file():
        errors.append(
            f"[R4] {required_path.relative_to(ROOT)}: Pi RPC 受控路径缺失")

expected_pi_read_tools = (
    "myagents_read",
    "myagents_grep",
    "myagents_find",
    "myagents_ls",
)
expected_pi_mutating_tools = (
    "myagents_edit",
    "myagents_write",
    "myagents_bash",
)
expected_pi_tools = expected_pi_read_tools + expected_pi_mutating_tools

if pi_adapter.is_file():
    pi_adapter_source = pi_adapter.read_text(encoding="utf-8")
    pi_adapter_tree = parse(pi_adapter)
    pi_constants: dict[str, object] = {}
    for node in pi_adapter_tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Name)
                    and target.id in {
                        "PI_READ_ONLY_TOOLS", "PI_MUTATING_TOOLS",
                        "PI_POLICY_VERSION", "PI_ATTEST_PREFIX",
                        "PI_PERMISSION_PREFIX", "PI_READY_STATUS_KEY",
                        "PI_POLICY_COMMAND", "_POLICY_NONCE_ENV",
                        "_POLICY_HASH_ENV", "_POLICY_PATH_ENV",
                        "_PROFILE_ENV", "_WORKSPACE_ENV",
                    }):
                try:
                    pi_constants[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pi_constants[target.id] = None
    expected_pi_constants = {
        "PI_READ_ONLY_TOOLS": expected_pi_read_tools,
        "PI_MUTATING_TOOLS": expected_pi_mutating_tools,
        "PI_POLICY_VERSION": "myagents.pi.policy/v1",
        "PI_ATTEST_PREFIX": "MYAGENTS_PI_ATTEST_V1:",
        "PI_PERMISSION_PREFIX": "MYAGENTS_PI_PERMISSION_V1:",
        "PI_READY_STATUS_KEY": "myagents.pi.policy",
        "PI_POLICY_COMMAND": "myagents-policy-v1",
        "_POLICY_NONCE_ENV": "MYAGENTS_PI_POLICY_NONCE",
        "_POLICY_HASH_ENV": "MYAGENTS_PI_POLICY_HASH",
        "_POLICY_PATH_ENV": "MYAGENTS_PI_POLICY_PATH",
        "_PROFILE_ENV": "MYAGENTS_PI_PROFILE",
        "_WORKSPACE_ENV": "MYAGENTS_PI_WORKSPACE",
    }
    if pi_constants != expected_pi_constants:
        errors.append(
            "[R4] pi_rpc/adapter.py: Pi policy/version/prefix 与 wrapper "
            "工具闭集必须保持固定")

    pi_adapter_class = next((
        node for node in pi_adapter_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PiRpcAdapter"
    ), None)
    pi_adapter_class_source = (
        ast.get_source_segment(pi_adapter_source, pi_adapter_class)
        if pi_adapter_class is not None else ""
    )
    pi_init = next((
        node for node in (pi_adapter_class.body if pi_adapter_class else [])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "__init__"
    ), None)
    permission_default = object()
    if pi_init is not None:
        for arg, default in zip(
                pi_init.args.kwonlyargs, pi_init.args.kw_defaults):
            if arg.arg == "permission_handler":
                permission_default = default
                break
    if not (isinstance(permission_default, ast.Constant)
            and permission_default.value is None):
        errors.append(
            "[R1] pi_rpc/adapter.py: PiRpcAdapter permission_handler "
            "默认值必须为 None（deny）")

    command_method = next((
        node for node in (pi_adapter_class.body if pi_adapter_class else [])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_command"
    ), None)
    command_source = (
        ast.get_source_segment(pi_adapter_source, command_method)
        if command_method is not None else ""
    )
    command_literals = {
        node.value for node in ast.walk(command_method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } if command_method is not None else set()
    required_pi_argv = {
        "--mode", "rpc", "--offline", "--no-approve", "--no-extensions",
        "--extension", "--no-skills", "--no-prompt-templates",
        "--no-themes", "--no-builtin-tools", "--tools", "--session-dir",
    }
    if (not required_pi_argv <= command_literals
            or "_tools_for" not in command_source
            or "_bridge_path" not in command_source
            or "json" in command_literals
            or "acp" in command_literals):
        errors.append(
            "[R4] pi_rpc/adapter.py: Pi argv 必须固定 rpc、关闭自动发现、"
            "只显式加载 bridge 并传入 profile wrapper 闭集")
    extension_flags = [
        node for node in ast.walk(command_method)
        if isinstance(node, ast.Constant)
        and node.value == "--extension"
    ] if command_method is not None else []
    if len(extension_flags) != 1:
        errors.append(
            "[R4] pi_rpc/adapter.py: Pi 只能显式加载一个 permission bridge")

    required_pi_adapter_seams = {
        "extensions", "myagents_permission_bridge.ts",
        "_POLICY_NONCE_ENV", "_POLICY_HASH_ENV", "_POLICY_PATH_ENV",
        "_PROFILE_ENV", "_WORKSPACE_ENV", "_handle_attestation",
        "_handle_ready_status", "_validate_policy_command",
        "sourceInfo", "hashlib.sha256", "ExecutionMode.READ_ONLY",
        "resume_session_id = None", "delivery_committed",
        "delivery_maybe_sent", "await self._reset_locked()",
        "_prompt_permission_gate", "permission_gate.wait()",
        "permission_gate.set()", "tool_name not in self._expected_tools",
        "last_assistant_end", "terminal_tool_failure",
        "_SESSION_MARKER_VERSION", "_read_session_marker",
        "_write_session_marker", 'state="materialized"',
        'expected_previous="reserved"', '"PI_OFFLINE": "1"',
        "last_assistant_end is None",
        "max_total_bytes=MAX_CLIPBOARD_IMAGE_BYTES",
        "max_images=_MAX_PROMPT_IMAGES",
        "_policy_generation", "_reclaim_policy_generation",
        "maxsize=_PERMISSION_EVENT_QUEUE_LIMIT",
        "raw_task = asyncio.create_task(anext(raw_stream))",
    }
    if not all(value in pi_adapter_class_source
               for value in required_pi_adapter_seams):
        errors.append(
            "[R4] pi_rpc/adapter.py: Pi 必须在首个 prompt 前完成 bridge/"
            "nonce/profile/workspace/tool-source attestation，并在 profile "
            "切换时重建进程和 fresh session")
    forbidden_pi_adapter_calls = {
        node.func.attr for node in ast.walk(pi_adapter_class)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and "fallback" in node.func.attr.lower()
    } if pi_adapter_class is not None else {"missing"}
    if forbidden_pi_adapter_calls:
        errors.append(
            "[R4] pi_rpc/adapter.py: Pi RPC-only 不得调用任何 fallback seam")

if pi_client.is_file():
    pi_client_source = pi_client.read_text(encoding="utf-8")
    pi_client_tree = parse(pi_client)
    pi_client_class = next((
        node for node in pi_client_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PiRpcClient"
    ), None)
    public_methods = {
        node.name for node in (pi_client_class.body if pi_client_class else [])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    expected_pi_client_public = {
        "pid", "running", "start", "get_state", "get_commands", "prompt",
        "abort", "extension_ui_response", "close",
    }
    if public_methods != expected_pi_client_public:
        errors.append(
            "[R4] pi_rpc/client.py: Pi client 公共面必须保持固定且不得公开 "
            "raw request/send/bash API")
    required_pi_client_seams = {
        "_EVENT_QUEUE_BYTE_LIMIT", "_active_prompt_event_bytes",
        "_queue_prompt_event", "_next_prompt_event",
        "_EXTENSION_UI_TASK_LIMIT", "duplicate extension UI request id",
    }
    if not all(value in pi_client_source
               for value in required_pi_client_seams):
        errors.append(
            "[R4] pi_rpc/client.py: Pi 事件流必须有累计字节预算，"
            "extension UI 必须限制并发并拒绝活跃重复 id")
    for node in ast.walk(pi_client_tree):
        if not isinstance(node, ast.Dict):
            continue
        pairs = {
            key.value: value.value
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        }
        if pairs.get("type") == "bash":
            fail("R4", pi_client, node,
                 "生产 client 禁止发送 raw Pi RPC type=bash；必须走权限 wrapper")
    extension_response_method = next((
        node for node in (pi_client_class.body if pi_client_class else [])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "extension_ui_response"
    ), None)
    extension_response_source = (
        ast.get_source_segment(pi_client_source, extension_response_method)
        if extension_response_method is not None else ""
    )
    if "_validated_extension_response" not in extension_response_source:
        errors.append(
            "[R4] pi_rpc/client.py: extension UI response 必须先做严格 shape/id 校验")

if pi_bridge.is_file():
    bridge_source = pi_bridge.read_text(encoding="utf-8")
    wrapper_match = re.search(
        r"export\s+const\s+WRAPPER_TOOL_NAMES\s*=\s*\[(.*?)\]\s*as\s+const",
        bridge_source,
        re.DOTALL,
    )
    wrapper_names = (
        tuple(re.findall(r'"([^"]+)"', wrapper_match.group(1)))
        if wrapper_match is not None else ()
    )
    if wrapper_names != expected_pi_tools:
        errors.append(
            "[R4] pi_rpc/extensions/myagents_permission_bridge.ts: wrapper "
            "工具必须精确为固定七项且顺序稳定")
    required_bridge_tokens = {
        '"myagents.pi.policy/v1"', '"MYAGENTS_PI_ATTEST_V1:"',
        '"MYAGENTS_PI_PERMISSION_V1:"', '"myagents.pi.policy"',
        '"myagents-policy-v1"', '"default"', '"read_only"',
        '"workspace_write"', "MYAGENTS_PI_POLICY_NONCE",
        "MYAGENTS_PI_POLICY_HASH", "MYAGENTS_PI_POLICY_PATH",
        "MYAGENTS_PI_PROFILE", "MYAGENTS_PI_WORKSPACE",
        'pi.on("session_start"', 'pi.on("tool_call"',
        "pi.getActiveTools()", "pi.getAllTools()", "sourceInfoMatches",
        "sameOrderedStrings", "allow_once:", "reject_once:",
        "permits.delete(event.toolCallId)", "canonicalizeInput",
        "rejectUnsafeMutation", "hasGitSegment", "nlink > 1",
        "PERMISSION_PREVIEW_MAX_BYTES = 4 * 1024",
        "PERMIT_TTL_MS = 60_000",
        "permissionDisplayInput", "_myagentsPreview", "TOOL_INPUT_KEYS",
        "assertDisplaySchema", "enforcePermissionDisplayBudget",
        "stableJsonByteLength",
        "argsHash: canonical.digest",
        "bash command is too large to display safely",
        'pi.on("session_start", (_event, ctx) => {',
        "const currentGeneration = ++generation",
        "void (async () => {",
        "if (currentGeneration !== generation) return",
        "generation: callGeneration",
        "permit.generation !== generation",
        "callGeneration !== generation",
    }
    if not all(token in bridge_source for token in required_bridge_tokens):
        errors.append(
            "[R4] pi_rpc/extensions/myagents_permission_bridge.ts: "
            "attestation、非阻塞 generation guard、active-tool/source 核验、"
            "逐次 permit、权限预览或路径边界被放宽")

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

# R5: natural discussion, explicit discussion, and natural-language
# collaboration are hard-bounded. Models produce content/plans only;
# deterministic state machines never redispatch.
discussion = ROOT / "discussion.py"
if not discussion.is_file():
    errors.append("[R5] discussion.py: 缺少有界讨论状态定义")
else:
    discussion_tree = parse(discussion)
    discussion_functions = {
        node.name for node in discussion_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required_discussion_functions = {
        "parse_discussion_request",
        "parse_natural_discussion_request",
        "parse_discussion_payload",
    }
    missing_discussion_functions = (
        required_discussion_functions - discussion_functions)
    if missing_discussion_functions:
        errors.append(
            "[R5] discussion.py: 自然语言、精确命令与 host payload "
            "必须收口到同一讨论请求，缺少 "
            + ", ".join(sorted(missing_discussion_functions)))
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

collaboration = ROOT / "collaboration.py"
if not collaboration.is_file():
    errors.append("[R5] collaboration.py: 缺少有界有序协作状态定义")
else:
    collaboration_tree = parse(collaboration)
    expected_collaboration_bounds = {
        "MIN_COLLABORATION_STEPS": 2,
        "MAX_COLLABORATION_STEPS": 4,
        "MAX_COLLABORATION_ASSIGNMENT_CHARS": 4000,
    }
    found_collaboration_bounds: dict[str, object] = {}
    for node in collaboration_tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Name)
                    and target.id in expected_collaboration_bounds):
                try:
                    found_collaboration_bounds[target.id] = ast.literal_eval(
                        node.value)
                except (ValueError, TypeError):
                    found_collaboration_bounds[target.id] = None
    if found_collaboration_bounds != expected_collaboration_bounds:
        errors.append(
            "[R5] collaboration.py: 协作边界必须保持 2..4 步且任务文本有界")

collaboration_dispatch = None
for node in ast.walk(tree):
    if (isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_dispatch_collaboration"):
        collaboration_dispatch = node
        break
if collaboration_dispatch is None:
    errors.append(
        "[R5] orchestrator.py: 缺少 _dispatch_collaboration 串行状态机")
else:
    calls_run_one = False
    has_serial_loop = any(
        isinstance(node, ast.For)
        for node in ast.walk(collaboration_dispatch)
    )
    for node in ast.walk(collaboration_dispatch):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if (isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == "dispatch"):
            fail("R5", orchestrator, node,
                 "有序协作不得递归 dispatch；必须在单 command 内推进")
        if (isinstance(target, ast.Attribute)
                and target.attr in {"gather", "create_task"}):
            fail("R5", orchestrator, node,
                 "有序协作步骤必须严格串行，不得 gather/create_task")
        if (isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == "_run_one"):
            calls_run_one = True
    if not has_serial_loop or not calls_run_one:
        errors.append(
            "[R5] orchestrator.py: 有序协作必须以普通循环串行调用 _run_one")

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
print("✓ R4 Kimi/OpenCode 受限 ACP-first + Qwen/WorkBuddy ACP-only + Pi attested RPC-only")
print("✓ R5 自然语言讨论、/discuss 与有序协作均有界且不递归 dispatch")
print("✓ R6 /workflow 固定阶段/单 writer/一次 repair/steering 有界")
PY

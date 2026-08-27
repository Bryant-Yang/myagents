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
        "AcpQwenAdapter", "AcpCodeBuddyAdapter"},
    ROOT / "dsh_acp/adapter.py": {"AcpDshAdapter"},
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
    "kimi", "codex", "opencode", "qwen", "codebuddy", "workbuddy", "dsh", "pi",
    "claude"}
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

# R4: production Kimi/OpenCode remain constrained ACP-first;
# Qwen/CodeBuddy/DSH remain ACP-only; Pi remains attested RPC-only. JSONL is
# allowed only behind each verified prepare-only seam.
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
    ("codebuddy", "acp", "AcpCodeBuddyAdapter"),
    ("dsh", "acp", "AcpDshAdapter"),
    ("pi", "rpc", "PiRpcAdapter"),
}
for name, transport, factory in sorted(expected_specs - registered_specs):
    errors.append(
        f"[R4] orchestrator.py: {name} 生产注册必须是 "
        f'AgentSpec("{name}", "{transport}", {factory}, ...)')
if any(name == "workbuddy" for name, _transport, _factory in registered_specs):
    errors.append(
        "[R4] orchestrator.py: 旧 @workbuddy 不得继续注册；"
        "独立 CLI 的产品身份必须是 @codebuddy")

acp_adapter = ROOT / "acp/adapter.py"
acp_source = acp_adapter.read_text(encoding="utf-8")
for legacy_name in (
    "AcpWorkBuddyAdapter",
    "MYAGENTS_WORKBUDDY_CLI",
    "MYAGENTS_WORKBUDDY_AUTH_METHOD",
    "WORKBUDDY_ACP_DEFAULT_ARGS",
    "WORKBUDDY_ACP_READ_ONLY_ARGS",
):
    if legacy_name in acp_source:
        errors.append(
            f"[R4] acp/adapter.py: 旧 CodeBuddy 身份标识仍存在：{legacy_name}")
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

expected_codebuddy_args = {
    "CODEBUDDY_ACP_DEFAULT_ARGS": (
        "--acp", "--acp-transport", "stdio",
        "--permission-mode", "default",
        "--subagent-permission-mode", "dontAsk",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "",
    ),
    "CODEBUDDY_ACP_READ_ONLY_ARGS": (
        "--acp", "--acp-transport", "stdio",
        "--permission-mode", "dontAsk",
        "--subagent-permission-mode", "dontAsk",
        "--tools", "Read,Glob,Grep",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "",
    ),
}
codebuddy_args = {}
for node in parse(acp_adapter).body:
    if not isinstance(node, ast.Assign):
        continue
    for target in node.targets:
        if (isinstance(target, ast.Name)
                and target.id in expected_codebuddy_args):
            try:
                codebuddy_args[target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                codebuddy_args[target.id] = None
for name, expected in expected_codebuddy_args.items():
    if codebuddy_args.get(name) != expected:
        errors.append(
            f"[R4] acp/adapter.py: {name} 必须固定 CodeBuddy runtime profile")
codebuddy_adapter_class = next(
    (node for node in parse(acp_adapter).body
     if isinstance(node, ast.ClassDef) and node.name == "AcpCodeBuddyAdapter"),
    None,
)
codebuddy_adapter_source = (
    ast.get_source_segment(acp_source, codebuddy_adapter_class)
    if codebuddy_adapter_class is not None else ""
)
if ("execution_cmd_overrides" not in codebuddy_adapter_source
        or "ExecutionMode.READ_ONLY" not in codebuddy_adapter_source
        or "CODEBUDDY_ACP_DEFAULT_ARGS" not in codebuddy_adapter_source
        or "CODEBUDDY_ACP_READ_ONLY_ARGS" not in codebuddy_adapter_source
        or "MYAGENTS_CODEBUDDY_AUTH_METHOD" not in codebuddy_adapter_source
        or "_codebuddy_auth_notification" not in codebuddy_adapter_source
        or "auth_timeout" not in codebuddy_adapter_source
        or "auth_required=True" not in codebuddy_adapter_source
        or "authenticate_on_demand=True" not in codebuddy_adapter_source
        or "CODEBUDDY_INTERNET_ENVIRONMENT" not in codebuddy_adapter_source
        or '"internal"' not in codebuddy_adapter_source):
    errors.append(
        "[R4] acp/adapter.py: CodeBuddy 必须普通轮 default、read_only 轮 "
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
        "[R4] acp/adapter.py: CodeBuddy 按需认证只允许原生整数 -32000 与"
        "精确 Authentication required 消息触发")
if ("_CODEBUDDY_APP_CLI" in acp_source
        or "/Applications/WorkBuddy.app" in acp_source):
    errors.append(
        "[R4] acp/adapter.py: CodeBuddy 只能使用可独立运行的官方 CLI，"
        "不得回退到 App 包内私有二进制")
codebuddy_resolver = next(
    (node for node in parse(acp_adapter).body
     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
     and node.name == "_resolve_codebuddy_cli"),
    None,
)
codebuddy_finder = next(
    (node for node in parse(acp_adapter).body
     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
     and node.name == "_find_codebuddy_cli"),
    None,
)
codebuddy_validator = next(
    (node for node in parse(acp_adapter).body
     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
     and node.name == "_validated_codebuddy_cli"),
    None,
)
codebuddy_resolver_source = (
    ast.get_source_segment(acp_source, codebuddy_resolver)
    if codebuddy_resolver is not None else ""
)
codebuddy_finder_source = (
    ast.get_source_segment(acp_source, codebuddy_finder)
    if codebuddy_finder is not None else ""
)
codebuddy_validator_source = (
    ast.get_source_segment(acp_source, codebuddy_validator)
    if codebuddy_validator is not None else ""
)
if codebuddy_finder_source.count("_validated_codebuddy_cli") < 2:
    errors.append(
        "[R4] acp/adapter.py: CodeBuddy 显式 CLI 与 PATH 候选都必须经过"
        "独立可执行文件校验")
if ("resolve(strict=True)" not in codebuddy_validator_source
        or "os.access" not in codebuddy_validator_source
        or "os.X_OK" not in codebuddy_validator_source
        or 'endswith(".app")' not in codebuddy_validator_source
        or '== "contents"' not in codebuddy_validator_source):
    errors.append(
        "[R4] acp/adapter.py: CodeBuddy CLI 校验必须 canonicalize、要求可执行，"
        "并拒绝 App bundle 内目标（含符号链接）")

# DSH is ACP-only. Both installed and source-backed launches use the official
# CLI with the stock ``myagents`` profile; the product ACP surface is a normal
# @myagents bundle loaded by that profile. Readiness is passive and source mode
# only locates an already-built official CLI.
dsh_adapter = ROOT / "dsh_acp/adapter.py"
dsh_source = dsh_adapter.read_text(encoding="utf-8")
dsh_tree = parse(dsh_adapter)
dsh_adapter_class = next(
    (node for node in dsh_tree.body
     if isinstance(node, ast.ClassDef) and node.name == "AcpDshAdapter"),
    None,
)
dsh_adapter_source = (
    ast.get_source_segment(dsh_source, dsh_adapter_class)
    if dsh_adapter_class is not None else ""
)


def dsh_function_source(name: str) -> str:
    node = next(
        (item for item in dsh_tree.body
         if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
         and item.name == name),
        None,
    )
    return ast.get_source_segment(dsh_source, node) if node is not None else ""


dsh_assignment_values: dict[str, object] = {}
for node in dsh_tree.body:
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        continue
    target = node.targets[0]
    if not isinstance(target, ast.Name):
        continue
    try:
        dsh_assignment_values[target.id] = ast.literal_eval(node.value)
    except (TypeError, ValueError):
        pass

expected_dsh_literals = {
    "_DSH_CLI_ENV": "MYAGENTS_DSH_CLI",
    "_DSH_SOURCE_ROOT_ENV": "MYAGENTS_DSH_SOURCE_ROOT",
    "_DSH_HOME_ENV": "DSH_HOME",
    "_DSH_PERSISTENCE_ENV": "DSH_ACP_PERSISTENCE_DIR",
    "_DSH_RUNTIME_HOME_ENV": "DSH_ACP_RUNTIME_HOME",
    "_DSH_ATTACHMENT_HOME_ENV": "DSH_ACP_ATTACHMENT_HOME",
    "_DSH_SETTINGS_FILE_ENV": "DSH_ACP_SETTINGS_FILE",
    "_DSH_CREDENTIALS_FILE_ENV": "DSH_ACP_CREDENTIALS_FILE",
    "_DSH_AGENTS_HOME_ENV": "DSH_AGENTS_HOME",
    "_DSH_RUNTIME_PROFILE_ENV": "DSH_ACP_PROFILE",
    "_DSH_PROFILE_NAME": "myagents",
    "_DSH_PROFILE_BUNDLES": (
        "@deepseek-ai/dsh-base", "@myagents/dsh-acp-host"),
    "_DSH_PLUGIN_NAME": "@myagents/dsh-acp-host",
    "_DSH_PLUGIN_VERSION": "0.1.0",
    "_DSH_RUNTIME_PACKAGE": "@deepseek-ai/dsh",
    "_DSH_RUNTIME_ROOT_PACKAGE": "@deepseek-ai/dsh-root",
    "_DSH_RUNTIME_VERSION": "0.1.1-rc.2",
    "DSH_ACP_WORKSPACE_PROFILE": "workspace-write",
    "DSH_ACP_READ_ONLY_PROFILE": "read-only",
    "_DSH_AGENT_NAME": "dsh-myagents-acp",
    "_DSH_PROFILE_META_KEY": "deepseek.ai/dsh-myagents-profile",
    "_DSH_POLICY_REVISION_META_KEY": (
        "deepseek.ai/dsh-myagents-policy-revision"),
    "_DSH_READ_ONLY_TOOLS_META_KEY": (
        "deepseek.ai/dsh-myagents-read-only-tools"),
    "_DSH_RUNTIME_VERSION_META_KEY": "deepseek.ai/dsh-runtime-version",
    "_DSH_COMPATIBILITY_REVISION_META_KEY": (
        "deepseek.ai/dsh-compatibility-revision"),
    "_DSH_POLICY_REVISION": 1,
    "_DSH_COMPATIBILITY_REVISION": 1,
    "_DSH_READ_ONLY_TOOLS": ["read", "glob", "grep"],
    "_DSH_PERMISSION_KINDS": {"allow_once", "reject_once"},
    "_DSH_PLUGIN_ENTRY_SHA256": (
        "a8515ac654705e07ea04c2f7c2f3df452500c9ad902652cb0d434af49aba3290"),
    "_DSH_PLUGIN_PATCH_SHA256": (
        "744d7ce4370c0bac08db1b53e0da407005a2e053077648b85adbd69c996afbb2"),
}
observed_dsh_literals = {
    name: dsh_assignment_values.get(name) for name in expected_dsh_literals
}
if (observed_dsh_literals != expected_dsh_literals
        or type(observed_dsh_literals.get("_DSH_POLICY_REVISION")) is not int
        or type(observed_dsh_literals.get(
            "_DSH_COMPATIBILITY_REVISION")) is not int):
    errors.append(
        "[R4] dsh_acp/adapter.py: DSH official CLI/profile/bundle、两种 "
        "execution safety profile 与 identity wire literals 必须保持精确")

required_dsh_adapter_tokens = {
    'Path("apps/cli/package.json")', 'Path("apps/cli/lib/bin.js")',
    "_validated_profile", "_validated_source_launch", "_resolve_dsh_launch",
    "_canonical_profile_directory", "_read_canonical_bounded_file",
    "_validated_empty_user_patch", "_MAX_USER_PATCH_BYTES",
    "_dsh_state_layout", "_validate_state_layout",
    "_validate_static_separation", "_validate_workspace_separation",
    "_read_config_snapshot", "_materialize_product_state",
    "_revalidate_launch", "dsh_readiness_probe", "resolve(strict=True)",
    "O_NOFOLLOW", "_atomic_private_write", "os.fchmod", "0o600",
    '"NODE_OPTIONS"', '"NODE_PATH"', "_DSH_CHILD_ENV_REMOVALS",
}
if (not all(token in dsh_source for token in required_dsh_adapter_tokens)
        or "_DSH_HOST_VERSION = _DSH_PLUGIN_VERSION" not in dsh_source):
    errors.append(
        "[R4] dsh_acp/adapter.py: DSH 必须被动解析官方 launcher/stock profile，"
        "固定标准 bundle identity，并隔离 state/workspace/config")

dsh_profile_source = dsh_function_source("_validated_profile")
required_dsh_profile_tokens = {
    "_DSH_PROFILE_BUNDLES", 'manifest.get("dependencies")',
    "_DSH_PLUGIN_NAME", "_DSH_PLUGIN_VERSION",
    'patch_value != "./cordis.patch.yml"',
    'main_value != "lib/index.js"', "_validated_bundle_artifact",
    "_DSH_PLUGIN_ENTRY_SHA256", "_DSH_PLUGIN_PATCH_SHA256",
    "_MAX_PLUGIN_ENTRY_BYTES", "_MAX_PLUGIN_PATCH_BYTES",
    "_canonical_profile_directory",
    "_read_canonical_bounded_file", "_MAX_MANIFEST_BYTES",
    'profile_home / "cordis.patch.yml"',
    'profile_dir / "cordis.patch.yml"',
}
if (not all(token in dsh_profile_source for token in required_dsh_profile_tokens)
        or dsh_profile_source.count("_validated_empty_user_patch(") != 2):
    errors.append(
        "[R4] dsh_acp/adapter.py: stock myagents profile 必须被动证明 exact "
        "base→host bundles、resolved plugin name/version、main=lib/index.js、"
        "dsh.bundle.patch=./cordis.patch.yml 与两层 later-wins 空 patch")

dsh_profile_dir_source = dsh_function_source("_canonical_profile_directory")
required_dsh_profile_dir_tokens = {
    "path.lstat()", "path.resolve(strict=True)", "stat.S_ISLNK",
    "stat.S_ISDIR", "resolved != lexical", "is_relative_to(profile_home)",
}
if not all(token in dsh_profile_dir_source
           for token in required_dsh_profile_dir_tokens):
    errors.append(
        "[R4] dsh_acp/adapter.py: DSH profiles/profile 必须拒绝 symlink/非目录/"
        "canonical 越界")

dsh_bounded_file_source = dsh_function_source("_read_canonical_bounded_file")
required_dsh_bounded_file_tokens = {
    'getattr(os, "O_NOFOLLOW"', "before.st_nlink != 1",
    "before.st_size > byte_limit", "path.lstat()", "resolve(strict=True)",
    "resolved != path.absolute()", "is_relative_to(parent)",
    "before.st_mtime_ns", "before.st_ctime_ns", "before_id != after_id",
}
if not all(token in dsh_bounded_file_source
           for token in required_dsh_bounded_file_tokens):
    errors.append(
        "[R4] dsh_acp/adapter.py: profile manifest/user patch 必须 no-follow、"
        "单链接、有界且通过读前后 identity 复核")

dsh_user_patch_source = dsh_function_source("_validated_empty_user_patch")
required_dsh_user_patch_tokens = {
    "os.path.lexists", "_MAX_USER_PATCH_BYTES", 'payload.decode("utf-8")',
    'semantic_lines != ["[]"]',
}
if not all(token in dsh_user_patch_source
           for token in required_dsh_user_patch_tokens):
    errors.append(
        "[R4] dsh_acp/adapter.py: bundle 后生效的 DSH home/profile patch 只能"
        "缺失或语义精确为 YAML 空数组 []")

dsh_revalidate_source = dsh_function_source("_revalidate_launch")
if ('_validated_profile(launch.profile_home)' not in dsh_revalidate_source
        or "launch.profile_dir" not in dsh_revalidate_source
        or "launch.plugin_root" not in dsh_revalidate_source):
    errors.append(
        "[R4] dsh_acp/adapter.py: spawn/复用前必须重验 profile、later-wins "
        "patch 与解析后的 bundle identity")

dsh_source_launch_source = dsh_function_source("_validated_source_launch")
required_dsh_source_launch_tokens = {
    "_DSH_RUNTIME_ROOT_PACKAGE", "_DSH_RUNTIME_PACKAGE",
    "_DSH_RUNTIME_VERSION", "_DSH_SOURCE_CLI_PACKAGE",
    "_DSH_SOURCE_CLI_BIN", "_declared_dsh_bin", 'find("node")',
    "_validated_executable",
}
forbidden_dsh_source_launch_tokens = {
    "src/bin.ts", "config/cordis.yml", "node_modules/tsx", "pnpm",
    "subprocess", "TSX_TSCONFIG_PATH", "DSH_ACP_SOURCE_ROOT",
    "DSH_ACP_ENV_DIR", "fingerprint", "git ",
}
if (not all(token in dsh_source_launch_source
            for token in required_dsh_source_launch_tokens)
        or any(token in dsh_source_launch_source
               for token in forbidden_dsh_source_launch_tokens)):
    errors.append(
        "[R4] dsh_acp/adapter.py: MYAGENTS_DSH_SOURCE_ROOT 只能被动定位 "
        "apps/cli/lib/bin.js 与 node，不得恢复 tsx/custom host/build/full-tree gate")

dsh_resolver_source = dsh_function_source("_resolve_dsh_launch")
required_dsh_resolver_tokens = {
    "_absolute_dsh_home", "_validated_profile", "_DSH_CLI_ENV",
    "_DSH_SOURCE_ROOT_ENV", "_validated_cli_package",
    "_validated_source_launch", 'find("dsh")', '"--profile"',
    "_DSH_PROFILE_NAME",
}
if (not all(token in dsh_resolver_source
            for token in required_dsh_resolver_tokens)
        or "subprocess" in dsh_resolver_source
        or "pnpm" in dsh_resolver_source
        or "shlex" in dsh_resolver_source):
    errors.append(
        "[R4] dsh_acp/adapter.py: installed/source 两条路径必须只启动官方 "
        "CLI --profile myagents，resolver 不得 build、执行 readiness probe 或解析 shell words")

dsh_validator_source = dsh_function_source("_validated_executable")
if ("resolve(strict=True)" not in dsh_validator_source
        or "os.access" not in dsh_validator_source
        or "os.X_OK" not in dsh_validator_source):
    errors.append(
        "[R4] dsh_acp/adapter.py: DSH 官方 CLI/node 必须 canonicalize 且可执行")

dsh_home_source = dsh_function_source("_absolute_dsh_home")
if ("_DSH_HOME_ENV" not in dsh_home_source
        or 'Path(home).expanduser() if home else Path.home()' not in dsh_home_source
        or ' / ".dsh"' not in dsh_home_source
        or "resolve(strict=True)" not in dsh_home_source):
    errors.append(
        "[R4] dsh_acp/adapter.py: DSH_HOME 必须保留为可证明存在的 stock profile home")

dsh_persistence_source = dsh_function_source("_dsh_persistence_dir")
if ("is_absolute" not in dsh_persistence_source
        or "raise AcpError" not in dsh_persistence_source
        or 'env.get("XDG_STATE_HOME"' not in dsh_persistence_source
        or 'env.get("HOME"' not in dsh_persistence_source):
    errors.append(
        "[R4] dsh_acp/adapter.py: DSH 显式 persistence override 必须是绝对路径，"
        "默认路径必须来自 myagents state root")

if ("execution_env_overrides" not in dsh_adapter_source
        or "ExecutionMode.READ_ONLY" not in dsh_adapter_source
        or "ExecutionMode.WORKSPACE_WRITE" not in dsh_adapter_source
        or "DSH_ACP_WORKSPACE_PROFILE" not in dsh_adapter_source
        or "DSH_ACP_READ_ONLY_PROFILE" not in dsh_adapter_source
        or "_validate_initialized_client" not in dsh_adapter_source
        or 'capabilities.get("loadSession") is not True'
        not in dsh_adapter_source
        or 'session_capabilities.get("close")' not in dsh_adapter_source
        or "_images_for_prompt" not in dsh_adapter_source
        or '"promptCapabilities"' not in dsh_adapter_source
        or "_dsh_state_layout" not in dsh_adapter_source
        or "_validate_workspace_separation" not in dsh_adapter_source
        or "_revalidate_launch" not in dsh_adapter_source
        or "_materialize_product_state" not in dsh_adapter_source
        or "self._client.cwd = workspace" not in dsh_adapter_source
        or "_DSH_AGENT_NAME" not in dsh_adapter_source
        or "_DSH_PROFILE_META_KEY" not in dsh_adapter_source
        or "_DSH_POLICY_REVISION_META_KEY" not in dsh_adapter_source
        or "_DSH_READ_ONLY_TOOLS_META_KEY" not in dsh_adapter_source
        or "_DSH_RUNTIME_VERSION_META_KEY" not in dsh_adapter_source
        or "_DSH_COMPATIBILITY_REVISION_META_KEY" not in dsh_adapter_source
        or "env_removals=" not in dsh_adapter_source
        or "_DSH_PERMISSION_KINDS" not in dsh_adapter_source
        or "len(option_ids) != len(options)" not in dsh_adapter_source
        or "fallback_adapter" in dsh_adapter_source
        or "jsonl" in dsh_adapter_source.lower()
        or "headless" in dsh_adapter_source.lower()):
    errors.append(
        "[R4] dsh_acp/adapter.py: AcpDshAdapter 必须 ACP-only，以进程级 env "
        "隔离 read_only/workspace_write；首个 session 前 hard-gate load/close，"
        "并绑定 workspace/state/identity/one-shot permission/image")
if "/Users/" in dsh_adapter_source or "/Users/" in dsh_resolver_source:
    errors.append(
        "[R4] dsh_acp/adapter.py: DSH adapter/resolver 禁止硬编码用户目录")
plugin_root = ROOT / "dsh_acp/plugin"
plugin_package = plugin_root / "package.json"
plugin_patch = plugin_root / "cordis.patch.yml"
plugin_index = plugin_root / "src/index.ts"
plugin_acp = plugin_root / "src/acp.ts"
plugin_acp_codec = plugin_root / "src/acp-codec.ts"
plugin_acp_content = plugin_root / "src/acp-content.ts"
plugin_runtime_contract = plugin_root / "runtime-contract.json"
plugin_build = plugin_root / "scripts/build.mjs"
plugin_typecheck = plugin_root / "scripts/typecheck.mjs"
plugin_profile_test = plugin_root / "tests/profile.spec.ts"
plugin_bin_test = plugin_root / "tests/bin.spec.ts"
plugin_bundle_test = plugin_root / "tests/bundle.spec.ts"
plugin_bridge_test = plugin_root / "tests/bridge.spec.ts"
plugin_approval_test = plugin_root / "tests/approval.spec.ts"
plugin_load_test = plugin_root / "tests/load.spec.ts"
plugin_vitest_config = plugin_root / "vitest.config.mjs"
dsh_release_check = ROOT / "scripts/check-dsh-plugin.sh"
dsh_package_script = ROOT / "scripts/package-dsh-plugin.sh"
dsh_runtime_contract_check = ROOT / "scripts/check-dsh-runtime-contract.py"
if not all(path.is_file() for path in (
        plugin_package, plugin_patch, plugin_index, plugin_acp,
        plugin_acp_codec, plugin_acp_content, plugin_profile_test,
        plugin_bin_test, plugin_bundle_test, plugin_bridge_test,
        plugin_approval_test, plugin_load_test, plugin_vitest_config,
        plugin_runtime_contract, plugin_build, plugin_typecheck,
        dsh_release_check, dsh_package_script, dsh_runtime_contract_check)):
    errors.append(
        "[R4] dsh_acp/plugin: myagents 必须拥有标准 bundle、built main、"
        "cordis.patch.yml 与完整 ACP contract source/tests")
else:
    package = json.loads(plugin_package.read_text(encoding="utf-8"))
    patch_text = plugin_patch.read_text(encoding="utf-8")
    plugin_text = plugin_index.read_text(encoding="utf-8")
    plugin_acp_text = plugin_acp.read_text(encoding="utf-8")
    plugin_codec_text = plugin_acp_codec.read_text(encoding="utf-8")
    plugin_content_text = plugin_acp_content.read_text(encoding="utf-8")
    plugin_build_text = plugin_build.read_text(encoding="utf-8")
    runtime_contract = json.loads(
        plugin_runtime_contract.read_text(encoding="utf-8"))
    package_exports = package.get("exports")
    package_files = package.get("files")
    package_dsh = package.get("dsh")
    if (package.get("name") != "@myagents/dsh-acp-host"
            or package.get("version") != "0.1.0"
            or package.get("main") != "lib/index.js"
            or not isinstance(package_exports, dict)
            or package_exports.get(".") != {"default": "./lib/index.js"}
            or package_exports.get("./cordis.patch.yml")
            != "./cordis.patch.yml"
            or not isinstance(package_files, list)
            or not {"lib/index.js", "cordis.patch.yml"} <= set(package_files)
            or any(str(item).startswith("src/") or str(item).endswith(".ts")
                   for item in package_files)
            or package_dsh != {
                "bundle": {"patch": "./cordis.patch.yml"}}
            or "bin" in package):
        errors.append(
            "[R4] dsh_acp/plugin/package.json: 标准 bundle 必须固定 myagents "
            "name/version、main/exports=lib/index.js、dsh.bundle.patch，且不得发布"
            "source executable")

    expected_peer_names = {
        "@deepseek-ai/cordis", "@deepseek-ai/schemastery",
        "@deepseek-ai/dsh-agent", "@deepseek-ai/dsh-attachment",
        "@deepseek-ai/dsh-llm", "@deepseek-ai/dsh-sandbox-policy",
        "@deepseek-ai/dsh-session", "@deepseek-ai/dsh-tools",
        "@deepseek-ai/dsh-user-approval",
    }
    public_packages = runtime_contract.get("publicPackages")
    expected_plugin_peers = {
        name: details.get("version")
        for name in expected_peer_names
        if isinstance(public_packages, dict)
        and isinstance((details := public_packages.get(name)), dict)
    }
    public_package_contracts_valid = (
        isinstance(public_packages, dict)
        and bool(public_packages)
        and all(
            isinstance(details, dict)
            and isinstance(details.get("entrySha256"), str)
            and isinstance(details.get("runtimeEntry"), str)
            and isinstance(details.get("runtimeEntrySha256"), str)
            and len(details["entrySha256"]) == 64
            and len(details["runtimeEntrySha256"]) == 64
            for details in public_packages.values()
        )
    )
    dsh_root_contract = runtime_contract.get("dshRoot")
    if (runtime_contract.get("schemaVersion") != 5
            or runtime_contract.get("hostVersion") != "0.1.0"
            or runtime_contract.get("compatibilityRevision") != 1
            or not isinstance(dsh_root_contract, dict)
            or dsh_root_contract.get("name")
            != "@deepseek-ai/dsh-root"
            or dsh_root_contract.get("version")
            != "0.1.1-rc.2"
            or dsh_root_contract.get("sourceCommit")
            != "b150a551b8d465e31e418e1b2eaf5e79bbb7d28e"
            or dsh_root_contract.get("cliEntry") != "apps/cli/lib/bin.js"
            or dsh_root_contract.get("cliEntrySha256")
            != "c0226687bb20f45c603ec6fe50f3de16d1c3510c3a803304ec575ef9bc366c62"
            or dsh_root_contract.get("cliRuntimeFiles") != {
                "bin.js": (
                    "c0226687bb20f45c603ec6fe50f3de16d1c3510c3a803304ec575ef9bc366c62"),
                "dump-config-D-jtgwY3.js": (
                    "f75ee5e1f3a7392103029f1b254188975c57c41ef6f959c887c2163bc7aaf47d"),
                "plugin-9h8shc4d.js": (
                    "6f4459da44f0e5bdb3c72471f4be0ee1929913be352baf8b0da7b700afc1804c"),
                "profile-boot-BnJoK_kl.js": (
                    "778c5b338674d986a49972be920c965d28b2c8cac85364ae77f8587070397663"),
                "profile-boot-DG5t9aNs.js": (
                    "f83ffea6a4d30cfbe02b41dabcc05104c4ad27bf79c74f601f0ddb6ccdf88969"),
            }
            or "runtimeFileCount" in dsh_root_contract
            or "runtimeTreeSha256" in dsh_root_contract
            or runtime_contract.get("acpSdk") != {
                "name": "@agentclientprotocol/sdk",
                "version": "0.25.1",
                "entrySha256": (
                    "a99ccb28840ca0338595e1f636cf41527f482bf4d45dded78fd15fdd61cd23d6"),
            }
            or runtime_contract.get("buildTool") != {
                "name": "esbuild",
                "version": "0.28.1",
                "entry": "lib/main.js",
                "entrySha256": (
                    "8331fe1d8b3a07381f33cc425fcfaa94776e263113653f80ec3ba433e9657e73"),
                "binary": "bin/esbuild",
                "binarySha256ByPlatform": {
                    "darwin-arm64": (
                        "e2dc9a52440a2a34f09434a2f4843cb1e30f84e40dcf238976ec61ef8cd7f36a"),
                },
            }
            or runtime_contract.get("profileBundle") != {
                "name": "@myagents/dsh-acp-host",
                "main": "lib/index.js",
                "patch": "cordis.patch.yml",
                "acpSdkBundled": True,
                "entrySha256": (
                    "a8515ac654705e07ea04c2f7c2f3df452500c9ad902652cb0d434af49aba3290"),
                "patchSha256": (
                    "744d7ce4370c0bac08db1b53e0da407005a2e053077648b85adbd69c996afbb2"),
            }
            or not public_package_contracts_valid
            or set(expected_plugin_peers) != expected_peer_names
            or package.get("peerDependencies") != expected_plugin_peers):
        errors.append(
            "[R4] dsh_acp/plugin: package peers、host/DSH/ACP SDK 版本与 "
            "checked-in standard bundle compatibility contract 必须精确一致")

    required_patch_tokens = {
        "id: session-persistence-jsonl", "compression: none",
        "packChunks: false", "id: sandbox-policy",
        "id: approval", "id: permission", "read-only:",
        "workspace-write:", "process.env.DSH_ACP_PROFILE",
        "id: myagents-dsh-acp-host",
        "name: '@myagents/dsh-acp-host'", "id: subagent",
        "id: tool-subagent", "id: web", "disabled: true",
    }
    if (not all(token in patch_text for token in required_patch_tokens)
            or any(token in patch_text for token in (
                "../src/", "src/bin", "config/cordis.yml"))):
        errors.append(
            "[R4] dsh_acp/plugin/cordis.patch.yml: stock base overlay 必须固定 "
            "uncompressed/unpacked JSONL、两 profile、递归工具禁用与唯一 built host")

    runtime_plugin_text = "\n".join((
        plugin_text, plugin_acp_text, plugin_codec_text, plugin_content_text,
    ))
    if ("@deepseek-ai/dsh-acp-demo" in runtime_plugin_text
            or "@deepseek-ai/dsh-acp/" in runtime_plugin_text
            or "AcpDemo" in runtime_plugin_text
            or re.search(
                r"@deepseek-ai/[^'\"\s]+/src(?:/|['\"])",
                runtime_plugin_text,
            )):
        errors.append(
            "[R4] dsh_acp/plugin: 产品 ACP server 不得委托 stock ACP demo、"
            "依赖 DSH 补丁或深层导入其未公开源码")

    required_plugin_index_tokens = {
        "ProductAcp", "installModelSelection", "profileSetup",
        "READ_ONLY_TOOLS", "agentCtx.tools.restrict",
        "agentCtx.tools.guard", "tools/pre-execute",
        "effectiveSandboxMode", "effectiveApprovalPolicy",
        "ctx.inject", "runtimeCtx.plugin(ProductAcp", "agentInfo:",
        "dsh-myagents-acp", "DSH_ACP_PERSISTENCE_DIR",
        "DSH_ACP_RUNTIME_HOME", "DSH_ACP_ATTACHMENT_HOME",
        "DSH_ACP_SETTINGS_FILE", "DSH_ACP_CREDENTIALS_FILE",
        "DSH_HOME", "DSH_AGENTS_HOME", "derivedStateDirectory",
        "rejectRuntimeInjection", "assertDisjoint", "process.cwd()",
        "fileURLToPath(import.meta.url)", "process.execPath",
        "deepseek.ai/dsh-runtime-version",
        "deepseek.ai/dsh-compatibility-revision",
    }
    if not all(token in plugin_text for token in required_plugin_index_tokens):
        errors.append(
            "[R4] dsh_acp/plugin/src/index.ts: 标准 Loader plugin 必须从 stock "
            "profile 注入 public services，并固定 state topology、工具闭集、"
            "execution policy attestation 与 wire identity")

    required_product_acp_tokens = {
        "AgentSideConnection", "loadSession", "closeSession",
        "requestPermission", "allow-once", "reject-once",
        "outcome.optionId === 'allow-once'",
        "outcome.optionId === 'reject-once'",
        "SessionPersistenceGuard", "persistence.list()",
        "persistence.locate(header)", "preflightDurableArtifact",
        "assertArtifactIdentity", "sameStatIdentity",
        "scanArtifactLines", "headerLineMatches",
        "MAX_DURABLE_REPLAY_BYTES", "MAX_DURABLE_REPLAY_EVENTS",
        "O_NOFOLLOW", "sessionCapabilities: { close: {} }",
        "loadSession: true",
    }
    if not all(token in plugin_acp_text
               for token in required_product_acp_tokens):
        errors.append(
            "[R4] dsh_acp/plugin/src/acp.ts: myagents-owned ACP server 必须拥有 "
            "load/close/permission、materialize 前 bounded JSONL preflight 与"
            "未知 option fail-closed 契约")
    if ("turnEndToStopReason" not in plugin_codec_text
            or "admitAcpPrompt" not in plugin_content_text):
        errors.append(
            "[R4] dsh_acp/plugin: terminal/content codec 必须归 myagents canonical source")

    required_build_tokens = {
        "MYAGENTS_DSH_SOURCE_ROOT", "MYAGENTS_DSH_ESBUILD_BIN",
        "verified esbuild binary must remain inside the stock DSH checkout",
        "--bundle", "--platform=node", "--format=esm", "--target=node24",
        "--external:@deepseek-ai/*", "@agentclientprotocol/sdk/dist/acp.js",
        "--alias:@agentclientprotocol/sdk=", "index.js",
    }
    if not all(token in plugin_build_text for token in required_build_tokens):
        errors.append(
            "[R4] dsh_acp/plugin/scripts/build.mjs: release build 必须生成唯一 "
            "lib/index.js，外置 DSH public peers 并固定打包 ACP SDK public entry")

    release_check_text = dsh_release_check.read_text(encoding="utf-8")
    required_release_invariant_tokens = {
        "MYAGENTS_DSH_SOURCE_ROOT", "apps/cli/lib/bin.js",
        'profile_home="$snapshot_dir/dsh-home"', 'DSH_HOME="$profile_home"',
        "plugin --profile myagents", 'add "$tarball" --offline',
        "MYAGENTS_DSH_TEST_HOME", "cordis.patch.yml", "lib/index.js",
        "pack --pack-destination", "tar -tzf", "--dump-config",
        "@deepseek-ai/dsh-base,@myagents/dsh-acp-host",
        "build-one", "build-two", "cmp -s", "--no-cache", "oxlint",
        "--deny-warnings", "--disable-nested-config", "head.before",
        "status.before", "tree.before", "head.after", "status.after",
        "tree.after", "git_root", "untracked-files=all",
        "os.lstat", "st_mtime_ns", "hashlib.sha256", "content_hash",
        '[[ -s "$snapshot_dir/status.before" ]]', "typecheck.mjs",
        "--no-optional-locks",
        "check-dsh-runtime-contract.py", "--source-root",
        "--bundle-entry", "--bundle-patch", "--contract",
    }
    if not all(token in release_check_text
               for token in required_release_invariant_tokens):
        errors.append(
            "[R4] scripts/check-dsh-plugin.sh: release contract 必须锁定所选 "
            "stock checkout，在临时 DSH_HOME 通过官方 plugin/profile/bundle "
            "路径，并证明测试前后 HEAD、Git 状态和完整文件树不变")
    release_preflight = release_check_text.find("--print-esbuild-bin")
    release_build = release_check_text.find("dsh_acp/plugin/scripts/build.mjs")
    release_postflight = release_check_text.find("--bundle-entry")
    if not 0 <= release_preflight < release_build < release_postflight:
        errors.append(
            "[R4] scripts/check-dsh-plugin.sh: source-only runtime contract "
            "preflight 必须早于任何 checkout build tool，bundle postflight 必须在构建后")
    if "/Users/" in release_check_text or "default_profile" in release_check_text:
        errors.append(
            "[R4] scripts/check-dsh-plugin.sh: release gate 只能使用临时 DSH_HOME，"
            "不得硬编码或探测用户默认 profile")
    package_script_text = dsh_package_script.read_text(encoding="utf-8")
    required_package_tokens = {
        "MYAGENTS_DSH_SOURCE_ROOT", "mktemp -d /tmp/myagents-dsh-package.",
        "dsh_acp/plugin/scripts/build.mjs", "pack --pack-destination",
        "myagents-dsh-acp-host-0.1.0.tgz", "拒绝覆盖已有 tarball",
        "lib/index.js", "runtime-contract.json", "THIRD_PARTY_NOTICES.md",
        "check-dsh-runtime-contract.py", "--source-root",
        "--bundle-entry", "--bundle-patch", "--contract",
    }
    if (not all(token in package_script_text for token in required_package_tokens)
            or "DSH_HOME" in package_script_text
            or "plugin --profile" in package_script_text
            or "/Users/" in package_script_text
            or "$HOME" in package_script_text):
        errors.append(
            "[R4] scripts/package-dsh-plugin.sh: 用户 setup 只能从 canonical "
            "source 生成不覆盖的标准 tarball，不得读取或修改 DSH profile")
    package_preflight = package_script_text.find("--print-esbuild-bin")
    package_build = package_script_text.find("dsh_acp/plugin/scripts/build.mjs")
    package_postflight = package_script_text.find("--bundle-entry")
    if not 0 <= package_preflight < package_build < package_postflight:
        errors.append(
            "[R4] scripts/package-dsh-plugin.sh: source-only runtime contract "
            "preflight 必须早于任何 checkout build tool，bundle postflight 必须在构建后")
    runtime_contract_check_text = dsh_runtime_contract_check.read_text(
        encoding="utf-8")
    required_runtime_contract_tokens = {
        '"rev-parse", "--show-toplevel"', '"rev-parse", "HEAD"',
        'dsh_root.get("sourceCommit")', 'source_root / "package.json"',
        'source_root / "node_modules" / "@agentclientprotocol" / "sdk"',
        'sdk_root / "dist/acp.js"', 'contract.get("publicPackages")',
        'package_root / "src/index.ts"', 'contract.get("profileBundle")',
        'dsh_root.get("cliEntrySha256")', 'expected.get("runtimeEntry")',
        'dsh_root.get("cliRuntimeFiles")', "_verify_cli_runtime_closure",
        "_RELATIVE_ESM_IMPORT_PATTERNS", "set(observed) != set(expected)",
        'expected.get("runtimeEntrySha256")', 'contract.get("buildTool")',
        'build_tool.get("binarySha256ByPlatform")',
        'arguments.print_esbuild_bin',
        'profile_bundle.get("entrySha256")',
        'profile_bundle.get("patchSha256")', "hashlib.sha256",
    }
    if not all(token in runtime_contract_check_text
               for token in required_runtime_contract_tokens):
        errors.append(
            "[R4] scripts/check-dsh-runtime-contract.py: release/package gate "
            "必须把实际 DSH commit、ACP/public entry 与 built bundle SHA-256 "
            "绑定到 checked-in runtime contract")
if ("session_close" not in (ROOT / "acp/client.py").read_text(encoding="utf-8")
        or "_close_active_session" not in acp_source):
    errors.append(
        "[R4] acp: 广告 sessionCapabilities.close 的 DSH session 必须在 "
        "profile 重建/关闭时有界收尾")
required_acp_terminal_and_prepare_tokens = {
    '_AUTH_NOTIFICATION_QUEUE_LIMIT = 64',
    'asyncio.Queue(maxsize=_AUTH_NOTIFICATION_QUEUE_LIMIT)',
    'except asyncio.QueueFull',
    'client.on_notification = None',
    'if stop_reason != "end_turn"',
    'raise AcpError(',
    'AgentDeliveryCancelledError',
    'cancel_sent = False',
    'cancel_sent = True',
    'cancel_sent or caller_cancelled is not None',
    'if stop_reason != "cancelled"',
}
if not all(token in acp_source
           for token in required_acp_terminal_and_prepare_tokens):
    errors.append(
        "[R4] acp/adapter.py: ACP prepare auth 通知必须有界、无 auth 的 load "
        "通知必须丢弃；只有 end_turn 可产生 done")

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
print("✓ R4 Kimi/OpenCode 受限 ACP-first + Qwen/CodeBuddy/DSH ACP-only + Pi attested RPC-only")
print("✓ R5 自然语言讨论、/discuss 与有序协作均有界且不递归 dispatch")
print("✓ R6 /workflow 固定阶段/单 writer/一次 repair/steering 有界")
PY

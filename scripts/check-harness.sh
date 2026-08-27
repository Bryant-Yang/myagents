#!/usr/bin/env bash
# myagents 项目级本地总门禁。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

required=(
  AGENTS.md
  HARNESS.md
  docs/SPEC.md
  docs/workflow.md
  docs/harness-controls.md
  docs/git-commit-conventions.md
  docs/adr/README.md
  docs/adr/0001-persistent-room-command-bus-mcp.md
  docs/adr/0002-durable-execution-observability.md
  docs/adr/0003-codex-app-server-transport.md
  docs/adr/0004-ephemeral-codex-host-threads.md
  docs/adr/0005-project-conversation-sessions.md
  docs/adr/0006-kimi-hybrid-transport-policy.md
  docs/adr/0007-opencode-hybrid-transport-policy.md
  docs/adr/0008-bounded-multi-agent-discussion.md
  docs/adr/0009-bounded-milestone-workflow-steering.md
  docs/adr/0010-multi-session-tui-management.md
  docs/adr/0011-session-scoped-natural-language-roles.md
  docs/adr/0012-agent-readiness-and-setup-ux.md
  docs/adr/0013-natural-language-sequential-collaboration.md
  docs/adr/0014-pi-rpc-permission-bridge.md
  docs/adr/0015-dsh-acp-only-transport.md
  docs/adr/0016-explicit-auto-approve-mode.md
  docs/adr/0017-native-model-backed-host.md
  scripts/check-redlines.sh
  scripts/check-dsh-runtime-contract.py
  scripts/package-dsh-plugin.sh
)

for path in "${required[@]}"; do
  if [ ! -f "$path" ]; then
    echo "✗ Harness 必需文件缺失：$path"
    exit 1
  fi
done

grep -q 'harness:controls-read-policy=on-demand' AGENTS.md
grep -q 'harness:control-map=minimum' docs/harness-controls.md
grep -q 'harness:behaviour-evidence=canonical-source' docs/SPEC.md
grep -q 'harness:behaviour-evidence=spec-reference-only' docs/harness-controls.md
grep -q 'harness:steering-trigger=min-occurrences-2' docs/harness-controls.md

python3 - <<'PY'
from __future__ import annotations

import pathlib
import re
import sys

root = pathlib.Path.cwd()
docs = [
    root / "AGENTS.md",
    root / "HARNESS.md",
    root / "docs/SPEC.md",
    root / "docs/workflow.md",
    root / "docs/harness-controls.md",
    root / "docs/git-commit-conventions.md",
    root / "docs/adr/README.md",
    root / "docs/adr/0001-persistent-room-command-bus-mcp.md",
    root / "docs/adr/0002-durable-execution-observability.md",
    root / "docs/adr/0003-codex-app-server-transport.md",
    root / "docs/adr/0004-ephemeral-codex-host-threads.md",
    root / "docs/adr/0005-project-conversation-sessions.md",
    root / "docs/adr/0006-kimi-hybrid-transport-policy.md",
    root / "docs/adr/0007-opencode-hybrid-transport-policy.md",
    root / "docs/adr/0008-bounded-multi-agent-discussion.md",
    root / "docs/adr/0009-bounded-milestone-workflow-steering.md",
    root / "docs/adr/0010-multi-session-tui-management.md",
    root / "docs/adr/0011-session-scoped-natural-language-roles.md",
    root / "docs/adr/0012-agent-readiness-and-setup-ux.md",
    root / "docs/adr/0013-natural-language-sequential-collaboration.md",
    root / "docs/adr/0014-pi-rpc-permission-bridge.md",
    root / "docs/adr/0015-dsh-acp-only-transport.md",
    root / "docs/adr/0016-explicit-auto-approve-mode.md",
    root / "docs/adr/0017-native-model-backed-host.md",
]
missing: list[str] = []
pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
for source in docs:
    text = source.read_text(encoding="utf-8")
    for target in pattern.findall(text):
        if target.startswith(("http://", "https://", "#")):
            continue
        path_text = target.split("#", 1)[0]
        if not path_text:
            continue
        target_path = (source.parent / path_text).resolve()
        if not target_path.exists():
            missing.append(
                f"{source.relative_to(root)} -> {target}")
if missing:
    print("✗ Harness 本地 Markdown 引用悬空：")
    for item in missing:
        print(f"  - {item}")
    sys.exit(1)
print("✓ Harness 文件、markers 与本地引用有效")
PY

bash scripts/check-redlines.sh

if [ ! -x .venv/bin/python ]; then
  echo "✗ 缺少 .venv/bin/python；先按 README.md 快速开始创建虚拟环境"
  exit 1
fi

.venv/bin/python -m py_compile \
  main.py myagents_mcp.py orchestrator.py host.py discussion.py collaboration.py workflow.py session_roles.py session_catalog.py session_manager.py tui_activity.py agent_readiness.py \
  acp/*.py pi_rpc/*.py codex_app_server/*.py native_agent/*.py adapters/*.py control/*.py storage/*.py workspace/*.py tests/*.py \
  scripts/e2e-m3-real.py scripts/e2e-m5-real.py
.venv/bin/python tests/test_agent_readiness.py
.venv/bin/python tests/test_basic.py
.venv/bin/python tests/test_session_roles.py
.venv/bin/python tests/test_tui_activity.py
.venv/bin/python tests/test_tui_completion.py
.venv/bin/python tests/test_native_agent.py
.venv/bin/python tests/test_host_backend.py
.venv/bin/python tests/test_discussion.py
.venv/bin/python tests/test_collaboration.py
.venv/bin/python tests/test_workflow.py
.venv/bin/python tests/test_tui_status.py
.venv/bin/python tests/test_clipboard_image.py
.venv/bin/python tests/test_session_catalog.py
.venv/bin/python tests/test_session_manager.py
.venv/bin/python tests/test_session_tui.py
.venv/bin/python tests/test_acp.py
.venv/bin/python tests/test_workbuddy_acp.py
.venv/bin/python tests/test_dsh_acp.py
.venv/bin/python tests/test_pi_rpc_client.py
.venv/bin/python tests/test_pi_adapter.py
.venv/bin/python tests/test_pi_permission_bridge.py
.venv/bin/python tests/test_kimi_hybrid.py
.venv/bin/python tests/test_opencode_hybrid.py
.venv/bin/python tests/test_phase2.py
.venv/bin/python tests/test_storage.py
.venv/bin/python tests/test_m25.py
.venv/bin/python tests/test_m3_bus.py
.venv/bin/python tests/test_m3_control.py
.venv/bin/python tests/test_m3_mcp.py
.venv/bin/python tests/test_codex_app_server.py

echo "✓ myagents Harness 全部通过"

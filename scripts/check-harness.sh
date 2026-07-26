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
  scripts/check-redlines.sh
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
  main.py orchestrator.py host.py \
  acp/*.py adapters/*.py tests/*.py
.venv/bin/python tests/test_basic.py
.venv/bin/python tests/test_acp.py
.venv/bin/python tests/test_phase2.py

echo "✓ myagents Harness 全部通过"

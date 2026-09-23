#!/usr/bin/env bash
# DSH 升级一条龙：npm 升级官方 dsh → 契约对齐 → bundle 重建 → profile 重装
# → 哈希对齐 → readiness + 真实 smoke。任何一步失败即停，已完成的写入
# 保持原子（refresh --accept 自带原子性）。
#
# 用法：
#   bash scripts/dsh-upgrade.sh                 # 升级到 @latest 并全链验证
#   bash scripts/dsh-upgrade.sh 0.1.7-rc.1      # 升级到指定版本
#   bash scripts/dsh-upgrade.sh --no-npm        # dsh 已手动升级，只做后续对齐
#
# 退出非 0 且输出含 "plugin tree failed to load"/"校验失败" 等，通常是上游
# API 破坏性变更：需要人工适配 dsh_acp/plugin/src 后重跑（--no-npm）。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DSH_SPEC="${1:-@latest}"
RUN_NPM=1
if [[ "$DSH_SPEC" == "--no-npm" ]]; then
  RUN_NPM=0
  DSH_SPEC="@latest"
fi

ESBUILD_BIN="$(command -v esbuild || true)"
if [[ -z "$ESBUILD_BIN" ]]; then
  echo "✗ 缺少 esbuild：npm install -g esbuild" >&2
  exit 2
fi

step() { echo; echo "== $1 =="; }

# 1) npm 升级官方 dsh
if [[ "$RUN_NPM" == "1" ]]; then
  step "1/6 npm install -g @deepseek-ai/dsh@$DSH_SPEC"
  npm install -g "@deepseek-ai/dsh@$DSH_SPEC" 2>&1 | grep -v "allow-scripts" || true
fi
dsh --version

# 2) 解析官方入口（lib/bin.js 绝对路径）
DSH_BIN="$(command -v dsh)"
DSH_REAL="$(readlink -f "$DSH_BIN")"
export MYAGENTS_DSH_CLI="$DSH_REAL"
export MYAGENTS_DSH_ESBUILD_BIN="$ESBUILD_BIN"
echo "官方入口: $MYAGENTS_DSH_CLI"

# 3) 契约对齐版本（必须先于构建：define 是构建时烧入的）
step "2/6 契约对齐版本"
.venv/bin/python scripts/dsh-contract-refresh.py --cli "$MYAGENTS_DSH_CLI" --accept

# 4) 重建 bundle + 重装 profile
step "3/6 重建 plugin bundle"
node dsh_acp/plugin/scripts/build.mjs --outdir /tmp/myagents-dsh-upgrade

step "4/6 重装 profile bundle"
cp /tmp/myagents-dsh-upgrade/index.js dsh_acp/plugin/lib/index.js
rm -f /tmp/myagents-dsh-acp-host-*.tgz
( cd dsh_acp/plugin && npm pack --pack-destination /tmp >/dev/null )
TGZ="$(ls -1t /tmp/myagents-dsh-acp-host-*.tgz | head -1)"
dsh plugin --profile myagents remove @myagents/dsh-acp-host >/dev/null
dsh plugin --profile myagents add "$TGZ"

# 5) 契约对齐产物哈希
step "5/6 契约对齐 entrySha256"
.venv/bin/python scripts/dsh-contract-refresh.py \
  --cli "$MYAGENTS_DSH_CLI" \
  --bundle-entry /tmp/myagents-dsh-upgrade/index.js --accept

# 6) readiness + 真实 smoke
step "6/6 readiness + 真实 smoke"
.venv/bin/python - <<'PYEOF'
import asyncio
import sys
sys.path.insert(0, ".")

from dsh_acp.adapter import AcpDshAdapter, dsh_readiness_probe

probe = dsh_readiness_probe()
if probe.state.value != "ready":
    raise SystemExit(f"readiness 未就绪：{probe.detail}")

async def main():
    adapter = AcpDshAdapter(permission="deny")
    if adapter._configuration_error:
        raise SystemExit(f"配置错误：{adapter._configuration_error}")
    chunks = []
    try:
        async for event in adapter.stream("只回复两个字:好的", "/tmp"):
            if event.kind == "text":
                chunks.append(event.text)
            elif event.kind == "error":
                chunks.append(f"[error] {event.text}")
            elif event.kind == "done":
                chunks.append("[done]")
    finally:
        await adapter.aclose()
    reply = "".join(chunks)
    if "[done]" not in reply:
        raise SystemExit(f"smoke 未到 done 终态：{reply[:300]}")
    print(f"smoke 回复: {reply[:200]}")

asyncio.run(main())
PYEOF

echo
echo "✓ DSH 升级完成（$(dsh --version)），全链验证通过。"
echo "  建议复跑 .venv/bin/python tests/test_dsh_acp.py 与"
echo "  bash scripts/check-redlines.sh 后提交契约变更。"

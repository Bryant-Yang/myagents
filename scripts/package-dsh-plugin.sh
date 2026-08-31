#!/usr/bin/env bash
# Build the canonical myagents DSH bundle into an installable tarball without
# modifying the stock DSH checkout or any user profile.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
source_input="${MYAGENTS_DSH_SOURCE_ROOT:-}"
output_input="${1:-}"

if [[ -z "$source_input" || "$source_input" != /* ]]; then
  echo "MYAGENTS_DSH_SOURCE_ROOT 必须指向绝对的 stock DSH checkout" >&2
  exit 2
fi
if [[ -z "$output_input" || "$output_input" != /* ]]; then
  echo "用法：scripts/package-dsh-plugin.sh /absolute/output-directory" >&2
  exit 2
fi
if [[ ! -d "$output_input" || -L "$output_input" ]]; then
  echo "输出目录必须是已存在的非 symlink 目录：$output_input" >&2
  exit 2
fi

source_root="$(cd "$source_input" && pwd -P)"
output_dir="$(cd "$output_input" && pwd -P)"
pnpm_bin="$(command -v pnpm || true)"
if [[ -z "$pnpm_bin" ]]; then
  echo "打包 DSH bundle 需要 pnpm" >&2
  exit 2
fi
if [[ ! -x "$repo_root/.venv/bin/python" ]]; then
  echo "缺少 myagents .venv/bin/python，无法校验 DSH runtime contract" >&2
  exit 2
fi
runtime_contract="$repo_root/dsh_acp/plugin/runtime-contract.json"
esbuild_bin="$("$repo_root/.venv/bin/python" \
  "$repo_root/scripts/check-dsh-runtime-contract.py" \
  --source-root "$source_root" \
  --contract "$runtime_contract" \
  --print-esbuild-bin)"

tarball="$output_dir/myagents-dsh-acp-host-0.1.1.tgz"
if [[ -e "$tarball" ]]; then
  echo "拒绝覆盖已有 tarball：$tarball" >&2
  exit 2
fi

package_root="$(mktemp -d /tmp/myagents-dsh-package.XXXXXX)"
cleanup() {
  python3 -c 'import shutil, sys
path = sys.argv[1]
if not path.startswith("/tmp/myagents-dsh-package."):
    raise SystemExit("unsafe cleanup path")
shutil.rmtree(path, ignore_errors=True)' "$package_root"
}
trap cleanup EXIT

stage="$package_root/package"
mkdir -p "$stage/lib"
MYAGENTS_DSH_SOURCE_ROOT="$source_root" \
  MYAGENTS_DSH_ESBUILD_BIN="$esbuild_bin" \
  node "$repo_root/dsh_acp/plugin/scripts/build.mjs" --outdir "$stage/lib"
if [[ ! -f "$stage/lib/index.js" ]]; then
  echo "DSH bundle 构建失败：未生成 lib/index.js" >&2
  exit 1
fi
cp "$repo_root/dsh_acp/plugin/package.json" \
  "$repo_root/dsh_acp/plugin/cordis.patch.yml" \
  "$repo_root/dsh_acp/plugin/runtime-contract.json" \
  "$repo_root/dsh_acp/plugin/README.md" \
  "$repo_root/dsh_acp/plugin/THIRD_PARTY_NOTICES.md" \
  "$stage/"

"$repo_root/.venv/bin/python" \
  "$repo_root/scripts/check-dsh-runtime-contract.py" \
  --bundle-entry "$stage/lib/index.js" \
  --bundle-patch "$stage/cordis.patch.yml" \
  --contract "$stage/runtime-contract.json"

"$pnpm_bin" --dir "$stage" pack --pack-destination "$output_dir" >/dev/null
if [[ ! -f "$tarball" ]]; then
  echo "DSH bundle 打包失败：未生成 $tarball" >&2
  exit 1
fi
printf '%s\n' "$tarball"

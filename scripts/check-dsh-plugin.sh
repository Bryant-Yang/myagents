#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
source_input="${MYAGENTS_DSH_SOURCE_ROOT:-}"
if [[ -z "$source_input" || "$source_input" != /* ]]; then
  echo "MYAGENTS_DSH_SOURCE_ROOT 必须指向绝对的 stock DSH checkout" >&2
  exit 2
fi
if [[ ! -d "$source_input" ]]; then
  echo "DSH checkout 不存在：$source_input" >&2
  exit 2
fi

source_root="$(cd "$source_input" && pwd -P)"
git_root="$(git -C "$source_root" rev-parse --show-toplevel)"
git_root="$(cd "$git_root" && pwd -P)"
if [[ "$source_root" != "$git_root" ]]; then
  echo "MYAGENTS_DSH_SOURCE_ROOT 必须是 DSH Git 根目录：$git_root" >&2
  exit 2
fi

vitest="$source_root/node_modules/.bin/vitest"
if [[ ! -x "$vitest" ]]; then
  echo "DSH checkout 缺少已安装的可执行 Vitest：$vitest" >&2
  exit 2
fi
oxlint="$source_root/node_modules/.bin/oxlint"
if [[ ! -x "$oxlint" ]]; then
  echo "DSH checkout 缺少已安装的可执行 Oxlint：$oxlint" >&2
  exit 2
fi
dsh_bin="$source_root/apps/cli/lib/bin.js"
if [[ ! -f "$dsh_bin" ]]; then
  echo "DSH checkout 缺少已构建的官方 CLI：$dsh_bin" >&2
  exit 2
fi
pnpm_bin="$(command -v pnpm || true)"
if [[ -z "$pnpm_bin" ]]; then
  echo "release gate 需要 pnpm" >&2
  exit 2
fi
if [[ ! -x "$repo_root/.venv/bin/python" ]]; then
  echo "release gate 缺少 myagents .venv/bin/python" >&2
  exit 2
fi
runtime_contract="$repo_root/dsh_acp/plugin/runtime-contract.json"
esbuild_bin="$("$repo_root/.venv/bin/python" \
  "$repo_root/scripts/check-dsh-runtime-contract.py" \
  --source-root "$source_root" \
  --contract "$runtime_contract" \
  --print-esbuild-bin)"

snapshot_dir="$(mktemp -d /tmp/myagents-dsh-plugin-check.XXXXXX)"
cleanup() {
  if [[ "$snapshot_dir" == /tmp/myagents-dsh-plugin-check.* \
      && -d "$snapshot_dir" ]]; then
    rm -rf -- "$snapshot_dir"
  fi
}
trap cleanup EXIT

snapshot_tree() {
  local output="$1"
  local tree_root="${2:-$source_root}"
  "$repo_root/.venv/bin/python" - "$tree_root" "$output" <<'PY'
import json
import hashlib
import os
import stat
import sys

root, output = sys.argv[1:]
if not os.path.lexists(root):
    with open(output, "w", encoding="utf-8") as stream:
        stream.write('["absent"]\n')
    raise SystemExit(0)
root_info = os.lstat(root)
if stat.S_ISLNK(root_info.st_mode):
    root_kind = "symlink"
    root_link = os.readlink(root)
elif stat.S_ISDIR(root_info.st_mode):
    root_kind = "directory"
    root_link = None
elif stat.S_ISREG(root_info.st_mode):
    root_kind = "file"
    root_link = None
else:
    root_kind = "other"
    root_link = None
rows = [[
    ".",
    root_kind,
    stat.S_IMODE(root_info.st_mode),
    root_info.st_uid,
    root_info.st_gid,
    root_info.st_ino,
    root_info.st_size,
    root_info.st_mtime_ns,
    root_link,
    None,
]]
walk = os.walk(root, topdown=True, followlinks=False) if root_kind == "directory" else []
for current, directories, files in walk:
    directories[:] = sorted(name for name in directories if name != ".git")
    for name in [*directories, *sorted(files)]:
        target = os.path.join(current, name)
        info = os.lstat(target)
        relative = os.path.relpath(target, root)
        if stat.S_ISLNK(info.st_mode):
            kind = "symlink"
            link = os.readlink(target)
        elif stat.S_ISDIR(info.st_mode):
            kind = "directory"
            link = None
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
            link = None
            digest = hashlib.sha256()
            with open(target, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            content_hash = digest.hexdigest()
        else:
            kind = "other"
            link = None
            content_hash = None
        if kind != "file":
            content_hash = None
        rows.append([
            relative,
            kind,
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            link,
            content_hash,
        ])
with open(output, "w", encoding="utf-8") as stream:
    for row in rows:
        stream.write(json.dumps(
            row, ensure_ascii=False, separators=(",", ":")) + "\n")
PY
}

assert_empty_user_patch() {
  local patch_path="$1"
  local label="$2"
  local required="$3"
  "$repo_root/.venv/bin/python" - "$patch_path" "$label" "$required" <<'PY'
import os
import stat
import sys

path, label, required = sys.argv[1:]
if not os.path.lexists(path):
    if required == "1":
        raise SystemExit(f"{label} 缺失：{path}")
    raise SystemExit(0)
metadata = os.lstat(path)
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
    raise SystemExit(f"{label} 必须是非 symlink 普通文件：{path}")
with open(path, "r", encoding="utf-8") as stream:
    semantic = [
        line.strip() for line in stream
        if line.strip() and not line.lstrip().startswith("#")
    ]
if semantic != ["[]"]:
    raise SystemExit(f"{label} 必须严格是 YAML 空数组层 []：{semantic!r}")
PY
}

assert_readiness() {
  local expected="$1"
  MYAGENTS_DSH_SOURCE_ROOT="$source_root" DSH_HOME="$profile_home" \
    "$repo_root/.venv/bin/python" - "$expected" <<'PY'
import sys

from dsh_acp.adapter import ReadinessState, dsh_readiness_probe

expected = ReadinessState[sys.argv[1]]
result = dsh_readiness_probe()
if result.state is not expected:
    raise SystemExit(
        f"DSH readiness expected {expected.value}, got "
        f"{result.state.value}: {result.detail}"
    )
PY
}

git --no-optional-locks -C "$source_root" rev-parse HEAD >"$snapshot_dir/head.before"
git --no-optional-locks -C "$source_root" status --porcelain=v1 -z --untracked-files=all \
  >"$snapshot_dir/status.before"
if [[ -s "$snapshot_dir/status.before" ]]; then
  echo "DSH invariant 失败：release gate 要求初始 checkout 完全干净" >&2
  git --no-optional-locks -C "$source_root" status --short --untracked-files=all >&2
  exit 1
fi
snapshot_tree "$snapshot_dir/tree.before"

stage="$snapshot_dir/package"
build_one="$snapshot_dir/build-one"
build_two="$snapshot_dir/build-two"
profile_home="$snapshot_dir/dsh-home"
mkdir -p "$stage/lib" "$build_one" "$build_two" "$profile_home"
profile_home="$(cd "$profile_home" && pwd -P)"

MYAGENTS_DSH_SOURCE_ROOT="$source_root" \
  MYAGENTS_DSH_ESBUILD_BIN="$esbuild_bin" \
  node "$repo_root/dsh_acp/plugin/scripts/build.mjs" --outdir "$build_one"
MYAGENTS_DSH_SOURCE_ROOT="$source_root" \
  MYAGENTS_DSH_ESBUILD_BIN="$esbuild_bin" \
  node "$repo_root/dsh_acp/plugin/scripts/build.mjs" --outdir "$build_two"
if ! cmp -s "$build_one/index.js" "$build_two/index.js"; then
  echo "bundle build 失败：相同源码两次构建不一致" >&2
  exit 1
fi
"$repo_root/.venv/bin/python" \
  "$repo_root/scripts/check-dsh-runtime-contract.py" \
  --bundle-entry "$build_one/index.js" \
  --bundle-patch "$repo_root/dsh_acp/plugin/cordis.patch.yml" \
  --contract "$runtime_contract"

cp "$repo_root/dsh_acp/plugin/package.json" \
  "$repo_root/dsh_acp/plugin/cordis.patch.yml" \
  "$repo_root/dsh_acp/plugin/runtime-contract.json" \
  "$repo_root/dsh_acp/plugin/README.md" \
  "$repo_root/dsh_acp/plugin/THIRD_PARTY_NOTICES.md" \
  "$stage/"
cp "$build_one/index.js" "$stage/lib/index.js"
"$pnpm_bin" --dir "$stage" pack --pack-destination "$snapshot_dir"
tarball="$(find "$snapshot_dir" -maxdepth 1 -name '*.tgz' -print -quit)"
if [[ -z "$tarball" || ! -f "$tarball" ]]; then
  echo "bundle pack 失败：没有生成 tarball" >&2
  exit 1
fi
if tar -tzf "$tarball" | grep -Eq '(^|/)src/|\.tsx?$|src/bin'; then
  echo "bundle pack 失败：安装包仍包含 TypeScript/source executable" >&2
  exit 1
fi

DSH_HOME="$profile_home" node "$dsh_bin" plugin --profile myagents \
  add "$tarball" --offline
profile_patch="$profile_home/profiles/myagents/cordis.patch.yml"
home_patch="$profile_home/cordis.patch.yml"
assert_empty_user_patch "$profile_patch" "DSH myagents profile user patch" 1
assert_empty_user_patch "$home_patch" "DSH home-level user patch" 0
assert_readiness READY

cp "$profile_patch" "$snapshot_dir/profile.patch.empty"
printf '%s\n' '- id: hmr' '  disabled: false' >"$profile_patch"
assert_readiness INVALID
cp "$snapshot_dir/profile.patch.empty" "$profile_patch"
assert_empty_user_patch "$profile_patch" "restored DSH profile user patch" 1
assert_readiness READY

if [[ -e "$home_patch" ]]; then
  cp "$home_patch" "$snapshot_dir/home.patch.empty"
  home_patch_was_present=1
else
  home_patch_was_present=0
fi
printf '%s\n' '- id: myagents-dsh-acp-host' '  disabled: true' >"$home_patch"
assert_readiness INVALID
if (( home_patch_was_present == 1 )); then
  cp "$snapshot_dir/home.patch.empty" "$home_patch"
else
  mv "$home_patch" "$snapshot_dir/home.patch.non-empty"
fi
assert_empty_user_patch "$home_patch" "restored DSH home-level user patch" 0
assert_readiness READY

DSH_HOME="$profile_home" node "$dsh_bin" --profile myagents --dump-config \
  >"$snapshot_dir/dump.yml"
if ! grep -q "name: '@myagents/dsh-acp-host'" "$snapshot_dir/dump.yml" \
    || ! grep -q 'id: myagents-dsh-acp-host' "$snapshot_dir/dump.yml"; then
  echo "official dump-config 未组合 myagents bundle" >&2
  exit 1
fi
if grep -Eq 'src/bin|\.tsx?|\.\./src/' "$snapshot_dir/dump.yml"; then
  echo "official dump-config 泄漏了 source/tsx 产品入口" >&2
  exit 1
fi
node - "$profile_home/profiles/myagents/package.json" <<'JS'
const manifest = require(process.argv[2])
const bundles = manifest?.dsh?.profile?.bundles
if (!Array.isArray(bundles)
    || bundles.join(',') !== '@deepseek-ai/dsh-base,@myagents/dsh-acp-host') {
  throw new Error(`unexpected profile bundle order: ${JSON.stringify(bundles)}`)
}
JS

set +e
"$oxlint" --deny-warnings --disable-nested-config \
  "$repo_root/dsh_acp/plugin/src" \
  "$repo_root/dsh_acp/plugin/tests" \
  "$repo_root/dsh_acp/plugin/scripts"
lint_status=$?
node "$repo_root/dsh_acp/plugin/scripts/typecheck.mjs" "$source_root"
typecheck_status=$?
MYAGENTS_DSH_SOURCE_ROOT="$source_root" \
  MYAGENTS_DSH_TEST_HOME="$profile_home" \
  "$vitest" run --no-cache --config \
  "$repo_root/dsh_acp/plugin/vitest.config.mjs"
test_status=$?
set -e

git --no-optional-locks -C "$source_root" rev-parse HEAD >"$snapshot_dir/head.after"
git --no-optional-locks -C "$source_root" status --porcelain=v1 -z --untracked-files=all \
  >"$snapshot_dir/status.after"
snapshot_tree "$snapshot_dir/tree.after"

invariant_status=0
if ! cmp -s "$snapshot_dir/head.before" "$snapshot_dir/head.after"; then
  echo "DSH invariant 失败：测试改变了 checkout HEAD" >&2
  invariant_status=1
fi
if ! cmp -s "$snapshot_dir/status.before" "$snapshot_dir/status.after"; then
  echo "DSH invariant 失败：测试改变了 tracked/untracked Git 状态" >&2
  git --no-optional-locks -C "$source_root" status --short --untracked-files=all >&2
  invariant_status=1
fi
if ! cmp -s "$snapshot_dir/tree.before" "$snapshot_dir/tree.after"; then
  echo "DSH invariant 失败：测试改变了 checkout 文件树（包括 ignored 文件）" >&2
  diff --unified=0 "$snapshot_dir/tree.before" "$snapshot_dir/tree.after" \
    | sed -n '1,120p' >&2 || true
  invariant_status=1
fi
if (( invariant_status != 0 )); then
  exit "$invariant_status"
fi
if (( lint_status != 0 )); then
  exit "$lint_status"
fi
if (( typecheck_status != 0 )); then
  exit "$typecheck_status"
fi
if (( test_status != 0 )); then
  exit "$test_status"
fi
echo "✓ DSH official bundle add/dump/ACP contract 通过；临时 profile 隔离且 stock checkout 未改变"

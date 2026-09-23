#!/usr/bin/env python3
"""从官方 DSH checkout 重新生成 runtime-contract.json（DSH 版本身份的唯一事实源）。

升级 DSH 的完整流程：
    MYAGENTS_DSH_SOURCE_ROOT=/path/to/deepseek-harness \\
        python scripts/dsh-contract-refresh.py            # dry-run，仅打印 diff
    ... 确认无误后 ...
    MYAGENTS_DSH_SOURCE_ROOT=/path/to/deepseek-harness \\
        python scripts/dsh-contract-refresh.py --accept   # 落盘并自证

脚本只被动读取 checkout、git 元数据与文件哈希；不执行 pnpm/build/CLI。
生成后立即用 check-dsh-runtime-contract.py 的同一套算法自证，确保
refresh 与验收 gate 永远算的是同一套东西。
"""

from __future__ import annotations

import argparse
import copy
import difflib
import importlib.util
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHECK_SCRIPT = _REPO_ROOT / "scripts" / "check-dsh-runtime-contract.py"
_DEFAULT_CONTRACT = _REPO_ROOT / "dsh_acp" / "plugin" / "runtime-contract.json"
_DEFAULT_PLUGIN_PACKAGE = _REPO_ROOT / "dsh_acp" / "plugin" / "package.json"
_DEFAULT_BUNDLE_ENTRY = _REPO_ROOT / "dsh_acp" / "plugin" / "lib" / "index.js"
_ESBUILD_ENTRY = "lib/main.js"
_ESBUILD_BINARY = "bin/esbuild"


def _load_check_script():
    spec = importlib.util.spec_from_file_location(
        "check_dsh_runtime_contract", _CHECK_SCRIPT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载验收脚本：{_CHECK_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load_check_script()


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"DSH contract refresh 失败：{message}")


def _sha(path: Path, label: str) -> str:
    return check._sha256(path, label)


def _cli_runtime_closure(source_root: Path, entry: Path) -> dict[str, str]:
    """与 check 的 _verify_cli_runtime_closure 同一遍 import 闭包，改为收集。"""
    cli_root = (source_root / "apps/cli/lib").resolve(strict=True)
    queue = [entry.resolve(strict=True)]
    observed: dict[str, str] = {}
    while queue:
        current = queue.pop(0)
        if not current.is_relative_to(cli_root):
            _fail(f"DSH CLI relative import 越出 lib root：{current}")
        relative = current.relative_to(cli_root).as_posix()
        if relative in observed:
            continue
        observed[relative] = _sha(current, f"DSH CLI runtime file {relative}")
        try:
            payload = current.read_bytes()
        except OSError as exc:
            _fail(f"DSH CLI runtime file 无法读取：{current}（{exc}）")
        imports: set[str] = set()
        for pattern in check._RELATIVE_ESM_IMPORT_PATTERNS:
            imports.update(
                match.decode("utf-8") for match in pattern.findall(payload))
        for specifier in sorted(imports):
            if "?" in specifier or "#" in specifier:
                _fail(f"DSH CLI relative import 含未支持后缀：{specifier!r}")
            candidate = current.parent / specifier
            if not candidate.suffix:
                candidate = candidate.with_suffix(".js")
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                _fail(f"DSH CLI relative import 缺失：{specifier!r}（{exc}）")
            if not resolved.is_relative_to(cli_root):
                _fail(f"DSH CLI relative import 越界：{specifier!r}")
            queue.append(resolved)
    return observed


def _resolve_installed_cli(raw: str) -> Path:
    """npm 安装模式的官方 dsh 入口（lib/bin.js）；只被动读取。"""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        _fail(f"--cli 必须是绝对路径：{candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        _fail(f"dsh 入口不存在：{candidate}（{exc}）")
    if not resolved.is_file():
        _fail(f"dsh 入口不是普通文件：{resolved}")
    package_root = resolved.parent.parent
    manifest = check._json_object(
        package_root / "package.json", "installed dsh package.json")
    if manifest.get("name") != "@deepseek-ai/dsh":
        _fail(
            "入口所属 package 不是官方 @deepseek-ai/dsh："
            f"{manifest.get('name')!r}（{package_root}）")
    return resolved


def _resolve_source_root(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        _fail(f"source root 必须是绝对路径：{candidate}")
    try:
        canonical = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        _fail(f"DSH source root 不存在：{candidate}（{exc}）")
    if not canonical.is_dir():
        _fail(f"DSH source root 不是目录：{canonical}")
    git_root = Path(check._git_value(canonical, "rev-parse", "--show-toplevel"))
    if git_root.resolve(strict=True) != canonical:
        _fail(f"DSH source root 不是 Git 根目录：{git_root}")
    return canonical


def _discover_esbuild(
    source_root: Path,
    explicit_version: str | None,
) -> tuple[str, Path]:
    pnpm_root = source_root / "node_modules/.pnpm"
    if not pnpm_root.is_dir():
        _fail(f"DSH checkout 缺少 pnpm store：{pnpm_root}")
    found: dict[str, Path] = {}
    for manifest_path in pnpm_root.glob(
            "esbuild@*/node_modules/esbuild/package.json"):
        manifest = check._json_object(manifest_path, "esbuild manifest")
        name = manifest.get("name")
        version = manifest.get("version")
        if name == "esbuild" and isinstance(version, str):
            found[version] = manifest_path.parent
    if explicit_version is not None:
        if explicit_version not in found:
            _fail(
                "--esbuild-version 在 checkout 中不存在："
                f"{explicit_version!r}，实际 {sorted(found)!r}")
        return explicit_version, found[explicit_version]
    if len(found) == 1:
        version, package_root = next(iter(found.items()))
        return version, package_root
    _fail(
        "checkout 中存在多个 esbuild 版本，无法自动选择："
        f"{sorted(found)!r}；请用 --esbuild-version 显式指定构建实际使用的版本")


def _public_packages(source_root: Path) -> dict[str, dict]:
    observed = check._source_packages(source_root)
    result: dict[str, dict] = {}
    for name in sorted(observed):
        package_root, manifest = observed[name]
        runtime_entry = check._runtime_entry(manifest, name)
        result[name] = {
            "version": manifest.get("version"),
            "entrySha256": _sha(
                package_root / "src/index.ts", f"{name} public source entry"),
            "runtimeEntry": runtime_entry,
            "runtimeEntrySha256": _sha(
                package_root / runtime_entry, f"{name} public runtime entry"),
        }
    return result


def _platform_key() -> str:
    machine = platform.machine().lower()
    machine = {"aarch64": "arm64", "x86_64": "x64"}.get(machine, machine)
    system = {"darwin": "darwin", "linux": "linux"}.get(sys.platform)
    if system is None:
        _fail(f"当前平台未纳入 DSH build-tool contract：{sys.platform}")
    return f"{system}-{machine}"


def _old_dict(parent: dict, key: str) -> dict:
    value = parent.get(key)
    return value if isinstance(value, dict) else {}


def _build_contract_installed(
    old: dict, cli_entry: Path, bundle_entry: Path | None) -> dict:
    """npm 安装模式：对齐 dshRoot.version 与 cliEntrySha256。

    可选 --bundle-entry 同时更新 profileBundle.entrySha256（重新构建
    plugin bundle 后）。源码绑定字段（sourceCommit/cliRuntimeFiles/
    publicPackages/acpSdk/buildTool）保留原值——installed 模式不消费
    它们，但它们已代表旧 checkout；下次拿源码树时必须用 --source-root
    重新对齐后才能跑源码 release gate。
    """
    package_root = cli_entry.parent.parent
    manifest = check._json_object(
        package_root / "package.json", "installed dsh package.json")
    new = copy.deepcopy(old)
    new["dshRoot"]["version"] = manifest.get("version")
    new["dshRoot"]["cliEntrySha256"] = _sha(
        cli_entry, "installed DSH CLI entry")
    if bundle_entry is not None:
        new["profileBundle"]["entrySha256"] = _sha(
            bundle_entry, "rebuilt myagents DSH bundle entry")
    return new


def _build_contract(
    source_root: Path,
    old: dict,
    *,
    bundle_entry: Path | None,
    esbuild_version: str | None,
) -> dict:
    root_manifest = check._json_object(
        source_root / "package.json", "DSH root manifest")
    if root_manifest.get("name") != "@deepseek-ai/dsh-root":
        _fail(
            "DSH root package name 不符合预期："
            f"{root_manifest.get('name')!r}")
    cli_manifest = check._json_object(
        source_root / "apps/cli/package.json", "DSH source CLI manifest")
    cli_entry = source_root / "apps/cli/lib/bin.js"
    if not cli_entry.is_file():
        _fail(f"DSH checkout 缺少已构建官方 CLI：{cli_entry}")

    dsh_root: dict = {
        "name": root_manifest.get("name"),
        "version": root_manifest.get("version"),
        "sourceCommit": check._git_value(source_root, "rev-parse", "HEAD"),
        "cliEntry": "apps/cli/lib/bin.js",
        "cliEntrySha256": _sha(cli_entry, "DSH official built CLI entry"),
        "cliRuntimeFiles": _cli_runtime_closure(source_root, cli_entry),
    }
    if cli_manifest.get("name") != "@deepseek-ai/dsh" or (
            cli_manifest.get("version") != dsh_root["version"]):
        _fail("DSH CLI manifest 与 root manifest 版本不一致")

    sdk_root = (
        source_root / "apps" / "cli" / "node_modules" /
        "@agentclientprotocol" / "sdk"
    ).resolve(strict=True)
    sdk_manifest = check._json_object(sdk_root / "package.json", "ACP SDK manifest")
    acp_sdk = {
        "name": sdk_manifest.get("name"),
        "version": sdk_manifest.get("version"),
        "entrySha256": _sha(
            sdk_root / "dist/acp.js", "ACP SDK public entry"),
    }

    esbuild_version_found, esbuild_root = _discover_esbuild(
        source_root, esbuild_version)
    old_platform_hashes = _old_dict(
        _old_dict(old, "buildTool"), "binarySha256ByPlatform")
    platform_hashes = dict(old_platform_hashes)
    binary_path = esbuild_root / _ESBUILD_BINARY
    platform_key = _platform_key()
    platform_hashes[platform_key] = _sha(
        binary_path, f"esbuild binary {platform_key}")
    build_tool = {
        "name": "esbuild",
        "version": esbuild_version_found,
        "entry": _ESBUILD_ENTRY,
        "entrySha256": _sha(esbuild_root / _ESBUILD_ENTRY, "esbuild JS entry"),
        "binary": _ESBUILD_BINARY,
        "binarySha256ByPlatform": dict(sorted(platform_hashes.items())),
    }

    profile_bundle = dict(_old_dict(old, "profileBundle"))
    patch_path = _DEFAULT_CONTRACT.parent / "cordis.patch.yml"
    profile_bundle["patchSha256"] = _sha(
        patch_path, "myagents DSH bundle patch")
    entry_path = bundle_entry if bundle_entry is not None else (
        _DEFAULT_BUNDLE_ENTRY if _DEFAULT_BUNDLE_ENTRY.is_file() else None)
    if entry_path is not None:
        profile_bundle["entrySha256"] = _sha(
            entry_path, "myagents DSH built entry")
    else:
        print(
            "注意：本地没有已构建 bundle entry，保留旧 entrySha256；"
            "package/release gate 会在构建后校验。",
            file=sys.stderr)

    plugin_manifest = check._json_object(
        _DEFAULT_PLUGIN_PACKAGE, "myagents DSH plugin package.json")
    plugin_version = plugin_manifest.get("version")
    if not isinstance(plugin_version, str) or not plugin_version.strip():
        _fail(f"{_DEFAULT_PLUGIN_PACKAGE} 缺少非空 version")

    contract = {
        "schemaVersion": old.get("schemaVersion", 5),
        "hostVersion": plugin_version,
        "compatibilityRevision": old.get("compatibilityRevision"),
        "policyRevision": old.get("policyRevision"),
        "dshRoot": dsh_root,
        "acpSdk": acp_sdk,
        "buildTool": build_tool,
        "profileBundle": profile_bundle,
        "publicPackages": _public_packages(source_root),
    }
    for key in ("compatibilityRevision", "policyRevision"):
        if contract[key] is None:
            _fail(f"现有契约缺少 {key}；revision 只能人工 bump，不能自动推导")
    return contract


def _sync_plugin_peer_dependencies(contract: dict) -> dict:
    """把 plugin package.json 的 DSH peer deps 同步到新版本。"""
    manifest = check._json_object(
        _DEFAULT_PLUGIN_PACKAGE, "myagents DSH plugin package.json")
    peers = manifest.get("peerDependencies")
    if not isinstance(peers, dict):
        return manifest
    public = contract["publicPackages"]
    changed = False
    for name in list(peers):
        pinned = public.get(name)
        if isinstance(pinned, dict) and peers[name] != pinned["version"]:
            peers[name] = pinned["version"]
            changed = True
    if changed:
        manifest["peerDependencies"] = dict(sorted(peers.items()))
    return manifest


def _render(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _unified_diff(old: dict, new: dict) -> str:
    old_text = _render(old).splitlines(keepends=True)
    new_text = _render(new).splitlines(keepends=True)
    return "".join(difflib.unified_diff(
        old_text, new_text, fromfile="runtime-contract.json（旧）",
        tofile="runtime-contract.json（新）"))


def _atomic_write(path: Path, text: str) -> Path:
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        handle.write(text)
        tmp = Path(handle.name)
    tmp.chmod(0o644)
    return tmp


def main() -> None:
    parser = argparse.ArgumentParser(
        description="重新生成 DSH runtime contract（默认 dry-run）")
    parser.add_argument(
        "--cli", type=str, default=None,
        help="npm 安装的官方 dsh 入口（lib/bin.js 绝对路径）；"
             "installed 模式只更新 dshRoot.version 与 cliEntrySha256")
    parser.add_argument(
        "--source-root", type=str, default=None,
        help="官方 DSH checkout 绝对路径；缺省读 MYAGENTS_DSH_SOURCE_ROOT")
    parser.add_argument(
        "--contract", type=Path, default=_DEFAULT_CONTRACT,
        help="契约文件路径（默认仓库内 dsh_acp/plugin/runtime-contract.json）")
    parser.add_argument(
        "--bundle-entry", type=Path, default=None,
        help="已构建 bundle entry（默认自动探测 dsh_acp/plugin/lib/index.js）")
    parser.add_argument(
        "--esbuild-version", type=str, default=None,
        help="checkout 中存在多个 esbuild 版本时显式指定")
    parser.add_argument(
        "--accept", action="store_true",
        help="写入新契约并同步 plugin package.json peerDependencies")
    arguments = parser.parse_args()

    for label, path in (
            ("contract", arguments.contract),
            ("bundle-entry", arguments.bundle_entry)):
        if path is not None and not path.is_absolute():
            _fail(f"{label} 必须使用绝对路径：{path}")
    raw_source = arguments.source_root or os.environ.get(
        "MYAGENTS_DSH_SOURCE_ROOT")
    raw_cli = arguments.cli or os.environ.get("MYAGENTS_DSH_CLI")
    if raw_source and raw_cli:
        _fail("--source-root 与 --cli 互斥：一次只能对齐一种模式")
    old = check._json_object(arguments.contract, "DSH runtime contract")

    bundle_entry = arguments.bundle_entry
    if bundle_entry is not None:
        try:
            bundle_entry = bundle_entry.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            _fail(f"bundle entry 不可用：{arguments.bundle_entry}（{exc}）")

    if raw_cli:
        cli_entry = _resolve_installed_cli(raw_cli)
        new = _build_contract_installed(old, cli_entry, bundle_entry)
        diff = _unified_diff(old, new)
        if not diff:
            print("✓ 契约已是最新，无需更新")
            return
        print(diff, end="")
        if not arguments.accept:
            print("（dry-run：未写入。确认后加 --accept）")
            return
        tmp_contract = _atomic_write(arguments.contract, _render(new))
        try:
            tmp_contract.replace(arguments.contract)
        except OSError as exc:
            tmp_contract.unlink(missing_ok=True)
            _fail(f"写入失败（原文件未动）：{exc}")
        print(f"✓ 已写入 {arguments.contract}")
        print(
            "注意：sourceCommit/cliRuntimeFiles/publicPackages/acpSdk/"
            "buildTool 仍基于旧源码 checkout，源码 release gate 在用 "
            "--source-root 重新对齐前不可用。")
        return

    if raw_source is None:
        _fail("必须提供 --source-root/--cli 或环境变量 "
              "MYAGENTS_DSH_SOURCE_ROOT/MYAGENTS_DSH_CLI")
    source_root = _resolve_source_root(raw_source)

    new = _build_contract(
        source_root, old,
        bundle_entry=bundle_entry,
        esbuild_version=arguments.esbuild_version)

    diff = _unified_diff(old, new)
    if not diff:
        print("✓ 契约已是最新，无需更新")
        return
    print(diff, end="")
    if not arguments.accept:
        print("（dry-run：未写入。确认后加 --accept）")
        return

    new_plugin_manifest = _sync_plugin_peer_dependencies(new)
    tmp_contract = _atomic_write(arguments.contract, _render(new))
    tmp_package = _atomic_write(_DEFAULT_PLUGIN_PACKAGE, _render(new_plugin_manifest))
    try:
        tmp_contract.replace(arguments.contract)
        tmp_package.replace(_DEFAULT_PLUGIN_PACKAGE)
    except OSError as exc:
        tmp_contract.unlink(missing_ok=True)
        tmp_package.unlink(missing_ok=True)
        _fail(f"写入失败（原文件未动）：{exc}")
    print(f"✓ 已写入 {arguments.contract}")
    print(f"✓ 已同步 {_DEFAULT_PLUGIN_PACKAGE}")

    # 自证：refresh 的产物必须立即通过验收 gate 的同一套校验。
    check._verify_source(source_root, check._json_object(
        arguments.contract, "DSH runtime contract"))
    print("✓ refresh 自证通过（check-dsh-runtime-contract 同一算法）")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as exc:
        _fail(str(exc))

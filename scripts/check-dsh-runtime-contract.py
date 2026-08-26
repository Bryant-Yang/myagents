#!/usr/bin/env python3
"""Verify the pinned DSH source and built bundle without mutating either tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import stat
import subprocess
import sys
from pathlib import Path


_RELATIVE_ESM_IMPORT_PATTERNS = (
    re.compile(rb'(?:\bfrom\s*|\bimport\s*)["\'](\./[^"\']+)["\']'),
    re.compile(rb'\bimport\s*\(\s*["\'](\./[^"\']+)["\']\s*\)'),
)


def _fail(message: str) -> None:
    raise SystemExit(f"DSH runtime contract 失败：{message}")


def _json_object(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"{label} 不是可读 JSON object：{path}（{exc}）")
    if not isinstance(payload, dict):
        _fail(f"{label} 必须是 JSON object：{path}")
    return payload


def _sha256(path: Path, label: str) -> str:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        _fail(f"{label} 不存在或无法解析：{path}（{exc}）")
    if stat.S_ISLNK(metadata.st_mode) or not resolved.is_file():
        _fail(f"{label} 必须是非 symlink 普通文件：{path}")
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        _fail(f"{label} 无法读取：{resolved}（{exc}）")
    return digest.hexdigest()


def _expect_sha(path: Path, expected: object, label: str) -> None:
    if not isinstance(expected, str) or len(expected) != 64:
        _fail(f"{label} contract SHA-256 无效：{expected!r}")
    actual = _sha256(path, label)
    if actual != expected:
        _fail(f"{label} SHA-256 漂移：期望 {expected}，实际 {actual}")


def _git_value(source_root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                str(source_root),
                *arguments,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail(f"无法读取 DSH Git identity（{exc}）")
    return completed.stdout.strip()


def _source_packages(source_root: Path) -> dict[str, tuple[Path, dict]]:
    packages: dict[str, tuple[Path, dict]] = {}
    for container in (source_root / "packages", source_root / "vendor"):
        if not container.is_dir():
            _fail(f"DSH source 缺少 package container：{container}")
        for manifest_path in container.glob("**/package.json"):
            relative_parts = manifest_path.relative_to(container).parts
            if "node_modules" in relative_parts or ".pnpm" in relative_parts:
                continue
            manifest = _json_object(manifest_path, "DSH package manifest")
            name = manifest.get("name")
            if not isinstance(name, str):
                continue
            if name in packages:
                _fail(f"DSH source package name 重复：{name}")
            packages[name] = (manifest_path.parent, manifest)
    return packages


def _runtime_entry(manifest: dict, package_name: str) -> str:
    exports = manifest.get("exports")
    candidate: object = None
    if isinstance(exports, str):
        candidate = exports
    elif isinstance(exports, dict):
        root_export = exports.get(".")
        if isinstance(root_export, str):
            candidate = root_export
        elif isinstance(root_export, dict):
            candidate = root_export.get("import") or root_export.get("default")
    if not isinstance(candidate, str):
        candidate = manifest.get("module") or manifest.get("main")
    if not isinstance(candidate, str) or not candidate.strip():
        _fail(f"{package_name} 未声明可验证的 ESM runtime entry")
    relative = candidate.removeprefix("./")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        _fail(f"{package_name} runtime entry 路径无效：{candidate!r}")
    return relative


def _platform_key() -> str:
    machine = platform.machine().lower()
    machine = {"aarch64": "arm64", "x86_64": "x64"}.get(machine, machine)
    system = {"darwin": "darwin", "linux": "linux"}.get(sys.platform)
    if system is None:
        _fail(f"当前平台未纳入 DSH build-tool contract：{sys.platform}")
    return f"{system}-{machine}"


def _verified_build_tool(source_root: Path, contract: dict) -> Path:
    build_tool = contract.get("buildTool")
    if not isinstance(build_tool, dict):
        _fail("runtime-contract.json 缺少 buildTool")
    name = build_tool.get("name")
    version = build_tool.get("version")
    if name != "esbuild" or not isinstance(version, str):
        _fail(f"buildTool identity 无效：{name!r}@{version!r}")
    matches = list((source_root / "node_modules/.pnpm").glob(
        f"esbuild@{version}*/node_modules/esbuild/package.json"
    ))
    if len(matches) != 1:
        _fail(f"必须精确定位一个 esbuild@{version} package，实际 {len(matches)} 个")
    package_root = matches[0].parent
    manifest = _json_object(matches[0], "esbuild manifest")
    if manifest.get("name") != name or manifest.get("version") != version:
        _fail(
            "esbuild identity/version 漂移："
            f"实际 {manifest.get('name')!r}@{manifest.get('version')!r}"
        )
    entry = build_tool.get("entry")
    binary = build_tool.get("binary")
    if not isinstance(entry, str) or not isinstance(binary, str):
        _fail("buildTool entry/binary 必须是相对路径")
    for value, label in ((entry, "entry"), (binary, "binary")):
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            _fail(f"buildTool {label} 路径无效：{value!r}")
    _expect_sha(
        package_root / entry,
        build_tool.get("entrySha256"),
        "esbuild JS entry",
    )
    platform_hashes = build_tool.get("binarySha256ByPlatform")
    platform_key = _platform_key()
    if not isinstance(platform_hashes, dict) or platform_key not in platform_hashes:
        _fail(f"esbuild binary 未 pin 当前平台：{platform_key}")
    binary_path = package_root / binary
    _expect_sha(
        binary_path,
        platform_hashes.get(platform_key),
        f"esbuild binary {platform_key}",
    )
    return binary_path.resolve(strict=True)


def _verify_cli_runtime_closure(
    source_root: Path,
    entry: Path,
    expected: object,
) -> None:
    if not isinstance(expected, dict) or not expected:
        _fail("dshRoot.cliRuntimeFiles 必须是非空 object")
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
        observed[relative] = _sha256(current, f"DSH CLI runtime file {relative}")
        try:
            payload = current.read_bytes()
        except OSError as exc:
            _fail(f"DSH CLI runtime file 无法读取：{current}（{exc}）")
        imports: set[str] = set()
        for pattern in _RELATIVE_ESM_IMPORT_PATTERNS:
            imports.update(match.decode("utf-8") for match in pattern.findall(payload))
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
    if set(observed) != set(expected):
        _fail(
            "DSH CLI runtime import closure 漂移："
            f"期望 {sorted(expected)!r}，实际 {sorted(observed)!r}"
        )
    for relative, actual_hash in observed.items():
        expected_hash = expected.get(relative)
        if actual_hash != expected_hash:
            _fail(
                f"DSH CLI runtime file {relative} SHA-256 漂移："
                f"期望 {expected_hash!r}，实际 {actual_hash}"
            )


def _verify_source(source_root: Path, contract: dict) -> Path:
    try:
        canonical_root = source_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        _fail(f"DSH source root 不存在：{source_root}（{exc}）")
    if canonical_root != source_root or not source_root.is_dir():
        _fail(f"DSH source root 必须是 canonical 绝对目录：{source_root}")
    git_root = Path(_git_value(source_root, "rev-parse", "--show-toplevel"))
    if git_root.resolve(strict=True) != source_root:
        _fail(f"DSH source root 不是 Git 根目录：{git_root}")

    dsh_root = contract.get("dshRoot")
    if not isinstance(dsh_root, dict):
        _fail("runtime-contract.json 缺少 dshRoot")
    root_manifest = _json_object(source_root / "package.json", "DSH root manifest")
    for key in ("name", "version"):
        if root_manifest.get(key) != dsh_root.get(key):
            _fail(
                f"DSH root {key} 漂移：期望 {dsh_root.get(key)!r}，"
                f"实际 {root_manifest.get(key)!r}"
            )
    actual_head = _git_value(source_root, "rev-parse", "HEAD")
    if actual_head != dsh_root.get("sourceCommit"):
        _fail(
            "DSH source commit 漂移："
            f"期望 {dsh_root.get('sourceCommit')!r}，实际 {actual_head!r}"
        )
    cli_entry = dsh_root.get("cliEntry")
    if not isinstance(cli_entry, str):
        _fail("dshRoot.cliEntry 必须是相对路径")
    cli_path = Path(cli_entry)
    if cli_path.is_absolute() or ".." in cli_path.parts:
        _fail(f"dshRoot.cliEntry 路径无效：{cli_entry!r}")
    cli_manifest = _json_object(
        source_root / "apps/cli/package.json",
        "DSH source CLI manifest",
    )
    declared_bin = cli_manifest.get("bin")
    declared_entry = (
        declared_bin.get("dsh") if isinstance(declared_bin, dict) else declared_bin
    )
    if (
        cli_manifest.get("name") != "@deepseek-ai/dsh"
        or cli_manifest.get("version") != dsh_root.get("version")
        or declared_entry != "lib/bin.js"
        or cli_entry != "apps/cli/lib/bin.js"
    ):
        _fail("DSH source CLI manifest/entry 与 runtime contract 不一致")
    _expect_sha(
        source_root / cli_path,
        dsh_root.get("cliEntrySha256"),
        "DSH official built CLI entry",
    )
    _verify_cli_runtime_closure(
        source_root,
        source_root / cli_path,
        dsh_root.get("cliRuntimeFiles"),
    )

    acp_sdk = contract.get("acpSdk")
    if not isinstance(acp_sdk, dict):
        _fail("runtime-contract.json 缺少 acpSdk")
    sdk_root = (
        source_root / "node_modules" / "@agentclientprotocol" / "sdk"
    ).resolve(strict=True)
    sdk_manifest = _json_object(sdk_root / "package.json", "ACP SDK manifest")
    if (
        sdk_manifest.get("name") != acp_sdk.get("name")
        or sdk_manifest.get("version") != acp_sdk.get("version")
    ):
        _fail(
            "ACP SDK identity/version 漂移："
            f"实际 {sdk_manifest.get('name')!r}@{sdk_manifest.get('version')!r}"
        )
    _expect_sha(
        sdk_root / "dist/acp.js",
        acp_sdk.get("entrySha256"),
        "ACP SDK public entry",
    )

    expected_packages = contract.get("publicPackages")
    if not isinstance(expected_packages, dict) or not expected_packages:
        _fail("runtime-contract.json publicPackages 必须是非空 object")
    observed_packages = _source_packages(source_root)
    for name, expected in expected_packages.items():
        if not isinstance(name, str) or not isinstance(expected, dict):
            _fail(f"publicPackages entry 无效：{name!r}")
        package = observed_packages.get(name)
        if package is None:
            _fail(f"DSH source 缺少 pinned public package：{name}")
        package_root, manifest = package
        if manifest.get("version") != expected.get("version"):
            _fail(
                f"{name} 版本漂移：期望 {expected.get('version')!r}，"
                f"实际 {manifest.get('version')!r}"
            )
        _expect_sha(
            package_root / "src/index.ts",
            expected.get("entrySha256"),
            f"{name} public source entry",
        )
        runtime_entry = _runtime_entry(manifest, name)
        if runtime_entry != expected.get("runtimeEntry"):
            _fail(
                f"{name} runtime entry 漂移："
                f"期望 {expected.get('runtimeEntry')!r}，实际 {runtime_entry!r}"
            )
        _expect_sha(
            package_root / runtime_entry,
            expected.get("runtimeEntrySha256"),
            f"{name} public runtime entry",
        )
    return _verified_build_tool(source_root, contract)


def _verify_bundle(entry: Path, patch: Path, contract: dict) -> None:
    profile_bundle = contract.get("profileBundle")
    if not isinstance(profile_bundle, dict):
        _fail("runtime-contract.json 缺少 profileBundle")
    _expect_sha(
        entry,
        profile_bundle.get("entrySha256"),
        "myagents DSH built entry",
    )
    _expect_sha(
        patch,
        profile_bundle.get("patchSha256"),
        "myagents DSH bundle patch",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--bundle-entry", type=Path)
    parser.add_argument("--bundle-patch", type=Path)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--print-esbuild-bin", action="store_true")
    arguments = parser.parse_args()
    paths = [("runtime contract", arguments.contract)]
    if arguments.source_root is not None:
        paths.append(("source root", arguments.source_root))
    if arguments.bundle_entry is not None:
        paths.append(("bundle entry", arguments.bundle_entry))
    if arguments.bundle_patch is not None:
        paths.append(("bundle patch", arguments.bundle_patch))
    for label, path in paths:
        if not path.is_absolute():
            _fail(f"{label} 必须使用绝对路径：{path}")
    if arguments.source_root is None and arguments.bundle_entry is None:
        _fail("至少指定 source preflight 或 bundle postflight")
    if (arguments.bundle_entry is None) != (arguments.bundle_patch is None):
        _fail("bundle entry 与 patch 必须同时指定")
    if arguments.print_esbuild_bin and arguments.source_root is None:
        _fail("--print-esbuild-bin 只适用于 source preflight")
    contract = _json_object(arguments.contract, "DSH runtime contract")
    esbuild_binary = None
    if arguments.source_root is not None:
        esbuild_binary = _verify_source(arguments.source_root, contract)
    if arguments.bundle_entry is not None:
        assert arguments.bundle_patch is not None
        _verify_bundle(arguments.bundle_entry, arguments.bundle_patch, contract)
    if arguments.print_esbuild_bin:
        assert esbuild_binary is not None
        print(esbuild_binary)
    else:
        print("✓ DSH source/bundle runtime contract 精确匹配")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as exc:
        _fail(str(exc))

"""DeepSeek Harness（DSH）的官方 profile ACP-only adapter。

DSH 由官方 ``dsh --profile myagents`` 入口启动。源码模式只把一个已经
构建好的 DSH checkout 当作官方 CLI 的定位来源；readiness 不执行命令、安装
依赖、构建源码，也不 fingerprint 整棵源码树。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Mapping

from acp.adapter import AcpAdapter, AgentPermissionHandler, SessionPreparation
from acp.client import AcpClient, AcpError
from adapters.base import AgentEvent, ExecutionMode
from agent_readiness import AgentReadiness, ExecutableResolver, ReadinessState
from clipboard_image import prompt_images

_CANCEL_TIMEOUT = 10
_INACTIVITY_TIMEOUT = 120
_TOOL_INACTIVITY_TIMEOUT = 900

_DSH_CLI_ENV = "MYAGENTS_DSH_CLI"
_DSH_SOURCE_ROOT_ENV = "MYAGENTS_DSH_SOURCE_ROOT"
_DSH_HOME_ENV = "DSH_HOME"
_DSH_PERSISTENCE_ENV = "DSH_ACP_PERSISTENCE_DIR"
_DSH_RUNTIME_HOME_ENV = "DSH_ACP_RUNTIME_HOME"
_DSH_ATTACHMENT_HOME_ENV = "DSH_ACP_ATTACHMENT_HOME"
_DSH_SETTINGS_FILE_ENV = "DSH_ACP_SETTINGS_FILE"
_DSH_CREDENTIALS_FILE_ENV = "DSH_ACP_CREDENTIALS_FILE"
_DSH_AGENTS_HOME_ENV = "DSH_AGENTS_HOME"
_DSH_RUNTIME_PROFILE_ENV = "DSH_ACP_PROFILE"

_DSH_PROFILE_NAME = "myagents"
_DSH_PROFILE_BUNDLES = (
    "@deepseek-ai/dsh-base",
    "@myagents/dsh-acp-host",
)
_DSH_PLUGIN_NAME = "@myagents/dsh-acp-host"
_DSH_PLUGIN_VERSION = "0.1.0"
_DSH_RUNTIME_PACKAGE = "@deepseek-ai/dsh"
_DSH_RUNTIME_ROOT_PACKAGE = "@deepseek-ai/dsh-root"
_DSH_RUNTIME_VERSION = "0.1.1-rc.2"
_DSH_SOURCE_CLI_PACKAGE = Path("apps/cli/package.json")
_DSH_SOURCE_CLI_BIN = Path("apps/cli/lib/bin.js")
_MAX_MANIFEST_BYTES = 1_048_576
_MAX_CONFIG_BYTES = 1_048_576
_MAX_USER_PATCH_BYTES = 65_536
_MAX_PLUGIN_ENTRY_BYTES = 1_048_576
_MAX_PLUGIN_PATCH_BYTES = 65_536
_UNAVAILABLE_COMMAND = "/__myagents_dsh_configuration_required__/dsh"

DSH_ACP_WORKSPACE_PROFILE = "workspace-write"
DSH_ACP_READ_ONLY_PROFILE = "read-only"
_DSH_AGENT_NAME = "dsh-myagents-acp"
_DSH_HOST_VERSION = _DSH_PLUGIN_VERSION
_DSH_PROFILE_META_KEY = "deepseek.ai/dsh-myagents-profile"
_DSH_POLICY_REVISION_META_KEY = "deepseek.ai/dsh-myagents-policy-revision"
_DSH_READ_ONLY_TOOLS_META_KEY = "deepseek.ai/dsh-myagents-read-only-tools"
_DSH_RUNTIME_VERSION_META_KEY = "deepseek.ai/dsh-runtime-version"
_DSH_COMPATIBILITY_REVISION_META_KEY = "deepseek.ai/dsh-compatibility-revision"
_DSH_POLICY_REVISION = 1
_DSH_COMPATIBILITY_REVISION = 1
_DSH_READ_ONLY_TOOLS = ["read", "glob", "grep"]
_DSH_PERMISSION_KINDS = {"allow_once", "reject_once"}
_DSH_PLUGIN_ENTRY_SHA256 = (
    "a8515ac654705e07ea04c2f7c2f3df452500c9ad902652cb0d434af49aba3290"
)
_DSH_PLUGIN_PATCH_SHA256 = (
    "744d7ce4370c0bac08db1b53e0da407005a2e053077648b85adbd69c996afbb2"
)


class _DshNotFoundError(AcpError):
    """A required DSH installation artifact is absent."""


@dataclass(frozen=True)
class _DshLaunch:
    command: tuple[str, ...]
    executable: Path
    package_root: Path
    profile_home: Path
    profile_dir: Path
    plugin_root: Path
    detail: str
    source_root: Path | None = None
    node: Path | None = None


@dataclass(frozen=True)
class _DshStateLayout:
    persistence: Path
    sessions: Path
    runtime_home: Path
    attachment_home: Path
    agents_home: Path
    settings_file: Path
    credentials_file: Path
    profile_home: Path
    settings_source: Path | None = None
    credentials_source: Path | None = None

    def child_environment(self) -> dict[str, str]:
        return {
            _DSH_PERSISTENCE_ENV: str(self.persistence),
            _DSH_RUNTIME_HOME_ENV: str(self.runtime_home),
            _DSH_ATTACHMENT_HOME_ENV: str(self.attachment_home),
            _DSH_SETTINGS_FILE_ENV: str(self.settings_file),
            _DSH_CREDENTIALS_FILE_ENV: str(self.credentials_file),
            _DSH_HOME_ENV: str(self.profile_home),
            _DSH_AGENTS_HOME_ENV: str(self.agents_home),
            _DSH_RUNTIME_PROFILE_ENV: DSH_ACP_WORKSPACE_PROFILE,
        }


_DSH_CHILD_ENV_REMOVALS = frozenset({
    _DSH_CLI_ENV,
    _DSH_SOURCE_ROOT_ENV,
    "DSH_ACP_ENV_DIR",
    "DSH_ACP_SOURCE_ROOT",
    "TSX_TSCONFIG_PATH",
    _DSH_PERSISTENCE_ENV,
    _DSH_RUNTIME_HOME_ENV,
    _DSH_ATTACHMENT_HOME_ENV,
    _DSH_SETTINGS_FILE_ENV,
    _DSH_CREDENTIALS_FILE_ENV,
    _DSH_HOME_ENV,
    _DSH_AGENTS_HOME_ENV,
    _DSH_RUNTIME_PROFILE_ENV,
    "NODE_OPTIONS",
    "NODE_PATH",
    "NODE_V8_COVERAGE",
    "TS_NODE_PROJECT",
    "TS_NODE_TRANSPILE_ONLY",
    "TS_NODE_COMPILER_OPTIONS",
    "TS_NODE_REQUIRE",
    "TS_NODE_FILES",
})


def _read_json_object(path: Path, label: str) -> dict:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise AcpError(f"{label} 不存在或无法解析：{path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.R_OK):
        raise AcpError(f"{label} 不是可读普通文件：{resolved}")
    if metadata.st_size > _MAX_MANIFEST_BYTES:
        raise AcpError(f"{label} 超过 {_MAX_MANIFEST_BYTES} bytes 上限：{resolved}")
    try:
        payload = resolved.read_bytes()
        parsed = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcpError(f"{label} 不是有效 JSON：{resolved}") from exc
    if len(payload) > _MAX_MANIFEST_BYTES or not isinstance(parsed, dict):
        raise AcpError(f"{label} 必须是有界 JSON object：{resolved}")
    return parsed


def _validated_executable(candidate: str | Path, source: str) -> Path:
    path = Path(candidate).expanduser()
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise AcpError(f"{source} 指定的可执行文件不存在：{path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise AcpError(f"{source} 指定的文件不可执行：{resolved}")
    return resolved


def _validated_regular_file(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise AcpError(f"{label} 不存在：{path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.R_OK):
        raise AcpError(f"{label} 不是可读普通文件：{resolved}")
    return resolved


def _declared_dsh_bin(package: dict, package_root: Path) -> Path:
    declared = package.get("bin")
    if isinstance(declared, str):
        relative = declared
    elif isinstance(declared, dict):
        relative = declared.get("dsh")
    else:
        relative = None
    if not isinstance(relative, str) or not relative.strip():
        raise AcpError("官方 DSH package.json 未声明 bin.dsh")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise AcpError(f"官方 DSH bin.dsh 路径无效：{relative!r}")
    return _validated_regular_file(package_root / relative_path, "官方 DSH bin.dsh")


def _validated_cli_package(entry: Path) -> Path:
    observed: list[str] = []
    for package_root in (entry.parent, entry.parent.parent):
        manifest_path = package_root / "package.json"
        if not manifest_path.is_file():
            continue
        manifest = _read_json_object(manifest_path, "DSH CLI package.json")
        observed.append(f"{manifest.get('name')!r}@{manifest.get('version')!r}")
        if manifest.get("name") != _DSH_RUNTIME_PACKAGE:
            continue
        if manifest.get("version") != _DSH_RUNTIME_VERSION:
            raise AcpError(
                "DSH CLI 版本不兼容：期望 "
                f"{_DSH_RUNTIME_VERSION}，实际 {manifest.get('version')!r}"
            )
        declared_entry = _declared_dsh_bin(manifest, package_root)
        if declared_entry != entry:
            raise AcpError(
                "DSH CLI 入口与 package.json 的 bin.dsh 不一致："
                f"entry={entry} declared={declared_entry}"
            )
        return package_root.resolve()
    detail = ", ".join(observed) or "未找到相邻 package.json"
    raise AcpError(
        "DSH CLI 不是可证明版本的官方 @deepseek-ai/dsh 入口："
        f"{entry}（{detail}）"
    )


def _absolute_dsh_home(environ: Mapping[str, str]) -> Path:
    configured = environ.get(_DSH_HOME_ENV, "").strip()
    if configured:
        candidate = Path(configured).expanduser()
    else:
        home = environ.get("HOME", "").strip()
        candidate = (Path(home).expanduser() if home else Path.home()) / ".dsh"
    if not candidate.is_absolute():
        raise AcpError(f"{_DSH_HOME_ENV} 必须解析为绝对路径：{candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AcpError(f"DSH profile home 不存在：{candidate}") from exc
    if not resolved.is_dir():
        raise AcpError(f"DSH profile home 不是目录：{resolved}")
    return resolved


def _canonical_profile_directory(profile_home: Path) -> Path:
    """Resolve the exact stock profile without accepting symlink indirection."""
    profiles_dir = profile_home / "profiles"
    profile_dir = profiles_dir / _DSH_PROFILE_NAME
    for label, path in (
        ("DSH profiles 目录", profiles_dir),
        ("DSH myagents profile 目录", profile_dir),
    ):
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AcpError(f"{label} 不存在或无法解析：{path}") from exc
        lexical = path.absolute()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AcpError(f"{label} 必须是非 symlink 目录：{path}")
        if resolved != lexical or not resolved.is_relative_to(profile_home):
            raise AcpError(
                f"{label} 必须 canonical 且位于 DSH profile home 内：{resolved}"
            )
    return profile_dir


def _read_canonical_bounded_file(
    path: Path,
    label: str,
    *,
    parent: Path,
    byte_limit: int,
) -> bytes:
    """Read one exact regular file through a stable no-follow descriptor."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(no_follow, int):
        raise AcpError(f"{label} 校验需要 O_NOFOLLOW 支持")
    flags = os.O_RDONLY | no_follow
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AcpError(f"{label} 无法安全打开：{path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > byte_limit
        ):
            raise AcpError(
                f"{label} 必须是单链接且不超过 {byte_limit} bytes 的普通文件"
            )
        try:
            path_metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AcpError(f"{label} 读取期间无法解析：{path}") from exc
        if (
            stat.S_ISLNK(path_metadata.st_mode)
            or (path_metadata.st_dev, path_metadata.st_ino)
            != (before.st_dev, before.st_ino)
            or resolved != path.absolute()
            or not resolved.is_relative_to(parent)
        ):
            raise AcpError(f"{label} 必须 canonical 且位于 {parent} 内")
        chunks: list[bytes] = []
        total = 0
        while total <= byte_limit:
            chunk = os.read(descriptor, min(65_536, byte_limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        before_id = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        after_id = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        try:
            final_path = path.lstat()
        except OSError as exc:
            raise AcpError(f"{label} 读取期间被移除：{path}") from exc
        if (
            len(payload) > byte_limit
            or len(payload) != before.st_size
            or before_id != after_id
            or stat.S_ISLNK(final_path.st_mode)
            or (final_path.st_dev, final_path.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise AcpError(f"{label} 读取期间发生变化")
        return payload
    finally:
        os.close(descriptor)


def _validated_empty_user_patch(path: Path, label: str, parent: Path) -> None:
    """Allow the stock empty layer only; any effective later patch is unsafe."""
    if not os.path.lexists(path):
        return
    payload = _read_canonical_bounded_file(
        path,
        label,
        parent=parent,
        byte_limit=_MAX_USER_PATCH_BYTES,
    )
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise AcpError(f"{label} 必须是 UTF-8 YAML 空数组") from exc
    if any(
        (ord(character) < 0x20 and character not in "\t\r\n")
        or ord(character) == 0x7F
        for character in text
    ):
        raise AcpError(f"{label} 包含 YAML 不允许的控制字符")
    semantic_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if semantic_lines != ["[]"]:
        raise AcpError(
            f"{label} 是 bundle 之后生效的 user patch；只允许 YAML 空数组 []"
        )


def _validated_bundle_artifact(
    path: Path,
    label: str,
    *,
    parent: Path,
    byte_limit: int,
    expected_sha256: str,
) -> None:
    payload = _read_canonical_bounded_file(
        path,
        label,
        parent=parent,
        byte_limit=byte_limit,
    )
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise AcpError(
            f"{label} 与 checked-in bundle contract 不匹配："
            f"期望 SHA-256 {expected_sha256}，实际 {actual_sha256}"
        )


def _validated_profile(profile_home: Path) -> tuple[Path, Path]:
    profile_dir = _canonical_profile_directory(profile_home)
    _validated_empty_user_patch(
        profile_home / "cordis.patch.yml",
        "DSH home-level user patch",
        profile_home,
    )
    _validated_empty_user_patch(
        profile_dir / "cordis.patch.yml",
        "DSH myagents profile user patch",
        profile_dir,
    )
    manifest_path = profile_dir / "package.json"
    manifest_payload = _read_canonical_bounded_file(
        manifest_path,
        "DSH myagents profile package.json",
        parent=profile_dir,
        byte_limit=_MAX_MANIFEST_BYTES,
    )
    try:
        manifest = json.loads(manifest_payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AcpError(
            f"DSH myagents profile package.json 不是有效 JSON：{manifest_path}"
        ) from exc
    if not isinstance(manifest, dict):
        raise AcpError("DSH myagents profile package.json 必须是 JSON object")
    dsh = manifest.get("dsh")
    profile = dsh.get("profile") if isinstance(dsh, dict) else None
    bundles = profile.get("bundles") if isinstance(profile, dict) else None
    if bundles != list(_DSH_PROFILE_BUNDLES):
        raise AcpError(
            "DSH myagents profile bundles 必须精确等于 "
            f"{list(_DSH_PROFILE_BUNDLES)!r}，实际 {bundles!r}"
        )
    dependencies = manifest.get("dependencies")
    dependency = (
        dependencies.get(_DSH_PLUGIN_NAME)
        if isinstance(dependencies, dict) else None
    )
    if not isinstance(dependency, str) or not dependency.strip():
        raise AcpError(f"DSH myagents profile 未安装 {_DSH_PLUGIN_NAME} dependency")

    package_path = (
        profile_dir / "node_modules" / "@myagents" / "dsh-acp-host" /
        "package.json"
    )
    plugin_manifest = _read_json_object(
        package_path, "myagents DSH bundle package.json"
    )
    plugin_root = package_path.resolve(strict=True).parent
    if plugin_manifest.get("name") != _DSH_PLUGIN_NAME:
        raise AcpError(
            "myagents DSH bundle package identity 不匹配："
            f"{plugin_manifest.get('name')!r}"
        )
    if plugin_manifest.get("version") != _DSH_PLUGIN_VERSION:
        raise AcpError(
            "myagents DSH bundle 版本不兼容：期望 "
            f"{_DSH_PLUGIN_VERSION}，实际 {plugin_manifest.get('version')!r}"
        )
    plugin_dsh = plugin_manifest.get("dsh")
    bundle = plugin_dsh.get("bundle") if isinstance(plugin_dsh, dict) else None
    patch_value = bundle.get("patch") if isinstance(bundle, dict) else None
    if patch_value != "./cordis.patch.yml":
        raise AcpError(
            "myagents DSH bundle dsh.bundle.patch 必须精确等于 "
            f"'./cordis.patch.yml'，实际 {patch_value!r}"
        )
    main_value = plugin_manifest.get("main")
    if main_value != "lib/index.js":
        raise AcpError(
            "myagents DSH bundle main 必须精确等于 'lib/index.js'，"
            f"实际 {main_value!r}"
        )
    patch_relative = Path(patch_value)
    if patch_relative.is_absolute() or ".." in patch_relative.parts:
        raise AcpError(f"myagents DSH bundle patch 路径无效：{patch_value!r}")
    _validated_bundle_artifact(
        plugin_root / patch_relative,
        "myagents DSH bundle patch",
        parent=plugin_root,
        byte_limit=_MAX_PLUGIN_PATCH_BYTES,
        expected_sha256=_DSH_PLUGIN_PATCH_SHA256,
    )
    _validated_bundle_artifact(
        plugin_root / main_value,
        "myagents DSH bundle main",
        parent=plugin_root,
        byte_limit=_MAX_PLUGIN_ENTRY_BYTES,
        expected_sha256=_DSH_PLUGIN_ENTRY_SHA256,
    )
    return profile_dir, plugin_root


def _validated_source_launch(
    raw_source_root: str,
    find: ExecutableResolver,
) -> tuple[Path, Path, Path]:
    candidate = Path(raw_source_root).expanduser()
    if not candidate.is_absolute():
        raise AcpError(f"{_DSH_SOURCE_ROOT_ENV} 必须是绝对路径：{candidate}")
    try:
        source_root = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AcpError(f"DSH 源码根不存在：{candidate}") from exc
    if not source_root.is_dir():
        raise AcpError(f"DSH 源码根不是目录：{source_root}")
    root_manifest = _read_json_object(
        source_root / "package.json", "DSH source root package.json"
    )
    if (
        root_manifest.get("name") != _DSH_RUNTIME_ROOT_PACKAGE
        or root_manifest.get("version") != _DSH_RUNTIME_VERSION
    ):
        raise AcpError(
            "DSH source root identity/version 不兼容：期望 "
            f"{_DSH_RUNTIME_ROOT_PACKAGE}@{_DSH_RUNTIME_VERSION}"
        )
    cli_package_root = (source_root / _DSH_SOURCE_CLI_PACKAGE).parent
    cli_manifest = _read_json_object(
        source_root / _DSH_SOURCE_CLI_PACKAGE, "DSH source CLI package.json"
    )
    if (
        cli_manifest.get("name") != _DSH_RUNTIME_PACKAGE
        or cli_manifest.get("version") != _DSH_RUNTIME_VERSION
    ):
        raise AcpError(
            "DSH source CLI identity/version 不兼容：期望 "
            f"{_DSH_RUNTIME_PACKAGE}@{_DSH_RUNTIME_VERSION}"
        )
    cli_entry = _declared_dsh_bin(cli_manifest, cli_package_root)
    expected_entry = _validated_regular_file(
        source_root / _DSH_SOURCE_CLI_BIN, "DSH source built CLI"
    )
    if cli_entry != expected_entry:
        raise AcpError(
            "DSH source CLI 必须使用官方 apps/cli/lib/bin.js build artifact"
        )
    node_candidate = find("node")
    if not node_candidate:
        raise _DshNotFoundError("已配置 DSH source root，但 PATH 未检测到 node")
    node = _validated_executable(node_candidate, "PATH 中的 node")
    return source_root, expected_entry, node


def _resolve_dsh_launch(
    *,
    environ: Mapping[str, str] | None = None,
    resolver: ExecutableResolver | None = None,
) -> _DshLaunch:
    env = os.environ if environ is None else environ
    find = shutil.which if resolver is None else resolver
    explicit_cli = env.get(_DSH_CLI_ENV, "").strip()
    if explicit_cli:
        entry = _validated_executable(explicit_cli, _DSH_CLI_ENV)
        package_root = _validated_cli_package(entry)
        source_root = None
        node = None
    elif raw_source_root := env.get(_DSH_SOURCE_ROOT_ENV, "").strip():
        source_root, entry, node = _validated_source_launch(raw_source_root, find)
        package_root = (source_root / "apps/cli").resolve()
    else:
        resolved = find("dsh")
        if not resolved:
            raise _DshNotFoundError(
                "当前进程 PATH 未检测到官方 dsh，且未配置 DSH source root"
            )
        entry = _validated_executable(resolved, "PATH 中的 dsh")
        package_root = _validated_cli_package(entry)
        source_root = None
        node = None

    profile_home = _absolute_dsh_home(env)
    profile_dir, plugin_root = _validated_profile(profile_home)
    if source_root is not None:
        assert node is not None
        command = (str(node), str(entry), "--profile", _DSH_PROFILE_NAME)
        detail = (
            "已检测到 DSH source 的官方 built CLI/profile："
            f"{entry} / {_DSH_PROFILE_NAME}"
        )
    else:
        command = (str(entry), "--profile", _DSH_PROFILE_NAME)
        detail = (
            f"已检测到官方 DSH CLI/profile：{entry} / {_DSH_PROFILE_NAME}"
        )
    return _DshLaunch(
        command,
        entry,
        package_root,
        profile_home,
        profile_dir,
        plugin_root,
        detail,
        source_root,
        node,
    )


def _dsh_persistence_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get(_DSH_PERSISTENCE_ENV, "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise AcpError(f"{_DSH_PERSISTENCE_ENV} 必须是绝对路径：{candidate}")
    else:
        xdg = env.get("XDG_STATE_HOME", "").strip()
        if xdg:
            base = Path(xdg).expanduser()
        else:
            home = env.get("HOME", "").strip()
            base = Path(home).expanduser() if home else Path.home()
            base = base / ".local" / "state"
        candidate = base / "myagents" / "dsh-acp"
    if not candidate.is_absolute():
        raise AcpError(f"DSH 产品状态目录必须解析为绝对路径：{candidate}")
    return candidate.resolve()


def _existing_config_source(
    candidate: Path,
    *,
    label: str,
    credentials: bool = False,
) -> Path | None:
    if not os.path.lexists(candidate):
        return None
    try:
        lexical = candidate.absolute()
        resolved = candidate.resolve(strict=True)
        metadata = candidate.lstat()
    except (OSError, RuntimeError) as exc:
        raise AcpError(f"DSH {label} 无法解析：{candidate}") from exc
    if lexical != resolved or stat.S_ISLNK(metadata.st_mode):
        raise AcpError(f"DSH {label} 不得经过 symlink：{candidate}")
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.R_OK):
        raise AcpError(f"DSH {label} 不是可读普通文件：{resolved}")
    if metadata.st_nlink != 1:
        raise AcpError(f"DSH {label} 不得是 hardlink：{resolved}")
    if credentials and os.name != "nt":
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o077:
            raise AcpError(
                f"DSH {label} 权限过宽（mode {mode:o}）；必须仅 owner 可读写"
            )
    return resolved


def _dsh_state_layout(
    profile_home: Path,
    environ: Mapping[str, str] | None = None,
) -> _DshStateLayout:
    persistence = _dsh_persistence_dir(environ)
    runtime_home = persistence / "runtime-home"
    config_home = persistence / "config-inputs"
    return _DshStateLayout(
        persistence,
        persistence / "sessions",
        runtime_home,
        persistence / "attachment-home",
        runtime_home / "agents",
        config_home / "settings.yaml",
        config_home / ".credentials.yaml",
        profile_home,
        _existing_config_source(profile_home / "settings.yaml", label="settings"),
        _existing_config_source(
            profile_home / ".credentials.yaml",
            label="credentials",
            credentials=True,
        ),
    )


def _fallback_state(profile_home: Path) -> _DshStateLayout:
    persistence = Path.home() / ".local/state/myagents/dsh-acp"
    runtime_home = persistence / "runtime-home"
    config_home = persistence / "config-inputs"
    return _DshStateLayout(
        persistence,
        persistence / "sessions",
        runtime_home,
        persistence / "attachment-home",
        runtime_home / "agents",
        config_home / "settings.yaml",
        config_home / ".credentials.yaml",
        profile_home,
    )


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def _validate_state_layout(state: _DshStateLayout) -> None:
    persistence = state.persistence
    if os.path.lexists(persistence):
        metadata = persistence.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AcpError(f"DSH 产品状态根必须是非 symlink 目录：{persistence}")
    expected: tuple[tuple[str, Path, bool], ...] = (
        ("sessions", state.sessions, True),
        ("runtime-home", state.runtime_home, True),
        ("attachment-home", state.attachment_home, True),
        ("agents", state.agents_home, True),
        ("settings", state.settings_file, False),
        ("credentials", state.credentials_file, False),
    )
    for label, path, directory in expected:
        try:
            relative = path.relative_to(persistence)
        except ValueError as exc:
            raise AcpError(f"DSH {label} 必须位于产品状态根内：{path}") from exc
        cursor = persistence
        for part in relative.parts:
            cursor = cursor / part
            if os.path.lexists(cursor) and stat.S_ISLNK(cursor.lstat().st_mode):
                raise AcpError(f"DSH {label} 路径不得包含 symlink：{cursor}")
        if not path.resolve().is_relative_to(persistence):
            raise AcpError(f"DSH {label} 解析到产品状态根之外：{path}")
        if os.path.lexists(path):
            metadata = path.lstat()
            valid = (
                stat.S_ISDIR(metadata.st_mode)
                if directory else stat.S_ISREG(metadata.st_mode)
            )
            if not valid:
                raise AcpError(f"DSH {label} 文件类型无效：{path}")


def _static_protected(launch: _DshLaunch) -> tuple[tuple[str, Path], ...]:
    items = [
        ("profile home", launch.profile_home),
        ("myagents profile", launch.profile_dir),
        ("DSH CLI package", launch.package_root),
        ("myagents bundle", launch.plugin_root),
    ]
    if launch.source_root is not None:
        items.append(("DSH source root", launch.source_root))
    return tuple(items)


def _validate_static_separation(state: _DshStateLayout, launch: _DshLaunch) -> None:
    _validate_state_layout(state)
    for label, protected in _static_protected(launch):
        if _paths_overlap(state.persistence, protected):
            raise AcpError(
                "DSH 产品状态目录与执行依赖重叠："
                f"state={state.persistence} {label}={protected}"
            )


def _validate_workspace_separation(
    state: _DshStateLayout,
    launch: _DshLaunch,
    workspace: Path,
) -> None:
    _validate_static_separation(state, launch)
    protected = (("产品状态目录", state.persistence), *_static_protected(launch))
    for label, path in protected:
        if _paths_overlap(workspace, path):
            raise AcpError(
                f"DSH workspace 与{label}重叠："
                f"workspace={workspace} protected={path}"
            )


def _read_config_snapshot(source: Path | None, label: str) -> bytes:
    if source is None:
        return b""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise AcpError(f"DSH {label} 原始配置无法安全打开：{source}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_CONFIG_BYTES
        ):
            raise AcpError(
                f"DSH {label} 原始配置必须是单链接且不超过 "
                f"{_MAX_CONFIG_BYTES} bytes 的普通文件"
            )
        payload = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
        after = os.fstat(descriptor)
        before_id = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        after_id = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        if len(payload) > _MAX_CONFIG_BYTES or before_id != after_id:
            raise AcpError(f"DSH {label} 原始配置读取期间发生变化")
        return payload
    finally:
        os.close(descriptor)


def _atomic_private_write(target: Path, payload: bytes) -> None:
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp_path = Path(raw_temp)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
        os.chmod(target, 0o600, follow_symlinks=False)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _materialize_product_state(state: _DshStateLayout) -> None:
    _validate_state_layout(state)
    try:
        state.persistence.mkdir(parents=True, mode=0o700, exist_ok=True)
        for directory in (
            state.sessions,
            state.runtime_home,
            state.attachment_home,
            state.agents_home,
            state.settings_file.parent,
        ):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            if os.name != "nt":
                directory.chmod(0o700)
    except OSError as exc:
        raise AcpError(f"DSH 产品状态目录无法创建：{state.persistence}") from exc
    _validate_state_layout(state)
    settings = _read_config_snapshot(state.settings_source, "settings")
    credentials = _read_config_snapshot(state.credentials_source, "credentials")
    try:
        _atomic_private_write(state.settings_file, settings)
        _atomic_private_write(state.credentials_file, credentials)
    except OSError as exc:
        raise AcpError("DSH 产品配置副本无法原子写入") from exc
    _validate_state_layout(state)


def _revalidate_launch(launch: _DshLaunch) -> None:
    if launch.source_root is None:
        entry = _validated_executable(launch.executable, "DSH CLI")
        if _validated_cli_package(entry) != launch.package_root:
            raise AcpError("DSH CLI package 在 readiness 后发生替换")
    else:
        assert launch.node is not None
        source_root, entry, node = _validated_source_launch(
            str(launch.source_root),
            lambda command: str(launch.node) if command == "node" else None,
        )
        if (
            source_root != launch.source_root
            or entry != launch.executable
            or node != launch.node
        ):
            raise AcpError("DSH source official launcher 在 readiness 后发生替换")
    if _validated_profile(launch.profile_home) != (
        launch.profile_dir, launch.plugin_root,
    ):
        raise AcpError("DSH myagents bundle 在 readiness 后发生替换")


def dsh_readiness_probe(
    *,
    environ: Mapping[str, str] | None = None,
    resolver: ExecutableResolver | None = None,
) -> AgentReadiness:
    """Pure-read probe for the official CLI/profile/plugin contract."""
    setup_hint = (
        "安装兼容版官方 dsh 并创建 myagents profile，或用 "
        "MYAGENTS_DSH_CLI / MYAGENTS_DSH_SOURCE_ROOT 定位官方入口；"
        "DSH_HOME 必须包含 profiles/myagents"
    )
    try:
        launch = _resolve_dsh_launch(environ=environ, resolver=resolver)
    except _DshNotFoundError as exc:
        return AgentReadiness("dsh", ReadinessState.NOT_FOUND, str(exc), setup_hint)
    except AcpError as exc:
        return AgentReadiness("dsh", ReadinessState.INVALID, str(exc), setup_hint)
    return AgentReadiness(
        "dsh", ReadinessState.READY, launch.detail, setup_hint,
        str(launch.executable),
    )


class AcpDshAdapter(AcpAdapter):
    """Official DSH profile adapter with two process-isolated policies."""

    def __init__(
        self,
        permission: str = "deny",
        *,
        cancel_timeout: float = _CANCEL_TIMEOUT,
        inactivity_timeout: float = _INACTIVITY_TIMEOUT,
        tool_inactivity_timeout: float = _TOOL_INACTIVITY_TIMEOUT,
    ) -> None:
        self._configuration_error: str | None = None
        self._dsh_workspace: str | None = None
        try:
            launch = _resolve_dsh_launch()
            state = _dsh_state_layout(launch.profile_home)
            _validate_static_separation(state, launch)
            command = launch.command
        except AcpError as exc:
            self._configuration_error = str(exc)
            profile_home = Path.home() / ".dsh"
            state = _fallback_state(profile_home)
            command = (_UNAVAILABLE_COMMAND, "--profile", _DSH_PROFILE_NAME)
            launch = _DshLaunch(
                command,
                Path(_UNAVAILABLE_COMMAND),
                Path(_UNAVAILABLE_COMMAND).parent,
                profile_home,
                profile_home / "profiles" / _DSH_PROFILE_NAME,
                Path(__file__).resolve().parent / "plugin",
                str(exc),
            )
        self._dsh_launch = launch
        self._dsh_state = state
        super().__init__(
            "dsh",
            command,
            permission=permission,
            cancel_timeout=cancel_timeout,
            inactivity_timeout=inactivity_timeout,
            tool_inactivity_timeout=tool_inactivity_timeout,
            env_overrides=state.child_environment(),
            env_removals=_DSH_CHILD_ENV_REMOVALS,
            execution_env_overrides={
                ExecutionMode.READ_ONLY: {
                    _DSH_RUNTIME_PROFILE_ENV: DSH_ACP_READ_ONLY_PROFILE,
                },
                ExecutionMode.WORKSPACE_WRITE: {
                    _DSH_RUNTIME_PROFILE_ENV: DSH_ACP_WORKSPACE_PROFILE,
                },
            },
        )

    @staticmethod
    def _normalized_workspace(workdir: str) -> str:
        candidate = Path(workdir).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AcpError(f"DSH workspace 不存在：{candidate}") from exc
        if not resolved.is_dir():
            raise AcpError(f"DSH workspace 必须是目录：{resolved}")
        return str(resolved)

    def stream_prepared(
        self,
        make_prompt: Callable[[SessionPreparation], str],
        workdir: str,
        resume_session_id: str | None = None,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        workspace = self._normalized_workspace(workdir)
        if self._configuration_error is None:
            _validate_workspace_separation(
                self._dsh_state, self._dsh_launch, Path(workspace)
            )
        if self._dsh_workspace is None:
            self._dsh_workspace = workspace
        elif self._dsh_workspace != workspace:
            raise AcpError(
                "DSH adapter 已绑定 workspace，拒绝静默换 cwd："
                f"bound={self._dsh_workspace} requested={workspace}；请先 aclose()"
            )
        return super().stream_prepared(
            make_prompt,
            workspace,
            resume_session_id,
            execution_mode=execution_mode,
        )

    def set_permission_handler(
        self, handler: AgentPermissionHandler | None
    ) -> None:
        if handler is None:
            super().set_permission_handler(None)
            return

        async def one_shot_only(agent_name: str, params: dict) -> dict:
            options = params.get("options")
            if not isinstance(options, list) or len(options) != 2:
                return {"outcome": "cancelled"}
            option_ids: set[str] = set()
            kinds: set[str] = set()
            for option in options:
                if not isinstance(option, dict):
                    return {"outcome": "cancelled"}
                option_id = option.get("optionId")
                kind = option.get("kind")
                if (
                    not isinstance(option_id, str) or not option_id
                    or not isinstance(kind, str)
                ):
                    return {"outcome": "cancelled"}
                option_ids.add(option_id)
                kinds.add(kind)
            if len(option_ids) != len(options) or kinds != _DSH_PERMISSION_KINDS:
                return {"outcome": "cancelled"}
            outcome = handler(agent_name, params)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            return outcome

        super().set_permission_handler(one_shot_only)

    def _validate_initialized_client(self, client: AcpClient) -> None:
        capabilities = client.capabilities
        if capabilities.get("loadSession") is not True:
            raise AcpError(
                "DSH ACP 未声明 agentCapabilities.loadSession=true；拒绝退化会话"
            )
        session_capabilities = capabilities.get("sessionCapabilities")
        if (
            not isinstance(session_capabilities, dict)
            or not isinstance(session_capabilities.get("close"), dict)
        ):
            raise AcpError(
                "DSH ACP 未声明 sessionCapabilities.close；拒绝生命周期不完整的 worker"
            )
        expected_profile = self._active_env_overrides.get(_DSH_RUNTIME_PROFILE_ENV)
        if expected_profile not in {
            DSH_ACP_WORKSPACE_PROFILE, DSH_ACP_READ_ONLY_PROFILE,
        }:
            raise AcpError("DSH ACP 当前进程没有已知 runtime profile")
        agent_info = client.agent_info
        if (
            not isinstance(agent_info, dict)
            or agent_info.get("name") != _DSH_AGENT_NAME
        ):
            actual = agent_info.get("name") if isinstance(agent_info, dict) else None
            raise AcpError(
                "DSH ACP agentInfo.name 身份校验失败："
                f"期望 {_DSH_AGENT_NAME!r}，实际 {actual!r}"
            )
        if agent_info.get("version") != _DSH_HOST_VERSION:
            raise AcpError(
                "DSH ACP agentInfo.version 校验失败："
                f"期望 {_DSH_HOST_VERSION!r}，实际 {agent_info.get('version')!r}"
            )
        metadata = agent_info.get("_meta")
        if not isinstance(metadata, dict):
            raise AcpError("DSH ACP agentInfo._meta 缺失")
        expected = {
            _DSH_PROFILE_META_KEY: expected_profile,
            _DSH_POLICY_REVISION_META_KEY: _DSH_POLICY_REVISION,
            _DSH_READ_ONLY_TOOLS_META_KEY: _DSH_READ_ONLY_TOOLS,
            _DSH_RUNTIME_VERSION_META_KEY: _DSH_RUNTIME_VERSION,
            _DSH_COMPATIBILITY_REVISION_META_KEY: _DSH_COMPATIBILITY_REVISION,
        }
        integer_revision_keys = {
            _DSH_POLICY_REVISION_META_KEY,
            _DSH_COMPATIBILITY_REVISION_META_KEY,
        }
        for key, value in expected.items():
            actual = metadata.get(key)
            if (
                key in integer_revision_keys
                and type(actual) is not int
            ) or actual != value:
                raise AcpError(
                    f"DSH ACP {key} 校验失败："
                    f"期望 {value!r}，实际 {actual!r}"
                )

    def _images_for_prompt(self, prompt: str) -> tuple:
        images = prompt_images(prompt, self._attachment_root)
        capabilities = self._client.capabilities.get("promptCapabilities", {})
        if images and capabilities.get("image") is not True:
            raise AcpError(
                "DSH ACP 未声明 promptCapabilities.image=true；拒绝退化为纯文本"
            )
        return images

    async def _prepare_locked(
        self,
        workdir: str,
        resume_session_id: str | None = None,
    ) -> SessionPreparation:
        if self._configuration_error is not None:
            raise AcpError(
                f"DSH ACP 未就绪：{self._configuration_error}；修复后执行 /agents rescan"
            )
        workspace = self._normalized_workspace(workdir)
        if self._dsh_workspace != workspace:
            raise AcpError("DSH workspace 生命周期绑定在 prepare 前发生漂移")
        _validate_workspace_separation(
            self._dsh_state, self._dsh_launch, Path(workspace)
        )
        if self._started and self._client.cwd != workspace:
            raise AcpError("DSH 活跃 ACP host 的进程 cwd 与 workspace 不一致")
        try:
            # Stock DSH hot-reloads both later-wins user patch layers. Recheck
            # even when this adapter is reusing an active process/session so a
            # patch introduced between turns cannot reach the next prompt.
            _revalidate_launch(self._dsh_launch)
        except Exception:
            if self._started:
                await self._reset(graceful=False)
            raise
        if not self._started:
            _materialize_product_state(self._dsh_state)
            self._client.cwd = workspace
        return await super()._prepare_locked(workspace, resume_session_id)

    async def aclose(self) -> None:
        await super().aclose()
        self._dsh_workspace = None

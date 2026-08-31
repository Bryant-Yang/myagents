"""DSH official profile adapter contract; all runtimes are local fakes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acp.client import AcpError
from adapters.base import ExecutionMode
from agent_readiness import ReadinessState
from dsh_acp import AcpDshAdapter, dsh_readiness_probe
import dsh_acp.adapter as adapter_module

SERVER = ROOT / "tests/fake_acp_server.py"
_FAKE_PLUGIN_ENTRY = b"export {}\n"
_FAKE_PLUGIN_PATCH = b"[]\n"
_RUNTIME_CONTRACT = json.loads(
    (ROOT / "dsh_acp/plugin/runtime-contract.json").read_text(encoding="utf-8")
)
_PROFILE_BUNDLE_CONTRACT = _RUNTIME_CONTRACT["profileBundle"]
assert _PROFILE_BUNDLE_CONTRACT["entrySha256"] == (
    adapter_module._DSH_PLUGIN_ENTRY_SHA256
)
assert _PROFILE_BUNDLE_CONTRACT["patchSha256"] == (
    adapter_module._DSH_PLUGIN_PATCH_SHA256
)
# This standalone contract script uses tiny local artifacts. The release gate
# separately proves that the production build matches the pinned digests above.
adapter_module._DSH_PLUGIN_ENTRY_SHA256 = hashlib.sha256(
    _FAKE_PLUGIN_ENTRY
).hexdigest()
adapter_module._DSH_PLUGIN_PATCH_SHA256 = hashlib.sha256(
    _FAKE_PLUGIN_PATCH
).hexdigest()


class FakeResolver:
    def __init__(self, values: dict[str, str | None]) -> None:
        self.values = values
        self.calls: list[str] = []

    def __call__(self, command: str) -> str | None:
        self.calls.append(command)
        return self.values.get(command)


def _write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)
    return path


def _fake_profile(root: Path) -> tuple[Path, Path]:
    home = root / "dsh-home"
    profile = home / "profiles/myagents"
    plugin = profile / "node_modules/@myagents/dsh-acp-host"
    plugin.mkdir(parents=True)
    (profile / "package.json").write_text(json.dumps({
        "name": "dsh-profile-myagents",
        "private": True,
        "dependencies": {"@myagents/dsh-acp-host": "0.1.1"},
        "dsh": {"profile": {"bundles": [
            "@deepseek-ai/dsh-base",
            "@myagents/dsh-acp-host",
        ]}},
    }) + "\n", encoding="utf-8")
    (profile / "cordis.patch.yml").write_text(
        "# Stock profile user layer; it must remain semantically empty.\n[]\n",
        encoding="utf-8",
    )
    (plugin / "package.json").write_text(json.dumps({
        "name": "@myagents/dsh-acp-host",
        "version": "0.1.1",
        "main": "lib/index.js",
        "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
    }) + "\n", encoding="utf-8")
    (plugin / "cordis.patch.yml").write_bytes(_FAKE_PLUGIN_PATCH)
    (plugin / "lib").mkdir()
    (plugin / "lib/index.js").write_bytes(_FAKE_PLUGIN_ENTRY)
    (home / "settings.yaml").write_text("models: {}\n", encoding="utf-8")
    credentials = home / ".credentials.yaml"
    credentials.write_text("tokens: {}\n", encoding="utf-8")
    credentials.chmod(0o600)
    return home, plugin


def _fake_cli_body(marker: Path | None = None) -> str:
    touch = ""
    if marker is not None:
        touch = f"Path({str(marker)!r}).touch()\n"
    return (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        + touch
        + "profile = sys.argv[sys.argv.index('--profile') + 1]\n"
        "os.environ['FAKE_ACP_AGENT_NAME'] = 'dsh-myagents-acp'\n"
        "os.environ['FAKE_ACP_AGENT_VERSION'] = '0.1.1'\n"
        "os.environ['FAKE_ACP_AGENT_PROFILE'] = "
        "os.environ.get('DSH_ACP_PROFILE', '')\n"
        "os.environ['FAKE_ACP_AGENT_RUNTIME_VERSION'] = '0.1.2-alpha.2'\n"
        "os.environ.setdefault("
        "'FAKE_ACP_AGENT_COMPATIBILITY_REVISION', '2')\n"
        "os.environ.setdefault("
        "'FAKE_ACP_AGENT_POLICY_REVISION', '1')\n"
        "os.environ['FAKE_ACP_AGENT_READ_ONLY_TOOLS'] = "
        "'[\"read\",\"glob\",\"grep\"]'\n"
        "if os.environ.get('FAKE_DSH_ARGV'):\n"
        "  with open(os.environ['FAKE_DSH_ARGV'], 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if os.environ.get('FAKE_DSH_ENV'):\n"
        "  with open(os.environ['FAKE_DSH_ENV'], 'a') as f:\n"
        "    f.write(json.dumps({\n"
        "      'profile': profile,\n"
        "      'runtimeProfile': os.environ.get('DSH_ACP_PROFILE'),\n"
        "      'dshHome': os.environ.get('DSH_HOME'),\n"
        "      'persistence': os.environ.get('DSH_ACP_PERSISTENCE_DIR'),\n"
        "      'sourceRoot': os.environ.get('DSH_ACP_SOURCE_ROOT'),\n"
        "      'tsx': os.environ.get('TSX_TSCONFIG_PATH'),\n"
        "      'cwd': os.getcwd(),\n"
        "    }) + '\\n')\n"
        "os.execv(sys.executable, [sys.executable, "
        "os.environ['FAKE_DSH_SERVER']])\n"
    )


def _fake_install(root: Path, marker: Path | None = None) -> tuple[Path, Path]:
    package = root / "official-dsh"
    entry = _write_executable(
        package / "lib/bin.js", _fake_cli_body(marker)
    )
    (package / "package.json").write_text(json.dumps({
        "name": "@deepseek-ai/dsh",
        "version": "0.1.2-alpha.2",
        "bin": {"dsh": "lib/bin.js"},
    }) + "\n", encoding="utf-8")
    link = root / "bin/dsh"
    link.parent.mkdir(parents=True)
    link.symlink_to(entry)
    return link, entry


def _fake_source(root: Path) -> tuple[Path, Path, Path]:
    source = root / "deepseek-harness"
    cli_root = source / "apps/cli"
    entry = _write_executable(cli_root / "lib/bin.js", _fake_cli_body())
    source.mkdir(parents=True, exist_ok=True)
    (source / "package.json").write_text(json.dumps({
        "name": "@deepseek-ai/dsh-root",
        "version": "0.1.2-alpha.2",
    }) + "\n", encoding="utf-8")
    (cli_root / "package.json").write_text(json.dumps({
        "name": "@deepseek-ai/dsh",
        "version": "0.1.2-alpha.2",
        "bin": {"dsh": "lib/bin.js"},
    }) + "\n", encoding="utf-8")
    node = _write_executable(
        root / "bin/node",
        "#!/bin/sh\nentry=$1\nshift\nexec \"$entry\" \"$@\"\n",
    )
    return source, entry, node


def _env(root: Path, cli: Path, home: Path) -> dict[str, str]:
    return {
        "HOME": str(root / "home"),
        "DSH_HOME": str(home),
        "XDG_STATE_HOME": str(root / "state-home"),
        "MYAGENTS_DSH_CLI": str(cli),
        "FAKE_DSH_SERVER": str(SERVER),
        "FAKE_ACP_STATE": str(root / "wire.log"),
        "FAKE_DSH_ARGV": str(root / "argv.jsonl"),
        "FAKE_DSH_ENV": str(root / "env.jsonl"),
    }


def _workspace(root: Path, name: str = "workspace") -> str:
    path = root / name
    path.mkdir()
    return str(path.resolve())


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def test_readiness_is_passive_for_installed_and_source_entries() -> None:
    with tempfile.TemporaryDirectory(prefix="dsh-ready-") as raw:
        root = Path(raw)
        home, _plugin = _fake_profile(root)
        marker = root / "must-not-run"
        cli, entry = _fake_install(root, marker)
        env = _env(root, cli, home)

        installed = dsh_readiness_probe(
            environ=env, resolver=FakeResolver({})
        )
        assert installed.state is ReadinessState.READY
        assert installed.executable == str(entry.resolve())
        assert not marker.exists()
        assert not (root / "state-home").exists()

        path_env = {key: value for key, value in env.items()
                    if key != "MYAGENTS_DSH_CLI"}
        path_ready = dsh_readiness_probe(
            environ=path_env, resolver=FakeResolver({"dsh": str(cli)})
        )
        assert path_ready.state is ReadinessState.READY
        assert not marker.exists()

        source, source_entry, node = _fake_source(root)
        source_env = {**env, "MYAGENTS_DSH_CLI": "",
                      "MYAGENTS_DSH_SOURCE_ROOT": str(source)}
        resolver = FakeResolver({"node": str(node)})
        source_ready = dsh_readiness_probe(
            environ=source_env, resolver=resolver
        )
        assert source_ready.state is ReadinessState.READY
        assert source_ready.executable == str(source_entry.resolve())
        assert resolver.calls == ["node"]
        assert not marker.exists()

        missing = dsh_readiness_probe(
            environ={}, resolver=FakeResolver({})
        )
        assert missing.state is ReadinessState.NOT_FOUND
        assert "MYAGENTS_DSH_CLI" in missing.setup_hint


def test_readiness_rejects_version_profile_and_bundle_drift() -> None:
    with tempfile.TemporaryDirectory(prefix="dsh-drift-") as raw:
        root = Path(raw)
        home, plugin = _fake_profile(root)
        cli, _entry = _fake_install(root)
        env = _env(root, cli, home)

        cli_package = root / "official-dsh/package.json"
        cli_data = json.loads(cli_package.read_text(encoding="utf-8"))
        cli_data["version"] = "9.9.9"
        cli_package.write_text(json.dumps(cli_data), encoding="utf-8")
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "版本不兼容" in result.detail
        cli_data["version"] = "0.1.2-alpha.2"
        cli_package.write_text(json.dumps(cli_data), encoding="utf-8")

        profile_path = home / "profiles/myagents/package.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["dsh"]["profile"]["bundles"].append("untrusted")
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "精确等于" in result.detail
        profile["dsh"]["profile"]["bundles"].pop()
        profile_path.write_text(json.dumps(profile), encoding="utf-8")

        plugin_path = plugin / "package.json"
        bundle = json.loads(plugin_path.read_text(encoding="utf-8"))
        bundle["version"] = "0.2.0"
        plugin_path.write_text(json.dumps(bundle), encoding="utf-8")
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "bundle 版本不兼容" in result.detail

        bundle["version"] = "0.1.1"
        bundle["dsh"]["bundle"]["patch"] = "./other.yml"
        plugin_path.write_text(json.dumps(bundle), encoding="utf-8")
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "cordis.patch.yml" in result.detail

        bundle["dsh"]["bundle"]["patch"] = "./cordis.patch.yml"
        bundle["main"] = "src/index.ts"
        plugin_path.write_text(json.dumps(bundle), encoding="utf-8")
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "lib/index.js" in result.detail

        bundle["main"] = "lib/index.js"
        plugin_path.write_text(json.dumps(bundle), encoding="utf-8")
        entry_path = plugin / "lib/index.js"
        entry_path.write_text("malicious entry\n", encoding="utf-8")
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "checked-in bundle contract" in result.detail
        entry_path.write_bytes(_FAKE_PLUGIN_ENTRY)

        patch_path = plugin / "cordis.patch.yml"
        patch_path.write_text("- id: untrusted\n", encoding="utf-8")
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "checked-in bundle contract" in result.detail
        patch_path.write_bytes(_FAKE_PLUGIN_PATCH)

        external_entry = root / "external-index.js"
        external_entry.write_bytes(_FAKE_PLUGIN_ENTRY)
        entry_path.unlink()
        entry_path.symlink_to(external_entry)
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "无法安全打开" in result.detail


def test_readiness_rejects_effective_later_wins_user_patches() -> None:
    with tempfile.TemporaryDirectory(prefix="dsh-user-patch-") as raw:
        root = Path(raw)
        home, _plugin = _fake_profile(root)
        cli, _entry = _fake_install(root)
        env = _env(root, cli, home)
        profile_patch = home / "profiles/myagents/cordis.patch.yml"
        home_patch = home / "cordis.patch.yml"

        assert dsh_readiness_probe(
            environ=env, resolver=FakeResolver({})
        ).state is ReadinessState.READY

        profile_patch.write_text(
            "- id: approval\n  config:\n    policy: auto\n",
            encoding="utf-8",
        )
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "只允许 YAML 空数组 []" in result.detail

        profile_patch.write_text("# official empty layer\n[]\n", encoding="utf-8")
        home_patch.write_text(
            "- insert:\n    - id: subagent\n      name: untrusted\n",
            encoding="utf-8",
        )
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "home-level user patch" in result.detail

        home_patch.write_text("# comments without a YAML value\n", encoding="utf-8")
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "只允许 YAML 空数组 []" in result.detail

        home_patch.write_text("# semantically empty\n[]\n", encoding="utf-8")
        assert dsh_readiness_probe(
            environ=env, resolver=FakeResolver({})
        ).state is ReadinessState.READY

        external_patch = root / "external-empty.patch.yml"
        external_patch.write_text("[]\n", encoding="utf-8")
        profile_patch.unlink()
        profile_patch.symlink_to(external_patch)
        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "无法安全打开" in result.detail


def test_readiness_rejects_profile_symlink_into_workspace() -> None:
    with tempfile.TemporaryDirectory(prefix="dsh-profile-symlink-") as raw:
        root = Path(raw)
        home, _plugin = _fake_profile(root)
        cli, _entry = _fake_install(root)
        env = _env(root, cli, home)
        workspace = Path(_workspace(root))
        profile = home / "profiles/myagents"
        moved = workspace / "profile"
        shutil.move(profile, moved)
        profile.symlink_to(moved, target_is_directory=True)

        result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
        assert result.state is ReadinessState.INVALID
        assert "非 symlink 目录" in result.detail


def test_readiness_rejects_symlinked_profile_parent_and_manifest() -> None:
    for target in ("profiles", "manifest"):
        with tempfile.TemporaryDirectory(
            prefix=f"dsh-{target}-symlink-"
        ) as raw:
            root = Path(raw)
            home, _plugin = _fake_profile(root)
            cli, _entry = _fake_install(root)
            env = _env(root, cli, home)
            if target == "profiles":
                profiles = home / "profiles"
                moved = root / "external-profiles"
                shutil.move(profiles, moved)
                profiles.symlink_to(moved, target_is_directory=True)
            else:
                manifest = home / "profiles/myagents/package.json"
                moved = root / "external-package.json"
                shutil.move(manifest, moved)
                manifest.symlink_to(moved)

            result = dsh_readiness_probe(environ=env, resolver=FakeResolver({}))
            assert result.state is ReadinessState.INVALID
            if target == "profiles":
                assert "非 symlink 目录" in result.detail
            else:
                assert "无法安全打开" in result.detail


def test_spawn_revalidates_user_patch_after_readiness() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="dsh-patch-race-") as raw:
            root = Path(raw)
            home, _plugin = _fake_profile(root)
            marker = root / "must-not-spawn"
            cli, _entry = _fake_install(root, marker)
            env = _env(root, cli, home)
            workspace = _workspace(root)
            assert dsh_readiness_probe(
                environ=env, resolver=FakeResolver({})
            ).state is ReadinessState.READY
            with patch.dict(os.environ, env, clear=True):
                adapter = AcpDshAdapter()
                (home / "profiles/myagents/cordis.patch.yml").write_text(
                    "- id: approval\n  config:\n    policy: auto\n",
                    encoding="utf-8",
                )
                try:
                    try:
                        async for _event in adapter.stream("blocked", workspace):
                            pass
                    except AcpError as exc:
                        assert "只允许 YAML 空数组 []" in str(exc)
                    else:
                        raise AssertionError("late user patch must block spawn")
                finally:
                    await adapter.aclose()
            assert not marker.exists()
            assert not _lines(root / "wire.log")

    asyncio.run(run())


def test_spawn_revalidates_bundle_artifact_after_readiness() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="dsh-bundle-race-") as raw:
            root = Path(raw)
            home, plugin = _fake_profile(root)
            marker = root / "must-not-spawn"
            cli, _entry = _fake_install(root, marker)
            env = _env(root, cli, home)
            workspace = _workspace(root)
            assert dsh_readiness_probe(
                environ=env, resolver=FakeResolver({})
            ).state is ReadinessState.READY
            with patch.dict(os.environ, env, clear=True):
                adapter = AcpDshAdapter()
                (plugin / "lib/index.js").write_text(
                    "malicious after readiness\n",
                    encoding="utf-8",
                )
                try:
                    try:
                        async for _event in adapter.stream("blocked", workspace):
                            pass
                    except AcpError as exc:
                        assert "checked-in bundle contract" in str(exc)
                    else:
                        raise AssertionError("late bundle drift must block spawn")
                finally:
                    await adapter.aclose()
            assert not marker.exists()
            assert not _lines(root / "wire.log")

    asyncio.run(run())


def test_active_session_revalidates_hot_user_patch_before_next_prompt() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="dsh-hot-patch-") as raw:
            root = Path(raw)
            home, _plugin = _fake_profile(root)
            cli, _entry = _fake_install(root)
            env = _env(root, cli, home)
            workspace = _workspace(root)
            with patch.dict(os.environ, env, clear=True):
                adapter = AcpDshAdapter()
                try:
                    first = [event async for event in adapter.stream(
                        "first", workspace,
                    )]
                    assert any(event.kind == "done" for event in first)
                    (home / "cordis.patch.yml").write_text(
                        "- insert:\n    - id: subagent\n      name: untrusted\n",
                        encoding="utf-8",
                    )
                    try:
                        async for _event in adapter.stream("second", workspace):
                            pass
                    except AcpError as exc:
                        assert "只允许 YAML 空数组 []" in str(exc)
                    else:
                        raise AssertionError("hot user patch must block next prompt")
                    assert adapter._started is False
                    assert adapter.session_id is None
                finally:
                    await adapter.aclose()
            prompts = [
                line for line in _lines(root / "wire.log")
                if line.startswith("prompt:")
            ]
            assert len(prompts) == 1

    asyncio.run(run())


def test_active_session_revalidates_hot_bundle_before_next_prompt() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="dsh-hot-bundle-") as raw:
            root = Path(raw)
            home, plugin = _fake_profile(root)
            cli, _entry = _fake_install(root)
            env = _env(root, cli, home)
            workspace = _workspace(root)
            with patch.dict(os.environ, env, clear=True):
                adapter = AcpDshAdapter()
                try:
                    first = [event async for event in adapter.stream(
                        "first", workspace,
                    )]
                    assert any(event.kind == "done" for event in first)
                    (plugin / "cordis.patch.yml").write_text(
                        "- id: untrusted\n",
                        encoding="utf-8",
                    )
                    try:
                        async for _event in adapter.stream("second", workspace):
                            pass
                    except AcpError as exc:
                        assert "checked-in bundle contract" in str(exc)
                    else:
                        raise AssertionError("hot bundle drift must block next prompt")
                    assert adapter._started is False
                    assert adapter.session_id is None
                finally:
                    await adapter.aclose()
            prompts = [
                line for line in _lines(root / "wire.log")
                if line.startswith("prompt:")
            ]
            assert len(prompts) == 1

    asyncio.run(run())


def test_source_gate_uses_only_official_built_cli_and_versions() -> None:
    with tempfile.TemporaryDirectory(prefix="dsh-source-") as raw:
        root = Path(raw)
        home, _plugin = _fake_profile(root)
        source, entry, node = _fake_source(root)
        env = {
            "DSH_HOME": str(home),
            "MYAGENTS_DSH_SOURCE_ROOT": str(source),
        }
        resolver = FakeResolver({"node": str(node)})
        assert dsh_readiness_probe(
            environ=env, resolver=resolver
        ).state is ReadinessState.READY

        unrelated = source / "packages/unrelated/src/index.ts"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("changed\n", encoding="utf-8")
        assert dsh_readiness_probe(
            environ=env, resolver=resolver
        ).state is ReadinessState.READY

        entry.unlink()
        failed = dsh_readiness_probe(environ=env, resolver=resolver)
        assert failed.state is ReadinessState.INVALID
        assert "bin.dsh" in failed.detail or "built CLI" in failed.detail


def test_adapter_commands_and_environment_follow_official_profile() -> None:
    with tempfile.TemporaryDirectory(prefix="dsh-command-") as raw:
        root = Path(raw)
        home, _plugin = _fake_profile(root)
        cli, entry = _fake_install(root)
        env = _env(root, cli, home)
        env.update({
            "DSH_ACP_SOURCE_ROOT": "/legacy/source",
            "TSX_TSCONFIG_PATH": "/legacy/tsx",
        })
        with patch.dict(os.environ, env, clear=True):
            adapter = AcpDshAdapter()
        assert adapter._cmd == [str(entry.resolve()), "--profile", "myagents"]
        assert adapter._fallback is None
        assert adapter._env_overrides["DSH_HOME"] == str(home.resolve())
        assert adapter._env_overrides["DSH_ACP_PROFILE"] == "workspace-write"
        assert adapter._execution_env_overrides[ExecutionMode.READ_ONLY] == {
            "DSH_ACP_PROFILE": "read-only"
        }
        assert "DSH_ACP_SOURCE_ROOT" not in adapter._env_overrides
        assert "TSX_TSCONFIG_PATH" not in adapter._env_overrides
        assert {
            "DSH_ACP_SOURCE_ROOT", "TSX_TSCONFIG_PATH", "NODE_OPTIONS",
        }.issubset(adapter._env_removals)
        assert not (root / "state-home").exists()

        source, source_entry, node = _fake_source(root)
        source_env = {
            **env,
            "MYAGENTS_DSH_CLI": "",
            "MYAGENTS_DSH_SOURCE_ROOT": str(source),
        }
        with (
            patch.dict(os.environ, source_env, clear=True),
            patch("dsh_acp.adapter.shutil.which", return_value=str(node)),
        ):
            source_adapter = AcpDshAdapter()
        assert source_adapter._cmd == [
            str(node.resolve()), str(source_entry.resolve()),
            "--profile", "myagents",
        ]
        assert all("tsx" not in item for item in source_adapter._cmd)
        assert str(ROOT / "dsh_acp/plugin/src/bin.ts") not in source_adapter._cmd


def test_runtime_keeps_workspace_cwd_and_rebuilds_on_policy_change() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="dsh-runtime-") as raw:
            root = Path(raw)
            home, _plugin = _fake_profile(root)
            cli, _entry = _fake_install(root)
            env = _env(root, cli, home)
            workspace = _workspace(root)
            with patch.dict(os.environ, env, clear=True):
                adapter = AcpDshAdapter()
                try:
                    first = [event async for event in adapter.stream(
                        "first", workspace,
                    )]
                    second = [event async for event in adapter.stream(
                        "review", workspace,
                        execution_mode=ExecutionMode.READ_ONLY,
                    )]
                finally:
                    await adapter.aclose()
            assert any(event.kind == "done" for event in first)
            assert any(event.kind == "done" for event in second)
            argv = [json.loads(line) for line in _lines(root / "argv.jsonl")]
            assert argv == [
                ["--profile", "myagents"],
                ["--profile", "myagents"],
            ]
            child_env = [json.loads(line) for line in _lines(root / "env.jsonl")]
            assert [item["runtimeProfile"] for item in child_env] == [
                "workspace-write", "read-only",
            ]
            assert {item["cwd"] for item in child_env} == {workspace}
            assert {item["dshHome"] for item in child_env} == {str(home.resolve())}
            assert all(item["sourceRoot"] is None for item in child_env)
            assert all(item["tsx"] is None for item in child_env)
            wire = _lines(root / "wire.log")
            assert len([line for line in wire if line.startswith("new:")]) == 2

    asyncio.run(run())


def test_default_deny_and_one_shot_permission_contract() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="dsh-permission-") as raw:
            root = Path(raw)
            home, _plugin = _fake_profile(root)
            cli, _entry = _fake_install(root)
            env = _env(root, cli, home)
            workspace = _workspace(root)
            with patch.dict(os.environ, env, clear=True):
                adapter = AcpDshAdapter()
                try:
                    events = [event async for event in adapter.stream(
                        "perm write", workspace,
                    )]
                finally:
                    await adapter.aclose()
            assert any(event.kind == "done" for event in events)
            assert any(
                '"outcome": "cancelled"' in line
                for line in _lines(root / "wire.log")
                if line.startswith("permission:")
            )

            decisions: list[str] = []

            async def allow(agent: str, params: dict) -> dict:
                decisions.append(agent)
                return {"outcome": {"outcome": "selected", "optionId": "allow"}}

            env["FAKE_ACP_STATE"] = str(root / "wire-allow.log")
            with patch.dict(os.environ, env, clear=True):
                adapter = AcpDshAdapter()
                adapter.set_permission_handler(allow)
                try:
                    events = [event async for event in adapter.stream(
                        "perm allow", workspace,
                    )]
                finally:
                    await adapter.aclose()
            assert decisions == ["dsh"]
            assert any(event.kind == "done" for event in events)

    asyncio.run(run())


def test_lifecycle_and_identity_fail_closed_without_fallback() -> None:
    async def run_case(
        override: str,
        expected: str,
        *,
        value: str | None = None,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh-gate-") as raw:
            root = Path(raw)
            home, _plugin = _fake_profile(root)
            cli, _entry = _fake_install(root)
            env = _env(root, cli, home)
            env[override] = (
                value if value is not None
                else "1" if override.startswith("FAKE_ACP_NO_") else "wrong"
            )
            if override == "FAKE_DSH_AGENT_NAME":
                # The wrapper deliberately fixes identity; use fake-server env
                # directly by asking wrapper to preserve this one override.
                pass
            workspace = _workspace(root)
            with patch.dict(os.environ, env, clear=True):
                adapter = AcpDshAdapter()
                assert adapter._fallback is None
                try:
                    try:
                        async for _event in adapter.stream("blocked", workspace):
                            pass
                    except AcpError as exc:
                        assert expected in str(exc)
                    else:
                        raise AssertionError("missing capability must fail closed")
                finally:
                    await adapter.aclose()
            assert not any(
                line.startswith(("new:", "prompt:"))
                for line in _lines(root / "wire.log")
            )

    asyncio.run(run_case("FAKE_ACP_NO_LOAD_CAP", "loadSession"))
    asyncio.run(run_case("FAKE_ACP_NO_CLOSE_CAP", "sessionCapabilities.close"))
    asyncio.run(run_case(
        "FAKE_ACP_AGENT_POLICY_REVISION",
        "policy-revision",
        value="true",
    ))
    asyncio.run(run_case(
        "FAKE_ACP_AGENT_COMPATIBILITY_REVISION",
        "compatibility-revision",
        value="true",
    ))


def test_product_state_is_isolated_and_user_config_is_not_modified() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="dsh-state-") as raw:
            root = Path(raw)
            home, _plugin = _fake_profile(root)
            cli, _entry = _fake_install(root)
            env = _env(root, cli, home)
            persistence = root / "private-state"
            env["DSH_ACP_PERSISTENCE_DIR"] = str(persistence)
            settings = home / "settings.yaml"
            credentials = home / ".credentials.yaml"
            originals = {
                path: (path.read_bytes(), path.stat())
                for path in (settings, credentials)
            }
            with patch.dict(os.environ, env, clear=True):
                adapter = AcpDshAdapter()
                try:
                    async for _event in adapter.stream(
                        "state", _workspace(root)
                    ):
                        pass
                finally:
                    await adapter.aclose()
            copies = (
                persistence / "config-inputs/settings.yaml",
                persistence / "config-inputs/.credentials.yaml",
            )
            for original, copied in zip((settings, credentials), copies):
                payload, before = originals[original]
                after = original.stat()
                assert original.read_bytes() == copied.read_bytes() == payload
                assert (before.st_ino, before.st_mtime_ns) == (
                    after.st_ino, after.st_mtime_ns
                )
                assert stat.S_IMODE(copied.stat().st_mode) == 0o600
                assert copied.stat().st_ino != original.stat().st_ino

            overlapping = dsh_readiness_probe(
                environ={**env, "DSH_ACP_PERSISTENCE_DIR": str(home)},
                resolver=FakeResolver({}),
            )
            # Readiness is intentionally limited to CLI/profile/version. The
            # runtime adapter owns topology validation before any process spawn.
            assert overlapping.state is ReadinessState.READY
            with patch.dict(
                os.environ,
                {**env, "DSH_ACP_PERSISTENCE_DIR": str(home)},
                clear=True,
            ):
                blocked = AcpDshAdapter()
                assert blocked._configuration_error is not None
                assert "重叠" in blocked._configuration_error

    asyncio.run(run())


if __name__ == "__main__":
    test_readiness_is_passive_for_installed_and_source_entries()
    test_readiness_rejects_version_profile_and_bundle_drift()
    test_readiness_rejects_effective_later_wins_user_patches()
    test_readiness_rejects_profile_symlink_into_workspace()
    test_readiness_rejects_symlinked_profile_parent_and_manifest()
    test_spawn_revalidates_user_patch_after_readiness()
    test_spawn_revalidates_bundle_artifact_after_readiness()
    test_active_session_revalidates_hot_user_patch_before_next_prompt()
    test_active_session_revalidates_hot_bundle_before_next_prompt()
    test_source_gate_uses_only_official_built_cli_and_versions()
    test_adapter_commands_and_environment_follow_official_profile()
    test_runtime_keeps_workspace_cwd_and_rebuilds_on_policy_change()
    test_default_deny_and_one_shot_permission_contract()
    test_lifecycle_and_identity_fail_closed_without_fallback()
    test_product_state_is_isolated_and_user_config_is_not_modified()
    print("ok  DSH official profile adapter/readiness contract")

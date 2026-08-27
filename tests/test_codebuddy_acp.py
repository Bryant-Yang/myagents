"""CodeBuddy ACP-only 生产接入契约。

运行：.venv/bin/python tests/test_codebuddy_acp.py
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acp.adapter import (
    AcpCodeBuddyAdapter,
    _open_browser,
    _resolve_codebuddy_cli,
)
from acp.client import AcpError, AcpRemoteError
from adapters.base import ExecutionMode
from orchestrator import AGENTS, Orchestrator

SERVER = Path(__file__).resolve().parent / "fake_acp_server.py"


def test_codebuddy_is_registered_as_acp_only() -> None:
    spec = AGENTS["codebuddy"]
    assert spec.transport == "acp"

    orch = Orchestrator("/tmp", persistent=False)
    adapter = orch.adapters["codebuddy"]
    assert isinstance(adapter, AcpCodeBuddyAdapter)
    assert adapter.name == "codebuddy"
    assert adapter._fallback is None


def test_codebuddy_runtime_profiles_are_isolated() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="myagents-codebuddy-fake-") as raw:
            root = Path(raw)
            wrapper = root / "codebuddy"
            argv_log = root / "argv.jsonl"
            state = root / "state.log"
            wrapper.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['FAKE_CODEBUDDY_ARGV'], 'a') as f:\n"
                "    f.write(json.dumps({\n"
                "        'argv': sys.argv[1:],\n"
                "        'internet_environment': os.environ.get(\n"
                "            'CODEBUDDY_INTERNET_ENVIRONMENT'),\n"
                "    }) + '\\n')\n"
                "os.execv(sys.executable, [sys.executable, "
                "os.environ['FAKE_CODEBUDDY_SERVER']])\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
            env = {
                "MYAGENTS_CODEBUDDY_CLI": str(wrapper),
                "FAKE_CODEBUDDY_ARGV": str(argv_log),
                "FAKE_CODEBUDDY_SERVER": str(SERVER),
                "FAKE_ACP_STATE": str(state),
                "FAKE_ACP_REQUIRE_AUTH": "1",
                "FAKE_ACP_AUTH_AT_NEW": "1",
                "FAKE_ACP_AUTH_URL": "1",
            }
            opened_urls: list[str] = []

            async def open_url(url: str) -> bool:
                opened_urls.append(url)
                return True

            with patch.dict(os.environ, env, clear=False):
                adapter = AcpCodeBuddyAdapter(
                    auth_url_opener=open_url,
                )
                try:
                    default_events = [
                        event async for event in adapter.stream("hello", raw)
                    ]
                    readonly_events = [
                        event async for event in adapter.stream(
                            "readonly", raw,
                            execution_mode=ExecutionMode.READ_ONLY,
                        )
                    ]
                finally:
                    await adapter.aclose()

            assert "".join(
                event.text for event in default_events if event.kind == "text"
            ) == "PONG"
            assert "".join(
                event.text for event in readonly_events if event.kind == "text"
            ) == "PONG"
            invocation_records = [
                json.loads(line)
                for line in argv_log.read_text(encoding="utf-8").splitlines()
            ]
            assert [item["argv"] for item in invocation_records] == [
                [
                    "--acp", "--acp-transport", "stdio",
                    "--permission-mode", "default",
                    "--subagent-permission-mode", "dontAsk",
                    "--strict-mcp-config", "--mcp-config",
                    '{"mcpServers":{}}', "--setting-sources", "",
                ],
                [
                    "--acp", "--acp-transport", "stdio",
                    "--permission-mode", "dontAsk",
                    "--subagent-permission-mode", "dontAsk",
                    "--tools", "Read,Glob,Grep",
                    "--strict-mcp-config", "--mcp-config",
                    '{"mcpServers":{}}', "--setting-sources", "",
                ],
            ]
            assert {
                item["internet_environment"] for item in invocation_records
            } == {"internal"}
            assert sum(
                line.startswith("new:")
                for line in state.read_text(encoding="utf-8").splitlines()
            ) == 4
            assert state.read_text(encoding="utf-8").splitlines().count(
                "authenticate:internal"
            ) == 2
            assert opened_urls == [
                "https://copilot.tencent.com/fake-auth",
                "https://copilot.tencent.com/fake-auth",
            ]

    asyncio.run(run())


def test_codebuddy_uses_product_name_in_tui() -> None:
    from main import ChatApp

    rendered = ChatApp._line("codebuddy", "收到")
    assert rendered.spans[0].style == "bold bright_magenta"

    orch = Orchestrator("/tmp", persistent=False)
    assert any(
        spec.name == "codebuddy" and spec.transport == "acp"
        for spec in orch.specs
    )
    assert "workbuddy" not in {spec.name for spec in orch.specs}
    assert "cbc" not in {spec.name for spec in orch.specs}


def test_codebuddy_authentication_is_bounded_and_fail_closed() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="myagents-codebuddy-auth-") as raw:
            root = Path(raw)
            wrapper = root / "codebuddy"
            state = root / "state.log"
            wrapper.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "os.execv(sys.executable, [sys.executable, "
                "os.environ['FAKE_CODEBUDDY_SERVER']])\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
            env = {
                "MYAGENTS_CODEBUDDY_CLI": str(wrapper),
                "FAKE_CODEBUDDY_SERVER": str(SERVER),
                "FAKE_ACP_STATE": str(state),
                "FAKE_ACP_REQUIRE_AUTH": "1",
                "FAKE_ACP_AUTH_AT_NEW": "1",
                "FAKE_ACP_AUTH_URL": "1",
                "FAKE_ACP_AUTH_HANG": "1",
            }
            opened_urls: list[str] = []

            async def open_url(url: str) -> bool:
                opened_urls.append(url)
                return True

            with patch.dict(os.environ, env, clear=False):
                adapter = AcpCodeBuddyAdapter(
                    auth_timeout=0.1,
                    auth_url_opener=open_url,
                )
                try:
                    try:
                        async for _event in adapter.stream("hello", raw):
                            pass
                    except AcpError as exc:
                        assert "认证" in str(exc)
                        assert "0.1" in str(exc)
                    else:
                        raise AssertionError("挂起的认证必须有限超时并失败")
                    assert adapter.session_id is None
                    assert adapter._started is False
                    assert adapter._client._proc is None or (
                        adapter._client._proc.returncode is not None
                    )
                finally:
                    await adapter.aclose()
            assert opened_urls == [
                "https://copilot.tencent.com/fake-auth",
            ]

    asyncio.run(run())


def test_codebuddy_rejects_untrusted_authentication_url() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="myagents-codebuddy-url-") as raw:
            root = Path(raw)
            wrapper = root / "codebuddy"
            state = root / "state.log"
            wrapper.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "os.execv(sys.executable, [sys.executable, "
                "os.environ['FAKE_CODEBUDDY_SERVER']])\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
            env = {
                "MYAGENTS_CODEBUDDY_CLI": str(wrapper),
                "FAKE_CODEBUDDY_SERVER": str(SERVER),
                "FAKE_ACP_STATE": str(state),
                "FAKE_ACP_REQUIRE_AUTH": "1",
                "FAKE_ACP_AUTH_AT_NEW": "1",
                "FAKE_ACP_AUTH_URL": "1",
                "FAKE_ACP_AUTH_URL_VALUE": "http://example.test/phishing",
            }
            opened_urls: list[str] = []

            async def open_url(url: str) -> bool:
                opened_urls.append(url)
                return True

            with patch.dict(os.environ, env, clear=False):
                adapter = AcpCodeBuddyAdapter(
                    auth_url_opener=open_url,
                )
                try:
                    try:
                        async for _event in adapter.stream("hello", raw):
                            pass
                    except AcpError as exc:
                        assert "不可信" in str(exc)
                    else:
                        raise AssertionError("非 HTTPS 官方认证地址必须拒绝")
                finally:
                    await adapter.aclose()
            assert opened_urls == []

    asyncio.run(run())


def test_codebuddy_auth_timeout_can_cancel_slow_browser_opener() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="myagents-codebuddy-slow-open-") as raw:
            root = Path(raw)
            wrapper = root / "codebuddy"
            state = root / "state.log"
            wrapper.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "os.execv(sys.executable, [sys.executable, "
                "os.environ['FAKE_CODEBUDDY_SERVER']])\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
            env = {
                "MYAGENTS_CODEBUDDY_CLI": str(wrapper),
                "FAKE_CODEBUDDY_SERVER": str(SERVER),
                "FAKE_ACP_STATE": str(state),
                "FAKE_ACP_REQUIRE_AUTH": "1",
                "FAKE_ACP_AUTH_AT_NEW": "1",
                "FAKE_ACP_AUTH_URL": "1",
                "FAKE_ACP_AUTH_HANG": "1",
            }
            opener_cancelled = asyncio.Event()

            async def slow_opener(_url: str) -> bool:
                try:
                    await asyncio.sleep(60)
                finally:
                    opener_cancelled.set()
                return True

            started = time.monotonic()
            with patch.dict(os.environ, env, clear=False):
                adapter = AcpCodeBuddyAdapter(
                    auth_timeout=0.05,
                    auth_url_opener=slow_opener,
                )
                try:
                    try:
                        async for _event in adapter.stream("hello", raw):
                            pass
                    except AcpError as exc:
                        assert "认证" in str(exc)
                    else:
                        raise AssertionError("慢浏览器 opener 必须可被认证超时抢占")
                finally:
                    await adapter.aclose()
            assert time.monotonic() - started < 0.5
            assert opener_cancelled.is_set()

    asyncio.run(run())


def test_codebuddy_rejects_blocking_browser_opener() -> None:
    try:
        AcpCodeBuddyAdapter(
            auth_url_opener=lambda _url: True,  # type: ignore[arg-type]
        )
    except TypeError as exc:
        assert "async callable" in str(exc)
    else:
        raise AssertionError("同步 opener 会阻塞 read loop，必须在启动前拒绝")


def test_codebuddy_reuses_existing_login_without_reauthentication() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="myagents-codebuddy-preauthenticated-",
        ) as raw:
            root = Path(raw)
            wrapper = root / "codebuddy"
            state = root / "state.log"
            wrapper.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "os.execv(sys.executable, [sys.executable, "
                "os.environ['FAKE_CODEBUDDY_SERVER']])\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
            env = {
                "MYAGENTS_CODEBUDDY_CLI": str(wrapper),
                "FAKE_CODEBUDDY_SERVER": str(SERVER),
                "FAKE_ACP_STATE": str(state),
                "FAKE_ACP_REQUIRE_AUTH": "1",
                "FAKE_ACP_AUTH_AT_NEW": "1",
                "FAKE_ACP_PREAUTHENTICATED": "1",
            }
            opened_urls: list[str] = []

            async def open_url(url: str) -> bool:
                opened_urls.append(url)
                return True

            with patch.dict(os.environ, env, clear=False):
                adapter = AcpCodeBuddyAdapter(auth_url_opener=open_url)
                try:
                    events = [
                        event async for event in adapter.stream("hello", raw)
                    ]
                finally:
                    await adapter.aclose()

            assert "".join(
                event.text for event in events if event.kind == "text"
            ) == "PONG"
            protocol = state.read_text(encoding="utf-8").splitlines()
            assert not any(
                line.startswith("authenticate:") for line in protocol
            )
            assert opened_urls == []

    asyncio.run(run())


def test_codebuddy_never_falls_back_to_app_private_cli() -> None:
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("acp.adapter.shutil.which", return_value=None),
    ):
        assert _resolve_codebuddy_cli() == "codebuddy"


def test_codebuddy_rejects_app_private_cli_from_env_and_path_symlink() -> None:
    with tempfile.TemporaryDirectory(
        prefix="myagents-codebuddy-private-cli-",
    ) as raw:
        root = Path(raw)
        private_cli = (
            root / "WorkBuddy.app" / "Contents" / "Resources" / "codebuddy"
        )
        private_cli.parent.mkdir(parents=True)
        private_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        private_cli.chmod(0o700)
        outside_link = root / "codebuddy"
        outside_link.symlink_to(private_cli)

        with patch.dict(
            os.environ,
            {"MYAGENTS_CODEBUDDY_CLI": str(private_cli)},
            clear=False,
        ):
            try:
                _resolve_codebuddy_cli()
            except AcpError as exc:
                assert "App 包内私有 CLI" in str(exc)
            else:
                raise AssertionError("显式路径不得指向 App 包内私有 CLI")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("acp.adapter.shutil.which", return_value=str(outside_link)),
        ):
            try:
                _resolve_codebuddy_cli()
            except AcpError as exc:
                assert "App 包内私有 CLI" in str(exc)
            else:
                raise AssertionError("PATH symlink 不得绕过私有 CLI 禁令")


def test_codebuddy_authentication_required_match_is_exact() -> None:
    exact = AcpRemoteError(-32000, "Authentication required")
    assert AcpCodeBuddyAdapter._is_authentication_required(exact) is True

    near_misses = (
        AcpRemoteError(-32000, "authentication required"),
        AcpRemoteError(-32000, "Authentication_required"),
        AcpRemoteError(-32000, " Authentication required "),
        AcpRemoteError("-32000", "Authentication required"),
        AcpRemoteError(-32000.0, "Authentication required"),
        AcpRemoteError(-32001, "Authentication required"),
    )
    assert not any(
        AcpCodeBuddyAdapter._is_authentication_required(error)
        for error in near_misses
    )


def test_browser_launcher_cancellation_reaps_process_group() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="myagents-codebuddy-launcher-",
        ) as raw:
            root = Path(raw)
            launcher = root / "xdg-open"
            child_pid_file = root / "child.pid"
            child_ready_file = root / "child.ready"
            child_code = (
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"open({str(child_ready_file)!r}, 'w').close()\n"
                "time.sleep(60)\n"
            )
            launcher.write_text(
                "#!/usr/bin/env python3\n"
                "import os, subprocess, sys, time\n"
                f"child_code = {child_code!r}\n"
                "child = subprocess.Popen([sys.executable, '-c', child_code])\n"
                f"while not os.path.exists({str(child_ready_file)!r}):\n"
                "    time.sleep(0.01)\n"
                f"open({str(child_pid_file)!r}, 'w').write(str(child.pid))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            launcher.chmod(0o700)

            with (
                patch("acp.adapter.Path.is_file", return_value=False),
                patch("acp.adapter.shutil.which", return_value=str(launcher)),
            ):
                task = asyncio.create_task(
                    _open_browser("https://copilot.tencent.com/fake-auth")
                )
                for _ in range(100):
                    if child_pid_file.exists():
                        break
                    await asyncio.sleep(0.01)
                assert child_pid_file.exists(), "fake launcher 未创建 descendant"
                child_pid = int(child_pid_file.read_text(encoding="utf-8"))
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("launcher 任务取消必须向上传播")

            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    return
                await asyncio.sleep(0.01)
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
            raise AssertionError(
                "launcher leader 退出后仍必须回收同进程组 descendant"
            )

    import contextlib

    asyncio.run(run())


def test_codebuddy_requires_advertised_authentication_method() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="myagents-codebuddy-no-auth-") as raw:
            root = Path(raw)
            wrapper = root / "codebuddy"
            state = root / "state.log"
            wrapper.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "os.execv(sys.executable, [sys.executable, "
                "os.environ['FAKE_CODEBUDDY_SERVER']])\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
            env = {
                "MYAGENTS_CODEBUDDY_CLI": str(wrapper),
                "FAKE_CODEBUDDY_SERVER": str(SERVER),
                "FAKE_ACP_STATE": str(state),
                "FAKE_ACP_AUTH_AT_NEW": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                adapter = AcpCodeBuddyAdapter()
                try:
                    try:
                        async for _event in adapter.stream("hello", raw):
                            pass
                    except AcpError as exc:
                        assert "未公布认证方式" in str(exc)
                    else:
                        raise AssertionError("CodeBuddy 不得跳过空 authMethods")
                finally:
                    await adapter.aclose()
            events = (
                state.read_text(encoding="utf-8").splitlines()
                if state.exists() else []
            )
            assert sum(line.startswith("new:") for line in events) == 1
            assert not any(line.startswith("authenticate:") for line in events)
            assert not any(line.startswith("prompt:") for line in events)

    asyncio.run(run())


def test_codebuddy_rejects_unadvertised_authentication_method() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="myagents-codebuddy-method-") as raw:
            root = Path(raw)
            wrapper = root / "codebuddy"
            state = root / "state.log"
            wrapper.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "os.execv(sys.executable, [sys.executable, "
                "os.environ['FAKE_CODEBUDDY_SERVER']])\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
            env = {
                "MYAGENTS_CODEBUDDY_CLI": str(wrapper),
                "MYAGENTS_CODEBUDDY_AUTH_METHOD": "invented",
                "FAKE_CODEBUDDY_SERVER": str(SERVER),
                "FAKE_ACP_STATE": str(state),
                "FAKE_ACP_REQUIRE_AUTH": "1",
                "FAKE_ACP_AUTH_AT_NEW": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                adapter = AcpCodeBuddyAdapter()
                try:
                    try:
                        async for _event in adapter.stream("hello", raw):
                            pass
                    except AcpError as exc:
                        assert "未公布认证方式" in str(exc)
                        assert "invented" in str(exc)
                    else:
                        raise AssertionError("未公布的认证 method 必须拒绝")
                finally:
                    await adapter.aclose()
            events = (
                state.read_text(encoding="utf-8").splitlines()
                if state.exists() else []
            )
            assert not any(line.startswith("authenticate:") for line in events)
            assert sum(line.startswith("new:") for line in events) == 1
            assert not any(line.startswith("prompt:") for line in events)

    asyncio.run(run())


if __name__ == "__main__":
    test_codebuddy_is_registered_as_acp_only()
    test_codebuddy_runtime_profiles_are_isolated()
    test_codebuddy_uses_product_name_in_tui()
    test_codebuddy_authentication_is_bounded_and_fail_closed()
    test_codebuddy_rejects_untrusted_authentication_url()
    test_codebuddy_auth_timeout_can_cancel_slow_browser_opener()
    test_codebuddy_rejects_blocking_browser_opener()
    test_codebuddy_reuses_existing_login_without_reauthentication()
    test_codebuddy_never_falls_back_to_app_private_cli()
    test_codebuddy_rejects_app_private_cli_from_env_and_path_symlink()
    test_codebuddy_authentication_required_match_is_exact()
    test_browser_launcher_cancellation_reaps_process_group()
    test_codebuddy_requires_advertised_authentication_method()
    test_codebuddy_rejects_unadvertised_authentication_method()
    print("\nCodeBuddy ACP 接入契约测试全部通过")

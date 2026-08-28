"""Phase 2 里程碑测试：通用 ACP runtime 接入统一 TUI。

覆盖：
- AgentSpec 注册：workers 保持各自协议，host 使用原生无工具模型 runtime
- ACP 增量上下文：不重复完整 transcript、跳过自己回复、失败不丢增量
- 权限：默认拒绝 / TUI 选择 / 等待可取消
- 生命周期：TUI 退出统一 aclose，fake ACP 无残留
- 可见状态：启动行显示各 agent 传输协议；session id 只展示一次

运行：.venv/bin/python tests/test_phase2.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acp.adapter import (
    QWEN_ACP_DEFAULT_CMD,
    QWEN_ACP_READ_ONLY_CMD,
    AcpAdapter,
    AcpOpenCodeAdapter,
    AcpQwenAdapter,
    AcpCodeBuddyAdapter,
)
from acp.client import AcpClient, AcpError
from adapters.base import AgentEvent, ExecutionMode
from adapters.kimi_adapter import KimiAdapter
from adapters.opencode_adapter import OpenCodeAdapter
from codex_app_server.adapter import CodexAppServerAdapter
from dsh_acp import AcpDshAdapter
from host import HostDecision
from native_agent import NativeAgentRuntime
from orchestrator import AGENTS, Orchestrator

SERVER = str(Path(__file__).parent / "fake_acp_server.py")
STATE = "/tmp/myagents_fake_acp_state_phase2"
os.environ["FAKE_ACP_STATE"] = STATE


def state_events() -> list[str]:
    if not os.path.exists(STATE):
        return []
    with open(STATE) as f:
        return f.read().splitlines()


def reset_state() -> None:
    if os.path.exists(STATE):
        os.remove(STATE)


# ---- 假 agent ----

class FakeJsonl:
    """无状态假 agent（等价 JSONL adapter）：记录最后一次 prompt。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.session_id = None
        self.last_prompt: str | None = None

    async def stream(self, prompt: str, workdir: str):
        self.last_prompt = prompt
        yield AgentEvent("text", f"{self.name} 回复")
        yield AgentEvent("done")


class StatefulFake:
    """有状态假 agent（等价 ACP adapter）：记录每一次 prompt，可注入失败。"""

    stateful_session = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.session_id = "fake-session"
        self.prompts: list[str] = []
        self.fail_next = False

    async def stream(self, prompt: str, workdir: str):
        self.prompts.append(prompt)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("模拟派发失败")
        yield AgentEvent("text", f"{self.name} 回复{len(self.prompts)}")
        yield AgentEvent("done")


class ClosableFake(FakeJsonl):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeHost(FakeJsonl):
    def __init__(self) -> None:
        super().__init__("host")
        self.route = HostDecision(["kimi"], "测试路由")

    async def decide(
        self, transcript: str, workdir: str, on_event=None, *, choices=None,
    ):
        return self.route


def make_orch(**adapters) -> Orchestrator:
    """真实 Orchestrator + 假 adapter；未指定的工人用 FakeJsonl 占位。"""
    orch = Orchestrator(workdir="/tmp", persistent=False)
    for name in (
            "kimi", "opencode", "qwen", "codebuddy", "dsh", "pi", "codex"):
        orch.adapters[name] = adapters.get(name, FakeJsonl(name))
    orch.host = FakeHost()
    orch.adapters["host"] = orch.host
    return orch


# ---- 1. AgentSpec 注册 ----

def test_agent_specs() -> None:
    assert AGENTS["kimi"].transport == "acp+jsonl"
    assert AGENTS["codex"].transport == "app-server"
    assert AGENTS["opencode"].transport == "acp+jsonl"
    assert AGENTS["qwen"].transport == "acp"
    assert AGENTS["codebuddy"].transport == "acp"
    assert AGENTS["dsh"].transport == "acp"
    assert AGENTS["pi"].transport == "rpc"

    orch = Orchestrator("/tmp", persistent=False)
    kimi = orch.adapters["kimi"]
    assert isinstance(kimi, AcpAdapter)
    assert kimi._cmd == ["kimi", "acp"]
    assert kimi._inactivity_timeout == 300
    assert getattr(kimi, "stateful_session", False) is True
    assert type(orch.adapters["codex"]) is CodexAppServerAdapter
    assert orch.adapters["codex"]._inactivity_timeout == 300
    opencode = orch.adapters["opencode"]
    assert isinstance(opencode, AcpOpenCodeAdapter)
    assert opencode._cmd == ["opencode", "acp"]
    assert opencode._inactivity_timeout == 300
    qwen = orch.adapters["qwen"]
    assert isinstance(qwen, AcpQwenAdapter)
    assert qwen._cmd == list(QWEN_ACP_DEFAULT_CMD)
    assert qwen._active_cmd == list(QWEN_ACP_DEFAULT_CMD)
    assert qwen._execution_cmd_overrides[ExecutionMode.READ_ONLY] == list(
        QWEN_ACP_READ_ONLY_CMD)
    assert qwen._fallback is None
    assert qwen._inactivity_timeout == 300
    codebuddy = orch.adapters["codebuddy"]
    assert isinstance(codebuddy, AcpCodeBuddyAdapter)
    assert codebuddy._fallback is None
    assert codebuddy._inactivity_timeout == 300
    dsh = orch.adapters["dsh"]
    assert isinstance(dsh, AcpDshAdapter)
    assert dsh._fallback is None
    assert dsh._inactivity_timeout == 300
    assert orch.adapters["pi"]._inactivity_timeout == 300
    assert getattr(orch.adapters["codex"], "stateful_session", False) is True
    assert orch.adapters["codex"].ephemeral_thread is False
    assert isinstance(orch.host.adapter, NativeAgentRuntime)
    assert orch.host.adapter.stateful_session is True
    assert orch.host.adapter.tool_policy == "none"
    assert not hasattr(orch.host.adapter, "set_permission_handler")
    assert isinstance(kimi._fallback, KimiAdapter)
    assert isinstance(opencode._fallback, OpenCodeAdapter)
    print("ok  AgentSpec 注册（kimi/opencode=ACP+JSONL，"
          "qwen/codebuddy/dsh=ACP，pi=RPC，codex=app-server）")


# ---- 2. ACP 增量上下文 ----

def test_incremental_context() -> None:
    """ACP agent 不重复完整 transcript：只收自上次派发后的新消息，
    且不重发它自己的回复；JSONL agent 仍收完整快照。"""
    kimi = StatefulFake("kimi")
    codex = FakeJsonl("codex")
    orch = make_orch(kimi=kimi, codex=codex)

    noop = lambda n, e: None
    asyncio.run(orch.dispatch("@kimi 任务一", noop))
    assert "任务一" in kimi.prompts[0]
    assert "自上次派发" in kimi.prompts[0]  # 用的是增量模板

    asyncio.run(orch.dispatch("@codex 说说看法", noop))
    # JSONL fallback 行为不回归：完整 transcript
    assert "任务一" in codex.last_prompt and "kimi 回复1" in codex.last_prompt

    asyncio.run(orch.dispatch("@kimi 任务二", noop))
    p = kimi.prompts[1]
    assert "任务二" in p              # 新的用户消息
    assert "说说看法" in p            # 上次派发后的用户消息
    assert "codex 回复" in p          # 其他 agent 的新回复
    assert "任务一" not in p          # 旧增量不重复
    assert "kimi 回复1" not in p      # 自己的回复不重发（已在 ACP session）
    print("ok  ACP 增量上下文（新消息齐全 + 不重复 + 跳过自己）")


def test_incremental_not_lost_on_failure() -> None:
    """失败不推进 cursor：下一轮重发失败轮的增量，上下文不丢。"""
    kimi = StatefulFake("kimi")
    orch = make_orch(kimi=kimi)
    noop = lambda n, e: None

    asyncio.run(orch.dispatch("@kimi 任务一", noop))
    kimi.fail_next = True
    events = []
    asyncio.run(orch.dispatch("@kimi 任务二", lambda n, e: events.append(e)))
    assert any(e.kind == "error" for e in events)
    assert "调用失败" in orch.history[-1].text  # history 诚实记录失败

    asyncio.run(orch.dispatch("@kimi 任务三", noop))
    p = kimi.prompts[-1]
    assert "任务二" in p and "任务三" in p   # 失败轮的增量补发
    assert "任务一" not in p                 # 但不退化成全量重发
    print("ok  失败后增量不丢（cursor 不推进，下轮补发）")


def test_provider_failures_are_actionable_in_events_and_timeline() -> None:
    """Qwen/Kimi 类 provider 原因穿过 ACP 后仍对用户可见。"""
    async def scenario(
        agent: str,
        data: dict,
        expected: str,
        forbidden: tuple[str, ...] = (),
    ) -> None:
        reset_state()
        os.environ.update({
            "FAKE_ACP_FAIL_PROMPT": "1",
            "FAKE_ACP_FAIL_PROMPT_CODE": "-32603",
            "FAKE_ACP_FAIL_PROMPT_MESSAGE": "Internal error",
            "FAKE_ACP_FAIL_PROMPT_DATA": json.dumps(data),
        })
        adapter = AcpAdapter(agent, [sys.executable, SERVER])
        orch = make_orch(**{agent: adapter})
        events: list[AgentEvent] = []
        try:
            outcome = await orch.dispatch(
                f"@{agent} 测试 provider 错误",
                lambda _name, event: events.append(event),
            )
            assert outcome.failures
            assert expected in orch.history[-1].text
            assert "Internal error" not in orch.history[-1].text
            assert any(
                event.kind == "error" and expected in event.text
                for event in events
            )
            visible = "\n".join(
                [orch.history[-1].text]
                + [event.text for event in events]
            )
            assert not any(secret in visible for secret in forbidden), visible
        finally:
            await orch.aclose()
            for key in (
                "FAKE_ACP_FAIL_PROMPT",
                "FAKE_ACP_FAIL_PROMPT_CODE",
                "FAKE_ACP_FAIL_PROMPT_MESSAGE",
                "FAKE_ACP_FAIL_PROMPT_DATA",
            ):
                os.environ.pop(key, None)

    async def run() -> None:
        await scenario(
            "qwen",
            {"details": (
                "request (11361 tokens) exceeds the available context "
                "size (8192 tokens)"
            )},
            "上下文窗口不足",
        )
        await scenario(
            "kimi",
            {
                "error": "Internal error",
                "details": (
                    "insufficient balance, please recharge; "
                    "\"access_token\": \"timeline-json-secret\", "
                    "client_secret: timeline-client-secret, "
                    "TOKEN: timeline-token-secret, "
                    "authorization: Basic timeline-colon-secret"
                ),
            },
            "账户余额或额度不足",
            (
                "timeline-json-secret",
                "timeline-client-secret",
                "timeline-token-secret",
                "timeline-colon-secret",
            ),
        )

    asyncio.run(run())
    print("ok  provider 错误 → event + timeline 可操作提示")


def test_session_id_info_once() -> None:
    """ACP session id 建立时通过 info 事件展示一次，后续轮次不刷屏。"""
    async def run() -> None:
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        infos1 = [ev async for ev in adapter.stream("fast round", "/tmp")
                  if ev.kind == "info"]
        assert len(infos1) == 1 and "fake-session-1" in infos1[0].text
        infos2 = [ev async for ev in adapter.stream("fast round", "/tmp")
                  if ev.kind == "info"]
        assert infos2 == []
        await adapter.aclose()
    asyncio.run(run())
    print("ok  session id 只展示一次（info 事件）")


# ---- 3. 权限 ----

async def make_client(**kwargs) -> AcpClient:
    client = AcpClient([sys.executable, SERVER], **kwargs)
    await client.start()
    return client


def test_permission_async_handler_selected() -> None:
    """TUI 注入的异步决策器：用户选 allow，agent 收到 selected outcome。"""
    async def run() -> None:
        reset_state()
        seen = []

        async def handler(params: dict) -> dict:
            seen.append(params)
            return {"outcome": "selected", "optionId": "allow"}

        client = await make_client(permission_handler=handler)
        sid = await client.session_new("/tmp")
        await client.prompt(sid, "需要 perm 一下")
        await client.close()
        assert seen and seen[0]["toolCall"]["title"] == "写文件"
        assert len(seen[0]["options"]) == 2  # 标题和 options 都交给了决策器
        perm = [e for e in state_events() if e.startswith("permission:")][0]
        assert '"outcome": "selected"' in perm and "allow" in perm
    asyncio.run(run())
    print("ok  异步权限处理器（用户选择 → selected）")


def test_permission_adapter_default_deny() -> None:
    """非 TUI 调用（无权限处理器）：adapter 级也安全拒绝。"""
    async def run() -> None:
        reset_state()
        adapter = AcpAdapter("fake", [sys.executable, SERVER])
        async for _ in adapter.stream("需要 perm 一下", "/tmp"):
            pass
        await adapter.aclose()
        perm = [e for e in state_events() if e.startswith("permission:")][0]
        assert '"outcome": "cancelled"' in perm
    asyncio.run(run())
    print("ok  无权限处理器 → adapter 级默认拒绝")


def test_permission_close_cancels_wait() -> None:
    """权限等待可取消：决策永远不返回时 close() 不挂起、不留 task。"""
    async def run() -> None:
        async def hanging_handler(params: dict) -> dict:
            await asyncio.Event().wait()  # 用户永远不点
            return {"outcome": "cancelled"}

        client = await make_client(permission_handler=hanging_handler)
        sid = await client.session_new("/tmp")
        task = asyncio.create_task(client.prompt(sid, "需要 perm 一下"))
        for _ in range(100):
            if client._permission_tasks:
                break
            await asyncio.sleep(0.05)
        assert client._permission_tasks, "权限请求未到达"
        await asyncio.wait_for(client.close(), timeout=10)  # 必须能关掉
        try:
            await task
            raise AssertionError("prompt 应该失败")
        except AcpError:
            pass
        assert not client._permission_tasks, "close 后仍有权限 task 残留"
    asyncio.run(run())
    print("ok  权限等待可取消（close 不挂起、无残留 task）")


def test_permission_close_multiple_tasks() -> None:
    """多个权限 task 同时结束时，done callback 修改注册表不能打断 close。"""
    async def run() -> None:
        client = AcpClient([sys.executable, SERVER])
        entered = 0
        both_entered = asyncio.Event()

        async def hanging_handler(params: dict) -> dict:
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await asyncio.Future()

        client.set_permission_handler(hanging_handler)
        for rid in (901, 902):
            task = asyncio.create_task(client._answer_permission(rid, {
                "options": [{"optionId": "allow", "kind": "allow_once"}],
            }))
            client._permission_tasks.add(task)
            task.add_done_callback(client._on_permission_task_done)

        await asyncio.wait_for(both_entered.wait(), timeout=2)
        await client.close()
        assert not client._permission_tasks

    asyncio.run(run())
    print("ok  多权限 task 并发收尾（close 遍历快照、无集合竞态）")


# ---- 4/5. TUI 集成：权限弹窗、状态可见、退出回收 ----

def _richlog_text(app) -> str:
    from textual.widgets import RichLog
    return "\n".join(str(line.text) for line in app.query_one(RichLog).lines)


def _activity_text(app) -> str:
    from textual.widgets import Static
    return str(app.query_one("#activity-panel", Static).render())


async def _wait_for(pilot, cond, timeout: float = 10) -> None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        await pilot.pause()
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等待超时")


def _make_tui_app(kimi_adapter) -> "object":
    from main import ChatApp
    return ChatApp(workdir="/tmp", orchestrator=make_orch(kimi=kimi_adapter))


def test_tui_permission_select() -> None:
    """TUI 权限弹窗：显示工具标题和 options，用户点 allow → agent 收到 selected。"""
    async def run() -> None:
        from textual.widgets import Button, Label
        from main import ComposerInput, PermissionScreen

        reset_state()
        app = _make_tui_app(AcpAdapter("kimi", [sys.executable, SERVER]))
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(ComposerInput)
            box.value = "@kimi 需要 perm secret-title 一下"
            await pilot.press("enter")
            await _wait_for(pilot, lambda: isinstance(app.screen, PermissionScreen))

            # 弹窗内容：来源 agent + 工具标题 + agent 提供的 options + 取消
            label = str(app.screen.query_one("#permission-summary", Label).render())
            assert "@kimi 请求执行" in label and "写文件" in label
            assert "API_TOKEN=[已隐藏]" in label
            assert "secret-value" not in label
            assert "examples/demo.txt" in label and "printf demo" in label
            assert "{" not in label and "\"rawInput\"" not in label
            labels = [str(b.label) for b in app.screen.query(Button)]
            assert "允许一次" in labels
            assert "拒绝这次" in labels
            assert "取消整个任务" in labels

            await pilot.click("#perm-opt-0")  # 允许一次
            await app.workers.wait_for_complete()
            assert not app._permission_futures, "权限 Future 未收尾"
        perms = [e for e in state_events() if e.startswith("permission:")]
        assert perms and '"outcome": "selected"' in perms[0] and "allow" in perms[0]
    asyncio.run(run())
    print("ok  TUI 权限弹窗（标题/options 可见，选择 → selected）")


def test_permission_screen_redacts_context_and_unknown_option_copy() -> None:
    """会话标题和 provider 自定义选项也属于不可信 UI 输入。"""
    async def run() -> None:
        from main import PermissionScreen
        from textual.widgets import Button, Label

        app = _make_tui_app(ClosableFake("kimi"))
        async with app.run_test() as pilot:
            app.push_screen(PermissionScreen(
                "kimi",
                {
                    "toolCall": {"title": "读取配置"},
                    "options": [{
                        "optionId": "opaque-secret-id",
                        "kind": "provider_custom",
                        "name": "API_KEY=provider-secret 自定义处理",
                    }],
                },
                session_label="排障 API_KEY=session-secret",
            ))
            await pilot.pause()
            summary = str(
                app.screen.query_one("#permission-summary", Label).render())
            buttons = "\n".join(
                str(button.label) for button in app.screen.query(Button))
            visible = summary + "\n" + buttons
            assert "session-secret" not in visible
            assert "provider-secret" not in visible
            assert "opaque-secret-id" not in visible
            assert "API_KEY=[已隐藏]" in visible

    asyncio.run(run())
    print("ok  权限上下文与未知选项文案统一脱敏")


def test_tui_yolo_auto_approve_mode() -> None:
    """显式 /yolo 不弹窗，只选择 agent 本次提供的 allow_once。"""
    async def run() -> None:
        from main import ChatApp, ComposerInput, PermissionScreen
        from textual.widgets import Static

        reset_state()
        app = ChatApp(
            workdir="/tmp",
            orchestrator=make_orch(
                kimi=AcpAdapter("kimi", [sys.executable, SERVER])),
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_toggle_yolo()
            await pilot.pause()
            status = str(app.query_one("#task-status", Static).render())
            assert "当前会话自动完全授权已开启" in status
            assert "只读阶段仍硬拒绝" in status

            box = app.query_one(ComposerInput)
            box.value = "@kimi 需要 perm 一下"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not isinstance(app.screen, PermissionScreen)
            assert not app._permission_futures

        permissions = [
            event for event in state_events()
            if event.startswith("permission:")
        ]
        assert len(permissions) == 1
        assert '"outcome": "selected"' in permissions[0]
        assert '"optionId": "allow"' in permissions[0]

    asyncio.run(run())
    print("ok  /yolo 自动批准（无弹窗 + allow_once + 持续风险提示）")


def test_tui_permission_cancel_on_exit() -> None:
    """权限等待中退出：按 cancelled 收尾，不留挂起 Future。"""
    async def run() -> None:
        from main import ComposerInput, PermissionScreen

        reset_state()
        app = _make_tui_app(AcpAdapter("kimi", [sys.executable, SERVER]))
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(ComposerInput)
            box.value = "@kimi 需要 perm 一下"
            await pilot.press("enter")
            await _wait_for(pilot, lambda: isinstance(app.screen, PermissionScreen))
            # 模拟退出收尾：等待中的权限按 cancelled 放行
            app._cancel_pending_permissions()
            await app.workers.wait_for_complete()
            assert not app._permission_futures
            await _wait_for(pilot, lambda: not isinstance(app.screen, PermissionScreen))
        perms = [e for e in state_events() if e.startswith("permission:")]
        assert perms and '"outcome": "cancelled"' in perms[0]
    asyncio.run(run())
    print("ok  权限等待中退出 → cancelled 收尾（无挂起 Future）")


def test_tui_permission_cancel_with_ctrl_x() -> None:
    """权限弹窗期间 Ctrl+X 必须同时结束权限等待和当前 command。"""
    async def run() -> None:
        from main import ComposerInput, PermissionScreen

        reset_state()
        app = _make_tui_app(AcpAdapter("kimi", [sys.executable, SERVER]))
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(ComposerInput)
            box.value = "@kimi 需要 perm 一下"
            await pilot.press("enter")
            await _wait_for(
                pilot, lambda: isinstance(app.screen, PermissionScreen))
            active = app.bus.active()
            assert active is not None
            await pilot.press("ctrl+x")
            await _wait_for(
                pilot,
                lambda: (not isinstance(app.screen, PermissionScreen)
                         and app.bus.get(active.command_id).status.value
                         == "cancelled"))
            assert app.bus.get(active.command_id).status.value == "cancelled"
            assert not app._permission_futures

    asyncio.run(run())
    print("ok  权限等待中 Ctrl+X → permission/command 同时取消")


def test_tui_permission_cancel_with_escape() -> None:
    """权限弹窗中的 Esc 也必须取消整个 command，而非只拒绝一次工具。"""
    async def run() -> None:
        from main import ComposerInput, PermissionScreen

        reset_state()
        app = _make_tui_app(AcpAdapter("kimi", [sys.executable, SERVER]))
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(ComposerInput)
            box.value = "@kimi 需要 perm 一下"
            await pilot.press("enter")
            await _wait_for(
                pilot, lambda: isinstance(app.screen, PermissionScreen))
            active = app.bus.active()
            assert active is not None
            await pilot.press("escape")
            await _wait_for(
                pilot,
                lambda: (not isinstance(app.screen, PermissionScreen)
                         and app.bus.get(active.command_id).status.value
                         == "cancelled"))
            assert not app._permission_futures

    asyncio.run(run())
    print("ok  权限等待中 Esc → permission/command 同时取消")


def test_tui_status_and_shutdown() -> None:
    """启动摘要与 /agents 可见传输协议；退出统一回收 fake ACP。"""
    async def run() -> None:
        from main import ComposerInput

        reset_state()
        kimi_acp = AcpAdapter("kimi", [sys.executable, SERVER])
        codex = ClosableFake("codex")
        opencode = ClosableFake("opencode")
        app = _make_tui_app(kimi_acp)
        app.orch.adapters["codex"] = codex
        app.orch.adapters["opencode"] = opencode
        async with app.run_test() as pilot:
            await pilot.pause()
            lines = _richlog_text(app)
            assert "聊天室已就绪" in lines
            assert "8/8" in lines
            app.action_show_agents()
            lines = _richlog_text(app)
            assert "@kimi · 可用 · ACP+JSONL" in lines
            assert "@codex · 可用 · APP-SERVER" in lines
            assert "@opencode · 可用 · ACP+JSONL" in lines
            assert "@qwen · 可用 · ACP" in lines
            assert "@dsh · 可用 · ACP" in lines
            assert "@pi · 可用 · RPC" in lines
            # 跑一轮，让 kimi acp 进程真的起来；session id 应展示一次
            box = app.query_one(ComposerInput)
            box.value = "@kimi fast round"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert "ACP session 已建立：fake-session-1" not in _richlog_text(app)
            app.action_toggle_details()
            await pilot.pause()
            assert "ACP session 已建立：fake-session-1" in _activity_text(app)
        # 退出后：所有 closable adapter 被 aclose
        assert codex.closed and opencode.closed
        assert kimi_acp._started is False
        await asyncio.sleep(0.3)
        out = subprocess.run(["pgrep", "-f", "fake_acp_server"],
                             capture_output=True, text=True).stdout.split()
        assert not out, f"残留 fake server 进程: {out}"
    asyncio.run(run())
    print("ok  TUI 状态可见（ACP+JSONL）+ 退出统一 aclose（无残留进程）")


# ---- P1 回归：同一 stateful agent 的并发 dispatch ----

def test_concurrent_same_agent_serialized() -> None:
    """delivery lock 契约：同一 stateful agent 的两个并发 dispatch 严格串行，
    第二轮只含新消息（不重复 first），顺序保持。"""
    async def run() -> None:
        gate = asyncio.Event()
        calls: list[str] = []

        class GatedStateful(StatefulFake):
            async def stream(self, prompt: str, workdir: str):
                calls.append(prompt)
                if "first" in prompt and not gate.is_set():
                    await gate.wait()  # 第一轮挂起，制造确定的并发窗口
                yield AgentEvent("text", f"回复{len(calls)}")
                yield AgentEvent("done")

        orch = make_orch(kimi=GatedStateful("kimi"))
        noop = lambda n, e: None
        t1 = asyncio.create_task(orch.dispatch("@kimi first", noop))
        await asyncio.sleep(0.2)          # t1 已进 stream 并挂起
        t2 = asyncio.create_task(orch.dispatch("@kimi second", noop))
        await asyncio.sleep(0.2)
        # t2 还在等 delivery lock：prompt 尚未构造，不能用旧 cursor 抢先发
        assert len(calls) == 1
        gate.set()
        await asyncio.gather(t1, t2)

        assert len(calls) == 2
        assert "first" in calls[0] and "second" not in calls[0]
        assert "second" in calls[1] and "first" not in calls[1]  # 不重复
        # 顺序保持：回复按派发顺序落进 history
        replies = [m.text for m in orch.history if m.speaker == "kimi"]
        assert replies == ["回复1", "回复2"]
    asyncio.run(run())
    print("ok  P1 同一 agent 并发 dispatch 严格串行（不重复、顺序保持）")


def test_concurrent_different_agents_parallel() -> None:
    """delivery lock 是每-agent 的：不同 agent 的并行扇出不受影响。"""
    async def run() -> None:
        gate = asyncio.Event()

        class GatedStateful(StatefulFake):
            async def stream(self, prompt: str, workdir: str):
                await gate.wait()
                yield AgentEvent("text", "kimi 回复")
                yield AgentEvent("done")

        orch = make_orch(kimi=GatedStateful("kimi"))
        noop = lambda n, e: None
        t1 = asyncio.create_task(orch.dispatch("@kimi first", noop))
        await asyncio.sleep(0.2)          # kimi 持有自己的锁并挂起
        # codex（无状态/另一把锁）必须能并发完成，不被 kimi 阻塞
        await asyncio.wait_for(orch.dispatch("@codex second", noop), timeout=5)
        assert not t1.done()
        gate.set()
        await t1
        assert [m.speaker for m in orch.history] == [
            "user", "user", "codex", "kimi"]
    asyncio.run(run())
    print("ok  P1 不同 agent 仍可并行扇出")


# ---- P2 回归：bootstrap 限界 ----

def test_bootstrap_bounded() -> None:
    """首次派发不发全部 history：bootstrap 只发最近 history_limit 条，
    成功后 cursor 推进到快照末尾，后续继续走增量。"""
    kimi = StatefulFake("kimi")
    orch = make_orch(kimi=kimi)  # history_limit 默认 12
    for i in range(30):
        orch._append_message("user", f"老消息{i}")
        orch._append_message("codex", f"旧回复{i}")
    noop = lambda n, e: None

    asyncio.run(orch.dispatch("@kimi 现在呢", noop))
    p = kimi.prompts[0]
    # 60 条历史 + 本条 = 61；窗口 = 最近 12 条（下标 49 起，即 旧回复24 起）
    assert "现在呢" in p and "旧回复29" in p
    assert "老消息25" in p
    assert "老消息24" not in p and "老消息0" not in p

    asyncio.run(orch.dispatch("@kimi 继续", noop))
    p2 = kimi.prompts[1]
    assert "继续" in p2
    assert "老消息25" not in p2      # bootstrap 之后回到纯增量
    assert "kimi 回复1" not in p2    # 自己的回复仍不重发
    print("ok  P2 bootstrap 限界（首发最近 12 条，之后纯增量）")


# ---- P2 回归：权限身份与 fail-closed ----

def test_permission_handler_identity() -> None:
    """通用 runtime：权限回调带 agent_name，多个 ACP agent 各自报上名字。"""
    kimi = AcpAdapter("kimi", [sys.executable, SERVER])
    claude = AcpAdapter("claude", [sys.executable, SERVER])
    orch = make_orch(kimi=kimi)
    orch.adapters["claude"] = claude
    seen: list[str] = []

    async def handler(agent_name: str, params: dict) -> dict:
        seen.append(agent_name)
        return {"outcome": "cancelled"}

    orch.set_permission_handler(handler)

    async def run() -> None:
        # client 层保持 params-only；adapter 注入的回调已绑定名字
        await kimi._client._permission_handler({"toolCall": {}})
        await claude._client._permission_handler({"toolCall": {}})
    asyncio.run(run())
    assert seen == ["kimi", "claude"]
    print("ok  P2 权限回调带 agent 身份（kimi / claude 各自标识）")


def test_permission_outcome_failclosed() -> None:
    """畸形/异常决策一律 fail-closed 成 cancelled，不发无效 outcome。"""
    async def run() -> None:
        # fake server 本次 options 只有 allow / deny
        cases = [
            ("返回 None", lambda p: None),
            ("缺 optionId 的 selected", lambda p: {"outcome": "selected"}),
            ("空 optionId", lambda p: {"outcome": "selected", "optionId": ""}),
            ("不属于本次 options 的 ID",
             lambda p: {"outcome": "selected", "optionId": "allow_eternally"}),
            ("完全畸形", lambda p: {"foo": 1}),
        ]
        for label, handler in cases:
            reset_state()
            client = await make_client(permission_handler=handler)
            sid = await client.session_new("/tmp")
            await client.prompt(sid, "需要 perm 一下")
            await client.close()
            perm = [e for e in state_events() if e.startswith("permission:")][0]
            assert '"outcome": "cancelled"' in perm, f"{label}: {perm}"

        async def boom(p: dict) -> dict:
            raise RuntimeError("决策器挂了")
        reset_state()
        client = await make_client(permission_handler=boom)
        sid = await client.session_new("/tmp")
        await client.prompt(sid, "需要 perm 一下")
        await client.close()
        perm = [e for e in state_events() if e.startswith("permission:")][0]
        assert '"outcome": "cancelled"' in perm

        # 正向：合法的非 allow option（reject_once 的 deny）必须能透传
        reset_state()
        client = await make_client(
            permission_handler=lambda p: {"outcome": "selected", "optionId": "deny"})
        sid = await client.session_new("/tmp")
        await client.prompt(sid, "需要 perm 一下")
        await client.close()
        perm = [e for e in state_events() if e.startswith("permission:")][0]
        assert '"outcome": "selected"' in perm and '"optionId": "deny"' in perm
    asyncio.run(run())
    print("ok  P2 权限 outcome 校验（None/畸形/异常 → cancelled）")


def test_permission_task_exception_consumed() -> None:
    """权限后台 task 的异常被 done callback 消费：无 never-retrieved warning，
    task 注册表不留残留。"""
    async def run() -> None:
        # 不 start：_send 必然失败，模拟 close 竞态下应答发不出去
        client = AcpClient([sys.executable, SERVER])
        errors: list[dict] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, ctx: errors.append(ctx))

        async def ok_handler(params: dict) -> dict:
            return {"outcome": "cancelled"}
        client.set_permission_handler(ok_handler)
        task = asyncio.create_task(client._answer_permission(99, {}))
        client._permission_tasks.add(task)
        task.add_done_callback(client._on_permission_task_done)
        await asyncio.sleep(0.1)
        import gc
        gc.collect()  # 若异常没被消费，回收时会触发 loop exception handler
        await asyncio.sleep(0.1)
        assert task.done() and task.exception() is not None  # send 确实失败了
        assert not errors, errors                            # 但已被消费
        assert not client._permission_tasks                  # 注册表无残留
    asyncio.run(run())
    print("ok  P2 权限 task 异常被消费（无 warning、无残留）")


if __name__ == "__main__":
    test_agent_specs()
    test_incremental_context()
    test_incremental_not_lost_on_failure()
    test_provider_failures_are_actionable_in_events_and_timeline()
    test_session_id_info_once()
    test_concurrent_same_agent_serialized()
    test_concurrent_different_agents_parallel()
    test_bootstrap_bounded()
    test_permission_async_handler_selected()
    test_permission_adapter_default_deny()
    test_permission_close_cancels_wait()
    test_permission_close_multiple_tasks()
    test_permission_handler_identity()
    test_permission_outcome_failclosed()
    test_permission_task_exception_consumed()
    test_tui_permission_select()
    test_permission_screen_redacts_context_and_unknown_option_copy()
    test_tui_yolo_auto_approve_mode()
    test_tui_permission_cancel_on_exit()
    test_tui_permission_cancel_with_ctrl_x()
    test_tui_permission_cancel_with_escape()
    test_tui_status_and_shutdown()
    print("\nPhase 2 全部通过")

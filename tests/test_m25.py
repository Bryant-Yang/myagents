"""M2.5 里程碑测试：Orchestrator 持久化 timeline + seq cursor。

覆盖：
- persistent 重启恢复 timeline（seq/created_at 续接）
- stateful agent cursor 落盘并在重启后恢复，只发新 seq
- 持久化 cursor 超出 timeline → 构造即 fail loudly
- 用户消息落盘失败：history 不变、adapter 不被调用
- checkpoint 写失败：adapter 已调用但内存/磁盘 cursor 不推进，下轮补发
- JSONL agent 重启后 prompt 含恢复的 history 快照
- ACP restore：load 成功保留 cursor；load 失败/无 capability 回退 new 时
  cursor 归 0 走 bootstrap；checkpoint 写失败零 prompt 且状态不假提交；
  明确未发送时 cursor 不推进；write 已开始或 remote error 时推进
  no-replay cursor；post-submit cursor 落盘失败必须 fatal 且禁止继续派发

全部使用 tempfile，不触碰真实状态目录。

运行：.venv/bin/python tests/test_m25.py
"""

import asyncio
import contextlib
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acp.adapter import AcpAdapter, SessionPreparation
from adapters.base import (
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    AgentEvent,
)
from orchestrator import Orchestrator, OrchestratorClosedError
from storage.store import (CorruptedStorageError, RoomBusyError, RoomStore,
                           WorkdirMismatchError)

SERVER = str(Path(__file__).parent / "fake_acp_server.py")
_FAKE_ENV_KEYS = ("FAKE_ACP_STATE", "FAKE_ACP_FAIL_LOAD",
                  "FAKE_ACP_FAIL_LOAD_CODE",
                  "FAKE_ACP_NO_LOAD_CAP", "FAKE_ACP_FAIL_PROMPT")


class FakeJsonl:
    """无状态假 agent：记录最后一次 prompt。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.last_prompt: str | None = None

    async def stream(self, prompt: str, workdir: str):
        self.last_prompt = prompt
        yield AgentEvent("text", f"{self.name} 回复")
        yield AgentEvent("done")


class StatefulFake:
    """有状态假 agent：代表 session 已另行恢复（本文件不测试 ACP）。"""

    stateful_session = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.prompts: list[str] = []

    async def stream(self, prompt: str, workdir: str):
        self.prompts.append(prompt)
        yield AgentEvent("text", f"{self.name} 回复{len(self.prompts)}")
        yield AgentEvent("done")


class _PairBarrier:
    def __init__(self) -> None:
        self.arrivals = 0
        self.ready = asyncio.Event()

    async def arrive(self) -> None:
        self.arrivals += 1
        if self.arrivals == 2:
            self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=1)


class _ParallelPreparedFake:
    """Stateful fake that submits once, then waits for structured cancel."""

    stateful_session = True

    def __init__(self, name: str, barrier: _PairBarrier) -> None:
        self.name = name
        self.session_id = f"{name}-session"
        self.prompts: list[str] = []
        self.cancelled = asyncio.Event()
        self.closed = asyncio.Event()

        self._barrier = barrier

    async def stream_prepared(
        self,
        prompt_factory,
        workdir: str,
        resume_session_id: str | None = None,
        **_kwargs,
    ):
        del workdir, resume_session_id
        prep = SessionPreparation(
            session_id=self.session_id,
            restored=False,
            load_failed=False,
            fresh=True,
        )
        self.prompts.append(prompt_factory(prep))
        await self._barrier.arrive()
        try:
            yield AgentEvent("delivery_committed")
            await asyncio.Event().wait()
            yield AgentEvent("text", f"{self.name}-late-reply")
            yield AgentEvent("done")
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.closed.set()


class _TerminalBeforeEventPreparedFake:
    """Accept a prepared prompt, then terminate before a public event."""

    stateful_session = True

    def __init__(self, terminal: str) -> None:
        self.terminal = terminal
        self.calls = 0

    async def stream_prepared(
        self,
        prompt_factory,
        workdir: str,
        resume_session_id: str | None = None,
        **_kwargs,
    ):
        del workdir, resume_session_id
        self.calls += 1
        prompt_factory(SessionPreparation(
            session_id="terminal-before-event-session",
            restored=False,
            load_failed=False,
            fresh=True,
        ))
        if self.terminal == "uncertain":
            raise AgentDeliveryUncertainError("accepted before first event")
        if self.terminal == "cancelled":
            raise AgentDeliveryCancelledError("cancelled after acceptance")
        raise AssertionError(self.terminal)
        yield  # pragma: no cover - keep this an async generator


class _ClosablePreparedFake:
    """Expose whether a callback-side abort explicitly closes the producer."""

    stateful_session = True

    def __init__(self) -> None:
        self.calls = 0
        self.closed = 0

    async def stream_prepared(
        self,
        prompt_factory,
        workdir: str,
        resume_session_id: str | None = None,
        **_kwargs,
    ):
        del workdir, resume_session_id
        self.calls += 1
        prompt_factory(SessionPreparation(
            session_id="closable-prepared-session",
            restored=self.calls > 1,
            load_failed=False,
            fresh=self.calls == 1,
        ))
        try:
            yield AgentEvent("delivery_committed")
            yield AgentEvent("text", f"prepared reply {self.calls}")
            yield AgentEvent("done")
        finally:
            self.closed += 1


class _ClosableJsonlFake:
    """Stateless counterpart for callback-side stream cleanup."""

    def __init__(self) -> None:
        self.calls = 0
        self.closed = 0

    async def stream(self, prompt: str, workdir: str):
        del prompt, workdir
        self.calls += 1
        try:
            yield AgentEvent("text", f"jsonl reply {self.calls}")
            yield AgentEvent("done")
        finally:
            self.closed += 1


class _Room:
    """临时房间：workdir 与 state_root 都在 tempfile 里，互不相含。"""

    def __init__(self) -> None:
        # Keep control.sock under macOS' short AF_UNIX pathname limit.
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        root = Path(self._tmp.name)
        self.workdir = str(root / "work")
        self.state_root = root / "state"
        Path(self.workdir).mkdir()

    def open_store(self) -> RoomStore:
        return RoomStore(self.workdir, state_root=self.state_root)

    def make_orch(self, **adapters) -> Orchestrator:
        """真实 Orchestrator（挂本房间的 store）+ 假 adapter。"""
        orch = Orchestrator(self.workdir, store=self.open_store())
        for name, adapter in adapters.items():
            orch.adapters[name] = adapter
        return orch

    def cleanup(self) -> None:
        self._tmp.cleanup()


def _raises(exc_type, fn) -> None:
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f"未抛出 {exc_type.__name__}")


# ---- 1. persistent 重启恢复 timeline ----

def test_restart_restores_timeline() -> None:
    room = _Room()
    orch1 = room.make_orch()
    m1 = orch1._append_message("user", "hello")
    assert m1.seq == 1 and m1.created_at  # seq + UTC 时间戳来自 store

    # 模拟重启：先 close 旧 owner（释放 lease），再开新 Orchestrator
    asyncio.run(orch1.aclose())
    orch2 = room.make_orch()
    assert [(m.seq, m.speaker, m.text) for m in orch2.history] == [
        (1, "user", "hello")]
    assert orch2.history[0].created_at == m1.created_at
    # seq 续接，不从 1 重新开始
    assert orch2._append_message("user", "world").seq == 2
    asyncio.run(orch2.aclose())
    room.cleanup()
    print("ok  persistent 重启恢复 timeline（seq 续接）")


# ---- 2. stateful cursor 落盘 + 重启恢复 ----

def test_stateful_cursor_survives_restart() -> None:
    room = _Room()
    kimi = StatefulFake("kimi")
    orch1 = room.make_orch(kimi=kimi)
    noop = lambda n, e: None

    asyncio.run(orch1.dispatch("@kimi 任务一", noop))
    # 成功交付后 cursor 落盘：推进到 prompt 构造时的快照末尾 seq=1
    #（user 消息；agent 自己的回复 seq2 靠 speaker 过滤跳过）
    assert orch1._cursors["kimi"] == 1
    store1 = room.open_store()
    assert store1.get_agent_state("kimi")["cursor"] == 1

    # 重启：先 close 旧 owner；cursor 从 state.json 恢复，只发 seq>1 的新消息
    asyncio.run(orch1.aclose())
    kimi2 = StatefulFake("kimi")
    orch2 = room.make_orch(kimi=kimi2)
    assert orch2._cursors["kimi"] == 1
    asyncio.run(orch2.dispatch("@kimi 任务二", noop))
    p = kimi2.prompts[0]
    assert "任务二" in p
    assert "任务一" not in p and "kimi 回复1" not in p  # 不重发已交付历史
    assert room.open_store().get_agent_state("kimi")["cursor"] == 3
    asyncio.run(orch2.aclose())
    room.cleanup()
    print("ok  stateful cursor 落盘 + 重启恢复（只发新 seq）")


# ---- 3. cursor 超出 timeline → 构造即 fail loudly ----

def test_cursor_ahead_of_timeline_fails() -> None:
    room = _Room()
    store = room.open_store()
    store.set_agent_state("kimi", cursor=5)  # 空 timeline 上的悬空 cursor
    _raises(CorruptedStorageError,
            lambda: Orchestrator(room.workdir, store=store))
    room.cleanup()
    print("ok  cursor 超出 timeline → 构造 fail loudly")


# ---- 4. 用户消息落盘失败：history 不变、adapter 不被调用 ----

def test_user_append_failure_atomic() -> None:
    room = _Room()
    kimi = StatefulFake("kimi")
    orch = room.make_orch(kimi=kimi)

    def boom(*args, **kwargs):
        raise OSError("模拟磁盘写失败")
    orch.store.append = boom  # type: ignore[union-attr]

    _raises(OSError,
            lambda: asyncio.run(orch.dispatch("@kimi 任务一", lambda n, e: None)))
    assert orch.history == []          # 没落盘成功就不进 history
    assert kimi.prompts == []          # adapter 根本没被调用
    asyncio.run(orch.aclose())
    room.cleanup()
    print("ok  用户消息落盘失败（history 不变 + adapter 未调用）")


# ---- 5. checkpoint 写失败：cursor 不推进，下轮补发 ----

def test_checkpoint_failure_resends() -> None:
    room = _Room()
    kimi = StatefulFake("kimi")
    orch = room.make_orch(kimi=kimi)
    noop = lambda n, e: None

    store = orch.store
    assert store is not None
    orig_set = store.set_agent_state

    def boom(*args, **kwargs):
        raise OSError("模拟 checkpoint 写失败")
    store.set_agent_state = boom  # type: ignore[method-assign]

    # adapter 正常执行完（回复已进 history/timeline），但 checkpoint 失败
    # → 异常向 dispatch 传播，内存/磁盘 cursor 都不推进
    _raises(OSError, lambda: asyncio.run(orch.dispatch("@kimi 任务一", noop)))
    assert len(kimi.prompts) == 1                       # adapter 确实被调用了
    assert [m.speaker for m in orch.history] == ["user", "kimi"]
    assert orch._cursors["kimi"] == 0                   # 内存 cursor 不动
    assert store.get_agent_state("kimi")["cursor"] == 0  # 磁盘 cursor 不动

    # 恢复写盘后下一轮：按旧 cursor 补发失败轮的增量
    store.set_agent_state = orig_set  # type: ignore[method-assign]
    asyncio.run(orch.dispatch("@kimi 任务二", noop))
    p = kimi.prompts[-1]
    assert "任务一" in p and "任务二" in p
    assert orch._cursors["kimi"] > 0
    assert store.get_agent_state("kimi")["cursor"] == orch._cursors["kimi"]
    asyncio.run(orch.aclose())
    room.cleanup()
    print("ok  checkpoint 写失败（cursor 不推进 + 下轮补发）")


# ---- 6. JSONL 重启后 prompt 含恢复的 history 快照 ----

def test_jsonl_restart_sees_history() -> None:
    room = _Room()
    orch1 = room.make_orch(codex=FakeJsonl("codex"))
    asyncio.run(orch1.dispatch("@codex 你好", lambda n, e: None))

    asyncio.run(orch1.aclose())  # 重启前先 close 旧 owner
    codex2 = FakeJsonl("codex")
    orch2 = room.make_orch(codex=codex2)
    asyncio.run(orch2.dispatch("@codex 继续", lambda n, e: None))
    prompt = codex2.last_prompt
    assert "你好" in prompt and "codex 回复" in prompt  # 恢复的历史进了快照
    assert "继续" in prompt
    asyncio.run(orch2.aclose())
    room.cleanup()
    print("ok  JSONL 重启后 prompt 含恢复的 history 快照")


# ---- ACP restore：真实 AcpAdapter + fake ACP server + tempfile RoomStore ----

def _set_fake_env(room: "_Room", **flags: str) -> None:
    os.environ["FAKE_ACP_STATE"] = str(Path(room._tmp.name) / "fake_acp_state")
    for key, value in flags.items():
        os.environ[key] = value


def _clear_fake_env() -> None:
    for key in _FAKE_ENV_KEYS:
        os.environ.pop(key, None)


def _fake_state_raw(room: "_Room") -> str:
    path = Path(room._tmp.name) / "fake_acp_state"
    return path.read_text() if path.exists() else ""


def _make_acp_orch(room: "_Room") -> tuple[Orchestrator, AcpAdapter]:
    adapter = AcpAdapter("kimi", [sys.executable, SERVER])
    return room.make_orch(kimi=adapter), adapter


def test_acp_load_success_keeps_cursor() -> None:
    """重启后 load 成功（restored）：保留 cursor，prompt 只含新 seq。"""
    room = _Room()
    _set_fake_env(room)
    noop = lambda n, e: None

    async def run() -> None:
        orch1, adapter1 = _make_acp_orch(room)
        await orch1.dispatch("@kimi fast round", noop)
        state = room.open_store().get_agent_state("kimi")
        assert state["session_id"] == "fake-session-1"
        cursor_r1 = state["cursor"]
        assert cursor_r1 > 0
        await orch1.aclose()  # 重启前 close 旧 owner（回收 adapter + 释放 lease）

        # 重启：新 store + 新 adapter；load 命中 stored session → 保留 cursor
        orch2, adapter2 = _make_acp_orch(room)
        assert orch2._cursors["kimi"] == cursor_r1
        await orch2.dispatch("@kimi 第二轮", noop)
        raw = _fake_state_raw(room)
        assert "load:fake-session-1" in raw
        tail = raw[raw.rindex("load:fake-session-1"):]
        assert "第二轮" in tail and "fast round" not in tail  # 纯增量
        await orch2.aclose()
        assert adapter2._started is False  # 进程已回收，无残留

    try:
        asyncio.run(run())
    finally:
        _clear_fake_env()
        room.cleanup()
    print("ok  ACP load 成功保留 cursor（prompt 不含旧内容）")


def test_acp_load_failure_resets_cursor() -> None:
    """load 被 agent 拒绝 → 回退 new：cursor 归 0，新 session id 落盘，
    bootstrap 只发最近 history_limit 条。"""
    room = _Room()
    store = room.open_store()
    for i in range(30):
        store.append("user", f"老消息{i}")          # seq 1..30
    store.set_agent_state("kimi", cursor=25, session_id="fake-session-1")
    _set_fake_env(
        room,
        FAKE_ACP_FAIL_LOAD="1",
        FAKE_ACP_FAIL_LOAD_CODE="-32002",
    )

    async def run() -> None:
        orch, adapter = _make_acp_orch(room)
        assert orch._cursors["kimi"] == 25  # 构造时从 store 恢复
        await orch.dispatch("@kimi 现在呢", lambda n, e: None)
        raw = _fake_state_raw(room)
        assert "load:fake-session-1" in raw            # 尝试过 load
        assert raw.index("load:") < raw.index("new:")  # 失败后回退 new
        tail = raw[raw.index("new:"):]
        # cursor 归 0 → bootstrap 最近 12 条（seq20..31）
        assert "老消息19" in tail       # 保留旧 cursor=25 时不会出现
        assert "老消息18" not in tail   # bootstrap 窗口有界
        assert "现在呢" in tail
        state = room.open_store().get_agent_state("kimi")
        assert state["session_id"] == "fake-session-1"  # 新 session id 落盘
        assert state["cursor"] == 31    # 成功后推进到快照末尾
        await orch.aclose()

    try:
        asyncio.run(run())
    finally:
        _clear_fake_env()
        room.cleanup()
    print("ok  ACP load 失败回退 new（cursor 归 0 + bootstrap 限界）")


def test_acp_no_load_capability_resets_cursor() -> None:
    """agent 不声明 loadSession：直接 new（不算失败），cursor 归 0。"""
    room = _Room()
    store = room.open_store()
    for i in range(30):
        store.append("user", f"老消息{i}")
    store.set_agent_state("kimi", cursor=25, session_id="fake-session-1")
    _set_fake_env(room, FAKE_ACP_NO_LOAD_CAP="1")

    async def run() -> None:
        orch, adapter = _make_acp_orch(room)
        await orch.dispatch("@kimi 现在呢", lambda n, e: None)
        raw = _fake_state_raw(room)
        assert "load:" not in raw      # capability 不支持，根本没尝试
        assert "new:" in raw
        tail = raw[raw.index("new:"):]
        assert "老消息19" in tail and "老消息18" not in tail
        state = room.open_store().get_agent_state("kimi")
        assert state["session_id"] == "fake-session-1"
        assert state["cursor"] == 31
        await orch.aclose()

    try:
        asyncio.run(run())
    finally:
        _clear_fake_env()
        room.cleanup()
    print("ok  ACP 无 load capability 直接 new（cursor 归 0）")


def test_acp_reconnect_load_keeps_cursor() -> None:
    """adapter aclose 后下一轮：fresh start + load 成功，保留 cursor。"""
    room = _Room()
    _set_fake_env(room)
    noop = lambda n, e: None

    async def run() -> None:
        orch, adapter = _make_acp_orch(room)
        await orch.dispatch("@kimi fast round", noop)
        cursor_r1 = orch._cursors["kimi"]
        assert cursor_r1 > 0
        await adapter.aclose()
        assert adapter._started is False

        await orch.dispatch("@kimi 第二轮", noop)
        raw = _fake_state_raw(room)
        assert "load:fake-session-1" in raw
        tail = raw[raw.rindex("load:fake-session-1"):]
        assert "第二轮" in tail and "fast round" not in tail
        assert orch._cursors["kimi"] > cursor_r1  # 正常推进
        await orch.aclose()

    try:
        asyncio.run(run())
    finally:
        _clear_fake_env()
        room.cleanup()
    print("ok  ACP aclose 后重连 load 成功（保留 cursor）")


def test_acp_checkpoint_failure_no_commit() -> None:
    """checkpoint 写失败：零 prompt、adapter reset、内存/磁盘不假提交、
    dispatch 抛异常（不伪装成 agent 调用失败）。"""
    room = _Room()
    store = room.open_store()
    store.append("user", "旧消息")  # seq 1
    store.set_agent_state("kimi", cursor=1, session_id="fake-session-1")
    _set_fake_env(room)

    async def run() -> None:
        orch, adapter = _make_acp_orch(room)
        assert orch._cursors["kimi"] == 1
        inner_store = orch.store
        assert inner_store is not None
        orig_set = inner_store.set_agent_state

        def boom(*args, **kwargs):
            raise OSError("模拟 checkpoint 写失败")
        inner_store.set_agent_state = boom  # type: ignore[method-assign]

        events = []
        try:
            await orch.dispatch("@kimi fast round",
                                lambda n, e: events.append(e))
            raise AssertionError("checkpoint 失败必须向 dispatch 传播")
        except Exception as exc:
            assert "checkpoint" in str(exc)
        # error event 如实发出，但不是"调用失败"式的 agent 错误收尾
        assert any(e.kind == "error" and "checkpoint" in e.text
                   for e in events)
        raw = _fake_state_raw(room)
        assert "load:fake-session-1" in raw  # prepare 已做
        assert "prompt:" not in raw          # 但零 prompt
        # adapter 按 stream_prepared 契约 reset fresh session
        assert adapter._started is False and adapter.session_id is None
        # 内存/磁盘状态不假提交
        assert orch._cursors["kimi"] == 1
        inner_store.set_agent_state = orig_set  # type: ignore[method-assign]
        state = room.open_store().get_agent_state("kimi")
        assert state == {"cursor": 1, "session_id": "fake-session-1"}
        # 没有伪装收尾：最后一条只是用户消息，无"调用失败"记录
        assert orch.history[-1].speaker == "user"
        await orch.aclose()

    try:
        asyncio.run(run())
    finally:
        _clear_fake_env()
        room.cleanup()
    print("ok  ACP checkpoint 写失败（零 prompt + 状态不假提交 + 抛异常）")


def test_acp_post_submit_cursor_failure_is_fatal_no_replay() -> None:
    """After delivery_committed, a cursor write failure must halt the room.

    It cannot be converted into an ordinary agent failure: that would leave a
    stale durable cursor and permit a later dispatch to replay the accepted
    prompt and its tool side effects.
    """
    room = _Room()
    _set_fake_env(room)

    async def run() -> None:
        orch, adapter = _make_acp_orch(room)
        inner_store = orch.store
        assert inner_store is not None
        orig_set = inner_store.set_agent_state
        set_calls = 0

        def fail_post_submit(*args, **kwargs):
            nonlocal set_calls
            set_calls += 1
            if set_calls == 2:
                raise OSError("post-submit cursor fsync failed")
            return orig_set(*args, **kwargs)

        inner_store.set_agent_state = fail_post_submit  # type: ignore[method-assign]
        events = []
        try:
            await orch.dispatch(
                "@kimi once-only", lambda _name, event: events.append(event))
        except OSError as exc:
            assert str(exc) == "post-submit cursor fsync failed"
        else:
            raise AssertionError("post-submit cursor 写失败必须原样传播")

        assert set_calls == 2  # prepare checkpoint 成功，commit checkpoint 失败
        assert any(
            event.kind == "error" and "checkpoint" in event.text
            for event in events
        ), events
        assert [message.speaker for message in orch.history] == ["user"]
        assert not any("调用失败" in message.text for message in orch.history)
        wire = _fake_state_raw(room)
        assert wire.count("once-only") == 1, wire

        # Even after the test store starts accepting writes again, this
        # orchestrator cannot safely infer the missing durable cursor.  It must
        # reject before appending another user message or touching the agent.
        inner_store.set_agent_state = orig_set  # type: ignore[method-assign]
        try:
            await orch.dispatch("@kimi second", lambda _name, _event: None)
        except OrchestratorClosedError:
            pass
        else:
            raise AssertionError("fatal no-replay 缺口后必须拒绝后续 dispatch")
        assert [message.text for message in orch.history] == ["@kimi once-only"]
        assert _fake_state_raw(room).count("once-only") == 1
        assert "second" not in _fake_state_raw(room)
        await orch.aclose()
        assert adapter._started is False

    try:
        asyncio.run(run())
    finally:
        _clear_fake_env()
        room.cleanup()
    print("ok  ACP post-submit cursor 写失败 → fatal + 不重放")


def _assert_terminal_checkpoint_failure_is_fatal(terminal: str) -> None:
    room = _Room()

    async def run() -> None:
        adapter = _TerminalBeforeEventPreparedFake(terminal)
        orch = room.make_orch(kimi=adapter)
        store = orch.store
        assert store is not None
        original = store.set_agent_state
        writes = 0

        def fail_no_replay_commit(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError(f"{terminal} no-replay checkpoint failed")
            return original(*args, **kwargs)

        store.set_agent_state = fail_no_replay_commit  # type: ignore[method-assign]
        try:
            await orch.dispatch(
                f"@kimi once-only {terminal}", lambda _name, _event: None)
        except OSError as exc:
            assert str(exc) == f"{terminal} no-replay checkpoint failed"
        else:
            raise AssertionError(
                f"{terminal} 后 checkpoint 写失败必须原样传播")

        assert writes == 2
        assert adapter.calls == 1
        assert orch._closed is True
        assert [message.speaker for message in orch.history] == ["user"]

        # 恢复存储也不能推测缺失的 durable cursor；后续派发必须在触碰
        # adapter 前拒绝，确保已接受 turn 不会被重放。
        store.set_agent_state = original  # type: ignore[method-assign]
        try:
            await orch.dispatch("@kimi replay", lambda _name, _event: None)
        except OrchestratorClosedError:
            pass
        else:
            raise AssertionError("no-replay checkpoint 缺口后必须 poison room")
        assert adapter.calls == 1
        assert [message.text for message in orch.history] == [
            f"@kimi once-only {terminal}"]
        await orch.aclose()

    try:
        asyncio.run(run())
    finally:
        room.cleanup()


def test_uncertain_checkpoint_failure_is_fatal_no_replay() -> None:
    _assert_terminal_checkpoint_failure_is_fatal("uncertain")
    print("ok  uncertain 后 checkpoint 写失败 → fatal + 不重放")


def test_cancelled_checkpoint_failure_is_fatal_no_replay() -> None:
    _assert_terminal_checkpoint_failure_is_fatal("cancelled")
    print("ok  cancelled 后 checkpoint 写失败 → fatal + 不重放")


def test_event_callback_failure_closes_agent_streams() -> None:
    """A consumer failure must not strand either producer behind its lock."""

    async def run_case(adapter) -> None:
        room = _Room()
        try:
            orch = room.make_orch(kimi=adapter)

            def broken_sink(_name: str, event: AgentEvent) -> None:
                if event.kind == "text":
                    raise RuntimeError("event sink failed")

            try:
                await orch.dispatch("@kimi first", broken_sink)
            except RuntimeError as exc:
                assert str(exc) == "event sink failed"
            else:
                raise AssertionError("event sink 异常必须穿透 dispatch")

            assert adapter.calls == 1
            assert adapter.closed == 1
            # A second delivery proves the prior iterator is no longer live.
            await asyncio.wait_for(
                orch.dispatch("@kimi second", lambda _name, _event: None),
                timeout=1,
            )
            assert adapter.calls == 2
            assert adapter.closed == 2
            await orch.aclose()
        finally:
            room.cleanup()

    async def run() -> None:
        await run_case(_ClosablePreparedFake())
        await run_case(_ClosableJsonlFake())

    asyncio.run(run())
    print("ok  event sink 失败显式关闭 prepared/普通 agent stream")


def test_acp_event_callback_failure_releases_writer_lock() -> None:
    """Real AcpAdapter regression for a sink abort between streamed events."""
    room = _Room()
    _set_fake_env(room)

    async def run() -> None:
        orch, adapter = _make_acp_orch(room)

        def broken_sink(_name: str, event: AgentEvent) -> None:
            if event.kind == "text":
                raise RuntimeError("ACP event sink failed")

        try:
            await orch.dispatch("@kimi first", broken_sink)
        except RuntimeError as exc:
            assert str(exc) == "ACP event sink failed"
        else:
            raise AssertionError("ACP sink 异常必须穿透 dispatch")

        assert adapter._lock.locked() is False
        await asyncio.wait_for(
            orch.dispatch("@kimi second", lambda _name, _event: None),
            timeout=2,
        )
        assert _fake_state_raw(room).count("prompt:") == 2
        await orch.aclose()

    try:
        asyncio.run(run())
    finally:
        _clear_fake_env()
        room.cleanup()
    print("ok  ACP event sink 失败回收 stream 并释放 writer lock")


def _install_parallel_checkpoint_failure(orch: Orchestrator):
    store = orch.store
    assert store is not None
    original = store.set_agent_state

    def fail_kimi_delivery(name: str, *args, **kwargs):
        # make_prompt includes session_id; delivery_committed only advances
        # cursor.  Fail exactly the latter for one participant.
        if name == "kimi" and kwargs.get("session_id") is None:
            raise OSError("parallel post-submit checkpoint failed")
        return original(name, *args, **kwargs)

    store.set_agent_state = fail_kimi_delivery  # type: ignore[method-assign]
    return store, original


def test_fanout_checkpoint_fatal_cancels_and_awaits_sibling() -> None:
    """Fatal state in one fan-out branch leaves no background history writer."""
    room = _Room()

    async def run() -> None:
        barrier = _PairBarrier()
        kimi = _ParallelPreparedFake("kimi", barrier)
        opencode = _ParallelPreparedFake("opencode", barrier)
        orch = room.make_orch(kimi=kimi, opencode=opencode)
        store, original = _install_parallel_checkpoint_failure(orch)
        try:
            await orch.dispatch(
                "@kimi @opencode once-only fanout",
                lambda _name, _event: None,
            )
        except OSError as exc:
            assert str(exc) == "parallel post-submit checkpoint failed"
        else:
            raise AssertionError("fan-out post-submit checkpoint 失败必须传播")

        # dispatch may return only after every sibling reached terminal
        # cancellation.  No task may append a late reply in the poisoned room.
        assert opencode.cancelled.is_set()
        assert opencode.closed.is_set()
        await asyncio.sleep(0)
        assert [message.speaker for message in orch.history] == ["user"]
        assert len(kimi.prompts) == len(opencode.prompts) == 1
        store.set_agent_state = original  # type: ignore[method-assign]
        await orch.aclose()

    try:
        asyncio.run(run())
    finally:
        room.cleanup()
    print("ok  fan-out fatal checkpoint 结构化取消并等待 sibling")


def test_discussion_checkpoint_fatal_cancels_and_awaits_round_sibling() -> None:
    """A poisoned discussion round cannot leak a participant or moderator."""
    room = _Room()

    async def run() -> None:
        barrier = _PairBarrier()
        kimi = _ParallelPreparedFake("kimi", barrier)
        opencode = _ParallelPreparedFake("opencode", barrier)
        moderator = FakeJsonl("host")
        orch = room.make_orch(kimi=kimi, opencode=opencode)
        orch.host = moderator
        orch.adapters["host"] = moderator
        store, original = _install_parallel_checkpoint_failure(orch)
        try:
            await orch.dispatch(
                "/discuss @kimi @opencode --rounds 2 -- fatal round",
                lambda _name, _event: None,
            )
        except OSError as exc:
            assert str(exc) == "parallel post-submit checkpoint failed"
        else:
            raise AssertionError("discussion post-submit checkpoint 失败必须传播")

        assert opencode.cancelled.is_set()
        assert opencode.closed.is_set()
        await asyncio.sleep(0)
        assert [message.speaker for message in orch.history] == ["user"]
        assert len(kimi.prompts) == len(opencode.prompts) == 1
        assert moderator.last_prompt is None
        store.set_agent_state = original  # type: ignore[method-assign]
        await orch.aclose()

    try:
        asyncio.run(run())
    finally:
        room.cleanup()
    print("ok  discussion round fatal checkpoint 取消 sibling 且不进 moderator")


def test_acp_prompt_remote_error_commits_no_replay_cursor() -> None:
    """A drained prompt remote error fails visibly but is never replayed."""
    room = _Room()
    _set_fake_env(room, FAKE_ACP_FAIL_PROMPT="1")

    async def run() -> None:
        orch, adapter = _make_acp_orch(room)
        events = []
        # 普通 agent 失败被包容：dispatch 不抛，但 history 诚实记录
        await orch.dispatch("@kimi fast round",
                            lambda n, e: events.append(e))
        assert any(e.kind == "error" for e in events)
        assert "调用失败" in orch.history[-1].text
        assert orch._cursors["kimi"] == 1
        state = room.open_store().get_agent_state("kimi")
        assert state["cursor"] == 1
        assert state["session_id"] == "fake-session-1"  # checkpoint 已提交
        await orch.aclose()

        # A fresh runtime may continue with later input, but must not resend
        # the prompt whose stdio drain already completed before remote error.
        os.environ.pop("FAKE_ACP_FAIL_PROMPT", None)
        orch2, _adapter2 = _make_acp_orch(room)
        await orch2.dispatch("@kimi second round", lambda _n, _e: None)
        prompt_log = _fake_state_raw(room)
        assert prompt_log.count("fast round") == 1, prompt_log
        assert prompt_log.count("second round") == 1, prompt_log
        assert orch2._cursors["kimi"] > 1
        await orch2.aclose()

    try:
        asyncio.run(run())
    finally:
        _clear_fake_env()
        room.cleanup()
    print("ok  ACP prompt remote error（cursor no-replay + 只发送一次）")


def test_acp_inactivity_timeout_commits_no_replay_cursor() -> None:
    """prompt 已开始后的静默超时属于结果不确定：失败可见，但不得重投。"""
    room = _Room()
    _set_fake_env(room)

    async def run() -> None:
        adapter = AcpAdapter(
            "kimi",
            [sys.executable, SERVER],
            inactivity_timeout=0.08,
            cancel_timeout=0.05,
        )
        orch = room.make_orch(kimi=adapter)
        events = []
        outcome = await orch.dispatch(
            "@kimi slow-never task",
            lambda name, event: events.append((name, event)),
        )
        assert outcome.failures and outcome.failures[0].agent == "kimi"
        assert "无活动" in outcome.failures[0].error
        assert any(event.kind == "error" for _, event in events)

        # user seq=1 已经送入原生 ACP session；结果不确定时固化 no-replay。
        state = room.open_store().get_agent_state("kimi")
        assert state["cursor"] == 1
        assert orch._cursors["kimi"] == 1
        assert "开始了" in orch.history[-1].text
        assert "调用失败" in orch.history[-1].text

        # 下一轮不得再次发送 slow-never；否则工具副作用可能重复。
        outcome2 = await orch.dispatch(
            "@kimi fast round", lambda _name, _event: None)
        assert not outcome2.failures
        raw = _fake_state_raw(room)
        assert raw.count("prompt:") == 2
        assert "slow-never task" in raw
        latest_prompt = raw[raw.rindex("prompt:"):]
        assert "slow-never task" not in latest_prompt
        assert "fast round" in latest_prompt
        await orch.aclose()

    try:
        asyncio.run(run())
    finally:
        _clear_fake_env()
        room.cleanup()
    print("ok  ACP inactivity 超时固化 no-replay cursor")


# ---- TUI 恢复显示 ----

def test_tui_restore_display() -> None:
    """mount 时按 seq 渲染持久化 history（只渲染），就绪行存在，
    mount/unmount 后 timeline 不重复 append。"""
    from textual.widgets import RichLog
    from main import ChatApp

    room = _Room()
    store = room.open_store()
    store.append("user", "恢复我")
    store.append("kimi", "已恢复的回复")

    async def run() -> None:
        orch = Orchestrator(room.workdir, store=room.open_store())
        assert [(m.seq, m.speaker) for m in orch.history] == [
            (1, "user"), (2, "kimi")]
        # workdir 不一致的注入必须 fail loudly，不能静默换房
        with tempfile.TemporaryDirectory() as other:
            _raises(WorkdirMismatchError,
                    lambda: ChatApp(workdir=other, orchestrator=orch))

        app = ChatApp(workdir=room.workdir, orchestrator=orch)
        async with app.run_test() as pilot:
            await pilot.pause()
            text = "\n".join(str(line.text)
                             for line in app.query_one(RichLog).lines)
            assert "[user] 恢复我" in text
            assert "[kimi] 已恢复的回复" in text
            # 顺序按 seq：user 在前，kimi 在后；恢复提示与就绪行都在
            assert text.index("[user] 恢复我") < text.index("[kimi] 已恢复的回复")
            assert "已恢复 2 条历史消息" in text
            assert "聊天室已就绪" in text

    try:
        asyncio.run(run())
        # mount/unmount 之后 timeline 仍恰好两条：恢复显示不产生 append
        final = room.open_store().read(0, limit=10)
        assert len(final["items"]) == 2
    finally:
        room.cleanup()
    print("ok  TUI 恢复显示（按 seq 渲染 + 不重复 append）")


def test_tui_ctrl_n_requests_fresh_named_session() -> None:
    """Ctrl+N 在 App 内创建稳定 selector 与“新会话”展示标题。"""
    from main import ChatApp

    room = _Room()
    existing = RoomStore(
        room.workdir, state_root=room.state_root, session_name="existing")
    existing.append("user", "保留我")

    async def run() -> None:
        orch = Orchestrator(room.workdir, store=room.open_store())
        app = ChatApp(workdir=room.workdir, orchestrator=orch)

        async with app.run_test() as pilot:
            old_id = app.session_manager.active_session_id
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            created = app.session_manager.snapshot()
            assert created.summary.room_id != old_id
            assert created.summary.title == "新会话"
            assert created.summary.session_name.startswith("chat-")

    try:
        asyncio.run(run())
        assert [item.text for item in existing.read()["items"]] == ["保留我"]
    finally:
        room.cleanup()
    print("ok  TUI Ctrl+N App 内创建稳定新会话")


def test_tui_slash_new_is_local_command() -> None:
    """`/new` 与 Ctrl+N 同义，绝不写 timeline 或交给 host。"""
    from textual.widgets import Input
    from main import ChatApp

    room = _Room()

    class GuardHost(FakeJsonl):
        async def decide(
            self, transcript: str, workdir: str, on_event=None, *, choices=None,
        ):
            raise AssertionError("/new 不得到达 host")

    async def run() -> None:
        orch = room.make_orch(kimi=StatefulFake("kimi"))
        host = GuardHost("host")
        orch.host = host
        orch.adapters["host"] = host
        app = ChatApp(workdir=room.workdir, orchestrator=orch)

        async with app.run_test() as pilot:
            old_id = app.session_manager.active_session_id
            box = app.query_one(Input)
            box.value = "/new"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert app.session_manager.active_session_id != old_id
            assert orch.history == []

    try:
        asyncio.run(run())
        assert room.open_store().read()["items"] == []
    finally:
        room.cleanup()
    print("ok  TUI /new 本地命令（不进 timeline、不调用 host）")


# ---- 持久确认时序与 close 竞态 ----

def test_tui_user_append_failure_shows_error() -> None:
    """用户消息落盘失败：RichLog 不出现该用户文本，出现红色持久化错误。"""
    from textual.widgets import Input, RichLog
    from main import ChatApp

    room = _Room()

    async def run() -> None:
        orch = Orchestrator(room.workdir, store=room.open_store())
        store = orch.store
        assert store is not None

        def boom(*args, **kwargs):
            raise OSError("模拟用户消息落盘失败")
        store.append = boom  # type: ignore[method-assign]

        app = ChatApp(workdir=room.workdir, orchestrator=orch)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(Input)
            box.value = "这条不该出现"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            text = "\n".join(str(line.text)
                             for line in app.query_one(RichLog).lines)
            assert "这条不该出现" not in text   # 未持久确认就不显示
            assert "派发失败" in text            # 持久化错误如实显示
            assert "落盘失败" in text

    try:
        asyncio.run(run())
        assert room.open_store().read()["items"] == []  # timeline 无残留
    finally:
        room.cleanup()
    print("ok  TUI 用户 append 失败（不显示文本 + 显示持久化错误）")


def test_agent_reply_append_failure_no_done() -> None:
    """agent 最终 append 失败：事件里有 text chunk 但无 done，dispatch 抛。"""
    room = _Room()

    async def run() -> None:
        kimi = StatefulFake("kimi")
        orch = room.make_orch(kimi=kimi)
        store = orch.store
        assert store is not None
        orig_append = store.append

        def selective(speaker, text, command_id=None):
            if speaker != "user":  # 用户消息照常，agent 回复落盘失败
                raise OSError("模拟回复落盘失败")
            return orig_append(speaker, text, command_id=command_id)
        store.append = selective  # type: ignore[method-assign]

        events = []
        try:
            await orch.dispatch("@kimi 你好",
                                lambda n, e: events.append((n, e)))
            raise AssertionError("回复 append 失败必须向 dispatch 传播")
        except OSError:
            pass
        kinds = [(n, e.kind) for n, e in events]
        assert ("user", "committed") in kinds      # 用户消息已确认
        assert ("kimi", "text") in kinds           # text chunk 已流出
        assert ("kimi", "done") not in kinds       # 但绝不能发 done
        assert kimi.prompts  # adapter 确实执行过
        await orch.aclose()

    try:
        asyncio.run(run())
    finally:
        room.cleanup()
    print("ok  agent 回复 append 失败（有 text 无 done + dispatch 抛）")


def test_queued_dispatch_aborted_on_close() -> None:
    """排队等 delivery lock 的同 agent dispatch：aclose 后拿到锁即放弃——
    不第二次调用 adapter（无 ACP 重启），抛 OrchestratorClosedError。"""
    room = _Room()
    _set_fake_env(room)

    async def run() -> None:
        orch, adapter = _make_acp_orch(room)
        noop = lambda n, e: None
        t1 = asyncio.create_task(orch.dispatch("@kimi slow", noop))
        # 等第一轮 prompt 真正到达 server（挂起在 slow）
        for _ in range(100):
            if "prompt:" in _fake_state_raw(room):
                break
            await asyncio.sleep(0.05)
        assert "prompt:" in _fake_state_raw(room)

        t2 = asyncio.create_task(orch.dispatch("@kimi fast round", noop))
        await asyncio.sleep(0.2)  # t2 已提交用户消息，排队等 delivery lock

        close_task = asyncio.create_task(orch.aclose())
        await asyncio.sleep(0.1)
        t1.cancel()  # 活跃轮按取消契约收尾，释放 adapter/delivery 锁
        with contextlib.suppress(asyncio.CancelledError):
            await t1
        await close_task

        try:
            await t2
            raise AssertionError("排队中的 dispatch 应抛 OrchestratorClosedError")
        except OrchestratorClosedError:
            pass
        raw = _fake_state_raw(room)
        assert raw.count("prompt:") == 1  # 第二轮没有 prompt
        assert raw.count("new:") == 1     # 没有 ACP 重启（无第二次 session/new）
        assert "load:" not in raw
        assert adapter._started is False

    try:
        asyncio.run(run())
    finally:
        _clear_fake_env()
        room.cleanup()
    print("ok  排队 dispatch 在 aclose 后放弃（无二次调用 + 抛 Closed）")


def test_dispatch_after_close_rejected() -> None:
    """aclose 后新 dispatch 直接拒绝：不写 timeline；aclose 幂等。"""
    room = _Room()

    async def run() -> None:
        orch = room.make_orch(kimi=StatefulFake("kimi"))
        await orch.aclose()
        await orch.aclose()  # 幂等
        try:
            await orch.dispatch("@kimi 你好", lambda n, e: None)
            raise AssertionError("closed 后 dispatch 应抛 OrchestratorClosedError")
        except OrchestratorClosedError:
            pass
        assert orch.history == []

    try:
        asyncio.run(run())
        assert room.open_store().read()["items"] == []  # timeline 零写入
    finally:
        room.cleanup()
    print("ok  aclose 后 dispatch 拒绝（不写 timeline + aclose 幂等）")


# ---- 房间单写者 lease ----

def test_lease_same_process_conflict() -> None:
    """同进程第二 owner 构造即抛 RoomBusyError；失败的构造不修改状态；
    第一 owner aclose 后第二可获取。"""
    room = _Room()
    orch1 = room.make_orch()
    try:
        # 同进程第二个 persistent Orchestrator：构造即拒绝
        _raises(RoomBusyError, lambda: room.make_orch())
        # 失败的构造没有修改任何状态：timeline 仍为空
        assert room.open_store().read()["items"] == []
        # 第一 owner 未关闭时正常工作
        orch1._append_message("user", "第一 owner 的消息")
        asyncio.run(orch1.aclose())
        # 第一 aclose 后第二可获取，历史完整
        orch2 = room.make_orch()
        assert [m.text for m in orch2.history] == ["第一 owner 的消息"]
        asyncio.run(orch2.aclose())
    finally:
        room.cleanup()
    print("ok  lease 同进程冲突（BusyError + aclose 后可获取）")


def test_lease_cross_process() -> None:
    """子进程真正持锁时父进程获取失败；子进程正常/异常退出后父进程可获取。"""
    room = _Room()
    repo = Path(__file__).resolve().parent.parent
    child_src = (
        "import sys, time;"
        f"sys.path.insert(0, {str(repo)!r});"
        "from storage.store import RoomStore;"
        f"s = RoomStore({room.workdir!r}, state_root={str(room.state_root)!r});"
        "lease = s.acquire_owner();"  # 必须持有引用，否则 __del__ 立即 release
        "print('acquired', flush=True);"
        "time.sleep(float(sys.argv[1]))"
    )

    def spawn(hold_seconds: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", child_src, hold_seconds],
            stdout=subprocess.PIPE, text=True)

    try:
        # 异常退出（SIGKILL）：持锁中父进程失败；kill 后 OS 释放，可获取
        proc = spawn("30")
        try:
            assert proc.stdout.readline().strip() == "acquired"
            _raises(RoomBusyError, lambda: room.make_orch())
        finally:
            proc.kill()
            proc.wait()
        orch = room.make_orch()  # 异常退出后父进程可获取
        asyncio.run(orch.aclose())

        # 正常退出：持锁中父进程失败；退出后可获取
        proc2 = spawn("0.3")
        assert proc2.stdout.readline().strip() == "acquired"
        _raises(RoomBusyError, lambda: room.make_orch())
        assert proc2.wait() == 0
        orch2 = room.make_orch()
        asyncio.run(orch2.aclose())
    finally:
        room.cleanup()
    print("ok  lease 跨进程（持锁拒绝 + 正常/异常退出后可获取）")


def test_stale_owner_lock_not_blocking() -> None:
    """stale owner.lock 不阻塞获取；lock 文件 0600；release 不删除文件。"""
    room = _Room()
    store = room.open_store()
    lock_path = store.room_dir / "owner.lock"
    lock_path.write_text("999999")  # 无 flock 的 stale 文件（进程早已退出）
    os.chmod(lock_path, 0o600)
    try:
        orch = room.make_orch()  # stale 文件不妨碍获取
        orch._append_message("user", "ok")
        mode = stat.S_IMODE(os.stat(lock_path).st_mode)
        assert mode == 0o600, oct(mode)
        # 当前 owner 的 PID 已写入（供冲突提示）
        assert lock_path.read_text().strip() == str(os.getpid())
        asyncio.run(orch.aclose())
        # release 不删除 owner.lock：删除已锁 inode 会产生双锁竞态
        assert lock_path.exists()
    finally:
        room.cleanup()
    print("ok  stale owner.lock 不阻塞（0600 + release 不删文件）")


if __name__ == "__main__":
    test_restart_restores_timeline()
    test_stateful_cursor_survives_restart()
    test_cursor_ahead_of_timeline_fails()
    test_user_append_failure_atomic()
    test_checkpoint_failure_resends()
    test_jsonl_restart_sees_history()
    test_acp_load_success_keeps_cursor()
    test_acp_load_failure_resets_cursor()
    test_acp_no_load_capability_resets_cursor()
    test_acp_reconnect_load_keeps_cursor()
    test_acp_checkpoint_failure_no_commit()
    test_acp_post_submit_cursor_failure_is_fatal_no_replay()
    test_uncertain_checkpoint_failure_is_fatal_no_replay()
    test_cancelled_checkpoint_failure_is_fatal_no_replay()
    test_event_callback_failure_closes_agent_streams()
    test_acp_event_callback_failure_releases_writer_lock()
    test_fanout_checkpoint_fatal_cancels_and_awaits_sibling()
    test_discussion_checkpoint_fatal_cancels_and_awaits_round_sibling()
    test_acp_prompt_remote_error_commits_no_replay_cursor()
    test_acp_inactivity_timeout_commits_no_replay_cursor()
    test_tui_restore_display()
    test_tui_ctrl_n_requests_fresh_named_session()
    test_tui_slash_new_is_local_command()
    test_tui_user_append_failure_shows_error()
    test_agent_reply_append_failure_no_done()
    test_queued_dispatch_aborted_on_close()
    test_dispatch_after_close_rejected()
    test_lease_same_process_conflict()
    test_lease_cross_process()
    test_stale_owner_lock_not_blocking()
    print("\nM2.5 全部通过")

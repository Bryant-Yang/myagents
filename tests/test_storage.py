"""storage 持久化模块测试（ADR-0001 §2.1、§2.2）。

只用 tempfile 注入 state_root，不触碰真实 XDG 目录，不调用外部 agent。
运行：.venv/bin/python tests/test_storage.py
"""

import json
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import (
    DEFAULT_SESSION_NAME,
    DEFAULT_READ_LIMIT,
    MAX_READ_LIMIT,
    MAX_RECORD_BYTES,
    MAX_TEXT_BYTES,
    CorruptedStorageError,
    LimitExceededError,
    RoomBusyError,
    RoomStore,
    SchemaVersionError,
    WorkdirMismatchError,
    normalize_workdir,
    normalize_session_name,
    room_id_for,
)


def make_env():
    """临时 workdir + 临时 state_root，互不重叠。"""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    workdir = root / "workdir"
    workdir.mkdir()
    state_root = root / "state"
    return tmp, workdir, state_root


def open_store(workdir, state_root) -> RoomStore:
    return RoomStore(workdir, state_root=state_root)


def test_room_identity():
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        normalized = normalize_workdir(workdir)
        assert store.workdir == normalized
        assert store.room_name == workdir.name
        assert store.room_id == room_id_for(normalized)
        assert normalized not in store.room_id  # room_id 不含可逆路径
        # 同一 workdir 再次打开 → 同一 room_id / room_dir
        again = open_store(workdir, state_root)
        assert again.room_id == store.room_id
        assert again.room_dir == store.room_dir
        # room dir 在 state_root 下，不在 workdir 下
        assert str(store.room_dir).startswith(str(state_root.resolve()))
    print("ok  room 身份与稳定 room_id")


def test_named_session_identity_isolation_and_validation():
    """同一 workdir 的命名会话完全隔离；default 保持旧 room_id。"""
    tmp, workdir, state_root = make_env()
    with tmp:
        normalized = normalize_workdir(workdir)
        legacy_id = hashlib.sha256(
            normalized.encode("utf-8")).hexdigest()[:16]
        default = RoomStore(workdir, state_root=state_root)
        alpha = RoomStore(
            workdir, state_root=state_root, session_name="alpha")
        beta = RoomStore(
            workdir, state_root=state_root, session_name="beta")

        assert default.session_name == DEFAULT_SESSION_NAME
        assert default.room_id == legacy_id
        assert room_id_for(normalized) == legacy_id
        assert room_id_for(normalized, DEFAULT_SESSION_NAME) == legacy_id
        assert len({default.room_id, alpha.room_id, beta.room_id}) == 3
        assert RoomStore.session_exists(
            workdir, "alpha", state_root=state_root)
        assert not RoomStore.session_exists(
            workdir, "missing", state_root=state_root)

        alpha.append("user", "alpha only")
        alpha.set_agent_state("kimi", cursor=1, session_id="alpha-session")
        beta.append("user", "beta only")
        assert [item.text for item in alpha.read()["items"]] == ["alpha only"]
        assert [item.text for item in beta.read()["items"]] == ["beta only"]
        assert default.read()["items"] == []

        reopened = RoomStore(
            workdir, state_root=state_root, session_name="alpha")
        assert reopened.get_agent_state("kimi") == {
            "cursor": 1, "session_id": "alpha-session"}
        state = json.loads(reopened.state_path.read_text(encoding="utf-8"))
        assert state["session_name"] == "alpha"

        for bad in ("", "   ", ".", "..", "a/b", "a\\b", "bad\nname",
                    "x" * 65):
            try:
                normalize_session_name(bad)
                raise AssertionError(f"非法 session name 应拒绝：{bad!r}")
            except ValueError:
                pass

        state["session_name"] = "beta"
        reopened.state_path.write_text(
            json.dumps(state), encoding="utf-8")
        try:
            RoomStore(workdir, state_root=state_root, session_name="alpha")
            raise AssertionError("state 中 session_name 不匹配应拒绝")
        except CorruptedStorageError:
            pass
    print("ok  命名会话身份隔离 + default 兼容 + 名称校验")


def test_append_and_restart_seq():
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        r1 = store.append("user", "你好")
        r2 = store.append("kimi", "收到", command_id="cmd-1")
        assert (r1.seq, r2.seq) == (1, 2)
        assert r1.created_at.endswith("Z")  # UTC ISO
        assert r2.command_id == "cmd-1"
        assert r1.command_id is None

        # 模拟重启：新建实例，seq 必须单调继续，不重置
        reopened = open_store(workdir, state_root)
        r3 = reopened.append("user", "重启后的消息")
        assert r3.seq == 3
        page = reopened.read()
        assert [r.seq for r in page["items"]] == [1, 2, 3]
        assert page["has_more"] is False
        assert page["next_after_seq"] == 3
    print("ok  重启后 seq 单调续接")


def test_pagination():
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        for i in range(120):
            store.append("user", f"msg {i}")

        page1 = store.read()  # 默认 50
        assert len(page1["items"]) == DEFAULT_READ_LIMIT == 50
        assert page1["has_more"] is True
        assert page1["next_after_seq"] == 50
        assert page1["items"][0].seq == 1

        page2 = store.read(after_seq=page1["next_after_seq"], limit=1000)
        # limit 被夹到最大 200，但这里只剩 70 条
        assert len(page2["items"]) == 70
        assert page2["has_more"] is False
        assert page2["next_after_seq"] == 120

        # 最大 limit 夹取：制造 300 条验证上界
        for i in range(180):
            store.append("user", f"extra {i}")
        page3 = store.read(after_seq=120, limit=99999)
        assert len(page3["items"]) == 180  # 剩余不足 200，全部返回
        assert page3["has_more"] is False
        assert page3["next_after_seq"] == 300
        page4 = store.read(after_seq=0, limit=99999)
        assert len(page4["items"]) == MAX_READ_LIMIT
        assert page4["has_more"] is True
        assert page4["next_after_seq"] == MAX_READ_LIMIT

        # 空页：after_seq 越过后不丢边界
        empty = store.read(after_seq=10_000)
        assert empty["items"] == []
        assert empty["has_more"] is False
        assert empty["next_after_seq"] == 10_000
    print("ok  pagination 边界与 limit 夹取")


def test_size_limits():
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        ok = "字" * (MAX_TEXT_BYTES // 3)  # 恰好在上限内的 UTF-8 文本
        store.append("user", ok)
        try:
            store.append("user", "x" * (MAX_TEXT_BYTES + 1))
            raise AssertionError("超限文本应抛 LimitExceededError")
        except LimitExceededError:
            pass
        # 超限 append 不消耗 seq
        nxt = store.append("user", "after overflow")
        assert nxt.seq == 2
        try:
            store.append("", "no speaker")
            raise AssertionError("空 speaker 应抛 ValueError")
        except ValueError:
            pass
    print("ok  文本/记录大小上限")


def test_corrupted_timeline():
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        store.append("user", "hello")
        # 追加一行垃圾：重开必须 fail loudly，绝不覆盖
        with open(store.timeline_path, "a", encoding="utf-8") as f:
            f.write("this is not json\n")
        try:
            open_store(workdir, state_root)
            raise AssertionError("损坏的 timeline 应抛 CorruptedStorageError")
        except CorruptedStorageError:
            pass
        # 损坏文件必须原样保留，没有被覆盖
        content = store.timeline_path.read_text(encoding="utf-8")
        assert "this is not json" in content and "hello" in content

        # seq 回退也算损坏
        tmp2, workdir2, state_root2 = make_env()
        with tmp2:
            s2 = open_store(workdir2, state_root2)
            s2.append("user", "a")
            s2.append("user", "b")
            lines = s2.timeline_path.read_text(encoding="utf-8").splitlines()
            bad = json.loads(lines[1])
            bad["seq"] = 1  # 非严格递增
            lines[1] = json.dumps(bad)
            s2.timeline_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                open_store(workdir2, state_root2)
                raise AssertionError("seq 回退应抛 CorruptedStorageError")
            except CorruptedStorageError:
                pass
    print("ok  timeline 损坏 fail loudly 且不覆盖")


def test_corrupted_state():
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        store.append("user", "hello")
        store.state_path.write_text("{broken json", encoding="utf-8")
        try:
            open_store(workdir, state_root)
            raise AssertionError("损坏的 state.json 应抛 CorruptedStorageError")
        except CorruptedStorageError:
            pass
        assert store.state_path.read_text(encoding="utf-8") == "{broken json"
    print("ok  state.json 损坏 fail loudly")


def test_unsupported_schema():
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        data = json.loads(store.state_path.read_text(encoding="utf-8"))
        data["schema"] = 999
        store.state_path.write_text(json.dumps(data), encoding="utf-8")
        try:
            open_store(workdir, state_root)
            raise AssertionError("不支持的 schema 应抛 SchemaVersionError")
        except SchemaVersionError:
            pass
    print("ok  不支持的 schema fail loudly")


def test_workdir_mismatch():
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        data = json.loads(store.state_path.read_text(encoding="utf-8"))
        data["workdir"] = "/somewhere/else"
        store.state_path.write_text(json.dumps(data), encoding="utf-8")
        try:
            open_store(workdir, state_root)
            raise AssertionError("workdir 不匹配应抛 WorkdirMismatchError")
        except WorkdirMismatchError:
            pass
    print("ok  workdir 不匹配 fail loudly")


def test_permissions():
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        store.append("user", "hello")
        store.set_agent_state("kimi", cursor=1, session_id="sess-1")

        def mode(p: Path) -> int:
            return stat.S_IMODE(os.stat(p).st_mode)

        assert mode(store.room_dir) == 0o700, oct(mode(store.room_dir))
        assert mode(store.state_path) == 0o600, oct(mode(store.state_path))
        assert mode(store.timeline_path) == 0o600, oct(mode(store.timeline_path))
        # 重开后权限仍被强制为规范值
        os.chmod(store.room_dir, 0o755)
        reopened = open_store(workdir, state_root)
        assert mode(reopened.room_dir) == 0o700
    print("ok  目录/文件权限")


def test_workdir_no_pollution():
    tmp, workdir, state_root = make_env()
    with tmp:
        before = set(workdir.iterdir())
        store = open_store(workdir, state_root)
        store.append("user", "hello")
        store.set_agent_state("kimi", cursor=1)
        store.read()
        after = set(workdir.iterdir())
        assert before == after, f"workdir 被污染：{after - before}"
    print("ok  workdir 无污染")


def test_atomic_state_roundtrip():
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        assert store.get_agent_state("kimi") == {"cursor": 0, "session_id": None}
        store.set_agent_state("kimi", cursor=7, session_id="sess-abc")
        store.set_agent_state("codex", cursor=3)

        # 重启后 cursor/session 映射完整恢复
        reopened = open_store(workdir, state_root)
        assert reopened.get_agent_state("kimi") == {
            "cursor": 7, "session_id": "sess-abc"}
        assert reopened.get_agent_state("codex") == {
            "cursor": 3, "session_id": None}

        # 原子写不留下临时文件
        leftovers = [p.name for p in reopened.room_dir.iterdir()
                     if p.name.startswith(".state.")]
        assert leftovers == [], f"原子写残留临时文件：{leftovers}"
        # state.json 内容合法且字段齐全
        data = json.loads(reopened.state_path.read_text(encoding="utf-8"))
        assert data["schema"] == 1
        assert data["workdir"] == reopened.workdir
        assert data["agents"]["kimi"]["cursor"] == 7
    print("ok  state 原子写 roundtrip")


def test_missing_state_not_overwritten():
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        store.append("user", "hello")
        store.state_path.unlink()  # 模拟 state.json 丢失
        try:
            open_store(workdir, state_root)
            raise AssertionError("缺少 state.json 的非空房间应抛 CorruptedStorageError")
        except CorruptedStorageError:
            pass
        # timeline 仍在，没有被清空重开
        assert "hello" in store.timeline_path.read_text(encoding="utf-8")
    print("ok  半初始化房间不静默覆盖")


def test_set_agent_state_write_failure_no_fake_commit():
    """写盘失败时内存与磁盘都必须保持旧值（copy-on-write，不假提交）。"""
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        store.set_agent_state("kimi", cursor=3, session_id="sess-old")

        def boom(_data):
            raise OSError("磁盘满了（模拟）")

        store._write_state = boom  # monkeypatch 写失败
        try:
            store.set_agent_state("kimi", cursor=9, session_id="sess-new")
            raise AssertionError("写失败应抛异常")
        except OSError:
            pass
        # 内存可见状态仍是旧值
        assert store.get_agent_state("kimi") == {
            "cursor": 3, "session_id": "sess-old"}
        # 重开（读磁盘）也是旧值
        reopened = open_store(workdir, state_root)
        assert reopened.get_agent_state("kimi") == {
            "cursor": 3, "session_id": "sess-old"}
    print("ok  set_agent_state 写失败不假提交")


def test_corrupted_agent_entry_raises():
    """agents[name] 损坏：打开时全量校验，构造即抛 CorruptedStorageError。"""
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        store.set_agent_state("kimi", cursor=5)
        data = json.loads(store.state_path.read_text(encoding="utf-8"))
        data["agents"]["kimi"] = "garbage"  # 损坏为非 object
        store.state_path.write_text(json.dumps(data), encoding="utf-8")

        try:
            open_store(workdir, state_root)
            raise AssertionError("损坏的 agent entry 应抛 CorruptedStorageError")
        except CorruptedStorageError:
            pass
        # 损坏内容不被覆盖
        assert "garbage" in store.state_path.read_text(encoding="utf-8")
        # 干净房间里未记录的 key 仍返回默认值
        tmp2, workdir2, state_root2 = make_env()
        with tmp2:
            clean = open_store(workdir2, state_root2)
            assert clean.get_agent_state("codex") == {
                "cursor": 0, "session_id": None}
    print("ok  agent entry 损坏 fail loudly（构造即抛）")


def test_agent_entry_field_validation():
    """缺 cursor / 缺 session_id / 负 cursor / 空 session id：
    构造阶段全部 fail loudly，不静默 bootstrap。"""
    bad_entries = [
        {"session_id": "sess-1"},              # 缺 cursor
        {"cursor": 1},                         # 缺 session_id
        {"cursor": -1, "session_id": None},    # 负 cursor
        {"cursor": 0, "session_id": ""},       # 空 session id
        {"cursor": True, "session_id": None},  # bool cursor
    ]
    for bad in bad_entries:
        tmp, workdir, state_root = make_env()
        with tmp:
            store = open_store(workdir, state_root)
            data = json.loads(store.state_path.read_text(encoding="utf-8"))
            data["agents"]["kimi"] = bad
            store.state_path.write_text(json.dumps(data), encoding="utf-8")
            try:
                open_store(workdir, state_root)
                raise AssertionError(f"应拒绝损坏 entry：{bad}")
            except CorruptedStorageError:
                pass
    print("ok  agent entry 字段校验（缺字段/负 cursor/空 session id）")


def test_relative_xdg_state_home_resolved():
    """相对 XDG_STATE_HOME 必须 resolve 后再做 workdir 重叠判断：
    指向 workdir 子目录时拒绝，不能绕过。"""
    from storage import StorageError
    tmp = tempfile.TemporaryDirectory(dir=Path.cwd())  # 相对路径相对 cwd
    old_xdg = os.environ.get("XDG_STATE_HOME")
    try:
        with tmp:
            workdir = Path(tmp.name) / "workdir"
            workdir.mkdir()
            # 相对路径指向 workdir 子目录：resolve 后与 workdir 重叠
            rel_xdg = os.path.relpath(workdir / "xdg", Path.cwd())
            assert not os.path.isabs(rel_xdg)
            os.environ["XDG_STATE_HOME"] = rel_xdg
            try:
                RoomStore(workdir)  # 不传 state_root，走默认根
                raise AssertionError("相对 XDG 指向 workdir 子目录应抛 StorageError")
            except StorageError:
                pass
            assert not (workdir / "xdg").exists()  # 拒绝时没留下任何目录
    finally:
        if old_xdg is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = old_xdg
    print("ok  相对 XDG_STATE_HOME resolve 后重叠判断不绕过")


def test_timeline_semantic_corruption():
    """逐项：seq<=0、空 speaker、非法/非 UTC-Z created_at、超限 text、
    超限整行记录都必须被拒绝，且损坏内容不被覆盖。"""
    good = {"seq": 1, "speaker": "user", "text": "hi",
            "created_at": "2026-07-26T08:00:00Z", "command_id": None}
    bad_lines = [
        {**good, "seq": 0},
        {**good, "seq": -3},
        {**good, "speaker": ""},
        {**good, "created_at": "not-a-time"},
        {**good, "created_at": "2026-07-26T08:00:00+08:00"},  # 非 UTC-Z
        {**good, "created_at": "2026-07-26 08:00:00Z"},       # 缺 T 分隔
        {**good, "text": "x" * (MAX_TEXT_BYTES + 1)},
    ]
    for bad in bad_lines:
        tmp, workdir, state_root = make_env()
        with tmp:
            store = open_store(workdir, state_root)
            line = json.dumps(bad, ensure_ascii=False)
            with open(store.timeline_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            try:
                open_store(workdir, state_root)
                raise AssertionError(f"应拒绝损坏记录：{bad}")
            except CorruptedStorageError:
                pass
            assert line in store.timeline_path.read_text(encoding="utf-8")

    # 超限整行记录（JSON 合法但行本身超长）
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        fat = json.dumps({**good, "command_id": "c" * MAX_RECORD_BYTES})
        with open(store.timeline_path, "a", encoding="utf-8") as f:
            f.write(fat + "\n")
        try:
            open_store(workdir, state_root)
            raise AssertionError("应拒绝超限整行记录")
        except CorruptedStorageError:
            pass
        assert fat in store.timeline_path.read_text(encoding="utf-8")
    print("ok  timeline 逐项语义校验（损坏不覆盖）")


def test_permissions_restored_on_reopen():
    """已有文件被改成 0644 后，重开必须强制恢复 0600。"""
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        store.append("user", "hello")
        store.set_agent_state("kimi", cursor=1)
        os.chmod(store.state_path, 0o644)
        os.chmod(store.timeline_path, 0o644)

        def mode(p: Path) -> int:
            return stat.S_IMODE(os.stat(p).st_mode)

        reopened = open_store(workdir, state_root)
        assert mode(reopened.state_path) == 0o600, oct(mode(reopened.state_path))
        assert mode(reopened.timeline_path) == 0o600, \
            oct(mode(reopened.timeline_path))
    print("ok  重开强制恢复 0600")


def test_state_root_inside_workdir_rejected():
    """state_root 等于 workdir 或位于其子目录：拒绝；父目录/兄弟目录可接受。"""
    tmp, workdir, state_root = make_env()
    with tmp:
        from storage import StorageError
        try:
            RoomStore(workdir, state_root=workdir)
            raise AssertionError("state_root == workdir 应抛 StorageError")
        except StorageError:
            pass
        try:
            RoomStore(workdir, state_root=workdir / "sub" / "state")
            raise AssertionError("state_root 在 workdir 子目录应抛 StorageError")
        except StorageError:
            pass
        assert not (workdir / "sub").exists()  # 拒绝时没有留下任何目录
        # 父目录下的兄弟目录可以接受
        ok_root = workdir.parent / "sibling-state"
        store = RoomStore(workdir, state_root=ok_root)
        store.append("user", "hi")
        assert set(workdir.iterdir()) == set()  # 且 workdir 零污染
    print("ok  state_root 与 workdir 重叠拒绝")


def test_lease_busy_error_includes_pid_hint():
    """锁冲突才抛 RoomBusyError，错误里带持有者 PID 提示。"""
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        lease = store.acquire_owner()
        try:
            store.acquire_owner()
            raise AssertionError("持锁中再次获取应抛 RoomBusyError")
        except RoomBusyError as exc:
            assert str(os.getpid()) in str(exc)  # PID hint
            assert store.workdir in str(exc)
        lease.release()
        # release 后立即可再获取；再次 release 幂等
        again = store.acquire_owner()
        again.release()
        lease.release()
        again.release()
    print("ok  lease busy 错误含 PID 提示（release 幂等）")


def test_lease_pid_write_failure_releases_lock():
    """PID write/fsync 失败：立即 unlock+close，原异常传播（非
    RoomBusyError），同一 lock 可立即重新获取，不依赖 __del__。"""
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        for target in ("write", "fsync"):
            orig = getattr(os, target)
            def boom(_fd, *args):
                raise OSError(f"模拟 {target} 失败")
            setattr(os, target, boom)
            try:
                try:
                    store.acquire_owner()
                    raise AssertionError(f"{target} 失败应抛 OSError")
                except OSError as exc:
                    assert not isinstance(exc, RoomBusyError)
            finally:
                setattr(os, target, orig)
            # lock 已同步释放：马上能重新获取
            lease = store.acquire_owner()
            lease.release()
    print("ok  lease PID 写入失败立即释放（可立即重获取）")


def test_lease_non_busy_oserror_not_rewritten():
    """open/chmod/flock 的非 busy 失败保持原异常，不改写成 RoomBusyError。"""
    import fcntl as fcntl_mod
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)

        # chmod 失败 → 原样 PermissionError
        orig_chmod = os.chmod
        def chmod_boom(_path, _mode):
            raise PermissionError("模拟 chmod 失败")
        os.chmod = chmod_boom
        try:
            try:
                store.acquire_owner()
                raise AssertionError("chmod 失败应抛 PermissionError")
            except PermissionError:
                pass
        finally:
            os.chmod = orig_chmod

        # flock 的非 busy OSError → 原样传播
        orig_flock = fcntl_mod.flock
        def flock_boom(_fd, _op):
            raise OSError("模拟 flock 非 busy 失败")
        fcntl_mod.flock = flock_boom
        try:
            try:
                store.acquire_owner()
                raise AssertionError("flock 非 busy 失败应抛 OSError")
            except OSError as exc:
                assert not isinstance(exc, RoomBusyError)
        finally:
            fcntl_mod.flock = orig_flock

        # 两次失败路径都没泄露 fd/锁：正常获取
        lease = store.acquire_owner()
        lease.release()
    print("ok  lease 非 busy OSError 不改写（chmod/flock 原样传播）")


def test_execution_event_journal_and_legacy_migration():
    """执行事件独立持久化；旧房间首次重开时安全补建 events.jsonl。"""
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        event = store.append_event(
            command_id="cmd-1", agent="kimi", kind="tool",
            text="执行 node --check")
        assert event.seq == 1
        assert event.command_id == "cmd-1"
        assert event.agent == "kimi"
        assert event.kind == "tool"
        page = store.read_events()
        assert [item.to_dict() for item in page["items"]] == [event.to_dict()]
        assert page["next_after_seq"] == 1 and page["has_more"] is False
        assert (store.events_path.stat().st_mode & 0o777) == 0o600

        # 模拟 M3 旧房间：只有合法 state/timeline，没有 events.jsonl。
        store.events_path.unlink()
        reopened = open_store(workdir, state_root)
        assert reopened.events_path.is_file()
        assert reopened.read_events()["items"] == []
        migrated = reopened.append_event(
            command_id="cmd-2", agent="system", kind="running",
            text="开始执行")
        assert migrated.seq == 1

        # 一旦文件存在，内容损坏必须 fail loudly。
        reopened.events_path.write_text("{broken\n", encoding="utf-8")
        try:
            open_store(workdir, state_root)
            raise AssertionError("损坏 events.jsonl 应拒绝打开")
        except CorruptedStorageError:
            pass
    print("ok  独立执行事件日志 + 旧房间兼容迁移 + 损坏拒绝")


def test_read_command_events_is_bounded_and_command_scoped():
    """详情读取只返回目标 command，并保留首尾证据而不是静默截尾。"""
    tmp, workdir, state_root = make_env()
    with tmp:
        store = open_store(workdir, state_root)
        assert store.read_latest_command_events(limit=4) is None
        store.append_event(
            command_id="cmd-detail", agent="system", kind="queued",
            text="进入队列")
        store.append_event(
            command_id="cmd-other", agent="system", kind="running",
            text="另一个任务")
        for index in range(5):
            store.append_event(
                command_id="cmd-detail", agent="kimi", kind="status",
                text=f"阶段 {index}")
        store.append_event(
            command_id="cmd-detail", agent="kimi", kind="completed",
            text="本轮响应结束")

        page = store.read_command_events("cmd-detail", limit=4)
        assert page["total_count"] == 7
        assert page["omitted_count"] == 3
        assert len(page["items"]) == 4
        assert all(
            item.command_id == "cmd-detail" for item in page["items"])
        assert page["items"][0].kind == "queued"
        assert page["items"][-1].kind == "completed"
        assert [item.seq for item in page["items"]] == sorted(
            item.seq for item in page["items"])
        assert page["kind_counts"] == {
            "queued": 1, "status": 5, "completed": 1}
        assert page["agents"] == ("system", "kimi")
        assert page["partial_char_count"] == 0

        latest_page = store.read_latest_command_events(limit=4)
        assert latest_page is not None
        assert latest_page["command_id"] == "cmd-detail"
        assert latest_page["items"][-1].kind == "completed"

        try:
            store.read_command_events("", limit=4)
            raise AssertionError("空 command_id 应拒绝")
        except ValueError:
            pass
    print("ok  command 详情读取隔离、有界且保留首尾证据")


if __name__ == "__main__":
    test_room_identity()
    test_named_session_identity_isolation_and_validation()
    test_append_and_restart_seq()
    test_pagination()
    test_size_limits()
    test_corrupted_timeline()
    test_corrupted_state()
    test_unsupported_schema()
    test_workdir_mismatch()
    test_permissions()
    test_workdir_no_pollution()
    test_atomic_state_roundtrip()
    test_missing_state_not_overwritten()
    test_set_agent_state_write_failure_no_fake_commit()
    test_corrupted_agent_entry_raises()
    test_agent_entry_field_validation()
    test_relative_xdg_state_home_resolved()
    test_timeline_semantic_corruption()
    test_permissions_restored_on_reopen()
    test_state_root_inside_workdir_rejected()
    test_lease_busy_error_includes_pid_hint()
    test_lease_pid_write_failure_releases_lock()
    test_lease_non_busy_oserror_not_rewritten()
    test_execution_event_journal_and_legacy_migration()
    test_read_command_events_is_bounded_and_command_scoped()
    print("\n全部通过")

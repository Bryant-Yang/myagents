"""M4.7 会话目录的公开行为验收。"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from session_catalog import SessionCatalog, SessionCatalogError
from storage.store import RoomStore


def test_catalog_lists_legacy_sessions_with_local_summaries() -> None:
    """旧 state 无展示元数据也能按当前项目发现，不需要破坏性迁移。"""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        workdir = root / "project"
        other = root / "other"
        workdir.mkdir()
        other.mkdir()
        state_root = root / "state"

        default = RoomStore(workdir, state_root=state_root)
        default.append("user", "检查 OpenCode 为什么没有输出")
        default.append("opencode", "我会检查")
        talk = RoomStore(workdir, state_root=state_root, session_name="talk")
        talk.append("user", "讨论 Qwen 本地模型")
        outside = RoomStore(other, state_root=state_root, session_name="outside")
        outside.append("user", "另一个项目")

        sessions = SessionCatalog(state_root).list_sessions(
            current_workdir=workdir,
            include_all=False,
        )

        assert [item.session_name for item in sessions] == ["talk", "default"]
        assert sessions[0].title == "talk"
        assert sessions[0].project_name == "project"
        assert sessions[0].message_count == 1
        assert sessions[0].last_user_message == "讨论 Qwen 本地模型"
        assert sessions[1].message_count == 2
        assert all(item.workdir == str(workdir.resolve()) for item in sessions)


def test_catalog_searches_visible_metadata_across_projects() -> None:
    """全部项目搜索只覆盖标题、摘要、项目名和路径。"""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        current = root / "alpha-project"
        other = root / "beta-project"
        current.mkdir()
        other.mkdir()
        state_root = root / "state"
        RoomStore(current, state_root=state_root).append("user", "普通对话")
        remote = RoomStore(
            other,
            state_root=state_root,
            session_name="model-check",
        )
        remote.append("user", "检查 Gemma 本地推理")

        catalog = SessionCatalog(state_root)
        by_message = catalog.list_sessions(
            current_workdir=current,
            include_all=True,
            query="gemma",
        )
        by_project = catalog.list_sessions(
            current_workdir=current,
            include_all=True,
            query="beta-project",
        )

        assert [item.session_name for item in by_message] == ["model-check"]
        assert [item.session_name for item in by_project] == ["model-check"]


def test_catalog_creates_stable_untitled_sessions() -> None:
    """新会话展示名与不可变 selector 分离，同一秒创建也不冲突。"""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        workdir = root / "project"
        workdir.mkdir()
        catalog = SessionCatalog(root / "state")

        first = catalog.create_session(workdir)
        second = catalog.create_session(workdir)

        assert first.title == second.title == "新会话"
        assert first.session_name.startswith("chat-")
        assert second.session_name.startswith("chat-")
        assert first.session_name != second.session_name
        assert first.room_id != second.room_id
        listed = catalog.list_sessions(current_workdir=workdir)
        assert {item.room_id for item in listed} == {first.room_id, second.room_id}


def test_catalog_renames_title_without_changing_identity() -> None:
    """重命名只改展示元数据，room/session selector 与历史保持原样。"""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        workdir = root / "project"
        workdir.mkdir()
        catalog = SessionCatalog(root / "state")
        created = catalog.create_session(workdir)
        store = RoomStore(
            workdir,
            state_root=root / "state",
            session_name=created.session_name,
        )
        store.append("user", "保留的历史")

        renamed = catalog.rename_session(created.room_id, "OpenCode 排障")

        assert renamed.title == "OpenCode 排障"
        assert renamed.room_id == created.room_id
        assert renamed.session_name == created.session_name
        reopened = RoomStore(
            workdir,
            state_root=root / "state",
            session_name=created.session_name,
        )
        assert reopened.session_title == "OpenCode 排障"
        assert [item.text for item in reopened.read()["items"]] == ["保留的历史"]


def test_catalog_delete_requires_exact_title_and_exact_room() -> None:
    """永久删除必须确认展示标题，且只删除被选择的唯一房间。"""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        workdir = root / "project"
        workdir.mkdir()
        catalog = SessionCatalog(root / "state")
        victim = catalog.create_session(workdir)
        victim = catalog.rename_session(victim.room_id, "待删除会话")
        survivor = catalog.create_session(workdir)
        survivor_dir = root / "state" / "rooms" / survivor.room_id

        try:
            catalog.delete_session(victim.room_id, confirmation="写错了")
        except ValueError as exc:
            assert "完整输入会话标题" in str(exc)
        else:
            raise AssertionError("错误确认文本不得删除会话")
        assert (root / "state" / "rooms" / victim.room_id).is_dir()

        catalog.delete_session(victim.room_id, confirmation="待删除会话")

        assert not (root / "state" / "rooms" / victim.room_id).exists()
        assert survivor_dir.is_dir()
        assert [item.room_id for item in catalog.list_sessions(
            current_workdir=workdir
        )] == [survivor.room_id]


def test_catalog_fails_loudly_for_incomplete_room_shaped_directory() -> None:
    """合法 room_id 形状的半初始化目录不能被选择器静默隐藏。"""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        workdir = root / "project"
        workdir.mkdir()
        state_root = root / "state"
        RoomStore(workdir, state_root=state_root)
        broken = state_root / "rooms" / "0123456789abcdef"
        broken.mkdir()
        (broken / "state.json").write_text("{}", encoding="utf-8")

        try:
            SessionCatalog(state_root).list_sessions(
                current_workdir=workdir
            )
        except SessionCatalogError as exc:
            assert "会话目录不完整" in str(exc)
        else:
            raise AssertionError("损坏的 room 形状目录不得被静默忽略")


if __name__ == "__main__":
    test_catalog_lists_legacy_sessions_with_local_summaries()
    test_catalog_searches_visible_metadata_across_projects()
    test_catalog_creates_stable_untitled_sessions()
    test_catalog_renames_title_without_changing_identity()
    test_catalog_delete_requires_exact_title_and_exact_room()
    test_catalog_fails_loudly_for_incomplete_room_shaped_directory()
    print("ok  会话目录兼容旧房间并生成本地摘要")

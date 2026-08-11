"""macOS 剪贴板图片与 TUI 本地粘贴命令验收。"""

from __future__ import annotations

import asyncio
import base64
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acp.client import AcpClient
from clipboard_image import (
    ClipboardImageError,
    TrustedImage,
    attachment_reference,
    capture_clipboard_png,
    prompt_images,
)
from codex_app_server.client import CodexAppServerClient
from main import ChatApp
from orchestrator import Orchestrator
from storage.store import RoomStore
from textual.widgets import Input


def _trusted_image(path: Path, root: Path | None = None) -> TrustedImage:
    attachment_root = (root or path.parent).resolve()
    resolved = path.resolve()
    return TrustedImage(
        path=resolved,
        attachment_root=attachment_root,
        relative_path=resolved.relative_to(attachment_root),
        data=resolved.read_bytes(),
    )


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


_PNG = (
    b"\x89PNG\r\n\x1a\n"
    + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    + _chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff"))
    + _chunk(b"IEND", b"")
)


def test_capture_clipboard_png_validates_and_secures_file() -> None:
    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(_PNG)
        return subprocess.CompletedProcess(command, 0, "", "")

    with TemporaryDirectory() as tmp:
        destination = Path(tmp) / "attachments"
        path = capture_clipboard_png(
            destination, run_command=fake_run, system="Darwin")
        assert path.parent.resolve() == destination.resolve()
        assert path.suffix == ".png"
        assert path.read_bytes() == _PNG
        assert os.stat(destination).st_mode & 0o777 == 0o700
        assert os.stat(path).st_mode & 0o777 == 0o600
    print("ok  剪贴板 PNG 安全落盘")


def test_capture_uses_short_monotonic_names_and_references() -> None:
    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(_PNG)
        return subprocess.CompletedProcess(command, 0, "", "")

    with TemporaryDirectory() as tmp:
        destination = Path(tmp) / "attachments"
        first = capture_clipboard_png(
            destination, run_command=fake_run, system="Darwin")
        second = capture_clipboard_png(
            destination, run_command=fake_run, system="Darwin")

        assert first.name == "img-0001.png"
        assert second.name == "img-0002.png"
        assert attachment_reference(first) == "[图片 1]"
        assert attachment_reference(second) == "[图片 2]"
        assert [image.path for image in prompt_images(
            "比较 [图片 1] 和 [图片 2]", destination
        )] == [first.resolve(), second.resolve()]

    print("ok  图片使用短编号名称和引用")


def test_capture_clipboard_png_rejects_missing_image() -> None:
    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 1, "", "clipboard does not contain PNG data")

    with TemporaryDirectory() as tmp:
        destination = Path(tmp) / "attachments"
        try:
            capture_clipboard_png(
                destination, run_command=fake_run, system="Darwin")
        except ClipboardImageError as exc:
            assert "没有可粘贴的 PNG 图片" in str(exc)
        else:
            raise AssertionError("非图片剪贴板没有被拒绝")
        assert not list(destination.glob("*.png"))
    print("ok  非图片剪贴板给出可操作错误")


def test_capture_clipboard_png_enforces_size_limit() -> None:
    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(_PNG)
        return subprocess.CompletedProcess(command, 0, "", "")

    with TemporaryDirectory() as tmp:
        destination = Path(tmp) / "attachments"
        try:
            capture_clipboard_png(
                destination,
                run_command=fake_run,
                system="Darwin",
                max_bytes=12,
            )
        except ClipboardImageError as exc:
            assert "超过" in str(exc)
        else:
            raise AssertionError("超限图片没有被拒绝")
        assert not list(destination.glob("*.png"))
    print("ok  剪贴板图片大小上限")


def test_capture_rejects_corrupt_png_and_symlink_root() -> None:
    def corrupt_run(command, **_kwargs):
        Path(command[-1]).write_bytes(
            b"\x89PNG\r\n\x1a\nfake-png-payload")
        return subprocess.CompletedProcess(command, 0, "", "")

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        destination = root / "attachments"
        try:
            capture_clipboard_png(
                destination, run_command=corrupt_run, system="Darwin")
        except ClipboardImageError as exc:
            assert "有效 PNG" in str(exc)
        else:
            raise AssertionError("损坏 PNG 没有被拒绝")
        assert not list(destination.glob("*.png"))

        outside = root / "outside"
        outside.mkdir()
        link = root / "linked-attachments"
        link.symlink_to(outside, target_is_directory=True)
        try:
            capture_clipboard_png(
                link, run_command=corrupt_run, system="Darwin")
        except ClipboardImageError as exc:
            assert "符号链接" in str(exc)
        else:
            raise AssertionError("符号链接附件根没有被拒绝")
        assert not list(outside.iterdir())
    print("ok  损坏 PNG 与符号链接附件根被拒绝")


def test_prompt_images_are_confined_to_current_room() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        attachments = root / "room" / "attachments"
        attachments.mkdir(parents=True)
        os.chmod(attachments, 0o700)
        trusted = attachments / "trusted.png"
        trusted.write_bytes(_PNG)
        os.chmod(trusted, 0o600)
        outside = root / "outside.png"
        outside.write_bytes(_PNG)
        linked = attachments / "linked.png"
        os.link(outside, linked)
        prompt = (
            f"[图片附件：{trusted}] "
            f"[图片附件：{outside}] "
            f"[图片附件：{linked}] "
            f"[图片附件：{trusted}]"
        )
        images = prompt_images(prompt, attachments)
        assert len(images) == 1
        assert images[0].path == trusted.resolve()
        assert images[0].data == _PNG
        symlink_root = root / "linked-root"
        symlink_root.symlink_to(attachments, target_is_directory=True)
        assert prompt_images(prompt, symlink_root) == ()
    print("ok  协议图片严格限制在当前房间")


def test_native_protocol_payloads_include_image() -> None:
    async def run() -> None:
        with TemporaryDirectory() as tmp:
            attachments = Path(tmp) / "attachments"
            attachments.mkdir()
            os.chmod(attachments, 0o700)
            image = attachments / "image.png"
            image.write_bytes(_PNG)
            os.chmod(image, 0o600)
            trusted = _trusted_image(image, attachments)

            acp = AcpClient(["unused"])
            acp_params = {}

            async def acp_request(method, params):
                assert method == "session/prompt"
                acp_params.update(params)
                return {"stopReason": "end_turn"}

            acp.request = acp_request
            await acp.prompt("session-1", "看图", (trusted,))
            assert acp_params["prompt"][1] == {
                "type": "image",
                "mimeType": "image/png",
                "data": base64.b64encode(_PNG).decode("ascii"),
            }

            codex = CodexAppServerClient(["unused"])
            codex_params = {}

            async def codex_request(method, params):
                assert method == "turn/start"
                codex_params.update(params)
                return {"turn": {"id": "turn-1"}}

            codex.request = codex_request
            await codex.turn_start(
                "thread-1", "看图", images=(trusted,))
            assert codex_params["input"][1] == {
                "type": "localImage",
                "path": str(image.resolve()),
            }

    asyncio.run(run())
    print("ok  ACP 与 Codex app-server 使用原生图片输入")


def test_tui_paste_image_inserts_reference_without_submitting() -> None:
    async def run() -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            workdir = root / "work"
            workdir.mkdir()
            store = RoomStore(workdir, state_root=root / "state")
            orch = Orchestrator(str(workdir), store=store)
            captured_destinations = []
            fail_capture = [False]

            def fake_capture(destination: Path) -> Path:
                captured_destinations.append(destination)
                if fail_capture[0]:
                    raise ClipboardImageError("测试图片读取失败")
                destination.mkdir(parents=True, exist_ok=True)
                image = destination / "img-0001.png"
                image.write_bytes(_PNG)
                os.chmod(image, 0o600)
                return image

            app = ChatApp(
                workdir=str(workdir),
                orchestrator=orch,
                clipboard_image_capture=fake_capture,
            )
            async with app.run_test() as pilot:
                box = app.query_one("#composer", Input)
                box.value = "@kimi 看一下 "
                box.cursor_position = len(box.value)
                box.value = "/paste-image"
                box.cursor_position = len(box.value)
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()

                assert captured_destinations == [
                    store.room_dir / "attachments"
                ]
                assert "[图片 1]" in box.value
                assert orch.history == []
                assert box.has_focus

                before = list(captured_destinations)
                app.copy_to_clipboard("普通文本")
                box.action_paste()
                assert box.value.endswith("普通文本")
                assert captured_destinations == before

                # Ctrl+V 的本地文本为空时才走图片；仍只改草稿。
                app.copy_to_clipboard("")
                box.value = "@kimi 看一下 "
                box.cursor_position = len(box.value)
                box.action_paste()
                await app.workers.wait_for_complete()
                assert box.value.startswith("@kimi 看一下 ")
                assert "[图片 1]" in box.value
                assert orch.history == []

                # 读取失败保持原草稿，不自动发送。
                fail_capture[0] = True
                draft = box.value
                app.action_paste_image()
                await app.workers.wait_for_complete()
                assert box.value == draft
                assert orch.history == []

    asyncio.run(run())
    print("ok  TUI 粘贴图片只插入附件引用、不提前提交")


if __name__ == "__main__":
    test_capture_clipboard_png_validates_and_secures_file()
    test_capture_uses_short_monotonic_names_and_references()
    test_capture_clipboard_png_rejects_missing_image()
    test_capture_clipboard_png_enforces_size_limit()
    test_capture_rejects_corrupt_png_and_symlink_root()
    test_prompt_images_are_confined_to_current_room()
    test_native_protocol_payloads_include_image()
    test_tui_paste_image_inserts_reference_without_submitting()
    print("\nClipboard image 全部通过")

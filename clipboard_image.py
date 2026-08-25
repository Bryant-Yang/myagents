"""macOS 系统剪贴板中的 PNG 图片安全落盘。"""

from __future__ import annotations

import contextlib
import os
import platform
import re
import stat
import struct
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MAX_CLIPBOARD_IMAGE_BYTES = 20 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ATTACHMENT_REFERENCE_RE = re.compile(
    r"\[图片\s+([1-9]\d*)\]|\[图片附件：([^\]\r\n]+)\]"
)
_SHORT_IMAGE_NAME_RE = re.compile(r"^img-([0-9]+)\.png$")
_APPLE_SCRIPT = r"""
on run argv
    set outputPath to item 1 of argv
    try
        set imageData to the clipboard as «class PNGf»
    on error
        error "clipboard does not contain PNG data"
    end try
    set outputFile to open for access POSIX file outputPath with write permission
    try
        set eof outputFile to 0
        write imageData to outputFile
        close access outputFile
    on error errorMessage number errorNumber
        try
            close access outputFile
        end try
        error errorMessage number errorNumber
    end try
end run
"""


class ClipboardImageError(RuntimeError):
    """剪贴板没有可用图片，或图片无法安全落盘。"""


class PromptImageBudgetError(ClipboardImageError):
    """一条 prompt 的可信图片数量或聚合字节数超过 transport 预算。"""


RunCommand = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class TrustedImage:
    """从当前房间附件目录读出的、可安全交给原生 transport 的图片。"""

    path: Path
    attachment_root: Path
    relative_path: Path
    data: bytes


def attachment_reference(path: Path) -> str:
    """把受管短文件名转换为草稿/时间线中的稳定用户引用。"""
    number = _short_image_number(Path(path).name)
    if number is None:
        raise ClipboardImageError("图片附件不是受管短编号文件")
    return f"[图片 {number}]"


def _short_image_number(name: str) -> int | None:
    match = _SHORT_IMAGE_NAME_RE.fullmatch(name)
    if match is None:
        return None
    number = int(match.group(1))
    if number < 1 or name != f"img-{number:04d}.png":
        return None
    return number


def _validate_png_data(data: bytes, info: os.stat_result, max_bytes: int) -> None:
    """校验已从同一文件描述符读取的 PNG 容器结构与 chunk CRC。"""
    if not stat.S_ISREG(info.st_mode):
        raise ClipboardImageError("剪贴板图片没有生成普通文件")
    if info.st_nlink != 1:
        raise ClipboardImageError("剪贴板图片不能是硬链接")
    if info.st_size <= len(_PNG_SIGNATURE):
        raise ClipboardImageError("剪贴板图片为空")
    if info.st_size > max_bytes:
        raise ClipboardImageError(
            f"剪贴板图片超过 {max_bytes // (1024 * 1024)} MiB 上限")
    if not data.startswith(_PNG_SIGNATURE):
        raise ClipboardImageError("剪贴板内容不是有效 PNG 图片")

    offset = len(_PNG_SIGNATURE)
    chunks = 0
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    while offset < len(data):
        if len(data) - offset < 12:
            raise ClipboardImageError("剪贴板内容不是有效 PNG 图片")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ClipboardImageError("剪贴板内容不是有效 PNG 图片")
        payload = data[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", data[end - 4:end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise ClipboardImageError("剪贴板内容不是有效 PNG 图片")
        chunks += 1
        if chunks == 1:
            if kind != b"IHDR" or length != 13:
                raise ClipboardImageError("剪贴板内容不是有效 PNG 图片")
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                width == 0
                or height == 0
                or bit_depth not in valid_depths.get(color_type, set())
                or compression != 0
                or filtering != 0
                or interlace not in {0, 1}
            ):
                raise ClipboardImageError("剪贴板内容不是有效 PNG 图片")
            saw_ihdr = True
        elif kind == b"IHDR":
            raise ClipboardImageError("剪贴板内容不是有效 PNG 图片")
        if kind == b"IDAT":
            saw_idat = True
        if kind == b"IEND":
            if length != 0 or end != len(data):
                raise ClipboardImageError("剪贴板内容不是有效 PNG 图片")
            saw_iend = True
        offset = end
    if not (saw_ihdr and saw_idat and saw_iend):
        raise ClipboardImageError("剪贴板内容不是有效 PNG 图片")


def _validate_png(path: Path, max_bytes: int) -> None:
    """校验刚刚由剪贴板写入的本地 PNG。"""
    info = path.lstat()
    if path.is_symlink():
        raise ClipboardImageError("剪贴板图片没有生成普通文件")
    _validate_png_data(path.read_bytes(), info, max_bytes)


def _private_attachment_root(root: Path) -> tuple[Path, int]:
    """打开当前房间附件目录，拒绝链接或非私有目录。"""
    root = Path(root).expanduser().absolute()
    info = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise ClipboardImageError("附件目录必须是真实目录，不能是符号链接")
    if info.st_mode & 0o777 != 0o700:
        raise ClipboardImageError("附件目录权限必须是 0700")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root, flags)
    opened = os.fstat(fd)
    if not stat.S_ISDIR(opened.st_mode) or opened.st_mode & 0o777 != 0o700:
        os.close(fd)
        raise ClipboardImageError("附件目录权限必须是 0700")
    return root.resolve(), fd


def _read_attachment_png(
    root: Path,
    relative_path: Path,
    *,
    max_bytes: int = MAX_CLIPBOARD_IMAGE_BYTES,
) -> bytes:
    """经受限目录句柄读取 PNG，避免校验后的路径替换。"""
    if (relative_path.is_absolute() or not relative_path.parts
            or any(part in {"", ".", ".."} for part in relative_path.parts)):
        raise ClipboardImageError("图片附件路径不合法")
    _root, directory_fd = _private_attachment_root(root)
    current_fd = directory_fd
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= (
            getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        for part in relative_path.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            if current_fd != directory_fd:
                os.close(current_fd)
            current_fd = next_fd
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(relative_path.parts[-1], file_flags, dir_fd=current_fd)
        try:
            info = os.fstat(file_fd)
            if info.st_size > MAX_CLIPBOARD_IMAGE_BYTES:
                raise ClipboardImageError(
                    "剪贴板图片超过 20 MiB 上限")
            if info.st_size > max_bytes:
                raise PromptImageBudgetError(
                    "协议图片总量超过当前 transport 上限")
            with os.fdopen(file_fd, "rb", closefd=True) as image_file:
                data = image_file.read(max_bytes + 1)
        except BaseException:
            # fdopen() only owns the descriptor after it returns successfully.
            with contextlib.suppress(OSError):
                os.close(file_fd)
            raise
        _validate_png_data(data, info, max_bytes)
        return data
    finally:
        if current_fd != directory_fd:
            os.close(current_fd)
        os.close(directory_fd)


def prompt_images(
    prompt: str,
    attachment_root: Path | None,
    *,
    max_total_bytes: int | None = None,
    max_images: int | None = None,
) -> tuple[TrustedImage, ...]:
    """提取当前房间内可信图片，并立即读为不可变快照。

    用户可以手写附件标记，因此不能把任意绝对路径直接交给 agent transport；
    只有位于编排器明确配置的当前房间附件目录内、权限和容器都正确的普通 PNG
    才可信。ACP 从 ``data`` 发送，避免 adapter 校验和 client 读取之间的 TOCTOU。
    """
    if attachment_root is None:
        return ()
    if (max_total_bytes is not None
            and (not isinstance(max_total_bytes, int)
                 or isinstance(max_total_bytes, bool)
                 or max_total_bytes < 1)):
        raise ValueError("max_total_bytes 必须是正整数")
    if (max_images is not None
            and (not isinstance(max_images, int)
                 or isinstance(max_images, bool)
                 or max_images < 1)):
        raise ValueError("max_images 必须是正整数")
    try:
        root, directory_fd = _private_attachment_root(attachment_root)
    except (ClipboardImageError, OSError):
        return ()
    else:
        os.close(directory_fd)
    found: list[TrustedImage] = []
    seen: set[Path] = set()
    total_bytes = 0
    for match in _ATTACHMENT_REFERENCE_RE.finditer(prompt):
        try:
            short_number, legacy_path = match.groups()
            if short_number is not None:
                if len(short_number) > 12:
                    continue
                relative = Path(f"img-{int(short_number):04d}.png")
                resolved = (root / relative).resolve(strict=True)
                relative = resolved.relative_to(root)
            else:
                candidate = Path(legacy_path).expanduser()
                if not candidate.is_absolute():
                    continue
                resolved = candidate.resolve(strict=True)
                relative = resolved.relative_to(root)
            if relative in seen:
                continue
            if max_images is not None and len(found) >= max_images:
                raise PromptImageBudgetError(
                    f"协议图片数量超过 {max_images} 张上限")
            remaining = (
                MAX_CLIPBOARD_IMAGE_BYTES
                if max_total_bytes is None
                else max_total_bytes - total_bytes
            )
            if remaining < 1:
                raise PromptImageBudgetError(
                    "协议图片总量超过当前 transport 上限")
            data = _read_attachment_png(
                root,
                relative,
                max_bytes=min(MAX_CLIPBOARD_IMAGE_BYTES, remaining),
            )
        except PromptImageBudgetError:
            raise
        except (ClipboardImageError, OSError, RuntimeError, ValueError):
            continue
        seen.add(relative)
        total_bytes += len(data)
        found.append(TrustedImage(
            path=root / relative,
            attachment_root=root,
            relative_path=relative,
            data=data,
        ))
    return tuple(found)


def verify_unchanged_image(image: TrustedImage) -> Path:
    """在只能传 ``localImage`` 路径的协议提交前复核同一附件。"""
    expected_path = image.attachment_root / image.relative_path
    if image.path != expected_path:
        raise ClipboardImageError("图片附件路径不可信")
    if _read_attachment_png(image.attachment_root, image.relative_path) != image.data:
        raise ClipboardImageError("图片附件在发送前发生变化")
    return image.path


def capture_clipboard_png(
    destination: Path,
    *,
    run_command: RunCommand = subprocess.run,
    system: str | None = None,
    max_bytes: int = MAX_CLIPBOARD_IMAGE_BYTES,
) -> Path:
    """把 macOS 剪贴板 PNG 保存到私有目录并返回绝对路径。"""
    if (system or platform.system()) != "Darwin":
        raise ClipboardImageError("当前仅支持 macOS 剪贴板图片")
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        info = destination.lstat()
        if destination.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise ClipboardImageError("附件目录必须是真实目录，不能是符号链接")
    else:
        # RoomStore 已创建父目录；不允许 parents=True 沿未知符号链接建目录。
        parent = destination.parent
        if parent.is_symlink():
            raise ClipboardImageError("附件目录父路径不能是符号链接")
        parent.resolve(strict=True)
        destination.mkdir(mode=0o700)
    os.chmod(destination, 0o700)
    highest = 0
    for entry in destination.iterdir():
        existing_number = _short_image_number(entry.name)
        if existing_number is not None:
            highest = max(highest, existing_number)
    number = highest + 1
    while True:
        path = destination / f"img-{number:04d}.png"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            reserved_fd = os.open(path, flags, 0o600)
        except FileExistsError:
            number += 1
            continue
        else:
            os.close(reserved_fd)
            break
    try:
        result = run_command(
            ["/usr/bin/osascript", "-e", _APPLE_SCRIPT, str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        path.unlink(missing_ok=True)
        raise ClipboardImageError(
            f"读取系统剪贴板失败：{exc}") from exc
    if result.returncode != 0:
        path.unlink(missing_ok=True)
        raise ClipboardImageError(
            "剪贴板里没有可粘贴的 PNG 图片") from None
    try:
        _validate_png(path, max_bytes)
        os.chmod(path, 0o600)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise

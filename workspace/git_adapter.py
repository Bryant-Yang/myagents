"""有界 Git 工作区检查器。

本模块是 transport adapter：只有这里可以启动 ``git`` 进程。workflow 和
Orchestrator 只消费不可变快照，不关心命令行细节。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_UNTRACKED_BYTES = 32 * 1024 * 1024
MAX_UNTRACKED_FILES = 1000
DEFAULT_GIT_TIMEOUT_SECONDS = 30.0
PROCESS_TERMINATE_GRACE_SECONDS = 1.0


class WorkspaceValidationError(RuntimeError):
    """工作区不能作为可复核 workflow fixed point。"""


@dataclass(frozen=True)
class WorkspaceSnapshot:
    root: str
    head: str
    branch: str
    fingerprint: str
    index_clean: bool


class GitWorkspaceInspector:
    """通过 Git CLI 捕获 baseline/candidate，并检测工作区漂移。"""

    def __init__(
        self,
        *,
        git_command: Sequence[str] = ("git",),
        timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> None:
        if not git_command:
            raise ValueError("git_command 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self._git_command = tuple(git_command)
        self._timeout_seconds = timeout_seconds

    async def capture_baseline(self, workdir: str) -> WorkspaceSnapshot:
        root = await self._repo_root(workdir)
        await self._assert_no_operation(root)
        snapshot, status = await self._snapshot(root)
        if status or not snapshot.index_clean:
            raise WorkspaceValidationError(
                "workflow 要求干净 Git 工作区（含 index flags、staging、"
                "tracked 与 untracked）")
        return snapshot

    async def capture_candidate(
        self,
        baseline: WorkspaceSnapshot,
    ) -> WorkspaceSnapshot:
        root = Path(baseline.root)
        await self._assert_no_operation(root)
        snapshot, _status = await self._snapshot(root)
        if snapshot.head != baseline.head or snapshot.branch != baseline.branch:
            raise WorkspaceValidationError(
                "writer 改变了 HEAD 或 branch；workflow 禁止 commit/checkout/reset")
        if not snapshot.index_clean:
            raise WorkspaceValidationError(
                "writer 改变了 index；workflow 禁止 staging")
        return snapshot

    async def assert_unchanged(self, expected: WorkspaceSnapshot) -> None:
        root = Path(expected.root)
        await self._assert_no_operation(root)
        current, _status = await self._snapshot(root)
        if current != expected:
            raise WorkspaceValidationError(
                "只读阶段检测到工作区漂移；拒绝继续 workflow")

    async def _repo_root(self, workdir: str) -> Path:
        value = await self._git(Path(workdir), "rev-parse", "--show-toplevel")
        root = Path(value.decode("utf-8", errors="strict").strip()).resolve()
        if not root.is_dir():
            raise WorkspaceValidationError("Git repo root 不存在")
        return root

    async def _snapshot(self, root: Path) -> tuple[WorkspaceSnapshot, bytes]:
        head = (await self._git(root, "rev-parse", "HEAD")).decode().strip()
        branch = (await self._git(
            root, "symbolic-ref", "--quiet", "--short", "HEAD",
        )).decode().strip()
        if not head or not branch:
            raise WorkspaceValidationError("workflow 要求可解析的 HEAD 与 branch")
        status_bytes = await self._git(
            root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        index_diff = await self._git(
            root, "diff", "--cached", "--binary", "--no-ext-diff")
        index_listing = await self._git(root, "ls-files", "--stage", "-z")
        index_verbose = await self._git(root, "ls-files", "-v", "-z")
        index_types = await self._git(root, "ls-files", "-t", "-z")
        has_intent_to_add = any(
            record.split(b" ", 2)[1].strip(b"0") == b""
            for record in index_listing.split(b"\0")
            if len(record.split(b" ", 2)) >= 3
        )
        has_assume_unchanged = any(
            record[:1].islower()
            for record in index_verbose.split(b"\0") if record
        )
        has_skip_worktree = any(
            record.startswith(b"S ")
            for record in index_types.split(b"\0") if record
        )
        index_clean = not any((
            index_diff,
            has_intent_to_add,
            has_assume_unchanged,
            has_skip_worktree,
        ))
        tracked_diff = await self._git(
            root, "diff", "--binary", "--no-ext-diff", "--no-textconv",
            "HEAD", "--")
        untracked_raw = await self._git(
            root, "ls-files", "--others", "--exclude-standard", "-z")
        untracked = [item for item in untracked_raw.split(b"\0") if item]
        if len(untracked) > MAX_UNTRACKED_FILES:
            raise WorkspaceValidationError(
                f"untracked 文件超过 {MAX_UNTRACKED_FILES} 个上限")

        digest = hashlib.sha256()
        for label, payload in (
            (b"head", head.encode()),
            (b"branch", branch.encode()),
            (b"status", status_bytes),
            (b"index", index_diff),
            (b"index-listing", index_listing),
            (b"index-verbose", index_verbose),
            (b"index-types", index_types),
            (b"tracked", tracked_diff),
        ):
            digest.update(label + b"\0" + payload + b"\0")

        total = 0
        for raw_path in sorted(untracked):
            try:
                relative = raw_path.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise WorkspaceValidationError(
                    "untracked 路径不是 UTF-8，无法稳定指纹") from exc
            candidate = root / relative
            resolved_parent = candidate.parent.resolve()
            try:
                resolved_parent.relative_to(root)
            except ValueError as exc:
                raise WorkspaceValidationError(
                    f"untracked 路径逃逸 repo：{relative}") from exc
            try:
                info = candidate.lstat()
            except OSError as exc:
                raise WorkspaceValidationError(
                    f"读取 untracked 文件失败：{relative}: {exc}") from exc
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise WorkspaceValidationError(
                    f"untracked 只允许普通文件：{relative}")
            total += info.st_size
            if total > MAX_UNTRACKED_BYTES:
                raise WorkspaceValidationError(
                    f"untracked 内容超过 {MAX_UNTRACKED_BYTES} 字节上限")
            try:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(candidate, flags)
                with os.fdopen(fd, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    before = (
                        opened.st_dev, opened.st_ino, opened.st_size,
                        opened.st_mtime_ns, opened.st_ctime_ns)
                    expected = (
                        info.st_dev, info.st_ino, info.st_size,
                        info.st_mtime_ns, info.st_ctime_ns)
                    if before != expected:
                        raise WorkspaceValidationError(
                            f"untracked 文件在打开前变化：{relative}")
                    content = handle.read(info.st_size + 1)
                    after_info = os.fstat(handle.fileno())
                    after = (
                        after_info.st_dev, after_info.st_ino,
                        after_info.st_size, after_info.st_mtime_ns,
                        after_info.st_ctime_ns)
                    if after != before:
                        raise WorkspaceValidationError(
                            f"untracked 文件在指纹期间变化：{relative}")
            except OSError as exc:
                raise WorkspaceValidationError(
                    f"读取 untracked 文件失败：{relative}: {exc}") from exc
            if len(content) != info.st_size:
                raise WorkspaceValidationError(
                    f"untracked 文件在指纹期间变化：{relative}")
            digest.update(b"untracked\0" + raw_path + b"\0" + content + b"\0")

        return WorkspaceSnapshot(
            root=str(root),
            head=head,
            branch=branch,
            fingerprint=digest.hexdigest(),
            index_clean=index_clean,
        ), status_bytes

    async def _assert_no_operation(self, root: Path) -> None:
        git_dir_raw = await self._git(
            root, "rev-parse", "--absolute-git-dir")
        git_dir = Path(git_dir_raw.decode().strip()).resolve()
        markers = (
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
            "rebase-merge", "rebase-apply",
        )
        if any((git_dir / marker).exists() for marker in markers):
            raise WorkspaceValidationError(
                "workflow 不接受未完成的 merge/rebase/cherry-pick/revert")

    async def _git(self, cwd: Path, *args: str) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                *self._git_command,
                *args,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise WorkspaceValidationError(f"无法启动 git：{exc}") from exc
        assert process.stdout is not None and process.stderr is not None

        async def read_bounded(
            stream: asyncio.StreamReader,
        ) -> bytes:
            buffer = bytearray()
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    return bytes(buffer)
                buffer.extend(chunk)
                if len(buffer) > MAX_GIT_OUTPUT_BYTES:
                    raise WorkspaceValidationError(
                        "git 输出超过有界读取上限")

        async def collect() -> tuple[bytes, bytes, int]:
            stdout_task = asyncio.create_task(read_bounded(process.stdout))
            stderr_task = asyncio.create_task(read_bounded(process.stderr))
            wait_task = asyncio.create_task(process.wait())
            tasks = (stdout_task, stderr_task, wait_task)
            try:
                stdout, stderr, returncode = await asyncio.gather(*tasks)
                return stdout, stderr, returncode
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        collector = asyncio.create_task(collect())
        try:
            stdout, stderr, returncode = await asyncio.wait_for(
                collector, timeout=self._timeout_seconds)
        except asyncio.TimeoutError as exc:
            await self._terminate_process(process)
            raise WorkspaceValidationError(
                f"git {' '.join(args)} 超时") from exc
        except BaseException:
            await self._terminate_process(process)
            raise
        if returncode != 0:
            detail = stderr.decode(
                "utf-8", errors="replace").strip()[:500]
            raise WorkspaceValidationError(
                f"git {' '.join(args)} 失败：{detail or returncode}")
        return stdout

    @staticmethod
    async def _terminate_process(
        process: asyncio.subprocess.Process,
    ) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(
                asyncio.shield(process.wait()),
                timeout=PROCESS_TERMINATE_GRACE_SECONDS,
            )
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

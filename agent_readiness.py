"""Agent 注册与本机就绪状态之间的深模块。

probe 只能观察当前进程的环境和文件系统，不能启动 CLI、联网或安装软件。
调用方只使用 snapshot/require/refresh，不解释各厂商命令路径。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping


_DETAIL_LIMIT = 500
_MAX_CONFIG_BYTES = 64 * 1024
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TABLE_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
_ENABLED_KEY_RE = re.compile(
    r"^(\s*)enabled\s*=\s*(?:true|false)(\s*(?:#.*)?)$")
_DEFAULT_CONFIG_RELATIVE_PATH = Path("myagents/config.toml")


class ReadinessState(str, Enum):
    READY = "ready"
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    DISABLED = "disabled"

    @property
    def label(self) -> str:
        return {
            ReadinessState.READY: "可用",
            ReadinessState.NOT_FOUND: "未检测到 CLI",
            ReadinessState.INVALID: "配置无效",
            ReadinessState.DISABLED: "已禁用",
        }[self]


@dataclass(frozen=True)
class AgentReadiness:
    name: str
    state: ReadinessState
    detail: str
    setup_hint: str
    executable: str | None = None

    @property
    def ready(self) -> bool:
        return self.state is ReadinessState.READY


ReadinessProbe = Callable[[], AgentReadiness]
ExecutableResolver = Callable[[str], str | None]


class AgentUnavailableError(ValueError):
    """一条命令引用了当前未就绪的 agent；调用方不得部分派发。"""

    def __init__(
        self,
        statuses: Iterable[AgentReadiness],
        *,
        purpose: str,
    ) -> None:
        items = tuple(statuses)
        self.names = tuple(item.name for item in items)
        detail = "；".join(
            f"@{item.name}：{item.detail}；{item.setup_hint}"
            for item in items
        )
        next_step = (
            "按上面建议处理后重试"
            if any(item.state is ReadinessState.DISABLED for item in items)
            else "修复后执行 /agents rescan"
        )
        super().__init__(
            f"{purpose}未开始，以下 agent 当前未就绪：{detail}。"
            f"{next_step}"
        )


class AgentEnablementError(ValueError):
    """全局 Agent 开关配置缺失安全属性或结构无效。"""


@dataclass(frozen=True)
class AgentControlCommand:
    action: str
    name: str | None = None


def parse_agent_control_command(text: str) -> AgentControlCommand | None:
    """解析本地 ``/agents`` 命令；不把未知子命令送给模型。"""
    stripped = text.strip()
    if not stripped.startswith("/agents"):
        return None
    parts = stripped.split()
    if not parts or parts[0] != "/agents":
        return None
    if len(parts) == 1:
        return AgentControlCommand("show")
    if parts == ["/agents", "rescan"]:
        return AgentControlCommand("rescan")
    if len(parts) == 3 and parts[1] in {"enable", "disable"}:
        name = parts[2]
        if not _AGENT_NAME_RE.fullmatch(name):
            raise AgentEnablementError(
                "agent 名称必须匹配 [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        return AgentControlCommand(parts[1], name)
    raise AgentEnablementError(
        "用法：/agents、/agents rescan、/agents enable <agent> 或 "
        "/agents disable <agent>")


def agent_config_path(
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    raw_home = values.get("XDG_CONFIG_HOME", "").strip()
    configured = Path(raw_home).expanduser() if raw_home else None
    home = (
        configured
        if configured is not None and configured.is_absolute()
        else Path.home() / ".config"
    )
    return home / _DEFAULT_CONFIG_RELATIVE_PATH


class AgentEnablementConfig:
    """同一私有 XDG 配置中的全局 Agent 开关。

    只拥有 ``[agents.<name>].enabled``；其他 section 原样保留。写入使用
    进程间锁和同目录原子替换，避免多个 TUI 互相截断配置。
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.path = (
            Path(path).expanduser()
            if path is not None else agent_config_path(environ)
        )

    def disabled_names(self, registered_names: Iterable[str]) -> frozenset[str]:
        payload, _ = _read_private_config(self.path)
        return _disabled_names_from_payload(payload, registered_names)

    def set_enabled(self, name: str, enabled: bool) -> Path:
        if not _AGENT_NAME_RE.fullmatch(name):
            raise AgentEnablementError("无效的 agent 名称")
        if not isinstance(enabled, bool):
            raise TypeError("enabled 必须是 bool")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        import fcntl
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise AgentEnablementError("当前平台不支持安全写入全局配置")
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | no_follow
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except OSError as exc:
            raise AgentEnablementError("无法安全锁定全局配置") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise AgentEnablementError("全局配置锁路径必须是普通文件")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            payload, source = _read_private_config(self.path)
            _disabled_names_from_payload(payload, ())
            updated = _patch_agent_enabled(source, name, enabled)
            if len(updated.encode("utf-8")) > _MAX_CONFIG_BYTES:
                raise AgentEnablementError("全局配置文件超过 64 KiB 上限")
            parsed = _parse_config_bytes(updated.encode("utf-8"))
            agents = parsed.get("agents", {})
            raw = agents.get(name, {}) if isinstance(agents, dict) else {}
            if not isinstance(raw, dict) \
                    or raw.get("enabled") is not enabled:
                raise AgentEnablementError("全局 Agent 开关写入校验失败")
            _atomic_write_private(self.path, updated.encode("utf-8"))
        finally:
            os.close(descriptor)
        return self.path


def _disabled_names_from_payload(
    payload: Mapping[str, object],
    registered_names: Iterable[str],
) -> frozenset[str]:
    registered = set(registered_names)
    agents = payload.get("agents", {})
    if not isinstance(agents, dict):
        raise AgentEnablementError("全局配置的 [agents] 必须是 TOML table")
    disabled: set[str] = set()
    for name, raw in agents.items():
        if not isinstance(name, str) or not _AGENT_NAME_RE.fullmatch(name):
            raise AgentEnablementError("[agents] 包含无效的 agent 名称")
        if not isinstance(raw, dict) or set(raw) - {"enabled"}:
            raise AgentEnablementError(
                f"[agents.{name}] 只允许 enabled 字段")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AgentEnablementError(
                f"[agents.{name}].enabled 必须是 bool")
        if name in registered and not enabled:
            disabled.add(name)
    return frozenset(disabled)


class AgentReadinessRegistry:
    """缓存一组被动 probe，并提供原子资格检查。"""

    def __init__(self, probes: Mapping[str, ReadinessProbe]) -> None:
        self._probes = dict(probes)
        self._statuses = {
            name: AgentReadiness(
                name,
                ReadinessState.NOT_FOUND,
                "尚未执行本机就绪探测",
                "执行 /agents rescan",
            )
            for name in self._probes
        }

    def snapshot(self) -> tuple[AgentReadiness, ...]:
        return tuple(self._statuses[name] for name in self._probes)

    def refresh(
        self,
        *,
        disabled_names: Iterable[str] = (),
    ) -> tuple[AgentReadiness, ...]:
        disabled = set(disabled_names)
        updated: dict[str, AgentReadiness] = {}
        for name, probe in self._probes.items():
            if name in disabled:
                updated[name] = AgentReadiness(
                    name,
                    ReadinessState.DISABLED,
                    "已由全局配置禁用；不会卸载、启动或调用该 agent",
                    f"执行 /agents enable {name}",
                )
                continue
            try:
                status = probe()
                if status.name != name:
                    raise ValueError(
                        f"probe 返回 agent {status.name!r}，预期 {name!r}")
                if not isinstance(status.state, ReadinessState):
                    raise TypeError("probe 返回了未知 readiness state")
            except Exception as exc:
                previous = self._statuses[name]
                status = AgentReadiness(
                    name,
                    ReadinessState.INVALID,
                    f"就绪探测失败：{exc}"[:_DETAIL_LIMIT],
                    previous.setup_hint,
                )
            updated[name] = status
        self._statuses = updated
        return self.snapshot()

    def require(self, names: Iterable[str], *, purpose: str) -> None:
        missing: list[AgentReadiness] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            status = self._statuses.get(name)
            if status is None:
                status = AgentReadiness(
                    name,
                    ReadinessState.INVALID,
                    "未注册 readiness probe",
                    "从 /agents 中选择已注册 agent",
                )
            if not status.ready:
                missing.append(status)
        if missing:
            raise AgentUnavailableError(missing, purpose=purpose)

    def mark_invalid(
        self,
        name: str,
        detail: str,
        *,
        setup_hint: str | None = None,
    ) -> None:
        previous = self._statuses[name]
        self._statuses[name] = AgentReadiness(
            name,
            ReadinessState.INVALID,
            detail[:_DETAIL_LIMIT],
            setup_hint or previous.setup_hint,
        )

    def set_status(self, status: AgentReadiness) -> None:
        """Replace one registered status after a backend-specific probe."""
        if status.name not in self._probes:
            raise KeyError(f"未注册 readiness probe：{status.name!r}")
        if not isinstance(status.state, ReadinessState):
            raise TypeError("status 包含未知 readiness state")
        self._statuses[status.name] = status


def _read_private_config(path: Path) -> tuple[dict[str, object], str]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise AgentEnablementError("当前平台不支持安全读取全局配置")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except FileNotFoundError:
        return {}, ""
    except OSError as exc:
        raise AgentEnablementError("无法安全读取全局配置") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise AgentEnablementError("全局配置路径必须是普通文件")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise AgentEnablementError("全局配置文件权限必须为 0600")
            if metadata.st_size > _MAX_CONFIG_BYTES:
                raise AgentEnablementError("全局配置文件超过 64 KiB 上限")
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise AgentEnablementError("全局配置文件超过 64 KiB 上限")
        payload = _parse_config_bytes(raw)
        return payload, raw.decode("utf-8")
    except AgentEnablementError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentEnablementError(
            "全局配置文件不是有效的 UTF-8 TOML") from exc


def _parse_config_bytes(raw: bytes) -> dict[str, object]:
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise AgentEnablementError(
            "全局配置文件不是有效的 UTF-8 TOML") from exc
    if not isinstance(payload, dict):
        raise AgentEnablementError("全局配置文件必须是 TOML object")
    return payload


def _patch_agent_enabled(source: str, name: str, enabled: bool) -> str:
    value = "true" if enabled else "false"
    lines = source.splitlines(keepends=True)
    target = f"agents.{name}"
    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        match = _TABLE_HEADER_RE.match(line.rstrip("\r\n"))
        if match is None:
            continue
        if start is not None:
            end = index
            break
        if match.group(1).strip() == target:
            start = index
    if start is None:
        parsed = _parse_config_bytes(source.encode("utf-8"))
        agents = parsed.get("agents", {})
        if isinstance(agents, dict) and name in agents:
            raise AgentEnablementError(
                f"[agents.{name}] 必须使用未加引号的标准 table 写法")
        prefix = source
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix:
            prefix += "\n"
        return prefix + f"[agents.{name}]\nenabled = {value}\n"

    enabled_index: int | None = None
    for index in range(start + 1, end):
        candidate = lines[index].rstrip("\r\n")
        if _ENABLED_KEY_RE.match(candidate):
            if enabled_index is not None:
                raise AgentEnablementError(
                    f"[agents.{name}] 包含重复 enabled 字段")
            enabled_index = index
    if enabled_index is None:
        newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
        lines.insert(start + 1, f"enabled = {value}{newline}")
    else:
        match = _ENABLED_KEY_RE.match(lines[enabled_index].rstrip("\r\n"))
        assert match is not None
        newline = "\r\n" if lines[enabled_index].endswith("\r\n") else "\n"
        lines[enabled_index] = (
            f"{match.group(1)}enabled = {value}{match.group(2)}{newline}"
        )
    return "".join(lines)


def _atomic_write_private(path: Path, payload: bytes) -> None:
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(raw_path)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise AgentEnablementError("无法原子写入全局配置") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def executable_probe(
    name: str,
    candidates: tuple[str, ...],
    setup_hint: str,
    *,
    resolver: ExecutableResolver | None = None,
) -> ReadinessProbe:
    """构造纯 PATH/文件 probe；不执行候选命令。"""
    if not candidates:
        raise ValueError("candidates 不能为空")
    find = resolver or shutil.which

    def probe() -> AgentReadiness:
        for command in candidates:
            candidate = find(command)
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                return AgentReadiness(
                    name,
                    ReadinessState.INVALID,
                    f"命令 {command} 的路径无效：{exc}"[:_DETAIL_LIMIT],
                    setup_hint,
                )
            if not resolved.is_file() or not os.access(resolved, os.X_OK):
                return AgentReadiness(
                    name,
                    ReadinessState.INVALID,
                    f"命令 {command} 不可执行：{resolved}"[:_DETAIL_LIMIT],
                    setup_hint,
                    str(resolved),
                )
            return AgentReadiness(
                name,
                ReadinessState.READY,
                f"已检测到 CLI：{resolved}"[:_DETAIL_LIMIT],
                setup_hint,
                str(resolved),
            )
        rendered = " / ".join(candidates)
        return AgentReadiness(
            name,
            ReadinessState.NOT_FOUND,
            f"当前进程 PATH 未检测到 {rendered}",
            setup_hint,
        )

    return probe

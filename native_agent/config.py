"""Passive configuration and readiness for the native model-backed host."""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from agent_readiness import AgentReadiness, ReadinessState


MODEL_PROVIDER_ENV = "MYAGENTS_MODEL_PROVIDER"
MODEL_BASE_URL_ENV = "MYAGENTS_MODEL_BASE_URL"
MODEL_ID_ENV = "MYAGENTS_MODEL_ID"
MODEL_API_KEY_ENV = "MYAGENTS_MODEL_API_KEY"
XDG_CONFIG_HOME_ENV = "XDG_CONFIG_HOME"

DEFAULT_MODEL_PROVIDER = "openai-compatible"
DEFAULT_MODEL_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_CONFIG_RELATIVE_PATH = Path("myagents/config.toml")
_MAX_PROVIDER_LENGTH = 64
_MAX_BASE_URL_LENGTH = 2048
_MAX_MODEL_ID_LENGTH = 512
_MAX_API_KEY_LENGTH = 8192
_MAX_CONFIG_BYTES = 64 * 1024
_MODEL_FILE_KEYS = frozenset({
    "provider", "base_url", "model_id", "api_key",
})
_ENV_TO_FILE_KEY = {
    MODEL_PROVIDER_ENV: "provider",
    MODEL_BASE_URL_ENV: "base_url",
    MODEL_ID_ENV: "model_id",
    MODEL_API_KEY_ENV: "api_key",
}


class NativeModelConfigurationError(ValueError):
    """Native model configuration is missing or unsafe."""


@dataclass(frozen=True)
class NativeModelConfig:
    """Validated provider selection; credentials stay out of repr/logs."""

    provider: str
    base_url: str
    model_id: str
    api_key: str | None = field(default=None, repr=False)
    source: str = "environment"

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "NativeModelConfig":
        values = os.environ if environ is None else environ
        return cls._from_values(values, source="环境变量")

    @classmethod
    def from_sources(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
    ) -> "NativeModelConfig":
        """Load the private TOML config, then apply explicit env overrides.

        Passing an isolated mapping without ``XDG_CONFIG_HOME`` keeps tests
        and embedded callers independent from the user's real home config.
        Production calls with ``environ=None`` always use the XDG path.
        """
        values = os.environ if environ is None else environ
        selected_path: Path | None
        if config_path is not None:
            selected_path = Path(config_path).expanduser()
        elif environ is None or XDG_CONFIG_HOME_ENV in values:
            selected_path = native_model_config_path(values)
        else:
            selected_path = None

        merged: dict[str, str] = {}
        loaded_from_file = False
        if selected_path is not None:
            file_values = _read_model_config_file(selected_path)
            merged.update({
                env_name: file_values[file_key]
                for env_name, file_key in _ENV_TO_FILE_KEY.items()
                if file_key in file_values
            })
            loaded_from_file = bool(file_values)

        overridden = False
        for env_name in _ENV_TO_FILE_KEY:
            if env_name in values:
                merged[env_name] = values[env_name]
                overridden = True
        if loaded_from_file and overridden:
            source = "配置文件 + 环境变量覆盖"
        elif loaded_from_file:
            source = "配置文件"
        else:
            source = "环境变量"
        return cls._from_values(merged, source=source)

    @classmethod
    def _from_values(
        cls,
        values: Mapping[str, str],
        *,
        source: str,
    ) -> "NativeModelConfig":
        provider = values.get(
            MODEL_PROVIDER_ENV, DEFAULT_MODEL_PROVIDER).strip()
        if len(provider) > _MAX_PROVIDER_LENGTH:
            raise NativeModelConfigurationError(
                "模型 provider 名称超过 64 字符上限")
        if provider != DEFAULT_MODEL_PROVIDER:
            raise NativeModelConfigurationError(
                "暂不支持所配置的模型 provider；"
                f"当前仅支持 {DEFAULT_MODEL_PROVIDER}")

        model_id = values.get(MODEL_ID_ENV, "").strip()
        if not model_id:
            raise NativeModelConfigurationError("未配置原生 host 模型")
        if len(model_id) > _MAX_MODEL_ID_LENGTH:
            raise NativeModelConfigurationError(
                "模型 id 超过 512 字符上限")
        if any(ch in model_id for ch in "\r\n\0"):
            raise NativeModelConfigurationError("模型 id 包含非法控制字符")

        raw_base_url = values.get(
            MODEL_BASE_URL_ENV, DEFAULT_MODEL_BASE_URL).strip()
        if len(raw_base_url) > _MAX_BASE_URL_LENGTH:
            raise NativeModelConfigurationError(
                "模型 base URL 超过 2048 字符上限")
        parsed = urlparse(raw_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise NativeModelConfigurationError(
                "模型 base URL 必须使用 http:// 或 https://")
        if (parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise NativeModelConfigurationError(
                "模型 base URL 不得包含凭据、query 或 fragment")
        base_url = raw_base_url.rstrip("/")

        api_key = values.get(MODEL_API_KEY_ENV, "").strip() or None
        if api_key is not None and len(api_key) > _MAX_API_KEY_LENGTH:
            raise NativeModelConfigurationError(
                "模型 API key 超过 8192 字符上限")
        if api_key is not None and any(ch in api_key for ch in "\r\n\0"):
            raise NativeModelConfigurationError(
                "模型 API key 包含非法控制字符")
        return cls(provider, base_url, model_id, api_key, source)


def native_model_config_path(
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    raw_home = values.get(XDG_CONFIG_HOME_ENV, "").strip()
    configured_home = Path(raw_home).expanduser() if raw_home else None
    config_home = (
        configured_home
        if configured_home is not None and configured_home.is_absolute()
        else Path.home() / ".config"
    )
    return config_home / DEFAULT_CONFIG_RELATIVE_PATH


def native_model_setup_hint(
    environ: Mapping[str, str] | None = None,
    *,
    config_path: str | os.PathLike[str] | None = None,
) -> str:
    path = (
        Path(config_path).expanduser()
        if config_path is not None else native_model_config_path(environ)
    )
    return (
        f"编辑 {path} 的 [host.model]，先从模型服务 /v1/models 获取精确 "
        "model_id；MYAGENTS_MODEL_* 仅用于临时覆盖"
    )


def _read_model_config_file(path: Path) -> dict[str, str]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise NativeModelConfigurationError(
            "当前平台不支持安全读取原生 host 配置文件")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | no_follow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise NativeModelConfigurationError(
            "无法安全读取原生 host 配置文件") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise NativeModelConfigurationError(
                    "原生 host 配置路径必须是普通文件")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise NativeModelConfigurationError(
                    "原生 host 配置文件权限必须为 0600")
            if metadata.st_size > _MAX_CONFIG_BYTES:
                raise NativeModelConfigurationError(
                    "原生 host 配置文件超过 64 KiB 上限")
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise NativeModelConfigurationError(
                "原生 host 配置文件超过 64 KiB 上限")
        payload = tomllib.loads(raw.decode("utf-8"))
    except NativeModelConfigurationError:
        raise
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NativeModelConfigurationError(
            "原生 host 配置文件不是有效的 UTF-8 TOML") from exc
    if set(payload) - {"host"}:
        raise NativeModelConfigurationError(
            "原生 host 配置文件包含未知顶层字段")
    host = payload.get("host", {})
    if not isinstance(host, dict) or set(host) - {"model"}:
        raise NativeModelConfigurationError(
            "原生 host 配置的 [host] 结构无效")
    model = host.get("model", {})
    if not isinstance(model, dict):
        raise NativeModelConfigurationError(
            "原生 host 配置的 [host.model] 结构无效")
    if set(model) - _MODEL_FILE_KEYS:
        raise NativeModelConfigurationError(
            "原生 host 配置的 [host.model] 包含未知字段")
    if any(not isinstance(value, str) for value in model.values()):
        raise NativeModelConfigurationError(
            "原生 host 配置的 [host.model] 字段必须是字符串")
    return dict(model)


def native_host_readiness_probe(
    *,
    environ: Mapping[str, str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
) -> AgentReadiness:
    """Read local config syntax only; never contacts or changes a provider."""
    try:
        config = NativeModelConfig.from_sources(
            environ, config_path=config_path)
    except NativeModelConfigurationError as exc:
        detail = str(exc)
        state = (
            ReadinessState.NOT_FOUND
            if detail == "未配置原生 host 模型"
            else ReadinessState.INVALID
        )
        return AgentReadiness(
            "host", state, detail, native_model_setup_hint(
                environ, config_path=config_path))
    return AgentReadiness(
        "host",
        ReadinessState.READY,
        f"原生模型：{config.model_id} · OpenAI-compatible · {config.source}",
        native_model_setup_hint(environ, config_path=config_path),
    )

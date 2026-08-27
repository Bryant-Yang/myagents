"""Passive configuration and readiness for model-backed host profiles."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field, replace
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
DEFAULT_MODEL_PROFILE = "default"
DEFAULT_CONFIG_RELATIVE_PATH = Path("myagents/config.toml")
_MAX_PROVIDER_LENGTH = 64
_MAX_BASE_URL_LENGTH = 2048
_MAX_MODEL_ID_LENGTH = 512
_MAX_API_KEY_LENGTH = 8192
_MAX_CONFIG_BYTES = 64 * 1024
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_LEGACY_MODEL_KEYS = frozenset({
    "provider", "base_url", "model_id", "api_key", "api_key_env",
    "models_discovery",
})
_NAMED_MODEL_KEYS = frozenset({
    "provider", "base_url", "model_id", "api_key_env",
    "models_discovery",
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
    """One validated profile; resolved credentials stay out of repr/logs."""

    provider: str
    base_url: str
    model_id: str
    api_key: str | None = field(default=None, repr=False)
    source: str = "environment"
    profile: str = DEFAULT_MODEL_PROFILE
    models_discovery: bool = True
    api_key_env: str | None = None

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "NativeModelConfig":
        values = os.environ if environ is None else environ
        return _config_from_values(
            {
                file_key: values[env_name]
                for env_name, file_key in _ENV_TO_FILE_KEY.items()
                if env_name in values
            },
            values,
            profile=DEFAULT_MODEL_PROFILE,
            source="环境变量",
            allow_inline_api_key=True,
        )

    @classmethod
    def from_sources(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
    ) -> "NativeModelConfig":
        return NativeModelCatalog.from_sources(
            environ, config_path=config_path
        ).require_profile(DEFAULT_MODEL_PROFILE)


@dataclass(frozen=True)
class NativeModelCatalog:
    """Validated named profiles from one private XDG configuration file."""

    profiles: dict[str, NativeModelConfig]
    config_path: Path | None
    _environ: Mapping[str, str] = field(repr=False, compare=False)

    @classmethod
    def from_sources(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
    ) -> "NativeModelCatalog":
        values = os.environ if environ is None else environ
        selected_path: Path | None
        if config_path is not None:
            selected_path = Path(config_path).expanduser()
        elif environ is None or XDG_CONFIG_HOME_ENV in values:
            selected_path = native_model_config_path(values)
        else:
            selected_path = None

        host: dict[str, object] = {}
        if selected_path is not None:
            host = _read_host_config_file(selected_path)

        profiles: dict[str, NativeModelConfig] = {}
        legacy = host.get("model", {})
        if not isinstance(legacy, dict):
            raise NativeModelConfigurationError(
                "原生 host 配置的 [host.model] 结构无效")
        if "api_key" in legacy:
            raise NativeModelConfigurationError(
                "[host.model] 不得保存 api_key；请改用 api_key_env 引用环境变量")
        merged = dict(legacy)
        overridden = False
        for env_name, file_key in _ENV_TO_FILE_KEY.items():
            if env_name in values:
                merged[file_key] = values[env_name]
                overridden = True
        if merged:
            source = (
                "配置文件 + 环境变量覆盖"
                if legacy and overridden
                else ("配置文件" if legacy else "环境变量")
            )
            profiles[DEFAULT_MODEL_PROFILE] = _config_from_values(
                merged,
                values,
                profile=DEFAULT_MODEL_PROFILE,
                source=source,
                allow_inline_api_key=True,
            )

        named = host.get("models", {})
        if not isinstance(named, dict):
            raise NativeModelConfigurationError(
                "原生 host 配置的 [host.models] 结构无效")
        for name, raw_profile in named.items():
            if not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name):
                raise NativeModelConfigurationError(
                    "模型 profile 名称必须匹配 [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
            if name == DEFAULT_MODEL_PROFILE:
                raise NativeModelConfigurationError(
                    "命名 profile 不得使用保留名 default；请使用 [host.model]")
            if not isinstance(raw_profile, dict):
                raise NativeModelConfigurationError(
                    f"模型 profile {name!r} 必须是 TOML table")
            profiles[name] = _config_from_values(
                raw_profile,
                values,
                profile=name,
                source=f"配置文件 profile {name}",
                allow_inline_api_key=False,
            )
        return cls(profiles, selected_path, dict(values))

    def require_profile(self, name: str) -> NativeModelConfig:
        try:
            config = self.profiles[name]
        except KeyError:
            if name == DEFAULT_MODEL_PROFILE:
                raise NativeModelConfigurationError(
                    "未配置原生 host 模型") from None
            available = "、".join(sorted(self.profiles)) or "（无）"
            raise NativeModelConfigurationError(
                f"未配置模型 profile {name!r}；可用 profile：{available}"
            ) from None
        if config.api_key_env is None:
            return config
        api_key = self._environ.get(config.api_key_env, "").strip() or None
        if api_key is None:
            raise NativeModelConfigurationError(
                f"模型 profile {name!r} 引用的凭据环境变量 "
                f"{config.api_key_env} 未设置")
        if len(api_key) > _MAX_API_KEY_LENGTH:
            raise NativeModelConfigurationError(
                "模型 API key 超过 8192 字符上限")
        if any(ch in api_key for ch in "\r\n\0"):
            raise NativeModelConfigurationError(
                "模型 API key 包含非法控制字符")
        return replace(config, api_key=api_key)

    def resolve(
        self,
        target: str,
        *,
        reference: str,
    ) -> NativeModelConfig:
        if reference == "profile":
            return self.require_profile(target)
        if reference != "model":
            raise NativeModelConfigurationError(
                f"未知模型引用类型：{reference!r}")
        base = self.require_profile(DEFAULT_MODEL_PROFILE)
        return replace(
            base,
            model_id=_validate_model_id(target),
            profile=f"exact:{target}",
            source=f"{base.source} + 精确 model id",
        )


def load_native_model_catalog(
    environ: Mapping[str, str] | None = None,
    *,
    config_path: str | os.PathLike[str] | None = None,
) -> NativeModelCatalog:
    return NativeModelCatalog.from_sources(
        environ, config_path=config_path)


def native_model_profile_names(
    environ: Mapping[str, str] | None = None,
    *,
    config_path: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """List declared profile names without resolving referenced secrets."""
    values = os.environ if environ is None else environ
    if config_path is not None:
        selected_path = Path(config_path).expanduser()
    elif environ is None or XDG_CONFIG_HOME_ENV in values:
        selected_path = native_model_config_path(values)
    else:
        selected_path = None
    host = (
        _read_host_config_file(selected_path)
        if selected_path is not None else {}
    )
    names: list[str] = []
    if host.get("model") or MODEL_ID_ENV in values:
        names.append(DEFAULT_MODEL_PROFILE)
    named = host.get("models", {})
    if not isinstance(named, dict):
        raise NativeModelConfigurationError(
            "原生 host 配置的 [host.models] 结构无效")
    for name in named:
        if not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name):
            raise NativeModelConfigurationError(
                "模型 profile 名称必须匹配 [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        if name == DEFAULT_MODEL_PROFILE:
            raise NativeModelConfigurationError(
                "命名 profile 不得使用保留名 default；请使用 [host.model]")
        names.append(name)
    return tuple(names)


def resolve_native_model_config(
    target: str,
    *,
    reference: str,
    environ: Mapping[str, str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
) -> NativeModelConfig:
    return load_native_model_catalog(
        environ, config_path=config_path
    ).resolve(target, reference=reference)


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
        f"编辑 {path} 的 [host.model] 或 [host.models.<name>]；"
        "支持模型发现的服务使用精确 /v1/models id，不支持发现的服务配置 exact "
        "model_id 与 api_key_env；MYAGENTS_MODEL_* 仅作当前进程临时覆盖"
    )


def native_host_readiness_probe(
    *,
    environ: Mapping[str, str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    profile: str = DEFAULT_MODEL_PROFILE,
    exact_model_id: str | None = None,
) -> AgentReadiness:
    """Read local config syntax only; never contacts or changes a provider."""
    try:
        catalog = load_native_model_catalog(
            environ, config_path=config_path)
        config = (
            catalog.resolve(exact_model_id, reference="model")
            if exact_model_id is not None
            else catalog.require_profile(profile)
        )
    except NativeModelConfigurationError as exc:
        detail = str(exc)
        state = (
            ReadinessState.NOT_FOUND
            if detail.startswith((
                "未配置模型 profile", "未配置原生 host 模型"))
            else ReadinessState.INVALID
        )
        return AgentReadiness(
            "host", state, detail, native_model_setup_hint(
                environ, config_path=config_path))
    discovery = "精确 /models 校验" if config.models_discovery \
        else "显式 model_id（首个请求校验）"
    return AgentReadiness(
        "host",
        ReadinessState.READY,
        f"模型 profile {config.profile}：{config.model_id} · "
        f"OpenAI-compatible · {discovery} · {config.source}",
        native_model_setup_hint(environ, config_path=config_path),
    )


def _config_from_values(
    raw: Mapping[str, object],
    environ: Mapping[str, str],
    *,
    profile: str,
    source: str,
    allow_inline_api_key: bool,
) -> NativeModelConfig:
    allowed = _LEGACY_MODEL_KEYS if allow_inline_api_key \
        else _NAMED_MODEL_KEYS
    unknown = set(raw) - allowed
    if unknown:
        rendered = ", ".join(sorted(str(item) for item in unknown))
        raise NativeModelConfigurationError(
            f"模型 profile {profile!r} 包含未知字段：{rendered}")
    for key, value in raw.items():
        if key == "models_discovery":
            if not isinstance(value, bool):
                raise NativeModelConfigurationError(
                    f"模型 profile {profile!r} 的 models_discovery 必须是 bool")
        elif not isinstance(value, str):
            raise NativeModelConfigurationError(
                f"模型 profile {profile!r} 的 {key} 必须是字符串")

    provider = str(raw.get("provider", DEFAULT_MODEL_PROVIDER)).strip()
    if len(provider) > _MAX_PROVIDER_LENGTH:
        raise NativeModelConfigurationError(
            "模型 provider 名称超过 64 字符上限")
    if provider != DEFAULT_MODEL_PROVIDER:
        raise NativeModelConfigurationError(
            "暂不支持所配置的模型 provider；"
            f"当前仅支持 {DEFAULT_MODEL_PROVIDER}")

    model_id = _validate_model_id(str(raw.get("model_id", "")))
    raw_base_url = str(raw.get("base_url", DEFAULT_MODEL_BASE_URL)).strip()
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

    inline_key = str(raw.get("api_key", "")).strip() or None
    api_key_env = str(raw.get("api_key_env", "")).strip() or None
    if inline_key is not None and api_key_env is not None:
        raise NativeModelConfigurationError(
            f"模型 profile {profile!r} 不能同时配置 api_key 与 api_key_env")
    if api_key_env is not None and not _ENV_NAME_RE.fullmatch(api_key_env):
        raise NativeModelConfigurationError(
            f"模型 profile {profile!r} 的 api_key_env 不是合法环境变量名")
    api_key = inline_key
    if api_key is not None and len(api_key) > _MAX_API_KEY_LENGTH:
        raise NativeModelConfigurationError(
            "模型 API key 超过 8192 字符上限")
    if api_key is not None and any(ch in api_key for ch in "\r\n\0"):
        raise NativeModelConfigurationError(
            "模型 API key 包含非法控制字符")
    return NativeModelConfig(
        provider,
        base_url,
        model_id,
        api_key,
        source,
        profile,
        bool(raw.get("models_discovery", True)),
        api_key_env,
    )


def _validate_model_id(value: str) -> str:
    model_id = value.strip()
    if not model_id:
        raise NativeModelConfigurationError("未配置原生 host 模型")
    if len(model_id) > _MAX_MODEL_ID_LENGTH:
        raise NativeModelConfigurationError(
            "模型 id 超过 512 字符上限")
    if any(ch in model_id for ch in "\r\n\0"):
        raise NativeModelConfigurationError("模型 id 包含非法控制字符")
    return model_id


def _read_host_config_file(path: Path) -> dict[str, object]:
    payload = _read_private_toml(path)
    if set(payload) - {"host"}:
        raise NativeModelConfigurationError(
            "原生 host 配置文件包含未知顶层字段")
    host = payload.get("host", {})
    if not isinstance(host, dict) or set(host) - {"model", "models"}:
        raise NativeModelConfigurationError(
            "原生 host 配置的 [host] 结构无效")
    model = host.get("model", {})
    if not isinstance(model, dict):
        raise NativeModelConfigurationError(
            "原生 host 配置的 [host.model] 结构无效")
    models = host.get("models", {})
    if not isinstance(models, dict):
        raise NativeModelConfigurationError(
            "原生 host 配置的 [host.models] 结构无效")
    return dict(host)


def _read_private_toml(path: Path) -> dict[str, object]:
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
    if not isinstance(payload, dict):
        raise NativeModelConfigurationError(
            "原生 host 配置文件必须是 TOML object")
    return payload

"""Passive configuration and readiness for the native model-backed host."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlparse

from agent_readiness import AgentReadiness, ReadinessState


MODEL_PROVIDER_ENV = "MYAGENTS_MODEL_PROVIDER"
MODEL_BASE_URL_ENV = "MYAGENTS_MODEL_BASE_URL"
MODEL_ID_ENV = "MYAGENTS_MODEL_ID"
MODEL_API_KEY_ENV = "MYAGENTS_MODEL_API_KEY"

DEFAULT_MODEL_PROVIDER = "openai-compatible"
DEFAULT_MODEL_BASE_URL = "http://127.0.0.1:1234/v1"
_MAX_PROVIDER_LENGTH = 64
_MAX_BASE_URL_LENGTH = 2048
_MAX_MODEL_ID_LENGTH = 512
_MAX_API_KEY_LENGTH = 8192
_SETUP_HINT = (
    "先调用模型服务 /v1/models 获取精确 id，再设置 "
    "MYAGENTS_MODEL_ID；LM Studio 默认可使用 "
    "MYAGENTS_MODEL_BASE_URL=http://127.0.0.1:1234/v1"
)


class NativeModelConfigurationError(ValueError):
    """Native model configuration is missing or unsafe."""


@dataclass(frozen=True)
class NativeModelConfig:
    """Validated provider selection; credentials stay out of repr/logs."""

    provider: str
    base_url: str
    model_id: str
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "NativeModelConfig":
        values = os.environ if environ is None else environ
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
        return cls(provider, base_url, model_id, api_key)


def native_host_readiness_probe(
    *,
    environ: Mapping[str, str] | None = None,
) -> AgentReadiness:
    """Read env syntax only; never contacts the provider or changes config."""
    try:
        config = NativeModelConfig.from_environ(environ)
    except NativeModelConfigurationError as exc:
        detail = str(exc)
        state = (
            ReadinessState.NOT_FOUND
            if detail == "未配置原生 host 模型"
            else ReadinessState.INVALID
        )
        return AgentReadiness("host", state, detail, _SETUP_HINT)
    return AgentReadiness(
        "host",
        ReadinessState.READY,
        f"原生模型：{config.model_id} · OpenAI-compatible",
        _SETUP_HINT,
    )

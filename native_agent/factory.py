"""Concrete native-host provider binding; generic runtime stays provider-free."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from adapters.base import DEFAULT_AGENT_INACTIVITY_TIMEOUT

from .config import (
    MODEL_ID_ENV,
    NativeModelConfig,
    NativeModelConfigurationError,
)
from .model import ModelProvider, ModelProviderError
from .openai_compatible import OpenAICompatibleProvider
from .runtime import HOST_SYSTEM_PROMPT, NativeAgentRuntime


@dataclass
class OpenAICompatibleEnvironmentResolver:
    """Resolve one provider/model pair lazily from current process config."""

    environ: Mapping[str, str] | None = None

    def __call__(self) -> tuple[ModelProvider, str]:
        try:
            config = NativeModelConfig.from_environ(self.environ)
        except NativeModelConfigurationError as exc:
            hint = (
                f"；请设置 {MODEL_ID_ENV}（精确 id 可由 /v1/models 获取）"
                if "未配置" in str(exc) else ""
            )
            raise ModelProviderError(
                f"原生 host 配置无效：{exc}{hint}") from exc
        return (
            OpenAICompatibleProvider(
                config.base_url,
                api_key=config.api_key,
            ),
            config.model_id,
        )


def create_native_host_runtime(
    *,
    environ: Mapping[str, str] | None = None,
    inactivity_timeout: float = DEFAULT_AGENT_INACTIVITY_TIMEOUT,
) -> NativeAgentRuntime:
    """Production factory for the myagents-owned, tool-less host runtime."""
    return NativeAgentRuntime.from_resolver(
        OpenAICompatibleEnvironmentResolver(environ),
        name="host",
        system_prompt=HOST_SYSTEM_PROMPT,
        inactivity_timeout=inactivity_timeout,
    )

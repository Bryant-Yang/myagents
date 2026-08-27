"""Concrete native-host provider binding; generic runtime stays provider-free."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from adapters.base import DEFAULT_AGENT_INACTIVITY_TIMEOUT

from .config import (
    MODEL_ID_ENV,
    NativeModelConfig,
    NativeModelConfigurationError,
    native_model_setup_hint,
)
from .model import ModelProvider, ModelProviderError
from .openai_compatible import OpenAICompatibleProvider
from .runtime import HOST_SYSTEM_PROMPT, NativeAgentRuntime


@dataclass
class OpenAICompatibleConfigResolver:
    """Resolve one provider/model pair lazily from file plus env overrides."""

    environ: Mapping[str, str] | None = None
    config_path: str | os.PathLike[str] | None = None

    def __call__(self) -> tuple[ModelProvider, str]:
        try:
            config = NativeModelConfig.from_sources(
                self.environ,
                config_path=self.config_path,
            )
        except NativeModelConfigurationError as exc:
            hint = (
                f"；{native_model_setup_hint(self.environ, config_path=self.config_path)}，"
                "或临时设置 "
                f"{MODEL_ID_ENV}"
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
    config_path: str | os.PathLike[str] | None = None,
    inactivity_timeout: float = DEFAULT_AGENT_INACTIVITY_TIMEOUT,
) -> NativeAgentRuntime:
    """Production factory for the myagents-owned, tool-less host runtime."""
    return NativeAgentRuntime.from_resolver(
        OpenAICompatibleConfigResolver(environ, config_path),
        name="host",
        system_prompt=HOST_SYSTEM_PROMPT,
        inactivity_timeout=inactivity_timeout,
    )


# Compatibility name retained for callers of the first native-host release.
OpenAICompatibleEnvironmentResolver = OpenAICompatibleConfigResolver

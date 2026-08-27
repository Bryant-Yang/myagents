"""Neutral model-provider interface used by the native agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, Sequence


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ModelTerminal:
    """Provider proof that its wire protocol reached an authoritative end."""

    protocol: str
    signal: str
    reason: str


@dataclass(frozen=True)
class ModelEvent:
    kind: str
    text: str = ""
    meta: dict = field(default_factory=dict)
    terminal: ModelTerminal | None = None


class ModelProviderError(RuntimeError):
    """A provider rejected or could not begin a model request."""


class ModelProviderUncertainError(ModelProviderError):
    """A committed model stream ended without an authoritative terminal."""


class ModelProvider(Protocol):
    """Provider-neutral streaming model seam; no agent/tool concepts."""

    async def list_models(self) -> tuple[str, ...]:
        ...

    def stream(
        self,
        messages: Sequence[ModelMessage],
        *,
        model_id: str,
    ) -> AsyncIterator[ModelEvent]:
        ...

    async def aclose(self) -> None:
        ...


class ModelProviderResolver(Protocol):
    """Lazy provider/model binding supplied by a concrete factory module."""

    def __call__(self) -> tuple[ModelProvider, str]:
        ...

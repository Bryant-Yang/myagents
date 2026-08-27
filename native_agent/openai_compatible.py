"""OpenAI-compatible Chat Completions provider adapter.

This module implements one provider at the neutral ``ModelProvider`` seam.
Responses API or Anthropic support belongs in sibling adapters, not in the
runtime or orchestrator.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

import httpx

from adapters.base import redact_sensitive_text

from .model import (
    ModelEvent,
    ModelDeliveryState,
    ModelMessage,
    ModelProviderCapabilities,
    ModelTerminal,
    ModelProviderError,
    ModelProviderUncertainError,
)


_MAX_ERROR_BYTES = 16 * 1024
_MAX_MODELS_BYTES = 1024 * 1024
_MAX_SSE_LINE_BYTES = 4 * 1024 * 1024


class OpenAICompatibleProvider:
    """HTTP/SSE adapter for ``/models`` and ``/chat/completions``."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        connect_timeout: float = 10.0,
        request_timeout: float = 30.0,
        models_discovery: bool = True,
    ) -> None:
        if connect_timeout <= 0 or request_timeout <= 0:
            raise ValueError("provider timeout 必须大于 0")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.capabilities = ModelProviderCapabilities(
            models_discovery=models_discovery)
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=None,
                write=request_timeout,
                pool=request_timeout,
            ),
        )
        self._model_ids: tuple[str, ...] | None = None
        self.delivery_state = ModelDeliveryState.NOT_ATTEMPTED
        self._closed = False

    async def list_models(self) -> tuple[str, ...]:
        self._require_open()
        if not self.capabilities.models_discovery:
            raise ModelProviderError("当前模型 provider 不支持模型列表发现")
        try:
            async with self._client.stream(
                "GET",
                f"{self._base_url}/models",
                timeout=httpx.Timeout(30.0),
            ) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        if int(declared) > _MAX_MODELS_BYTES:
                            raise ModelProviderError(
                                "模型服务 /v1/models 响应超过 1 MiB 上限")
                    except ValueError:
                        pass
                body, overflow = await self._read_limited(
                    response, _MAX_MODELS_BYTES)
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"模型服务 /v1/models 不可用：{self._safe(str(exc))}") from exc
        if overflow:
            raise ModelProviderError("模型服务 /v1/models 响应超过 1 MiB 上限")
        if response.status_code < 200 or response.status_code >= 300:
            raise ModelProviderError(
                f"模型服务 /v1/models 返回 HTTP {response.status_code}："
                f"{self._safe(body.decode('utf-8', errors='replace'))}")
        try:
            payload = json.loads(body)
            data = payload["data"]
            if not isinstance(data, list):
                raise TypeError("data 不是数组")
            model_ids = tuple(
                item["id"] for item in data
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item["id"].strip()
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelProviderError(
                "模型服务 /v1/models 返回了无效响应") from exc
        if not model_ids:
            raise ModelProviderError("模型服务 /v1/models 没有可用模型 id")
        self._model_ids = model_ids
        return model_ids

    def stream(
        self,
        messages: Sequence[ModelMessage],
        *,
        model_id: str,
    ) -> AsyncIterator[ModelEvent]:
        """Reset delivery state synchronously before returning the iterator."""
        self._require_open()
        self.delivery_state = ModelDeliveryState.NOT_ATTEMPTED
        return self._stream(messages, model_id=model_id)

    async def _stream(
        self,
        messages: Sequence[ModelMessage],
        *,
        model_id: str,
    ) -> AsyncIterator[ModelEvent]:
        if self.capabilities.models_discovery:
            model_ids = self._model_ids or await self.list_models()
            if model_id not in model_ids:
                available = "、".join(model_ids[:10])
                suffix = "…" if len(model_ids) > 10 else ""
                raise ModelProviderError(
                    self._safe(
                        f"模型 {model_id!r} 不在 /v1/models 中；可用："
                        f"{available}{suffix}"))
        payload = {
            "model": model_id,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "stream": True,
        }
        # A non-2xx response is an authoritative pre-commit rejection.  Once
        # 2xx headers arrive the service has accepted the turn, so every later
        # transport failure is uncertain and must not be replayed.
        committed = False
        terminal_reason: str | None = None
        self.delivery_state = ModelDeliveryState.ATTEMPTED
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    self.delivery_state = ModelDeliveryState.REJECTED
                    body, _overflow = await self._read_limited(
                        response, _MAX_ERROR_BYTES)
                    raise ModelProviderError(
                        f"模型请求返回 HTTP {response.status_code}："
                        f"{self._safe(body.decode('utf-8', errors='replace'))}")
                committed = True
                self.delivery_state = ModelDeliveryState.COMMITTED
                yield ModelEvent("committed")
                async for line in self._iter_sse_lines(response):
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith(":"):
                        yield ModelEvent("activity")
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        if terminal_reason is None:
                            raise ModelProviderUncertainError(
                                "模型流缺少 finish_reason")
                        yield ModelEvent(
                            "done",
                            meta={"finish_reason": terminal_reason},
                            terminal=ModelTerminal(
                                "openai-compatible-chat-completions",
                                "finish_reason+[DONE]",
                                terminal_reason,
                            ),
                        )
                        return
                    try:
                        event = json.loads(raw)
                        choice = event["choices"][0]
                        if not isinstance(choice, dict):
                            raise TypeError("choice 不是对象")
                    except (json.JSONDecodeError, KeyError, IndexError,
                            TypeError) as exc:
                        raise ModelProviderUncertainError(
                            "模型 SSE 返回了无效事件") from exc
                    delta = choice.get("delta")
                    if isinstance(delta, dict):
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            yield ModelEvent("text", text)
                    finish_reason = choice.get("finish_reason")
                    if isinstance(finish_reason, str) and finish_reason:
                        terminal_reason = finish_reason
                    usage = event.get("usage")
                    if isinstance(usage, dict):
                        yield ModelEvent("usage", meta=dict(usage))
        except ModelProviderError:
            raise
        except httpx.HTTPError as exc:
            message = self._safe(str(exc))
            if self.delivery_state in {
                ModelDeliveryState.ATTEMPTED,
                ModelDeliveryState.COMMITTED,
            }:
                raise ModelProviderUncertainError(
                    f"模型流在提交后断开：{message}") from exc
            raise ModelProviderError(f"模型请求失败：{message}") from exc
        if committed:
            raise ModelProviderUncertainError("模型流结束但缺少 [DONE] 终态")
        raise ModelProviderError("模型请求未开始")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    def _require_open(self) -> None:
        if self._closed:
            raise ModelProviderError("模型 provider 已关闭")

    async def _read_limited(
        self,
        response: httpx.Response,
        limit: int,
    ) -> tuple[bytes, bool]:
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            remaining = limit - len(chunks)
            if len(chunk) > remaining:
                chunks.extend(chunk[:max(0, remaining)])
                return bytes(chunks), True
            chunks.extend(chunk)
        return bytes(chunks), False

    async def _iter_sse_lines(
        self,
        response: httpx.Response,
    ) -> AsyncIterator[str]:
        """Split SSE incrementally so one unterminated line stays bounded."""
        pending = bytearray()
        async for chunk in response.aiter_bytes():
            start = 0
            while True:
                newline = chunk.find(b"\n", start)
                if newline < 0:
                    tail = chunk[start:]
                    if len(pending) + len(tail) > _MAX_SSE_LINE_BYTES:
                        raise ModelProviderUncertainError(
                            "模型 SSE 单行超过 4 MiB 上限")
                    pending.extend(tail)
                    break
                segment = chunk[start:newline]
                if len(pending) + len(segment) > _MAX_SSE_LINE_BYTES:
                    raise ModelProviderUncertainError(
                        "模型 SSE 单行超过 4 MiB 上限")
                pending.extend(segment)
                raw = bytes(pending)
                pending.clear()
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                yield raw.decode("utf-8", errors="replace")
                start = newline + 1
        if pending:
            yield bytes(pending).decode("utf-8", errors="replace")

    def _safe(self, value: str) -> str:
        text = value
        if self._api_key:
            text = text.replace(self._api_key, "[已隐藏]")
        return redact_sensitive_text(text, limit=2000)

"""Tool-less native agent runtime over a neutral model provider."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from adapters.base import (
    DEFAULT_AGENT_INACTIVITY_TIMEOUT,
    AgentDeliveryCancelledError,
    AgentDeliveryUncertainError,
    AgentEvent,
    ExecutionMode,
)

from .model import (
    ModelEvent,
    ModelDeliveryState,
    ModelMessage,
    ModelProvider,
    ModelProviderError,
    ModelProviderResolver,
    ModelProviderUncertainError,
)


HOST_SYSTEM_PROMPT = """\
你是 myagents 聊天室的 host（moderator/supervisor）。你负责理解用户意图、
直接回答、把任务路由给已给定的 worker、主持有界讨论并总结结果。
你没有任何工具：不能读取或写入文件，不能运行命令，不能访问网络，也不能调用
skill。不得声称已经执行这些动作。无论调用方使用何种授权模式，都保持无工具。
严格遵循每次用户消息中给出的输出格式和候选闭集。"""


@dataclass(frozen=True)
class NativeSessionPreparation:
    """Native runtime equivalent of the stateful adapter prepare contract."""

    session_id: str
    restored: bool
    load_failed: bool
    fresh: bool


class NativeAgentRuntime:
    """Stateful ``AgentAdapter`` backed only by a model provider.

    The runtime deliberately has no tool or permission interface. Execution
    mode and TUI ``/yolo`` therefore cannot grant shell or file capabilities.
    A provider can be injected for tests or resolved lazily by a concrete
    factory, so an unconfigured host remains constructible and is blocked by
    readiness before the first request.
    """

    stateful_session = True
    tool_policy = "none"

    def __init__(
        self,
        provider: ModelProvider,
        *,
        model_id: str,
        name: str = "native",
        system_prompt: str,
        inactivity_timeout: float = DEFAULT_AGENT_INACTIVITY_TIMEOUT,
    ) -> None:
        if not model_id.strip():
            raise ValueError("model_id 不能为空")
        self._initialize(
            provider=provider,
            model_id=model_id,
            name=name,
            system_prompt=system_prompt,
            inactivity_timeout=inactivity_timeout,
            resolver=None,
        )

    @classmethod
    def from_resolver(
        cls,
        resolver: ModelProviderResolver,
        *,
        name: str = "host",
        system_prompt: str = HOST_SYSTEM_PROMPT,
        inactivity_timeout: float = DEFAULT_AGENT_INACTIVITY_TIMEOUT,
    ) -> "NativeAgentRuntime":
        """Create lazily without binding runtime code to a provider type."""
        instance = cls.__new__(cls)
        instance._initialize(
            provider=None,
            model_id=None,
            name=name,
            system_prompt=system_prompt,
            inactivity_timeout=inactivity_timeout,
            resolver=resolver,
        )
        return instance

    def _initialize(
        self,
        *,
        provider: ModelProvider | None,
        model_id: str | None,
        name: str,
        system_prompt: str,
        inactivity_timeout: float,
        resolver: ModelProviderResolver | None,
    ) -> None:
        if not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")
        if inactivity_timeout <= 0:
            raise ValueError("inactivity_timeout 必须大于 0")
        self.name = name
        self.session_id: str | None = str(uuid.uuid4())
        self._provider = provider
        self._model_id = model_id
        self._system_prompt = system_prompt
        self._inactivity_timeout = inactivity_timeout
        self._provider_resolver = resolver
        self._messages: list[ModelMessage] = []
        self._lock = asyncio.Lock()
        self._closed = False
        self._prepared_once = False
        self._suppress_replay_once = False
        # A clean fresh runtime may bootstrap bounded history. After a
        # committed uncertain/cancelled turn, the next fresh session must not
        # rewind the durable cursor and replay that turn.
        self.replay_history_on_fresh_session = True

    def stream(
        self,
        prompt: str,
        workdir: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        return self._stream(prompt, workdir, execution_mode=execution_mode)

    async def _stream(
        self,
        prompt: str,
        _workdir: str,
        *,
        execution_mode: ExecutionMode,
    ) -> AsyncIterator[AgentEvent]:
        del execution_mode  # The profile remains tool-less in every mode.
        async with self._lock:
            self._prepared_once = True
            async for event in self._stream_locked(prompt):
                yield event

    async def stream_prepared(
        self,
        make_prompt: Callable[[NativeSessionPreparation], str],
        workdir: str,
        resume_session_id: str | None = None,
        *,
        execution_mode: ExecutionMode = ExecutionMode.DEFAULT,
    ) -> AsyncIterator[AgentEvent]:
        """Prepare/checkpoint/prompt under the same single-writer lock."""
        del workdir, execution_mode
        async with self._lock:
            self._require_open()
            restored = (
                resume_session_id is not None
                and resume_session_id == self.session_id
            )
            load_failed = resume_session_id is not None and not restored
            fresh = not self._prepared_once or load_failed
            if load_failed:
                self.session_id = str(uuid.uuid4())
                self._messages.clear()
            prep = NativeSessionPreparation(
                self.session_id or str(uuid.uuid4()),
                restored=restored,
                load_failed=load_failed,
                fresh=fresh,
            )
            self.session_id = prep.session_id
            self.replay_history_on_fresh_session = (
                not self._suppress_replay_once
            )
            try:
                prompt = make_prompt(prep)
            except BaseException:
                if fresh:
                    self.session_id = str(uuid.uuid4())
                    self._messages.clear()
                    self._prepared_once = False
                raise
            self._suppress_replay_once = False
            self._prepared_once = True
            async for event in self._stream_locked(prompt):
                yield event

    async def _stream_locked(self, prompt: str) -> AsyncIterator[AgentEvent]:
        self._require_open()
        provider, model_id = self._ensure_provider()
        request_messages = (
            [ModelMessage("system", self._system_prompt)]
            + self._messages
            + [ModelMessage("user", prompt)]
        )
        reply: list[str] = []
        completed = False
        committed = False
        provider_stream = provider.stream(
            request_messages, model_id=model_id).__aiter__()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        anext(provider_stream),
                        timeout=self._inactivity_timeout,
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    delivery_state = provider.delivery_state
                    if committed or delivery_state in {
                        ModelDeliveryState.ATTEMPTED,
                        ModelDeliveryState.COMMITTED,
                    }:
                        if not committed:
                            yield AgentEvent("delivery_committed")
                        self._poison_session()
                        raise AgentDeliveryUncertainError(
                            "原生模型会话连续 "
                            f"{self._inactivity_timeout:g} 秒无活动，"
                            "已取消本轮请求") from None
                    raise ModelProviderError(
                        "模型请求在提交前连续 "
                        f"{self._inactivity_timeout:g} 秒无活动") from None
                if event.kind == "committed":
                    committed = True
                elif event.kind == "text":
                    reply.append(event.text)
                elif event.kind == "done":
                    terminal = event.terminal
                    if (terminal is None
                            or not terminal.protocol
                            or not terminal.signal
                            or not terminal.reason):
                        raise ModelProviderUncertainError(
                            "模型 provider 未提供权威终态证明")
                    completed = True
                mapped = self._map_event(event)
                if mapped is not None:
                    yield mapped
        except ModelProviderUncertainError as exc:
            delivery_state = provider.delivery_state
            if delivery_state in {
                ModelDeliveryState.ATTEMPTED,
                ModelDeliveryState.COMMITTED,
            } and not committed:
                yield AgentEvent("delivery_committed")
            self._poison_session()
            raise AgentDeliveryUncertainError(str(exc)) from exc
        except asyncio.CancelledError as exc:
            delivery_state = provider.delivery_state
            request_attempted = delivery_state in {
                ModelDeliveryState.ATTEMPTED,
                ModelDeliveryState.COMMITTED,
            }
            if committed or request_attempted:
                if not committed:
                    yield AgentEvent("delivery_committed")
                self._poison_session()
                raise AgentDeliveryCancelledError(
                    "原生模型请求已提交并被取消") from exc
            raise
        finally:
            if not completed:
                closer = getattr(provider_stream, "aclose", None)
                if closer is not None:
                    try:
                        await closer()
                    except (asyncio.CancelledError, Exception):
                        # Delivery/cancellation is authoritative; stream
                        # teardown must not replace it.
                        pass
        if not completed:
            if committed:
                self._poison_session()
                raise AgentDeliveryUncertainError("模型流缺少权威终态")
            raise ModelProviderError("模型请求未开始")
        self._messages.extend((
            ModelMessage("user", prompt),
            ModelMessage("assistant", "".join(reply)),
        ))

    def _ensure_provider(self) -> tuple[ModelProvider, str]:
        if self._provider is not None and self._model_id is not None:
            return self._provider, self._model_id
        if self._provider_resolver is None:
            raise ModelProviderError("native model provider 未初始化")
        try:
            self._provider, self._model_id = self._provider_resolver()
        except ModelProviderError:
            raise
        except Exception as exc:
            raise ModelProviderError(
                "模型 provider 解析失败") from exc
        if not isinstance(self._model_id, str) or not self._model_id.strip():
            raise ModelProviderError("模型 provider 未返回有效 model id")
        return self._provider, self._model_id

    def _require_open(self) -> None:
        if self._closed:
            raise ModelProviderError("native agent runtime 已关闭")

    def _poison_session(self) -> None:
        self.session_id = str(uuid.uuid4())
        self._messages.clear()
        self._prepared_once = False
        self._suppress_replay_once = True
        self.replay_history_on_fresh_session = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._provider is not None:
            await self._provider.aclose()

    @staticmethod
    def _map_event(event: ModelEvent) -> AgentEvent | None:
        if event.kind == "committed":
            return AgentEvent("delivery_committed")
        if event.kind == "text":
            return AgentEvent("text", event.text)
        if event.kind == "activity":
            return AgentEvent("activity")
        if event.kind == "usage":
            return AgentEvent("info", "模型用量", {"usage": event.meta})
        if event.kind == "done":
            return AgentEvent("done", meta=dict(event.meta))
        return None

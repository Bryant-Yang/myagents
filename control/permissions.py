"""可重新连接的 daemon 权限等待模块。

权限请求的完整参数只留在拥有 adapter 的进程内；控制客户端只能看到脱敏标题
与本次服务端给出的 option 闭集。任何未知 request/option 或关闭竞态都
fail-closed 为 cancelled。
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from adapters.base import redact_sensitive_text

MAX_PENDING_PERMISSIONS = 32
MAX_PERMISSION_OPTIONS = 32
MAX_OPTION_ID_CHARS = 200


class PermissionBrokerError(ValueError):
    """权限等待或选择不满足当前请求契约。"""


@dataclass(frozen=True)
class PermissionOption:
    option_id: str
    kind: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "option_id": self.option_id,
            "kind": self.kind,
            "name": self.name,
        }


@dataclass(frozen=True)
class PermissionRequest:
    request_id: str
    agent: str
    tool_call: dict[str, str]
    options: tuple[PermissionOption, ...]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "agent": self.agent,
            "tool_call": dict(self.tool_call),
            "options": [option.to_dict() for option in self.options],
            "created_at": self.created_at,
        }


@dataclass
class _PendingPermission:
    request: PermissionRequest
    future: asyncio.Future[dict[str, str]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class PermissionBroker:
    """把 adapter 权限等待收敛为 list/resolve/aclose 三个操作。"""

    def __init__(self, *, max_pending: int = MAX_PENDING_PERMISSIONS) -> None:
        if (isinstance(max_pending, bool) or not isinstance(max_pending, int)
                or not 1 <= max_pending <= 128):
            raise ValueError("max_pending 必须是 1..128 的整数")
        self._max_pending = max_pending
        self._pending: OrderedDict[str, _PendingPermission] = OrderedDict()
        self._closed = False

    async def request(self, agent: str, params: dict[str, Any]) -> dict[str, str]:
        if self._closed:
            return {"outcome": "cancelled"}
        if not isinstance(agent, str) or not agent.strip():
            raise PermissionBrokerError("agent 必须是非空字符串")
        if not isinstance(params, dict):
            raise PermissionBrokerError("权限参数必须是 object")
        if len(self._pending) >= self._max_pending:
            return {"outcome": "cancelled"}

        tool = params.get("toolCall")
        if not isinstance(tool, dict):
            tool = {}
        title = redact_sensitive_text(
            str(tool.get("title") or "(未命名工具)"), limit=500
        )
        tool_call = {"title": title}
        tool_call_id = tool.get("toolCallId")
        if isinstance(tool_call_id, str) and tool_call_id:
            tool_call["tool_call_id"] = redact_sensitive_text(
                tool_call_id, limit=200
            )

        raw_options = params.get("options")
        if not isinstance(raw_options, list):
            raw_options = []
        if len(raw_options) > MAX_PERMISSION_OPTIONS:
            return {"outcome": "cancelled"}
        options: list[PermissionOption] = []
        seen: set[str] = set()
        for raw in raw_options:
            if not isinstance(raw, dict):
                continue
            option_id = raw.get("optionId")
            if (not isinstance(option_id, str) or not option_id
                    or len(option_id) > MAX_OPTION_ID_CHARS
                    or option_id in seen):
                if isinstance(option_id, str) and option_id:
                    return {"outcome": "cancelled"}
                continue
            seen.add(option_id)
            options.append(PermissionOption(
                option_id=option_id,
                kind=redact_sensitive_text(
                    str(raw.get("kind") or ""), limit=80
                ),
                name=redact_sensitive_text(
                    str(raw.get("name") or "使用此选项"), limit=160
                ),
            ))

        request = PermissionRequest(
            request_id=str(uuid.uuid4()),
            agent=redact_sensitive_text(" ".join(agent.split()), limit=80),
            tool_call=tool_call,
            options=tuple(options),
            created_at=_utc_now(),
        )
        future: asyncio.Future[dict[str, str]] = (
            asyncio.get_running_loop().create_future()
        )
        pending = _PendingPermission(request, future)
        self._pending[request.request_id] = pending
        try:
            return await future
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        finally:
            if self._pending.get(request.request_id) is pending:
                self._pending.pop(request.request_id, None)

    def list_pending(self) -> tuple[PermissionRequest, ...]:
        return tuple(item.request for item in self._pending.values())

    def resolve(
        self,
        request_id: str,
        *,
        outcome: str,
        option_id: str | None = None,
    ) -> dict[str, str]:
        if not isinstance(request_id, str) or not request_id:
            raise PermissionBrokerError("request_id 必须是非空字符串")
        pending = self._pending.get(request_id)
        if pending is None:
            raise PermissionBrokerError("权限请求不存在或已经解决")
        if outcome == "cancelled":
            if option_id is not None:
                raise PermissionBrokerError("cancelled 不接受 option_id")
            result = {"outcome": "cancelled"}
        elif outcome == "selected":
            if not isinstance(option_id, str) or not option_id:
                raise PermissionBrokerError("selected 必须提供 option_id")
            allowed = {
                option.option_id for option in pending.request.options
            }
            if option_id not in allowed:
                raise PermissionBrokerError("option_id 不属于本次权限请求")
            result = {"outcome": "selected", "optionId": option_id}
        else:
            raise PermissionBrokerError("outcome 只能是 selected 或 cancelled")
        self._pending.pop(request_id, None)
        if not pending.future.done():
            pending.future.set_result(result)
        return result

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = tuple(self._pending.values())
        self._pending.clear()
        for item in pending:
            if not item.future.done():
                item.future.set_result({"outcome": "cancelled"})
        await asyncio.sleep(0)

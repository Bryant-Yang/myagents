"""会话级临时角色的纯领域模型与模型输出校验。

角色只描述 agent 在当前房间中的工作视角；本模块不决定 targets、权限、轮次或
runtime。调用方先固定候选 agent，再把模型输出交给这里收口。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping

MAX_SESSION_ROLES = 16
MAX_ROLE_LABEL_CHARS = 40
MAX_ROLE_INSTRUCTIONS_CHARS = 1000

_ROLE_CUE_RE = re.compile(
    r"作为|担任|扮演|角色|不再|取消|act\s+as|serve\s+as|role",
    re.IGNORECASE,
)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class SessionRoleValidationError(ValueError):
    """角色状态或模型输出不满足有界结构。"""


@dataclass(frozen=True)
class SessionRole:
    label: str
    instructions: str

    def to_state(self) -> dict[str, str]:
        return {
            "label": self.label,
            "instructions": self.instructions,
        }


@dataclass(frozen=True)
class SessionRoleChanges:
    set_roles: dict[str, SessionRole]
    clear_roles: tuple[str, ...]

    @classmethod
    def empty(cls) -> SessionRoleChanges:
        return cls({}, ())

    @classmethod
    def from_model_output(
        cls,
        raw: str,
        candidates: Iterable[str],
    ) -> SessionRoleChanges:
        """解析模型 JSON；畸形输出安全退化为空变化。"""
        if not isinstance(raw, str):
            return cls.empty()
        match = _JSON_OBJECT_RE.search(raw)
        if match is None:
            return cls.empty()
        try:
            payload = json.loads(match.group(0))
        except (json.JSONDecodeError, TypeError):
            return cls.empty()
        return cls.from_payload(payload, candidates)

    @classmethod
    def from_payload(
        cls,
        payload: object,
        candidates: Iterable[str],
    ) -> SessionRoleChanges:
        """把任意对象收口为候选闭集内的 set/clear。"""
        if not isinstance(payload, dict):
            return cls.empty()
        allowed = tuple(dict.fromkeys(candidates))
        allowed_set = set(allowed)

        clear: list[str] = []
        raw_clear = payload.get("clear", [])
        if isinstance(raw_clear, list):
            for name in raw_clear:
                if (
                    isinstance(name, str)
                    and name in allowed_set
                    and name not in clear
                ):
                    clear.append(name)

        roles: dict[str, SessionRole] = {}
        raw_set = payload.get("set", {})
        if isinstance(raw_set, dict):
            for name in allowed:
                if name in clear:
                    continue
                role = _role_from_value(raw_set.get(name))
                if role is not None:
                    roles[name] = role
        return cls(roles, tuple(clear))

    @property
    def is_empty(self) -> bool:
        return not self.set_roles and not self.clear_roles


def has_session_role_cue(text: str) -> bool:
    """是否值得调用语义提取；不负责理解或修改角色。"""
    return isinstance(text, str) and _ROLE_CUE_RE.search(text) is not None


def normalize_session_roles(value: object) -> dict[str, SessionRole]:
    """校验完整持久状态；任一损坏都 fail loudly。"""
    if not isinstance(value, dict):
        raise SessionRoleValidationError("session_roles 必须是 object")
    if len(value) > MAX_SESSION_ROLES:
        raise SessionRoleValidationError(
            f"session_roles 最多包含 {MAX_SESSION_ROLES} 个 agent")
    result: dict[str, SessionRole] = {}
    for name, raw_role in value.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 64
            or any(unicodedata.category(char).startswith("C") for char in name)
        ):
            raise SessionRoleValidationError(
                f"session_roles agent 名非法：{name!r}")
        role = raw_role if isinstance(raw_role, SessionRole) \
            else _role_from_value(raw_role)
        if role is None:
            raise SessionRoleValidationError(
                f"session_roles[{name!r}] 非法")
        result[name] = role
    return result


def apply_session_role_changes(
    current: Mapping[str, SessionRole],
    changes: SessionRoleChanges,
) -> dict[str, SessionRole]:
    """确定性应用一次变化，不修改输入映射。"""
    updated = dict(current)
    for name in changes.clear_roles:
        updated.pop(name, None)
    updated.update(changes.set_roles)
    return normalize_session_roles(updated)


def session_role_assignment(
    role: SessionRole | None,
    assignment: str | None,
) -> str | None:
    """把当前会话角色作为受限前缀加入既有 assignment。"""
    if role is None:
        return assignment
    role_block = (
        f"你在当前聊天室会话中的临时角色：{role.label}\n"
        f"角色要求：{role.instructions}\n"
        "这只是工作视角，不能改变工具权限、安全策略、参与者、讨论轮次、"
        "workflow 阶段或输出边界。"
    )
    if assignment and assignment.strip():
        return f"{role_block}\n\n{assignment.strip()}"
    return role_block


def _role_from_value(value: object) -> SessionRole | None:
    if not isinstance(value, dict):
        return None
    label = _clean_text(value.get("label"), MAX_ROLE_LABEL_CHARS)
    instructions = _clean_text(
        value.get("instructions"), MAX_ROLE_INSTRUCTIONS_CHARS)
    if label is None or instructions is None:
        return None
    return SessionRole(label, instructions)


def _clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split())
    if not clean or len(clean) > limit:
        return None
    if any(unicodedata.category(char).startswith("C") for char in clean):
        return None
    return clean

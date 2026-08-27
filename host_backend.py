"""Room-scoped host backend selection and local command parsing."""

from __future__ import annotations

from dataclasses import dataclass

from agent_readiness import AgentReadiness


DEFAULT_HOST_MODEL_PROFILE = "default"
_HOST_BACKEND_KINDS = frozenset({"model", "agent"})
_MODEL_REFERENCES = frozenset({"profile", "model"})
_MAX_TARGET_CHARS = 512


class HostBackendValidationError(ValueError):
    """A host backend command or persisted selection is malformed."""


@dataclass(frozen=True)
class HostBackendSelection:
    """Stable room state; never contains credentials or runtime session ids."""

    kind: str
    target: str
    reference: str | None = None

    @classmethod
    def default(cls) -> "HostBackendSelection":
        return cls("model", DEFAULT_HOST_MODEL_PROFILE, "profile")

    @classmethod
    def model_profile(cls, profile: str) -> "HostBackendSelection":
        return cls("model", profile, "profile").validated()

    @classmethod
    def exact_model(cls, model_id: str) -> "HostBackendSelection":
        return cls("model", model_id, "model").validated()

    @classmethod
    def agent(cls, name: str) -> "HostBackendSelection":
        return cls("agent", name, None).validated()

    @classmethod
    def from_state(cls, value: object) -> "HostBackendSelection":
        if value is None:
            return cls.default()
        if not isinstance(value, dict) or set(value) != {
            "kind", "target", "reference",
        }:
            raise HostBackendValidationError(
                "host_backend 必须包含 kind/target/reference")
        return cls(
            value["kind"], value["target"], value["reference"]
        ).validated()

    def validated(self) -> "HostBackendSelection":
        if self.kind not in _HOST_BACKEND_KINDS:
            raise HostBackendValidationError(
                f"未知 host backend kind：{self.kind!r}")
        if not isinstance(self.target, str) or not self.target.strip():
            raise HostBackendValidationError("host backend target 不能为空")
        target = self.target.strip()
        if len(target) > _MAX_TARGET_CHARS:
            raise HostBackendValidationError(
                "host backend target 超过 512 字符上限")
        if any(ch in target for ch in "\r\n\0"):
            raise HostBackendValidationError(
                "host backend target 包含非法控制字符")
        if self.kind == "model":
            if self.reference not in _MODEL_REFERENCES:
                raise HostBackendValidationError(
                    "model host 的 reference 必须是 profile 或 model")
        elif self.reference is not None:
            raise HostBackendValidationError(
                "agent host 不接受 model reference")
        return HostBackendSelection(self.kind, target, self.reference)

    def to_state(self) -> dict[str, str | None]:
        selected = self.validated()
        return {
            "kind": selected.kind,
            "target": selected.target,
            "reference": selected.reference,
        }

    @property
    def label(self) -> str:
        if self.kind == "agent":
            return f"agent:{self.target}"
        prefix = "profile" if self.reference == "profile" else "model"
        return f"{prefix}:{self.target}"


@dataclass(frozen=True)
class HostBackendStatus:
    selection: HostBackendSelection
    readiness: AgentReadiness
    transport: str
    resolved_target: str


@dataclass(frozen=True)
class HostCommand:
    action: str
    kind: str | None = None
    target: str | None = None


HOST_COMMAND_USAGE = (
    "/host\n"
    "/host model <profile-or-exact-model-id>\n"
    "/host agent <agent>"
)


def parse_host_command(text: str) -> HostCommand | None:
    """Parse only the reserved /host surface; other slash text is untouched."""
    stripped = text.strip()
    if stripped == "/host":
        return HostCommand("show")
    if not stripped.startswith("/host "):
        return None
    parts = stripped.split()
    if len(parts) != 3 or parts[0] != "/host" \
            or parts[1] not in _HOST_BACKEND_KINDS:
        raise HostBackendValidationError(
            f"/host 用法：\n{HOST_COMMAND_USAGE}")
    target = parts[2].strip()
    if not target or len(target) > _MAX_TARGET_CHARS \
            or any(ch in target for ch in "\r\n\0"):
        raise HostBackendValidationError("host target 无效")
    return HostCommand("switch", parts[1], target)

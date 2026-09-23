"""runtime-contract.json 是 DSH 身份与版本号的唯一事实源。

全仓库不再允许任何代码、测试或文档复制具体版本字符串：
Python adapter、TypeScript plugin（经 esbuild/vitest define 注入）、
refresh 脚本与验收 gate 全部以本模块加载的契约为准。升级 DSH 版本时
只运行 ``scripts/dsh-contract-refresh.py --accept`` 重写契约文件，
其余位置零改动。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

_CONTRACT_PATH = Path(__file__).resolve().parent / "plugin" / "runtime-contract.json"

_SCHEMA_VERSION = 5
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ContractError(RuntimeError):
    """契约文件缺失、损坏或 schema 不被当前代码理解。"""


def contract_path() -> Path:
    """契约文件的仓库内路径。"""
    return _CONTRACT_PATH


def validate(contract: object, *, path: Path | None = None) -> dict:
    """校验契约结构；返回原 dict。结构漂移必须在这里 fail-closed。"""
    label = f"DSH runtime contract（{path or _CONTRACT_PATH}）"
    if not isinstance(contract, dict):
        raise ContractError(f"{label} 必须是 JSON object")
    if contract.get("schemaVersion") != _SCHEMA_VERSION:
        raise ContractError(
            f"{label} schemaVersion 不被理解：期望 {_SCHEMA_VERSION}，"
            f"实际 {contract.get('schemaVersion')!r}"
        )
    for key in ("hostVersion",):
        value = contract.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ContractError(f"{label} 缺少非空 {key}")
    dsh_root = contract.get("dshRoot")
    if not isinstance(dsh_root, dict) or not isinstance(
        dsh_root.get("version"), str
    ) or not dsh_root["version"].strip():
        raise ContractError(f"{label} 缺少非空 dshRoot.version")
    for key in ("compatibilityRevision", "policyRevision"):
        value = contract.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(f"{label} 缺少 integer {key}")
    profile_bundle = contract.get("profileBundle")
    if not isinstance(profile_bundle, dict):
        raise ContractError(f"{label} 缺少 profileBundle")
    for key in ("entrySha256", "patchSha256"):
        value = profile_bundle.get(key)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ContractError(f"{label} profileBundle.{key} 不是 sha256")
    return contract


_LOCK = threading.Lock()
_CACHE: dict | None = None


def load(*, path: Path | None = None) -> dict:
    """加载并校验契约（进程内缓存）；失败抛 ContractError。

    测试可通过 monkeypatch 本函数注入合成契约。
    """
    global _CACHE
    target = path or _CONTRACT_PATH
    with _LOCK:
        if path is None and _CACHE is not None:
            return _CACHE
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"无法读取 {target}：{exc}") from exc
        contract = validate(payload, path=target)
        if path is None:
            _CACHE = contract
        return contract


def reset_cache() -> None:
    """丢弃进程内缓存（测试用）。"""
    global _CACHE
    with _LOCK:
        _CACHE = None


def plugin_version() -> str:
    """@myagents/dsh-acp-host 的 pinned 版本（hostVersion）。"""
    return load()["hostVersion"]


def runtime_version() -> str:
    """官方 @deepseek-ai/dsh runtime 的 pinned 版本。"""
    return load()["dshRoot"]["version"]


def compatibility_revision() -> int:
    """plugin ↔ adapter wire 契约 revision。"""
    return load()["compatibilityRevision"]


def policy_revision() -> int:
    """profile/工具闭集语义 revision。"""
    return load()["policyRevision"]


def profile_bundle_entry_sha256() -> str:
    """已构建 bundle entry 的 pinned sha256。"""
    return load()["profileBundle"]["entrySha256"]


def profile_bundle_patch_sha256() -> str:
    """cordis.patch.yml 的 pinned sha256。"""
    return load()["profileBundle"]["patchSha256"]

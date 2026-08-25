"""Pi permission bridge contract tests (Node stub only).

These tests load the production extension with a fake Pi extension API.  They
never start the real Pi CLI and never mutate the user's Pi configuration.

Run: .venv/bin/python tests/test_pi_permission_bridge.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "pi_rpc/extensions/myagents_permission_bridge.ts"
HARNESS = Path(__file__).with_name(
    "fixtures") / "pi_permission_bridge_harness.mjs"

VERSION = "myagents.pi.policy/v1"
ATTEST_PREFIX = "MYAGENTS_PI_ATTEST_V1:"
PERMISSION_PREFIX = "MYAGENTS_PI_PERMISSION_V1:"
STATUS_KEY = "myagents.pi.policy"
NONCE = "process_nonce_0123456789"

READ_TOOLS = [
    "myagents_read",
    "myagents_grep",
    "myagents_find",
    "myagents_ls",
]
ALL_TOOLS = [
    *READ_TOOLS,
    "myagents_edit",
    "myagents_write",
    "myagents_bash",
]


def _decode_title(title: str, prefix: str) -> dict:
    assert title.startswith(prefix), title
    encoded = title.removeprefix(prefix)
    encoded += "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))


def _bridge_hash() -> str:
    return hashlib.sha256(BRIDGE.read_bytes()).hexdigest()


def _stable_json(value: dict) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )


def _stable_json_bytes(value: dict) -> int:
    return len(_stable_json(value).encode("utf-8"))


def _args_hash(value: dict) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _run(
    workspace: Path,
    *,
    profile: str = "default",
    active_tools: list[str] | None = None,
    attestation_choice: str | None = None,
    permission_choices: list[str | None] | None = None,
    operations: list[dict] | None = None,
    env_overrides: dict[str, str | None] | None = None,
    bad_source_tool: str | None = None,
    context_cwd: str | None = None,
    has_ui: bool = True,
    defer_attestations: bool = False,
) -> dict:
    policy_hash = _bridge_hash()
    active = active_tools or (
        READ_TOOLS if profile == "read_only" else ALL_TOOLS
    )
    env: dict[str, str | None] = {
        "MYAGENTS_PI_POLICY_NONCE": NONCE,
        "MYAGENTS_PI_POLICY_HASH": policy_hash,
        "MYAGENTS_PI_POLICY_PATH": str(BRIDGE.resolve()),
        "MYAGENTS_PI_PROFILE": profile,
        "MYAGENTS_PI_WORKSPACE": str(workspace.resolve()),
    }
    env.update(env_overrides or {})
    config = {
        "caseId": os.urandom(6).hex(),
        "workspace": str(workspace),
        "outsidePath": str(workspace.parent / "outside"),
        "env": env,
        "activeTools": active,
        "attestationChoice": (
            f"ack:{NONCE}"
            if attestation_choice is None
            else attestation_choice
        ),
        "permissionChoices": permission_choices or [],
        "operations": operations or [],
        "badSourceTool": bad_source_tool,
        "contextCwd": context_cwd,
        "hasUI": has_ui,
        "deferAttestations": defer_attestations,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(config).encode("utf-8")
    ).decode("ascii")
    result = subprocess.run(
        ["node", str(HARNESS), str(BRIDGE), encoded],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["importError"] is None, output
    return output


def _call(tool: str, call_id: str, input_: dict) -> dict:
    return {
        "kind": "call",
        "toolName": tool,
        "toolCallId": call_id,
        "input": input_,
    }


def _execute(tool: str, call_id: str, input_: dict) -> dict:
    return {
        "kind": "execute",
        "toolName": tool,
        "toolCallId": call_id,
        "input": input_,
    }


def test_registers_only_unique_wrappers_and_exact_attestation() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-bridge-") as raw:
        workspace = Path(raw)
        output = _run(workspace)

    assert output["tools"] == ALL_TOOLS
    assert not ({"read", "grep", "find", "ls", "edit", "write", "bash"}
                & set(output["tools"]))
    assert output["commands"] == ["myagents-policy-v1"]
    assert len(output["selectCalls"]) == 1
    attestation = output["selectCalls"][0]
    payload = _decode_title(attestation["title"], ATTEST_PREFIX)
    assert payload == {
        "version": VERSION,
        "nonce": NONCE,
        "policyHash": _bridge_hash(),
        "profile": "default",
        "workspace": str(workspace.resolve()),
        "activeTools": ALL_TOOLS,
        "tools": [
            {
                "name": name,
                "sourceInfo": {
                    "path": str(BRIDGE.resolve()),
                    "source": "extension",
                    "scope": "temporary",
                    "origin": "top-level",
                },
            }
            for name in ALL_TOOLS
        ],
    }
    assert attestation["options"] == [f"ack:{NONCE}", f"deny:{NONCE}"]
    assert output["statusCalls"] == [{
        "key": STATUS_KEY,
        "text": f"ready:{NONCE}:{_bridge_hash()}",
    }]


def test_attestation_is_fail_closed_on_bad_ack_hash_tools_source_or_ui() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-attest-") as raw:
        workspace = Path(raw)
        cases = [
            {"attestation_choice": f"ack:{NONCE}-forged"},
            {"env_overrides": {"MYAGENTS_PI_POLICY_HASH": "0" * 64}},
            {"active_tools": [*ALL_TOOLS, "bash"]},
            {"bad_source_tool": "myagents_read"},
            {"has_ui": False},
        ]
        for case in cases:
            output = _run(
                workspace,
                operations=[
                    _call("myagents_read", "read-1", {"path": "a.txt"}),
                    _execute(
                        "myagents_read", "read-1", {"path": "a.txt"},
                    ),
                ],
                **case,
            )
            assert output["statusCalls"] == [], case
            assert output["operations"][0]["result"]["block"] is True
            assert "policy" in output["operations"][0]["result"]["reason"]
            assert "error" in output["operations"][1]
            assert output["delegateCalls"] == []


def test_session_start_returns_before_deferred_attestation_and_current_ack_readies() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-async-attest-") as raw:
        workspace = Path(raw)
        target = workspace / "ready.txt"
        target.write_text("ready", encoding="utf-8")
        canonical = str(target.resolve())
        output = _run(
            workspace,
            defer_attestations=True,
            operations=[
                _call("myagents_read", "too-early", {"path": canonical}),
                {"kind": "resolveAttestation", "index": 0, "choice": "$ACK"},
                _call("myagents_read", "after-ack", {"path": canonical}),
                _execute("myagents_read", "after-ack", {"path": canonical}),
            ],
        )

    assert output["sessionStarts"] == [{"reason": "startup", "returned": True}]
    assert output["operations"][0]["result"]["block"] is True
    assert "not ready" in output["operations"][0]["result"]["reason"]
    assert output["operations"][2]["result"] is None
    assert "result" in output["operations"][3]
    assert output["statusCalls"] == [{
        "key": STATUS_KEY,
        "text": f"ready:{NONCE}:{_bridge_hash()}",
    }]


def test_old_generation_ack_cannot_ready_a_reloaded_session() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-old-ack-") as raw:
        workspace = Path(raw)
        target = workspace / "generation.txt"
        target.write_text("generation", encoding="utf-8")
        canonical = str(target.resolve())
        output = _run(
            workspace,
            defer_attestations=True,
            operations=[
                {"kind": "sessionStart", "reason": "reload"},
                {"kind": "resolveAttestation", "index": 0, "choice": "$ACK"},
                _call("myagents_read", "old-ack", {"path": canonical}),
                {"kind": "resolveAttestation", "index": 1, "choice": "$ACK"},
                _call("myagents_read", "new-ack", {"path": canonical}),
                _execute("myagents_read", "new-ack", {"path": canonical}),
            ],
        )

    assert output["sessionStarts"] == [
        {"reason": "startup", "returned": True},
        {"reason": "reload", "returned": True},
    ]
    assert output["operations"][2]["result"]["block"] is True
    assert output["statusCalls"] == [{
        "key": STATUS_KEY,
        "text": f"ready:{NONCE}:{_bridge_hash()}",
    }]
    assert output["operations"][4]["result"] is None
    assert "result" in output["operations"][5]


def test_workspace_read_is_canonical_auto_permitted_once_and_digest_bound() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-read-") as raw:
        workspace = Path(raw)
        target = workspace / "docs/readme.txt"
        target.parent.mkdir()
        target.write_text("hello", encoding="utf-8")
        canonical = str(target.resolve())
        output = _run(
            workspace,
            operations=[
                _call("myagents_read", "read-1", {"path": "docs/readme.txt"}),
                _execute("myagents_read", "read-1", {"path": canonical}),
                _execute("myagents_read", "read-1", {"path": canonical}),
                _call("myagents_read", "read-2", {"path": canonical}),
                _execute(
                    "myagents_read", "read-2",
                    {"path": canonical, "limit": 1},
                ),
            ],
        )

    assert len(output["selectCalls"]) == 1  # attestation only
    assert output["operations"][0] == {
        "kind": "call", "result": None, "input": {"path": canonical},
    }
    assert "result" in output["operations"][1]
    assert "error" in output["operations"][2]
    assert "permit" in output["operations"][2]["error"]
    assert "error" in output["operations"][4]
    assert "digest" in output["operations"][4]["error"]
    assert len(output["delegateCalls"]) == 1


def test_external_reads_ask_with_nonce_bound_payload_and_exact_choice() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-external-") as raw:
        root = Path(raw)
        workspace = root / "workspace"
        workspace.mkdir()
        outside = root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        canonical = str(outside.resolve())
        output = _run(
            workspace,
            permission_choices=["$ALLOW", "allow_once:forged"],
            operations=[
                _call("myagents_read", "outside-1", {"path": canonical}),
                _execute("myagents_read", "outside-1", {"path": canonical}),
                _call("myagents_read", "outside-2", {"path": canonical}),
                _execute("myagents_read", "outside-2", {"path": canonical}),
            ],
        )

    assert len(output["selectCalls"]) == 3
    request = output["selectCalls"][1]
    payload = _decode_title(request["title"], PERMISSION_PREFIX)
    assert payload["version"] == VERSION
    assert payload["processNonce"] == NONCE
    assert payload["toolCallId"] == "outside-1"
    assert payload["toolName"] == "myagents_read"
    assert payload["input"] == {"path": canonical}
    assert payload["argsHash"] == _args_hash({"path": canonical})
    call_nonce = payload["callNonce"]
    assert request["options"] == [
        f"allow_once:{call_nonce}", f"reject_once:{call_nonce}",
    ]
    assert "result" in output["operations"][1]
    assert output["operations"][2]["result"]["block"] is True
    assert "error" in output["operations"][3]
    assert len(output["delegateCalls"]) == 1


def test_write_and_edit_ask_inside_but_hard_block_outside_git_and_hardlinks() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-mutation-") as raw:
        root = Path(raw)
        workspace = root / "workspace"
        workspace.mkdir()
        inside = workspace / "notes.txt"
        inside.write_text("old", encoding="utf-8")
        outside = root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        git_file = workspace / ".git/config"
        git_file.parent.mkdir()
        git_file.write_text("git", encoding="utf-8")
        hardlink_source = workspace / "hardlink-source.txt"
        hardlink_source.write_text("old", encoding="utf-8")
        hardlink = workspace / "hardlink.txt"
        os.link(hardlink_source, hardlink)
        output = _run(
            workspace,
            permission_choices=["$ALLOW", "$ALLOW"],
            operations=[
                _call(
                    "myagents_edit", "edit-1",
                    {"path": str(inside), "edits": [
                        {"oldText": "old", "newText": "new"},
                    ]},
                ),
                _execute(
                    "myagents_edit", "edit-1",
                    {"path": str(inside.resolve()), "edits": [
                        {"oldText": "old", "newText": "new"},
                    ]},
                ),
                _call(
                    "myagents_write", "write-1",
                    {"path": "new.txt", "content": "hello"},
                ),
                _call(
                    "myagents_write", "write-out",
                    {"path": str(outside), "content": "no"},
                ),
                _call(
                    "myagents_write", "write-git",
                    {"path": str(git_file), "content": "no"},
                ),
                _call(
                    "myagents_edit", "edit-hardlink",
                    {"path": str(hardlink), "edits": [
                        {"oldText": "old", "newText": "no"},
                    ]},
                ),
            ],
        )

    assert len(output["selectCalls"]) == 3  # attest + edit + write
    assert "result" in output["operations"][1]
    assert output["operations"][2]["result"] is None
    for index in (3, 4, 5):
        assert output["operations"][index]["result"]["block"] is True
    assert "outside" in output["operations"][3]["result"]["reason"]
    assert ".git" in output["operations"][4]["result"]["reason"]
    assert "hard link" in output["operations"][5]["result"]["reason"]


def test_bash_requires_exact_allow_in_write_profiles_and_readonly_denies() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-bash-") as raw:
        workspace = Path(raw)
        for profile in ("default", "workspace_write"):
            output = _run(
                workspace,
                profile=profile,
                permission_choices=["$ALLOW", "$REJECT"],
                operations=[
                    _call(
                        "myagents_bash", "bash-1", {"command": "pwd"},
                    ),
                    _execute(
                        "myagents_bash", "bash-1", {"command": "pwd"},
                    ),
                    _call(
                        "myagents_bash", "bash-2", {"command": "pwd"},
                    ),
                ],
            )
            assert "result" in output["operations"][1]
            assert output["operations"][2]["result"]["block"] is True
            assert len(output["delegateCalls"]) == 1

        readonly = _run(
            workspace,
            profile="read_only",
            operations=[
                _call("myagents_bash", "bash-ro", {"command": "pwd"}),
                _call(
                    "myagents_write", "write-ro",
                    {"path": "x", "content": "x"},
                ),
            ],
        )
    assert readonly["tools"] == ALL_TOOLS  # registered, not active
    assert len(readonly["selectCalls"]) == 1
    assert all(
        operation["result"]["block"] is True
        for operation in readonly["operations"]
    )
    assert all(
        "read_only" in operation["result"]["reason"]
        for operation in readonly["operations"]
    )


def test_large_unicode_write_uses_bounded_preview_but_binds_full_arguments() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-large-write-") as raw:
        workspace = Path(raw)
        canonical = str((workspace / "中文记录.txt").resolve())
        content = "本地模型输出：允许写入。\n" * 1_200
        full_input = {"path": canonical, "content": content}
        assert len(json.dumps(full_input, ensure_ascii=False)) > 8 * 1024
        output = _run(
            workspace,
            permission_choices=["$ALLOW"],
            operations=[
                _call("myagents_write", "large-write", full_input),
                _execute("myagents_write", "large-write", full_input),
            ],
        )

    request = output["selectCalls"][1]
    payload = _decode_title(request["title"], PERMISSION_PREFIX)
    assert payload["argsHash"] == _args_hash(full_input)
    preview = payload["input"]
    assert preview["path"] == canonical
    assert preview["content"] != content
    assert content.startswith(preview["content"])
    assert _stable_json_bytes(preview) <= 4096
    assert preview["_myagentsPreview"] == {
        "truncated": True,
        "fullBytes": len(json.dumps(
            full_input, ensure_ascii=False, separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")),
        "argsHash": _args_hash(full_input),
    }
    assert "result" in output["operations"][1]
    assert output["delegateCalls"] == [{
        "toolCallId": "large-write",
        "name": "write",
        "input": full_input,
        "cwd": str(workspace.resolve()),
    }]


def test_permission_input_enforces_the_exact_4096_byte_boundary() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-preview-boundary-") as raw:
        workspace = Path(raw)
        canonical = str((workspace / "boundary.txt").resolve())
        empty = {"path": canonical, "content": ""}
        exact_input = {
            "path": canonical,
            "content": "x" * (4096 - _stable_json_bytes(empty)),
        }
        over_input = {
            "path": canonical,
            "content": exact_input["content"] + "x",
        }
        assert _stable_json_bytes(exact_input) == 4096
        assert _stable_json_bytes(over_input) == 4097

        output = _run(
            workspace,
            permission_choices=["$ALLOW", "$ALLOW"],
            operations=[
                _call("myagents_write", "write-exact", exact_input),
                _execute("myagents_write", "write-exact", exact_input),
                _call("myagents_write", "write-over", over_input),
                _execute("myagents_write", "write-over", over_input),
            ],
        )

    exact_payload = _decode_title(
        output["selectCalls"][1]["title"], PERMISSION_PREFIX,
    )
    over_payload = _decode_title(
        output["selectCalls"][2]["title"], PERMISSION_PREFIX,
    )
    assert exact_payload["input"] == exact_input
    assert _stable_json_bytes(exact_payload["input"]) == 4096
    assert exact_payload["argsHash"] == _args_hash(exact_input)
    assert _stable_json_bytes(over_payload["input"]) == 4096
    assert over_payload["input"]["_myagentsPreview"]["truncated"] is True
    assert over_payload["argsHash"] == _args_hash(over_input)
    assert [call["input"] for call in output["delegateCalls"]] == [
        exact_input,
        over_input,
    ]


def test_large_escaped_write_content_is_truncated_by_stable_json_budget() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-escaped-write-") as raw:
        workspace = Path(raw)
        canonical = str((workspace / "escaped.txt").resolve())
        full_input = {"path": canonical, "content": "\x01" * 5_000}
        output = _run(
            workspace,
            permission_choices=["$ALLOW"],
            operations=[
                _call("myagents_write", "escaped-write", full_input),
                _execute("myagents_write", "escaped-write", full_input),
            ],
        )

    payload = _decode_title(output["selectCalls"][1]["title"], PERMISSION_PREFIX)
    assert _stable_json_bytes(payload["input"]) <= 4096
    assert payload["input"]["content"] != full_input["content"]
    assert full_input["content"].startswith(payload["input"]["content"])
    assert payload["argsHash"] == _args_hash(full_input)
    assert "result" in output["operations"][1]
    assert output["delegateCalls"][0]["input"] == full_input


def test_oversized_write_with_unknown_fields_is_blocked_before_permission() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-write-schema-") as raw:
        workspace = Path(raw)
        output = _run(
            workspace,
            permission_choices=["$ALLOW"],
            operations=[
                _call(
                    "myagents_write",
                    "write-unknown",
                    {
                        "path": "notes.txt",
                        "content": "visible",
                        "hiddenTail": "do-not-hide-this" * 400,
                    },
                ),
            ],
        )

    assert len(output["selectCalls"]) == 1
    assert output["operations"][0]["result"]["block"] is True
    assert "schema" in output["operations"][0]["result"]["reason"]
    assert output["delegateCalls"] == []


def test_edit_display_rejects_nested_unknown_or_malformed_schema() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-edit-schema-") as raw:
        workspace = Path(raw)
        target = workspace / "notes.txt"
        target.write_text("old", encoding="utf-8")
        output = _run(
            workspace,
            permission_choices=["$ALLOW", "$ALLOW"],
            operations=[
                _call(
                    "myagents_edit",
                    "edit-nested-unknown",
                    {
                        "path": str(target),
                        "edits": [{
                            "oldText": "old",
                            "newText": "new",
                            "hiddenTail": "do-not-hide-this" * 400,
                        }],
                    },
                ),
                _call(
                    "myagents_edit",
                    "edit-malformed",
                    {
                        "path": str(target),
                        "edits": [{"oldText": ["old"], "newText": "new"}],
                    },
                ),
            ],
        )

    assert len(output["selectCalls"]) == 1
    assert all(
        operation["result"]["block"] is True
        and "schema" in operation["result"]["reason"]
        for operation in output["operations"]
    )
    assert output["delegateCalls"] == []


def test_write_with_unbounded_canonical_path_is_blocked_before_permission() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-write-path-") as raw:
        workspace = Path(raw)
        output = _run(
            workspace,
            permission_choices=["$ALLOW"],
            operations=[
                _call(
                    "myagents_write",
                    "write-long-path",
                    {
                        "path": "/".join(["\x01" * 200] * 4),
                        "content": "visible",
                    },
                ),
            ],
        )

    assert len(output["selectCalls"]) == 1
    assert output["operations"][0]["result"]["block"] is True
    assert "4096 UTF-8 bytes" in output["operations"][0]["result"]["reason"]
    assert output["delegateCalls"] == []


def test_oversized_non_mutation_input_is_not_approved_with_a_hidden_tail() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-large-grep-") as raw:
        root = Path(raw)
        workspace = root / "workspace"
        outside = root / "outside"
        workspace.mkdir()
        outside.mkdir()
        output = _run(
            workspace,
            permission_choices=["$ALLOW"],
            operations=[
                _call(
                    "myagents_grep",
                    "large-grep",
                    {"path": str(outside), "pattern": "token-" * 1_000},
                ),
            ],
        )

    assert len(output["selectCalls"]) == 1
    assert output["operations"][0]["result"]["block"] is True
    assert "cannot be truncated safely" in output["operations"][0]["result"]["reason"]
    assert output["delegateCalls"] == []


def test_oversized_bash_is_blocked_instead_of_approving_a_hidden_tail() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-large-bash-") as raw:
        workspace = Path(raw)
        output = _run(
            workspace,
            permission_choices=["$ALLOW"],
            operations=[
                _call(
                    "myagents_bash", "large-bash",
                    {"command": "printf x;" * 1_000},
                ),
            ],
        )
    assert len(output["selectCalls"]) == 1
    assert output["operations"][0]["result"]["block"] is True
    assert "too large" in output["operations"][0]["result"]["reason"]
    assert output["delegateCalls"] == []


def test_paths_reject_ambiguous_forms_prefix_collision_and_symlink_escape() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-path-") as raw:
        root = Path(raw)
        workspace = root / "work"
        workspace.mkdir()
        sibling = root / "work-copy/secret.txt"
        sibling.parent.mkdir()
        sibling.write_text("secret", encoding="utf-8")
        link = workspace / "escape"
        link.symlink_to(sibling)
        bad_paths = [
            "bad\x00path", "~/secret", "file:///tmp/secret",
            "@attachment", str(sibling), str(link),
        ]
        operations = [
            _call(
                "myagents_write", f"bad-{index}",
                {"path": path, "content": "no"},
            )
            for index, path in enumerate(bad_paths)
        ]
        output = _run(workspace, operations=operations)

    assert len(output["selectCalls"]) == 1
    assert all(
        operation["result"]["block"] is True
        for operation in output["operations"]
    )
    reasons = "\n".join(
        operation["result"]["reason"]
        for operation in output["operations"]
    )
    assert "ambiguous" in reasons
    assert "outside" in reasons


def test_symlink_external_read_is_canonicalized_and_requires_permission() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-read-link-") as raw:
        root = Path(raw)
        workspace = root / "workspace"
        workspace.mkdir()
        outside = root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = workspace / "linked.txt"
        link.symlink_to(outside)
        output = _run(
            workspace,
            permission_choices=["$REJECT"],
            operations=[
                _call("myagents_read", "linked", {"path": str(link)}),
            ],
        )

    assert len(output["selectCalls"]) == 2
    assert output["operations"][0]["input"]["path"] == str(
        outside.resolve())
    assert output["operations"][0]["result"]["block"] is True


def test_permit_expires_before_delegate() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-expiry-") as raw:
        workspace = Path(raw)
        target = workspace / "readme.txt"
        target.write_text("hello", encoding="utf-8")
        canonical = str(target.resolve())
        output = _run(
            workspace,
            operations=[
                _call("myagents_read", "expire", {"path": canonical}),
                {"kind": "advance", "ms": 60_001},
                _execute("myagents_read", "expire", {"path": canonical}),
            ],
        )
    assert "expired" in output["operations"][2]["error"]
    assert output["delegateCalls"] == []


def test_permit_rechecks_path_fingerprint_before_delegate() -> None:
    with tempfile.TemporaryDirectory(prefix="myagents-pi-fingerprint-") as raw:
        workspace = Path(raw)
        target = workspace / "readme.txt"
        target.write_text("first", encoding="utf-8")
        canonical = str(target.resolve())
        output = _run(
            workspace,
            operations=[
                _call("myagents_read", "replace", {"path": canonical}),
                {
                    "kind": "replaceFile", "path": canonical,
                    "content": "second",
                },
                _execute("myagents_read", "replace", {"path": canonical}),
            ],
        )
    assert "changed" in output["operations"][2]["error"]
    assert output["delegateCalls"] == []


if __name__ == "__main__":
    test_registers_only_unique_wrappers_and_exact_attestation()
    test_attestation_is_fail_closed_on_bad_ack_hash_tools_source_or_ui()
    test_session_start_returns_before_deferred_attestation_and_current_ack_readies()
    test_old_generation_ack_cannot_ready_a_reloaded_session()
    test_workspace_read_is_canonical_auto_permitted_once_and_digest_bound()
    test_external_reads_ask_with_nonce_bound_payload_and_exact_choice()
    test_write_and_edit_ask_inside_but_hard_block_outside_git_and_hardlinks()
    test_bash_requires_exact_allow_in_write_profiles_and_readonly_denies()
    test_large_unicode_write_uses_bounded_preview_but_binds_full_arguments()
    test_permission_input_enforces_the_exact_4096_byte_boundary()
    test_large_escaped_write_content_is_truncated_by_stable_json_budget()
    test_oversized_write_with_unknown_fields_is_blocked_before_permission()
    test_edit_display_rejects_nested_unknown_or_malformed_schema()
    test_write_with_unbounded_canonical_path_is_blocked_before_permission()
    test_oversized_non_mutation_input_is_not_approved_with_a_hidden_tail()
    test_oversized_bash_is_blocked_instead_of_approving_a_hidden_tail()
    test_paths_reject_ambiguous_forms_prefix_collision_and_symlink_escape()
    test_symlink_external_read_is_canonicalized_and_requires_permission()
    test_permit_expires_before_delegate()
    test_permit_rechecks_path_fingerprint_before_delegate()
    print("\nPi permission bridge 契约测试全部通过")

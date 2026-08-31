# `@myagents/dsh-acp-host`

This directory is the canonical source of the DeepSeek Harness host used by
myagents. It owns the ACP wire state machine, product identity, model/tool
composition, durable load/close behavior and two runtime permission profiles.
The DSH checkout is an immutable runtime dependency used only through its
public agent, session, persistence, attachment, tool and approval exports.

Stock DSH's ACP package/demo does not expose the product identity and exact
`session/load` / `session/close` wire semantics required by the myagents
contract. The local `src/acp*.ts` compatibility bridge therefore derives from
DeepSeek's MIT ACP implementation and is maintained and tested here rather
than copied back into the DSH repository. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

This package is a standard DSH profile bundle. `package.json` declares
`dsh.bundle.patch`, `cordis.patch.yml` overlays the stock base bundle, and the
only installed runtime entry is built JavaScript at `lib/index.js`. The ACP SDK
is bundled into that artifact; DSH services remain exact peer dependencies and
are supplied by the official profile runtime. Source and packaged operation use
the same Loader plugin and patch contract—there is no product-specific CLI or
`tsx` executable.

- `workspace-write`: exact built-in `read`, `glob`, and `grep` calls proceed;
  every other tool requires one-shot ACP approval.
- `read-only`: only the captured built-in `read`, `glob`, and `grep`
  definitions remain visible and executable; same-name shadows are denied.

The wire identity remains `dsh-myagents-acp`. The bundle is installed with the
official command and booted through the ordinary profile launcher:

```bash
mkdir -p /tmp/myagents-dsh-package
MYAGENTS_DSH_SOURCE_ROOT=/absolute/path/to/deepseek-harness \
  bash scripts/package-dsh-plugin.sh /tmp/myagents-dsh-package
DSH_HOME=/absolute/product/profile-home \
  dsh plugin --profile myagents add \
  /tmp/myagents-dsh-package/myagents-dsh-acp-host-0.1.1.tgz --offline
DSH_HOME=/absolute/product/profile-home \
  DSH_ACP_PROFILE=read-only \
  dsh --profile myagents
```

Run the packaging command from the myagents repository root. It stages the
built package under `/tmp`, refuses to overwrite an existing tarball, and does
not write `lib/` into the canonical source directory.

The source host always publishes stateful `session/new`, `session/load`,
`session/close`, `session/prompt` and `session/cancel`. It refuses to publish
ACP until persistence and query services are present. Only append-origin user,
assistant and bounded tool lifecycle events cross the wire; internal prompts,
replacement projections and tool output do not. Permission responses bind to
the exact owned agent, call id, tool name, complete arguments and captured
metadata identity. Unknown responses fail closed. Only a completed DSH turn is
encoded as ACP `end_turn`; error and unknown terminals reject the prompt.

`DSH_HOME` is the official profile home and is intentionally separate from
product state. `DSH_ACP_PERSISTENCE_DIR` and every product path must be canonical
and absolute. The launcher fixes sessions, runtime home, attachment home, agents
home and private `config-inputs` below that state root. Existing user settings/credentials are
read through bounded no-follow handles and atomically copied to mode-0600
product files; the host never receives the original paths. It rejects symlink
escape and source/state/workspace/plugin/executable/config overlap before boot. `.env`
and Node loader injection variables are removed before spawn and rejected again
by the installed host.

The official profile path is part of the security boundary. `profiles/` and
`profiles/myagents/` must be real, canonical directories below `DSH_HOME`, not
symlinks, and the profile manifest is read through a bounded, single-link,
no-follow descriptor with before/after identity checks. Stock DSH applies
`$DSH_HOME/cordis.patch.yml` and
`$DSH_HOME/profiles/myagents/cordis.patch.yml` after bundle layers. Each of
those later-wins files must therefore be absent or be a canonical, single-link
file of at most 64 KiB whose only non-blank, non-comment line is exactly `[]`.
An empty/comments-only file or any effective patch fails closed. The launcher
revalidates this profile contract before spawn and before every reused turn.

`runtime-contract.json` records the tested release baseline: host and
compatibility revisions, DSH root package/version and source-commit provenance,
the official built CLI, ACP SDK, public package source and runtime-export hashes,
the platform build-tool identity, and the standard bundle entry/patch hashes. It
deliberately contains no whole-runtime-tree compatibility
fingerprint. Python readiness verifies the official CLI/root-package identity
and exact installed bundle hashes; it does not inspect Git or hash the complete
DSH checkout. Before executing a checkout build tool, the explicit
release/package gate binds the actual Git HEAD, built CLI, ACP SDK, public
source/runtime entries and esbuild binary to this contract. A separate
postflight binds the produced bundle; the gate's before/after
full-tree comparison also proves validation did not mutate the immutable
checkout. Persistence is
fixed to uncompressed, unpacked JSONL. Before any DSH history materialization,
`session/load` uses the public `list`/`locate` seam and a no-follow file scan to
enforce 4096 events / 16 MiB, then checks file identity again after
materialization and before resume.

Release-only bundle contracts (not part of the dependency-free default
myagents Harness) run from an installed DSH checkout:

```bash
MYAGENTS_DSH_SOURCE_ROOT=/absolute/path/to/deepseek-harness \
  bash scripts/check-dsh-plugin.sh
```

Run that command from the myagents repository root. The gate builds twice and
compares the artifacts, packs a tarball, installs it with official `dsh plugin`
into a temporary `DSH_HOME`, verifies `--dump-config`, and performs ACP
initialize/new/close handshakes against the installed profile. It does not call
a real model or touch the user's profile, and it fails if the stock DSH
checkout's HEAD, Git state, metadata or complete file-content hash tree changes.

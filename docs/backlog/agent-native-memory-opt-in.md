# Opt-in: agent-native memory (relax the F37 deny)

Status: **proposed** (2026-07-11) Date: 2026-07-11 Owner: Vladimir Makarevich

An exploratory, default-off config flag that lets an operator opt into the coding agent's **own built-in memory** instead of (or alongside) the orchestrator's memory subsystem. Today Claude Code's native "auto memory" is unconditionally suppressed by a deliberate security deny (F37); this item adds a knob to relax that deny for operators who want the vendor's cross-task learning and accept its cost. Scoped to the Claude provider in V1 (Codex's native memory is not managed today and its semantics are not determinable from this repo). This is the "agent-native" source in the broader memory-source picture — the audited counterpart is the orchestrator's own store ([memory/index.md](memory/index.md), refined in [memory-concepts-over-episodic-ledger.md](memory-concepts-over-episodic-ledger.md)).

## The problem

There is no operator control over the agent's native memory — it is hardcoded off for Claude and unmanaged for Codex. Concretely, `_native_memory_deny_tools()` (`providers/claude.py:172-190`) unconditionally appends `Write/Edit/Read(<config_dir>/**)` to `--disallowedTools`, so the spawned Claude agent cannot use its native auto-memory at `~/.claude/projects/<repo>/memory/`. That deny was a deliberate fix: the docstring records that native memory "escapes current.diff, the commit, the redaction net, and the orchestrator's own audit — an unredacted originSessionId was observed leaking". So the current posture is safe but rigid: an operator who would rather use the vendor's memory (vendor-maintained, improving, and a useful A/B against the orchestrator's own store whose lift is still unproven on a synthetic baseline) has no way to turn it on.

## Constraints

- **Core does not know the CLI syntax.** The flag is a generic config knob; only the Claude adapter translates it into the deny/allow decision. The core and the Codex adapter are unaffected.
- **No secrets in logs / store / artifacts.** This is the crux. F37 exists precisely because native memory writes to a store outside the orchestrator's redaction net, and a real leak (an unredacted `originSessionId`) was observed there. Relaxing it is therefore a deliberate, operator-owned risk acceptance — the default must stay off (deny in place), and turning it on must be a conscious act.
- **Launch without shell interpolation** — unaffected (argv list, `shell=False`).
- **Auditability.** The orchestrator's implicit contract "a run is explainable from its artifacts" weakens when native memory is on (a hidden, unaudited input joins the run). Accepted and documented as the cost of the opt-in.
- **Cross-platform.** The deny already resolves the config dir cross-platform (`CLAUDE_CONFIG_DIR` else `~/.claude`); the gate adds no new path logic.

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| **Do nothing (keep F37)** | Zero operator flexibility. Retained as the default-off behavior, but rejected as the whole answer. |
| **Hardened: relocate the store under `.worc/`** — when on, set `CLAUDE_CONFIG_DIR` to a per-repo path under `.worc/agent-memory/` (already commit-excluded, inspectable/clearable by the orchestrator) | Deferred, not chosen for V1. It buys inspectability/clearability but requires extending the env seam to _set_ values (`build_child_env` is forward-only today, `security/env.py:90-102`). The harms it defends against — commit contamination and cross-repo bleed — do not apply to Claude's auto-memory anyway (see Decision), so the extra machinery isn't justified yet. Recorded as the hardening path if the naive version proves insufficient. |
| **Symmetric Claude + Codex** | Rejected for V1: Codex's native memory feature (paths, whether it reads `AGENTS.md`/`~/.codex`, how to toggle) is not determinable from this repo, and Codex is unmanaged today. Designing a symmetric relocation blind would be speculative. Codex is a separate open question. |
| **Deliver knowledge by prompt-path (the existing skills "Model A")** | Different mechanism: an orchestrator-controlled, read-only reference surfaced in the prompt footer (`providers/_adapter_base.py:101-104`). It does not give the agent its own persistent cross-task learning, so it doesn't satisfy the goal. |

## Decision

Add an opt-in, **default-off** config flag that, for the Claude provider only, gates the F37 native-memory deny. Off (default): behavior is unchanged — `<config_dir>/**` Write/Edit/Read stays denied. On: the deny is dropped and Claude's native auto-memory persists to `~/.claude/projects/<repo>/memory/` across tasks on that repo.

We take the **naive gate** (flip the deny) rather than the hardened relocation because that store is **repo-keyed and never committed** — so commit contamination and cross-repo bleed simply do not apply to it — and the shared-`HOME` scoping resolves to an effectively per-repo directory. The accepted residual cost is a store that is **unaudited and has no redaction guarantee** (the F37 leak precedent), which is acceptable only behind an explicit, default-off operator opt-in. The cost of the alternatives: hardened relocation adds env-seam machinery for harms that don't apply here; do-nothing keeps zero flexibility.

This composes into a clean memory-source choice for the operator: disable the orchestrator's own memory (`memory.enabled: false`) and enable native memory, run both, or neither.

## Open questions

1. **Flag placement**: `security.*` (it relaxes a security control, so putting the risk-acceptance in the security block is honest and discoverable) versus `agents.*` / provider config? Leaning `security.*`.
2. **Guard against casual enablement**: given the observed leak, should turning it on require more than a bare `true` — e.g. a one-time explicit log warning, or a sub-key that names the accepted risk — so it is not flipped without intent?
3. **Both-on semantics**: if the orchestrator's memory and native memory are both enabled, the agent receives two memory channels (the deterministic injected packet + the opaque native store). Confirm this is merely redundant/advisory and document it; decide whether to warn.
4. **Codex**: what native memory does Codex actually have, and does it need a symmetric deny (it is unmanaged today)? Separate investigation before any Codex knob.

## Implementation notes

- **Gate the deny** (`providers/claude.py:286-290`): only append `_native_memory_deny_tools()` when the flag is off. The function already resolves `CLAUDE_CONFIG_DIR`/`~/.claude` (`claude.py:187-189`); no path logic changes.
- **Thread the flag**: `build_claude_argv` signature and its caller `ClaudeCodeProvider._build_argv` (`claude.py:408-416`); add the knob to the config schema (`config/schema.py`, `SecurityConfig` or `ProviderConfig`) with a version bump, the loader, `packaged/config.example.yaml`, and the operator guide. State the accepted risk in the config comment.
- **No env-seam change** for the naive version: `build_child_env` (`security/env.py:90-102`) stays forward-only. (The hardened alternative is the only thing that would need it to _set_ `CLAUDE_CONFIG_DIR`.)
- **Codex adapter unchanged** — there is no deny to gate today.
- **Docs to sync**: `config.example.yaml`, the guide quickstart, and a cross-link from the memory hub ([memory/index.md](memory/index.md)) framing orchestrator-memory vs agent-native memory as the two selectable sources.

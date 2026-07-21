# Backlog: agent read-isolation from the `.worc` runtime home

Status: **open — design accepted, implementation plan pending** Date: 2026-07-21 Owner: Vladimir Makarevich

Decision record, not a spec. It captures why a launched coding agent can currently read the orchestrator's entire `.worc/` runtime home, why "just deny `.worc/`" breaks the way results flow between nodes, and the two-root model + phased enforcement chosen to fix it. Parked here for traceability; the implementation plan (task files) will land in this same folder and be linked from the task table below. Nothing here overrides the hard invariants in [.agents/rules/](../../../.agents/rules/), [../../../CLAUDE.md](../../../CLAUDE.md), or [../../../AGENTS.md](../../../AGENTS.md).

A concrete before/after walkthrough of one task run — the artifact trees, the paths handed to the agent, the provider `argv`, and mermaid diagrams — is in [happy-path.md](happy-path.md).

## The problem

The orchestrator writes its whole "kitchen" into `<repo>/.worc/` on the target repository, and the coding agent it launches can read all of it. Three code facts make this the shape of the problem:

1. **The agent runs in the repo root, and `.worc/` is a subdirectory of that root.** The working directory is `repo.local_path` ([agent.py:526](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L526), [evaluator.py:278](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L278)); the runtime home is `<repo>/.worc/` ([cli.py:1088](../../../src/wastech_orchestrator/cli.py#L1088)). `.worc/` is only kept out of git — nothing removes it from the agent's field of view.
2. **Intermediate results reach the agent as file paths that it reads from disk** — this is the sanctioned inter-node channel. Node outputs are written under `.worc/logs/<task-id>/` and injected into the prompt only as paths (`{plan_path}`, `{diff_path}`, `{review_path}`, `{<node>_path}` plus the "Context files" footer, [base.py:178-206](../../../src/wastech_orchestrator/providers/base.py#L178-L206)). Inlining the content into the prompt is **forbidden by invariant** ([prompts.py:4-11](../../../src/wastech_orchestrator/core/prompts.py#L4-L11), [architecture.md:30](../../../.agents/rules/architecture.md#L30)). So reading from `.worc/` is not incidental — it is how results are handed forward.
3. **What the agent needs and what must stay hidden are interleaved in the same subtree.** Inside `.worc/logs/<task-id>/stages/<node>/run-*/`, the agent-facing `<node>.out.md` / `findings.json` sit next to `rendered-prompt.md` and the `<attempt>-<provider>/` raw model I/O. A blanket deny of `.worc/**` would also cut the agent off from its own context; a naive carve-out of the task-log subtree would still expose the rendered prompts and raw provider streams.

## What today's model assumes

- **One in-place working tree.** There is no per-task worktree or clone; the agent edits `repo.local_path` directly, and `.worc/` lives inside it (see the separate worktree decision record, [concurrent-task-worktrees.md](../archive/concurrent-task-worktrees.md)).
- **Context is delivered by files, referenced by path** ([architecture.md:30](../../../.agents/rules/architecture.md#L30)) — never inlined.
- **Read restriction is asymmetric between providers.** Claude already supports per-path read denial (`security.denied_read_paths` → `--disallowedTools Read(<glob>)`, [claude.py:180-192](../../../src/wastech_orchestrator/providers/claude.py#L180-L192); default `.env`, `secrets/**`). Codex has **no** per-path read deny — "the sandbox is the isolation" — and its OS sandbox restricts writes/network, not reads. Projecting deny-reads into the Codex runtime is an open P0, [CODX-003](../codex-provider-improvements/p0-codx-003-enforce-deny-policy.md). There is no Codex sandbox on Windows at all.

## What lives under `.worc/` (hide vs keep readable)

| Keep readable — current task's results the agent is pointed at | Hide — agent never needs it, lives in `.worc/` today |
| --- | --- |
| `plan.md` (`{plan_path}`), the diff file (`{diff_path}`), checks report (`{checks_path}`) | `.worc/.env` (real tokens, [cli.py:132-149](../../../src/wastech_orchestrator/cli.py#L132-L149)) |
| review `findings.json` (`{review_path}`), generic `<node>.out.md` (`{<node>_path}`) | `state.db` (state + raw session ids + unredacted findings) |
| subtask spec (`{subtask_spec_path}`), handoff brief (`{predecessor_context}`), node memory packet (`{memory_path}`) | `flows/**/roles/*.md` (system prompts, own and other nodes') |
| (outside `.worc/`) the task file `tasks/<id>.md` (`{task_path}`) — unchanged | `prompt-audit/`, `stages/**/rendered-prompt.md`; `stages/**/<attempt>-<provider>/` raw streams; `memory/`; `security-reports/`; **other tasks'** logs |

## Constraints the design must respect

- **`denied_read_paths` must not be reused for `.worc/**`.** It is `REPLACES`-not-extends (`reference.md:8`) and overloaded with two other jobs: the redaction pass globs its patterns and reads matched files as "secrets" ([redaction.py:175](../../../src/wastech_orchestrator/providers/redaction.py#L175)), and the skill scanner skips files matching it ([skills.py:158-169](../../../src/wastech_orchestrator/core/skills.py#L158-L169)). Pointing it at `.worc/**` would make the orchestrator read `state.db` and logs as secrets.
- **Non-weakening.** The deny cannot be relaxable by a task, `extra_args`, or a flow node ([architecture.md:38](../../../.agents/rules/architecture.md#L38), [security.md](../../../.agents/rules/security.md)); the config validator must reject unsafe settings.
- **No hardcoding of node ids / topics** ([architecture.md:7-8](../../../.agents/rules/architecture.md#L7-L8)). The split must be layout-level and generic (keyed by task-id/node-id), not per-node branching.
- **Cross-platform.** Windows/Linux/macOS parity is mandatory; any OS-sandbox enforcement must have a defined Windows story (fail-closed under `strict_isolation`).
- **`.env` is already contained** — child env is rebuilt from an allowlist ([env.py:90-102](../../../src/wastech_orchestrator/security/env.py#L90-L102)), so the incremental exposure this ADR closes is chiefly state/audit/prompts/other-tasks, not env secrets.
- **Greenfield** — the orchestrator is not deployed anywhere, so no migration/back-compat machinery is warranted.

## Alternatives considered

| Option | Verdict |
| --- | --- |
| Do nothing — agent keeps reading all of `.worc/` | Rejected. The named problem stands: agent can read secrets/state/prompts/other tasks. |
| Inline intermediate results into the prompt so the agent needs no `.worc/` read | Rejected. Directly violates the path-only invariant ([prompts.py:4-11](../../../src/wastech_orchestrator/core/prompts.py#L4-L11)); would push large/redacted content into the request boundary. |
| Reuse `security.denied_read_paths` for `.worc/**` | Rejected. Overloaded + `REPLACES` (see Constraints) — would make the orchestrator read the runtime home "as secrets" and drift as new subdirs appear. |
| Point-deny inside `.worc/` by enumerating sensitive globs (Claude `Read()` only) | Partial/fragile. Claude-only, list drifts with new subpaths, and does not move toward hard isolation. |
| **Two independent roots (curated exchange vs private home) + phased enforcement** | **Chosen.** Removes the interleaving structurally, keeps the path-only channel, and makes the Phase-2 relocation a path change rather than a rewrite. |

## Decision

**1. Split `.worc/` into two independent roots (accepted read surface = "curated exchange").**

- **`private_home`** — everything the agent never needs: `.env`, `state.db`, `flows/`, `memory/`, `security-reports/`, `prompt-audit/`, `rendered-prompt.md`, `<attempt>-<provider>/` raw streams, and other tasks' logs. Today `<repo>/.worc/`; in Phase 2 relocatable **outside** the agent's working tree.
- **`exchange_root`** — only the **current** task's agent-facing results (`plan.md`, diff, checks report, review `findings.json`, `<node>.out.md`, subtask spec, handoff brief, memory packet). In-repo, gitignored, always reachable by the agent. It must be a **separate root, not nested under `.worc/`**, so relocating `private_home` in Phase 2 leaves the exchange in place and does not touch the agent's read surface. The exchange carries the already-redacted copies (`write_node_output` runs `redact_text`), so it is safe to expose.

**2. Enforce the boundary in two phases (accepted threat model = "both, phased").**

- **Phase 1 — hygiene (now).** Land the two-root refactor; add a **dedicated** Claude read-deny of `private_home` in `--disallowedTools` (not via `denied_read_paths`); for Codex, stop handing `.worc/` paths and add a hygiene line to role prompts. Honest residual limits: Claude's `Read()` deny does not stop `Bash(cat …)`, and the Codex measure is obscurity, not enforcement.
- **Phase 2 — hard isolation (later).** Relocate `private_home` outside the agent's working tree (so Codex's broad reads and Claude's `Bash` can no longer reach it), plus a generated Codex OS-sandbox read-deny profile (macOS Seatbelt / Linux Landlock), fail-closed on Windows under `strict_isolation`. This is the [CODX-003](../codex-provider-improvements/p0-codx-003-enforce-deny-policy.md) mechanism applied to the runtime home; the Claude deny then becomes belt-over-suspenders. Phase-2 relocation shares open questions with the worktree decision record ([concurrent-task-worktrees.md](../archive/concurrent-task-worktrees.md), "Runtime-home placement").

## Consequences

- The path-only prompt invariant and the "context by files" model are preserved — only the destination of the agent-facing artifacts changes (exchange instead of interleaved task logs).
- The audit trail (rendered prompts, prompt-audit, raw provider streams) is unchanged in location and completeness; it simply stops being agent-readable.
- Phase 1 gives Claude a real (if `Bash`-leaky) deny and Codex a best-effort surface; the guarantee becomes provider- and platform-uniform only after Phase 2.
- New surfaces to keep out of commits: the exchange root must be gitignored and excluded from scoped staging alongside `.worc/` ([git_manager.py](../../../src/wastech_orchestrator/git_manager.py) `RUNTIME_EXCLUDED_DIRS`).

## Open questions

- **Exchange placement/name.** Recommended default: a separate in-repo gitignored root, e.g. `<repo>/.worc-io/<task-id>/`. Alternative `.worc/exchange/` now with a split at relocation is dirtier for Phase 2.
- **Config toggle.** Recommended: none — always-on, non-weakenable behavior (greenfield; matches the non-weakening invariant). Confirm before adding any operator escape.
- **Verify redaction of the on-disk `findings.json` and the `{memory_path}` packet** before they land in the agent-readable exchange (they are already agent-readable today, so no net-new exposure is expected — confirm).
- **Phase-2 `private_home` topology** (out-of-tree location, and its interaction with `state.db` placement) overlaps the worktree decision record's runtime-home open question; resolve once.
- **`_WORC_HOME` literal is duplicated** ([orchestrator.py:180](../../../src/wastech_orchestrator/core/orchestrator.py#L180) vs `cli.py`, tracked in [follow_ups.md](../follow_ups.md)). Centralizing it into one source is a natural prerequisite for the Phase-2 relocation.

## Implementation notes

Pointers for whoever picks this up — not a spec. The touch points for Phase 1 (Layer 0 refactor + Claude deny):

| Area | Change |
| --- | --- |
| [artifacts.py](../../../src/wastech_orchestrator/providers/artifacts.py) | Introduce `exchange_root` beside the private `artifacts_root`; route agent-facing `task_artifact_dir`/`node_run_dir` outputs to the exchange; `latest_run_file` resolves fan-in from it. |
| [postprocess.py](../../../src/wastech_orchestrator/core/flow/postprocess.py) | `apply_output_artifact` + `write_node_output` write to the exchange. |
| [evaluator.py](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) | `findings.json` (`{review_path}`) to the exchange. |
| [agent.py](../../../src/wastech_orchestrator/core/flow/nodes/agent.py) | `git.write_current_diff` (`{diff_path}`), `_node_output_paths`, `{memory_path}` packet to the exchange. |
| [context_paths.py](../../../src/wastech_orchestrator/core/flow/context_paths.py) | Path variables point at the exchange. |
| [observability.py](../../../src/wastech_orchestrator/core/flow/observability.py) | `rendered-prompt.md` / `prompt-audit/` **stay** in `private_home` (unchanged). |
| [claude.py](../../../src/wastech_orchestrator/providers/claude.py) | Dedicated internal `Read(<private_home>/**)` deny in `--disallowedTools` — not through `denied_read_paths`. |
| [codex.py](../../../src/wastech_orchestrator/providers/codex.py) | Phase 1: nothing enforceable (obscurity + role-prompt). Phase 2: generated OS-sandbox read-deny profile (aligns with CODX-003). |
| [git_manager.py](../../../src/wastech_orchestrator/git_manager.py) | Add the exchange root to gitignore / `.git/info/exclude` and scoped-staging exclusions. |
| [composition.py](../../../src/wastech_orchestrator/composition.py) / [cli.py](../../../src/wastech_orchestrator/cli.py) | Wire `exchange_root`; centralize the `.worc` home literal (prep for Phase-2 relocation). |
| Docs | New rule in [security.md](../../../.agents/rules/security.md), invariant note in [architecture.md](../../../.agents/rules/architecture.md), `docs/configuration.md`/`operations.md`, and the shipped guide `packaged/guide/flows/prompt-variables.md` (paths now resolve to the exchange, not `.worc/logs`). |

## Implementation plan

This cluster is organized by **phase**, not by the `codex-provider-improvements` P0/P1/P2 severity scheme — this is a new capability delivered in two phases (see Decision), not a set of fixes for already-violated invariants. Task files are named `wri-NNN-*.md`.

| Phase | ID | Task | Depends on |
| --- | --- | --- | --- |
| 1 | WRI-001 | [Two-root artifact layout: curated exchange vs private home](wri-001-two-root-exchange-layout.md) | — |
| 1 | WRI-002 | [Enforce a Claude read-deny of the private home](wri-002-claude-private-home-read-deny.md) | WRI-001 |
| 1 | WRI-003 | [Codex Phase-1 hygiene (obscurity) with honest limits](wri-003-codex-phase1-hygiene.md) | WRI-001 |
| 2 | WRI-004 | [Centralize the `.worc` home literal into one injectable seam](wri-004-centralize-worc-home-seam.md) | WRI-001 |
| 2 | WRI-005 | [Relocate the private home outside the agent's working tree](wri-005-relocate-private-home-out-of-tree.md) | WRI-004 |
| 2 | WRI-006 | [Generated Codex OS-sandbox read-deny of the private home](wri-006-codex-os-sandbox-read-deny.md) | WRI-005; CODX-001/002/003 |

### Milestones

**Milestone 1 — Phase 1 (hygiene).** WRI-001, then WRI-002 and WRI-003 in parallel. Exit: agent-facing results live in a curated in-repo exchange; Claude cannot `Read` the private home; Codex is handed only exchange paths (obscurity, documented as such); the audit trail is unchanged. Residuals documented honestly (Claude `Bash`; Codex obscurity).

**Milestone 2 — Phase 2 (hard isolation).** WRI-004 → WRI-005 → WRI-006. Exit: the private home is out of the agent's working tree and OS-sandbox-denied for Codex (fail-closed on Windows under `strict_isolation`); the deny is a real cross-provider guarantee, with the Claude `Read`-deny as defense in depth.

### Delivery rules

As for the sibling cluster ([../codex-provider-improvements/README.md](../codex-provider-improvements/README.md)): preserve the hard invariants; add/update behavior tests in the same change; update configuration/operations/architecture docs when the public contract changes (`/sync-docs`); run the full Definition of Done gates; mark a task completed only when every acceptance criterion is demonstrated; and never mark obscurity or a warning as completion of an enforcement outcome.

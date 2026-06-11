# Phase 6 — Security and observability

**Goal:** harden the cross-cutting security guarantees with adversarial tests, complete the audit
trail and observability surface, and ship the operations documentation. This phase adds little new
behaviour — it **proves** the invariants that earlier phases implemented and fills the final §16
gaps. It is the gate to the project's Definition of Done.

**Spec:** §8.1 (failure report), §10 (artifacts/audit), §12 (security model), §16 (final DoD), §19.5
(injection defence). **Rules:** [security.md](../rules/security.md), [architecture.md](../rules/architecture.md).

**Prerequisites:** Phases 1–5 — a working end-to-end pipeline. This phase audits and stress-tests it.

---

## Logical blocks

### 6.1 Security enforcement audit (§12)
Verify every enforcement point is wired and prove it can't be bypassed:
- **Env allowlist (§12.3):** only `security.allowed_environment` keys reach any child process
  (providers and the Check Runner). No token/secret forwarded implicitly.
- **No shell interpolation (§12.5):** every external call is an argv list; no user string is ever
  spliced into a command, env, command path, or working path.
- **Denied reads/commands (§12, security.md §11):** `denied_read_paths` (`.env`, `secrets/**`) are
  excluded from agent reads and from logging; `denied_commands` (`git commit`/`git push`/
  `gh pr create`) target the agent process — only the Git Manager publishes.
- **`strict_isolation` (§12.8):** when true, inability to enable the required isolation **fails
  preflight** with an error (no silent downgrade).
- **No direct push to `base_branch`; PR is mandatory** (§12.12, §12.11).

### 6.2 Injection defence (`security/injection.py`, §19.5)
- The frontmatter argv-shaped-token scanner used by the §19 gate (P5): reject values beginning with
  `-`/`--`, or containing newlines, `;`, backticks, `$(`, `|`, or a path separator where a non-path
  field is expected → `injection_suspected`. The **body** is never rejected for shell-like content.
- Restate and test the **structural guarantee**: task content reaches providers **only as file
  paths** in `AgentRunRequest` — no task field becomes a CLI flag, env var, command path, working
  path, or security setting. Normalization is **reject-don't-sanitize**: a value that changes under
  normalization (beyond documented slug folding) is rejected.

### 6.3 "Policy can't be weakened" — adversarial tests
Prove the central invariant from two angles:
- A **task** (front matter / override) cannot relax the sandbox/permissions, change a provider
  command/`extra_args`, or alter any security setting (rejected at the P1 validator / P4 router).
- **`extra_args`** cannot disable the sandbox/approvals for either provider (Codex
  `--dangerously-bypass-…` / `--sandbox danger-full-access`; Claude `--dangerously-skip-permissions`)
  — rejected by config validation **and** by each adapter's builder.
- A conditional `authorization_failed`/`permission_denied` fallback only ever moves to a **same-or-
  stricter** profile (P4) — never a looser one.

### 6.4 Redaction & no-secrets-anywhere (§12.6)
End-to-end assertions that **no secret lands in any sink**: the redacted `request.json`, `stdout`/
`stderr` logs, `events.jsonl`, SQLite rows, the ledger, and the failure report. Seed a fake secret
into the environment/inputs and assert it never appears in any written artifact or DB column.

### 6.5 Audit completeness & artifact registration (§9, §10)
- Every run records both `stage_runs` and `provider_attempts` (primary **and** any fallback) with
  status, error class, timestamps, exit code, and before/after commit SHAs; both runs of a
  fallback remain in the audit (§7.4).
- Artifacts are registered in SQLite **with a checksum** (§10); logs are append-only / never
  overwritten; the commit/push/PR fingerprints are persisted (idempotency, §13).
- `failure_report.json` + `stuck.md` are complete per §8.1/§10 (exhausted loop + limit, all counter
  values, last failing check output, last blocking findings, final diff; decomposition fields when
  applicable).

### 6.6 Observability (`logging`, coding-style.md)
- Structured logging keyed by `task_id`, `stage`, `attempt`, `provider` across the pipeline; **never**
  logs secrets/tokens/full env. A clear operator-facing trace of route source, fallback decisions,
  skip/decompose decisions, loop counters, and terminal outcome.

### 6.7 Operations documentation (§16 DoD)
Write the operator guide (e.g. `docs/operations.md`, linked from the README): installation,
**preflight** for both CLIs, authorization setup (git + each agent, configured **outside** the
orchestrator), the footprint modes and when to use each, diagnostics (reading artifacts, the ledger,
`failure_report.json`/`stuck.md`), and the `manual_action_required` recovery playbook. This is an
explicit §16 completion criterion.

---

## Tests

Largely **adversarial / negative**, complementing earlier phases:
- env allowlist leak attempts; no-shell-interpolation across all call sites; denied read/command
  enforcement; `strict_isolation` preflight failure.
- injection scan: each argv-shaped-token reason; the file-path-only structural guarantee; reject-
  don't-sanitize normalization.
- policy-weakening attempts via task and `extra_args` for both providers — all rejected; the
  same-or-stricter fallback constraint.
- a seeded secret never appears in any artifact / log / SQLite row / ledger / failure report.
- audit completeness: primary+fallback attempts both recorded; artifacts carry checksums;
  fingerprints persisted; failure report content complete.

## Definition of Done

- [ ] Every §12 enforcement point is verified by a test; isolation/permissions cannot be weakened by
      a task or `extra_args` for either provider.
- [ ] The injection scanner + the file-path-only structural guarantee are implemented and tested
      (§19.5); normalization rejects rather than sanitizes.
- [ ] No secret appears in any artifact, log, SQLite row, ledger entry, or failure report (proven by
      a seeded-secret test).
- [ ] Audit is complete: primary+fallback attempts recorded, artifacts checksummed, publish
      fingerprints persisted; `failure_report.json`/`stuck.md` complete per §8.1/§10.
- [ ] Structured, secret-free logging keyed by task/stage/attempt/provider.
- [ ] Operations documentation covers install / preflight / authorization / diagnostics for both
      CLIs (§16).
- [ ] Unit + integration + e2e suites all pass; `/run-checks` green.

## Final project Definition of Done (spec §16)

Closing this phase means the whole project DoD holds:
- [ ] Codex and Claude Code are reachable **only** through the common `AgentProvider`.
- [ ] The default routes work (Claude for refinement/planning/implementation/fixing/summary; Codex
      for review); task overrides are bounded by the allowlist.
- [ ] Infrastructure fallback works and is fully audited; quality failures never switch providers.
- [ ] The state machine recovers after a controlled restart; refinement enriches incomplete tasks
      and is skipped for complete ones; one task at a time; the ledger records every terminal task.
- [ ] Flag-gated decomposition is off by default, accepts a split only under the deterministic rule,
      runs sequentially on one branch into one PR, and the global fix budget bounds the whole task.
- [ ] The final `summary` is a what/how/integration/why handoff that becomes the PR body and never
      blocks a reviewed change.
- [ ] Commit/push/PR are done **only** by the orchestrator and never duplicated; the security policy
      can't be relaxed via a task or `extra_args`; unit/integration/e2e pass; the ops docs exist.

## Not in this phase (v2, spec §18.2)

Human-in-the-loop (clarifying questions / action approval), Telegram integration,
reasoning/complexity levels per task, richer task parsing, and parallel/graph decomposition with
worktrees.

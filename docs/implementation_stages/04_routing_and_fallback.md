# Phase 4 — Routing and fallback

**Goal:** build the **Agent Router** — the layer that, for each stage, resolves the primary/fallback provider from config + task override, enforces the allowlist, and performs **infrastructure-only** fallback within a stage. This is where the "fallback is infra-only" invariant is made real.

**Spec:** §4.2 (router), §5 (routing + overrides), §7.2 (fallback-eligible), §7.3 (no-fallback cases), §7.4 (partial changes), §8.1 (`stage_attempts`). **Rules:** [architecture.md](../rules/architecture.md).

**Prerequisites:** Phases 1–3 — config + both adapters. The Router calls **only** the `AgentProvider` contract; it must not know any CLI syntax (architecture.md dependency direction: `core → router → provider(interface)`).

---

## Logical blocks

### 4.1 Route resolution (`routing/router.py`)

- For a given `Stage`, resolve `(primary, fallback)` from `agents.routing`, then apply a **task override** (`agents.<stage>` front-matter) if present.
- Override is accepted **only** when (§5): the stage is known, the provider is in `agents.allowed`, and the override changes **neither** the provider command, `extra_args`, **nor** any security setting (reuse the P1 helper). Resolution happens **after** task validation and **before** the branch is created; once a stage has started its route is **never** changed retroactively (architecture.md).
- Record the **route source** (global config vs. task override) for the audit (persisted in P5).
- Default routes (§5): refinement/planning/implementation/fixing/summary → primary `claude`, fallback `codex`; review → primary `codex`, fallback `claude`.

### 4.2 Provider availability + selection

- Honor the `agents.allowed` allowlist; a primary/fallback not in the allowlist is a configuration error (already rejected in P1, re-checked defensively here).
- Hold the constructed `AgentProvider` instances (from P2/P3) and hand the Router the right one per resolved id. The Router does not build providers' commands — it selects and invokes them.

### 4.3 Fallback policy (`routing/router.py`) — the core of this phase

- On a stage run, call the primary's `run()`. On `ProviderError`:
  - **Fallback iff** the error class is in `FALLBACK_ELIGIBLE` (§7.2): `binary_not_found`, `unsupported_version`, `authentication_failed`, `rate_limited`, `network_unavailable`, `provider_unavailable`, `timeout`, `process_crashed`, `invalid_output`.
  - **Conditional fallback** for `authorization_failed`/`permission_denied`: allowed **only** when the denial is provider-specific **and** the fallback runs in the same or a **stricter** permission profile (§7.2). Never relax the policy to make fallback possible.
- **No fallback** (§7.3) for: failed tests/linters, review findings, incomplete fulfilment despite a clean CLI exit (`task_failure`), Git errors, invalid task/config, exhausted fix cycles, security violations. The Router returns these to the Core to route to `fixing`/`failed`/`manual_action_required` — it does **not** retry them on another provider.
- A run that returns `AgentRunResult(status=failed)` for a **quality** reason is **not** a fallback trigger — only a raised infra `ProviderError` is.

### 4.4 `stage_attempts` counter (§8.1)

- The Router owns the per-stage attempt count, **including** provider fallback within that stage, bounded by `agents.max_stage_attempts`. This counter is independent of the fix loops (those are P5). Exhausting `max_stage_attempts` without success surfaces a terminal-for-this-stage failure to the Core.
- The counter value is returned for persistence (the State Store is P5); the Router stays stateless beyond what it returns.

### 4.5 Partial-change snapshot hooks (§7.4)

- Define the snapshot contract the Core will use around a run: **before** — current commit SHA, `git status --porcelain`, a diff checksum, the existing-artifact list; **after** an infra failure that already changed files — a post-attempt snapshot + diff, and the fallback receives the **current diff** plus a "partial attempt" note in its prompt/context. Changes are **never** rolled back automatically.
- The actual git/snapshot execution is the Git Manager + Core (P5); here we define the data the Router/Core exchange so P5 wires it without reshaping the Router.

---

## Tests

**Unit:**

- Route resolution: defaults per stage; a valid task override is applied; an override naming an unknown stage / a non-allowlisted provider / a security-or-command change is rejected.
- Fallback decision table: each `ErrorClass` → fallback or not; the conditional auth/permission rule (same-or-stricter profile only); a quality `status=failed` does **not** trigger fallback.
- `stage_attempts`: increments across fallback within a stage and stops at `max_stage_attempts`.

**Integration (fake CLIs, building on P2/P3 harness):**

- a successful fallback (primary infra-fails, fallback succeeds);
- an **infrastructure error after files were changed** → the fallback receives the current diff;
- fallback **denied** on a quality failure (the failure is surfaced, not retried elsewhere).

## Definition of Done

- [ ] Router resolves primary/fallback per stage from config + a validated task override, records the route source, and never changes a started stage's route.
- [ ] Fallback fires **only** for `FALLBACK_ELIGIBLE` classes (plus the conditional, same-or-stricter auth/permission case) and **never** for quality failures or any §7.3 case.
- [ ] `stage_attempts` is counted (incl. fallback) and bounded by `max_stage_attempts`.
- [ ] The partial-change snapshot contract (§7.4) is defined and unit-covered; no automatic rollback.
- [ ] Router depends only on the `AgentProvider` interface — no CLI syntax, no state-machine changes.
- [ ] `/run-checks` green, including the three required integration scenarios.

## Not in this phase

- The state machine, fix loops, the global `fix_iterations` budget, and the stuck condition (P5/§8.1).
- Actually executing git snapshots/diffs (P5 Git Manager) — only the contract is defined here.
- Deciding when a stage even runs (the pipeline) — P5.

# Phase 3 — Claude Code adapter

**Goal:** add the second concrete provider — **ClaudeCodeProvider** — on top of the Phase 2
infrastructure, reaching full parity with Codex so the two are interchangeable behind the
`AgentProvider` contract. Consolidate the fake-CLI integration harness both adapters share.

**Spec:** §4.4 (adapters), §10 (artifacts), §12 (security). **Rules:** [security.md](../rules/security.md).

**Prerequisites:** Phase 2 — `process.py`, `env.py`, `redaction.py`, `artifacts.py`, `errors.py`,
and the `CodexProvider` pattern. This phase reuses all of them; it must add **no** new
provider-agnostic infrastructure (if it needs to, that's a sign the P2 boundary was wrong — fix P2).

**Reference:** [Claude Code CLI Reference](https://code.claude.com/docs/en/cli-reference),
[Settings](https://code.claude.com/docs/en/settings), [Security](https://code.claude.com/docs/en/security).

---

## Logical blocks

### 3.1 ClaudeCodeProvider (`providers/claude.py`)
Implements `AgentProvider` for `id = "claude"` (§4.4), mirroring the Codex adapter's shape:
- **`preflight()`** → `ProviderHealth`: detect the `claude` executable, parse the version, check
  auth/config status and required capabilities via available Claude Code commands; diagnostics
  carry **no secrets**.
- **`run(request)`**: build a safe `claude -p` (headless/print) invocation as an **argv list** from
  the request + `ProviderConfig`. Map the **permission mode**, **allowed/denied tools**, sandbox,
  the model, `max_turns`/`max_budget_usd` (from config), and `timeout`. Request `stream-json`
  output and pass task/plan/diff/review context **only as file paths** (§19.5).
- Parse the `stream-json` event stream → `structured_output`, `final_message`, `usage`,
  `session_id` (audit only). Reuse `providers/errors.py` for exit-code/stderr normalization and
  `providers/artifacts.py` for the §10 artifact set.

### 3.2 Permission/sandbox mapping
- Translate the request's `permission_profile` (e.g. `workspace-write`) into the correct Claude
  permission mode + allowed/denied tool set, equivalent in strictness to the Codex sandbox mapping.
- **Reject** any `extra_args` that would weaken isolation (e.g. `--dangerously-skip-permissions`) —
  defence-in-depth alongside the P1 config validator. A fallback target must run in the **same or a
  stricter** profile (this constraint is enforced by the Router in P4; the adapter must faithfully
  apply whatever profile it's handed and never silently relax it).
- The denied-commands blacklist (`git commit`/`git push`/`gh pr create`, §12) targets the agent
  process: the adapter must never let the agent perform publishing.

### 3.3 Shared fake-CLI integration harness (`tests/`)
- Promote the Phase-2 stub executables into a reusable harness parametrized by provider, so the
  same scenario matrix runs against both `codex` and `claude` without duplication.
- Stub scripts emit canned stdout/stderr/exit codes (and JSONL / stream-json shaped events) to
  simulate each outcome deterministically — **no network, no real CLI** (testing.md).

---

## Tests

**Unit (Claude-specific):**
- Command builder: request + config → exact `claude -p` argv with the right permission mode and
  allowed/denied tools; a `--dangerously-skip-permissions`-style `extra_args` is rejected.
- `stream-json` parsing: well-formed stream → populated result; malformed → `invalid_output`.
- Permission mapping: `workspace-write` → the expected Claude mode/tool set; an attempt to map to a
  weaker-than-requested profile is rejected.

**Integration (shared harness, both providers):** success; `binary_not_found`;
`authentication_failed`; `rate_limited`; `timeout`; `process_crashed`; malformed output. The matrix
must pass identically for `codex` and `claude`, proving interchangeability.

## Definition of Done

- [ ] `ClaudeCodeProvider` implements `preflight()` and `run()`, builds a safe `claude -p`, parses
      `stream-json`, maps permission mode/allowed-denied tools/sandbox, and normalizes errors.
- [ ] It reuses the P2 infra unchanged (no new provider-agnostic modules introduced here).
- [ ] Permission/sandbox mapping is at least as strict as the requested profile; isolation-weakening
      `extra_args` are rejected.
- [ ] The shared fake-CLI harness runs the full scenario matrix against **both** providers, green.
- [ ] Both providers satisfy `isinstance(p, AgentProvider)` and are behaviourally interchangeable
      from the contract's point of view.
- [ ] `/run-checks` green.

## Not in this phase

- Choosing which provider runs a stage, or switching on failure — that is the Router/fallback (P4).
- The pipeline, Git, state, and recovery (P5).

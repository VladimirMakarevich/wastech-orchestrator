# Autonomous run — open questions & decisions

This file records every ambiguity resolved by choice, contradiction found between an ADR and the code, ADR open-question decided, or scope cut made during the unattended implementation of the three refined ADRs (prompt-and-supervisor authoring contract, node-output prompt variables, subtask-context handoff) on branch `feat/prompt-supervisor-handoff` (2026-07-02). The operator should skim this in the morning.

Each entry: **Situation** (what was ambiguous/contradictory), **Decision** (what I did + the one-line rationale), **Impact / files** (paths touched; reversible?; what to double-check).

## [Block 1] Flow-aware valid-set implemented fully in Block 1 — 2026-07-02

- **Situation:** the plan mandates the lint's valid-set be flow-aware "from the start" so Block 2 extends it with "zero lint rework". Two readings: (a) ship an empty node-derived set in Block 1 and fill it in Block 2, or (b) implement the node-derived derivation now so the plumbing is genuinely flow-aware.
- **Decision:** chose (b) — `core/flow/prompt_vars.py::node_output_vars` already derives `{<id>_path}` for every agent node, so `valid_prompt_vars` = `ALLOWED_PROMPT_VARS ∪ node-derived`. Rationale: the Block-1 test "add a node, its future-derived name path is reachable" only passes cleanly this way, and it delivers the "zero rework" goal literally (Block 2 adds the *rendering/persistence* of `{<id>_path}`, not the valid-set). No packaged prompt references `{<id>_path}` at Block 1, so this is behavior-neutral there.
- **Impact / files:** `core/flow/prompt_vars.py` (new). Reversible. Operator should note the node-output *value population* + regex widening + reserved-prefix guard land in Block 2; Block 1 alone never populates `{<id>_path}`.

## [Block 1] Lint surfaced at `preflight`, scans agent+evaluator role files — 2026-07-02

- **Situation:** the ADR says "preflight/validate-time lint" and "scans each flow's role files"; the exact surface and node scope were unspecified.
- **Decision:** added `FlowRegistry.lint_all()` (parallel to `validate_all()`) and printed non-fatal `flow <name>: WARN — …` lines in `cli.run_preflight`, leaving the `ok` gate untouched (verbatim render is the safe fallback). The lint scans every node carrying a `role_file` (agent + evaluator); checks/hitl/publish nodes have no template and are skipped. Best-effort IO: an unreadable/traversing role file is skipped by the lint (the fatal path check + run-time read surface it). Rationale: `preflight` is the existing diagnostics surface and already runs `validate_all`.
- **Impact / files:** `core/flow/registry.py`, `core/flow/validator.py`, `cli.py`. Reversible. Double-check: the lint is intentionally NOT wired into `validate_all` (which install/preflight treat as fatal) — keeping warnings non-fatal.

## [Block 1] `{?…}` adoption limited to `documentation.md` — 2026-07-02

- **Situation:** the ADR asks to adopt `{?name}…{/name}` in the packaged role prompts, wrapping optional clauses. Most packaged prompts already wrap their only optional var (`{?memory_path}`, `{?subtask_spec_path}`), or reference only always-present vars (`{repo}`, `{task_id}`).
- **Decision:** wrapped only `implementation/documentation.md`'s bare `{plan_path}` / `{diff_path}` (the one real dangle risk — either can be empty). Left the deep_research/security_audit prompts as-is because their `{repo}` / `{task_id}` references are always populated. Verified the packaged flows lint clean (a regression test pins this).
- **Impact / files:** `packaged/flows/implementation/documentation.md`. Reversible. Low risk — a text-only prompt change.

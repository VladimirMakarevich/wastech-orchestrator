# Autonomous run — open questions & decisions

This file records every ambiguity resolved by choice, contradiction found between an ADR and the code, ADR open-question decided, or scope cut made during the unattended implementation of the three refined ADRs (prompt-and-supervisor authoring contract, node-output prompt variables, subtask-context handoff) on branch `feat/prompt-supervisor-handoff` (2026-07-02). The operator should skim this in the morning.

Each entry: **Situation** (what was ambiguous/contradictory), **Decision** (what I did + the one-line rationale), **Impact / files** (paths touched; reversible?; what to double-check).

## [Block 1] Flow-aware valid-set implemented fully in Block 1 — 2026-07-02

- **Situation:** the plan mandates the lint's valid-set be flow-aware "from the start" so Block 2 extends it with "zero lint rework". Two readings: (a) ship an empty node-derived set in Block 1 and fill it in Block 2, or (b) implement the node-derived derivation now so the plumbing is genuinely flow-aware.
- **Decision:** chose (b) — `core/flow/prompt_vars.py::node_output_vars` already derives `{<id>_path}` for every agent node, so `valid_prompt_vars` = `ALLOWED_PROMPT_VARS ∪ node-derived`. Rationale: the Block-1 test "add a node, its future-derived name path is reachable" only passes cleanly this way, and it delivers the "zero rework" goal literally (Block 2 adds the _rendering/persistence_ of `{<id>_path}`, not the valid-set). No packaged prompt references `{<id>_path}` at Block 1, so this is behavior-neutral there.
- **Impact / files:** `core/flow/prompt_vars.py` (new). Reversible. Operator should note the node-output _value population_ + regex widening + reserved-prefix guard land in Block 2; Block 1 alone never populates `{<id>_path}`.

## [Block 1] Lint surfaced at `preflight`, scans agent+evaluator role files — 2026-07-02

- **Situation:** the ADR says "preflight/validate-time lint" and "scans each flow's role files"; the exact surface and node scope were unspecified.
- **Decision:** added `FlowRegistry.lint_all()` (parallel to `validate_all()`) and printed non-fatal `flow <name>: WARN — …` lines in `cli.run_preflight`, leaving the `ok` gate untouched (verbatim render is the safe fallback). The lint scans every node carrying a `role_file` (agent + evaluator); checks/hitl/publish nodes have no template and are skipped. Best-effort IO: an unreadable/traversing role file is skipped by the lint (the fatal path check + run-time read surface it). Rationale: `preflight` is the existing diagnostics surface and already runs `validate_all`.
- **Impact / files:** `core/flow/registry.py`, `core/flow/validator.py`, `cli.py`. Reversible. Double-check: the lint is intentionally NOT wired into `validate_all` (which install/preflight treat as fatal) — keeping warnings non-fatal.

## [Block 1] `{?…}` adoption limited to `documentation.md` — 2026-07-02

- **Situation:** the ADR asks to adopt `{?name}…{/name}` in the packaged role prompts, wrapping optional clauses. Most packaged prompts already wrap their only optional var (`{?memory_path}`, `{?subtask_spec_path}`), or reference only always-present vars (`{repo}`, `{task_id}`).
- **Decision:** wrapped only `implementation/documentation.md`'s bare `{plan_path}` / `{diff_path}` (the one real dangle risk — either can be empty). Left the deep_research/security_audit prompts as-is because their `{repo}` / `{task_id}` references are always populated. Verified the packaged flows lint clean (a regression test pins this).
- **Impact / files:** `packaged/flows/implementation/documentation.md`. Reversible. Low risk — a text-only prompt change.

## [Block 2] Node-output persistence lives in the orchestrator post-node hook — 2026-07-02

- **Situation:** the ADR splits the node-output writer between `agent.py` ("after a node runs, persist `<node_id>.out.md`") and `postprocess.py` ("the writer can live beside `apply_output_artifact`"). It does not say which layer _calls_ it.
- **Decision:** the writer is `postprocess.write_node_output`, **called from the orchestrator's `_engine_post_node` hook** right after `apply_output_artifact` (the existing symmetric seam), not from inside the runner. `agent.py` resolves `{<id>_path}` _statelessly_ by checking whether `<id>.out.md` exists on disk, so it works on resume and regardless of write site. Rationale: mirrors exactly how `apply_output_artifact` (also a post-node concern) is wired, and keeps the runner focused on running + prompt-building.
- **Impact / files:** `core/flow/postprocess.py`, `core/orchestrator.py` (`_engine_post_node`), `core/flow/nodes/agent.py` (`_node_output_paths`). Reversible.

## [Block 2] Node-kind-aware lint; channel is agent-only for producing AND consuming — 2026-07-02

- **Situation:** the ADR says "Only agent nodes get `{<node_id>_path}`" and "the generic channel does not extend to [evaluator/checks/human]". Ambiguous whether an evaluator may _consume_ an agent's `{<id>_path}`. Block-1's lint used one flow-wide valid-set for all nodes.
- **Decision:** kept the channel agent-only on **both** sides — only agent nodes produce `.out.md` (`node_output_vars` iterates agent nodes), and only the **agent** runner is given the widened effective allowlist (`evaluator.py` unchanged, still the static `ALLOWED_PROMPT_VARS`). To keep the lint honest, made it **node-kind-aware**: an agent role file is linted against the flow-derived set, an evaluator role file against the static core set — so an evaluator referencing `{scan_path}` (which would render verbatim) is correctly flagged. This is a small extension beyond the plan's literal "extend the valid-set" wording, done for lint accuracy. Rationale: literal reading of "does not extend to them".
- **Impact / files:** `core/flow/validator.py` (`lint_prompt_variables`), `core/flow/prompt_vars.py`, `core/flow/nodes/agent.py`. Reversible. Double-check: if an operator _wants_ an evaluator to read an upstream agent's output path, that is currently a lint warning + verbatim render — revisit by widening the evaluator runner's allowlist if a real need appears.

## [Block 2] Node-output redaction uses the memory extra-secrets set, harvested once per run — 2026-07-02

- **Situation:** the ADR requires `<node_id>.out.md` to pass "the same redaction the memory/handoff writes use". `structured_output` (a possible content source) is **not** adapter-redacted, unlike `final_message`.
- **Decision:** `write_node_output` runs `redact_text(content, extra_secrets=…)`; the orchestrator passes `_memory_extra_secrets()` (secret-named env values + denied-read-file secrets — the exact set the memory write path uses), computed **once per run** and captured in the post-node closure rather than re-harvested per node.
- **Impact / files:** `core/flow/postprocess.py`, `core/orchestrator.py`. Reversible.

## [Block 3] Deleted the dead packaged `roles/summary.md`; kept `roles/supervisor.md` — 2026-07-02

- **Situation:** the ADR flags `roles/summary.md` as "currently-dead" and asks to "revive it as a flow-owned finalize prompt file". Implementation-time check required (grep for a reader).
- **Decision:** confirmed no Python reader (config default is `roles/supervisor.md`; no `finalize` global prompt exists) and no test references the _packaged_ copy (the parity fixture has its own separate `tests/core/flows/roles/summary.md`, untouched). So I **deleted** the packaged `roles/summary.md` and created the flow-owned finalize lens at `packaged/flows/implementation/summary.md`, plus the flow-owned observe lens `packaged/flows/implementation/supervisor.md`. Kept `roles/supervisor.md` — it is still the live global `config.supervisor.role_file` default (and the observe fallback for flows without a `supervisor:` block, e.g. deep_research / security_audit).
- **Impact / files:** deleted `packaged/flows/roles/summary.md`; added `implementation/{supervisor,summary}.md`; set the `supervisor:` block in `implementation.yaml`. Reversible (restore from git). `install`/`upgrade-flows` seed whatever exists under `packaged/flows/`, so the deletion just stops seeding a dead file.

## [Block 3] Finalize prompt = finalize lens + task + structured sections (AC-S4 preserved) — 2026-07-02

- **Situation:** today's `_finalize_prompt` was `observe_lens + hardcoded "## Final synthesis" text`. The ADR makes finalize a separate flow-local lens (`finalize_role_file` → built-in). Restructuring changes the _wording_ of the finalize prompt.
- **Decision:** `_finalize_prompt` now = the **finalize lens** (`finalize_role_file` → `_BUILTIN_FINALIZE`, self-contained) + a `## Task under review` line + the code-appended structured sections (`follow_ups`, `memory_delta`). The observe lens is no longer prepended to finalize (the warm session already carries the observations). AC-S4 holds by construction: when neither memory nor `emit_follow_ups` is on, the turn stays **free-text** (no `output_schema`), exactly as before — only the prompt text changed, not the turn's structure. `emit_follow_ups` is stored on the Supervisor from the flow block; memory's `emit_delta` stays a finalize() param (orthogonal, config-level).
- **Impact / files:** `core/supervisor.py`. Reversible. Double-check: for a flow with **no** `finalize_role_file` the finalize wording is now the leaner `_BUILTIN_FINALIZE` (not the old observe-lens + synthesis text) — behavior-equivalent (same free-text summary), wording only.

## [Block 3] follow_ups are evidence-gated; severity defaults to medium — 2026-07-02

- **Situation:** the ADR says each `follow_ups` record is "minimal and grounded" with `evidence`, but did not spell out the drop rule or defaults.
- **Decision:** `parse_follow_ups` is **evidence-gated** — a record with no non-empty `title` or no non-empty `evidence` is silently dropped (never raised), so an ungrounded "refactor idea" the model invents cannot reach `summary.{json,md}`. An invalid/missing `severity` defaults to `medium`. Schema stays hardcoded in code (a flow reshapes wording, never the contract). Mirrors `_parse_skill_map`'s best-effort discipline.
- **Impact / files:** `core/supervisor.py`. Reversible.

## [Block 4] Handoff assembled just-before-running the successor (not literally "after \_commit\_subtask") — 2026-07-02

- **Situation:** the ADR's seam says produce the handoff "after `_commit_subtask` (so `commit_sha` is available) and before `reset_for_next_subtask`" — i.e. produce the _next_ unit's brief right after committing the current one.
- **Decision:** I instead assemble each unit's brief **just before running that unit**, from its committed `depends_on` predecessors (read from the store). For a strictly-sequential run these are equivalent — all predecessors (orders < the unit) are already committed, so `commit_sha` is available — but "before running the unit" is **more robust on resume**: if a predecessor was committed in a prior run and is skipped this run (`unit.order in committed → continue`), the "after-commit" placement would never produce the successor's brief, whereas reading committed predecessors from the store always works. Handles the intra-task diamond (3 ← [1,2]) directly.
- **Impact / files:** `core/orchestrator.py` `_fan_out_subtasks` + `_assemble_predecessor_context`. Reversible. Behavior matches the ADR's intent; only the placement differs.

## [Block 4] Added `GitManager.files_in_commit` for the floor's "changed files" — 2026-07-02

- **Situation:** the deterministic floor lists a predecessor's **changed files**, but no existing git seam returns the files of a specific commit (`changed_code_*` are working-tree / since-base).
- **Decision:** added `GitManager.files_in_commit(sha)` (`git diff-tree --no-commit-id --name-only -r <sha>`) routed through the same safe argv runner as every git call (no shell, mandatory timeout), best-effort (`[]` on any error). It is on the concrete `GitManager` only — **not** added to the node-runner `GitPort` — since only the orchestrator (which holds the real GitManager) assembles the floor. The other floor facts (commit sha, acceptance criteria, spec pointer, title) come from the store + `SubtaskSpec`, no new git call.
- **Impact / files:** `git_manager.py`. Reversible.

## [Block 4] Handoff redaction happens once, at the orchestrator write site — 2026-07-02

- **Situation:** the ADR says the supervisor `handoff()` runs the memory-subsystem redaction "before writing". But the supervisor does **not** write the file — the orchestrator does (it owns the floor + the brief).
- **Decision:** `handoff()` returns the (unwritten) rendered brief; the orchestrator concatenates the deterministic floor + the brief and redacts the **whole** `.handoff.md` content once via `redact_text(..., extra_secrets=self._memory_extra_secrets())` (the same secret set memory/node-output use) before writing. This satisfies "no secret in the artifact" for both layers with a single chokepoint at the write site, rather than redacting the brief in the supervisor and the floor separately.
- **Impact / files:** `core/supervisor.py` (`handoff`), `core/orchestrator.py` (`_assemble_predecessor_context`). Reversible.

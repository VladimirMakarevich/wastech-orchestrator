# WRI-001 — Two-root artifact layout: curated exchange vs private home

**Status:** open **Phase:** 1 (hygiene) — foundational **Source:** [decision record](README.md), [happy-path.md](happy-path.md) **Dependencies:** —

## Problem

The launched agent runs with `cwd` = repo root and reads intermediate results from disk under `.worc/logs/<task-id>/`. Some agent-facing outputs (`<node>.out.md`, evaluator `findings.json`) sit **inside** a per-node `stages/<node>/run-*/` dir next to audit/secret artifacts (`rendered-prompt.md`, the `<attempt>-<provider>/` raw streams); others (`plan.md`, `current.diff`, the checks log) live one level up at the task root `.worc/logs/<task-id>/`; and `prompt-audit/` is a further task-level audit surface. All of it shares the same `.worc/` that also holds `.env`, `state.db`, `flows/`, the `memory/` store and other tasks' logs. Because the needed and the sensitive are interleaved across one tree, `.worc/` cannot be read-restricted without cutting the agent off from its own context.

Two layout facts the split must respect: (a) a node with an `output_artifact` slot (`plan`, `enriched_spec`, `summary`) writes to the **task root**, not a run dir, and its generic `<node>.out.md` is suppressed; a node without a slot writes `<node>.out.md` into the run dir. (b) A decomposed subtask inserts a `sub-<NN>/` level under `stages/<node>/` for the provider attempt dirs but **not** for the `node_run_dir` outputs — so the routing must be generic over task-id/node-id/run and cover the `sub-<NN>/` case, never per-node-id branching.

## Required outcome

A second, independent artifact root — the curated **exchange** — holds exactly the current task's agent-facing intermediate results and nothing else. Every artifact the agent is pointed at by a `{…_path}` variable resolves into the exchange; every audit/secret artifact stays under the private home. The exchange is in-repo, gitignored, never committed, and is a **sibling** of (not nested under) the private home, so a later relocation of the private home (WRI-005) leaves it untouched.

## In scope

- Introduce `exchange_root` alongside the private `artifacts_root` in the artifacts layer. Decide the layout explicitly: reusing the `task_artifact_dir`/`node_run_dir` builders against the new root yields `<repo>/.worc-io/logs/<task-id>/…` (the builders always insert the `logs/` segment). Either keep that (`.worc-io/logs/<task-id>/`) or parameterize the builders to drop `logs/` for the exchange — pick one and make the happy-path walkthrough match. Do **not** leave the docs showing `.worc-io/<task-id>/` while the code reuses a builder that produces `.worc-io/logs/<task-id>/`.
- Route the agent-facing writers to the exchange: the `output_artifact` slots (`plan.md`, and any `enriched_spec`/`summary` slot a flow points a variable at), generic `<node>.out.md` (`write_node_output`), **the `tool` node's redacted `stdout.txt`** ([tool.py](../../../src/wastech_orchestrator/core/flow/nodes/tool.py) `_write_redacted_artifacts`) — this is the tool nodes' `{<node_id>_path}` channel used by the blog/content flows (`check_length`, `check_chapter`), and it is **not** `write_node_output`, so it must be routed explicitly — evaluator `findings.json`, the working-tree diff, the checks command log (`{checks_path}`), subtask spec + handoff brief, and the memory retrieval packet (`{memory_path}` — the packet only; the memory **store** stays private).
- Keep the audit/secret writers under the private home: `rendered-prompt.md`, `prompt-audit/` + `timeline.jsonl`, the `<attempt>-<provider>/` raw streams, `task.enriched.md`, supervisor `summary.md`/`summary.json`, validation/failure reports, **the `checks` nodes' structured JSON reports (`citation.json`, `dependency_scan.json`) — they are not agent-facing (no `{<node>_path}` variable; only agent/tool nodes get that channel), so they stay private even though they share a `checks` run dir with nothing agent-facing**, `state.db`, `flows/`, the `tools/` executables, the `memory/` store, `security-reports/`. Because a node run dir mixes agent-facing and private files, the split is **per file within the run dir**, not per directory.
- Update **every** downstream resolver so each `{…_path}` and the "Context files" footer point into the exchange — not only `build_path_context` (which is just the 5-key `repo`/`task`/`plan`/`diff`/`checks`/`review` collector, and also feeds the P5 tool-node stdin `paths` object), `latest_run_file`, `_node_output_paths`, and the footer builder, but also: the orchestrator **resume-path resolver** (`orchestrator.py` re-derives `plan_path`/`diff_path`/`checks_path`/`review_path` on resume), `subtask_spec_path` (`decomposition.py`), and the `predecessor_context` (handoff-brief) assembler. `task.enriched.md` has no prompt variable and stays private (confirmed audit-only); the `plan`/`summary` slots and every generic `<node>.out.md` route to the exchange.
- Gitignore the exchange root the same way `.worc/` is ignored — a tracked-`.gitignore` line **and** a clone-local `.git/info/exclude` line — and give it its own `git check-ignore` probe target (the `.worc/state.db` probe does not cover `.worc-io/`, which has no `state.db`). Scoped staging excludes it by virtue of the ignore, not a pathspec; adding it to `RUNTIME_EXCLUDED_DIRS` alone does not exclude it from `git add`.
- **Extend the exchange's lifecycle to match the private home's — the split must not orphan it.** Two existing mechanisms only know the private home today and would leave the exchange dangling: (a) `worc logs clean` ([cli.py](../../../src/wastech_orchestrator/cli.py)) sweeps `worc_home_for(config)/logs` only — it must also sweep the matching exchange task dirs (same `--keep`/`--all`/confirmation semantics), or the exchange grows unbounded with no operator lever; (b) `archive_task_artifacts` ([artifacts.py](../../../src/wastech_orchestrator/providers/artifacts.py)) archives the private `logs/<task>/` on a fresh / `restart_in_place` rerun but not the exchange — so the prior attempt's stale exchange files survive the reset and, because fan-in resolves by run number (`latest_run_file`), can coexist with or shadow the fresh run. The exchange must mirror **whatever that rerun mode does to the private task dir**, per mode: **fresh / `restart_in_place`** archive the private dir ([orchestrator.py:1118,1155](../../../src/wastech_orchestrator/core/orchestrator.py#L1118)) → the exchange must be archived/cleared in lockstep; **`continue`** ([orchestrator.py:1161-1209](../../../src/wastech_orchestrator/core/orchestrator.py#L1161)) deliberately **keeps** all prior artifacts and re-enters at the interrupted node — it must **not** clear the exchange, because `_restore_engine_inputs` re-reads `plan.md`/`current.diff`/`findings.json` from it to resume. Do not blanket-archive the exchange on every rerun; gate it on the fresh/restart path only. State the retention policy explicitly (exchange lifecycle = private-home lifecycle; deleted together on `logs clean`; archived by fresh/restart and kept by continue; auto-deleted on success by WRI-007).
- Sync docs to the new location.

## Acceptance criteria

- [ ] A fresh happy-path run writes plan / diff / checks / `<node>.out.md` / findings / subtask spec / handoff / memory packet **only** under `exchange_root`, and rendered-prompt / prompt-audit / raw provider streams / summary **only** under the private home.
- [ ] Every `{…_path}` variable and the context-files footer resolve to a path under `exchange_root`; `{task_path}` is unchanged (outside both homes).
- [ ] Downstream fan-in (`{plan_path}`, `{diff_path}`, `{review_path}`, `{<node>_path}`) reads the correct artifact from the exchange, including on a node's repeat run (latest run wins).
- [ ] The exchange root is gitignored and excluded from scoped staging; no run ever stages it.
- [ ] `worc logs clean` (bare, `--keep N`, `--all`) removes the exchange task dirs together with the private-home ones; a fresh / `restart_in_place` rerun archives/clears the exchange in lockstep with the private home (no stale prior-attempt file the fan-in could pick up), while a `continue` rerun **keeps** the exchange so the resumed node still resolves plan/diff/findings.
- [ ] Artifacts written to the exchange are redaction-scrubbed exactly as today (no new unredacted content).
- [ ] The engine still branches on no specific node id; the split is layout-level and generic. Verified against **all** packaged flows — `implementation`, `deep_research`, `security_audit`, `blog_article`(`_revise`), `content_chapter`, `content_translate`, `merge` — which use only the generic node-kind channels (agent `<node>.out.md`, tool `stdout.txt`, evaluator `findings.json`, dedicated `plan`/`diff`/`checks`/`review`); a custom flow on any topic that uses the same kinds is covered with no core change.
- [ ] Docs and the shipped guide describe the exchange location; `/sync-docs` clean; prettier `proseWrap:never`.

## Verification

- Unit tests on the artifacts layer for the split write destinations and `latest_run_file` resolution across multiple runs of a node.
- A pipeline/integration test with fake CLIs asserting the two-tree layout after a full happy-path run and that every `{…_path}` value points into the exchange.
- Lifecycle tests: `worc logs clean` removes the exchange dirs alongside the private ones; a `rerun` archives/clears both trees and the fresh run does not resolve a stale exchange file.
- Git tests: the exchange is ignored and never staged (via gitignore, not a staging pathspec).
- Redaction tests on exchange artifacts.

## Out of scope

- Any read-deny enforcement (WRI-002 for Claude, WRI-003/WRI-006 for Codex).
- Relocating the private home out of the working tree (WRI-005).
- Changing the path-only prompt contract (paths only, never inlined content) — it is preserved as-is.

## Likely implementation areas

- src/wastech_orchestrator/providers/artifacts.py
- src/wastech_orchestrator/core/flow/postprocess.py
- src/wastech_orchestrator/core/flow/nodes/evaluator.py
- src/wastech_orchestrator/core/flow/nodes/agent.py
- src/wastech_orchestrator/core/flow/nodes/tool.py (route the redacted `stdout.txt` to the exchange)
- src/wastech_orchestrator/core/flow/nodes/checks.py (keep `citation.json` / `dependency_scan.json` private)
- src/wastech_orchestrator/core/flow/context_paths.py and prompt_vars.py
- src/wastech_orchestrator/core/orchestrator.py (resume-path resolver for plan/diff/checks/review)
- src/wastech_orchestrator/core/decomposition.py (subtask spec path) and the predecessor-context assembler
- src/wastech_orchestrator/composition.py and cli.py
- src/wastech_orchestrator/git_manager.py
- tests/ (artifacts, pipeline/integration, git; include a decomposed-subtask run so `sub-<NN>/` routing is covered)
- docs/operations.md, docs/glossary.md, src/wastech_orchestrator/packaged/guide/flows/prompt-variables.md

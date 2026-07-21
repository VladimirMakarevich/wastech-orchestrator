# WRI-001 — Two-root artifact layout: curated exchange vs private home

**Status:** open **Phase:** 1 (hygiene) — foundational **Source:** [decision record](README.md), [happy-path.md](happy-path.md) **Dependencies:** —

## Problem

The launched agent runs with `cwd` = repo root and reads intermediate results from disk under `.worc/logs/<task-id>/`, where agent-facing outputs (`plan.md`, `<node>.out.md`, `findings.json`, the diff, the checks report) are interleaved with audit/secret artifacts (`rendered-prompt.md`, `prompt-audit/`, the `<attempt>-<provider>/` raw streams) inside the same `.worc/` that also holds `.env`, `state.db`, `flows/`, `memory/` and other tasks' logs. Because the needed and the sensitive are interleaved in one subtree, `.worc/` cannot be read-restricted without cutting the agent off from its own context.

## Required outcome

A second, independent artifact root — the curated **exchange** — holds exactly the current task's agent-facing intermediate results and nothing else. Every artifact the agent is pointed at by a `{…_path}` variable resolves into the exchange; every audit/secret artifact stays under the private home. The exchange is in-repo, gitignored, never committed, and is a **sibling** of (not nested under) the private home, so a later relocation of the private home (WRI-005) leaves it untouched.

## In scope

- Introduce `exchange_root` alongside the private `artifacts_root` in the artifacts layer; default `<repo>/.worc-io/<task-id>/`.
- Route the agent-facing writers to the exchange: planning `plan.md` (the `output_artifact: plan` slot), generic `<node>.out.md` (`write_node_output`), evaluator `findings.json`, the working-tree diff, the checks report, subtask spec + handoff brief, and the memory retrieval packet (`{memory_path}` — the packet only; the memory **store** stays private).
- Keep the audit/secret writers under the private home: `rendered-prompt.md`, `prompt-audit/` + `timeline.jsonl`, the `<attempt>-<provider>/` raw streams, `task.enriched.md`, supervisor `summary.md`/`summary.json`, validation/failure reports, `state.db`, `flows/`, the `memory/` store, `security-reports/`.
- Update downstream resolution so every `{…_path}` and the "Context files" footer point into the exchange: `latest_run_file`, `build_path_context`, `_node_output_paths`, and the footer builder.
- Gitignore the exchange root and add it to the runtime-excluded dirs / scoped-staging exclusions (alongside `.worc/`).
- Sync docs to the new location.

## Acceptance criteria

- [ ] A fresh happy-path run writes plan / diff / checks / `<node>.out.md` / findings / subtask spec / handoff / memory packet **only** under `exchange_root`, and rendered-prompt / prompt-audit / raw provider streams / summary **only** under the private home.
- [ ] Every `{…_path}` variable and the context-files footer resolve to a path under `exchange_root`; `{task_path}` is unchanged (outside both homes).
- [ ] Downstream fan-in (`{plan_path}`, `{diff_path}`, `{review_path}`, `{<node>_path}`) reads the correct artifact from the exchange, including on a node's repeat run (latest run wins).
- [ ] The exchange root is gitignored and excluded from scoped staging; no run ever stages it.
- [ ] Artifacts written to the exchange are redaction-scrubbed exactly as today (no new unredacted content).
- [ ] The engine still branches on no specific node id; the split is layout-level and generic.
- [ ] Docs and the shipped guide describe the exchange location; `/sync-docs` clean; prettier `proseWrap:never`.

## Verification

- Unit tests on the artifacts layer for the split write destinations and `latest_run_file` resolution across multiple runs of a node.
- A pipeline/integration test with fake CLIs asserting the two-tree layout after a full happy-path run and that every `{…_path}` value points into the exchange.
- Git tests: the exchange is ignored and never staged (scoped-staging pathspec excludes it).
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
- src/wastech_orchestrator/core/flow/context_paths.py and prompt_vars.py
- src/wastech_orchestrator/composition.py and cli.py
- src/wastech_orchestrator/git_manager.py
- tests/ (artifacts, pipeline/integration, git)
- docs/operations.md, docs/glossary.md, src/wastech_orchestrator/packaged/guide/flows/prompt-variables.md

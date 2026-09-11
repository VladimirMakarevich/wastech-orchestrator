# Phase 08 — Configurable report directory, private policy included

- **Status:** ☑
- **Depends on:** none (worc, this repository; independent of phases 01 and 02)
- **Delivers:** FR-W4 — a flow may declare `report_dir: <base>` for both report output policies, `{report_dir}` becomes a prompt variable, the path is validated at flow load, and `private_control_workspace_report` may name a base outside `.worc/` with the existing publish-time leak check as the guard. Absorbs the standalone "configurable report directory" backlog item, which this phase removes from the index; the one extension over it is the private-policy allowance (D16).

## Goal

Give the optional triage flow (phase 07) a report home the connector may read without touching `.worc/` — `.worc-connect/triage/<task_id>/` — and, in the same change, let a `deep_research` operator point that flow's deliverable somewhere other than `docs/research/`. Moves AC-W4.

## Steps

1. `src/wastech_orchestrator/core/flow/schema.py` — `FlowDoc.report_dir: str | None = None`. `src/wastech_orchestrator/core/flow/snapshot.py` — add `report_dir` to `_FLOW_FIELDS` and `_parse_flow_doc`; the fingerprint is SHA-256 over the raw `flow:` mapping, so it already covers the key.
2. `src/wastech_orchestrator/core/flow/output_policy.py` — `resolve_output_policy(policy, task_id, report_dir=None)`: when set, `report_subdir = f"{report_dir}/{task_id}"` for **both** report policies; `required_files` and `private` unchanged (`report.md` for the private policy, `report.md` + `sources.json` for `repository_document`). The four call sites (`core/orchestrator.py`, `flow/nodes/checks.py`, `flow/nodes/agent.py`, `flow/nodes/publish.py`) pass `snapshot.doc.report_dir`.
3. `src/wastech_orchestrator/core/flow/validator.py` — the path rule, the actual work: repo-relative POSIX, no absolute path or drive letter, no `..`, no backslash, every segment a portable path segment and not a Windows device name (reuse `security/identifiers.py`, never a new check); not under the reserved roots `.worc/`, `.worc-io/`, the configured `paths.tasks_dir` tree, or `.git/`; a leading-dot directory outside those roots (`.worc-connect/`) is ordinary. The private policy is **not** refused an override — the "never enters git" invariant is enforced where it is today, at publish (`PublishNodeRunner._store_private_report` raises `NodeManualRequired` on any git-trackable file under the report dir); the validator cannot see the ignore state and must not pretend to. A load-time refusal names the key and the violated rule.
4. `src/wastech_orchestrator/core/prompts.py` + `core/flow/context_paths.py` — `{report_dir}` joins `ALLOWED_PROMPT_VARS` / `build_path_context` (the resolved repo-relative directory for this task, `report_subdir`); the `tool` node gets it in `paths` for free. Absent for `code_change` flows (render as empty / refused by the validator when a `code_change` flow's prompt references it — pick one and test it).
5. `src/wastech_orchestrator/packaged/flows/deep_research/` — `synthesis.md`, `architecture_design.md`, `verifier.md`, `critic.md`: the literal `{repo}/docs/research/{task_id}/` becomes `{report_dir}`. Mandatory, not optional: with the engine change alone the agent keeps writing to the old literal path and the after-stage guard hard-stops the task on the first write once an operator sets `report_dir`.
6. Docs on this branch — every page that names `docs/research` or `security-reports` today: `packaged/guide/flows/README.md`, `reference.md` (the `report_dir` key, its rule, the two policies, the gitignore requirement for a private base outside `.worc/`), `prompt-variables.md` (`{report_dir}`), `roles.md`, `packaged/guide/footprint.md`, `packaged/guide/README.md`, and `packaged/guide/skills/worc-flow/SKILL.md`. `.agents/rules/` does not name the directories (verified 2026-09-11). Breadcrumb in the PR description for the derived `flow-authoring.md`, `glossary.md`, `worc_architecture.md` on the documentation branch. Remove `docs/backlog/configurable-report-dir.md` and its index row (this phase is its implementation), and tick this phase here.
7. Tests, gates, mdlint.

## Files touched

- `src/wastech_orchestrator/core/flow/{schema,snapshot,output_policy,validator}.py`
- `src/wastech_orchestrator/core/prompts.py`, `src/wastech_orchestrator/core/flow/context_paths.py`
- `src/wastech_orchestrator/core/orchestrator.py`, `src/wastech_orchestrator/core/flow/nodes/{checks,agent,publish}.py` (one line each)
- `src/wastech_orchestrator/packaged/flows/deep_research/*.md`
- `src/wastech_orchestrator/packaged/guide/flows/`, `src/wastech_orchestrator/packaged/guide/skills/worc-flow/SKILL.md`
- `tests/core/test_flow_{output_policy,snapshot,validator,deep_research}.py`, a publish-node test
- `docs/backlog/configurable-report-dir.md` (removed), `docs/backlog/README.md`

## Invariants in play

- **A private report never enters git.** Unchanged mechanism, unchanged place: the publish node refuses a git-trackable report file, fail-closed. An operator who points the private policy at an un-ignored directory gets `manual_action_required`, not a leak.
- **Containment.** The after-stage write guard confines the flow's writing nodes to `report_subdir`; the override only moves the directory, it does not widen it.
- **Path validation is the security surface** — the reserved roots (`.worc/`, `.worc-io/`, `tasks/`, `.git/`) and the portable-segment rules are refused at load; the check reuses `security/identifiers.py`.
- **The flow selects, the core resolves.** No flow may point at an arbitrary template; only a base directory, with the engine still appending `/<task_id>`.
- **Cross-platform.** The value is POSIX in the YAML and compared with `Path.as_posix()`; a backslash is a refusal, not a conversion; device names are refused on every OS.
- **Greenfield.** No `report_dir` ⇒ today's directories byte-for-byte; no migration.

## Tests

- Validator: every refusal in AC-W4 (each reserved root, absolute, drive letter, `..`, backslash, device name); `.worc-connect/triage` accepted for both policies; `docs/adr` accepted.
- Output policy: resolution with and without the override for both policies; `code_change` ignores it.
- Snapshot: `report_dir` parsed; fingerprint changes when it changes.
- Publish node: private policy + gitignored base → artifacts registered, git untouched; private policy + trackable base → `NodeManualRequired`, nothing staged.
- Prompts: `{report_dir}` renders to the resolved directory; the `deep_research` suite passes unchanged with the prompts on the variable.

## Docs to sync in this phase

- `packaged/guide/flows/{README,reference,prompt-variables,roles}.md`, `packaged/guide/footprint.md`, `packaged/guide/README.md`, `packaged/guide/skills/worc-flow/SKILL.md`.
- `docs/backlog/README.md` (drop the `configurable-report-dir.md` row, note it landed as this phase) and `docs/backlog/tracker-connector/` — tick this phase.
- PR description breadcrumb: the derived flow-authoring, glossary and architecture pages on the documentation branch.

## Acceptance for this phase

- [x] AC-W4 passes in full.
- [x] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports` and `pytest` are green; `python tools/mdlint.py` is green.

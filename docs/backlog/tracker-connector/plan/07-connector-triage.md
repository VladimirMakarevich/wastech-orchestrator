# Phase 07 — Optional triage

- **Status:** ☐
- **Depends on:** 04, 05, and worc phase 08 (the report directory the flow declares; Q-6 decided 2026-09-11)
- **Delivers:** FR-C15 — `triage.enabled: true` turns a gated item into a triage task first; the connector reads the report and either runs the same task builder (verdict `actionable`) or writes back `needs-info` / `duplicate` / `declined`. The flow and its role prompts ship in the connector repository and are installed into `.worc/flows/` by `install-flow`, only when the switch is on.

## Goal

Add the analyse-and-reproduce mechanic the operator originally described, as a second path through the machinery phases 03–05 already proved — without the connector running an agent or the agent authoring a task. Moves AC-15.

## Steps

1. **Report channel (Q-6, decided):** the flow declares `output_policy: private_control_workspace_report`, `publishing: none`, `report_dir: .worc-connect/triage` (worc phase 08, D16). The report lands at `<repo>/.worc-connect/triage/<task_id>/report.md`; the connector reads it from its own home and never from `.worc/`. `install-flow` refuses to install on a worc whose flow validator does not know `report_dir` (detected by `worc --version` against the phase 08 release) and says which worc is needed.
2. `packaged/flows/issue_triage.yaml` + role prompts — read-only scoping and analysis nodes, an optional `workspace-write` reproduction node whose deliverable is a failing test carried **as text in the report** (the private policy confines every write to `{report_dir}`, so a test file cannot be left in the tree and no branch is pushed), a non-blocking evaluator, and a report node whose output carries a machine-readable verdict block (`verdict: actionable | needs-info | duplicate | declined`, `reason`, optional `duplicate_of`, optional draft acceptance criteria, optional `failing_test` with a suggested path and body). Prompts name the directory only through `{report_dir}`. The flow passes worc's flow validator; it can weaken nothing.
3. `cli.py install-flow` — copies the flow and prompts into `.worc/flows/`, refusing to overwrite an operator-edited copy without `--force` (the same edge the `upgrade-flows` backlog item names).
4. `core/builder.py` — the triage task: `task_type: <triage flow>`, `priority: high`, the item as body; the implementation task from a report: same builder, body = report's draft + provenance, acceptance criteria from the report when present.
5. `core/reconcile.py` — the triage task's `done` → read `.worc-connect/triage/<task_id>/report.md` (missing file → `worc:failed` with a comment naming the missing report; never a fallback to another location) → dispatch on the verdict; `needs-info` posts the report's question as a comment and labels `worc:needs-info`; a later author reply (item `updatedAt` advances, gate still satisfied) re-triggers triage with `seq + 1`. The implementation task built from an `actionable` report carries the `failing_test` block, when present, in its body under a fixed heading — the builder, not the agent, decides that.
6. Tests with a fixture report per verdict.

## Files touched

- connector: `src/worc_connect/packaged/flows/issue_triage.yaml`, `src/worc_connect/packaged/flows/issue_triage/*.md`, `src/worc_connect/{cli}.py`, `src/worc_connect/core/{builder,reconcile}.py`, `tests/`.

## Invariants in play

- The agent proposes; the connector decides and formats — a report never becomes a queued task without passing through the deterministic builder.
- The connector still launches no agent; triage runs inside worc's sandbox as an ordinary operator flow.
- The flow is validated by worc like any other; it cannot raise a permission ceiling, disable approvals, or publish on its own.
- The connector installs the flow only on explicit operator command and never edits `config.yaml`.

## Tests

- AC-15 in full; `install-flow` overwrite refusal; each verdict's write-back; re-trigger on author reply.

## Docs to sync in this phase

- Connector README (what triage adds, its cost — one agent run per item — and the `needs-info` round-trip); configuration reference (`triage.*`).

## Acceptance for this phase

- [ ] With `triage.enabled: true`, a real labelled issue produces a triage task, then either an implementation task or a `worc:needs-info` comment, per the report.
- [ ] With the switch off nothing from this phase is reachable and no flow file is written.
- [ ] Both CI families green.

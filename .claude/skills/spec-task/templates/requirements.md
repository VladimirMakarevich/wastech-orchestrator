<!-- WHAT the solution must do, derived from problem.md. Requirements describe behavior and outcomes,
     not implementation. Each one must be verifiable — acceptance-criteria.md will check it. -->

# Requirements — {{TASK_TITLE}}

Derived from [problem.md](problem.md).

## Functional requirements

<!-- Numbered so other documents can reference them (FR-1, FR-2 …). Keep each atomic and testable. -->

- **FR-1** — As the operator, I can `<capability>` so that `<outcome>`.
- **FR-2** — …

## Non-functional requirements

<!-- Only the ones that actually apply. Drop the rest — don't gold-plate. -->

- **NFR-1 (Cross-platform)** — behaves identically on Windows, Linux and macOS; paths, line endings and process control are platform-correct.
- **NFR-2 (Security envelope)** — cannot be used to weaken isolation, approvals or the flag ceiling at any value of `security.strict_isolation`; no secrets reach logs, `state.db` or artifacts.
- **NFR-3 (Fail-closed determinism)** — an invalid task, config or flow is rejected before a provider is launched; ambiguity never silently resolves to the permissive branch.
- **NFR-4 (Cost / tokens)** — `<budget, e.g. adds no per-node prompt growth; the extra call is bounded to one per task>`.
- **NFR-5 (Observability)** — `<what a run must leave behind: a ledger entry, a log line, an audit row — enough to explain the outcome after the fact>`.

## Versioned surfaces touched

<!-- Anything with a schema or a stored shape, because changing it forces a version bump and a
     compatibility decision: the `config.yaml` schema, `state.db` tables, the flow schema, task-file
     fields, the `.worc/` layout, the packaged operator guide. Say "none" if none. -->

## Dependencies & assumptions

<!-- What must already be true: a provider CLI and its version, an authenticated account, an
     existing node id or status, a config key that already exists. Assumptions get verified in
     design.md — an unverified one is an open question. -->

## Priority

<!-- Must / Should / Could per requirement, so the plan can sequence and out-of-scope can absorb the rest. -->

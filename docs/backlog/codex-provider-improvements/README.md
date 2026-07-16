# Codex provider improvement backlog

**Status:** open
**Created:** 2026-07-14
**Source:** [full Codex-provider review](../../analysis/2026-07-14-codex-provider-full-review.md)
**Owner:** unassigned

This folder contains implementation tasks, not ADRs. Each file defines a concrete outcome,
acceptance criteria, verification requirements and explicit exclusions. Design decisions discovered
during implementation still require the normal ADR process when they change architecture or a hard
invariant; that does not replace completing the task's acceptance criteria.

## Priority definitions

- **P0 — security blocker:** the current implementation can violate a hard invariant or make a
  security guarantee that it does not enforce.
- **P1 — correctness/currentness blocker:** required provider behavior is wrong, incomplete, or
  prevents the repository Definition of Done.
- **P2 — robustness/maintainability:** important hardening that should follow the P0/P1 work but
  does not currently create the same direct security exposure.

## Prioritized task list

Tasks are sorted first by priority and then by recommended implementation order inside that
priority.

| Priority | ID | Task | Source finding | Depends on |
| --- | --- | --- | --- | --- |
| P0 | CODX-001 | [Close authority-expanding Codex extra_args](p0-codx-001-close-extra-args-security-bypasses.md) (**done**) | CXP-02 | — |
| P0 | CODX-002 | [Make offline Codex invocation fail closed](p0-codx-002-controlled-offline-invocation.md) (**done**) | CXP-03 | CODX-001 |
| P0 | CODX-003 | [Enforce denied commands and denied read paths](p0-codx-003-enforce-deny-policy.md) (**done**) | CXP-01 | CODX-001, CODX-002 |
| P0 | CODX-004 | [Redact structured provider output before persistence](p0-codx-004-redact-structured-output.md) | CXP-04 | — |
| P1 | CODX-005 | [Eliminate the raw-artifact crash window](p1-codx-005-eliminate-raw-artifact-crash-window.md) | CXP-05 | CODX-004 |
| P1 | CODX-006 | [Implement current Codex reasoning semantics](p1-codx-006-current-reasoning-semantics.md) | CXP-06 | — |
| P1 | CODX-007 | [Refresh model defaults and establish one source of truth](p1-codx-007-refresh-model-defaults.md) | CXP-07 | CODX-006 |
| P1 | CODX-008 | [Add a typed Codex capability policy](p1-codx-008-typed-capability-policy.md) | CXP-08 | CODX-001, CODX-002 |
| P1 | CODX-009 | [Add typed and path-safe image inputs](p1-codx-009-typed-image-inputs.md) | CXP-08 | CODX-008 |
| P1 | CODX-010 | [Verify Codex authentication during preflight](p1-codx-010-auth-preflight.md) | CXP-09 | — |
| P1 | CODX-011 | [Restore cross-platform mypy compliance](p1-codx-011-windows-mypy-portability.md) | CXP-13 | — |
| P2 | CODX-012 | [Strengthen the Codex CLI contract preflight](p2-codx-012-cli-contract-preflight.md) | CXP-10 | CODX-002, CODX-006, CODX-008 |
| P2 | CODX-013 | [Update the Codex JSONL event parser](p2-codx-013-current-jsonl-event-parser.md) | CXP-11 | — |
| P2 | CODX-014 | [Tighten Windows sandbox failure detection](p2-codx-014-tighten-windows-failure-detection.md) | CXP-12 | — |
| P2 | CODX-015 | [Restore ruff line-length compliance](p2-codx-015-restore-ruff-compliance.md) | CXP-13 | — |

## Coverage of the review

All findings from the source review are represented:

- CXP-01 through CXP-07 map one-to-one to CODX-003, CODX-001, CODX-002, CODX-004,
  CODX-005, CODX-006 and CODX-007 respectively.
- CXP-08 is intentionally split into capability governance (CODX-008) and typed image inputs
  (CODX-009), because they have different deliverables and can be verified independently.
- CXP-09 through CXP-12 map to CODX-010, CODX-012, CODX-013 and CODX-014.
- CXP-13 is split into the cross-platform mypy defect (CODX-011) and mechanical ruff cleanup
  (CODX-015).

## Delivery rules

For every task:

1. Preserve the hard invariants in AGENTS.md and .agents/rules.
2. Add or update behavior tests in the same change.
3. Update configuration, operations and architecture documentation when the public contract changes.
4. Run the full Definition of Done gates.
5. Change the task status to completed only after every acceptance criterion is demonstrated.
6. Do not mark a weaker fallback or a warning as completion when the task requires fail-closed
   enforcement.

## Recommended milestones

### Milestone A — security boundary

CODX-001 → CODX-002 → CODX-003, with CODX-004 deliverable in parallel.

Exit condition: task input and configuration cannot expand authority, denied operations are
actually blocked, offline is demonstrably offline, and structured output cannot persist secrets.

### Milestone B — current provider contract

CODX-005, CODX-006, CODX-007, CODX-008, CODX-009, CODX-010 and CODX-011.

Exit condition: the provider accurately represents models/reasoning/capabilities, accepts typed
image inputs safely, reports authentication honestly and passes mandatory static checks.

### Milestone C — compatibility and robustness

CODX-012, CODX-013, CODX-014 and CODX-015.

Exit condition: incompatible CLI surfaces fail in preflight, current JSONL failures are classified
correctly, Windows diagnostics do not cause false fallbacks, and all quality gates are green.

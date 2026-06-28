# 04.1 — `worc memory` CLI

[phase](index.md) · [design §5,§7](../../design.md) · [acceptance: AC-C1, AC-SF4](../../acceptance-criteria.md)

**Goal:** the operator surface to inspect and repair memory — `show | validate | compact | restore`, each with a `--dry-run` plan before execute.

## Scope

In: a nested `worc memory` subparser (its own `add_subparsers`) modeled on `cmd_upgrade_config`; the four verbs; `--dry-run`; disabled-state no-op (Q10). Out: the `CleanupJob` the verbs invoke (04.2), the `DerivedIndex` (04.4), the snapshot/restore primitive (01.4).

## Approach

- Add `memory` to the main subparsers (`src/wastech_orchestrator/cli.py` ~line 197) with its **own** `add_subparsers` (model the nested `logs` subparser ~line 525 and `cmd_upgrade_config` ~line 705).
- Verbs (Q4 — locked, **no** `defrag` alias):
  - `show` — read-only summary of the store (tiers, counts, recent audit).
  - `validate` — read-only staleness/contradiction report (via `DerivedIndex`).
  - `compact` — **mutating**; because the operator is in the foreground it may run a **fuller** pass than the bounded idle job (FR6).
  - `restore` — **mutating**; snapshot rollback, reusing the 01.4 `restore` primitive (AC-SF4).
- `--dry-run` prints the plan before executing for the mutating verbs (AC-C1). Mutating verbs run **only when no task is active**; atomic writes guard races (FR6).
- Disabled (Q10) → no-op with a clear "memory disabled" notice.
- Hand-editing the plain `md`/`jsonl` files stays a first-class path; a `worc memory add/edit` is a possible V1.x (out of scope here).

## Files

- `src/wastech_orchestrator/cli.py` (subparser + `cmd_memory_*` handlers).

## Tests

- All four verbs exist; mutating verbs show a `--dry-run` plan before execute (AC-C1).
- `show` / `validate` are read-only.
- Disabled → no-op notice, no side effects (Q10).

## Done when

AC-C1 holds; the four verbs work with `--dry-run`; disabled is a clean no-op.

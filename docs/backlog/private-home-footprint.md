# Private-home footprint: group the per-task roots and explain the seals

Status: **proposed** Date: 2026-07-27 Owner: Vladimir Makarevich

Two operator-observation items about what the orchestrator leaves in the target repo's `.worc/`. Both were reported after operating against `wastech-mdlint` and both are about **comprehension of the private-home layout**, not about correctness: the first is a layout/naming change, the second turns out to be correct-by-design behavior whose only real defect is that nothing tells the operator so.

Neither part changes read isolation. All four per-task roots sit under `private_home` and are named explicitly in [`InternalDenyPolicy`](../../src/wastech_orchestrator/runtime_layout.py) — the agent must never read them, and that stays true whatever they are called.

## Evidence base (`wastech-mdlint`, verified 2026-07-27)

`.worc/` after eight consecutive successful tasks (`p10-01-governance-docs-4`, `p9-11-01`…`p9-11-07`):

| Root | Contents | Size |
| --- | --- | --- |
| `control-bundles/` | 8 dirs — one per task | 448 KB |
| `instruction-bundles/` | 8 dirs — one per task | 168 KB |
| `exchange-seals/` | 8 dirs, each with exactly one `seal-000001/` | 572 KB |
| `exchange-quarantine/` | **absent** — no mutation was ever detected | — |
| `.worc-io/` | present and **empty** — every terminal exchange was torn down as designed | 0 B |

Every one of the eight `manifest.json` files records `"final_status": "done"`. For scale: the same `.worc/` holds 55 MB of `logs/` and 33 MB of `memory/`, so ~1.2 MB across the three per-task roots (~150 KB/task) is **not** a disk problem. It is a legibility problem — three sibling directories at the root of the operator's control home, none of which the operator has any way to interpret.

## Part 1 — `control-bundles/` and `instruction-bundles/` should live under one `bundles/` parent

### Observation

> Combine `control-bundles` and `instruction-bundles` into one subfolder — e.g. a `bundles` root with `control` + `instruction` inside it.

### Current behavior (verified)

Each frozen bundle root is a separate top-level directory of `private_home`, named by its own constant in [`runtime_layout.py`](../../src/wastech_orchestrator/runtime_layout.py):

```python
CONTROL_BUNDLE_DIRNAME = "control-bundles"  # runtime_layout.py:46
INSTRUCTION_BUNDLE_DIRNAME = "instruction-bundles"  # runtime_layout.py:54
EXCHANGE_SEAL_DIRNAME = "exchange-seals"  # runtime_layout.py:61
EXCHANGE_QUARANTINE_DIRNAME = "exchange-quarantine"  # runtime_layout.py:67
```

Because every path is already resolved through these four constants, the change is genuinely small — they are the only place the literals live in `src/`:

| What | Where | Size |
| --- | --- | --- |
| The two (or four) dirname constants | [`runtime_layout.py:46`](../../src/wastech_orchestrator/runtime_layout.py#L46), [`:54`](../../src/wastech_orchestrator/runtime_layout.py#L54) | the whole change |
| Deny-policy construction | [`composition.py:81-82`](../../src/wastech_orchestrator/composition.py#L81) | 2 lines (may collapse to 1 — see below) |
| Bundle-dir resolution | [`orchestrator.py:2083`](../../src/wastech_orchestrator/core/orchestrator.py#L2083), [`instruction_bundle.py:130`](../../src/wastech_orchestrator/core/flow/instruction_bundle.py#L130), [`exchange_seal.py:120`](../../src/wastech_orchestrator/core/flow/exchange_seal.py#L120) | already constant-driven, 0 lines |
| Hardcoded literals in tests | `tests/core/test_orchestrator.py` (3), `tests/core/test_composition_layout.py` (1), `tests/providers/test_claude_command.py` (4), `tests/providers/test_codex_profile.py` (2) | 10 lines |
| Operator docs | see Part 2 — there is nothing to update yet, which is itself the finding | — |

**No migration.** These roots are private runtime state, not operator-authored content, and nothing outside the orchestrator reads them. A rename simply orphans whatever is on disk from an older build; the operator deletes it (or `logs clean`'s successor does — see [runtime-artifact-retention.md](runtime-artifact-retention.md)). Do not write migration code for this.

### The choice to make (recommendation, not a decision)

- **(A) The literal ask** — `bundles/control/<task-id>/` + `bundles/instruction/<task-id>/`. Two directories become one; `exchange-seals/` and `exchange-quarantine/` stay where they are. Gives a natural home for a future third bundle type.
- **(B) Group all four per-task roots** — one parent (e.g. `runs/`) holding `control-bundles/`, `instruction-bundles/`, `exchange-seals/`, `exchange-quarantine/`. All four share the same defining property: per-task private state keyed by task id, never agent-readable.

**Recommended: B**, because it pays for itself twice beyond the tidier listing. The `InternalDenyPolicy` bundle entries collapse into a single deny prefix ([`runtime_layout.py`](../../src/wastech_orchestrator/runtime_layout.py) `denied_paths`), and retention gets **one** root to reason about instead of four scattered ones — which is exactly the open question Part 2 of [runtime-artifact-retention.md](runtime-artifact-retention.md) is stuck on. Grouping does not force a uniform retention policy: per-root policy still applies (quarantine is security evidence and probably must never auto-delete).

Whichever wins, two constraints hold: the parent must not be confusable with the repo's committed `tasks/` lifecycle tree (so **not** `tasks/`), and the grouping must stay a pure path change — the deny set, the manifest digests, and the quiescence precondition on sealing (`get_exchange_guard` → `exchange_active_unsafe`, [`orchestrator.py:3993-4001`](../../src/wastech_orchestrator/core/orchestrator.py#L3993)) are all untouched.

## Part 2 — `exchange-seals/` is non-empty after a fully successful run

### Observation

> Why is `exchange-seals` not empty, when no task failed and everything completed successfully?

### What actually happens (verified — this is by design)

`exchange-seals/` is non-empty **precisely because** the run succeeded. [`_seal_terminal_exchange`](../../src/wastech_orchestrator/core/orchestrator.py#L3980) runs at **every** terminal transition, and `done` is a terminal status like any other: [`seal_exchange`](../../src/wastech_orchestrator/core/flow/exchange_seal.py#L297) builds a checksum manifest of the live `.worc-io/<task-id>/`, copies it into `exchange-seals/<task-id>/seal-<NNNNNN>/`, re-verifies it, and then removes the in-repo exchange. The eight manifests in `wastech-mdlint` all read `"final_status": "done"` — the seal is the **archive of what the agent last saw** (`task.md`, `plan.md`, `current.diff`, `stages/<stage>/…`), not a record that something went wrong.

The failure-side artifact is the _other_ directory: `exchange-quarantine/`, written by [`quarantine_contaminated`](../../src/wastech_orchestrator/core/flow/exchange_seal.py#L452) only when mutation detection reports an agent-side change to the exchange (`exchange_contaminated`, or a before/after manifest pair diffed by `diff_exchange_manifests`). In `wastech-mdlint` it does not exist at all — the correct signal for eight clean runs.

So there is no defect in the sealing behavior. There are three real gaps behind the confusion:

1. **The name and its neighbor mislead.** "Seal" reads as forensics, and it sits directly beside `exchange-quarantine/` — a genuine incident artifact. An operator scanning `.worc/` reasonably concludes something was quarantined-adjacent. A success-path archive should not be shelved next to the tainted-evidence root, or should not be named like one.
2. **Zero operator-facing documentation.** `grep` over the installed `.worc/guide/` and `.worc/config.example.yaml` in `wastech-mdlint` returns **no match** for any of `control-bundles`, `instruction-bundles`, `exchange-seals`, `exchange-quarantine`. The only place these roots are explained is orchestrator source docstrings and [`.claude/skills/analyze-task-run/SKILL.md`](../../.claude/skills/analyze-task-run/SKILL.md#L65) — neither of which is in the operator's tree. Four directories appear in the operator's own control home with no reachable explanation of what they are, whether they are safe to delete, or which one means trouble.
3. **No retention, so "non-empty" only ever grows.** One `seal-<NNNNNN>` per terminal transition, per task, forever. Already covered as an open question in [runtime-artifact-retention.md](runtime-artifact-retention.md) Part 2 — not re-opened here.

### Proposed direction

Not a redesign — sealing on success is correct and stays:

- Document the four roots where the operator will actually meet them: the shipped `packaged/guide/` copy, plus `docs/operations.md` on `main`. Say for each: what writes it, when, whether its presence is normal, whether it is safe to delete, and that the agent can never read it.
- State plainly that a seal on a `done` task is the expected outcome, and that **`exchange-quarantine/` is the directory whose existence means something happened**.
- Consider separating the two by location or by name so the success archive does not read as an incident (this folds naturally into Part 1's grouping decision — e.g. seals and quarantine need not be siblings).
- Consider a read surface for it. The seal is the highest-value post-mortem artifact the orchestrator keeps (`analyze-task-run` reads it), and today the only way in is a manual `find` under a directory the operator has been told nothing about.

## Relationship to the adjacent item

[runtime-artifact-retention.md](runtime-artifact-retention.md) owns **how long** any of this is kept (`logs clean` gaps, `tasks/done/`, the four per-task roots, the stray `config.yaml.bak-*` / `state.db*.bak*` files). This item owns **where it lives and whether the operator can understand it**. They should be decided together — the grouping choice in Part 1 (B) directly simplifies the retention question — but they are separately implementable, and neither blocks the other.

## Out of scope

- Retention, pruning, and caps for any of the four roots — that is the retention item.
- `InternalDenyPolicy`/`ProviderWriteGuardPolicy` semantics, the manifest digests, the `_SEAL_FORMAT` version, and the quiescence precondition on sealing: a path/naming change must not touch any of them.
- `logs/` (55 MB) and `memory/` (33 MB) in `wastech-mdlint` — much larger, already owned elsewhere (`logs clean` / `memory compact`).
- Any migration path for existing on-disk bundles. Private runtime state, greenfield deployment, no compatibility to preserve.

## Acceptance criteria (sketch, for whoever picks this up)

- A decision is recorded on the grouping shape (A, B, or keep-as-is) with its rationale, and the `InternalDenyPolicy` deny set is expressed against the chosen shape — proven by test, not by coincidence of location.
- After the change, the per-task private roots are reachable from a single named constant path, no literal dirname survives outside `runtime_layout.py` in `src/`, and the tests assert the new layout instead of hardcoding old literals.
- The operator-facing docs present on the branch explain all four roots — including that a `seal-*` after a successful task is normal and that `exchange-quarantine/` is the one that signals a problem. On `dev` that means the shipped `src/wastech_orchestrator/packaged/guide/` copy; a doc-impact note flags `operations.md` for the `main` pass.
- Cross-platform: the grouping is `pathlib`-only with POSIX-form stored strings, and the deny projection is verified on Windows path shapes as well as POSIX.
- `pytest`, `mypy src`, `lint-imports`, `ruff` all green; `runtime_layout.py` stays a stdlib-only leaf with no new imports.

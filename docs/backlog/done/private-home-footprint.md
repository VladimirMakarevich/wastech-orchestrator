# Private-home footprint: group the per-task roots and explain the seals

Status: **accepted 2026-07-27** (every fork closed in [Acceptance decisions](#acceptance-decisions-2026-07-27)) Date: 2026-07-27 Updated: 2026-07-27 (line references re-verified against `dev`) Owner: Vladimir Makarevich

Two operator-observation items about what the orchestrator leaves in the target repo's `.worc/`. Both were reported after operating against `wastech-mdlint` and both are about **comprehension of the private-home layout**, not about correctness: the first is a layout/naming change, the second turns out to be correct-by-design behavior whose only real defect is that nothing tells the operator so.

Neither part changes read isolation. All four per-task roots sit under `private_home` and two of them are named explicitly in [`InternalDenyPolicy`](../../../src/wastech_orchestrator/runtime_layout.py#L100) — the agent must never read them, and that stays true whatever they are called.

## Evidence base (`wastech-mdlint`, verified 2026-07-27)

`.worc/` after eight consecutive successful tasks (`p10-01-governance-docs-4`, `p9-11-01`…`p9-11-07`):

| Root | Contents | Size |
| --- | --- | --- |
| `control-bundles/` | 8 dirs — one per task | 448 KB |
| `instruction-bundles/` | 8 dirs — one per task | 168 KB |
| `exchange-seals/` | 8 dirs, each with exactly one `seal-000001/` | 572 KB |
| `exchange-quarantine/` | **absent** — no mutation was ever detected | — |
| `.worc-io/` | present and **empty** — every terminal exchange was torn down as designed | 0 B |

Every one of the eight `manifest.json` files records `"final_status": "done"`. For scale: the same `.worc/` holds 55 MB of `logs/` and 33 MB of `memory/`, and a **single** per-task log dir (`logs/p9-11-07-custom-missing-id`, 5.7 MB) is larger than all three per-task roots combined. So ~1.2 MB across them (~150 KB/task) is **not** a disk problem. It is a legibility problem — three sibling directories at the root of the operator's control home, none of which the operator has any way to interpret.

## Part 1 — the per-task roots should live under one parent

### Observation

> Combine `control-bundles` and `instruction-bundles` into one subfolder — e.g. a `bundles` root with `control` + `instruction` inside it.

### Current behavior (verified)

Each frozen bundle root is a separate top-level directory of `private_home`, named by its own constant in [`runtime_layout.py`](../../../src/wastech_orchestrator/runtime_layout.py):

```python
CONTROL_BUNDLE_DIRNAME = "control-bundles"  # runtime_layout.py:46
INSTRUCTION_BUNDLE_DIRNAME = "instruction-bundles"  # runtime_layout.py:54
EXCHANGE_SEAL_DIRNAME = "exchange-seals"  # runtime_layout.py:61
EXCHANGE_QUARANTINE_DIRNAME = "exchange-quarantine"  # runtime_layout.py:67
```

Because every path is already resolved through these four constants, the change is genuinely small — they are the only place the literals live in `src/`:

| What | Where | Size |
| --- | --- | --- |
| The two (or four) dirname constants | [`runtime_layout.py:46`](../../../src/wastech_orchestrator/runtime_layout.py#L46), [`:54`](../../../src/wastech_orchestrator/runtime_layout.py#L54) | the whole change |
| Deny-policy construction | [`composition.py:81-82`](../../../src/wastech_orchestrator/composition.py#L81) | 2 lines (may collapse to 1 — see below) |
| Bundle-dir resolution | [`orchestrator.py:2084`](../../../src/wastech_orchestrator/core/orchestrator.py#L2084), [`instruction_bundle.py:131`](../../../src/wastech_orchestrator/core/flow/instruction_bundle.py#L131), [`exchange_seal.py:122`](../../../src/wastech_orchestrator/core/flow/exchange_seal.py#L122) and [`:127`](../../../src/wastech_orchestrator/core/flow/exchange_seal.py#L127) | already constant-driven, 0 lines |
| Hardcoded literals in tests | 11 lines: `tests/providers/test_claude_command.py` (5), `tests/core/test_orchestrator.py` (3), `tests/providers/test_codex_profile.py` (2), `tests/core/test_composition_layout.py` (1). Only the two _bundle_ literals appear — no test hardcodes `exchange-seals`/`exchange-quarantine`, so option (B) below costs no extra test churn | 11 lines |
| Operator docs | see Part 2 — there is nothing to update yet, which is itself the finding | — |

**No migration code — but the rename is no longer invisible.** These roots are private runtime state, not operator-authored content, and nothing outside the orchestrator reads them, so there is nothing to convert. What changed since this item was first written is that the read-isolation cluster **merged to `main` on 2026-07-25** (`61ef90f`, #39), so the old directories exist on disk in real targets today (eight of each in `wastech-mdlint`). A rename orphans them, and **no CLI verb can currently reach them** — `logs clean` walks only `.worc/logs/` ([runtime-artifact-retention.md](runtime-artifact-retention.md) Part 1). So the operator-visible consequence is a `.worc/` that shows both the old and the new shape until they delete the old one by hand. Still do not write migration code; do state the orphaning in the change's docs, and prefer landing the retention item's cleanup path with or before this one.

### The choice that was on the table

- **(A) The literal ask** — `bundles/control/<task-id>/` + `bundles/instruction/<task-id>/`. Two directories become one; `exchange-seals/` and `exchange-quarantine/` stay where they are. Gives a natural home for a future third bundle type.
- **(B) Group all four per-task roots** — one parent holding `control-bundles/`, `instruction-bundles/`, `exchange-seals/`, `exchange-quarantine/`. All four share the same defining property: per-task private state keyed by task id, never agent-readable.

**Decided: (B) under `runs/`** — see PH-D1 and PH-D2 in [Acceptance decisions](#acceptance-decisions-2026-07-27). Grouping does not force a uniform retention policy: per-root policy still applies, and `exchange-quarantine/` is exempt from automatic deletion ([runtime-artifact-retention.md](runtime-artifact-retention.md) RA-D6).

**Be precise about what the deny-set collapse buys, because it is not "more secure".** Everything under `private_home` is already denied transitively; the bundle roots are named explicitly so the provider projection denies them **by name rather than by coincidence of location**, and so they stay denied once `private_home` moves out of tree ([`runtime_layout.py:110-118`](../../../src/wastech_orchestrator/runtime_layout.py#L110)). Grouping preserves that property with one entry instead of two — a legibility and maintenance win, not a coverage win. It follows that the acceptance test must assert the named parent appears in `denied_paths`; a test that passes only because the parent happens to sit under `private_home` proves nothing.

Whichever wins, three constraints hold:

- The parent must not be confusable with the repo's committed `tasks/` lifecycle tree (so **not** `tasks/`, which `.worc/` already uses for `tasks/rejected/` besides).
- It must not collide with an existing `.worc/` child: `config.yaml`, `flows/`, `git-null-hooks/`, `guide/`, `logs/`, `memory/`, `state.db*`, `tasks/`, `workspace/`, `.env*`. Both `runs/` and `bundles/` are free.
- The grouping must stay a pure path change — the deny semantics, the manifest digests, and the quiescence precondition on sealing (`get_exchange_guard` → `exchange_active_unsafe`, [`orchestrator.py:4014`](../../../src/wastech_orchestrator/core/orchestrator.py#L4014)) are all untouched.

## Part 2 — `exchange-seals/` is non-empty after a fully successful run

### Observation

> Why is `exchange-seals` not empty, when no task failed and everything completed successfully?

### What actually happens (verified — this is by design)

`exchange-seals/` is non-empty **precisely because** the run succeeded. [`_seal_terminal_exchange`](../../../src/wastech_orchestrator/core/orchestrator.py#L4000) runs at **every** terminal transition, and `done` is a terminal status like any other: [`seal_exchange`](../../../src/wastech_orchestrator/core/flow/exchange_seal.py#L299) builds a checksum manifest of the live `.worc-io/<task-id>/`, copies it into `exchange-seals/<task-id>/seal-<NNNNNN>/`, re-verifies it, and then removes the in-repo exchange. The eight manifests in `wastech-mdlint` all read `"final_status": "done"` — the seal is the **archive of what the agent last saw** (`task.md`, `plan.md`, `current.diff`, `stages/<stage>/…`), not a record that something went wrong.

The failure-side artifact is the _other_ directory: `exchange-quarantine/`, written by [`quarantine_contaminated`](../../../src/wastech_orchestrator/core/flow/exchange_seal.py#L454) only when mutation detection reports an agent-side change to the exchange (`exchange_contaminated`, or a before/after manifest pair diffed by `diff_exchange_manifests`). In `wastech-mdlint` it does not exist at all — the correct signal for eight clean runs.

So there is no defect in the sealing behavior. There are three real gaps behind the confusion:

1. **The name and its neighbor mislead.** "Seal" reads as forensics, and it sits directly beside `exchange-quarantine/` — a genuine incident artifact. An operator scanning `.worc/` reasonably concludes something was quarantined-adjacent. A success-path archive should not be shelved next to the tainted-evidence root, or should not be named like one.
2. **Zero operator-facing documentation — in the source of truth, not just in the installed copy.** `grep` over `src/wastech_orchestrator/packaged/` (the whole shipped tree: `guide/`, `flows/`, `config.example.yaml`) returns **no match** for any of `control-bundles`, `instruction-bundles`, `exchange-seals`, `exchange-quarantine` — so every future `worc install` reproduces the same silence. The only place these roots are explained is orchestrator source docstrings and [`.claude/skills/analyze-task-run/SKILL.md:65`](../../../.claude/skills/analyze-task-run/SKILL.md#L65) — neither of which is in the operator's tree. Four directories appear in the operator's own control home with no reachable explanation of what they are, whether they are safe to delete, or which one means trouble. There is also no page in `guide/` that owns the `.worc/` footprint at all (`README.md`, `best-practices.md`, `decision-guide.md`, `config/`, `flows/`, `skills/`, `tasks/`), so where this lands is itself a decision.
3. **No retention, so "non-empty" only ever grows.** One `seal-<NNNNNN>` per terminal transition, per task, forever. Already covered as an open question in [runtime-artifact-retention.md](runtime-artifact-retention.md) Part 2 — not re-opened here.

### Direction

Not a redesign — sealing on success is correct and stays. Each item below is settled in [Acceptance decisions](#acceptance-decisions-2026-07-27):

- Document the four roots where the operator will actually meet them: the shipped `packaged/guide/` copy, plus `docs/operations.md` on `main`. Say for each: what writes it, when, whether its presence is normal, whether it is safe to delete, and that the agent can never read it. (PH-D4 — a new page owns this.)
- State plainly that a seal on a `done` task is the expected outcome, and that **`exchange-quarantine/` is the directory whose existence means something happened**. (PH-D4.)
- Separate the success archive from the incident root so the former does not read as the latter. (PH-D3 — the `runs/` grouping does this; no rename.)
- A read surface for the seal. It is the highest-value post-mortem artifact the orchestrator keeps and, because the in-repo `.worc-io/<task-id>/` is removed at terminal, it is the **only** place a finished task's agent-facing `plan.md` / `current.diff` / findings survive at all — `analyze-task-run` reads it for exactly that reason. Today the only way in is a manual `find` under a directory the operator has been told nothing about. (PH-D5 — deferred to its own item.)

## Relationship to the adjacent item

[runtime-artifact-retention.md](runtime-artifact-retention.md) owns **how long** any of this is kept (`logs clean` gaps, the four per-task roots, and the orchestrator-written `config.yaml.bak-*` / `flows.bak-*` files). This item owns **where it lives and whether the operator can understand it**. They were decided together on 2026-07-27, and the dependency runs both ways: PH-D1's single `runs/` parent is what made that item's retention policy (RA-D6) tractable, and that item's cleanup verb is what saves the operator from hand-deleting the orphans this rename creates. They remain separately implementable — but implementing this one first is cheaper, because then the retention verb is written against the final layout instead of being ported to it.

## Out of scope

- Retention, pruning, and caps for any of the four roots — that is the retention item.
- `InternalDenyPolicy`/`ProviderWriteGuardPolicy` semantics, the manifest digests, the `_SEAL_FORMAT` version, and the quiescence precondition on sealing: a path/naming change must not touch any of them.
- `logs/` (55 MB) and `memory/` (33 MB) in `wastech-mdlint` — much larger, already owned elsewhere (`logs clean` / `memory compact`).
- Any migration path for existing on-disk bundles. Private runtime state, greenfield deployment, no compatibility to preserve — the orphaned old directories are documented, not converted.

## Acceptance decisions (2026-07-27)

### PH-D1 — option (B): all four per-task roots move under one `runs/` parent

`.worc/runs/` becomes the single parent of `control-bundles/`, `instruction-bundles/`, `exchange-seals/`, and `exchange-quarantine/`, each keeping its own name and its own `<task-id>/` layer underneath. The wider variant wins over the literal ask because it pays three times: the operator's control home stops showing three uninterpretable siblings, the deny set gains one named entry instead of two, and — the load-bearing one — the retention policy in [runtime-artifact-retention.md](runtime-artifact-retention.md) RA-D6 gets **one** root to reason about, which is what let that item be accepted at all.

The cost is the same as option (A)'s: the four constants plus 11 hardcoded lines in tests. No test hardcodes `exchange-seals`/`exchange-quarantine`, so widening the scope from two roots to four adds no test churn beyond the two bundle literals that (A) would have touched anyway.

### PH-D2 — the parent is named `runs/`

Collision-free against every existing `.worc/` child (`config.yaml`, `flows/`, `git-null-hooks/`, `guide/`, `logs/`, `memory/`, `state.db*`, `tasks/`, `workspace/`, `.env*`), and it names what the four roots actually hold: per-task state from a run. Explicitly **not** `tasks/` — that would collide both with the repo-root task lifecycle tree and with `.worc/tasks/rejected/`. Not `bundles/` either: two of the four roots are not bundles.

### PH-D3 — the seals/quarantine adjacency problem is solved by PH-D1, not by a further split

The complaint behind Part 2 was that a success archive sits beside a tainted-evidence root in the operator's control home. Once both are children of `runs/`, neither is a top-level sibling of anything the operator browses, and the guide page from PH-D4 states the difference in words. No renaming of `exchange-seals/` and no separate location for it: the seal is genuinely the archive of a sealed exchange, and inventing a second name for it to avoid a forensic connotation would trade an accurate name for a comfortable one.

### PH-D4 — a new page in the shipped guide owns the `.worc/` footprint

The `guide/` tree has no page that owns what the orchestrator leaves in the target repo, which is why all four roots are undocumented in the operator's copy. A new page gets that job: for each root — what writes it, when, whether its presence is normal, whether it is safe to delete, and that the agent can never read it. It must state plainly that a `seal-*` on a `done` task is the expected outcome and that `exchange-quarantine/` is the directory whose existence means something happened. It is also the page [runtime-artifact-retention.md](runtime-artifact-retention.md) RA-D6 needs for documenting what automatic cleanup removes and what survives, so the two changes share one destination rather than each bolting a paragraph onto an unrelated page.

This is the one decision here taken by default rather than by an explicit owner pick — the alternative (a section inside `best-practices.md` or `README.md`) is defensible, and the choice is reversible at implementation time.

### PH-D5 — a read surface for the seals is a follow-up, not part of this item

Not required to make the grouping or the documentation correct. It gains weight from the retention decision, though: an operator who switches automatic cleanup off specifically to analyze runs ([runtime-artifact-retention.md](runtime-artifact-retention.md) RA-D6, manual mode) is left with a hand-written `find` into a directory they were told nothing about — and because the in-repo `.worc-io/<task-id>/` is removed at terminal, the seal is the only surviving copy of a finished task's `plan.md` / `current.diff`. Worth raising as its own item once the grouping lands.

## Acceptance criteria (for whoever picks this up)

- All four per-task roots resolve under `.worc/runs/`, and `runs/` appears **by name** in `InternalDenyPolicy.denied_paths` — proven by a test that would fail if the entry were dropped and only the transitive `private_home` deny remained.
- After the change, the per-task private roots are reachable from a single named constant path, no literal dirname survives outside `runtime_layout.py` in `src/`, and the tests assert the new layout instead of hardcoding old literals (11 lines across four test files today).
- A new page in the shipped `src/wastech_orchestrator/packaged/guide/` copy explains all four roots — what writes each one and when, whether its presence is normal, whether it is safe to delete, that the agent can never read it, that a `seal-*` after a successful task is normal, that `exchange-quarantine/` is the one that signals a problem, and that the move to `runs/` leaves the old directories behind for the operator to delete. A doc-impact note flags `operations.md` for the `main` pass.
- Cross-platform: the grouping is `pathlib`-only with POSIX-form stored strings, and the deny projection is verified on Windows path shapes as well as POSIX.
- `pytest`, `mypy src`, `lint-imports`, `ruff` all green; `runtime_layout.py` stays a stdlib-only leaf with no new imports.

# VF-23 — the pending-queue order the operator sees is not the order the daemon runs: bytewise filename tie-break, platform-dependent, and `worc list` bypasses the ranking entirely

Status: **open — task** Date: 2026-07-25 Owner: Vladimir Makarevich Related: [VF-21](runtime-validation-findings.md) (`watch` tick / queue behavior), [VF-14](runtime-validation-findings.md) (ledger/UX observability)

## Problem (operator-stated)

The operator reads the queue order off the filesystem — the IDE / file manager listing of `tasks/pending/` — and the daemon runs a **different** order. Nothing is racing and nothing is non-deterministic; the two orders simply disagree, so the operator cannot predict which task runs next. Concretely: with `p9-07-…` and `p10-01-…` both pending, the IDE shows `p9-07` first and the orchestrator claims `p10-01` first.

## Current behavior (verified)

[`scan_pending_sorted`](../../../src/wastech_orchestrator/cli.py#L1266-L1281) is the declared single source of truth for run order — shared by `watch_once` and the read-only monitor — and it sorts by `(priority_rank, path)`:

- `priority_rank` comes from the front-matter `priority` (`high`=0, `mid`=1, `low`=2; unrecognised/missing folds to `mid`) — [`task/model.py:80-98`](../../../src/wastech_orchestrator/task/model.py#L80-L98). This is the only intentional ordering lever.
- the tie-break is the `Path` itself, and [`select_pending`](../../../src/wastech_orchestrator/cli.py#L1214-L1218) is `sorted(folder.iterdir())` — i.e. **bytewise on the filename**, not natural/numeric.

Three distinct defects follow.

### 1. Bytewise tie-break disagrees with every file manager (the reported symptom)

`p10-01…` sorts before `p9-07…` because at position 2 `'1'` (0x31) < `'9'` (0x39). File managers and IDEs use **natural** sort, which groups `p9` before `p10`. The orchestrator is deterministic and self-consistent — it is just ordering by bytes where the operator (and the phase-numbered naming convention this repo's task files actually use) means numbers.

### 2. `sorted(Path)` is platform-dependent — the run order differs on Windows

Sorting `Path` objects compares `PurePath._str_normcase`, which is case-**sensitive** on POSIX and case-**folded** on Windows. So the same `pending/` folder yields a different claim order per OS. Verified:

| order | result |
| --- | --- |
| POSIX `sorted(Path)` (today) | `P9-08`, `p10-01`, `p9-07`, `p9-10-01`, `p9-9-x` |
| Windows `sorted(PureWindowsPath)` (today) | `p10-01`, `p9-07`, `P9-08`, `p9-10-01`, `p9-9-x` |
| natural + explicit casefold (proposed) | `p9-07`, `P9-08`, `p9-9-x`, `p9-10-01`, `p10-01` |

This violates the mandatory cross-platform invariant in [AGENTS.md](../../../AGENTS.md) — the scheduler's decision must not depend on the host OS — and it is invisible today because the validation runs are all macOS.

### 3. `worc list --pending` does not use the ranking at all

`scan_pending_sorted`'s docstring states it is shared "so the displayed order can never drift", and [`worc top`](../../../src/wastech_orchestrator/cli.py#L3314-L3321) does use it. But [`_list_sections`](../../../src/wastech_orchestrator/cli.py#L3536-L3539) builds the pending section straight from `select_pending`, so `worc list` shows **raw filename order with no `priority_rank` and no queue filter**. A `priority: high` task therefore appears mid-list in `worc list` and runs first — and a task belonging to another instance's `queue` is listed as if this instance would run it. So the drift the docstring rules out already exists inside the CLI, independent of defects 1–2.

## What to build

### 1. One natural, platform-stable ordering key

Replace the `path` tie-break with an explicit key used by every ordering consumer:

- **Natural**: split the filename into digit and non-digit runs; digit runs compare numerically. Compare a digit run as `(len(digits_without_leading_zeros), digits_without_leading_zeros)` rather than `int(...)` so a pathological all-digits filename cannot cost unbounded work, and `007` still equals `7` in magnitude while sorting stably.
- **Platform-stable**: casefold explicitly in the key instead of relying on `Path.__lt__`, so POSIX and Windows agree. Never sort `Path` objects for a scheduling decision again.
- **Total and deterministic**: append the raw filename as the final tie-break so distinct names never compare equal (e.g. `p9-07` vs `p9-7`, or two names differing only in case). The order must be a strict total order over the folder — the queue cannot depend on `iterdir()` yield order.

Put it beside `select_pending` in `cli.py` (or in `task/model.py` next to `priority_rank` if it is wanted outside the CLI) — one function, one docstring stating that this is the operator-visible order.

### 2. Route every consumer through it

`scan_pending_sorted` and `select_pending` obviously; and **fix defect 3** by having `worc list`'s pending section go through `scan_pending_sorted` (priority-ranked, queue-filtered) rather than `select_pending`. `worc list` and `worc top` must print the same sequence as `watch` claims. Check the other `select_pending` callers while there: [`find_task_file`](../../../src/wastech_orchestrator/cli.py#L1284-L1289) (order only matters for an ambiguous match) and the `promote --all` subtask/root walk at [`cli.py:1374-1376`](../../../src/wastech_orchestrator/cli.py#L1374-L1376), where `NN-<slug>.md` subtask specs have exactly the same `10` vs `9` problem.

### 3. Make the order legible to the operator

The root cause of the confusion is that the operator infers order from a tool that does not know the rules (priority, queue, `depends_on`). Even with natural sort, priority and queue can still reorder the queue away from the alphabetical listing. So state the effective order where the operator looks: `worc list --pending` should show the rank position and the `priority`/`queue` it sorted on (`worc top` already shows priority and queue per row), and the docs should say plainly: **read the order from `worc list`/`worc top`, not from the file manager.**

## Non-goals

- **`priority` semantics are unchanged** — `high → mid → low` stays the primary key and the only intentional lever. This task changes only the tie-break _within_ a priority.
- **`depends_on` precedence is unchanged** — a WAITING higher-priority task is still skipped so an eligible lower-priority task runs ([operations.md](../../operations.md) wait-forever policy).
- **Not a concurrency change** — the single-active-task invariant is untouched.
- **No new config key.** A `sort: natural|bytewise` switch would preserve exactly the confusion being removed; natural is simply the correct default. (Zero-padding task filenames is a workaround an operator can already use, not a reason to keep bytewise.)

## Acceptance criteria

- [ ] With `p9-07-…`, `P9-08-…`, `p9-9-…`, `p9-10-01-…`, `p10-01-…` pending at equal priority, `scan_pending_sorted` returns them in that order, and `watch_once` claims `p9-07` first.
- [ ] The same folder produces the **identical** order under POSIX and Windows path semantics (test both flavours explicitly — no `sorted(Path)` in any scheduling path).
- [ ] The ordering key is a strict total order: distinct filenames never compare equal, and the result is independent of `iterdir()` yield order (assert against a shuffled input).
- [ ] `priority` still dominates the filename order, and `depends_on` still dominates `priority`.
- [ ] `worc list --pending`, `worc top`, the console `ps` view, and `watch`'s actual claim order are the same sequence for the same folder — including when priorities differ and when another instance's `queue` is present.
- [ ] `promote --all` processes `NN-<slug>.md` subtask specs in natural order (`9-…` before `10-…`).
- [ ] Leading zeros do not create a distinct rank (`p9-07` and `p9-7` sort adjacently by magnitude, with a deterministic tie-break between them).

## Docs to update in the same change

- [docs/task-authoring.md:68](../../task-authoring.md), [:305](../../task-authoring.md) — both say "ties broken by filename"; state that it is **natural (numeric-aware) filename order**, and that priority/queue/`depends_on` can still move a task away from the alphabetical listing.
- [src/wastech_orchestrator/packaged/guide/README.md:71](../../../src/wastech_orchestrator/packaged/guide/README.md) — the shipped operator copy of the same table.
- [docs/operations.md:283](../../operations.md) — `worc list`: it now shows the ranked, queue-filtered order; add the "read the order from `worc list`/`worc top`, not from your file manager" note.
- `scan_pending_sorted` / `select_pending` docstrings — the "already filename-sorted" claim becomes wrong once the key moves.

## Likely area

[`cli.py`](../../../src/wastech_orchestrator/cli.py): the new ordering key, `select_pending`, `scan_pending_sorted`, `_list_sections`, and the `promote --all` walk. Tests: [`tests/test_cli_top.py`](../../../tests/test_cli_top.py) (`test_scan_pending_sorted_orders_by_priority_then_filename` pins the current bytewise expectation and must be rewritten), plus `tests/test_cli_promote.py` and the `watch_once` ordering tests in `tests/core/test_cli_pipeline.py`.

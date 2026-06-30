# Task priority field

Status: **accepted (implemented)** (2026-06-26) Date: 2026-06-26 Owner: Vladimir Makarevich

The orchestrator currently picks pending tasks in deterministic lexicographic filename order. This ADR adds an optional `priority` field to task files so operators can mark tasks as `low`, `mid`, or `high` and have the scheduler run the most important ones first.

## The problem

When a `tasks/` folder contains several pending tasks, the orchestrator processes them in alphabetical filename order ([cli.py `select_pending()`](../../../../src/wastech_orchestrator/cli.py)). There is no way to promote a hot-fix or critical feature to the front of the queue without renaming files.

## Constraints

- **Single-active-task invariant** — the scheduler runs one task at a time; priority sorting is a re-ordering of the _eligibility queue_, not a concurrency change.
- **`depends_on` is always stronger than priority** — a task becomes eligible only after its dependencies are satisfied; priority determines which of several eligible tasks goes first.
- **Fail-closed parsing** — an unrecognised or missing `priority` value must not prevent a task from running; the field is optional with a safe default.
- No new status or state-db schema bump required for this change.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Positional order (filename prefix `01-`, `02-`) | Already supported implicitly; doesn't give semantic meaning and requires file renaming, which disrupts git history. |
| Numeric 0–9 | Too many levels; semantics of "5 vs 6" are ambiguous. |
| Numeric 1–3 | Fewer levels but string labels are self-documenting and already used in issue trackers. |
| `depends_on` priority propagation | Adds implicit coupling across task files; operator intent becomes hard to trace. Deferred explicitly. |
| Anti-starvation / round-robin | Out of scope for this ADR; simple descending sort is sufficient until a worktree-concurrency model lands. |
| Dynamic re-prioritization via CLI | Out of scope; requires a running-task mutation API. |

## Decision

Add an optional `priority: low | mid | high` field to task files. Default when absent: `mid`. The scheduler sorts ELIGIBLE tasks by priority descending (`high → mid → low`), breaking ties with the existing lexicographic filename order. `depends_on` resolution happens before sorting — only eligible tasks are ranked.

Cost of not choosing this: the only alternative to land priority control without a schema change is positional filenames, which creates unnecessary git churn and carries no semantic meaning.

## Open questions

None — all scope boundaries confirmed by the author.

## Implementation notes

The change is concentrated in three places:

1. **Task schema** — add `priority: Literal["low", "mid", "high"] = "mid"` to the Pydantic task model (wherever `id`, `title`, `auto_merge` are defined). Fail-closed: accept and strip unknown strings, falling back to `"mid"`.

2. **`_scan_depends_on()`** in [cli.py](../../../../src/wastech_orchestrator/cli.py) — also extract `priority` from the lightweight scan pass (same YAML/JSON parse used today).

3. **`select_pending()` / `watch_once()`** in [cli.py](../../../../src/wastech_orchestrator/cli.py) — after `dependency_eligibility()` classifies tasks as ELIGIBLE/WAITING/BROKEN, sort the ELIGIBLE slice by `(priority_rank, filename)` before picking the first candidate. Priority rank: `high=0`, `mid=1`, `low=2` (lower = runs first).

No new config keys, no state-db change, no CLI flag needed for the basic feature. The `worc list` command (if/when built) should display priority alongside status.

## As built (2026-06-26)

The shared `TaskPriority` literal, `DEFAULT_PRIORITY` (`mid`), `normalize_priority()`, and `priority_rank()` live in [model.py](../../../../src/wastech_orchestrator/task/model.py) (the one source of truth the gate, parser, and scheduler share); `priority` is in `ALLOWED_TASK_KEYS`. The gate populates `NormalizedTask.priority` via `normalize_priority`; the parser round-trips it (legacy manifests → `mid`). The lightweight scan `_scan_depends_on` was renamed to **`_scan_pending_meta`** and now returns a `_PendingScan` named tuple (`task_id`, `depends_on`, `priority_rank`). `watch_once` sorts the whole scanned list by `(priority_rank, filename)` and keeps the existing skip-WAITING / reject-BROKEN loop — equivalent to "sort the eligible slice", and a minimal change.

Two small deltas from the notes above:

- **Fail-open, not reject.** The constraint "an unrecognised or missing `priority` must not prevent a task from running" is honored literally: an unknown string _or_ a wrong type folds to `mid` (no `INVALID_FIELD_TYPE`). This is the one deliberate exception to the otherwise fail-closed field policy.
- **Default is a concrete `mid`** on the model (not `None`/tri-state) — there is no config key to defer to.

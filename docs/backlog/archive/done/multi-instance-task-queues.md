# Task queue tags for multiple worc instances

Status: **implemented** (2026-06-26) Date: 2026-06-26 Owner: Vladimir Makarevich

Give each task an optional `queue` tag and each orchestrator instance a matching selector, so several worc instances can share one git-distributed task pool without two of them grabbing the same task. Static partitioning by string equality — no dynamic claiming, no auto-balancing, no cross-process coordination. This is the simplest mechanism that satisfies the actual need ("don't collide") and matches the original instinct ("add some kind of tag to tasks").

## The problem

The single processing slot is enforced **in-process only**: `acquire_slot()` queries the instance's own `.worc/state.db` for active tasks (`core/orchestrator.py`), and the `orchestrator.pid` file prevents only a duplicate `watch` daemon on the same artifact root (`process_control.py`). Neither sees across instances. When several worc instances run as separate clones on one machine and the task pool is distributed through git — tasks committed and pushed to `base_branch`, each clone's `watch` loop does fetch/ff-pull and sees the same files in `tasks/pending` — both clones independently treat a given task as pending and run it. The result is duplicated work: two agent runs, two branches, two PRs for one task. There is no inter-instance mutual exclusion today.

## Constraints

- **The single-active-task invariant stays per-instance.** This design does not relax it; each instance still runs one task at a time and reconciles its own `state.db`. (Relaxing the slot to N is the separate, deferred [worktree concurrency](archive/concurrent-task-worktrees.md) record — a different problem: one instance running many tasks.)
- **No shared `state.db`.** Each clone owns its database; the only channel guaranteed visible to all instances is the git remote itself. The selector must therefore be derivable without any shared runtime state — which static tagging satisfies (the tag travels in the task file through git; the selector is local config).
- **Richer task parsing must stay fail-closed** (backlog README). A malformed `queue` value rejects the task rather than defaulting silently.
- **No new task status, no schema migration.** Greenfield MVP — the change is a parsed field plus a selection filter, not a state-machine change.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Do nothing — one worc per pool | Documents the limitation but leaves the real collision unsolved once the operator runs more than one instance. |
| Dynamic claim/lease via git ref | True work-queue with auto-balancing, but real distributed-systems cost: push races on claim, stale claims after a crash, push latency on every tick. Over-built for "don't collide" — no balancing was asked for. |
| Local filesystem lock (shared claim dir) | Atomic `mkdir`/`O_EXCL` claim dir on the machine is simple, but it is machine-local (won't extend past one host) and lives outside git — a hidden side-channel next to the git-distributed pool. Rejected for a smaller, in-task mechanism. |
| Static `queue` tag + per-instance selector | **Chosen.** Zero coordination, zero races, no shared state. Cost is manual assignment and an operator-enforced invariant (below). |

## Decision

Each task carries an optional `queue` string in its front-matter; each instance has a selector (`orchestrator.queue` in its `config.yaml`, overridable per launch with `worc watch --queue <name>`). An instance only picks a pending task when `task.queue == instance.queue` — plain string equality. Both sides default to `default`, so a pool with no tags and one instance with no selector behaves exactly as today; an untagged task lands in `default` and is taken only by a `default` instance. The cost of the rejected alternatives is what justifies this: dynamic claiming buys auto-balancing nobody asked for at the price of git-race and stale-claim handling, and a local lock buys nothing over an in-task tag while being machine-local and off to the side of git.

The mechanism partitions; it does not arbitrate. **Two instances with the same selector on the same pool still collide** — "one worc per queue" is an operator-enforced invariant, not something the code guarantees. That is the accepted boundary of a static-partition design.

## Open questions — resolved

- **Same-selector guard.** _Resolved: no guard._ "One worc per queue" stays an operator-enforced invariant; the code does not re-introduce a machine-local FS side-channel the decision otherwise avoids.
- **Multi-queue selectors.** _Deferred._ Kept to single-string equality (`--queue <name>`, one task `queue`); serving several queues per instance and targeting a set are additive extensions if the need appears.
- **Cross-queue `depends_on`.** _Resolved: document only._ Out-of-queue tasks are invisible to an instance, so the existing merge-gated scheduling already handles it — a dependent in `alpha` whose dependency is in `beta` stays WAITING until `beta`'s task is merged; if no instance serves `beta` it waits indefinitely (operator responsibility). No detection added.
- **Decomposition inheritance.** _Resolved: implicit._ Subtasks run inside the parent's pipeline on the parent's branch and never pass through `watch_once`'s pending-file selection, so the parent's `queue` governs them. There is no per-subtask `queue` field. Documented in `docs/task-authoring.md`.
- **`worc install` prompt.** _Resolved: no prompt._ The install template seeds `orchestrator.queue: "default"`; operators set a queue only when they actually run multiple instances.
- **`worc list`.** _Deferred._ There is no `worc list` command today; showing/filtering tasks by queue is recorded as a follow-up rather than built now.

## Implementation notes

Pointers, not a spec:

- **Task parsing**: add `queue` (default `default`, non-empty string, fail-closed on malformed) to the task model, and extend the lightweight front-matter scan (`_scan_pending_meta`, `cli.py`) to also read `queue` so eligibility stays a cheap read.
- **Selection filter**: in `select_pending()` / `watch_once()` (`cli.py`), drop pending tasks whose `queue` does not equal the instance selector. This is the whole behavioral change in the loop.
- **Config**: new `orchestrator.queue` key (default `default`) — loader, validation, config-schema version bump, and the install templates / `config_writer`.
- **CLI**: `--queue` flag on `worc watch` (and consider `worc run`) overriding the config value.
- **`worc list`**: optionally show each task's queue and/or filter by it — read-only, nice-to-have.
- **Docs**: `docs/configuration.md` (the new key) and `docs/task-authoring.md` (the new field) in the same change.

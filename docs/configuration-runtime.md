# Configuration: Runtime and Repository

Part of the [configuration reference](configuration.md): the `config.yaml` blocks that describe the outer loop and its surroundings — `orchestrator`, `repo`, `paths`, `telegram`, `logging`, and `memory`.

## `orchestrator`

Controls the outer queue behavior.

```yaml
orchestrator:
  auto_mode:
    enabled: false
    confirm_next_task: false
    confirm_timeout_s: 900
  poll_interval_seconds: 300
  queue: "default"
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `auto_mode.enabled` | boolean | `false` | When `true`, `watch` can pick another pending task after terminal cleanup succeeds. |
| `auto_mode.confirm_next_task` | boolean | `false` | When `true`, `watch` sends a Telegram approve/deny prompt before claiming each pending task. Approve → claim and run it; deny / timeout / no transport → leave it pending and stop chaining for this cycle (fail-closed STOP). Gates **new claims only** — resuming an in-flight task on daemon restart is never gated. Requires `telegram.enabled` (preflight rejects the gate otherwise). Non-durable: a daemon restart mid-prompt simply re-asks next tick. |
| `auto_mode.confirm_timeout_s` | integer `> 0` | `900` | How long that gate waits for the answer. Deliberately its own key rather than [`telegram.ask_timeout_s`](#telegram): the HITL ceiling bounds a node asking a human mid-run, where the alternative to waiting is parking a half-finished task; the claim gate only holds an idle slot and gets another chance on the next tick, so hours buy nothing. `0` is rejected at validation rather than read as "off" (it would fail closed on every tick) — off is `confirm_next_task: false`. |
| `poll_interval_seconds` | integer `>= 0` | `300` | `watch` loop interval: each tick runs `git fetch` + `pull --ff-only` on `base_branch` to discover git-pushed tasks, then re-scans. `0` makes `watch` a single pass (no loop, no periodic sync). `--poll-seconds` overrides it. |
| `queue` | non-empty string | `"default"` | This instance's queue selector. When several worc instances share one git-distributed task pool, `watch` only picks a pending task whose front-matter `queue` equals this value (plain string equality — static partitioning, no balancing). Both sides default to `"default"`, so a single untagged instance behaves exactly as before; an untagged task lands in `"default"` and is taken only by a `"default"` instance. `--queue` overrides it. |

A refusal is remembered. Deny, silence and "no transport" are one answer to `watch`, and the task that drew it is left alone for **one hour** instead of being re-prompted every `poll_interval_seconds` — three ticks used to mean three prompts. The memory lives in the `watch` loop and is not persisted: a restart is the operator's own "ask me again". A remembered refusal still ends that cycle rather than falling through to the next pending task, and the skip is logged at `debug`, so such a tick's summary reads "nothing to do". `stop` reaches a gate that is still waiting — the prompt is abandoned and the daemon shuts down cooperatively instead of being escalated to a kill (see [telegram.md](telegram.md)).

Auto mode does not enable concurrency. The v1 contract keeps one active task at a time. The `queue` selector partitions the pool across instances; it does not arbitrate — two instances with the same selector on the same pool still collide, so "one worc per queue" is an operator-enforced invariant. A task in queue A that `depends_on` a task in queue B simply waits until B's task is merged; if no instance serves B it waits indefinitely (operator responsibility).

### Runtime observability options

Logging and heartbeat settings are global CLI options. `--log-level` is the only one that also has a persisted config key ([`logging.level`](#logging)); the rest are CLI-only:

| Option | Default | Meaning |
| --- | --- | --- |
| `--log-level {debug,info,warning,error}` | `logging.level`, else `info` | Minimum operator log level; **overrides** the persisted `logging.level`. |
| `--log-format {logfmt,json}` | `logfmt` | Format used for stderr and `--log-file`. |
| `--log-file PATH` | unset | Also write a rotating 10 MB operator log with five backups. |
| `--heartbeat-seconds N` | `30` | Progress interval for long provider/check/Git calls and the HITL human-input wait; `0` disables. |

The `watch` subcommand also accepts `--poll-seconds N` and `--queue NAME` (placed after `watch`), which override `orchestrator.poll_interval_seconds` and `orchestrator.queue` for that run. `restart` accepts the same two flags for the fresh loop it starts.

The global `--env-file PATH` loads environment variables from a file before any command runs. With no flag, the orchestrator auto-loads the `.env` beside the resolved `config.yaml` (`<repo-root>/.worc/.env`) when present. Loading never overrides an already-exported variable (the real environment wins) and only populates the orchestrator's own process (a value reaches a child only if its name is in `security.allowed_environment`). Loading itself is silent — the `.env` status (a secret-free `count` + `path`) is reported by `preflight` as a health line, not echoed on every command. A missing explicit `--env-file` fails closed (exit 2); a missing auto-discovered `.worc/.env` is a silent no-op. See [operations → Authorization](operations-install.md#2-authorization-configured-outside-the-orchestrator).

Place global options before the subcommand:

```bash
python -m wastech_orchestrator \
  --config ./config.yaml \
  --log-file ./logs/orchestrator.jsonl \
  --log-format json \
  --heartbeat-seconds 30 \
  watch
```

Use `python -m wastech_orchestrator --config ./config.yaml status [task-id]` for a read-only snapshot from the persisted state database.

## `repo`

Describes the target repository clone that agents edit and the Git Manager publishes.

```yaml
repo:
  url: "git@github.com:OWNER/REPO.git"
  local_path: "./workspace/repo"
  base_branch: "main"
  branch_prefix: "worc"
  branch_mode: "new"
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `url` | string | `""` | Remote repository URL. |
| `local_path` | string | `"./workspace/repo"` | Dedicated clone/worktree used for agent runs. |
| `base_branch` | string | `"main"` | Branch checked out before task branches and, by default, after terminal cleanup (see `checkout_base_on_cleanup`). |
| `branch_prefix` | string | `"worc"` | Prefix for default task branches: `worc/<epoch>-<task-id>-<slug>` (`<epoch>` is the unix timestamp at branch-prep time, so a re-run never collides). The full auto-generated name is capped at 50 chars — the slug is truncated to fit, or dropped entirely if the prefix already fills the budget. A task-level `branch_name` overrides the full name; an override longer than 50 chars logs a warning and falls back to the auto-generated name. |
| `branch_mode` | `new` \| `existing` \| `current` | `"new"` | Instance default for **where task git operations point** (added in `schema_version` 26). `new` creates a fresh task branch from `base_branch` (today's behavior). `existing` works in a named, already-existing branch (the task supplies `branch_ref`). `current` works in whatever branch the working tree is on — no create, switch, pull, or clean-tree requirement. A per-task `branch_mode` overrides this. A branch is **orchestrator-owned only in `new`** — destructive git ops (reset-to-base, force-checkout-away, branch delete) run only there, and a fresh `rerun` in `existing`/`current` is refused (use `rerun --continue`). See [task authoring](task-authoring.md#branch_mode). |
| `checkout_base_on_cleanup` | `bool` \| `null` | `null` | Whether terminal cleanup returns the working tree to `base_branch` after a terminal outcome (added in `schema_version` 30). `null` (default) defers to `branch_mode`: `new` returns to base, `existing` and `current` stay on the branch. `false` never returns (a global off switch, including `new`); `true` forces `new` and `existing` to return. `current` always stays regardless (the operator owns its tree). Instance-only — no per-task override. Set `false` (or use `existing`/`current`) when every task runs on one shared branch and the switch-back is pure noise. |

Git credentials are not stored in this file. Configure SSH, a credential helper, or `gh auth login` outside the orchestrator.

## `paths`

Where the task lifecycle lives. Optional — omit the block to take the default.

```yaml
paths:
  tasks_dir: "tasks"
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tasks_dir` | string | `"tasks"` | Repo-relative directory holding the `preparing` / `pending` / `done` / `failed` lifecycle subfolders. Rename it to avoid clashing with a repo that already uses `tasks/` for something else. Gitignored by default — see [How-To §6](how-to.md#6-track-your-task-files-tasks-in-git). |

`preparing/` is the staging area the `watch` scanner never looks in: compose a task there at leisure, then `worc promote [ID_OR_FILE]` (or `worc promote --all`) moves it into `pending/` atomically, so a half-written file is never claimed mid-edit. A decomposition root pulls its subtask specs along with it.

The value is validated as repo-relative: no absolute path, no `~`, no `..` traversal, and it must **not** live under the `.worc/` home (the agent's read-deny root, excluded from every commit by path, and already home to the `tasks/rejected` quarantine). A repo-relative subpath (e.g. `config/tasks`) is allowed. The lifecycle subfolder names themselves are fixed.

`worc install --tasks-dir DIR` scaffolds this directory, writes it here, and seeds its ignore line, all from the one value (an unsafe value is refused by the config validator). Whether the line is seeded is the wizard's `Track task files in git?` question (default no; `--track-tasks` / `--no-track-tasks`); no config key records the answer — git's ignore state is what the runtime reads (see [How-To → Track your task files in git](how-to.md#6-track-your-task-files-tasks-in-git)). Rename it **afterwards** and you do those last two steps yourself: create the lifecycle subfolders (the orchestrator does not auto-create a renamed root) and move the `/tasks/` line in `.gitignore`, because the orchestrator asks git about whatever directory this key names. While that directory is gitignored — `install`'s default — the audit commit is a no-op and lifecycle moves simply do not appear in Git history; no extra config either way.

## `telegram`

Optional terminal notifications and blocking human-in-the-loop for `refinement`, `planning`, and dangerous-diff guardrails.

```yaml
telegram:
  enabled: false
  bot_token_env: "TELEGRAM_BOT_TOKEN"
  chat_id_env: "TELEGRAM_CHAT_ID"
  ask_timeout_s: 28800
  trace: false
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | `false` | Enables Telegram transport. Blocking HITL fails closed if credentials/transport are unavailable. |
| `bot_token_env` | string | `"TELEGRAM_BOT_TOKEN"` | Valid environment-variable name containing the bot token. |
| `chat_id_env` | string | `"TELEGRAM_CHAT_ID"` | Valid environment-variable name containing the numeric target chat id. |
| `ask_timeout_s` | integer | `28800` | Maximum wait for a human reply; must be greater than zero. |
| `trace` | boolean | `false` | Live per-node progress feed (added in `schema_version` 21). When on, the orchestrator pushes one best-effort message per executed flow node finish — `<emoji> <node-id> → <outcome>` (e.g. `✅ implementation → done`, `🔁 review → rework`, `❌ testing → fail`), carrying only the node id + outcome (no diff/prompt/agent text). A non-blocking evaluator that accepted only because its `max_rework_per_stage` budget ran out traces as `⚠️ <node-id> → accept (rework budget exhausted)` (the same event is always logged as a console warning, independent of this flag). Fire-and-forget: a send failure never touches the pipeline, and it is a no-op when Telegram is disabled. |

The config stores environment variable **names only**. Token and chat-id values are resolved from the orchestrator process environment at startup and are never written to config, SQLite, logs, or artifacts. Terminal delivery is best-effort and never changes a completed task outcome. A blocking question/approval is fail-closed: disabled/unconfigured transport, timeout, transport failure, or an ambiguous approval moves the task to `manual_action_required`.

Preflight requires a non-zero numeric chat id, bot access to the chat, no configured webhook, and a working `getUpdates` polling API. Use one bot/chat with one orchestrator poller. See [telegram.md](telegram.md) for setup and `telegram-test`.

## `logging`

Optional block persisting operator log verbosity and on-disk artifact retention. Omit it to take the defaults.

```yaml
logging:
  level: warning # debug | info | warning | error
  artifacts: standard # minimal | standard | full
  clean_runs_on_success: true # a successful task evicts its own .worc/runs/ subtree
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `level` | enum | `warning` | Minimum operator trace level. The `--log-level` CLI flag overrides it when given; precedence is `--log-level` > `logging.level` > `warning`. `warning` is the shipped posture, so an omitted `logging` block is already the quiet level — set `info` for the per-stage play-by-play. |
| `artifacts` | enum | `standard` | Which per-attempt provider files survive under `logs/<task-id>/stages/.../<attempt>-<provider>/`. |
| `clean_runs_on_success` | boolean | `true` | A task that finishes **successfully** evicts its own per-task state under `.worc/runs/` — the frozen control + instruction bundles and its sealed exchanges. Failed / parked / `manual_action_required` tasks are never cleaned automatically, and quarantined exchange evidence never is at all. Set `false` to keep every run for analysis and reclaim on demand with `worc runs clean` (available either way). Per-task **log** dirs are out of scope — those stay with `worc logs clean`. |

The `artifacts` level prunes each attempt directory at the end of a run (after the stream has been parsed and `result.json` written, so it is always safe — the orchestrator's authoritative state lives in `state.db`, not the artifacts):

| Level | Files kept |
| --- | --- |
| `minimal` | `result.json` only — even on failure (it records the exit code + normalized error class). |
| `standard` | `result.json`, `stdout.log`, `stderr.log`. (`events.jsonl` is a redacted copy of `stdout.log`, so nothing unique is lost; `request.json` is dropped.) |
| `full` | everything: `request.json`, `stdout.log`, `stderr.log`, `events.jsonl`, `output-schema.json`, `result.json`. |

`minimal` makes remote post-mortem debugging harder (no stdout/stderr from failed runs); use it only on well-understood, frequently-run pipelines. This level governs **only** the per-attempt provider files inside each `<attempt>-<provider>/` dir. Prompt-audit is **independent** of it (governed by [`prompt_audit`](configuration-flows-supervisor.md#prompt_audit)), and so is the **per-run operator-facing history** — the rendered prompt, review findings/summary, generic `<node-id>.out.md`, checks reports, and tool streams written at the `stages/<node-id>/run-<node-run-id>/` level (above the attempt dir), plus the once-only task-level slots (`plan.md`, `summary.md`, `current.diff`). None of these is ever pruned by `artifacts`; the only thing that removes them is an explicit [`worc logs clean`](operations.md) of the whole task tree. Reclaim disk from accumulated task directories that way.

## `memory`

> **Experimental — not stable.** The subsystem runs, but its store is unaudited and carries no redaction guarantee, and its curation quality is still being reworked. The block's shape, defaults and knobs can change **without a migration path**, so leave it off (the shipped default) unless you are deliberately experimenting.

Optional block configuring the persistent, repo-scoped memory subsystem. Omit the whole block (or set `enabled: false`) for the pre-memory behavior exactly: no store is written, no candidate delta is produced, memory packets are empty, `worc memory` is a no-op, and no background cleanup runs. A fresh `worc install` writes **no `memory` block at all** — the absence _is_ off.

> **Memory also requires [`supervisor.enabled: true`](configuration-flows-supervisor.md#supervisor).** That layer's closing turn is the only path that writes anything memory can later read back, so `supervisor.enabled: false` resolves `memory.enabled` to `false` for the run and prints a warning naming both keys. Set `memory.enabled: false` yourself to make the file say what runs.

```yaml
memory:
  enabled: false # SHIPPED DEFAULT — turn it on only to experiment
  short_term_ttl_days: 30
  packet_max_lines: 120
  packet_max_long_term: 3
  packet_max_entity: 5
  packet_max_episodic: 3
  promote_min_tasks: 2
  promote_window_days: 60
  cleanup_min_interval_s: 300
  cleanup_max_scanned: 200
  cleanup_max_edits: 50
  cleanup_max_wall_clock_s: 5.0
  cleanup_promotions_per_pass: 0
```

| Field | Type | Default | Meaning |
| --- | --- | --: | --- |
| `enabled` | bool | `false` | Master switch. Absent block or `false` = no memory behavior at all, and `install` writes no `memory` block. Forced to `false` for the run when [`supervisor.enabled`](configuration-flows-supervisor.md#supervisor) is `false`. |
| `short_term_ttl_days` | int | `30` | Episodic entries expire after this many days; long-term has no TTL. |
| `packet_max_lines` | int | `120` | Hard line backstop for one per-node memory brief. |
| `packet_max_long_term` | int | `3` | Max long-term lessons in one retrieval packet. |
| `packet_max_entity` | int | `5` | Max entity cards in one packet. |
| `packet_max_episodic` | int | `3` | **Inert since V2** — the episodic tier is write-only and never injected into a packet; kept only as the absent-block default. |
| `promote_min_tasks` | int | `2` | Recurrence gate for **`artifact-backed`** lessons only; `repo-observed` / `human-curated` / `review-verified` lessons promote on first sight. A gated lesson must have recurred in ≥ this many tasks ... |
| `promote_window_days` | int | `60` | ... within this many days to be eligible for long-term promotion. |
| `cleanup_min_interval_s` | int | `300` | Minimum seconds between background-cleanup passes. |
| `cleanup_max_scanned` | int | `200` | Max records examined per cleanup pass. |
| `cleanup_max_edits` | int | `50` | Max records changed per cleanup pass. |
| `cleanup_max_wall_clock_s` | float | `5.0` | Per-pass wall-clock ceiling, in seconds. |
| `cleanup_promotions_per_pass` | int | `0` | Promotions a cleanup pass may make; `0` = cleanup never creates a long-term lesson (it only demotes / expires / quarantines / merges). |

The subsystem was built phase by phase and refined by a durable-concepts V2 design pass. When enabled, the **write path** is active: memory is written once per task at finalization — the supervisor proposes a candidate delta on its existing summary turn (zero extra LLM calls), and the deterministic `MemoryService` redacts, validates, assigns trust, merges, and promotes-or-quarantines it; a terminal failure writes a deterministic short-term record (never long-term). Repo-verified / human / review lessons promote on first sight; `artifact-backed` lessons still wait for recurrence (`promote_min_tasks`). The **read path** is also active: before a node runs, a deterministic, model-free `PacketBuilder` assembles a small per-node retrieval packet of durable **lessons and entity cards only** — the episodic tier is a write-only shell and is never injected, and rotting evidence pointers (commit SHAs, task ids, log-dir paths) are filtered out of what the agent sees — within the `packet_max_*` caps, written under the gitignored `logs/<task-id>/memory/<node>.md`; the node prompt receives only that packet's path via the `{memory_path}` variable, never the store root. The packet is **node-driven**: it is built for any node whose role prompt references `{memory_path}` (the packaged `planning` / `implementation` / `review` / `fixing` prompts do by default, and a custom node opts in with no code change), and a node with no relevant memory gets no packet (the variable renders empty). The **curation path** is also active: a bounded, model-free `CleanupJob` runs in the `watch` daemon's idle gap (only when no task is active, rate-limited by `cleanup_min_interval_s`, within the `cleanup_max_*` budget) and expires episodes past their TTL, remaps moved entity files and lessons (same basename — rewriting the key so a refactor does not lose durable knowledge) or quarantines truly stale ones, and merges duplicate lessons — it **never** creates a long-term lesson and **never** edits code/docs (it snapshots first, so a bad pass is reversible). Operators inspect and repair the store with [`worc memory`](operations-logs.md#managing-the-memory-store-worc-memory) (`show` / `validate` / `compact` / `restore` / `clear`). The canonical store lives under the gitignored `<repo>/.worc/memory/` home and is never committed.

# Operations: Logs, State, and Notifications

Part of the [operations guide](operations.md): what a run leaves behind, how to reclaim it, and how the operator hears about a run — structured logs, `logs clean`, `runs clean`, `worc memory`, and Telegram.

## Structured logs

The pipeline emits a secret-free **logfmt** trace on stderr (keyed by `task_id` / `stage` / `attempt` / `provider`): operation start/end/failure and duration, route source, fallback/skip/decompose decisions, fix-loop counters, and the terminal outcome. Secrets are never logged (and a redaction filter scrubs records as a safety net).

```text
ts=… level=info task_id=task-001 stage=implementation primary=codex msg="stage started"
ts=… level=info task_id=task-001 stage=implementation provider=codex attempt=1 elapsed_seconds=30.0 msg="provider heartbeat"
ts=… level=info task_id=task-001 stage=implementation primary=codex duration_seconds=84.2 msg="stage completed"
ts=… level=info task_id=task-001 stage=review msg="falling back" from=codex to=claude error_class=rate_limited
ts=… level=warn task_id=task-001 msg="task stuck" limit=max_total_fix_iterations fix_iterations=30
ts=… level=info task_id=task-001 msg=terminal final_status=done pr_url=… cleanup_safe=True
```

For a durable live trace, enable the rotating file handler and heartbeat:

```bash
python -m wastech_orchestrator \
  --log-file ./logs/orchestrator.jsonl \
  --log-format json \
  --heartbeat-seconds 30 \
  watch
```

These are global CLI options and must come before the subcommand. The file rotates at 10 MB and keeps five backups. Supported formats are `logfmt` and newline-delimited `json`. `--heartbeat-seconds 0` disables heartbeat records.

The persisted operator verbosity lives in [`logging.level`](configuration-runtime.md#logging); `--log-level` overrides it for a single run.

## Cleaning up logs (`logs clean`)

`.worc/logs/` grows indefinitely — every task leaves per-attempt artifacts, and a long-lived daemon keeps appending to its own log. `worc logs clean` **sweeps the whole `.worc/logs/` root**, not just the subdirectories: the per-task artifact dirs _and_ the daemon logs (`daemon.log`, its rotated backups, and `daemon-startup.log`). The one thing it keeps by default is the **ledger** (`completed.jsonl`, the task-state audit trail).

```bash
worc logs clean                 # sweep the whole logs root (ledger kept); confirms first
worc logs clean --keep 20       # keep the 20 most recently modified task dirs, remove the rest
worc logs clean --all           # also remove the ledger; always confirms
worc logs clean --keep 20 --all # both: keep 20 task dirs AND drop the ledger
worc logs clean --yes           # skip the confirmation prompt (for scripts/cron)
```

`--keep N` runs without a prompt (an explicit count is clear intent), except `--keep 0` (≡ delete-all), which confirms like the bare form. `--keep N --all` honors **both** flags.

Two safety refusals, each with its own message:

- **A task is active** → the command refuses outright rather than deleting artifacts a running task is still writing. Run it while `watch` is idle.
- **A watch daemon is live** → the task dirs are swept but the **daemon logs are held back**, because the daemon has them open and `top`/`shell` are tailing them. Stop the daemon first if reclaiming those is the point.

To bound the footprint going forward instead of cleaning periodically, lower the per-run artifact retention with [`logging.artifacts`](configuration-runtime.md#logging) (`minimal` / `standard` / `full`).

> `daemon-startup.log` no longer grows with the run. A console-spawned daemon drops its stderr handler once its `--log-file` is configured, so that file holds only the pre-configuration output — the real startup error `worc shell`'s `up` surfaces when a daemon dies before it can log anywhere else. The daemon's live stream is `daemon.log` (or whatever `--log-file` names).

## Reclaiming per-task runtime state (`runs clean`)

Separately from the logs, each task leaves private per-task runtime state under `.worc/runs/`: its frozen `control-bundles/` and `instruction-bundles/`, and its `exchange-seals/`. A **successful** task evicts its own subtree automatically ([`logging.clean_runs_on_success`](configuration-runtime.md#logging), default `true`); failed, parked, and `manual_action_required` tasks keep theirs so you can debug them, and `exchange-quarantine/` — tainted evidence — is never removed automatically at all.

```bash
worc runs clean                        # remove every task's bundles + seals; confirms first
worc runs clean --keep 5               # keep the 5 most recently touched tasks
worc runs clean --include-quarantine   # also drop quarantined exchange evidence
worc runs clean --yes                  # skip the confirmation prompt
```

Like `logs clean` it refuses while a task is active. `--keep N` runs without a prompt except `--keep 0`. What the orchestrator leaves in a target repository, and how each part grows, is owned by the shipped page `.worc/guide/footprint.md` — including the ledger's uncapped growth (~630 B per terminal task, the one artifact no retention setting bounds).

## Managing the memory store (`worc memory`)

When the [`memory`](configuration-runtime.md#memory) subsystem is enabled, the orchestrator keeps a persistent, repo-scoped store under the gitignored `.worc/memory/`. It is self-maintaining — a bounded background `CleanupJob` runs in the `watch` daemon's idle gap — but `worc memory` lets an operator inspect and repair it directly. With memory disabled (or the block absent) every verb is a clean no-op.

```bash
worc memory show                # tier counts, audit-chain health, snapshot count (read-only)
worc memory validate            # entity cards whose paths/symbols are gone (read-only)
worc memory compact             # run a fuller cleanup pass now (expire/remap/quarantine/merge)
worc memory compact --dry-run   # print the plan without writing anything
worc memory restore             # roll the store back to the most recent audit snapshot
worc memory restore --snapshot LABEL   # roll back to a specific snapshot
worc memory restore --dry-run   # list the files that would be restored
worc memory clear               # empty every record tier to zero (reversible: a snapshot is taken first)
worc memory clear --kind long   # clear only one tier: short | long | entity | quarantine
worc memory clear --dry-run     # show what would be cleared, write nothing
worc memory clear -y            # skip the y/N confirmation
worc memory clear --purge       # HARD reset: remove the whole .worc/memory/ store, audit log + snapshots too (irreversible)
```

`show` and `validate` are read-only. Every memory mutation writes an append-only audit row with pre/post content hashes **and a concrete human-readable rationale** — a promotion says why it qualified, a quarantine names its cause (e.g. `quarantined: non-durable trust 'agent-inferred'`, `held short-term: awaiting recurrence (1/2 tasks)`) — so `show`/`validate` explain _why_ a well-evidenced candidate was held, not just that it was. The mutating verbs (`compact`, `restore`, `clear`) **refuse while a task is active** (run them when the orchestrator is idle) and support `--dry-run` to preview the plan first. `compact` is the foreground counterpart of the idle cleanup: it lifts the scan/edit/wall-clock budget but keeps every safety rule — it never creates a long-term lesson, never edits code, snapshots before mutating, and quarantines (never silently deletes) stale entries. A bad pass is reversible with `restore`, which rewinds the tier files from an `audit/snapshots/` snapshot (the append-only audit log itself is never rewound). Hand-editing the plain `*.jsonl` / `*.md` files under `.worc/memory/` stays a supported path; the curation verbs are the audited, redaction-safe alternative.

`clear` empties the store to zero when you want a fresh start. By default it clears the record tiers through the same audited seam (one `prune` row per file) after snapshotting first, so `clear` is reversible with `restore` and leaves the audit chain intact; `--kind` narrows it to a single tier (`short`, `long`, `entity`, or `quarantine`) and the default is all four. For a true teardown, `--purge` removes the entire `.worc/memory/` directory — the audit log and snapshots included — which is **irreversible** and therefore prompts for a typed `YES` (the store is re-seeded lazily on next use). `--kind` and `--purge` are mutually exclusive; both the content clear and the purge take `-y/--yes` to skip the prompt.

## Telegram HITL and notifications

Set `telegram.enabled: true`, then export the variables named by `telegram.bot_token_env` and `telegram.chat_id_env`. The values themselves must not be placed in `config.yaml`. Use a dedicated project bot/chat and only one long-poll consumer; webhook mode is incompatible.

After the completed-task ledger record is written, the orchestrator sends one best-effort message for `done`, `failed`, or `manual_action_required`. Every message leads with a severity glyph so the needs-attention cases stand out at a glance in the chat (`✅` done, `🛑` manual_action_required, `❌` failed). A clean `done` stays a single terse line — id, status, PR URL when present. A `failed`/`manual_action_required` expands into an actionable body (VF-22): the task **title**, where it **stopped** (flow node and, when applicable, the fix loop and round count), a one-sentence **prose reason** mapped from the internal stop token (an unmapped token still prints verbatim, never dropped), the single most-severe **blocking review finding** with its paths, and the on-disk **`stuck.md`** report to open next — all assembled from the task's `state.db` row and its `failure_report.json`, degrading cleanly to the terse line when that context is absent. A Telegram or network failure is logged with credentials redacted and does not change the already-determined terminal outcome. The message is plain text (no `parse_mode`), redacted, and bounded to Telegram's 4096-char limit. Task `contacts` are appended as plain-text mentions.

`refinement` and `planning` may emit one typed free-form question or yes/no approval. Questions use ForceReply; approvals use inline buttons. Only the configured chat and exact prompt/callback are accepted. The answer is persisted as redacted JSON and passed to the repeated stage through `human_input_path`, never CLI argv.

After `implementation` and `fixing`, the dangerous-diff gate can require approval before tests. Which diffs raise it is set by [`security.trust_level`](configuration-agents.md#trust_level-approval-policy) (overridable per task): `strict` gates every tracked-file deletion/rename or dependency manifest/lock change; `auto` (the fresh-install default) gates none of those — only a [`security.protected_paths`](configuration-agents.md#protected_paths-always-ask-floor) match asks. An exact planning approval already covering the same risk and normalized paths skips a repeat prompt. Ordinary diffs and routine commit/push/PR do not ask.

**Every Telegram call is bounded, and a stop can reach one in flight.** These calls run inside a `watch` tick, and the loop consults its stop channels only around ticks — so a stalled send used to take the daemon's answer to `stop` with it, up to the ladder's tree kill (with `telegram.trace: true` that is one call per node). Each client call now carries a deadline, enforced in two layers: the await is cancelled, and the worker thread is abandoned when that does not return. The one exception is the HITL long poll, which carries the operator's own hours-long `ask_timeout_s` — clipping it would break the human gate — but its individual long-poll requests are capped at 5 s so a stop is consulted between them. And the claim gate is interruptible: a `stop` while [`auto_mode.confirm_next_task`](configuration-runtime.md#orchestrator) is waiting withdraws the prompt and resolves the wait as `cancelled` — a class of its own, distinct from `timeout`, because the operator was never late — instead of politely waiting out `confirm_timeout_s` and being escalated to a kill.

Timeout, transport failure, ambiguous approval, or a repeated stage request moves the task to `manual_action_required`. Waiting is stored in `logs/<task-id>/hitl/*.json`; restart resumes the existing Telegram message/deadline without adding a state-machine status.

Verify setup:

```bash
worc --config ./config.yaml preflight
worc --config ./config.yaml telegram-test --timeout-seconds 60
```

Full BotFather/chat-id setup and troubleshooting: [telegram.md](telegram.md).

Monitor from another terminal:

```bash
tail -f logs/orchestrator.jsonl
python -m wastech_orchestrator --config ./config.yaml status
python -m wastech_orchestrator --config ./config.yaml status task-001
```

`status` opens the configured artifact root's `state.db` read-only. Without an id it reports active tasks, or the latest task when none is active. It does not invoke providers, checks, or Git. The displayed provider is the route's configured primary; it does not claim that a currently running subprocess has already succeeded.

---

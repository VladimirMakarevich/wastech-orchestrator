# `telegram`, `logging`, `memory`, `tools`, `prompt_audit`

**You are an operator (or an agent helping one) configuring wastech-orchestrator.** This page documents the remaining blocks: how a human is asked and notified, how loud the logs are and how long artifacts live, the optional repo-scoped memory, the custom tool-node timeout, and the prompt audit.

For the fields not on this page see [reference.md](reference.md), which also carries the cross-field rules that apply across blocks; for the how-to walkthrough see [README.md](README.md) and for safe defaults [best-practices.md](best-practices.md).

## `telegram` — human-in-the-loop and notifications

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `telegram.enabled` | bool | `false` | — | Enable HITL prompts + notifications. Required by `auto_mode.confirm_next_task` and provider `max_turns_gate`. |
| `telegram.bot_token_env` | string | `"TELEGRAM_BOT_TOKEN"` | Must be a valid env-var name (`^[A-Za-z_][A-Za-z0-9_]*$`). | The env var _name_ holding the bot token (never the token value). |
| `telegram.chat_id_env` | string | `"TELEGRAM_CHAT_ID"` | Same env-name rule; must resolve to a non-zero numeric chat id. | The env var name holding the chat id. |
| `telegram.ask_timeout_s` | int | `28800` (8h) | `> 0` | Blocking HITL timeout for a node asking a human mid-run — fails closed on timeout. Not the auto-mode claim gate's, which has its own `orchestrator.auto_mode.confirm_timeout_s` ([reference.md](reference.md)). |
| `telegram.trace` | bool | `false` | — | `true` = live per-node progress feed (best-effort; node id + outcome only). |

## `logging` — operator verbosity and artifact retention

| Field | Type / values | Default | Meaning |
| --- | --- | --- | --- |
| `logging.level` | `debug` \| `info` \| `warning` \| `error` | `warning` | Operator trace verbosity. The `--log-level` CLI flag overrides it for one run. `warning` is the shipped posture, so an omitted `logging` block is already the quiet level; set `info` for the per-stage play-by-play. |
| `logging.artifacts` | `minimal` \| `standard` \| `full` | `standard` | Per-attempt provider files kept: `minimal` = `result.json` only; `standard` = + stdout/stderr; `full` = everything. Reclaim disk with `worc logs clean`. |
| `logging.clean_runs_on_success` | boolean | `true` | A task that finishes **successfully** evicts its own per-task state under `.worc/runs/` (frozen control + instruction bundles, sealed exchanges). Failed / parked / manual-action tasks and quarantined exchange evidence are never cleaned automatically. Set `false` to keep every run for analysis and reclaim on demand with `worc runs clean` (available either way). Per-task log dirs are out of scope — those stay with `worc logs clean`. See [footprint.md](../footprint.md). |

## `memory` — persistent, repo-scoped memory

> **Experimental — not stable.** The subsystem runs, but its store is unaudited and carries no redaction guarantee, and its curation quality is still being reworked. The block's shape, defaults and knobs can change without a migration path, so leave it off (the shipped default) unless you are deliberately experimenting.

Omitting the whole block ⇒ `enabled: false` (no store, empty packets, CLI no-op). All numeric knobs are runtime-clamped — never fatal. Defaults are deliberately small (precision over recall).

Memory also requires `supervisor.enabled: true`. That layer's closing turn is the only path that writes anything memory can later read back, so with the layer off memory would keep adding a packet to every prompt without ever learning — `supervisor.enabled: false` therefore resolves `memory.enabled` to `false` for the run and prints a warning naming both keys. Set `memory.enabled: false` yourself to make the file say what runs.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `memory.enabled` | bool | `false` (`install` writes no `memory` block — the absence *is* off) | Global memory toggle. Forced to `false` for the run when `supervisor.enabled` is `false`. Set it `true` for the store, the candidate delta and the per-node packets. |
| `memory.short_term_ttl_days` | int | `30` | Episodic entries expire after N days (long-term has no TTL). |
| `memory.packet_max_lines` | int | `120` | Hard line backstop for a per-node memory brief. |
| `memory.packet_max_long_term` | int | `3` | Max long-term lessons per packet. |
| `memory.packet_max_entity` | int | `5` | Max entity cards per packet. |
| `memory.packet_max_episodic` | int | `3` | Inert since V2 (episodic tier is write-only). |
| `memory.promote_min_tasks` | int | `2` | Recurrence gate for artifact-backed lessons (repo-verified / human / review lessons promote on first sight). |
| `memory.promote_window_days` | int | `60` | Window for the recurrence gate. |
| `memory.cleanup_min_interval_s` | int | `300` | Minimum seconds between background cleanup passes. |
| `memory.cleanup_max_scanned` | int | `200` | Max records examined per pass. |
| `memory.cleanup_max_edits` | int | `50` | Max records changed per pass. |
| `memory.cleanup_max_wall_clock_s` | float | `5.0` | Per-pass wall-clock ceiling. |
| `memory.cleanup_promotions_per_pass` | int | `0` | Doc-only invariant: cleanup never promotes; non-zero is inert. |

## `tools` — custom tool-node timeout

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tools.default_timeout_seconds` | int | `3600` | Flow-wide default wall-clock timeout for a `kind: tool` node whose own `timeout_seconds` is unset (precedence: node → this → built-in 3600s). The tool feature itself is enabled per-flow (see [../flows/reference.md](../flows/reference.md)), not here. A tool that exceeds it parks the task at `manual_action_required` (not a quality fail). |

## `prompt_audit` (top-level)

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `prompt_audit` | bool | `false` | Record each step's rendered prompt + who-metadata under `logs/<task-id>/prompt-audit/`, twice over: `<NNNNNN>-<node>.md` for reading (a fenced `json` metadata header, then the prompt as plain body text) and `timeline.jsonl` for parsing (one whole record per line, prompt included). `model` / `reasoning` are the **effective** values the settled attempt ran with (a node that overrides neither still names what it ran on); `model_configured` / `reasoning_configured` beside them are the flow node's own overrides, `null` when it declares none. Each row in `agents` carries its own provider/attempt/fallback/status plus the model and reasoning **that** attempt ran at, so a stage that failed over to the other provider does not report one model for two CLIs. A per-task `prompt_audit` always overrides this. |


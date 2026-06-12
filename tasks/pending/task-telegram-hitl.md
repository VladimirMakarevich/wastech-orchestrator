---
id: task-telegram-hitl
title: "Implement Telegram human-in-the-loop and notifications"
refined: false
decompose: false
contacts:
  - "@maintainer"
---

## Description

Telegram support is currently **inert**: `TelegramConfig` is parsed (`telegram.enabled`,
`telegram.bot_token_env`, `telegram.chat_id_env`, `telegram.ask_timeout_s`) and `python-telegram-bot`
is already a dependency, but nothing sends messages or asks a human anything. Implement the Telegram
integration described in the architecture (`docs/codex_git_orchestrator_architecture.md` §4.7) and the
product backlog (`docs/backlog/product_backlog.md`): a single mechanism for **clarifying questions /
action approval** plus **terminal notifications**.

Add a small, self-contained transport module (e.g. `src/wastech_orchestrator/notify/telegram.py`)
that the Core uses through a narrow interface, so the Core never imports `python-telegram-bot`
directly and the feature can be unit-tested without network access (inject a fake sender).

Scope this change to:

1. **Notifier / transport.** Resolve the bot token and chat id **only** from the environment
   variables *named* by `telegram.bot_token_env` / `telegram.chat_id_env` (never from config values).
   When `telegram.enabled: false`, or those env vars are absent, the integration is a clean no-op
   (log a single debug line, do not raise). All sends are best-effort: a Telegram/network failure
   must never change a task's terminal outcome or crash the pipeline.

2. **`ask_human(question, context, task_id, kind, timeout)`.** The blocking HITL primitive from
   §4.7: `kind="question"` (free-form answer) or `kind="approval"` (yes/no on a dangerous action).
   It posts to the chat and waits for a reply up to `telegram.ask_timeout_s`, returning the answer
   (and a deterministic timeout result when no reply arrives). It blocks **only the current stage**
   and must not alter the single-active-task invariant (§8.2) or the state machine.

3. **Terminal notifications.** On every terminal outcome (`done` / `failed` /
   `manual_action_required`), send one message with the task id, final status, and the PR URL when
   present. Wire this into the Core's terminal path (`core/orchestrator.py`) behind the notifier
   interface, after the ledger record is written. Keep it exactly-once per terminal transition.

## Acceptance criteria

- [ ] A new transport module exposes a small interface (e.g. `send_notification(...)` and
      `ask_human(...)`); the Core depends on that interface, not on `python-telegram-bot`.
- [ ] Token and chat id come **only** from the env vars named in `telegram.*`; with
      `telegram.enabled: false` or missing env vars the integration is a silent no-op and the
      pipeline behaves exactly as today.
- [ ] On `done` / `failed` / `manual_action_required`, exactly one Telegram message is sent (when
      enabled) containing task id, final status, and PR URL if any; a send failure does not change
      the outcome or raise.
- [ ] `ask_human` blocks the current stage until a reply or `ask_timeout_s`, returns the reply for
      `kind="question"` and a yes/no for `kind="approval"`, and the timeout path is deterministic.
- [ ] No secret values (bot token, chat id) appear in logs, SQLite, or artifacts — covered by a test
      that asserts redaction (reuse the existing redaction layer in `providers/redaction.py`).
- [ ] Unit tests cover enabled/disabled, send-success, send-failure (no raise, no outcome change),
      and the `ask_human` answer + timeout paths using a **fake** sender — no real network calls.
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, and `pytest` all pass.
- [ ] Docs + CHANGELOG updated in the same change (`docs/operations.md` HITL/notifications,
      `docs/configuration.md` `telegram.*`, the `[Unreleased]` CHANGELOG entry), per `/sync-docs`.

## Constraints

- Do **not** weaken the security policy (`docs/rules/security.md`): tokens/chat ids stay in env
  vars only, never in `config.yaml` / task files / logs; no change to sandbox/approval handling.
- Keep the hard invariants (CLAUDE.md): the Core stays provider/transport-agnostic behind an
  interface; only the orchestrator owns commit/push/PR; the state machine and fallback are unchanged.
- Do not add new third-party dependencies — `python-telegram-bot` is already declared.
- Tests must be hermetic: no real Telegram API calls; inject the sender.
- Keep it one coherent change (single PR); deep per-stage wiring of clarifying questions beyond the
  terminal notification + the reusable `ask_human` primitive can be a follow-up if it grows the diff.

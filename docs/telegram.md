# Telegram setup and operations

Telegram is optional. When enabled, it provides terminal notifications, clarification questions, and narrowly scoped approvals for dangerous diffs. Use a separate bot and chat for each orchestrator project.

## 1. Create a project bot

1. Open the verified `@BotFather` account in Telegram.
2. Run `/newbot`.
3. Choose a project-specific display name and username.
4. Store the returned token in your secret manager. Do not put it in `config.yaml`, a task file, shell history, logs, or repository files.

The supported topology is one bot, one dedicated chat, and one orchestrator poller. Do not attach the same bot to a webhook or another application that calls `getUpdates`.

## 2. Create and initialize the chat

For a private chat:

1. Open the new bot.
2. Send `/start`.

For a group:

1. Create a dedicated project group.
2. Add the bot.
3. Send `/start@<bot_username>` or another message that mentions the bot.

The bot must be able to read the message used to discover the chat and send messages to that chat.

## 3. Obtain the numeric chat id

Export the token without printing it:

```bash
export TELEGRAM_BOT_TOKEN='...'
```

After sending `/start` or a group message, run:

```bash
python - <<'PY'
import asyncio
import os

from telegram import Bot


async def main() -> None:
    async with Bot(os.environ["TELEGRAM_BOT_TOKEN"]) as bot:
        updates = await bot.get_updates(limit=100, timeout=0)
        for update in updates:
            chat = update.effective_chat
            if chat is not None:
                print(chat.id, chat.type, chat.title or chat.full_name or "")


asyncio.run(main())
PY
```

Private chat ids are normally positive; group/supergroup ids are normally negative. Configure the numeric value, not `@username`.

If no updates appear, send another message to the bot and confirm that no webhook or other poller is consuming updates.

## 4. Configure environment and YAML

The orchestrator reads the **values** from its process environment; YAML holds only the variable **names**. Provide the values either way — an exported variable always wins over the file:

- **`.worc/.env`** (recommended) — put the values in `<repo>/.worc/.env`, which the orchestrator auto-loads at startup. The whole `.worc/` home is gitignored, so the file is never committed; `worc install` drops a `.worc/.env.example` to copy:

  ```bash
  # <repo>/.worc/.env
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=-1001234567890
  ```

- **`export`** — export them in the shell/service that starts the orchestrator. Use this when you launch from outside the repo, or to override the file for a single run:

  ```bash
  export TELEGRAM_BOT_TOKEN='...'
  export TELEGRAM_CHAT_ID='-1001234567890'
  ```

Configure only the environment-variable names:

```yaml
telegram:
  enabled: true
  bot_token_env: "TELEGRAM_BOT_TOKEN"
  chat_id_env: "TELEGRAM_CHAT_ID"
  ask_timeout_s: 28800
  trace: false # optional live per-node progress feed
```

`ask_timeout_s` must be greater than zero. Environment-variable names must match normal shell env name syntax. A service manager such as systemd, launchd, Docker, or Kubernetes can inject the same variables into the orchestrator process, or point it at a file with `--env-file PATH`.

## 5. Remove a webhook

HITL uses long polling. Telegram does not allow `getUpdates` while an outgoing webhook is active. Remove it using the environment-held token:

```bash
python - <<'PY'
import asyncio
import os

from telegram import Bot


async def main() -> None:
    async with Bot(os.environ["TELEGRAM_BOT_TOKEN"]) as bot:
        await bot.delete_webhook(drop_pending_updates=False)


asyncio.run(main())
PY
```

Do not set `drop_pending_updates=True` unless intentionally discarding unprocessed bot updates.

## 6. Verify preflight

Run:

```bash
wastech-orchestrator --config ./config.yaml preflight
```

Telegram preflight checks:

- token presence and `getMe`;
- numeric chat id and bot access to the chat;
- absence of a webhook;
- availability of `getUpdates`.

Expected output includes:

```text
telegram: OK (bot=@project_bot, chat=project-chat, polling ready)
```

## 7. Run the live reply smoke test

```bash
wastech-orchestrator --config ./config.yaml telegram-test --timeout-seconds 60
```

The bot sends a ForceReply message. Reply directly to that message in the configured chat. Success means the reply matched the configured chat and exact Telegram message before the deadline.

The command does not run a provider, process a task, edit repository files, commit, push, or open a PR.

## 8. Runtime behavior

- `refinement` and `planning` may send one question or approval per checkpoint.
- Questions require a reply to the exact prompt.
- Approvals use inline Approve/Deny buttons. **Every** press in the configured chat is acknowledged so you always get feedback: a matching press shows "Approved — continuing." / "Denied — will reconsider."; a stale or duplicate press (a superseded request, or a button left over after a restart) shows an alert, "This approval is no longer active — check the latest message", and is logged as a near-miss rather than silently dropped. Press the button on the **most recent** request.
- Deletions and dependency manifest/lock changes produced by `implementation` or `fixing` require approval unless an exact planning approval already covers the same category and path set.
- Ordinary diffs and routine commit/push/PR publishing do not require Telegram approval.
- Timeout, transport failure, ambiguous approval, or a repeated stage request moves the task to `manual_action_required`.
- `contacts` from task front matter are plain-text mentions only. They do not select the chat.
- **Live step-trace** (`telegram.trace: true`, off by default): the orchestrator pushes one best-effort message per executed flow node finish — `<emoji> <node-id> → <outcome>`, e.g. `✅ implementation → done`, `🔁 review → rework`, `❌ testing → fail` (✅ accept/done/pass, 🔁 rework, ❌ fail, ▶️ otherwise). This gives a remote operator live visibility into a long `watch` run between the start and the terminal notification. It carries only the node id + outcome — never diff, prompt, or agent text — and is fire-and-forget: a send failure never affects the pipeline, and a skipped node emits nothing. Independent of local log verbosity (this is a Telegram push, not a file). The leading emoji keeps trace lines visually distinct from approval/question prompts in the same chat.

Waiting state is stored in `logs/<task-id>/hitl/*.json`, not as a new state-machine status. After a restart, the orchestrator resumes the persisted message/deadline or re-runs the stage with the persisted answer.

## 9. Troubleshooting

`telegram: SKIP (disabled)`

- Set `telegram.enabled: true` for this workspace.

`env var(s) not set`

- Export the exact variables named by `bot_token_env` and `chat_id_env` in the orchestrator process, not only in a different terminal.

`chat id must be a numeric Telegram chat id`

- Use the numeric id discovered from updates. Group ids are commonly negative.

`an outgoing webhook is configured`

- Remove the webhook as shown above. Ensure another deployment does not recreate it.

`Conflict: terminated by other getUpdates request` / `only one poller may run per bot token`

- Another process is polling the same bot token (Telegram allows exactly one `getUpdates` consumer per token). This is the classic two-working-directories hazard — two orchestrator clones sharing one bot. Preflight and the run-time poller now detect the 409 and report it clearly. Stop the other poller or give each deployment a separate project bot/token.

`chat not found` or `Forbidden`

- Send `/start` in a private chat, add the bot to the group, confirm it has not been removed or blocked, and verify the numeric id.

Prompt arrives but reply times out

- Reply to the exact ForceReply message, not as a new standalone message.
- Confirm the reply is in the configured chat.
- Ensure only one poller uses the bot.

Approval button keeps spinning, or shows "no longer active"

- Every press in the configured chat is now acknowledged, so a perpetually spinning button means the press did not reach the active poller at all — check for a second poller on the same token (the `Conflict` case above) or a webhook.
- "This approval is no longer active" means the press reached the poller but did not match the active prompt (a superseded request, an expired interaction, a restarted task already moved to `manual_action_required`, or a leftover button from a previous run). Act on the most recent message.

Messages are truncated

- Telegram limits text messages to 4096 characters. The orchestrator redacts first and appends a truncation marker.

## 10. Security checklist

- Keep token and chat id in environment-backed secrets.
- Use a dedicated project bot/chat with the minimum participants needed.
- Do not paste credentials into human answers.
- Do not use `contacts` as an authorization mechanism.
- Treat Telegram as a notification/approval transport, not a remote shell.
- Preserve the PR and CI control layer; Telegram approval does not merge a PR.

## References

- [Telegram Bot API: getUpdates](https://core.telegram.org/bots/api#getupdates)
- [Telegram Bot API: answerCallbackQuery](https://core.telegram.org/bots/api#answercallbackquery)
- [python-telegram-bot Bot API](https://docs.python-telegram-bot.org/en/stable/telegram.bot.html)

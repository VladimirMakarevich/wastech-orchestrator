# Telegram setup and operations

Telegram is optional. When enabled, it provides terminal notifications, clarification questions, narrowly scoped approvals for dangerous diffs, the two operator confirmation gates, and an optional live per-node progress trace. Use a separate bot and chat for each orchestrator project.

With it disabled — the default — every one of those is a silent no-op: the transport resolves to a null notifier, terminal notifications go nowhere, and any blocking request fails closed to `manual_action_required` rather than proceeding unattended. The same happens when `enabled: true` but either environment variable is missing or blank, so a half-configured setup degrades safely instead of pretending to ask.

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

Private chat ids are normally positive; group/supergroup ids are normally negative. Configure the numeric value, not `@username` — preflight requires the resolved value to be a non-zero integer with an optional leading `-` and no leading zeros.

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

`ask_timeout_s` must be greater than zero, and it is the ceiling for every prompt: a per-node or per-command wait is clamped **down** to it, never up. Environment-variable names must match normal shell env name syntax. A service manager such as systemd, launchd, Docker, or Kubernetes can inject the same variables into the orchestrator process, or point it at a file with `--env-file PATH`.

Both env-var names and `ask_timeout_s` are checked when the config loads, so a malformed value fails **every** command, not just `preflight`.

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
worc --config ./config.yaml preflight
```

Telegram preflight checks:

- token presence and `getMe`;
- numeric chat id and bot access to the chat;
- absence of a webhook;
- availability of `getUpdates`.

Expected output includes:

```text
env: OK — loaded 2 variable(s) from /path/to/repo/.worc/.env
telegram: OK (bot=@project_bot, chat=project-chat, polling ready)
```

The `env:` line is the fastest confirmation that the file holding the token and chat id was found and loaded (it prints the path and a count, never a name or a value). With `enabled: false` the Telegram line is `telegram: SKIP (disabled)` and never fails the gate.

## 7. Run the live reply smoke test

```bash
worc --config ./config.yaml telegram-test --timeout-seconds 60
```

The command first re-runs the Telegram preflight checks and prints that line; only then does the bot send a ForceReply message. Reply directly to that message in the configured chat. Success means the reply matched the configured chat and exact Telegram message before the deadline.

The command does not run a provider, process a task, edit repository files, commit, push, or open a PR.

Exit codes and messages:

| Output | Exit | Meaning |
| --- | --- | --- |
| `telegram-test: OK (correlated reply received)` | 0 | A reply to that exact message arrived from the configured chat. |
| `telegram-test: FAIL (telegram.enabled is false)` | 1 | The feature is off; nothing was sent. |
| `telegram: FAIL — …` | 1 | Preflight failed; nothing was sent. |
| `telegram-test: FAIL (timeout \| transport_error \| invalid_response)` | 1 | The prompt went out but no usable answer came back. |
| `error: --timeout-seconds must be > 0` | 2 | Usage error (a missing config also exits 2). |

`--timeout-seconds` is still capped by `telegram.ask_timeout_s`, so a value above it waits only that long.

## 8. Runtime behavior

- **Which nodes may ask.** A flow node opts in with a `hitl:` block. In the packaged flows that is `refinement` and `planning` (`implementation`), `scope` (`security_audit`), and `refinement` (`deep_research`). Such a node gets exactly **one** round-trip per run: it emits the request, the answer is fed back, and the node re-runs once with it. `allow_question` / `allow_approval` are read as a single opt-in — which kind a node actually sends is steered by its role prompt (`refinement` asks a clarifying question; `planning` may also ask you to approve a risky change), not enforced by the core.
- **Standalone `hitl` gate nodes.** A flow may also place a bare `hitl` node (`signal: question` or `signal: approval`) between nodes — no agent, no prompt text, just the pause. An approval branches the graph on `route:approve` / `route:deny`; a question continues (`done`) on any non-empty answer. Its optional `timeout_s` overrides `ask_timeout_s` downward for that node only.
- Questions require a reply to the exact prompt.
- Approvals use inline Approve/Deny buttons. **Every** press the waiting poller sees in the configured chat is acknowledged so you always get feedback: a matching press shows "Approved — continuing." / "Denied — will reconsider."; a stale or duplicate press (a superseded request, or a button left over after a restart) shows an alert, "This approval is no longer active — check the latest message for the current request", and is logged as a near-miss rather than silently dropped. A press from any other chat is ignored and never acknowledged. Press the button on the **most recent** request.
- **Dangerous-diff approval** runs in **three** places, not one: after every workspace-write node (`implementation`, `fixing`, and `documentation` in the packaged implementation flow), at such a node's own `hitl` round-trip on the way back, and once more **immediately before the publishing commit**. The third exists because any node with a shell can commit — a `tool`, an `evaluator`, a read-only agent attempt, and under `security.strict_isolation: false` that is every node — so a flow ending in one (or with no writing node at all, like `security_audit`) would otherwise publish content nobody had been asked about. **A denial at the pre-publish point is a stop, not a rework:** the agent is gone by then, so nothing is committed or pushed and the task parks in `manual_action_required`. What the diff is measured against is the **last commit the orchestrator itself made** for the task (or the task's base until there is one), never `HEAD` — so an agent that commits its own work mid-run still reaches the gate. Two layers: a changed path matching [`security.protected_paths`](configuration.md#protected_paths-always-ask-floor) always asks, at any level; with [`security.trust_level: strict`](configuration.md#trust_level-approval-policy) (a task's front matter may override it) a deletion/rename or a dependency manifest/lock change also asks. The shipped defaults — `trust_level: auto`, empty `protected_paths` — mean no diff-shape approval at all, so this gate is opt-in. An identical approval already given earlier in the same task (the `planning` pre-approval, or an earlier node's) counts, matched on the exact risk category **and** path set, so the same change is never queried twice; a new or expanded set still asks.
- Deny is not terminal by itself: the node re-runs once with the denial as context, and only if the diff is still gated afterwards does the task move to `manual_action_required`.
- Ordinary diffs and routine commit/push/PR publishing do not require Telegram approval.
- **Operator confirmation gates** (full-auto `watch`, both off by default, both fail-closed). When `orchestrator.auto_mode.confirm_next_task` is on, `watch` asks an approve/deny before claiming each pending task (id + title only); deny / timeout / no answer leaves it pending and stops chaining for that cycle. When a Claude provider's `max_turns_gate` is on, a run that exhausts `max_turns` asks **continue/stop** (the same Approve/Deny buttons — Approve continues); continue resumes the same agent session with a fresh turn grant, deny / timeout / no answer stops the run (as without the gate). Both require `telegram.enabled` — an enabled gate with no transport is rejected when the config loads, so every command fails, not just `preflight`. A silent operator never advances an autonomous action. See [configuration.md](configuration.md) for the keys.
- Timeout, transport failure, ambiguous approval, or a repeated stage request moves the task to `manual_action_required`.
- **What the agent sees of your answer** is only the sanitized packet `.worc-io/<task-id>/hitl/<node-id>.answer.json` — `{kind, question, answer, approved}`, already redacted. The interaction id, Telegram message id, deadline, and failure bookkeeping stay in the private record and never reach the provider.
- **Terminal notification** (best-effort, one per finished task, independent of `trace`). A clean finish stays terse: `✅ [<task-id>] status=done pr=<url>`. A needs-attention terminal (`🛑 manual_action_required`, `❌ failed`) expands into a multi-line body — title, the node it stopped at and its loop, the fix-round count, a prose reason, the top blocking finding with its paths, the branch, the PR, and the on-disk report to open next — with each line emitted only when that datum exists. A run that edited its own governance/instruction files adds a `Governance files changed:` notice; it is a notice, never a block.
- `contacts` from task front matter are plain-text mentions only. They do not select the chat.
- **Live step-trace** (`telegram.trace: true`, off by default): the orchestrator pushes one best-effort message per executed flow node finish — `[<task-id>] <emoji> <node-id> → <outcome>`, e.g. `[T-101] ✅ implementation → done`, `[T-101] 🔁 review → rework`, `[T-101] ❌ testing → fail` (✅ accept/done/pass, 🔁 rework, ❌ fail, ▶️ otherwise, including a `hitl` node's `route:approve` / `route:deny`). This gives a remote operator live visibility into a long `watch` run between the start and the terminal notification. It carries only the node id + outcome — never diff, prompt, or agent text — and is fire-and-forget: a send failure never affects the pipeline, and a skipped node emits nothing. Independent of local log verbosity (this is a Telegram push, not a file). The leading emoji keeps trace lines visually distinct from approval/question prompts in the same chat.

  Three **⚠️ synthetic labels** are not plain node verdicts but events worth flagging, and each is also always logged as a console warning independent of this flag:

  | Trace | Means | What to do |
  | --- | --- | --- |
  | `⚠️ <node> → accept (rework budget exhausted)` | A **non-blocking** evaluator spent its `max_rework_per_stage` with a finding still open, so it accepted and the flow continued (never `manual`). | Read the finding in the PR follow-ups — the gate deliberately moved on, and it may still need attention. |
  | `⚠️ <node> → done (node wrote to the workspace unexpectedly)` | A node that gets a shell without write access produced a file — a granted read-only agent node, a Codex read-only node, an evaluator, an operator `tool`. The run does **not** park; the change is neither published nor handed downstream. | Note it; a stray file is inert. Worth checking the role prompt. |
  | `⚠️ <node> → done (node changed git control state)` | A node drifted git control state — a moved `HEAD`, the index, a hook, `.git/config`. The run warns and continues. | **Treat this as a stop-the-run signal.** Unlike a stray file, a planted `.git/hooks/post-commit` is executed by the next git command in that clone — and the next one is the orchestrator's own commit or push. If you did not do it yourself, kill the run, discard the clone, inspect what was planted. |
  | `⚠️ <node> → done (publish adopted commits it did not make)` | Publishing found commits on the remote branch that the orchestrator never put there, merged them in locally, re-ran the quality gate over the combination, and only then pushed. The run succeeded. | Nothing to fix — but the task's reported diff is measured from the base and therefore now covers someone else's work too. With `publish: pull_request` the PR body says this as well; with `push`/`commit` this trace is the **only** place it is said. |

  The git-control label is checked first of the two write labels, being the sharper event. **Neither parks the task, on any node class** — including `workspace-write`. That is a change from the earlier model, and the cost is stated plainly: the fingerprint reads "the state moved", never whose hand moved it, so it is the operator who has to decide. What holds a commit made _inside_ a run is a different mechanism — the pre-publish [dangerous-diff gate](configuration.md#where-you-are-asked-in-three-places), which measures from the last commit the orchestrator itself made and therefore sees it.

Waiting state is stored in `.worc/logs/<task-id>/hitl/*.json`, not as a new state-machine status. One file per interaction, named by what asked: `<node-id>.json` (a node's embedded question/approval), `node-<node-id>.json` (a standalone `hitl` gate), `guardrail-<node-id>-cycle-<n>.json` (a dangerous diff), `turn-gate-<node-id>.json` (the max-turns gate); a decomposed run adds a `-subtask-<n>` suffix. Each carries a `status` — `waiting`, `answered`, `consumed`, or the failure (`timeout` / `transport_error` / `invalid_response`) — plus the redacted request and the transport handle. After a restart, the orchestrator resumes the persisted message/deadline or re-runs the stage with the persisted answer; a `waiting` max-turns gate is resolved before the provider is touched, so a restart never silently burns a fresh turn grant. The `confirm_next_task` gate is the one deliberate exception: it writes no artifact, so a daemon restarted mid-prompt simply re-asks on the next cycle.

The wait is also visible in the run log without Telegram: entering it logs `awaiting human input`, the `--heartbeat-seconds` timer (default 30, `0` disables) repeats a heartbeat while it blocks, and it closes with `human input resolved` plus the status. Only ids, kind, and timeout are logged — the question text and paths stay in the redacted artifact.

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

- Presses are read only while a run is actually waiting on an interaction. A perpetually spinning button therefore means either that nothing is waiting any more (the task already timed out, was denied, or reached a terminal status) or that the press never reached the poller — check for a second poller on the same token (the `Conflict` case above) or a webhook.
- "This approval is no longer active" means the press reached the poller but did not match the active prompt (a superseded request, an expired interaction, a restarted task already moved to `manual_action_required`, or a leftover button from a previous run). Act on the most recent message.

Messages are truncated

- Telegram limits text messages to 4096 characters. The orchestrator redacts first and appends a `[message truncated by wastech-orchestrator]` marker.

`telegram update backlog is too large to drain safely`

- Before sending a prompt the orchestrator discards the pending update backlog so an old message can never be mistaken for the answer; it gives up past roughly a thousand queued updates and the interaction fails as a transport error. Keep the chat dedicated to the orchestrator, and do not pre-answer a prompt that has not arrived yet — anything sent before the prompt is drained.

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

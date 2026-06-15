# Stage 08 - Telegram integration and human-in-the-loop

> **Status: implemented and verified (2026-06-13).**
>
> This document records the post-MVP implementation. The canonical behavior is also reflected in [00_orchestrator_final_plan.md](00_orchestrator_final_plan.md). Operator setup lives in [../telegram.md](../telegram.md).

## Goal

Telegram is the optional transport for:

- best-effort terminal notifications;
- blocking clarification questions from `refinement` and `planning`;
- yes/no approval requests from `refinement` and `planning`;
- deterministic approval guardrails for tracked-file deletion and dependency manifest/lock changes produced by `implementation` or `fixing`.

Routine code changes and routine commit/push/PR publishing remain automatic. Telegram does not select a repository, provider, route, or chat dynamically.

## Architecture

The Core depends only on the transport-neutral `Notifier` protocol:

```text
Core
  -> Notifier.start_ask(...) -> AskHandle
  -> persist HITL artifact
  -> Notifier.wait_for_answer(handle) -> AskResult
  -> re-run the same stage with human_input_path

TelegramNotifier
  -> sendMessage (ForceReply or inline keyboard)
  -> getUpdates long polling
  -> answerCallbackQuery for a matched approval
```

`ask_human(...)` remains a convenience facade over the two-phase contract. The durable `AskHandle` contains only correlation metadata: interaction id, kind, deadline, Telegram message id, and update offset. Bot tokens and chat ids are never included.

The Core still calls providers only through `AgentProvider`. Human answers are passed to both providers as `AgentRunRequest.human_input_path`; provider adapters add only that artifact path to their context footer. The answer is never placed in CLI argv and is never interpolated into the stage prompt.

## Typed stage output

`refinement` and `planning` now use strict structured output.

Common fields:

```json
{
  "content": "stage result",
  "human_input": {
    "kind": "question",
    "question": "Which database should be used?",
    "context": "Repository evidence supports two choices.",
    "risk": "clarification",
    "paths": ["src/storage.py"]
  }
}
```

`human_input` may be `null`. When present, its exact keys are:

- `kind`: `question` or `approval`;
- `question`: non-empty bounded text;
- `context`: bounded supporting text;
- `risk`: `clarification`, `deletion`, `dependency`, or `other`;
- `paths`: bounded repository-relative paths, normalized to a sorted unique set.

`planning` additionally returns `decompose` and `subtasks`. The Core validates exact top-level keys, exact `human_input` keys, path safety, and every subtask field/type independently of provider schema enforcement. Invalid output fails the pipeline; it is not treated as a human request.

## Telegram UX and correlation

- Questions use `ForceReply`; only a non-empty reply to the exact bot message is accepted.
- Approvals use inline **Approve** / **Deny** buttons; only callback data containing the exact interaction id and attached to the exact prompt message is accepted.
- Replies and callbacks from any chat other than the configured numeric `chat_id` are ignored.
- Pending updates are drained before a prompt is sent. The returned offset is stored in the durable handle, so old updates cannot satisfy a new request.
- Every processed update advances the local offset. Matched callbacks are acknowledged with `answerCallbackQuery`.
- `getUpdates` uses bounded long polling and an HTTP read timeout longer than the polling timeout.
- Outgoing text is redacted and limited to Telegram's 4096-character message limit.
- `contacts` are rendered only as plain-text mentions. They do not select the chat and grant no access.

The supported deployment is one bot and one dedicated numeric chat used by one orchestrator process. A webhook or another `getUpdates` consumer for the same bot conflicts with this design.

## Durable interaction artifacts

No SQLite schema change was introduced. Recovery is driven by atomically replaced JSON artifacts:

```text
logs/<task-id>/hitl/
  refinement.json
  planning.json
  guardrail-implementation[-subtask-<n>]-cycle-<n>.json
  guardrail-fixing[-subtask-<n>]-cycle-<n>.json
```

The actual filename is deterministic for the stage/checkpoint. Each artifact records:

- schema version, task id, stage, and optional subtask;
- redacted request (`kind`, question, context, risk, normalized paths);
- secret-free handle, Telegram message id, update offset, and wall-clock deadline;
- redacted answer, approval value, failure class, and processing status.

Relevant statuses are `waiting`, `answered`, `consumed`, `timeout`, `transport_error`, `invalid_response`, `reconsidering`, and `reconsidered`. They are artifact-local statuses, not new canonical task-state values.

## Stage behavior

For `refinement` or `planning`:

1. The provider returns a typed `human_input` signal.
2. The Core sends the prompt and persists the handle before waiting.
3. A valid answer is written to the same artifact.
4. The same stage is run once more with `human_input_path`.
5. The stage must return `human_input: null`; a second request at that checkpoint moves the task to `manual_action_required`.
6. The artifact is marked `consumed`.

An explicit approval denial is therefore returned once to the same stage for safe plan/spec revision. Questions and approvals share the same one-round-trip limit.

## Dangerous-diff guardrails

After every `implementation` and `fixing` run, before tests:

1. Git Manager produces the current diff and structured changed-path entries.
2. The Core detects:
   - deletion of tracked files, including the old side of a rename;
   - changes to known dependency manifests and lock files for Python, Node, Rust, Go, Ruby, PHP, Java/Gradle, Swift/CocoaPods, Dart, Elixir, .NET, C/C++, Clojure, Scala, Bazel, Nix, and Terraform.
3. An ordinary diff continues without Telegram.
4. A dangerous diff requires an inline approval unless a consumed, approved planning request has exactly the same risk category and normalized path set.
5. Any added path or changed category requires a separate approval.

If approval is denied, the same editing stage is run exactly once with the denial artifact. The agent must remove or safely rework the dangerous change. If dangerous changes remain, the task becomes `manual_action_required`. Routine publishing never triggers this approval.

## Failure and recovery semantics

The feature is fail-closed for blocking HITL:

- timeout -> `manual_action_required`;
- transport send/poll error -> `manual_action_required`;
- ambiguous approval -> `manual_action_required`;
- second signal after an answer -> `manual_action_required`;
- dangerous diff expanded relative to its persisted request -> `manual_action_required`;
- restart during denied-change reconsideration -> `manual_action_required`.

On restart:

- `waiting` resumes polling the original message with the original offset and deadline;
- `answered` re-runs the stage with the existing artifact;
- `consumed` is safe to replay as stage context if recovery re-enters that checkpoint;
- malformed or unexpected artifact states fail closed.

The canonical state machine remains unchanged: there is no `waiting_human` task status.

## Configuration and preflight

`telegram.ask_timeout_s` must be greater than zero. `bot_token_env` and `chat_id_env` must be valid environment-variable names.

Preflight verifies:

- Telegram is enabled, or reports `SKIP`;
- both named environment variables are present;
- `chat_id` is a non-zero numeric Telegram chat id;
- the token can call `getMe`;
- the bot can access the configured chat;
- no webhook is configured;
- `getUpdates` is available for long polling.

`wastech-orchestrator telegram-test --timeout-seconds N` runs preflight, sends a real ForceReply question, and succeeds only after a correlated reply. It does not process a task or modify the repository.

## Security properties

- Credentials are resolved only from the orchestrator process environment.
- Tokens and chat ids are excluded from handles, provider requests, SQLite, and HITL artifacts.
- Known credential literals, token-shaped text, and sensitive assignments are redacted from outgoing messages and transport errors.
- Telegram replies are stored only in redacted form.
- User text never enters provider CLI argv.
- Paths from an agent signal must be repository-relative and cannot contain traversal.
- A task's `contacts` field cannot change the configured chat.
- Approval scope is narrow: deletion/dependency changes only. It does not delegate Git ownership to an agent and does not weaken sandbox or approval policy.

## Verification matrix

Coverage includes:

- ForceReply questions and inline approval buttons;
- exact prompt/callback correlation, foreign chats, stale and unrelated updates;
- callback acknowledgement, update offsets, long-poll/read timeouts, timeout and transport errors;
- message bounds and credential redaction;
- token/chat/webhook/polling preflight and `telegram-test`;
- invalid timeout and environment-variable names;
- question/approval flows in `refinement` and `planning`;
- one-round-trip enforcement and answer reinjection via `human_input_path`;
- durable waiting-message restart recovery;
- ordinary diffs, deletion/dependency approvals, exact planning reuse, expanded diffs, denial reconsideration, and remaining-risk manual escalation;
- provider footer/request artifacts for both Codex and Claude;
- fake-client end-to-end behavior without network access.

The optional live smoke test is intentionally credential-gated and is run with `wastech-orchestrator telegram-test`.

## Known limitations

- One orchestrator poller per bot is supported.
- Telegram is not a general remote command channel.
- There is no edit/delete of already-sent prompts after terminal resolution.
- HITL interactions are not queryable as first-class SQLite rows; registered JSON artifacts are the recovery source of truth.
- A process crash after a denial enters reconsideration is escalated rather than risking a duplicate editing run.

## References

- [Telegram Bot API: getUpdates](https://core.telegram.org/bots/api#getupdates)
- [Telegram Bot API: answerCallbackQuery](https://core.telegram.org/bots/api#answercallbackquery)
- [python-telegram-bot Bot API](https://docs.python-telegram-bot.org/en/stable/telegram.bot.html)

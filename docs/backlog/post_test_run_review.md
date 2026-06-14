# Post-test-run review — improvements

This document collects the improvements we identified while reviewing the **first real orchestrator
test run**: the `task-pr-title-override` task on 2026-06-14. It is kept separate from
[follow_ups.md](follow_ups.md) (build-time tech-debt) on purpose — every item here comes from watching
the orchestrator drive an actual task end to end. Add new test-run findings here.

All items are **candidate** unless noted. The *Refs* lines point at the code to change.

> **Status (2026-06-14): all items below are resolved.** Items 1.1, 1.2, 2.1, 2.2, 3.1, 4.1, and 5.1
> were implemented across four phases; each section carries a `Resolved (2026-06-14)` note with the
> code refs, and the changes are recorded in [CHANGELOG.md](../../CHANGELOG.md) and
> [follow_ups.md](follow_ups.md).

## Background — what the run showed

The task's own work was correct and complete: the `pr_title` field was added across the task model,
the validation gate, and the publish step, with tests and docs. The run still failed, and the failure
was in the machinery around the agent, not in the agent's code:

1. The `types` check ran as `mypy .`. The explicit `.` overrode the project's configured scope
   (`[tool.mypy] files = ["src"]`), so the gate type-checked `tests/` — 219 pre-existing errors that
   have nothing to do with the task. The task could never make the gate pass, so it looped
   `testing → fixing`.
2. The fixing agent's only realistic way out — exclude tests from mypy in `pyproject.toml` — is a
   dependency-manifest change, so it tripped the dangerous-diff guardrail and raised a Telegram
   approval request.
3. The operator pressed "Approve", but the press was lost, so the guardrail stayed `waiting` and the
   run eventually failed.

The items below group into four areas: the quality gate / check discovery, planning context & skills,
repo hygiene, and Telegram human-in-the-loop reliability.

## 1. Quality gate & check discovery

### 1.1 Hotfix — check detection ignores the project's configured tool scope

*Headline finding; caused the real task failure.*

> **Resolved (2026-06-14).** `RepositoryInspector` now reads `[tool.mypy] files`/`exclude` and the
> `[tool.ruff]` scope keys from `pyproject.toml` into `RepositoryEvidence`, and `CheckCandidateDetector`
> emits a scoped command (`mypy src` / bare `mypy` for exclude-only / `ruff check`) instead of
> appending `.` when a scope is configured. No-scope behavior is unchanged. Unsafe scope paths
> (absolute / `..`) are rejected. See `checks/inspect.py` (`_tool_scopes`, `_safe_scope_paths`),
> `checks/detect.py` (`_ruff_argv`/`_mypy_argv`), and `tests/checks/test_checks_detect.py`.

`CheckCandidateDetector` builds the type check as `mypy .` (and lint as `ruff check .`) from
tool-presence alone, never reading the project's configured scope. An explicit `.` path **overrides**
`[tool.mypy] files = ["src"]`, so the gate type-checked `tests/` — 219 pre-existing errors unrelated to
the task — which the fixing agent could never (and shouldn't) clear. Its only viable workaround
(`exclude = ["^tests/"]` in `pyproject.toml`) tripped the dependency-diff guardrail and the run failed
closed.

Next step: teach detection to respect `[tool.mypy] files`/`exclude` (emit a no-arg `mypy`, or
`mypy <files>`, when a scope is configured), and consider the same for `ruff`. Also document the
`checks.commands` pin as the supported escape hatch. This is a control-layer fix and is the quickest
way to let `pr_title` re-run green.

*Refs:* `checks/detect.py` (`_venv_checks`/`_manifest_checks`/`_plain_python`); `checks/inspect.py`;
the failed `pr_title` run (`logs/task-pr-title-override/`).

### 1.2 Check discovery v2 — resolve at run time, agent-assisted, human-checked

> **Resolved (2026-06-14).** Discovery now runs at task start (`checks.discovery.run_at_task_start`),
> so `auto` mode can resolve/agent-assist in-band; install-time discovery is a cache-warming option.
> Re-resolution fires **only on infrastructure proof** — a check launch failure (bounded once per
> task via `_reresolve_on_launch_failure`), fingerprint change, or low confidence — and never on a
> quality failure (that routes to `fixing`, with an explicit guard/comment). A *changed* command set
> is a sensitive change: written to the profile (`commands_signature` + `approved` fields, profile
> schema v2) and human-approved on first use, fail-closed on denial/timeout/no-notifier; the
> first-ever set is auto-approved + recorded. `auto`-mode configured commands **pin** their named
> slot and let detection fill the rest (a pin is never silently replaced). The discovery agent is told
> machine config beats prose; proposals still flow through validate-argv → probe → approve.
> `model`/`reasoning`/`agent_fallback`/`run_at_task_start`/`approve_command_changes` are now in
> `config.example.yaml`; `config.yaml` schema_version → 5. See `checks/resolver.py`
> (`reresolve`/`ReResolveReason`/`_select` pinning), `checks/profile.py`, `core/orchestrator.py`
> (`_check_preflight`/`_gate_check_commands`/`_reresolve_on_launch_failure`), `checks/agent.py`.

Today check discovery is deterministic only and runs at install time. The agent fallback is off by
default (no model set) and never runs while a task is in progress. The `pr_title` run showed the cost:
the detector produced `mypy .`, which ignored the project's `mypy src` scope, and nothing could fix it
at run time. The goal is to let discovery work out the right commands while the task runs — safely.
This is the larger rework that eventually absorbs 1.1. Agreed shape:

- **Run discovery inside the state machine, not only at install.** Work out the check commands when a
  task starts, save the result, and recompute only when the repo changes (the fingerprint + `refresh`
  machinery already exists). Keep install-time discovery as an optional way to warm the cache.
- **Only re-run discovery when there is real proof the command is wrong** — it fails to launch, the
  tool is missing, the config/CI files changed, or deterministic detection is low-confidence. Never
  re-run discovery just because a check reported failures. Reacting to failures would let the gate
  quietly rewrite its own command until it passes — exactly what the agent tried in `pr_title`
  (`mypy .` → `mypy src` / `exclude tests`).
- **When detection is unsure and a check keeps failing, ask a human** instead of letting the agent
  silently change the command. For example: "the type check runs `mypy .` and fails, but your pyproject
  limits mypy to `src` — should the command be `mypy src`?" Treat any change to the set of check
  commands as a sensitive change (write it to the audit profile, approve it on first use), because it
  decides what "passing" means.
- **In `auto` mode, let `commands` add to detection instead of replacing it.** A configured command
  should pin only the one check it names (e.g. `{name: types, argv: [mypy, src]}`) and let detection
  fill in the rest. Today a single non-empty entry silently turns detection off for everything else.
- **Make the discovery model and reasoning configurable and visible.** The setting already exists in
  the schema (`checks.discovery.model` / `reasoning`) but is hidden from the example config, off by
  default (empty model), and install-only. Show it in `config.example.yaml`, document it, and wire it
  into the run-time path. Keep it opt-in so default runs stay deterministic. (This is the model for the
  discovery *agent* — the Check Runner itself just executes commands and uses no model.)
- **Give the discovery agent repo context to read** — `CLAUDE.md`, `AGENTS.md`, `pyproject.toml`,
  `Makefile`, CI files. These often state the real check commands in plain text (`CLAUDE.md` here
  literally says `mypy src`). Treat this context as **untrusted evidence, not truth**: these files are
  controlled by the repo, so a bad one could try to inject an unsafe command. Every proposed command
  still goes through the same funnel — validate the argv (no shell, no forbidden flags), probe that it
  launches, write it to the resolved profile for audit, and ask for human approval the first time. When
  plain-text hints and machine config disagree, prefer the machine config (`[tool.mypy] files`, CI) and
  use the prose only as a tiebreaker.

*Refs:* `checks/detect.py`; `checks/resolver.py`; `checks/agent.py`; `checks/discovery_factory.py`;
`checks/inspect.py`; `config/schema.py` (`CheckDiscoveryConfig`); `config.example.yaml`
(`checks.discovery`).

## 2. Planning context & skills

### 2.1 Planning-selected skill references per stage (skill inventory)

> **Resolved (2026-06-14).** New `core/skills.py` scans the **target repo's** `.claude/skills/*/SKILL.md`
> (name+description only, bounded, frontmatter-only). Planning emits a `skills` list in its structured
> output (`core/hitl.py`); the Core deterministically accepts only scanned, non-gate-duplicating names
> (`resolve_planning_skills`) and records the choice + any dropped names in `plan.md`. Chosen files reach
> `implementation`/`fixing` as read-only reference paths via the new `{skills_path}` prompt variable and
> `AgentRunRequest.skill_reference_paths` (identical footer in both providers — never the Claude Skill
> tool). **Decision (with the operator):** the scan root is the target repo clone (`<repo>/.claude/skills`),
> the architecturally-correct tree the agent can read — not the orchestrator's own dev skills. New
> `skills:` config block (`scan_root`/`exclude`, default-excluding `run-checks`/`test`/`sync-docs`).

Repo skills (`.claude/skills/*/SKILL.md`) are provider-neutral markdown (`name`/`description`
frontmatter + body). Idea: at task start scan the skill **inventory** (name+description only — cheap,
mirroring the check inventory in `checks/inspect.py`), surface it to the `planning` stage, and have
planning emit the *relevant* skills as part of its structured plan output (task-aware, in-band,
auditable in `plan.md`). Downstream stages (`implementation`/`fixing`) then receive the chosen
`SKILL.md` files as **read-only reference paths** via the existing `prompts.append` mechanism — advisory
only, never invoked, subject to the same `extra_args`/permission rejection.

Surface **only procedural-knowledge skills** (`fake-cli`, `add-provider`); **exclude gate-duplicating
ones** (`run-checks`, `test`, `sync-docs`) the orchestrator already owns as deterministic gates
(two-sources-of-truth + scope-creep risk). Provider-neutral: pass as file paths, never the Claude-only
Skill tool (Codex has none). This is a context-layer quality improvement — **orthogonal** to
check-detection correctness; it would not by itself prevent a control-layer bug like the `mypy .` scope
issue.

*Refs:* `planning` stage; `core/prompts.py` (`prompts.append`); `checks/inspect.py` (inventory-scan
analog); `.claude/skills/`; the deferred `agent_instructions:` extra in the "Prompt customization v1"
row of [follow_ups.md](follow_ups.md).

### 2.2 Planning context dedup (skills ↔ user prompt)

> **Resolved (2026-06-14).** `compute_skill_dedup` (in `core/skills.py`) deterministically compares the
> operator's appended planning guidance (`prompts.append` for `planning`) against the chosen skill
> bodies at the markdown-heading level (v1): a skill section whose normalized heading matches a user
> heading is recorded in `plan.md` as covered-by-your-instructions, so the operator's explicit text wins
> and the agent is not handed the same guidance twice. Visible in `plan.md`, not a hidden post-process;
> a no-op when there is no appended planning text. Token-overlap-threshold matching (catching
> restatements under different headings) is a possible v2 follow-up.

Companion to 2.1. When `planning` assembles the stage context it should **de-duplicate overlapping
guidance**: operator/user prompt text (via `prompts.append`) may already restate points that a
referenced `SKILL.md` also covers, so the agent would get the same instruction twice (prompt bloat,
dilution, and the risk of subtly conflicting phrasings). Next step: when planning picks skills and the
prompt is assembled, detect overlap between the appended user-prompt content and the chosen skill bodies
and keep a single source (prefer the user's explicit text; drop or just reference the skill section it
duplicates). Keep it deterministic — overlap resolution belongs in the planning step and should be
visible in `plan.md`, not a hidden post-process.

*Refs:* `planning` stage; `core/prompts.py` (`prompts.append`); item 2.1 above.

## 3. Repo hygiene

### 3.1 Stop tracking `checks/resolved-profile.json` (and fix the pid ignore target)

> **Resolved (2026-06-14).** `checks/` is now in `EXCLUDED_DIRS`, `_local_only_dirs` (every footprint)
> and `RUNTIME_GITIGNORE_LINES`, so the generated profile never enters a code commit or the operator's
> `git status`. The Git Manager guarantees the runtime ignores exist in the clone it operates in
> (`ensure_runtime_excludes`, called at branch-prep). **Decision (with the operator):** the
> `.git/info/exclude` (local, per-clone) default was kept — the orchestrator does not force a tracked
> `.gitignore` mutation on the operator's repo — and reliability is instead guaranteed in the running
> clone. `preflight_footprint` deliberately does *not* gate on an already-committed `checks/` (that
> would block the very repos that hit the leak); the operator untracks it once with `git rm --cached`.
> See `git_manager.py` and `tests/git/test_git_manager.py`.

Two runtime files leak into the operator's git status / commits under the in-repo footprint:

- **`checks/resolved-profile.json` is the real gap.** It is a generated runtime artifact (the resolved
  check profile) but it is not ignored and not excluded from the orchestrator's own commit, so it gets
  tracked — it was committed as part of the failed `pr_title` run. Two reasons: it is not in
  `RUNTIME_GITIGNORE_LINES` (so `install`/`init` never add it to the ignore list), and `checks/` is not
  in `EXCLUDED_DIRS` (so the `git add` guard in `staged_pathspec`/`is_excluded` lets it slip into the
  code commit). Fix both: add the `checks/` artifact dir to `RUNTIME_GITIGNORE_LINES` **and** to
  `EXCLUDED_DIRS`. Decision: ignore the whole `checks/` dir (it is generated, like `logs/`), rather than
  the single filename — simpler and future-proof.
- **`orchestrator.pid` is already handled, but maybe not where the user expects.** It is already in both
  `_EXCLUDED_FILES` and `RUNTIME_GITIGNORE_LINES`, but `install`/`init` write the ignores to
  `.git/info/exclude` (local, per-clone) by default and only touch the tracked `.gitignore` with
  `--gitignore-tracked`. The user expects the repo's `.gitignore` to be updated at install. Decide: make
  `.gitignore` the default for the in-repo footprint, or at least make sure these runtime files are
  reliably ignored in the clone the orchestrator actually runs in. (Note the current default avoids
  forcing a shared/tracked ignore on the operator's repo — so this is a real policy choice, not just a
  bug.)

*Refs:* `git_manager.py` (`_EXCLUDED_FILES`, `EXCLUDED_DIRS`, `RUNTIME_GITIGNORE_LINES`,
`append_runtime_excludes`, `staged_pathspec`/`is_excluded`); `cli.py` `cmd_init`/`cmd_install`
(`--gitignore-tracked`); `checks/store.py` (`<artifacts_root>/checks/resolved-profile.json`); the
"`.gitignore` for in-repo runtime files" row in [follow_ups.md](follow_ups.md) (2026-06-12).

## 4. Telegram human-in-the-loop

### 4.1 The Approve button press can be silently lost

*Real bug hit during the run.*

> **Resolved (2026-06-14).** `poll_reply` now acknowledges **every** callback in the configured chat
> (matching → continue/deny; near-miss → a `show_alert` "no longer active" toast) and logs near-misses
> with a secret-free reason (`wrong_message_id`/`unexpected_data`/`message_none`); a foreign chat's
> callback is never acknowledged. The offset still advances (re-fetching a near-miss forever would spin
> the loop), but never *silently*. A second `getUpdates` consumer on the same token (Telegram 409
> Conflict — the two-working-dirs hazard) is detected and surfaced with a clear "only one poller per bot
> token" message at preflight and run time; the existing webhook-must-be-off preflight is unchanged. No
> token or raw chat id is logged. See `notify/telegram.py` and `tests/notify/test_http_client.py`.

During the `pr_title` run the operator pressed "Approve" several times within the timeout (around the
2-minute mark) but nothing happened — the press never registered, the guardrail stayed `waiting`, and
the task eventually failed. The poller (`poll_reply`) waits on Telegram `getUpdates` for the whole
timeout, but the way it handles updates makes any miss permanent and silent:

- For every update it receives it advances the offset (`offset = update.update_id + 1`) and then, on a
  non-match, just `continue`s. The next `getUpdates` call confirms (deletes) that update, so a button
  press that did not match the exact conditions is **thrown away for good** — and pressing again sends
  the same `callback_data`, which misses the same way.
- It only calls `answer_callback_query` on a match, so a dropped press leaves the button with no
  acknowledgement — to the user it looks like "nothing was sent".
- Likely reasons the press misses on the first try (need to confirm against a live run): more than one
  `getUpdates` consumer on the same bot token (e.g. two orchestrator instances — note the two working
  dirs `wastech-orchestrator` and `wastech-orchestrator-orchestrator` — Telegram allows only one
  `getUpdates` per token); a webhook set on the bot (then callbacks never arrive via `getUpdates`); or
  `callback_query.message` arriving as `None`/inaccessible.
- Hardening to do: don't advance the offset past an update you didn't handle without logging it; log
  "near-miss" callbacks (right chat but wrong `message_id`/`data`, or `message` is `None`); acknowledge
  every press with `answer_callback_query` (even a non-match, so the user gets feedback); make sure only
  one poller runs per bot token, and detect/warn on webhook mode in preflight.

*Refs:* `notify/telegram.py` (`poll_reply`, `send_prompt`/`_drain_pending`, `wait_for_answer`,
`check_polling`, `get_webhook_info`); `notify/interface.py` (`AskHandle.update_offset`);
`core/hitl.py`; the `pr_title` guardrail
(`logs/task-pr-title-override/hitl/guardrail-fixing-cycle-10.json`).

## 5. Task summary / reporting

### 5.1 Failure summary is too verbose (it inlines the whole diff)

> **Resolved (2026-06-14).** `write_minimal_summary` now takes a `diff_stat` + `task_ref` instead of
> the full description + diff: the committed `<id>.summary.md` shows a `git diff --stat` (files + line
> counts), links to the task file, and points to the already-redacted `logs/<id>/current.diff` for the
> full patch. New `GitManager.diff_stat()`. This also closes a latent gap where the old fallback inlined
> an *unredacted* `cumulative_committed_diff()` into the committed summary. Only the deterministic
> fallback changed (happy-path agent summary and skipped-stage stub untouched). See `ledger.py`,
> `core/orchestrator.py` (`_summary`/`_summary_md_body`/`_task_ref`), `tests/core/test_ledger.py`.

When a task has no agent-authored summary — it failed before the `summary` stage ran, or the stage
could not produce one — the orchestrator falls back to `write_minimal_summary`, which writes the task
title, the **full task description**, and the **entire diff** inline into the committed
`<id>.summary.md`. For the failed `pr_title` run that produced a ~580-line file that is mostly the raw
diff. The point of `summary.md` is a short, readable explanation, so the fallback should stay small
too.

Note this is **only** the deterministic fallback. On the happy path the `summary` stage runs an agent
and its concise message becomes the summary; a skipped summary stage writes a tiny stub. So a
successful task already gets a short summary — this item is just about making the failure/no-agent
fallback compact.

Next step: in the fallback, replace the inline full diff with a `git diff --stat` summary (files +
line counts) and/or a pointer to the diff artifact, and trim the verbatim task description (link to the
task file instead of pasting it).

*Refs:* `core/orchestrator.py` (`_summary` fallback branch, `_summary_md_body`,
`write_minimal_summary`); the failed `pr_title` summary
(`tasks/failed/task-pr-title-override.summary.md`).

## Suggested order

1. **1.1** — small, control-layer fix; unblocks re-running `pr_title` green.
2. **4.1** — any approval-gated task can stall on this; reliability first.
3. **3.1** and **5.1** — cheap repo hygiene and reporting cleanups.
4. **2.1 / 2.2 / 1.2** — larger scheduled work; 1.2 eventually absorbs 1.1.

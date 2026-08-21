# `config.yaml` — field reference

**You are an operator (or an agent helping one) configuring wastech-orchestrator.** This is the entry page of the complete, self-contained reference for every `config.yaml` field — its allowed values, its default, its constraints, and when to change it. You do not need the internet or the repo's own `docs/` to fill in a config. This page carries the rules that apply everywhere, the core identity blocks, and the cross-field gotchas; the rest is one page per concern (see [Where the rest of the fields are](#where-the-rest-of-the-fields-are)). The copy-paste template carrying these same fields is `config.example.yaml` (`worc install` copies it verbatim into `.worc/`, beside the `config.yaml` it generates); this reference explains what each one means. For the _how-to_ walkthrough ("build in this order") see [README.md](README.md); for safe defaults see [best-practices.md](best-practices.md).

Two rules apply everywhere:

- **Unknown keys fail closed.** A key the schema does not know — at the top level or inside any block — is a hard load error (a few long-removed keys are silently tolerated for back-compat). Do not invent fields.
- **Three list fields _replace_, never extend, their defaults:** `security.allowed_environment`, `security.denied_read_paths`, `security.denied_commands`. If you write one, write the whole list you need. Note what that means for `allowed_environment` in particular: `config.example.yaml` ships the cross-platform **union** of names, while the `config.yaml` `worc install` generates holds only the **host OS** default — so the two files legitimately differ, and copying a list between machines can drop a name the new host needs.

Blocks appear in the packaged order, here and across the pages listed below. **Every block is optional to the loader** — omit any one and it takes the defaults shown. Only one thing is structurally mandatory: exactly one `agents.providers.<id>.primary: true`, so a config with no `agents.providers` map is rejected. `repo` is optional in the same technical sense but not in practice — its defaults (`url: ""`, `local_path: "./workspace/repo"`) are placeholders, so a usable config always sets it.

## Where the rest of the fields are

The reference is split by concern, so a page you open to answer one question is not the whole config. Every field is documented in exactly one of these; the cross-field rules at the end of this page apply across all of them.

- **[agents.md](agents.md)** — `agents`: providers, the fix budgets, decomposition, transient-failure retry, and each provider's CLI settings.
- **[security.md](security.md)** — `security` and `validation`: the guardrail block (environment allowlist, isolation, the dangerous-diff gate, protected paths) and the task-input hardening gate.
- **[checks.md](checks.md)** — `checks`: the quality gate and its per-project command sets.
- **[supervisor.md](supervisor.md)** — `supervisor`: the oversight layer, its cadence, and what each `observe.mode` costs.
- **[runtime.md](runtime.md)** — `telegram`, `skills`, `logging`, `memory`, `tools`, `prompt_audit`: notifications, artifact retention, memory, and the remaining top-level keys.

## `schema_version`

| Field | Type | Default | Constraint | Meaning |
| --- | --- | --- | --- | --- |
| `schema_version` | int | current is `38` | A value **greater** than the orchestrator's supported version fails closed ("upgrade wastech-orchestrator"); equal or lower is accepted, absent is accepted. | The config format version. `worc upgrade-config` re-emits the file at the current version. |

## `orchestrator` — the watch loop and task queue

| Field | Type / values | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `orchestrator.auto_mode.enabled` | bool | `false` | — | `true` lets `watch` pick the next pending task automatically after one finishes (task chaining). Leave off for one-task-at-a-time. |
| `orchestrator.auto_mode.confirm_next_task` | bool | `false` | **Requires `telegram.enabled: true`.** | `true` asks approve/deny in Telegram before claiming _each_ next task; deny/timeout/no-transport stops chaining (fail-closed). Gates new claims only — never a resume. |
| `orchestrator.poll_interval_seconds` | int | `300` | `>= 0` | Seconds between `watch` ticks (each tick fetch/pulls `base_branch`, then processes pending). `0` = single pass, no loop, no periodic sync. |
| `orchestrator.queue` | string | `"default"` | Non-empty / non-whitespace. | This instance's selector: `watch` only claims a pending task whose `queue` equals this (string equality). Set it when several worc instances share one task pool. Override per launch with `--queue`. |

## `repo` — repository identity and branch policy

| Field | Type / values | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `repo.url` | string | `""` | — | The Git remote the orchestrator pushes to. `install` fills it in from your clone's `origin`. It is also what pins every `gh` call to a repository (`--repo owner/name`), so a rewritten `~/.config/gh/hosts.yml` or `url.*.insteadOf` cannot retarget a pull request; when it names no hosted repository the clone's `origin` answers instead, read once before any agent starts. A URL that names none either way — an ssh **alias** (`git@myhost:o/n.git`), a `file://` URL, a local path — leaves every call unpinned and the open-PR probe unasked; `worc preflight` prints that as its own `gh-repo-pin` line (a failure when `create_pull_request` is on, a warning otherwise). |
| `repo.local_path` | string | `"./workspace/repo"` | — | The local clone the orchestrator operates in. |
| `repo.base_branch` | string | `"main"` | — | Branch tasks fork from, and (by default) return to after cleanup. |
| `repo.branch_prefix` | string | `"worc"` | — | Task branch naming: `worc/<task-id>-<slug>`. Leave default unless the project mandates another prefix. |
| `repo.branch_mode` | `new` \| `existing` \| `current` | `new` | — | Instance default for where task git ops point (a per-task `branch_mode` overrides it). `new` = fork a fresh branch from base (the only mode where destructive git ops run); `existing` = a named pre-existing branch; `current` = the working-tree branch as-is (no create/switch/clean-check). |
| `repo.checkout_base_on_cleanup` | bool \| null (tri-state) | `null` | — | Whether terminal cleanup returns the tree to `base_branch`. `null` = defer to `branch_mode` (`new` returns; `existing`/`current` stay); `false` = never return (global off, incl. `new`); `true` = force `new`+`existing` to return. `current` always stays. |

## `paths` — where the task lifecycle lives

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `paths.tasks_dir` | string | `"tasks"` | Repo-relative (no absolute, `~`, or `..`); must **not** live under `.worc/`. Lifecycle subfolder names (`preparing`/`pending`/`done`/`failed`) are fixed. | The repo-relative dir holding the task lifecycle. Rename it only to avoid clashing with a repo that already uses `tasks/`. `install` scaffolds the default `tasks/`; for another name, create its subfolders yourself. |

## `git` — publishing and audit trail

| Field | Type / values | Default | Meaning / when to use |
| --- | --- | --- | --- |
| `git.create_pull_request` | bool | `true` | `false` = commit + push the task branch only, open no PR. |
| `git.pr_base` | string | `"main"` | The branch the published PR targets (usually `base_branch`). |
| `git.auto_merge` | bool | `false` | **DANGER — bypasses the human review gate.** `true` merges every published PR to `pr_base`. A per-task `auto_merge` wins outright over this. Enable only with protected branches + required CI already enforcing your bar. |
| `git.auto_merge_strategy` | `merge` \| `squash` \| `rebase` | `squash` | The `gh pr merge` strategy when a merge fires. |
| `git.auto_merge_wait_for_checks` | bool | `false` | `true` arms GitHub-native auto-merge (`--auto`) — merge only after required checks pass. |
| `git.merge_flow` | string | `"merge"` | The flow `worc merge-task` runs to resolve base-merge conflicts (seeded at `.worc/flows/merge.yaml`). Clean merges are mechanical; only a conflicting base-merge runs it. |
| `git.footprint.audit_commit_message` | string | `"chore(orchestrator): audit trail for {task_id}"` | Template for the separate audit commit (the task file + its `<id>.summary.md`, not a second code commit). |
| `git.footprint.audit_on_branch` | `task` \| `sibling` | `task` | `task` = audit commit on the same branch as the code; `sibling` = on `<branch>-audit`. |

### When the task branch already exists on the remote

Publishing does not assume the branch on `origin` got there by us. What it holds decides what happens:

- **It matches our commit** — nothing is sent, and the operation is recorded as done.
- **It is behind us** — an ordinary push. (This is the case that used to be recorded as published with nothing sent, which mattered most when `branch_name` came from the task file or you chose the branch yourself.)
- **It diverged, and it is exactly the commit we recorded pushing** — a lease-guarded force-push replaces our own stale push, and nothing else.
- **It diverged from something we never pushed** — those commits are merged in **locally**, the quality gate is run again over the combination, and only if it passes does anything reach `origin`; the pull request then says which commits were adopted. Nothing is pushed when those checks fail — the task is parked with the combination on disk and your branch on the remote untouched. With `publish: push` or `commit` there is no pull request to carry that sentence, so it is said in the run log and in the ⚠️ Telegram trace instead. A merge conflict here stops the task for you as well: resolving one needs an agent, and publishing runs after the agent is gone, so the working tree is restored and the task is parked (`worc merge-task` is the way through).

A push is also refused outright if the destination of `origin` changed during the task — a rewritten remote URL, `insteadOf`/`pushInsteadOf` or `pushurl`. The message names the host and path it would have gone to, with any credentials stripped. The comparison is against where a push would have gone when the task's branch was prepared, recorded then and kept, so it holds for `worc merge-task` too — which runs later, in a different process, after the agent has been and gone.

## Cross-field rules and gotchas (read before you finish)

- **Exactly one `primary`.** One `agents.providers.<id>.primary: true`, and that provider must be in `agents.allowed`.
- **Reasoning is per-provider.** Claude accepts `{low, medium, high, xhigh, max}`; Codex accepts `{minimal, low, medium, high, xhigh, max}` (`max` maps to `xhigh`). A value that validates for one provider can be rejected for the other — including on each of `supervisor.observe.reasoning` / `.finalize.reasoning` / `.handoff.reasoning` against the resolved supervisor provider (one provider serves all three phases, so each is checked against it).
- **Telegram-gated fields.** `orchestrator.auto_mode.confirm_next_task` and any provider `max_turns_gate` require `telegram.enabled: true`.
- **Ordering constraints.** `max_total_fix_iterations >= max_fix_cycles`; `retry.max_delay_s >= retry.base_delay_s`; `decomposition.max_subtasks >= 2`.
- **Replace-not-extend.** `allowed_environment`, `denied_read_paths`, `denied_commands` replace their defaults wholesale. `extra_environment` is **not** one of them — it has no default to replace, so writing it only ever adds the names you list. For `allowed_environment` that also means the generated `config.yaml` (host OS default) and the shipped `config.example.yaml` (cross-platform union) differ by design; `PATH` is mandatory in either, and on Windows `SystemRoot` is a launch-critical FAIL in preflight and at `run`/`watch`/`rerun` start. Advanced mode widens agent-side children only: orchestrator-owned `git`/`gh` keeps this list.
- **Full access is not available at all.** Codex `--sandbox danger-full-access` and Claude `--permission-mode bypassPermissions` are rejected wherever they can appear — a provider's `extra_args`, a flow node's `extra_args`, and the argv build — at every value of `strict_isolation`. The `agents.providers.<provider>.sandbox` key that used to carry the Codex half is gone as of config v38, and a config that still has it fails to load as an unknown key: `upgrade-config` does not remove it, so delete the line by hand.
- **Install vs dataclass defaults differ** for a few fields: `memory.enabled` (`true`), `skills.dynamic` (`false`), provider `model`/`reasoning`, and `supervisor` (provider and every phase model pinned to the primary; `observe.reasoning` delivered as `low`, the other phases as `high`). The table shows both.
- **Comments are stripped on upgrade.** `worc upgrade-config` preserves values but re-emits the file without inline comments — keep the _reason_ for an unusual value recoverable elsewhere.

After editing: run `worc preflight` (providers, isolation, environment, Telegram) and `worc validate-flow --all` (flows are not checked by preflight — a config edit can invalidate a flow). Treat config editing as done only when both are green.

Preflight owns the full report for every verdict that depends on **this host**, which is why one config file can be valid everywhere and still not run here. `allowed-environment: FAIL` is that shape: on a Windows host it reports an `allowed_environment` missing `SystemRoot`; the complete failure signature and reason live in the `allowed_environment` row of [`security.md`](security.md). `run`/`watch`/`rerun` repeat this launch-critical gate so a config edited after preflight still fails before work. The host-independent half — `PATH` must be covered — is a load error instead. The `allowed-environment:` pattern report says what each pattern matched **here**, names anything dropped as secret-bearing, and states which child-process scope it describes; a clean expansion is INFO, a secret-name drop is WARN, and a resumed run repeats the posture before launching more work. `assigned-paths:` resolves symlinks, case aliases, provider homes and the env-file, and may repair the clone-local `.git/info/exclude`; preflight runs no task, but it is not filesystem-read-only.

Preflight also asks each provider's CLI whether it holds stored credentials and reports it as `auth=logged_in (…)` / `auth=logged_out` / `auth=unknown`. A provider reporting `logged_out` fails preflight **whatever its role** — a fallback that cannot start is not a fallback, and its silence is only discovered at the moment it is needed — and `worc run`, `worc watch` and `worc rerun` refuse to start for the same reason. Fix it by logging that CLI in, or by removing it from `agents.allowed` if this host does not use it. Note two things about that answer: it reports credential _presence_, not validity (an expired token still reads as present until a real call fails), and on macOS the CLIs resolve credentials through the Keychain via `$USER`, so trimming that name out of `security.allowed_environment` makes a logged-in CLI report logged out. An `unknown` answer only warns — a probe that cannot answer never blocks a run.

# `config.yaml` — complete field reference

**You are an operator (or an agent helping one) configuring wastech-orchestrator.** This is the complete, self-contained reference for every `config.yaml` field — its allowed values, its default, its constraints, and when to change it. You do not need the internet or the repo's own `docs/` to fill in a config. The copy-paste template carrying these same fields is `config.example.yaml` (`worc install` copies it verbatim into `.worc/`, beside the `config.yaml` it generates); this file explains what each one means. For the _how-to_ walkthrough ("build in this order") see [README.md](README.md); for safe defaults see [best-practices.md](best-practices.md).

Two rules apply everywhere:

- **Unknown keys fail closed.** A key the schema does not know — at the top level or inside any block — is a hard load error (a few long-removed keys are silently tolerated for back-compat). Do not invent fields.
- **Three list fields _replace_, never extend, their defaults:** `security.allowed_environment`, `security.denied_read_paths`, `security.denied_commands`. If you write one, write the whole list you need. Note what that means for `allowed_environment` in particular: `config.example.yaml` ships the cross-platform **union** of names, while the `config.yaml` `worc install` generates holds only the **host OS** default — so the two files legitimately differ, and copying a list between machines can drop a name the new host needs.

Blocks appear below in the packaged order. **Every block is optional to the loader** — omit any one and it takes the defaults shown. Only one thing is structurally mandatory: exactly one `agents.providers.<id>.primary: true`, so a config with no `agents.providers` map is rejected. `repo` is optional in the same technical sense but not in practice — its defaults (`url: ""`, `local_path: "./workspace/repo"`) are placeholders, so a usable config always sets it.

## `schema_version`

| Field | Type | Default | Constraint | Meaning |
| --- | --- | --- | --- | --- |
| `schema_version` | int | current is `36` | A value **greater** than the orchestrator's supported version fails closed ("upgrade wastech-orchestrator"); equal or lower is accepted, absent is accepted. | The config format version. `worc upgrade-config` re-emits the file at the current version. |

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
| `repo.url` | string | `""` | — | The Git remote the orchestrator pushes to. |
| `repo.local_path` | string | `"./workspace/repo"` | — | The local clone the orchestrator operates in. |
| `repo.base_branch` | string | `"main"` | — | Branch tasks fork from, and (by default) return to after cleanup. |
| `repo.branch_prefix` | string | `"worc"` | — | Task branch naming: `worc/<task-id>-<slug>`. Leave default unless the project mandates another prefix. |
| `repo.branch_mode` | `new` \| `existing` \| `current` | `new` | — | Instance default for where task git ops point (a per-task `branch_mode` overrides it). `new` = fork a fresh branch from base (the only mode where destructive git ops run); `existing` = a named pre-existing branch; `current` = the working-tree branch as-is (no create/switch/clean-check). |
| `repo.checkout_base_on_cleanup` | bool \| null (tri-state) | `null` | — | Whether terminal cleanup returns the tree to `base_branch`. `null` = defer to `branch_mode` (`new` returns; `existing`/`current` stay); `false` = never return (global off, incl. `new`); `true` = force `new`+`existing` to return. `current` always stays. |

## `paths` — where the task lifecycle lives

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `paths.tasks_dir` | string | `"tasks"` | Repo-relative (no absolute, `~`, or `..`); must **not** live under `.worc/`. Lifecycle subfolder names (`preparing`/`pending`/`done`/`failed`) are fixed. | The repo-relative dir holding the task lifecycle. Rename it only to avoid clashing with a repo that already uses `tasks/`. `install` scaffolds the default `tasks/`; for another name, create its subfolders yourself. |

## `agents` — providers, fix budgets, decomposition, retry

| Field | Type / values | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `agents.allowed` | list of `claude` / `codex` | `[claude, codex]` | Each must be a known provider; must include the `primary` provider. | The providers this instance may launch. Keep it to what is actually installed. |
| `agents.max_stage_attempts` | int | `3` | — | Provider attempts for one node before the stage fails, counting the cross-provider fallback hop (separate from `agents.retry`). |
| `agents.max_fix_cycles` | int | `15` | — | Max iterations of a single fix loop (e.g. test → fixing → test). |
| `agents.max_total_fix_iterations` | int | `30` | Must be `>= max_fix_cycles`. | Hard global cap across all fix loops and subtasks. |

### `agents.decomposition` — split a task into subtasks

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `agents.decomposition.enabled` | bool | `false` | — | Global gate that _permits_ decomposition (a per-task `decomposition` field overrides it; the flow + planning still decide whether a split actually happens). Turn on once the team is ready for one-task-many-subtasks planning. |
| `agents.decomposition.max_subtasks` | int | `8` | Must be `>= 2`. | A proposed split with more than this many subtasks (or fewer than 2) is rejected and the task runs as a single unit. |

### `agents.retry` — transient provider-failure recovery

Applies per provider in `[primary, fallback]`; only transient classes (`PROVIDER_UNAVAILABLE` / `NETWORK_UNAVAILABLE`) are retried. Leave at defaults unless a provider is flaky on your host.

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `agents.retry.max_attempts` | int | `2` | `>= 0` (`0` disables retry) | Same-provider retries _after_ the first attempt. |
| `agents.retry.base_delay_s` | float | `2.0` | `>= 0` | Exponential-backoff base: `min(base * 2**k, max_delay_s)`, no jitter. |
| `agents.retry.max_delay_s` | float | `30.0` | `>= 0` and `>= base_delay_s` | Per-retry delay cap. |
| `agents.retry.max_blocked_s` | float | `21600.0` (6h) | `>= 0` | Park ceiling: once every provider is exhausted and **any** attempt reported an outage _or_ a rate-limit, the task parks resumable and fails only after this much total parked wall-clock. A fallback provider failing on something worse (expired credentials, say) does not cancel the park. When a provider reports the instant its own limit window reopens, the task idles until then instead of retrying every poll — that only ever _shortens_ the wait, and this ceiling still ends it. `worc top` / `worc status` show the wake instant. |

### `agents.providers.<id>` — per-provider CLI settings

`<id>` is `codex` or `claude`. Values marked "install" differ from the bare dataclass default when written by `worc install`.

| Field | Type / values | Default (dataclass / install) | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `.command` | string | the id (`"claude"` / `"codex"`) | — | Executable name on `PATH`, or an absolute path to the CLI. |
| `.model` | string | `""` / install: claude `claude-sonnet-5`, codex `gpt-5.4` | Passed through unverified. `""`/blank = the CLI/account default. | Pin a model. Do not invent model ids. |
| `.timeout_seconds` | int | `7200` | — | Per-attempt CLI wall-clock ceiling (seconds). |
| `.permission_profile` | `read-only` \| `workspace-write` | `workspace-write` | A flow node's ceiling may lower it, never raise it. | Access level for this provider's runs. Claude: `read-only`→`dontAsk` (Read/Glob/Grep only), `workspace-write`→`acceptEdits` (+Edit/Write/Bash). On macOS/Linux/WSL2 a workspace-write Claude also runs its OS Bash sandbox; native Windows runs a Bash-less restricted mode. **Codex** generates a per-attempt permission profile at this level that denies the private/control homes + secrets + `CODEX_HOME`, keeps the exchange and resolved Git dirs read-only, disables network, and is proven by a no-model `codex sandbox` canary before each run (fails closed under `strict_isolation`). The canary isolates user configuration with a temporary `CODEX_HOME` on POSIX; on native Windows it retains the configured/default home because the Windows sandbox keeps its accounts and capability grants there, while the inline generated profile remains authoritative. The separate MCP inventory always uses an empty temporary home. |
| `.extra_args` | list[str] | `[]` | Sandbox/permission-weakening flags **and** authority-bearing Claude flags (tools/settings/MCP/plugins/agents/add-dir/Chrome/IDE/worktree/system-prompt/session) are **rejected at config time**. | Raw CLI flags appended verbatim — for benign options only. |
| `.sandbox` | `danger-full-access` \| null | `null` | Codex-only. `danger-full-access` loads but is **rejected at preflight unless `security.strict_isolation: false`**; `read-only`/`workspace-write` are **rejected at config time** (use `permission_profile` — `upgrade-config` migrates them). | The operator's full-access **escape only** (removes Codex isolation). The access level itself is no longer set here — use `permission_profile`. |
| `.max_turns` | positive int, or `"none"` / `"max"`, or null | `400` | `<= 0` or other strings are errors. | Claude turn cap. `none`/`max`/`null` = no cap. Pair a low cap with `max_turns_gate` for a Telegram continue prompt. |
| `.reasoning` | string \| null | `null` / install: `high` | **Per-provider set:** Claude ∈ `{low, medium, high, xhigh, max}`; Codex ∈ `{minimal, low, medium, high, xhigh, max}` (`max`→`xhigh`). | Reasoning effort. A value valid for one provider may be invalid for the other. |
| `.primary` | bool | `false` | **Exactly one** provider across the map must be `true`, and it must be in `agents.allowed`. | The global primary: runs every flow node with no explicit `provider`, and is the sole infrastructure-fallback target. |
| `.max_turns_gate` | bool | `false` | **Requires `telegram.enabled: true`.** Claude-only. | `true` = hitting `max_turns` pauses for a Telegram continue/stop prompt (resumes the same session) instead of failing. Makes a low `max_turns` safe. |
| `.allow_native_memory` | bool | `false` | Claude-only; installer never writes it. | **Opt-in risk.** `true` drops the deny that confines Claude Code's own auto-memory, letting it persist across tasks in a HOME store **outside** the orchestrator's redaction net and audit trail. This is the **only** switch that lifts the *write* side of that deny — `disable_read_isolation` lifts only the read side — so the store stays unwritable until you set this. With it on, each run logs a `native Claude memory ON` warning, the same way the other relaxations announce themselves. Leave off unless you accept that. |

## `security` — the guardrail block

| Field | Type / values | Default (dataclass / install) | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `security.strict_isolation` | bool | `true` | — | Fail-closed sandbox. `true` **rejects full-access provider modes at preflight** (Codex `danger-full-access`, Claude `bypassPermissions`) and, host-aware, a **workspace-write Claude on a Linux/WSL2 host missing `bubblewrap`+`socat`** (its Bash sandbox is unavailable). Set `false` only to consciously accept full-access / unsandboxed runs. Absolutely-forbidden flags (`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--yolo`, `--ignore-rules`) stay banned regardless. |
| `security.disable_read_isolation` | bool | `true` (read-isolation **off** out of the box; set `false` to keep it on) | Operator-config only (never a task / flow / `extra_args` key). | Escape hatch that fully disables **read**-isolation for provider runs: Claude reloads native `CLAUDE.md` + project settings/hooks/MCP/skills (`--setting-sources project`, no `--strict-mcp-config`) and Codex reloads the user + project `.codex` config/hooks/rules; the private read-deny projection (`.worc`/env-file/provider homes/frozen bundles) is lifted. The **write** side (exchange/`.git`/`tasks/` write-deny, commit/staging gates, PR control) and the `denied_read_paths` blacklist stay in force — including the write-deny on Claude's own config home, which only `agents.providers.claude.allow_native_memory` lifts. `strict_isolation` is the master switch and always wins toward relaxation — effective off = `disable_read_isolation OR NOT strict_isolation`, so `strict_isolation: false` forces it on. `preflight` and the run log announce `read-isolation: OFF` when in effect (never a silent weakening). Native Windows Codex requires the elevated sandbox backend to enforce the private read-deny projection; without it, read-isolation **on** fails closed during preflight or the pre-launch canary instead of running with unproven isolation. |
| `security.allow_git_evidence` | bool | `false` (the grant is **off**; a declaring flow runs as if it had not declared) | Operator-config only (never a task / flow / `extra_args` key). | Master switch for the **read-only git-evidence grant**. A flow node that sets `git_evidence: true` may then run the read-only git verbs — `log`, `show`, `diff`, `blame`, `status`, `rev-list`, `rev-parse`, `ls-files`, `shortlog`, `describe`, `cat-file`, `for-each-ref` — so an audit node can cite the commit that closed a milestone instead of grepping a changelog and calling it delivery history. The declaration and this switch are both required: a flow can express the need but cannot grant itself the capability, and turning this on hands a shell only to the nodes that asked, never to every read-only node in the run. It does **not** make the node writable — Claude scopes the shell to those verbs and write-denies the whole clone in its OS sandbox (and refuses the attempt outright on a host where it cannot sandbox a shell), Codex's `read-only` sandbox already forbids every mutation, `denied_commands` stays the floor beneath both, and commit/push/PR remain the orchestrator's alone. **Read "master switch" as a grant switch, not a kill switch:** off does not mean "no node can read git history anywhere". It means "no node *gains* a capability it did not already have" — and a Codex `read-only` node can run `git log` today regardless, because its sandbox permits commands (its mutation ban comes from the workspace being mounted `read` with the network off, not from a verb list). So with this off, the same flow still has different reach depending on which provider runs the node; turning it on is what makes them match. On Claude the switch is the whole shell, which is why the asymmetry is visible there and not here. `preflight` and the run log announce `git-evidence: ON` when in effect. |
| `security.allowed_environment` | list[str] | OS-aware base (`PATH`, `HOME`, `USER`, `USERPROFILE`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR` + OS-launch essentials): 9 names on Linux/macOS, 19 on Windows | **REPLACES** the default. Must list `PATH` (spelled exactly) — a config without it is rejected at load. | The only env var _names_ forwarded to child processes (a name absent from the host is skipped). Keep the OS-launch essentials for your OS or the CLI may fail to start; secret **values** never go here. What `install` writes is the **host OS** default, not the 22-name cross-platform union in `config.example.yaml` — that union is the template `worc upgrade-config` merges from, and it is safe to keep as-is on any OS. Two names are checked so a trimmed list cannot fail silently: missing `PATH` is a load error (nothing would find the agent CLI or `git`), and on a Windows host a missing `SystemRoot` is a `worc preflight` FAIL — without it the Node-based `claude.exe` aborts at startup with exit `0xC0000409` before printing anything, so a run reports only that the CLI "did not succeed". |
| `security.extra_environment` | mapping[str, str] | `{}` (no default to replace — the key either assigns something or it does not) | Values must be **quoted strings** (`1` is a YAML int and is refused, with a hint); an empty string is a valid value. `PATH` in any case, a secret-looking name, a name outside `[A-Za-z_][A-Za-z0-9_]*`, and two names differing only in case are all rejected at load. | Variables the orchestrator **assigns** to every child process it starts — the agent CLI, the Check Runner, the dependency scanners, `git`/`gh`. This is the complement of `allowed_environment`: forwarding passes on a name whose value comes from your shell (unset on the next machine, and a forgotten `export` is skipped in silence), while this key pins the value itself. Use it for toolchain roots and cache paths (`DOTNET_ROOT`, `NUGET_PACKAGES`, `npm_config_cache`). Applied after forwarding, so a name in both wins here. **Never put credentials in it:** the value sits in `config.yaml` in plaintext, nothing can check a value for secrecy, and the agent CLIs read their own credentials from their own stores. `worc preflight` prints the names you set, never the values. |
| `security.denied_read_paths` | list of globs | `[".env", "secrets/**"]` | **REPLACES** the default. Same glob dialect as `protected_paths`. | Paths the agent CLI may never read (enforced as `--disallowedTools`), so a task cannot exfiltrate secrets. |
| `security.denied_commands` | list[str] | `["git commit", "git push", "gh pr create", "gh pr merge"]` | **REPLACES** the default. | Commands the agent/checks may never run. Keep every entry you need — publishing is the orchestrator's job, not the agent's. |
| `security.trust_level` | `strict` \| `auto` | `auto` | Per-task `trust_level` overrides it. | Approval policy for the mid-task dangerous-diff gate. `strict` = gate every tracked-file deletion/rename or dependency-manifest/lock edit; `auto` = only a `protected_paths` match asks. Never lowers the hard ceiling — only which diffs raise the gate. |
| `security.protected_paths` | list of repo-relative globs | `[]` | Each must be repo-relative (no absolute/`~`/`..`). | The always-ask floor: any change to a matching path requires approval regardless of `trust_level`. `[]` = no floor. Add sensitive surfaces (e.g. `.github/workflows/**`, `src/security/**`). |

## `validation` — input hardening gate

Rejects a malformed task **before** any branch or agent runs.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `validation.max_task_bytes` | int | `262144` | Reject a task file larger than this. |
| `validation.max_task_lines` | int | `5000` | Reject a task file with more lines. |
| `validation.max_line_bytes` | int | `8192` | Reject any single line longer than this. |
| `validation.max_control_ratio` | float | `0.01` | Reject if control/binary chars exceed this fraction (0.01 = 1%). |
| `validation.quarantine_folder` | string | `"./.worc/tasks/rejected"` | Where rejected task files are moved. |

Two things this block does **not** control, because they are not the operator's to relax (the keys that pretended to be were removed in config v35 — `upgrade-config` strips them): the required front matter (`id`, a non-blank `title`, and a non-empty `Description` section) and the deny on an unrecognized front-matter key. Both are hard-coded in the task gate. `id` becomes a branch fragment, a run directory, and a state-store key, so dropping it would break identity rather than loosen a policy.

## `checks` — the quality gate (per-project command sets)

Empty / omitted `command_sets` means **no gate** (every task passes the checks node). The runner runs the union of sets whose `paths` glob the task diff; a set with no `paths` always runs on a non-empty diff; an empty diff runs nothing. Nothing is auto-discovered — you author this.

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `checks.timeout_seconds` | int | `7200` | `> 0` | Global per-command timeout (argv, no shell). |
| `checks.command_sets` | mapping `<name>` → set | `{}` | (per-set below) | Named per-project sets. Single-root repo: one catch-all set (no `paths`). Monorepo: one set per real path-ownership boundary. **If any flow you run produces documents rather than code** (a `deep_research`-style flow committing Markdown), the catch-all recommendation stops being enough on its own: with no `paths` it fires on that diff too, so a research run pays for the whole code gate — and if the set contains a command that *rewrites* files, that run parks on the green-but-dirtying guard. Either scope the catch-all's `paths` to code, or keep it and add a documents set (e.g. `paths: ["**/*.md"]` running a Markdown format **check**). |

### `checks.command_sets.<name>` — one command set

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `.commands` | list of command specs | — (required) | Non-empty. | The commands in the set. |
| `.paths` | list of repo-relative globs | `[]` | Each non-empty. | Diff-path selectors deciding when the set runs. Empty = always runs on any non-empty diff. `**` crosses dirs, `*` stays within a segment. |
| `.timeout_seconds` | int \| null | `null` (→ global) | If set, `> 0`. | Per-set override of the global per-command timeout (e.g. a slow iOS build). |
| `.skip_if_unavailable` | bool | `false` (fail-closed) | — | `true` = when the toolchain binary is absent, skip loudly (never silently "passed"). Use only for genuinely optional toolchains (e.g. iOS checks on Linux), never to paper over a broken required suite. **It is not an escape hatch for a missing toolchain:** the gate is fail-closed on an incomplete run, so a set that was the *only* one the diff selected and is then skipped leaves nothing run and parks the task at `manual_action_required` — the same place the launch failure would have. When you genuinely want a gate not to run, disable the node per task (`nodes.<checks-node-id>.enabled: false`), do not skip your way there. |

### `checks.command_sets.<name>.commands[]` — one command

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `.argv` | list[str] | — (required) | Non-empty; no shell metacharacters; no forbidden/sandbox-weakening args; must not match a `denied_commands` entry. | The command as an explicit argv list (no shell string), e.g. `[ruff, check, .]`. |
| `.name` | string \| null | `null` | — | A readable logical name (`tests`, `lint`, `types`) — keeps logs legible. |
| `.cwd` | string \| null | `null` (= clone root) | Repo-relative, no `..`/absolute. | Working dir for the command. Use only when it must run below the repo root (monorepo subproject). |

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

## `telegram` — human-in-the-loop and notifications

| Field | Type | Default | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `telegram.enabled` | bool | `false` | — | Enable HITL prompts + notifications. Required by `auto_mode.confirm_next_task` and provider `max_turns_gate`. |
| `telegram.bot_token_env` | string | `"TELEGRAM_BOT_TOKEN"` | Must be a valid env-var name (`^[A-Za-z_][A-Za-z0-9_]*$`). | The env var _name_ holding the bot token (never the token value). |
| `telegram.chat_id_env` | string | `"TELEGRAM_CHAT_ID"` | Same env-name rule; must resolve to a non-zero numeric chat id. | The env var name holding the chat id. |
| `telegram.ask_timeout_s` | int | `28800` (8h) | `> 0` | Blocking HITL timeout — fails closed on timeout. |
| `telegram.trace` | bool | `false` | — | `true` = live per-node progress feed (best-effort; node id + outcome only). |

## `skills` — repo skill selection

At task start the orchestrator discovers every tracked `SKILL.md` in the clone; chosen ones reach a node as read-only reference paths (never executed).

| Field | Type | Default | Meaning / when to use |
| --- | --- | --- | --- |
| `skills.dynamic` | bool | `false` (whole block absent) — but **`true` if the `skills:` block is present without this key** | `true` lets the supervisor propose a node→skills map once per task (adds one turn). Note the asymmetry: omitting `skills:` entirely ⇒ `false`; writing `skills:` with no `dynamic:` ⇒ `true`. `install` writes `false`. |
| `skills.strict` | bool | `false` | Governs operator pins: `false` = warn and skip an unresolved pin; `true` = stop the task (`manual_action_required`). |

## `supervisor` — the oversight layer

A read-only layer above every flow that observes completed nodes and writes the final summary. Its `permission_profile` is forced `read-only` in code. It is on by default and can be removed entirely with `enabled: false`, in which case the pull-request body is rendered deterministically from the run's recorded facts instead.

Three keys are one-per-layer and stay at the top; model and effort are **per phase**, under `observe` / `finalize` / `handoff`.

| Field | Type / values | Default (dataclass / install) | Constraint | When to use |
| --- | --- | --- | --- | --- |
| `supervisor.enabled` | bool | `true` / install: `true` | — | `false` removes the layer: no per-step notes, no summary turn, no subtask handoff brief, no `skills.dynamic` proposal, and every key below inert (one warning says so). The PR body is then written deterministically. Also forces `memory.enabled` to `false` for the run — see [`memory`](#memory--persistent-repo-scoped-memory). |
| `supervisor.role_file` | string | `"roles/supervisor.md"` | No path traversal (`..`/absolute). | The observe-lens prompt. Never loaded when the cadence resolves to `none`. |
| `supervisor.provider` | `codex` \| `claude` \| null | `null` / install: pinned to the primary | Must be in `agents.allowed` when set. | `null` inherits the global primary; pin it so the phase models reach a provider that accepts them. |
| `supervisor.observe.mode` | `all` \| `selected` \| `events` \| `none` | `events` / install: `events` | A flow may only narrow it (see below). | How often a completed step is worth an LLM note. See the table under it. |
| `supervisor.observe.triggers` | list of `rework` \| `failure` \| `fallback` | all three | Closed set; an unknown name is rejected. | Narrows which deviations count under `events` — e.g. `[failure]` to be notified of failures only. |
| `supervisor.observe.include_nodes` | list of node ids | `[]` | — | The nodes observed under `mode: selected`; ignored in every other mode. |
| `supervisor.observe.model` | string \| null | `null` / install: the primary's model | Passed through unverified; a vendor/primary mismatch warns. | `null` = the resolved provider's default. The **cheap** one: this phase is advisory and can fire on every step of a deep fix loop. Also governs the once-per-task skill proposal. |
| `supervisor.observe.reasoning` | string \| null | `null` / install: `low` | Per-provider set (as providers, above). | `null` = the resolved provider's default. Capped to `high` in code even if you set a max tier. |
| `supervisor.finalize.model` | string \| null | `null` / install: the primary's model | As above. | The turn that writes `summary.md` — the pull-request body, and the only part of a long run most readers see. Worth more than `observe`. |
| `supervisor.finalize.reasoning` | string \| null | `null` / install: `high` | Per-provider set. | `null` = the resolved provider's default. A max tier (`xhigh`/`max`) is capped to `high` when the turn is structured. |
| `supervisor.handoff.model` | string \| null | `null` / install: the primary's model | As above. | The subtask brief between regions of a decomposed task. Unused by a flow that never decomposes. |
| `supervisor.handoff.reasoning` | string \| null | `null` / install: `high` | Per-provider set. | `null` = the resolved provider's default. |

Keep every phase **at or below** the producer nodes' tier. This layer is advisory — it never routes, reworks, or blocks — so a model stronger than `agents.providers` inverts the budget: the reasoning that decides the deliverable gets the weaker one.

### What `observe.mode` costs

Ranked by how many calls the mode can produce — which is also the order a flow may narrow along.

| Mode | Observes | Use it when |
| --- | --- | --- |
| `none` | nothing | The flow's quality is already held by a blocking gate. `finalize` and the summary still happen. |
| `events` (default) | only a deviation: an evaluator sending work back (or accepting after exhausting its rework budget), a step whose run failed, a step that fell back to the non-primary provider | Almost always. Cost tracks what went wrong, not how long the run was. |
| `selected` | exactly `include_nodes` | You want notes on two named steps and nothing else. |
| `all` | every executed step | Debugging the run itself. This is what a long run pays for. |

`tool`, `checks` and the terminal `publish` node are never observed under **any** mode — their result is already a durable fact the finalize packet carries verbatim, so an advisory note about a pass/fail bought nothing and cost a full call per run.

**What a mode actually cost you is measured, not guessed.** Each run writes a `supervisor_usage` block into `.worc/logs/<task-id>/summary.json` (local only, never committed): calls, input, cached input, output, cost and provider wall time, as a total and split by job — `observe`, `finalize`, `handoff`, `skill`. Read the `observe` versus `finalize` split on your own flow before tuning this setting; the table above ranks the modes by how many calls they *can* produce, and that block tells you what they did produce.

Switching observations off and removing the layer are two different levels. `observe.mode: none` silences the per-step notes and keeps the synthesis; `enabled: false` removes the layer including that synthesis, and the pull-request body is then rendered from the same recorded facts the packet is built from — the same sections, without the interpretation.

Switching observations off does not cost you the summary. The finalize turn runs on a **fresh** session seeded by a deterministic packet of the run's facts (`.worc-io/<task-id>/supervisor/packet.json`) built from the recorded node runs and each node's own output — never from the observations — so its input is a few kilobytes regardless of how long the run was, a resumed task's summary is as complete as a first run's, and `mode: none` still produces a full PR body. The finalize turn is also handed every in-flow evaluator's recorded verdict and findings, so a gate that accepted **with** findings cannot be summarized as one that simply passed.

A flow may **narrow** the cadence in its own `supervisor.observe.mode` but never widen it: a flow declaring a broader mode than yours fails validation before any node runs, naming both modes (a flow is authored content and must not be able to spend more than you allowed). The packaged content flows ship `none`; `implementation` ships `events`. One consequence worth knowing: because a flow that *states* `events` is asserting it needs deviation notes, setting your global mode to `none` is rejected for that flow rather than silently degrading it — narrow the flow's own copy if that is what you want. The rule does not apply at `enabled: false` — there is no cadence to widen, so a flow that declares `events` runs unchanged. That is why removing the layer is its own key rather than a global `mode: none`.

**Check the change before you queue work against it.** The rejection is fatal but cheap — it happens during flow resolution, before branch prep, so no provider runs and nothing is committed — yet the task has already been claimed by then and ends in terminal `failed`, which you have to re-queue by hand. `worc validate-flow` runs exactly the validator the engine runs at dispatch, read-only and without claiming anything, so run it after editing `observe.mode` on either side:

```bash
worc validate-flow --all && worc watch
```

Exit `0` = every checked flow is valid, `1` = at least one is not, `2` = flow name not found or the config would not load — so `&&` gates the run correctly. Two practical notes: the command needs a flow NAME or `--all` (a bare `worc validate-flow` is a usage error and exits `2`, which would block the chain for the wrong reason), and `--all` checks **every** file in `.worc/flows/`, so one unrelated broken flow there fails the gate. Name the flow you are about to run — `worc validate-flow implementation && worc watch` — when that is a problem.

There is no cap on what the layer may spend beyond the mode itself: no call budget, no token ceiling. The digest the finalize turn reads is bounded deterministically in code (8 000 characters), the mode bounds the frequency, and `all` is a deliberate operator choice.

## `logging` — operator verbosity and artifact retention

| Field | Type / values | Default | Meaning |
| --- | --- | --- | --- |
| `logging.level` | `debug` \| `info` \| `warning` \| `error` | `info` | Operator trace verbosity. The `--log-level` CLI flag overrides it. |
| `logging.artifacts` | `minimal` \| `standard` \| `full` | `standard` | Per-attempt provider files kept: `minimal` = `result.json` only; `standard` = + stdout/stderr; `full` = everything. Reclaim disk with `worc logs clean`. |
| `logging.clean_runs_on_success` | boolean | `true` | A task that finishes **successfully** evicts its own per-task state under `.worc/runs/` (frozen control + instruction bundles, sealed exchanges). Failed / parked / manual-action tasks and quarantined exchange evidence are never cleaned automatically. Set `false` to keep every run for analysis and reclaim on demand with `worc runs clean` (available either way). Per-task log dirs are out of scope — those stay with `worc logs clean`. See [footprint.md](../footprint.md). |

## `memory` — persistent, repo-scoped memory

Omitting the whole block ⇒ `enabled: false` (no store, empty packets, CLI no-op). All numeric knobs are runtime-clamped — never fatal. Defaults are deliberately small (precision over recall).

Memory also requires `supervisor.enabled: true`. That layer's closing turn is the only path that writes anything memory can later read back, so with the layer off memory would keep adding a packet to every prompt without ever learning — `supervisor.enabled: false` therefore resolves `memory.enabled` to `false` for the run and prints a warning naming both keys. Set `memory.enabled: false` yourself to make the file say what runs.

| Field | Type | Default (dataclass / install) | Meaning |
| --- | --- | --- | --- |
| `memory.enabled` | bool | `false` / install: `true` | Global memory toggle. Forced to `false` for the run when `supervisor.enabled` is `false`. |
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
| `prompt_audit` | bool | `false` | Record each step's rendered prompt + who-metadata (provider/model/attempt/fallback/status) under `logs/<task-id>/prompt-audit/`. A per-task `prompt_audit` always overrides this. |

## Cross-field rules and gotchas (read before you finish)

- **Exactly one `primary`.** One `agents.providers.<id>.primary: true`, and that provider must be in `agents.allowed`.
- **Reasoning is per-provider.** Claude accepts `{low, medium, high, xhigh, max}`; Codex accepts `{minimal, low, medium, high, xhigh, max}` (`max` maps to `xhigh`). A value that validates for one provider can be rejected for the other — including on each of `supervisor.observe.reasoning` / `.finalize.reasoning` / `.handoff.reasoning` against the resolved supervisor provider (one provider serves all three phases, so each is checked against it).
- **Telegram-gated fields.** `orchestrator.auto_mode.confirm_next_task` and any provider `max_turns_gate` require `telegram.enabled: true`.
- **Ordering constraints.** `max_total_fix_iterations >= max_fix_cycles`; `retry.max_delay_s >= retry.base_delay_s`; `decomposition.max_subtasks >= 2`.
- **Replace-not-extend.** `allowed_environment`, `denied_read_paths`, `denied_commands` replace their defaults wholesale. `extra_environment` is **not** one of them — it has no default to replace, so writing it only ever adds the names you list. For `allowed_environment` that also means the generated `config.yaml` (host OS default) and the shipped `config.example.yaml` (cross-platform union) differ by design; `PATH` is mandatory in either, and on Windows `SystemRoot` is a preflight FAIL.
- **Full access needs `strict_isolation: false`.** Codex `danger-full-access` / Claude `bypassPermissions` load but are rejected at preflight unless you turn `strict_isolation` off (owning the risk).
- **Install vs dataclass defaults differ** for a few fields: `memory.enabled` (`true`), `skills.dynamic` (`false`), provider `model`/`reasoning`, and `supervisor` (provider and every phase model pinned to the primary; `observe.reasoning` delivered as `low`, the other phases as `high`). The table shows both.
- **Comments are stripped on upgrade.** `worc upgrade-config` preserves values but re-emits the file without inline comments — keep the _reason_ for an unusual value recoverable elsewhere.

After editing: run `worc preflight` (providers, isolation, environment, Telegram) and `worc validate-flow --all` (flows are not checked by preflight — a config edit can invalidate a flow). Treat config editing as done only when both are green.

Preflight owns every verdict that depends on **this host**, which is why one config file can be valid everywhere and still not run here. `allowed-environment: FAIL` is that shape: on a Windows host it reports an `allowed_environment` missing `SystemRoot` and names the exit code (`0xC0000409`) you would otherwise have to recognize from a CLI that printed nothing. The host-independent half of the same rule — `PATH` is mandatory — is a load error instead, so that the same file gets the same answer on every machine.

Preflight also asks each provider's CLI whether it holds stored credentials and reports it as `auth=logged_in (…)` / `auth=logged_out` / `auth=unknown`. A provider reporting `logged_out` fails preflight **whatever its role** — a fallback that cannot start is not a fallback, and its silence is only discovered at the moment it is needed — and `worc run`, `worc watch` and `worc rerun` refuse to start for the same reason. Fix it by logging that CLI in, or by removing it from `agents.allowed` if this host does not use it. Note two things about that answer: it reports credential _presence_, not validity (an expired token still reads as present until a real call fails), and on macOS the CLIs resolve credentials through the Keychain via `$USER`, so trimming that name out of `security.allowed_environment` makes a logged-in CLI report logged out. An `unknown` answer only warns — a probe that cannot answer never blocks a run.

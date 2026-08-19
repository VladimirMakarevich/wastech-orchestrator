# `agents` — providers, budgets, decomposition, retry

**You are an operator (or an agent helping one) configuring wastech-orchestrator.** This page documents the `agents` block: which CLIs may run, which one is primary, how many fix cycles a task gets, whether decomposition is allowed, how a transient provider failure is retried, and each provider's own settings.

For the fields not on this page see [reference.md](reference.md), which also carries the cross-field rules that apply across blocks; for the how-to walkthrough see [README.md](README.md) and for safe defaults [best-practices.md](best-practices.md).

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
| `.permission_profile` | `read-only` \| `workspace-write` | `workspace-write` | A flow node's ceiling may lower it, never raise it. | Access level for this provider's runs. Claude: `read-only`→`dontAsk` (Read/Glob/Grep only), `workspace-write`→`acceptEdits` (+Edit/Write/Bash). On macOS/Linux/WSL2 a workspace-write Claude also runs its OS Bash sandbox; native Windows runs a Bash-less restricted mode. Where no OS sandbox exists at all (native Windows; Linux/WSL2 without `bubblewrap`+`socat`) preflight and the run log print a loud line saying so — it never blocks the run. What stops an unsandboxed shell is the attempt itself: under `strict_isolation: true` a node that would keep one is refused (`CAPABILITY_UNAVAILABLE`) or loses the shell when it launches, so the cost of a broken sandbox host falls on that node, not on the whole run. **Codex** generates a per-attempt permission profile at this level that denies the private/control homes + secrets + `CODEX_HOME`, keeps the exchange and resolved Git dirs read-only, disables network, and is proven by a no-model `codex sandbox` canary before each run (fails closed under `strict_isolation`). The canary isolates user configuration with a temporary `CODEX_HOME` on POSIX; on native Windows it retains the configured/default home because the Windows sandbox keeps its accounts and capability grants there, while the inline generated profile remains authoritative. The separate MCP inventory always uses an empty temporary home. |
| `.extra_args` | list[str] | `[]` | Sandbox/permission-weakening flags **and** authority-bearing Claude flags (tools/settings/MCP/plugins/agents/add-dir/Chrome/IDE/worktree/system-prompt/session) are **rejected at config time**. | Raw CLI flags appended verbatim — for benign options only. |
| `.max_turns` | positive int, or `"none"` / `"max"`, or null | `400` | `<= 0` or other strings are errors. | Claude turn cap. `none`/`max`/`null` = no cap. Pair a low cap with `max_turns_gate` for a Telegram continue prompt. |
| `.reasoning` | string \| null | `null` / install: `high` | **Per-provider set:** Claude ∈ `{low, medium, high, xhigh, max}`; Codex ∈ `{minimal, low, medium, high, xhigh, max}` (`max`→`xhigh`). | Reasoning effort. A value valid for one provider may be invalid for the other. |
| `.primary` | bool | `false` | **Exactly one** provider across the map must be `true`, and it must be in `agents.allowed`. | The global primary: runs every flow node with no explicit `provider`, and is the sole infrastructure-fallback target. |
| `.max_turns_gate` | bool | `false` | **Requires `telegram.enabled: true`.** Claude-only. | `true` = hitting `max_turns` pauses for a Telegram continue/stop prompt (resumes the same session) instead of failing. Makes a low `max_turns` safe. |
| `.allow_native_memory` | bool | `false` | Claude-only; installer never writes it. | **Opt-in risk.** `true` drops the deny that confines Claude Code's own auto-memory, letting it persist across tasks in a HOME store **outside** the orchestrator's redaction net and audit trail. This is the **only** switch that lifts the *write* side of that deny — `disable_read_isolation` lifts only the read side — so the store stays unwritable until you set this. With it on, each run logs a `native Claude memory ON` warning, the same way the other relaxations announce themselves. Leave off unless you accept that. |


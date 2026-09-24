# Configuration: Agents and Security

Part of the [configuration reference](configuration.md): the two `config.yaml` blocks that decide how a coding agent is launched and what it may reach — `agents` and `security`.

## `agents`

Controls provider availability, retry/fix budgets, decomposition, the global primary, and provider-specific settings.

```yaml
agents:
  allowed:
    - claude
    - codex

  max_stage_attempts: 3
  max_fix_cycles: 15
  max_total_fix_iterations: 30
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `allowed` | list of `claude`, `codex` | `["claude", "codex"]` | Providers the router may use. Every flow node's `provider` (and the global primary) must be in this list. |
| `max_stage_attempts` | integer | `3` | Provider **hops** allowed for a stage (primary + cross-provider fallback). The transient-retry budget (`agents.retry`) is separate and does **not** count against this. |
| `max_fix_cycles` | integer | `15` | Fix cycles for a single local failing loop (test-driven or review-driven, counted separately). |
| `max_total_fix_iterations` | integer | `30` | Hard global fix cap across the whole task and all subtasks. Must be `>= max_fix_cycles`. |

Validation requires `max_total_fix_iterations >= max_fix_cycles`. Node-disable is **per-task only** (`nodes.<node-id>.enabled: false` in a task; see [operations → disabling flow nodes](operations-publishing.md#disabling-flow-nodes-per-task)) — there is no config knob for it. The global `agents.skip_stages` list was **removed in `schema_version` 10**, and the `agents.allow_review_skip` gate in **`schema_version` 13** (per-task disable is by flow node id; the operator owns which nodes are safe to disable — no `review`-special-case). Older configs that still carry either key load fail-open (it is ignored); `upgrade-config` strips it.

### `agents.decomposition`

Off by default. When enabled, planning may propose a sequential subtask list for large tasks; the Core accepts it only under deterministic rules and then runs each accepted subtask through the flow's `sub_flow` region (`implementation → testing → review → fixing`), committing each.

```yaml
agents:
  decomposition:
    enabled: false
    max_subtasks: 8
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | `false` | Enables the planning sub-phase for decomposition. |
| `max_subtasks` | integer | `8` | Maximum accepted split size; must be at least `2`. |

`agents.decomposition.enabled` sets the **default** decomposition gate; whether a split actually happens is decided by the flow's `decomposition:` block and the planning node's gate proposal, accepted only under the deterministic rules. A task may override the _gate_ (not the logic) with the optional `decomposition: true|false` field — task-wins over the global, mirroring `auto_merge`/`prompt_audit` (see [task authoring](task-authoring.md#decomposition)). A task still cannot change `max_subtasks`, the provider, or security settings, and the old `decompose` flag (removed in `schema_version` 11) is unrelated — it forced a split; `decomposition` only permits one. The decorative `min_size_signal` and `commit_per_subtask` keys were **removed in `schema_version` 12** (neither was ever read — the size hint lives in the planning role prompt and every accepted subtask is always committed). Older configs that still carry them load fail-open (the keys are ignored); `upgrade-config` strips them.

### `agents.retry`

Optional (added in `schema_version` 20); absent → the defaults below. Bounds the orchestrator's recovery from a **transient** provider failure — a 5xx / network blip classified `provider_unavailable` or `network_unavailable` (never a quality failure, never a `timeout`). The flow is: retry the **same** provider with exponential backoff → switch to the other allowed provider (symmetric Claude↔Codex) → if **both** are unavailable, **park** the task as resumable instead of failing it, until `max_blocked_s` elapses. A subscription/session **`rate_limited`** is treated the same way at the park stage — it is _not_ tight-retried on the same provider (a rate limit wants a long defer, not a hot loop), but it _is_ fallback-eligible and, if **every** provider is rate-limited, it **parks** (resumable) and waits out the reset window rather than failing or burning the queue.

```yaml
agents:
  retry:
    max_attempts: 2
    base_delay_s: 2.0
    max_delay_s: 30.0
    max_blocked_s: 21600.0
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `max_attempts` | integer | `2` | Same-provider retries **after** the first failed attempt (so `2` ⇒ up to 3 invocations). Applied **per provider** in the `[primary, fallback]` sequence; counted separately from `max_stage_attempts`. `0` disables transient retry. |
| `base_delay_s` | number | `2.0` | Backoff base. Each retry waits `min(base_delay_s * 2**k, max_delay_s)` (deterministic, no jitter). |
| `max_delay_s` | number | `30.0` | Per-retry delay cap. Must be `>= base_delay_s`. |
| `max_blocked_s` | number | `21600.0` | Soft-pause ceiling (total parked wall-clock): a task parked because **every** provider was transiently unavailable **or** rate-limited is failed only after it has stayed parked this long. Default 6h comfortably outlasts a provider's ~5h usage window so a rate-limited task waits out the reset and resumes. |

Validation rejects negative values and `max_delay_s < base_delay_s`. Each attempt (including every retry) is recorded in the `provider_attempts` audit trail. See [operations → provider outage behavior](operations-running.md#provider-outage-behavior) for the operator-facing behavior.

### Provider routing (node-based)

There is no `agents.routing` block. Provider routing lives on the **flow node**: each agent/evaluator node may declare a `provider:` (`codex` | `claude`), and a node with no `provider` runs on the **global primary** — the one configured provider with `primary: true` (see [`agents.providers`](#agentsproviders)). Infrastructure fallback is **symmetric** across the two allowed providers: when a node's primary differs from the global primary, an infrastructure failure falls back to the global primary; when the node already runs on the global primary, it falls back to the **other** allowed provider (Claude↔Codex). With only one allowed provider there is no fallback target — an infrastructure failure is handled by the `agents.retry` same-provider budget, then the soft pause.

A `checks` node and a `publish` node never run an agent (in the packaged `implementation` flow, the nodes `testing` and `publish`). The Check Runner owns the first; the Git Manager owns the second.

Rules (enforced at load/validate and at flow preflight):

- exactly one configured provider must set `primary: true`, and it must be in `agents.allowed`;
- every flow node's declared `provider` must be in `agents.allowed` (rejected at preflight otherwise);
- a legacy `agents.routing` block is tolerated and ignored on load; `upgrade-config` strips it.

Fallback is only for infrastructure errors such as missing binaries, authentication errors, rate limits, provider unavailability, timeouts, process crashes, and invalid provider output. Test failures and review findings go to `fixing`, not fallback.

### `agents.providers`

Configures each provider adapter. Provider adapters are the only layer that knows CLI syntax.

```yaml
agents:
  providers:
    claude:
      command: "claude"
      primary: true # the global primary — runs any node with no provider; sole fallback target
      model: "claude-sonnet-5" # explicit default; "" = use the CLI/account default
      reasoning: high # low | medium | high | xhigh | max
      timeout_seconds: 7200
      max_turns: 400 # positive int = turn cap; "none" or "max" = no cap (unlimited)
      max_turns_gate: false # on: hitting max_turns prompts continue/stop (needs telegram)
      permission_profile: "workspace-write"
      extra_args: []
    codex:
      command: "codex"
      model: "gpt-5.5" # "" = use the Codex CLI/account default
      reasoning: high # minimal | low | medium | high | xhigh | max→xhigh
      timeout_seconds: 7200
      permission_profile: "workspace-write" # codex's isolation knob (a generated permission profile)
      extra_args: []
```

Common fields:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `command` | string | provider id (`"claude"` or `"codex"`) | Executable name or path. On Windows, pin an absolute native `.exe` when npm, the Codex app, or an IDE expose conflicting installations; see [How-To §5](how-to.md#5-fix-conflicting-codex-installations-on-windows). |
| `primary` | boolean | `false` | Marks the **global primary**. Exactly one configured provider must set it; that provider runs any flow node with no explicit `provider` and is the sole infrastructure-fallback target. It must also be in `agents.allowed`. |
| `model` | string | `claude-sonnet-5` (claude), `gpt-5.5` (codex) | Provider model setting. The packaged template ships an explicit default per provider; set `""` to fall back to the CLI/account default. (A flow node may override it per node.) |
| `reasoning` | string or null | `high` (both, in the packaged template) | Provider-specific reasoning effort level. Claude accepts `low`, `medium`, `high`, `xhigh`, `max` and maps it to `--effort`. Codex accepts `minimal`, `low`, `medium`, `high`, `xhigh`, plus legacy `max` mapped to `xhigh`, and passes it as `-c model_reasoning_effort="..."`. Set `null` to omit the override and use the CLI/account default. (A flow node may override it per node.) |
| `timeout_seconds` | integer | `7200` | Timeout for a stage run. |
| `permission_profile` | string (`read-only` \| `workspace-write`) | `"workspace-write"` | Orchestrator access level for this provider's runs (a flow node may lower it, never raise it). Claude maps it to a permission mode + tool set (+ OS Bash sandbox on macOS/Linux/WSL2). **Codex** generates a per-attempt permission profile at this level that denies the private/control home + secrets, keeps the exchange and the resolved Git dirs read-only, disables network, and is re-proven by a no-model `codex sandbox` canary before **every** attempt that gets a shell — at either value of `strict_isolation`, since under `false` the generated profile _is_ the whole local floor. A write that lands is a security violation and fails the attempt before the model is called; a probe that cannot run at all is reported as a host capability gap, never as a pass. See [Proving the isolation claim](#whether-the-policy-is-enforced-here-and-what-reports-it). |
| `extra_args` | list of strings | `[]` | Additional provider CLI arguments after safety validation. |

Provider-specific fields:

| Provider | Field | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `claude` | `max_turns` | integer or `none`/`max` | `400` if omitted | Claude turn cap. A positive integer caps agentic turns; `none` or `max` (case-insensitive), or YAML `null`, means **no cap** — the orchestrator omits `--max-turns` so the CLI runs without a turn limit. A non-positive integer or any other string is rejected (falls back to the default 400). |
| `claude` | `max_turns_gate` | boolean | `false` | When `true`, a run that exhausts `max_turns` (`error_max_turns`) pauses for a durable Telegram **continue/stop** prompt instead of failing immediately. Continue resumes the same agent session with a fresh turn grant; deny / timeout / no answer stops (terminal, as without the gate). Each continue needs a fresh approval, and timeout → STOP bounds an unattended loop (no separate resume cap in v1). With this on, a low `max_turns` (~50–100) is safe — short by default, extendable on demand. Requires `telegram.enabled` (preflight). Claude-only (Codex has no turn cap). |

**Two provider keys that no longer exist.** `agents.providers.codex.sandbox` was removed in `schema_version` **38**: its one remaining value selected a provider full-access mode, and that is now refused outright (below), so there is no value left for it to take. `agents.providers.claude.allow_native_memory` is gone too — the deny it used to lift no longer exists, because the agent CLIs' own config homes (`~/.claude` / `$CLAUDE_CONFIG_DIR`, `$CODEX_HOME`) are outside every deny projection at any value of any key (see [`security`](#security)). Neither is tolerated on load and neither is stripped by `upgrade-config`: a config still carrying one is a **load error** with a message telling you to delete the line, deliberately, because stripping it silently would read as "the key still works, it just does nothing".

#### `extra_args`

`extra_args` appends extra arguments to the provider's CLI invocation, verbatim. It is empty (`[]`) by default, so nothing extra is passed.

**Sources (both operator-authored).** The provider-level list here (`agents.providers.<id>.extra_args`) applies to every run of that provider. A flow node may also declare its own `extra_args` (a packaged flow, or a flow under `.worc/flows/`), applied only to that node — see [Per-node overrides in flows](configuration-flows-supervisor.md#per-node-overrides-in-flows). The two are concatenated (the provider list first, then the node's) and both go through the same validation. A task **cannot** set `extra_args`: task content never builds a command line.

**How it is applied.** Arguments are passed as an argument list, never through a shell — each entry is one literal token, and shell metacharacters (`;`, `|`, `$(...)`) are not interpreted. A flag that takes a value is two entries (`["--add-dir", "../lib"]`) or one with `=` (`["--add-dir=../lib"]`). The list is appended **after** the orchestrator's own flags, so where a CLI resolves duplicates last-wins it can override an earlier value — but it can never override the safety rules below.

**What you can pass.** Anything your installed CLI accepts that does not weaken isolation or approvals — for example an extra working directory, an output/format option, or a config override. Confirm exact flag names against `claude --help` / `codex exec --help`; the orchestrator forwards them unchanged and does **not** check that a flag exists (an unknown flag simply makes the CLI fail, surfaced as a provider error). Illustrative — verify against your CLI version:

```yaml
agents:
  providers:
    claude:
      extra_args: ["--add-dir", "../shared-lib"] # add another working directory
    codex:
      extra_args: ["-c", "tools.web_search=true"] # a Codex -c config override
```

**What is always forbidden.** Flags that disable approvals / sandbox / hook-trust wholesale are refused unconditionally — they cannot be enabled through a config or a flow node, regardless of `strict_isolation`:

| Forbidden argument | Provider | Reason |
| --- | --- | --- |
| any `--dangerously*` flag (`--dangerously-bypass-approvals-and-sandbox`, `--dangerously-skip-permissions`, `--dangerously-bypass-hook-trust`, …) | both | disables approvals / sandbox / hook-trust |
| `--allow-dangerously-skip-permissions` | Claude | enables the permission bypass (same class as `--dangerously-skip-permissions`) |
| `--yolo`, `--ignore-rules` | Codex | disables approvals |
| `--sandbox danger-full-access` (or `-s danger-full-access`) | Codex | discards the generated permission profile wholesale — the clone's `.git` becomes writable and the enforcement canary has no profile left to prove |
| `--permission-mode bypassPermissions` | Claude | drops the permission prompts — no isolation |
| `--sandbox` / `-s` with no value | Codex | malformed: would swallow the next token |

**The two provider full-access selectors are on that list, and there is no setting that takes them off it.** They used to be an operator-selectable escape gated by `strict_isolation`; they are now refused at **every** value of it, because each removes the write floor the whole product rests on. There is no `agents.providers.<id>.sandbox` key any more either, so the field form is gone with the flag form.

Both spellings of a valued flag are recognized — `--flag value` and `--flag=value` — so neither form slips through. `--dangerously-skip-permissions` stays listed separately even though it is functionally the flag form of `bypassPermissions`: keeping the whole `--dangerously*` namespace bright is the parity rule.

**Three layers enforce this, not one.** The config validator rejects the whole config before any task runs; the flow validator applies a **config-independent ceiling** to a node's `extra_args` at flow load; and each adapter's argv builder checks again at launch. A value that reaches neither of the first two still cannot be launched.

**The quieter neighbour: `--permission-mode auto`.** Not forbidden outright — an operator may legitimately restate a mode — but refused whenever it is _weaker_ than the profile the node asked for, checked over the provider config and the flow node's `extra_args` **together**. The same holds for any other `--permission-mode` escalation over the resolved profile (e.g. `acceptEdits` above a `read-only` node).

**Reserved Claude `extra_args` (WRI-002).** Claude flags that would re-open a surface the adapter deliberately closes are **rejected regardless of `strict_isolation`**: `--tools`, `--allowedTools`/`--allowed-tools`, `--disallowedTools`/`--disallowed-tools`, `--settings`, `--setting-sources`, `--mcp-config`, `--strict-mcp-config`, `--add-dir`, `--file`, `--agent`/`--agents`, `--plugin-dir`/`--plugin-url`, `--chrome`/`--no-chrome`/`--ide`/`--remote-control`/`--remote-control-session-name-prefix`, `--bg`/`--background`/`--worktree`/`-w`/`--tmux`, `--system-prompt`/`--system-prompt-file`/`--append-system-prompt`/`--append-system-prompt-file`, `--session-id`/`--fork-session`/`--no-session-persistence`/`--resume`/`-r`/`--continue`/`-c`/`--from-pr`, `--safe-mode`, `--bare`, `--disable-slash-commands`. The orchestrator owns tools/settings/MCP/session — a task or flow cannot supply them. Note the short forms are reserved too, and that `-c` means `--continue` here (under Codex the same token is `--config`, reserved for its own reason below); both are matched in split (`--tools X`) and inline (`--tools=X`) form.

**Reserved Codex `extra_args` (WRI-003).** Codex flags that would select, replace, or weaken the permission profile, config-isolation, workspace, tool, or approval/sandbox policy the adapter owns are likewise **rejected regardless of `strict_isolation`**: `-c`/`--config`, `-p`/`--profile`, `-P`/`--permission-profile`, `-s`/`--sandbox`, the approval/sandbox-mode selectors `--full-auto` and `-a`/`--ask-for-approval`, `--add-dir`, `--ignore-user-config`, `--enable`/`--disable`, `--oss`, `--local-provider`, `--skip-git-repo-check`, `--ephemeral`, `--strict-config`, `-C`/`--cd`, and the output-plumbing flags the adapter sets itself (`--output-schema`, `--json`, `-o`/`--output-last-message`, `--color`). `--full-auto` matters especially: it turns on `--sandbox workspace-write`, and selecting **any** `--sandbox` mode makes Codex stop applying the generated `default_permissions="worc"` profile — the private-file read denials (`.worc`/`.env`/`state.db`) would silently vanish. There is no full-access escape left to reach for: `--sandbox danger-full-access` is absolutely forbidden above, and the `sandbox` config field that once selected it no longer exists.

```yaml
# Neither of these loads, at any value of security.strict_isolation:
extra_args: ["--sandbox", "danger-full-access"] # Codex — refused
extra_args: ["--permission-mode", "bypassPermissions"] # Claude — refused
```

Do not pass secrets through `extra_args`: values become argv tokens and are recorded (redacted) in the request artifact.

**Read-isolation escape hatch — `security.disable_read_isolation`.** This operator-config switch relaxes only the **read** side of the isolation envelope. It **defaults to `true`** — read-isolation is off out of the box, a deliberate deployment-posture choice (set `disable_read_isolation: false` to keep it on). When in effect, providers run their **native project-instruction/config discovery**: Claude reloads `CLAUDE.md` + project settings/hooks/MCP/skills (`--setting-sources project`, and `--strict-mcp-config` is dropped) and Codex reloads the user + project `.codex` config/hooks/rules.

**What it does _not_ open, and this is the correction worth reading:** the private set. `.worc`, the resolved env-file and the frozen `runs/` bundles stay read- **and** write-denied at either value of this key. Native discovery never needed them — an agent CLI reads its own user config and credentials in its own process, outside the sandbox profile — while opening the set handed the agent your `.worc/.env`, which is the file the orchestrator keeps its own secrets in, through the plain `Read` tool.

Separately, and in the opposite direction from what a reader expects: the agent CLIs' **own** config homes (`~/.claude` / `$CLAUDE_CONFIG_DIR`, `$CODEX_HOME`) are not part of any deny projection at all, at either value of any key, and no setting restores one. Credentials live there and so does configuration the CLIs load on their own next start. What that costs is stated in [advanced mode](#the-advanced-mode-strict_isolation-false) — it is an accepted cost of the same nature.

The **write** side is otherwise untouched: the exchange / `.git` / `tasks/` write-deny, the commit and staging gates, and the PR control layer all stay, as does the public `denied_read_paths` blacklist. (Repository governance/instruction files — `AGENTS.md`, `.agents/rules/**` — are **not** write-denied: editing them is ordinary work reported to the operator, not blocked; add them to `security.protected_paths` if you want an always-ask step on any change.)

`strict_isolation` is the master switch and always wins toward relaxation: the effective state is `disable_read_isolation OR NOT strict_isolation`, so `strict_isolation: false` forces read-isolation off regardless, overriding even an explicit `disable_read_isolation: false`. Like the other isolation settings it is **operator-config only** — a task, `extra_args`, or a flow node can never set it — and both `worc preflight` and the run log announce `read-isolation: OFF` when it is in effect, so the reduced isolation is never silent. Native Windows Codex needs the elevated sandbox backend to enforce the private read-deny projection; without it, read-isolation **on** fails closed during preflight or the pre-launch canary rather than running with unproven isolation.

## `security`

Controls isolation, child-process environment allowlisting and assignment, denied read paths, denied commands, and the dangerous-diff approval policy.

**Read the default column carefully: this is the one block where what `install` writes differs from what an omitted key means.** The dataclass/loader fallback is the fail-closed posture; `install` writes the relaxed one, key by key, so an operator reading their own file sees the relaxation. A config that _drops_ those keys goes back to fail-closed.

```yaml
security:
  # What `install` writes — this IS the advanced mode. Omit the key and you get `true`.
  strict_isolation: false
  disable_read_isolation: true
  allow_git_evidence: true

  allowed_environment: # OS-aware default; omit the key to let the loader resolve it per host
    - "PATH" # mandatory — a list that does not cover it is a load error
    - "HOME"
    - "USER"
    - "USERPROFILE"
    - "CODEX_HOME"
    - "CLAUDE_CONFIG_DIR"
    - "SystemRoot" # Windows: without it the Node-based claude.exe crashes at startup (0xC0000409)
    - "DOTNET_*" # a prefix pattern: a name plus ONE trailing `*`
    # ... plus the rest of the Windows or Linux/macOS essentials — see config.example.yaml
  extra_environment: {} # variables the orchestrator ASSIGNS (never credentials — plaintext here)
  denied_read_paths:
    - ".env"
    - "secrets/**"
  denied_commands:
    - "git commit"
    - "git push"
    - "gh pr create"
    - "gh pr merge"
  trust_level: "auto" # approval policy for the dangerous-diff gate; strict | auto (default)
  protected_paths: [] # globs that ALWAYS require approval on any change (the always-ask floor)
```

| Field | Type | Default (dataclass / install) | Meaning |
| --- | --- | --- | --- |
| `strict_isolation` | boolean | dataclass/loader `true` / **install writes `false`** | The master posture switch. `false` **is** the [advanced mode](#the-advanced-mode-strict_isolation-false) — not merely a relaxed sandbox — and it is what a fresh install puts in your file; `true` is the fail-closed sandbox and is still what a config that **omits** the key gets. It is **not** a full-access gate: the provider full-access selectors and the absolutely-forbidden flags are refused at either value (see [`extra_args`](#extra_args)). What _is_ checked at either value is whether a provider's configured isolation is **legal** — preflight and the run fail when it is not, and under `false` that check matters most, because there the generated permission profile is the whole local floor. Whether an OS-enforced write floor exists for the run at all is a separate, **advisory** verdict, and **two** things reach it: a host where no OS sandbox can exist, and `false` itself — the mode raises no OS sandbox for Claude on **any** host, so a macOS machine that reports no gap under `true` prints `isolation-floor: NONE` under `false`. Codex is not symmetric and the mode does not pretend otherwise: it always gets a generated permission profile. See [When the host cannot sandbox](#whether-the-policy-is-enforced-here-and-what-reports-it). |
| `disable_read_isolation` | boolean | `true` (read-isolation **off** out of the box; set `false` to keep it on) | Operator-config only (never a task / flow / `extra_args` key). Relaxes only the **read** side: Claude reloads native `CLAUDE.md` + project settings/hooks/MCP/skills (`--setting-sources project`, no `--strict-mcp-config`), Codex reloads the user + project `.codex` config/hooks/rules. It does **not** open the private set — `.worc`, the resolved env-file and the frozen `runs/` bundles stay read- and write-denied at either value. The **write** side (exchange / `.git` / `tasks/` write-deny, commit and staging gates, PR control) and the `denied_read_paths` blacklist stay in force. `strict_isolation` is the master switch and always wins toward relaxation: effective off = `disable_read_isolation OR NOT strict_isolation`, so `strict_isolation: false` forces it on. `worc preflight` and the run log announce `read-isolation: OFF`. Full discussion under [`extra_args`](#extra_args) above. |
| `allowed_environment` | list of strings | OS-aware: the cross-platform base (`PATH`, `HOME`, `USER`, `USERPROFILE`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`) **+ the host OS's launch essentials** — **9 names on Linux/macOS, 19 on Windows** | The name gate. **Replaces** the default wholesale. An entry is an exact name **or a prefix pattern** — a name plus one trailing `*` (`DOTNET_*`, `PATH*`). `*` anywhere else is a load error (`*`, `A*B`, `**`, `*SUFFIX`), as is a secret-bearing prefix (`SECRET_*`); every expanded name passes the secret-name filter. Matching and de-duplication are case-insensitive on Windows, case-sensitive elsewhere. **`PATH` coverage is mandatory** — exactly, or by a pattern — and a list missing it is a **load error**. On **Windows** a missing `SystemRoot` is a launch-critical **FAIL** in `worc preflight` _and_ at `run` / `watch` / `rerun` start, at either mode value (the Node-based `claude.exe` was observed aborting `0xC0000409` before printing anything). Names absent from the parent environment are simply skipped. `install` does **not** write this key: the loader resolves the host OS default at load, so the same config adapts when it moves between machines — writing the list is what freezes it. A strict-mode _prefix_ match cannot implicitly forward a name loaded from `.worc/.env`; an exact entry can. Under advanced mode the list gates only orchestrator-owned `git`/`gh`. Preflight and the run log print the expansion, what each pattern matched **here**, anything dropped as secret-bearing, and which child-process scope it describes. |
| `extra_environment` | mapping string → string | `{}` (nothing to replace — the key either assigns something or it does not) | Variables the orchestrator **assigns** to agent / check / tool children and to orchestrator-owned `git`/`gh`. Keys and values must be YAML **strings** — quote scalar-looking text (`on`, `1`, `1.10`) rather than relying on YAML coercion; an empty string is valid. Rejected: `PATH` in any case, a secret-looking name, a name outside `[A-Za-z_][A-Za-z0-9_]*`, and two names differing only in case. Use it for toolchain roots and cache paths (`DOTNET_ROOT`, `NUGET_PACKAGES`, `npm_config_cache`), **never credentials** — values are plaintext in `config.yaml`. Preflight prints names, never values. See [What `extra_environment` may point at](#what-extra_environment-may-point-at). |
| `denied_read_paths` | list of globs | `.env`, `secrets/**` | Paths the agent CLI may never read (enforced as `--disallowedTools`), so a task cannot exfiltrate secrets. Same glob dialect as `protected_paths`. **Replaces** the default. |
| `denied_commands` | list of strings | `git commit`, `git push`, `gh pr create`, `gh pr merge` | Commands the agent / checks may never run. **Replaces** the default wholesale — keep every entry you need. Friction and telemetry, not the floor: it binds tool calls, not a sandboxed shell. |
| `trust_level` | `"strict"` \| `"auto"` | `"auto"` (fresh install; the schema fallback for an absent block is `"strict"`) | Approval policy for the mid-task dangerous-diff gate. A task may override it with a front-matter `trust_level` field. Never lowers the hard ceiling — only which diffs raise the gate. See below. |
| `protected_paths` | list of repo-relative globs | `[]` | Globs that **always** require approval on any change, at any `trust_level` — the always-ask floor no level can lower. See below. |
| `allow_git_evidence` | boolean | dataclass/loader `false` / **install writes `true`** | Grant switch for the **read-only git-evidence** capability. Operator-config only (never a task / flow / `extra_args` key). **Inert under advanced mode** — there every node already has an unscoped shell, so the grant has no capability left to add and its twelve-verb scoping is not applied. See below. |

Only the orchestrator's Git Manager commits, pushes, and creates PRs. Agent providers do not — at either value of every key in this reference.

### The advanced mode (`strict_isolation: false`)

There is one door, not a matrix: `strict_isolation: false` **is** the advanced mode. It means "full freedom for the agent under your responsibility, except the floor", and it is what a fresh `install` writes into your file.

**What it relaxes — five axes.**

- **Environment.** The parent environment is forwarded **whole** to every process run on the agent's behalf — the agent CLIs, the check commands, the dependency scanners, the tool nodes — so `allowed_environment` is not consulted for any of them. Two exceptions: the names your `.worc/.env` defines stay withheld (the agent is denied reading that file, so forwarding its contents would route around the deny — name one in `extra_environment` to get it back), and `extra_environment` still wins on top. The orchestrator's own `git` and `gh` keep the allowlist: the mode widens what the _agent_ may do, and those are not the agent — a `GH_REPO` from your shell would retarget a pull request, and `GIT_DIR` would move both the commands and the paths the write-guard protects.
- **Tools.** No allowlist reaches the agent CLI, so every built-in tool exists and **every node has a shell** — `read-only` ones included, and the skill tool along with them, which is why this is the only mode in which a flow node can name a skill (see [Your repository's own skills](#your-repositorys-own-skills-and-what-a-flow-node-decides)).
- **Write.** The agent writes wherever the operator's own account can, not only inside the clone, which is what lets `dotnet build` find `~/.nuget` and `npm ci` find `~/.npm` with no cache redirection on your part — and, said plainly, includes a directory on `PATH`, which is the right to replace an executable this orchestrator later runs, its own `git`/`gh` among them. There is no width limit left to name: the mode used to express this as a volume-wide grant inside its OS sandbox, and now raises no sandbox at all (see the **Sandbox** axis below), so what bounds a command the agent starts is the operating system's own permissions and nothing of ours.
- **Sandbox.** On **Claude** no OS sandbox is raised, on **any** host — the axis the mode was found to have been quietly keeping. It is its own axis because the other four do not reach it: writes and domains were already fully open here, and the sandbox still refused things neither of them describes. The case that forced it: Angular's default on-disk cache takes a System V semaphore through `lmdb-js`, the vendor's seatbelt profile permits POSIX IPC and no System V at all, so `npm run build` died with `exit 134` while the unsandboxed Check Runner ran the same command green — and no vendor setting reaches that axis. **Codex is not symmetric**: it always gets a generated permission profile (`read-only` / `workspace-write`) at either value of the key, because the one selector that would remove it is absolutely forbidden. The mode still writes Claude a settings file carrying `sandbox.enabled: false` rather than leaving the default to the target repository's own project settings.
- **Network.** Every node is online whatever its flow granted — and that is three surfaces, not one boundary: the sandboxed shell, the CLI's own `WebFetch`/`WebSearch` (which do not pass through that sandbox), and Codex's `web_search` (which runs on its backend, outside the permission profile). There is no domain filtering and none is planned: an allowlist that has to pass `github.com` for ordinary dependencies holds nothing.

One width has to be said out loud because it is the opposite of what a reader expects: **the agent CLIs' own config homes (`~/.claude` / `$CLAUDE_CONFIG_DIR`, `$CODEX_HOME`) are protected by nothing** — not write-denied, not read-denied, at every value of every key, with no setting that restores a deny. Credentials live there (`$CODEX_HOME/auth.json` is a file on disk; Claude's token sits in the macOS Keychain, elsewhere in files), and so does configuration the CLIs load on their own next start — hooks in `~/.claude/settings.json`, MCP server commands in `$CODEX_HOME/config.toml`. An agent that writes there has planted code that runs outside any sandbox later; one that reads there can exfiltrate a token over the network this mode grants. It is a deliberate, accepted cost: a whole-home deny protects a directory where the need is per-file, and it breaks the CLI outright — the standalone Codex package keeps the `codex` binary itself inside `$CODEX_HOME`, and denying that home stops its own `apply_patch` sandbox helper from executing, so no patch lands at all.

**What it does not do.** It unlocks no provider full-access mode — those are forbidden at every value of the key. It skips no proof: the config-legality check and the per-provider capability probes run at either setting, because here the generated permission profile **is** the whole local floor, which makes it the last configuration to excuse from demonstrating it.

**The floor, in four levels.** `worc preflight` and the run log announce the mode in **one line** that points here rather than reciting all four — which is how a loud line stops being read. Levels 3 and 4 are the ones to decide about.

1. **The integrity of the task's own state is held mechanically on Codex, and asked for on Claude.** The clone's `.git` and the private `.worc` have to stay unwritable, and in this mode the two providers hold that differently — the single most important line on this page. **Codex** re-proves it before every attempt under its own generated profile and without a model: a write into the gitdir, the common dir, the hooks dir and `tasks/` has to be refused or the attempt fails before the model is called. **Claude** raises **no OS sandbox here at all**, on any host, so nothing binds the subprocesses a command starts — not a script from `package.json`, not `git` run by hand, not a shell started by either. What still holds there is the tool-level write denies, the full editor set (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`), which never passed through a sandbox and so lost nothing; plus the request stated in the prompt every node receives, and detection after the fact. Preflight and the run log say it on their own `isolation-floor: NONE` line rather than leaving you to infer it. The denied set is wider than the two names suggest — the resolved env-file and the frozen `runs/` tree are write-denied by name too — and narrower in one place: the agent CLIs' own config homes, as above.
2. **Publication to your origin is not held, and is not detected either.** A branch that appears or moves on `origin`, and a pull request that appears on the task head, are **ordinary working state**: neither parks a run, and neither is checked against the orchestrator's own records. What happens instead is recovery — see [When the task branch already exists on the remote](configuration-checks-git.md#when-the-task-branch-already-exists-on-the-remote). One consequence before you share a branch: a pull request **you** opened on the task branch is retitled and appended to like any other.
3. **Publication anywhere else is held by nothing**, and it is reachable today: the agent has the network. Credentials are picked up automatically and are not withheld — `gh` reads its own `hosts.yml`, `git` over HTTPS reads your `credential.helper`, over SSH your key — so a repository assembled outside the clone and pushed to any address is neither prevented nor noticed. Nothing is planned to hold this. What is asked instead is stated in the prompt every node receives: no commit, push, merge, tag or pull request, to this repository's remote or any other address, by any route.
4. **Publication as the orchestrator is reported by detection, not held by it.** The agent does not publish and does not touch `.git` — it replaces what the orchestrator publishes _with_. Three measures answer it: your user git config and the clone's own agent-CLI config are fingerprinted around the attempt; every `gh` call names its repository outright; and the executables the orchestrator launches (`git`, `gh`, the agent CLIs, `ps`, the daemon launcher) are resolved **once at startup** and used as those paths for the rest of the process, so a program planted on `PATH` while the agent works does not change what the orchestrator runs. Read **reported** literally — drift never stops the run.

Two things that last measure cannot cover: a substitution made **between** runs (each run resolves fresh, so a stand-in already in place is simply what gets pinned), and an edit to the **installed package's own code** — pinning the launcher answers "which `worc`", never "whose `git_manager.py`". If the machine's `PATH` directories or the installed package are writable by something you do not control, this mode is not the place to start.

**Redaction widens to compensate.** With the name gate gone, a secret-named environment variable is scrubbed from logs and artifacts by its **name** alone, without the allowlist excusing any — the layer that catches a secret with no recognizable shape, and it would collect nothing here otherwise. Accepted cost: a secret-named variable holding something harmless can appear as `[REDACTED]`. `PWD` and `OLDPWD` are exempt by name, because they match on the `pwd` segment while holding a path, and scrubbing them turned every `file:line` citation into `[REDACTED]/src/foo.py:42`.

#### Your repository's own skills, and what a flow node decides

Because every built-in tool exists here, the agent CLI's native skill discovery is live: the target repository's `.claude/skills/**` is loaded and can be invoked. Your **user-level** `~/.claude/skills` is not — the orchestrator selects project settings only, so user-global hooks, MCP servers and plugins stay out of the run.

**Skills are off unless a node asks.** A node declaring neither [`skills`](flow-authoring.md#node-declared-skills) nor `allow_skills` is launched with the provider's own per-attempt off-switch (Claude `--disable-slash-commands`, Codex `--disable skill_search`), and every built-in flow's node is in that state — a skill the flow never requested cannot fire on its own `description`. Naming one is itself the request; `allow_skills: true` turns them on without requiring any.

**Requiring is advanced mode only.** Under `strict_isolation: true` the `Skill` tool does not exist for the session — the tool gate carries the profile baseline and nothing else — so `skills:` and an explicit `allow_skills: true` are **refused at validation** rather than accepted and left inert (an inert `skills` would let a run report success having skipped your tested step). Omitting the key is never an error, and `allow_skills: false` is legal at every value of the switch: a flow may always narrow.

**A skill is not part of the frozen control plane.** The flow YAML and role prompts are copied into an immutable per-task bundle at task start; a skill is an ordinary repository file the CLI reads for itself at launch, so a writing node earlier in the run can change one and the next node reads the changed text. The orchestrator neither froze it nor reviewed it.

**The off-switch stops invocation, not reading.** With skills off the session genuinely reports no skill tool and an empty skill list — a real CLI gate, not a sentence in the prompt. But a node in this mode has a shell, so it can still locate a `SKILL.md`, read it and follow it as ordinary text. Read the switch as "this node will not run a skill as a step", never as "this node cannot see them".

**Where the run's posture is recorded.** Two places, dividing the timeline rather than backing each other up: the frozen control bundle's `manifest.json` records the mode the task **started** in (written before the first node runs, so it is there even for a task that never reached a terminal transition), and the completed ledger record carries `advanced_mode` for a task that **finished**. A successful run sweeps its own `runs/` subtree, so the bundle is gone exactly when the ledger record exists. There is deliberately **no marker in the pull request**: from a merged PR detached from the machine you cannot tell which mode produced it — only `.worc/logs/completed.jsonl` on the host that ran it can answer that.

### Whether the policy is enforced here, and what reports it

Three things about these keys are **host** questions rather than config questions, so they are answered by `worc preflight` and the run log rather than here. [operations → Preflight](operations-preflight.md#3-preflight-both-clis) documents each verdict line in full; what matters when you are writing the config is which way each one fails:

- **No OS write floor for this run** is **advisory** — preflight prints one `isolation-floor: NONE (<provider>: <cause>)` status line per provider without a floor (the run log the same, as `isolation floor NONE (…)`), and the run continues. The line names the cause and stops there; what the missing floor costs — `.git` and `.worc` writable, so control-plane tamper detection, exchange quarantine and `state.db` integrity are unenforced — is stated once in the shipped `.worc/guide/config/security.md` rather than reprinted into every report. Two different things print it. A **host** that cannot sandbox at all (native Windows; Linux/WSL2 missing `bubblewrap` + `socat`) prints it at either value of `strict_isolation`, and there the line carries the remedy. `strict_isolation: false` prints it on **every** host, for Claude, because the mode raises no sandbox anywhere — that arm carries no remedy on purpose: the remedy is `strict_isolation: true`, an operator decision rather than a missing dependency. Codex's floor does not move with the key. Under `strict_isolation: true` a refusal still happens, just later and narrower: the attempt that actually needs a sandboxed shell ends `capability_unavailable`, a per-node verdict a fallback provider can cover. An **illegal configuration**, by contrast, is `isolation: FAIL` and fatal at either value.
- **The policy is proved by probes, not claims.** Codex re-runs the exact profile under `codex sandbox` (no model, no network) before every attempt that gets a shell, proving the private home unreadable, the exchange read-only, the CLI binary itself executable under the profile, and a write into each Git-control root refused. A write that lands fails the attempt before the model is called. `worc preflight` runs the same battery against a throwaway fixture; `worc preflight --paid-isolation-probe` adds the one probe that cannot be free (one billed call per supporting provider, verdict read from the filesystem); it **declines and spends nothing** under `strict_isolation: false`, where no sandbox is raised on any host — there is no enforcement left to demonstrate, and the `isolation-floor: NONE` line already reports its absence. One rule governs reading any of it: **"nothing was written" is not a pass** — it is `NOT DEMONSTRATED`.
- **The per-attempt fingerprint reports; it never parks.** Git control state is fingerprinted around every attempt that gets a shell and a change is a `WARNING` plus a ⚠️ trace, on every node class. Read it as a stop-the-run signal. What it watches beyond the clone's `.git` — the push-URL digest, content digests of the agent's own CLI config and your user git config, and the `gh` repository pin — and what it deliberately does not watch (what `origin` holds for the task branch, whether a PR is open on it, where the base branch is) is in [operations → Git footprint](operations-publishing.md#5-git-footprint-and-the-audit-commit).

### What `extra_environment` may point at

`extra_environment` exists to redirect a toolchain's root or cache — `NUGET_PACKAGES`, `CARGO_HOME`, `npm_config_cache`. Inside the clone is the useful destination under strict isolation; advanced mode may also use writable paths outside it. In either mode one class of value is dangerous in a way no name check catches: a path landing on the orchestrator's own control surface. A build writing into `.worc/` corrupts the run that launched it; one writing into `.git/` corrupts the repository; one pointed at the exchange rewrites what the next node is told.

The check is split in two halves that answer to different rules, and a collision counts in **either** direction (a value may be neither a protected directory's parent nor something inside it):

- **Lexical** — no filesystem access at all, so one config file gets one verdict on every machine. It runs at **config load**: a value overlapping `.git`, `.worc`, `.worc-io`, the tasks dir, the env-file, or a `denied_read_paths` glob is rejected there.
- **Canonical** — resolves symlinks, `~`, and the case/UNC aliases of a single path, which needs the filesystem _this_ host has. It runs in **`worc preflight`** and at task start, reported on the `assigned-paths:` line. Because it resolves real paths and may **repair** the clone-local `.git/info/exclude`, preflight is not filesystem-read-only.

For an in-clone path-list value the orchestrator writes one clone-local `.git/info/exclude` rule per path element, and each task start repairs those rules — so a cache redirected into the clone never shows up as untracked work. Outside-clone paths warn only under strict isolation.

Assignments apply **after** forwarding and override case-insensitively on Windows. For orchestrator-owned `git`/`gh` they pass one more gate, a **whitelist** over the `GIT_*` / `GH_*` / `GITHUB_*` namespace: only `GIT_CONFIG_GLOBAL`, `GH_TOKEN` and `GITHUB_TOKEN` reach such a process, so a retargeting name — including one a future `git`/`gh` release invents — never does. Names outside those prefixes (a proxy, a locale, a toolchain root) are unaffected.

### `trust_level` (approval policy)

The dangerous-diff gate pauses for a human approval when an agent's diff deletes/renames a tracked file or touches a dependency manifest/lock. `trust_level` sets **which** of those changes actually ask (it never lowers the hard ceiling — env-allowlist, the `bypassPermissions`/`--dangerously-*` ban, and `cwd` containment hold at every level):

- **`strict`** — gate every tracked-file deletion/rename **or** dependency-manifest/lock edit. The behavior before this knob existed.
- **`auto`** _(default at install)_ — routine in-repo deletions/renames/edits do **not** gate; the diff-shape gate is off. The only thing that raises an approval is a `protected_paths` match. This is the recommended default: in-repo changes are git-reversible, and the published PR remains the review backstop.

A raised gate is **fail-closed**: a denial, a timeout, or no notifier stops the task in `manual_action_required` (nothing proceeds unreviewed). A task may override the global level with a front-matter `trust_level: strict|auto` (the task value wins; it does not affect `protected_paths`).

#### What the gate measures from

`trust_level` decides _which_ diffs raise the gate; this is _what_ the diff is measured against. The reference point is **the last commit the orchestrator itself made for the task** — or, until it has made one, the task's base (`base_ref`). It is deliberately **not `HEAD`**: a commit made inside the task would then leave nothing for the gate to see, and the one question you are asked before publishing would go quiet exactly when something unusual happened. So if an agent commits its own work mid-run, its content still reaches the gate and you are still asked.

#### Where you are asked, in three places

1. At a **writing node**, after its edit. A denial here sends the node back with the reason, so the agent can revise.
2. At that node's `hitl` round-trip on the way back. A node that asks you a question mid-run and also writes goes through the gate like any other writing node — asking a question was never meant to be a way past it.
3. Once more **immediately before the publishing commit**. This is the one that makes the promise hold for every flow: any node with a shell can commit — a `tool`, an `evaluator`, a read-only agent attempt, and under [advanced mode](#the-advanced-mode-strict_isolation-false) that is _every_ node — and none of them parks over it, so a flow that ends with one (or has no writing node at all, like `security_audit`) would otherwise reach publication with content nobody had been asked about. A denial there is a **stop, not a retry**: the agent is gone by then, so nothing is committed or pushed and the task parks for you.

An approval you already gave earlier in the same task for the same change is **not requested twice**. Two consequences worth expecting:

- In a decomposed task, deletions you approved on the first subtask are not put to you again on the second — the reference moves with each subtask commit — while the run's reported diff and the pull-request body still describe the whole task.
- If the state being published contains commits the orchestrator did not make, it says so in the log **and in the pull-request body**, and records the adopted commit rather than reporting a commit it never performed. The change itself has passed the gate; the commit message and authorship are whoever made them.

### `protected_paths` (always-ask floor)

Repo-relative globs whose files require approval on **any** change (create/edit/delete/rename), regardless of `trust_level`:

```yaml
security:
  trust_level: "auto"
  protected_paths:
    - "src/security/**" # any change under the security subtree asks first
    - ".github/workflows/**" # CI changes always ask
```

- **Glob dialect** is the same as `checks.command_sets[].paths`: `**` crosses directories (`**/*.md` matches `README.md` and `docs/a/b.md`), a single `*` stays within one path segment (`*.md` matches only top-level `.md` files). Patterns are repo-relative; an absolute path, `~`, or `..` traversal is rejected at config load.
- **A floor, not a deny.** It means "always ask a human," not "never change." It is `config.yaml`-only (a task can never widen or narrow it) and is checked before the level, so even `trust_level: auto` still gates a protected-path change.

> Migration note: `trust_level` + `protected_paths` replace the former `security.deletion_approval_exempt_paths` allowlist. `upgrade-config` strips the old key; there is no automatic conversion (the new model is the inverse — an always-ask floor rather than a skip-list).

### `allow_git_evidence` (the read-only git-evidence grant)

A flow node may declare `git_evidence: true` to say "this node's job needs delivery history" — an audit pass that should cite the commit which closed a milestone rather than grepping a changelog and calling that evidence. The **declaration alone grants nothing**: a flow can express the need but cannot hand itself the capability. The dataclass default is `false`, but **`install` writes `true`**. Only with `security.allow_git_evidence: true` does such a node get the read-only git verbs — `log`, `show`, `diff`, `blame`, `status`, `rev-list`, `rev-parse`, `ls-files`, `shortlog`, `describe`, `cat-file`, `for-each-ref`. The grant reaches **only the nodes that asked**, never every `read-only` node in the run.

It does **not** make the node writable. Claude scopes the shell to those verbs and write-denies the whole clone in its OS sandbox (and refuses the attempt outright on a host where it cannot sandbox a shell); Codex's `read-only` sandbox already forbids every mutation; `denied_commands` stays the floor beneath both; commit/push/PR remain the orchestrator's alone. `worc preflight` and the run log announce `git-evidence: ON` when it is in effect.

> **Read "master switch" as a _grant_ switch, not a kill switch.** Off does not mean "no node can read git history anywhere" — it means "no node _gains_ a capability it did not already have." A Codex `read-only` node can run `git log` today regardless, because its sandbox permits commands (its mutation ban comes from the workspace being mounted read-only with the network off, not from a verb list). So with this off, one flow still has **different reach depending on which provider runs the node**; turning it on is what makes the two match. On Claude the switch _is_ the whole shell, which is why the asymmetry is visible there and not on Codex.

One consequence follows for the `read-only` **profile definition** itself: it means "cannot write", not "has no shell". See [worc_architecture.md §4.9](worc_architecture.md) and the [Glossary](glossary.md).

**Under advanced mode this key stops meaning anything.** `strict_isolation: false` gives every node an unscoped shell, so the grant has no capability left to add and its twelve-verb scoping is not applied. Leave it off there; it neither adds nor removes reach. That is also why `install` writing `true` beside `strict_isolation: false` is not a contradiction — it is the read-side posture that takes effect the moment strict isolation is turned back on.

**A note on what used to be here.** This grant was once the single bounded exception to a git-control-state _park_: drift observed after an attempt normally ended the task `manual_action_required`, and only a `read-only` node holding this grant warned and continued. **That model is gone.** Git-control drift now warns and continues for **every** node class — see [What the per-attempt fingerprint watches](#whether-the-policy-is-enforced-here-and-what-reports-it) for what is watched and what the warning asks of you.

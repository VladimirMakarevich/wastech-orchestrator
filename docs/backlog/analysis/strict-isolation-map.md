# `security.strict_isolation` — what it disables/keeps, per provider (code-verified map)

Status: **analysis only** (read-only investigation, no behavior change) Date: 2026-07-24 Owner: Vladimir Makarevich

Scope: the **current working tree** of `feat/agent-worc-read-isolation` — i.e. **with the uncommitted VF-5 (native-discovery rollback) and VF-6 (`disable_read_isolation` escape hatch) changes applied**, not `HEAD` (`8f9aac2`). Every claim cites `file:line` in that working tree. The code and `.agents/rules/` are the source of truth; this is a navigation aid.

## 0. The two switches and the one formula

- `SecurityConfig.strict_isolation: bool` — required key, default `true` via the loader ([config/loader.py:553](../../../src/wastech_orchestrator/config/loader.py)).
- `SecurityConfig.disable_read_isolation: bool = False` — VF-6 operator escape hatch, off by default ([config/schema.py:360](../../../src/wastech_orchestrator/config/schema.py)).
- **Effective read-isolation state** — the single derived property every adapter reads:

  ```python
  # config/schema.py:362-369
  @property
  def read_isolation_off(self) -> bool:
      return self.disable_read_isolation or not self.strict_isolation
  ```

So there are **two independent axes**, not one:

| Axis | Fed by | Governs |
| --- | --- | --- |
| **Permission/sandbox/preflight axis** | `strict_isolation` **directly** | Full-access modes, the isolation preflight, Claude Bash-sandbox host handling, Codex canary/smoke gating, `**`-glob rejection |
| **Read-isolation axis** | `read_isolation_off` = `disable_read_isolation OR NOT strict_isolation` | Native project-instruction/config **discovery-disable** + the private **read-deny projection** (`InternalDenyPolicy`) |

`strict_isolation: false` moves **both** axes toward relaxation (it is a term in the `read_isolation_off` formula). `disable_read_isolation: true` moves **only** the read-isolation axis and leaves the permission/sandbox ceiling fully in force.

Both are **operator-config only**: the loader reads them only from the `security:` mapping ([config/loader.py:534-554](../../../src/wastech_orchestrator/config/loader.py)); there is no task / `extra_args` / flow-node path that can set either.

## 1. Master matrix (rows = mechanism; both providers)

Legend for "gated by strict?": **direct** = keyed on `strict_isolation`; **via read_isolation_off** = keyed on the VF-6 formula (so strict=false flips it, but `disable_read_isolation` can flip it alone too); **no** = strict-independent.

| Mechanism | strict=true (default, `disable_read_isolation` unset) | strict=false | Gated by strict? | Enforcement point (`file:line`) | Provider |
| --- | --- | --- | --- | --- | --- |
| **Full-access mode select** (Codex `sandbox: danger-full-access`; Claude `--permission-mode bypassPermissions` in `extra_args`) | **Rejected at preflight** — `isolation_reasons` returns a reason → `check_isolation` → `PipelineFailed` | **Allowed** — preflight is skipped, full-access mode emitted | **direct** | preflight skip [core/orchestrator.py:2322-2329](../../../src/wastech_orchestrator/core/orchestrator.py); reasons: codex [providers/codex.py:507-509](../../../src/wastech_orchestrator/providers/codex.py) / claude [providers/claude.py:725-730](../../../src/wastech_orchestrator/providers/claude.py); detector [security/forbidden_args.py:77-100](../../../src/wastech_orchestrator/security/forbidden_args.py) | both |
| **Flow-node `extra_args` full-access** (a node selecting danger-full-access / bypassPermissions) | **Fatal flow-validation error** | **Allowed** (gate skipped) | **direct** | [core/flow/validator.py:554-564](../../../src/wastech_orchestrator/core/flow/validator.py) | both |
| **Weaker/​escalating `--permission-mode` in `extra_args`** (Claude) | Flagged by preflight (`_reject_weaker_permission_override`) → fail | Allowed (appended last, CLI last-wins) | **direct** (only via the preflight, which is skipped when strict=false) | [providers/claude.py:531-559](../../../src/wastech_orchestrator/providers/claude.py) (used only by `isolation_reasons`) | claude |
| **Absolutely-forbidden flags** (`--dangerously*`, `--yolo`, `--ignore-rules`, `--allow-dangerously-skip-permissions`, malformed `--sandbox`) | Hard-rejected in `build_*_argv` **and** validator | **Still hard-rejected** | **no** | [security/forbidden_args.py:52-74](../../../src/wastech_orchestrator/security/forbidden_args.py); claude [providers/claude.py:608-612](../../../src/wastech_orchestrator/providers/claude.py); codex [providers/codex.py:422-426](../../../src/wastech_orchestrator/providers/codex.py) | both |
| **Reserved authority flags** (`_RESERVED_CLAUDE_FLAGS` / `_RESERVED_CODEX_FLAGS` — `--tools`, `--settings`, `--setting-sources`, `--mcp-config`, `-c/--config`, `-p/--profile`, `-s/--sandbox`, `--full-auto`, `-a`, `--ignore-user-config`, `--add-dir`, …) | Hard-rejected | **Still hard-rejected** | **no** | claude set [providers/claude.py:359-402](../../../src/wastech_orchestrator/providers/claude.py) + [426-438](../../../src/wastech_orchestrator/providers/claude.py); codex set [providers/codex.py:114-151](../../../src/wastech_orchestrator/providers/codex.py) + [304-310](../../../src/wastech_orchestrator/providers/codex.py) | both |
| **Claude Bash OS-sandbox, Linux/WSL2 missing `bwrap`+`socat`** | Workspace-write raises pre-model `CAPABILITY_UNAVAILABLE` (refuses unsandboxed Bash) | Keeps **unsandboxed** Bash (operator owns risk) | **direct** | [providers/claude.py:202-207](../../../src/wastech_orchestrator/providers/claude.py); preflight reason [providers/claude.py:731-736](../../../src/wastech_orchestrator/providers/claude.py) | claude |
| **Claude Bash on native Windows** (no supported sandbox) | Bash **dropped** from workspace-write (restricted mode; Edit/Write stay) | Keeps **unsandboxed** Bash | **direct** | [providers/claude.py:196-201](../../../src/wastech_orchestrator/providers/claude.py) | claude |
| **Codex per-attempt `codex sandbox -P` canary** | Runs; a leak → non-fallback `CONFIGURATION_ERROR`, undemonstrable → `CAPABILITY_UNAVAILABLE` | Runs for read-only/workspace-write; **skipped only on the danger-full-access escape** (no profile emitted) | **no** directly — but the escape that skips it is itself strict=false-only | gate [providers/codex.py:662-666](../../../src/wastech_orchestrator/providers/codex.py); outcome [providers/codex_canary.py:264-364](../../../src/wastech_orchestrator/providers/codex_canary.py) | codex |
| **Codex `worc preflight` no-model capability smoke (H7)** | Runs for a healthy Codex | **Returns `None`** (not run) | **direct** | [providers/codex.py:703-704](../../../src/wastech_orchestrator/providers/codex.py); caller [cli.py:2557](../../../src/wastech_orchestrator/cli.py) | codex |
| **`denied_read_paths` unbounded `**` glob** (Codex profile) | **Rejected** (`CONFIGURATION_ERROR`) — cannot prove cross-platform | Allowed (with `glob_scan_max_depth` bound) | **direct** | [providers/codex_profile.py:89-97](../../../src/wastech_orchestrator/providers/codex_profile.py) | codex |
| **① Private read-deny projection** (`InternalDenyPolicy`: `.worc` control+private homes, `.env`/env-file, provider homes incl. `CODEX_HOME`/`~/.claude`, frozen control+instruction bundles) | **Read-denied.** Claude: `Read`+`Write`+`Edit` tool denies + sandbox `denyRead`. Codex: profile `deny` (read+write). | **Read allowed, write still denied.** Claude: only `Write`+`Edit` denies, `denyRead=[]`. Codex: profile grant downgraded `deny`→`read`. | **via read_isolation_off** | claude tools [providers/claude.py:659-667](../../../src/wastech_orchestrator/providers/claude.py); claude sandbox [providers/claude.py:491-494](../../../src/wastech_orchestrator/providers/claude.py); codex profile [providers/codex_profile.py:142-151](../../../src/wastech_orchestrator/providers/codex_profile.py) | both |
| **② Native discovery-disable** (Claude `--setting-sources ""` + `--strict-mcp-config`; Codex `--ignore-user-config` + project `trust_level="untrusted"` + `--disable hooks`) | **Disabled.** Claude loads no settings/hooks/MCP/skills/plugins/`CLAUDE.md`; Codex ignores user `config.toml`, distrusts `.codex`, disables `hooks`. | **Restored (native).** Claude `--setting-sources project`, drops `--strict-mcp-config`; Codex loads user config, `trust_level="trusted"`, re-enables `hooks`. | **via read_isolation_off** | claude [providers/claude.py:627-645](../../../src/wastech_orchestrator/providers/claude.py); codex [providers/codex.py:385-393](../../../src/wastech_orchestrator/providers/codex.py) | both |
| **F37 Claude native-memory deny** (`<config_dir>/projects/<repo>/memory`) | Read+Write+Edit denied — unless `allow_native_memory: true` | **READ deny lifted; Write/Edit deny STAYS** (fixed 2026-08-04 — it used to drop the whole rule, leaving the store with zero deny rules, and agents wrote to the operator's HOME) | **read side via read_isolation_off; write side only via the per-provider `allow_native_memory` opt-in** | [providers/claude.py](../../../src/wastech_orchestrator/providers/claude.py) `_native_memory_deny_tools` / `build_claude_argv` | claude |
| **Required-flag preflight** (`--strict-mcp-config` presence probe) | Required in `claude --help` | Dropped from the required set (not emitted) | **via read_isolation_off** | [providers/claude.py:884-888](../../../src/wastech_orchestrator/providers/claude.py) | claude |
| **Write-guard** (exchange read-only; `.git`/common-dir/hooks; `tasks/`; tracked instruction files `AGENTS.md`/`CLAUDE.md`) — all Write/Edit-denied | Enforced | **Enforced (unchanged)** | **no** | claude [providers/claude.py:668-671](../../../src/wastech_orchestrator/providers/claude.py); codex [providers/codex_profile.py:137-140](../../../src/wastech_orchestrator/providers/codex_profile.py); policy [runtime_layout.py:145-186](../../../src/wastech_orchestrator/runtime_layout.py) | both |
| **WRI-009 Git control-state fingerprint + staged-set gate + clean-index preflight + subprocess neutralization** | Enforced | **Enforced (unchanged)** — no `strict_isolation` reference in `git_manager.py` | **no** | `git_manager.py` (no strict ref); node bracket [core/flow/nodes/agent.py] / [core/flow/nodes/base.py] | orchestrator (both) |
| **PR control layer** (no direct push to `base_branch`; publish only via PR; scoped staging) | Enforced | **Enforced (unchanged)** | **no** | git manager / publish node | orchestrator (both) |
| **Public `denied_read_paths` blacklist** (`.env`, `secrets/**`) | Enforced | **Enforced (unchanged)** — explicitly kept even under read_isolation_off | **no** | claude [providers/claude.py:652](../../../src/wastech_orchestrator/providers/claude.py); codex [providers/codex_profile.py:152-159](../../../src/wastech_orchestrator/providers/codex_profile.py) | both |
| **`denied_commands` → Claude `--disallowedTools`** (Codex projection descoped) | Enforced (Claude) | **Enforced (unchanged)** | **no** | [providers/claude.py:652](../../../src/wastech_orchestrator/providers/claude.py) | claude |
| **Network policy** (F17b: Codex workspace-write never online; `web_search="disabled"` offline; Claude web tools only on grant; profile `network.enabled=false`) | From flow `request.network_access` | **Same (unchanged)** | **no** (flow/validator-driven) | codex [providers/codex.py:462-468](../../../src/wastech_orchestrator/providers/codex.py); claude [providers/claude.py:212-213](../../../src/wastech_orchestrator/providers/claude.py); profile [providers/codex_profile.py:165](../../../src/wastech_orchestrator/providers/codex_profile.py) | both |
| **Managed/admin policy** (Claude enterprise managed settings; Codex `--include-managed-config` in canary) | Applies (trusted-computing-base) | **Applies (unchanged)** | **no** | claude comment [providers/claude.py:644](../../../src/wastech_orchestrator/providers/claude.py); codex canary [providers/codex_canary.py:256](../../../src/wastech_orchestrator/providers/codex_canary.py) | both |
| **Fundamentals** (argv-not-shell; prompt on stdin; no secrets in logs/DB/artifacts; path-identity validation) | Enforced | **Enforced (unchanged)** | **no** | across `providers/`, `security/identifiers.py` | both |
| **Router fallback** (`fallback_allowed`) | `CAPABILITY_UNAVAILABLE` uses host-verified conditional fallback; full-access preflight can block a route | strict=false suppresses the missing-sandbox `CAPABILITY_UNAVAILABLE` and permits full-access routes | **indirect** (strict shapes the error classes / preflight, not `fallback_allowed` itself) | [routing/router.py:67-91](../../../src/wastech_orchestrator/routing/router.py) | both |

The isolation **preflight itself** (`security/isolation.py`) is on the permission/sandbox axis and is **orthogonal to read-isolation**: `isolation_reasons` never inspects `read_isolation_off`, and the preflight validates the write/permission/sandbox ceiling regardless of the escape hatch ([security/isolation.py:16-21](../../../src/wastech_orchestrator/security/isolation.py)).

## 2. What `strict_isolation: false` UNLOCKS (the escape-hatches)

Turning strict off (owning the risk) relaxes, in one setting, **two groups**:

**Permission/sandbox axis (direct):**

1. **Full-access provider modes** become selectable via operator config: Codex `sandbox: danger-full-access` → `--sandbox danger-full-access` (no profile, no canary), Claude `--permission-mode bypassPermissions` in `extra_args` (last-wins). The isolation preflight ([core/orchestrator.py:2322](../../../src/wastech_orchestrator/core/orchestrator.py)) and the flow-validator gate ([core/flow/validator.py:554](../../../src/wastech_orchestrator/core/flow/validator.py)) are skipped.
2. **Unsandboxed Claude Bash** on hosts without a supported sandbox: Linux/WSL2 missing `bwrap`+`socat` (no longer `CAPABILITY_UNAVAILABLE`) and native Windows (Bash kept instead of dropped).
3. **Codex `worc preflight` capability smoke** is skipped (returns `None`).
4. **`denied_read_paths` unbounded `**` globs** are accepted (bounded scan) instead of rejected.

**Read-isolation axis (because `NOT strict_isolation` is a term in `read_isolation_off`):** 5. **Private read-deny lifted** — `.worc`, the env-file, provider homes, and frozen bundles become **readable** by the agent (still write-denied). 6. **Native discovery restored** — Claude reloads `CLAUDE.md` + project settings/hooks/MCP/skills/plugins (`--setting-sources project`, `--strict-mcp-config` dropped); Codex reloads user `config.toml`, trusts the project `.codex` config/rules, re-enables `hooks`. 7. **F37 Claude native-memory deny lifted.**

Group 2 is the **read-isolation subset that `disable_read_isolation: true` unlocks on its own** while keeping strict's permission/sandbox ceiling (group 1) fully in force. That surgical middle ground did not exist before VF-6.

## 3. What stays in force ALWAYS (strict-independent)

- **Write-guard**: exchange read-only, `.git`/common-dir/hooks write-denied, `tasks/` lifecycle write-denied, tracked instruction files (`AGENTS.md`/`AGENTS.override.md`/`CLAUDE.md`) write-denied (VF-5 immutability).
- **WRI-009**: Git control-state fingerprint, untrusted-filter refuse-gate, full staged-set allowlist gate, audit-commit lifecycle-digest check, clean-index preflight, subprocess/env neutralization.
- **PR control layer**: no direct push to `base_branch`; publish only through a PR; scoped staging (never `git add -A`).
- **Public blacklists**: `denied_read_paths` (Claude `Read()` denies / Codex profile `deny`) and `denied_commands` (Claude `--disallowedTools`) — projected regardless of both switches.
- **Absolutely-forbidden flags** and **reserved authority flags** — rejected in `build_*_argv` and the validator regardless of strict (re-opening a closed surface is never the sanctioned opt-out; the only sanctioned opt-out is the gated full-access mode).
- **Network policy** (flow/validator-driven), **managed/admin policy** (TCB), **process-tree quiescence barrier**, and the **fundamentals** (argv-not-shell, prompt-on-stdin, no secrets in logs/DB/artifacts, path-identity validation).

This matches security.md §MANDATORY: the fundamentals "that carry no isolation trade-off — argv-not-shell launching, no secrets…, path-identity validation, and the PR control layer — stay in force regardless."

## 4. KEY QUESTION — does `strict_isolation: false` lift (a) the read-deny projection and (b) discovery-disable, and is the VF-6 formula a NEW binding?

**Answer (current working tree): YES to both — and it is a NEW binding introduced by the uncommitted VF-6 change.**

**(a)+(b) — today `strict_isolation: false` lifts both.** Both the private read-deny projection and the native-discovery-disable are driven **solely** by the derived `read_isolation_off`, which is `disable_read_isolation OR NOT strict_isolation` ([config/schema.py:369](../../../src/wastech_orchestrator/config/schema.py)). That property is threaded into both adapters — Claude [providers/claude.py:937,951](../../../src/wastech_orchestrator/providers/claude.py), Codex [providers/codex.py:857,712,678](../../../src/wastech_orchestrator/providers/codex.py) — and every read-side branch keys on it: the Claude tool-deny kinds ([claude.py:666](../../../src/wastech_orchestrator/providers/claude.py)), sandbox `denyRead` ([claude.py:494](../../../src/wastech_orchestrator/providers/claude.py)), `--setting-sources` ([claude.py:627-645](../../../src/wastech_orchestrator/providers/claude.py)); the Codex profile grant ([codex_profile.py:149](../../../src/wastech_orchestrator/providers/codex_profile.py)) and native-config restore ([codex.py:385-393](../../../src/wastech_orchestrator/providers/codex.py)). Because `NOT strict_isolation` is a disjunct, **`strict_isolation: false` forces `read_isolation_off` true**, lifting read-denies **and** restoring native discovery — even if `disable_read_isolation` is explicitly `false`.

So it is **not** true (today) that "strict only affects the permission/sandbox/preflight mode and read-isolation is unconditional." The read-isolation axis is now coupled to `strict_isolation` through the VF-6 formula.

**Is the coupling NEW? YES.** At `HEAD` (`8f9aac2`, pre-VF-5/VF-6):

- `read_isolation_off` / `disable_read_isolation` **do not exist** (absent from `schema.py`).
- `build_claude_argv`/`build_codex_argv` **do not accept** a `read_isolation_off` parameter.
- Claude emitted `--setting-sources ""` + `--strict-mcp-config` **unconditionally**, and projected the internal deny set as `("Read","Write","Edit")` **unconditionally** (`HEAD:providers/claude.py:642`), with `deny_read = [...deny_policy.denied_paths]` **unconditional** (`HEAD:claude.py:490`).
- Codex emitted `--ignore-user-config` + `trust_level="untrusted"` **unconditionally** (`HEAD:codex.py:374-376`) and set the profile grant to `"deny"` **unconditionally** (`HEAD:codex_profile.py:144`).

At HEAD, therefore, `strict_isolation` affected **only** the permission/sandbox axis (full-access preflight, flow-validator full-access gate, Claude Bash-sandbox host handling, Codex canary/smoke gating, `**`-glob rejection). **Read-isolation — discovery-disable + read-deny projection — was ALWAYS ON regardless of `strict_isolation`.** The `disable_read_isolation OR NOT strict_isolation` formula is a brand-new binding added by the uncommitted VF-6 work.

**Does it change behavior for existing `strict_isolation: false` configs? YES (semantically).** A config that ran `strict_isolation: false` at HEAD executed with read-isolation **fully ON** (native discovery disabled, `.worc`/secrets/provider-homes read-denied) while merely permitting a full-access _mode_ if one was also selected. After this change the **same** config runs with read-isolation **OFF** — native `CLAUDE.md`/`AGENTS.md` + project settings/hooks/MCP/skills discovery restored and the private read-deny projection lifted — **without** the operator selecting any full-access mode. That is a real behavior change on the strict=false path. Practical blast radius is bounded only because the project is greenfield/undeployed (per project memory, the orchestrator is not deployed anywhere), so no production strict=false config exists yet — but any local/test strict=false config is affected, and the semantics changed.

## 5. Doc ↔ code discrepancies

| # | Where | Finding | Severity |
| --- | --- | --- | --- |
| D1 | `README.md` §"Threat model" line 100 (and the §3 framing) | The decision-record still frames `strict_isolation: false` as **only** the full-access opt-out that "deliberately disables the Codex/Claude isolation guarantee," and never mentions `disable_read_isolation` or that strict=false now **independently forces read-isolation off** (native discovery restored + private read-deny lifted) even without a full-access mode. The threat matrix's "describes the default `strict_isolation: true` contract" caveat stays technically true, but the strict=false description is now incomplete post-VF-6. The canonical rule file (security.md §3) and the operator docs (configuration.md, operations.md, packaged guide) **are** updated and correct — this gap is only in the backlog ADR. | Low (ADR lag; code + rules authoritative) |
| D2 | wri-011 lines 24/40/41/53/63/64/72; wri-003 lines 49/81 | In-scope items and (unchecked) acceptance criteria still literally mandate "disable provider-native `AGENTS.md`/`CLAUDE.md` discovery + inject the frozen manifest" — the exact control VF-5 **reversed** and VF-6 further made conditional. Each doc carries a top VF-5 amendment banner ("everything below … is reversed"), so it is a _documented_ supersession, but the inline ACs were not struck through and read as still-required to anyone who skips the banner. Neither doc mentions VF-6. | Low–Medium (mitigated by banner) |
| D3 | wri-002 line 51 | "Options considered" text still suggests `--safe-mode` for config-isolation; the shipped code deliberately uses `--setting-sources ""` and **not** `--safe-mode` (to preserve the F37 `allow_native_memory` opt-in). Pre-existing aspirational note, not a live contradiction. | Info |
| D4 | [operations.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/operations.md) line 201 | The `isolation: OK (strict_isolation=false)` bullet couples "keeps unsandboxed Bash" with "read-isolation is disabled." Both are indeed true under strict=false, but the phrasing attaches the read-isolation consequence to the Bash-sandbox sentence; line 202 (`read-isolation: OFF`) states it precisely. Minor imprecision, not wrong. | Info |

No discrepancy found in: security.md §3/§10, configuration.md (§319 prose + §351 table row), the packaged `config.example.yaml` / `guide/config/reference.md` rows, or the `preflight`/run-log warnings — all match the working-tree code.

## 6. One-line takeaways

- `strict_isolation` today drives **two** axes: a **direct** permission/sandbox/preflight axis, and — **new in VF-6** — the **read-isolation** axis via `read_isolation_off = disable_read_isolation OR NOT strict_isolation`.
- `disable_read_isolation: true` = relax **only** read-isolation, keep the full permission/sandbox ceiling. `strict_isolation: false` = relax **both** (and it overrides an explicit `disable_read_isolation: false`).
- The write side (write-guard, WRI-009, PR control), the public blacklists, the forbidden/reserved flags, network/managed policy, and the fundamentals are **strict-independent** and stay on always.

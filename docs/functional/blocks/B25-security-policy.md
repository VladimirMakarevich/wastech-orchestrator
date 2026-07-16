# B25 — Security Policy Enforcement

> Reconstructed from code (`security/forbidden_args.py`, `security/env.py`, `security/injection.py`, `security/isolation.py`, `security/profiles.py`) and tests (`tests/security/`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/security/forbidden_args.py`, `src/wastech_orchestrator/security/env.py`, `src/wastech_orchestrator/security/injection.py`, `src/wastech_orchestrator/security/isolation.py`, `src/wastech_orchestrator/security/profiles.py`

## Responsibility

A set of small, pure security decision functions that together enforce the system invariant **"a task or `extra_args` may never weaken the security policy"**. They cover the environment allowlist, common forbidden bypass flags, the closed Codex argument/config parser, front-matter injection, offline isolation preflight, and permission-profile strictness. Each runs at more than one call site so the guarantee holds in depth; none launches a process.

The **network access control** is part of this same policy surface even though its mapping lives in the adapters (B18): a flow grants network only by declaring `network_policy` (B29), and the adapters translate that single boolean onto sandbox network / web tools while never touching the filesystem sandbox or approvals ceiling. It is documented here as a security control and cross-linked.

## Public surface

- `find_forbidden_args(args) -> list[str]` ([forbidden_args.py:38](../../../src/wastech_orchestrator/security/forbidden_args.py#L38)) — one reason string per offending token; `[]` means safe.
- `FORBIDDEN_SANDBOX_VALUE = "danger-full-access"` ([forbidden_args.py:21](../../../src/wastech_orchestrator/security/forbidden_args.py#L21)) — the sandbox value that must never be selected.
- `build_child_env(allowed_keys, parent_env=None) -> dict[str, str]` ([env.py:18](../../../src/wastech_orchestrator/security/env.py#L18)) — child env from the allowlist only.
- `scan_frontmatter(frontmatter) -> InjectionFinding | None` ([injection.py:49](../../../src/wastech_orchestrator/security/injection.py#L49)) — first argv-shaped front-matter value, or `None`.
- `scan_value(key, value) -> InjectionFinding | None` ([injection.py:58](../../../src/wastech_orchestrator/security/injection.py#L58)) — recursive single-value scan.
- `InjectionFinding(key, reason)` ([injection.py:37](../../../src/wastech_orchestrator/security/injection.py#L37)) — frozen finding with a `.detail` property.
- `check_isolation(config) -> list[str]` ([isolation.py:32](../../../src/wastech_orchestrator/security/isolation.py#L32)) — one reason per provider whose required isolation cannot be enabled; `[]` means OK.
- `is_same_or_stricter(candidate, reference) -> bool` ([profiles.py:23](../../../src/wastech_orchestrator/security/profiles.py#L23)) — `True` iff `candidate` is at least as strict; fail-closed.

## Behavior

### Forbidden bypass-flag detection (the single source of truth)

`find_forbidden_args` is the one place that decides whether an argv token "disables the sandbox/approvals". For each token it takes the part before `=` and rejects it when it starts with `--dangerously` or is one of the standalone flags `{--yolo, --ignore-rules}` ([forbidden_args.py:25-30](../../../src/wastech_orchestrator/security/forbidden_args.py#L25), [:47-48](../../../src/wastech_orchestrator/security/forbidden_args.py#L47)). For `--sandbox`/`-s` it reads the value either inline (`--sandbox=…`) or from the next token and rejects `danger-full-access`; it also rejects a **dangling** sandbox flag with a missing or empty value (last token, or a trailing `=`), which would otherwise be treated as safe (audit #28, 2026-06-22) — defense in depth, it can never weaken isolation ([forbidden_args.py:33](../../../src/wastech_orchestrator/security/forbidden_args.py#L33), [:50-60](../../../src/wastech_orchestrator/security/forbidden_args.py#L50)). The `--dangerously` prefix rule is deliberately broad so any **future** `--dangerously*` flag is caught without a code change ([forbidden_args.py:47](../../../src/wastech_orchestrator/security/forbidden_args.py#L47)). Reasons are returned unqualified (no config path, no provider prefix) so each caller can frame them in its own terms ([forbidden_args.py:38-43](../../../src/wastech_orchestrator/security/forbidden_args.py#L38)).

The common detector remains the Claude/check defense-in-depth hinge. Codex adds `parse_codex_extra_args`: a typed parser that recognizes split and equals `-c`/`--config` forms, accepts only a short harmless allowlist, canonicalizes successful entries, and reports only option/config-key names. Provider defaults are checked at config load, node values in the config-aware flow layer, and the combined values again in `CodexProvider` immediately before spawn. Thus a bypass of either load-time layer still cannot widen the final process authority.

### Environment allowlist (no implicit secret forwarding)

`build_child_env` returns a fresh dict containing exactly the allowlisted keys that exist in the parent, in allowlist order; a key absent from the parent is skipped, never added as empty ([env.py:30](../../../src/wastech_orchestrator/security/env.py#L30)). The parent defaults to the live `os.environ` only when no `parent_env` is passed ([env.py:29](../../../src/wastech_orchestrator/security/env.py#L29)). The child therefore **never** inherits the parent's full environment, so no secret or token (`OPENAI_API_KEY`, `GITHUB_TOKEN`, …) is forwarded implicitly — credentials are configured outside the orchestrator. Every external launch builds its env this way: the orchestrator's run env ([orchestrator.py:910](../../../src/wastech_orchestrator/core/orchestrator.py#L910)), the git manager ([git_manager.py:188](../../../src/wastech_orchestrator/git_manager.py#L188)), and the check runner ([check_runner.py:124](../../../src/wastech_orchestrator/check_runner.py#L124)).

The allowlist intentionally **does** forward `HOME` (and `CODEX_HOME` / `CLAUDE_CONFIG_DIR` when present) so the spawned CLIs find their own auth stores. Codex separates auth from config with its fixed `--ignore-user-config` boundary; the auth path is neither copied nor recorded. Instruction-file discovery is a separate CLI behavior: the agents may still read global `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` plus target-repo instructions — see B18. That prompt context cannot change the adapter's fixed argv/config capability ceiling.

### Controlled Codex invocation (provider-owned enforcement)

Codex `extra_args` validation prevents operator/flow arguments from expanding authority, while the
provider itself establishes the positive effective policy. `_controlled_config_values` marks the
working project untrusted and explicitly sets sandbox network, web search, empty MCP/hooks/skills
surfaces, app defaults, and startup side channels ([codex.py:266-288](../../../src/wastech_orchestrator/providers/codex.py#L266)). `build_codex_argv` adds strict config, ignores user config and user/project rules, and disables every external feature without a typed grant before fresh or resume execution ([codex.py:330-417](../../../src/wastech_orchestrator/providers/codex.py#L330)). The only grant is `AgentRunRequest.network_access`, which enables shell network where supported plus live web search; apps/MCP/browser/computer-use/plugins/hooks remain false.

The provider rejects an offline `danger-full-access` attempt before spawn and writes a
credential/path-free `capabilities.json` for every started attempt
([codex.py:291-327](../../../src/wastech_orchestrator/providers/codex.py#L291),
[codex.py:691-703](../../../src/wastech_orchestrator/providers/codex.py#L691)). Preflight requires
Codex `>= 0.144.4` and all boundary primitives before any model turn. This runtime enforcement is
adapter-owned because only the provider may know Codex CLI syntax; the core/security modules remain
provider-agnostic.

### Front-matter injection scan (belt-and-braces over a structural guarantee)

The primary guarantee is **structural**, not from this scan: task content reaches providers **only as file paths** in `AgentRunRequest` (`task_path`, `plan_path`, …) and is never spliced into argv, env, the command path, or any security setting, so task body text can never become a CLI flag ([injection.py:5-8](../../../src/wastech_orchestrator/security/injection.py#L5)). `scan_frontmatter` is the belt-and-braces layer on top: it inspects **front-matter values only** (never the body — legitimate tasks embed shell snippets) for argv-shaped tokens, returning the first `InjectionFinding` ([injection.py:49-55](../../../src/wastech_orchestrator/security/injection.py#L49)). A string value is rejected when, after stripping, it begins with `-` ("value starts with '-'") ([injection.py:62-63](../../../src/wastech_orchestrator/security/injection.py#L62)), contains any token in `INJECTION_SUBSTRINGS` — `;`, a backtick, `|`, `$(`, `\n`, `\r` ("argv-shaped token") ([injection.py:34](../../../src/wastech_orchestrator/security/injection.py#L34), [:64-65](../../../src/wastech_orchestrator/security/injection.py#L64)), or matches a forbidden-flag shape via `find_forbidden_args` ("forbidden flag shape") ([injection.py:66-67](../../../src/wastech_orchestrator/security/injection.py#L66)). Mappings and lists are scanned recursively, building dotted/indexed key paths like `agents.review` or `contacts[1]` ([injection.py:69-79](../../../src/wastech_orchestrator/security/injection.py#L69)). The policy is **reject, don't sanitize**: a value is refused rather than silently fixed ([injection.py:15-16](../../../src/wastech_orchestrator/security/injection.py#L15)). The scan is run by the validation gate (B16) over the parsed front-matter ([validation_gate.py:226](../../../src/wastech_orchestrator/task/validation_gate.py#L226)).

The "path separator where a non-path field is expected" case is intentionally **not** a distinct reject here: the task `id` is bound by the strict `^[a-z0-9][a-z0-9._-]{0,63}$` regex and route overrides are bound to the `ProviderId` enum, so the only free-text non-path fields (`title`, `contacts`) cannot designate a path ([injection.py:17-21](../../../src/wastech_orchestrator/security/injection.py#L17)).

### Isolation preflight (offline, deterministic, fail-closed, per-provider)

`check_isolation` drives the `strict_isolation` gate: it asks each provider that may run whether its configured isolation can be enabled **without launching any CLI**, so the gate is unit-testable and runs before a branch is ever created ([isolation.py:5-8](../../../src/wastech_orchestrator/security/isolation.py#L5)). It dispatches by `ProviderId` to the adapters' pure `isolation_reasons` ([isolation.py:26-29](../../../src/wastech_orchestrator/security/isolation.py#L26)) and prefixes every reason with the provider id so the caller can surface one combined message ([isolation.py:44](../../../src/wastech_orchestrator/security/isolation.py#L44)). Only providers in `agents.allowed` are checked — every flow node either declares an allowed `provider` or defaults to the (also-allowed) global primary, so a configured-but-unused provider block never bricks an otherwise-valid run ([isolation.py:48-58](../../../src/wastech_orchestrator/security/isolation.py#L48)). The adapter rules are themselves offline and fail-closed: Codex flags a `danger-full-access` sandbox and any forbidden `extra_args` ([codex.py:244-249](../../../src/wastech_orchestrator/providers/codex.py#L244)); Claude flags an unknown/`bypassPermissions`/full-access mode plus forbidden or permission-weakening `extra_args` ([claude.py:332-342](../../../src/wastech_orchestrator/providers/claude.py#L332)).

The pipeline (B06) calls `check_isolation` only when `security.strict_isolation` is true, and a non-empty result raises `PipelineFailed("strict_isolation: …")` before any side effect — no silent downgrade ([orchestrator.py:849-856](../../../src/wastech_orchestrator/core/orchestrator.py#L849)). The CLI `preflight` command reports the same verdict read-only ([cli.py:846-852](../../../src/wastech_orchestrator/cli.py#L846)).

### Permission-profile strictness (conditional fallback only)

`is_same_or_stricter` ranks the profiles `read-only` (rank 0, strictest) below `workspace-write` (rank 1) and returns `rank(candidate) <= rank(reference)` ([profiles.py:17-20](../../../src/wastech_orchestrator/security/profiles.py#L17), [:34](../../../src/wastech_orchestrator/security/profiles.py#L34)). An **unrecognized** profile on either side returns `False` — the orchestrator may never relax policy to enable a fallback ([profiles.py:32-33](../../../src/wastech_orchestrator/security/profiles.py#L32)). The router (B17) uses it for the conditional fallback rule: `authorization_failed` / `permission_denied` may fall back only when the fallback provider's profile is the same or stricter ([router.py:73](../../../src/wastech_orchestrator/routing/router.py#L73)). The flow validator (B29) reuses it twice — to reject a node `permission_profile` that exceeds the flow's `permission_ceiling` ([validator.py:291-292](../../../src/wastech_orchestrator/core/flow/validator.py#L291)) and to require that at least one allowed provider can operate at the ceiling ([validator.py:350-351](../../../src/wastech_orchestrator/core/flow/validator.py#L350)).

### Network policy (a security control, mapped in the adapters)

Network access is **off by default** and is granted only by a flow declaring `network_policy` (B29). The runners reduce that declaration to a single boolean on the request, defaulting `False`. Codex always renders the effective state: without the grant it sets sandbox network `false` and web search `disabled`; with the grant it enables sandbox network where supported and live web search, while every other external channel remains disabled ([codex.py:266-288](../../../src/wastech_orchestrator/providers/codex.py#L266), [codex.py:384-417](../../../src/wastech_orchestrator/providers/codex.py#L384)). Claude appends the web tools `("WebFetch", "WebSearch")` to `--allowedTools`, never relaxing the filesystem permission mode. Network is therefore a toggle layered on top of the isolation ceiling — it cannot widen filesystem permissions, approvals, or unrelated external capabilities.

## Invariants & guarantees

- **Defense in depth on provider arguments:** Claude's common scan and Codex's closed parser run at load and command-build time, so a task or `extra_args` cannot weaken policy even if one layer is bypassed.
- **No implicit secret forwarding:** `build_child_env` returns only allowlisted keys present in the parent — never the full parent env ([env.py:30](../../../src/wastech_orchestrator/security/env.py#L30)); the input mapping is never mutated.
- **Task text can never become a flag:** structurally, task content reaches providers only as file paths; the front-matter scan is an additional, value-only guard ([injection.py:5-8](../../../src/wastech_orchestrator/security/injection.py#L5)).
- **Reject, don't sanitize:** suspect front-matter values are refused, not rewritten ([injection.py:15-16](../../../src/wastech_orchestrator/security/injection.py#L15)).
- **Fail-closed:** an unknown profile in `is_same_or_stricter` → `False` ([profiles.py:32-33](../../../src/wastech_orchestrator/security/profiles.py#L32)); an unknown provider/profile in isolation is skipped/flagged, never assumed safe; `strict_isolation` failure raises before any branch is created ([orchestrator.py:856](../../../src/wastech_orchestrator/core/orchestrator.py#L856)).
- **Offline & pure:** none of the five primitives launch a process or mutate state; `check_isolation` only queries adapter rules ([isolation.py:32-37](../../../src/wastech_orchestrator/security/isolation.py#L32)).
- **Network toggles only network:** the `network_policy`→`network_access` boolean adds sandbox network / web tools but never the filesystem sandbox, approvals ceiling, apps, MCP, browser, computer-use, plugins, or hooks ([codex.py:384-417](../../../src/wastech_orchestrator/providers/codex.py#L384)).

## Dependencies

- **Uses:** B18 (the adapters' `isolation_reasons` rules, dispatched by `check_isolation`); `forbidden_args` is reused internally by `injection.scan_value`.
- **Used by:** B05 (provider-specific config validation); B18 (provider builders repeat their argument policy and own `network_access` mapping); B16 (`scan_frontmatter`); B17 (`is_same_or_stricter`); B29 (common plus Codex node argument validation, ceiling, network policy); B06/B01 (`check_isolation`); B19/B22/B24 (`build_child_env`).

## Tests

- `tests/security/test_forbidden_args.py` — every bypass shape is detected (`--dangerously*`, `--yolo`, `--ignore-rules`, `--sandbox[=] danger-full-access`, `-s`, offending flag not first) and safe args yield no reasons; the sandbox reason names the forbidden value.
- `tests/security/test_env.py` — only allowlisted keys survive; parent secrets (`OPENAI_API_KEY`, `GITHUB_TOKEN`) are never forwarded; missing keys are skipped not blanked; empty allowlist → empty env; allowlist order preserved; defaults to `os.environ`; the parent mapping is not mutated.
- `tests/security/test_injection.py` — clean front-matter passes; argv-shaped values (`-`, `;`, backtick, `|`, `$(`, newline) and forbidden-flag shapes are rejected with the right key/reason; nested dicts/lists get dotted/indexed keys; plain prose passes; the strict task-id regex rejects normalize-changing ids (reject-don't-sanitize).
- `tests/security/test_isolation.py` — adapter `isolation_reasons` flag unknown/full-access profiles, bypass `extra_args`, and `danger-full-access` sandbox; `check_isolation` passes the default config, prefixes reasons with the provider id, checks only `agents.allowed` providers, and ignores a forbidden but unallowed provider.
- `tests/security/test_no_shell_interpolation.py` — structural proof that `subprocess` is used only in the safe runner, `shell=False`, no module enables a shell, and check commands are split into argv (a shell metachar stays a literal token).
- `tests/security/test_denied_reads.py` — `denied_read_paths` harvesting (B21) feeds redaction and builds Claude `Read(...)` deny patterns (adjacent secret-handling coverage that shares this test directory).

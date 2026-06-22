# Provider config: operator-owned full access (Codex sandbox + Claude permissions), drop `max_budget_usd`, explicit model/reasoning defaults

Status: **done** (2026-06-22, config v14 — see the Done entry in [archive/follow_ups_history.md](archive/follow_ups_history.md)) Date: 2026-06-22 Owner: Vladimir Makarevich

Detail file for the [follow_ups.md](follow_ups.md) item "Provider config cleanup (operator-owned full access, drop `max_budget_usd`, explicit model/reasoning defaults)". Three independent provider-config changes, bundled because they all touch the same surface (`config.example.yaml`, the schema/loader, `install/config_writer.py`, the provider adapters) and ship together as one config-version bump.

## Requirements (operator request, 2026-06-22)

1. **Allow selecting the full-access mode of each provider** — the orchestrator must not _hard-forbid_ it. For Codex that is the `--sandbox danger-full-access` value; for Claude it is the `bypassPermissions` permission mode (and, more generally, a `--permission-mode` override that escalates above the resolved profile). Using either is the operator's responsibility (they author the config/flow and own the risk).
2. **Remove `max_budget_usd`** — it is declared but read nowhere; delete the dead field.
3. **Ship explicit default model + reasoning** for both providers instead of the empty `model: ""` / `reasoning: null` placeholders.

## Background (verified in code)

- **Codex full access** (`--sandbox danger-full-access`) is hard-banned in two layers: the shared detector `find_forbidden_args` rejects `--sandbox danger-full-access` ([forbidden_args.py:58-59](../../src/wastech_orchestrator/security/forbidden_args.py#L58-L59)), and the adapter rejects a resolved full-access sandbox ([codex.py:125-126](../../src/wastech_orchestrator/providers/codex.py#L125-L126)). Separately, `strict_isolation` preflight reports it as "no isolation" via `isolation_reasons` ([codex.py:189-190](../../src/wastech_orchestrator/providers/codex.py#L189-L190)).
- **Claude full access** (the `bypassPermissions` mode) is hard-banned by `_reject_weaker_permission_override`, which rejects `--permission-mode bypassPermissions` _and_ any `--permission-mode` override more permissive than the resolved profile — called both at runtime in `build_claude_argv` ([claude.py:229](../../src/wastech_orchestrator/providers/claude.py#L229)) and in `isolation_reasons` ([claude.py:281](../../src/wastech_orchestrator/providers/claude.py#L281)). The `--dangerously-skip-permissions` flag form is separately blocked by the `--dangerously*` prefix rule in `find_forbidden_args`. (`map_permission` rejecting `FORBIDDEN_SANDBOX_VALUE` at [claude.py:161-162](../../src/wastech_orchestrator/providers/claude.py#L161-L162) is about the `permission_profile` _field_, not the `--permission-mode` extra_args path — Claude has no `danger-full-access` profile.)
- `max_budget_usd` is declared ([config/schema.py:140](../../src/wastech_orchestrator/config/schema.py#L140)), parsed ([loader.py:320](../../src/wastech_orchestrator/config/loader.py#L320), [loader.py:342](../../src/wastech_orchestrator/config/loader.py#L342)), and written by the wizard for `claude` only ([config_writer.py:61](../../src/wastech_orchestrator/install/config_writer.py#L61)) — but **consumed nowhere** (grep across `src/` and `tests/` finds no reader; only `max_turns` reaches an argv, at [claude.py:260-261](../../src/wastech_orchestrator/providers/claude.py#L260-L261)). Consistent with the P4 decision to scope budgets out (no fatal budget check; runtime clamp).
- `model`/`reasoning` are already wired for **both** providers (codex `--model`/`--reasoning-effort` at [codex.py:157-172](../../src/wastech_orchestrator/providers/codex.py#L157-L172); claude `--model`/`--effort` at [claude.py:245-252](../../src/wastech_orchestrator/providers/claude.py#L245-L252)). Empty `""`/`null` means "omit the flag, let the CLI pick its own default". This requirement only changes the **shipped default values**, not the resolution logic.

## Decisions (locked)

1. **Full access is gated by the existing `strict_isolation` knob, not made unconditional — symmetrically for both providers.** Lift the _absolute_ ban (Codex `FORBIDDEN_SANDBOX_VALUE` checks; Claude `_reject_weaker_permission_override`) so the orchestrator no longer hardcodes a refusal — but keep `strict_isolation: true` (the default) rejecting full access at preflight, because full access _is_ "no isolation" (security rule #3). The operator opts in deliberately by setting `security.strict_isolation: false`. This satisfies "no orchestrator-imposed restriction / operator's responsibility" while preserving the fail-closed-by-default posture and security rule #3. A truly unconditional allow (ignoring `strict_isolation`) is rejected: it would silently void the `strict_isolation` invariant with no opt-in.
2. **The `--dangerously*` / `--yolo` / `--ignore-rules` namespace stays absolutely forbidden for both providers** — those disable approvals/hook-trust wholesale, broader than selecting a full-access _value/mode_. So the sanctioned full-access path is the **structured** one: Codex `--sandbox danger-full-access` (or the `sandbox:` field), Claude `--permission-mode bypassPermissions` in `extra_args`. The `--dangerously-skip-permissions` _flag_ form remains banned even though it is functionally equivalent to `bypassPermissions` — keeping the `--dangerously*` line bright is the parity rule. (Revisit only if an operator specifically needs the flag form.)
3. **`""`/`null` stays a valid "use the CLI default" sentinel.** Requirement #3 changes only the _shipped_ default values in the template and the `init` wizard; an operator may still blank a field to fall back to the CLI/account default. The resolution chain (`model_for`/`reasoning_for`) is unchanged.

## Target design

### 1. Operator-owned full access (both providers)

- `security/forbidden_args.py` — drop the `value == FORBIDDEN_SANDBOX_VALUE` branch ([forbidden_args.py:58-59](../../src/wastech_orchestrator/security/forbidden_args.py#L58-L59)) so `--sandbox danger-full-access` in `extra_args` is no longer a hard config/runtime error. Keep `FORBIDDEN_SANDBOX_VALUE` exported (still used by `isolation_reasons`). Keep the malformed-`--sandbox`-with-no-value rejection and all `--dangerously*`/`--yolo`/`--ignore-rules` handling.
- `providers/codex.py` — delete the `if sandbox == FORBIDDEN_SANDBOX_VALUE: raise` guard in `build_codex_argv` ([codex.py:125-126](../../src/wastech_orchestrator/providers/codex.py#L125-L126)); `--sandbox danger-full-access` now passes through to the CLI. **Keep** `isolation_reasons` reporting it ([codex.py:189-190](../../src/wastech_orchestrator/providers/codex.py#L189-L190)) — that is the `strict_isolation` gate (Decision 1).
- `providers/claude.py` — stop hard-raising on a permissive `--permission-mode` override: in `build_claude_argv`, drop the `_reject_weaker_permission_override(...)` call ([claude.py:229](../../src/wastech_orchestrator/providers/claude.py#L229)) so `extra_args: ["--permission-mode", "bypassPermissions"]` (or any escalation above the profile) passes through to the CLI (appended after the orchestrator's own `--permission-mode`, so the CLI's last-wins resolution applies). **Keep** `isolation_reasons` calling `_reject_weaker_permission_override` ([claude.py:281](../../src/wastech_orchestrator/providers/claude.py#L281)) so the override is reported as "no isolation" and `strict_isolation: true` blocks it at preflight (the gate). Leave `map_permission` ([claude.py:161-162](../../src/wastech_orchestrator/providers/claude.py#L161-L162)) untouched (a different field; Claude has no full-access _profile_). The `--dangerously-skip-permissions` ban via `find_forbidden_args` stays (Decision 2). The `_reject_weaker_permission_override` helper is retained — it is now only the preflight reporter, no longer a runtime hard-raise.
- Net effect: with `strict_isolation: true`, a full-access Codex sandbox _or_ a Claude `--permission-mode` escalation in the provider config fails preflight (`check_isolation` → reason → error). With `strict_isolation: false` both launch. No new config field.

### 2. Remove `max_budget_usd`

Mirror the v12 removal of `min_size_signal`/`commit_per_subtask` and the v11 `auto_merge_allow_per_task` removal (tolerate-on-load + strip-on-upgrade), so a stale config still loads fail-open:

- `config/schema.py` — delete `max_budget_usd` from `ProviderConfig` ([schema.py:140](../../src/wastech_orchestrator/config/schema.py#L140)) and fix the field comment on [schema.py:137](../../src/wastech_orchestrator/config/schema.py#L137) (drop the `max_budget_usd` mention).
- `config/loader.py` — remove `max_budget_usd` from the `_build_provider` allowed key set ([loader.py:320](../../src/wastech_orchestrator/config/loader.py#L320)) and add it to that call's `tolerated=` set; delete the `max_budget_usd=_opt_float(...)` constructor arg ([loader.py:342](../../src/wastech_orchestrator/config/loader.py#L342)).
- `config/upgrade.py` — add `("agents.providers.claude", "max_budget_usd")` and `("agents.providers.codex", "max_budget_usd")` to `_REMOVED_KEYS` ([upgrade.py:32-39](../../src/wastech_orchestrator/config/upgrade.py#L32-L39)) so `upgrade-config` strips it from any operator config, and extend the explanatory comment with the new version.
- `install/config_writer.py` — delete the `block["max_budget_usd"] = None` line ([config_writer.py:61](../../src/wastech_orchestrator/install/config_writer.py#L61)); `max_turns` stays.
- `templates/config.example.yaml` + repo-root `config.example.yaml` — delete the `max_budget_usd: null` line ([config.example.yaml:45](../../src/wastech_orchestrator/templates/config.example.yaml#L45)).

### 3. Explicit default model + reasoning

Set concrete defaults in **both** the packaged template (`templates/config.example.yaml`) and the repo-root `config.example.yaml`, and in the `init` wizard's `_provider_block` ([config_writer.py:54-66](../../src/wastech_orchestrator/install/config_writer.py#L54-L66)) — these are the two places defaults are materialized. No loader/schema change (the fields already exist).

**Candidate values — confirm exact ids/levels at implementation (operator's cost/quality call):**

| provider | `model` (candidate) | `reasoning` (candidate) |
| --- | --- | --- |
| `claude` | `claude-sonnet-4-6` (cost-default) or `claude-opus-4-8` (max-quality; already the example in [task-authoring.md:280](../task-authoring.md#L280)) | `high` |
| `codex` | confirm against the installed Codex CLI's accepted `--model` values before pinning | `high` (Codex caps at `xhigh`; `max`→`xhigh`) |

Do **not** invent a Codex model id — verify it against the CLI in use. Keep `discovery.model`/`reasoning` (the cheap agent-discovery knob, [config.example.yaml:91-92](../../src/wastech_orchestrator/templates/config.example.yaml#L91-L92)) separate and unchanged.

## Config version

Bump `CONFIG_SCHEMA_VERSION` 12 → next, and `schema_version:` in both `config.example.yaml` files to match. **Coordinate with the concurrent [stage-enum-removal](stage-enum-removal.md) task, which also bumps 12 → 13** — whichever lands first takes 13, the other takes 14. An old config carrying `max_budget_usd` loads fail-open (tolerated) and is stripped by `upgrade-config`.

## Test impact

- `tests/security/test_forbidden_args.py` (or equivalent) — the `--sandbox danger-full-access` case flips from "rejected" to "allowed"; assert the `--dangerously*`/`--yolo`/`--ignore-rules` (incl. Claude `--dangerously-skip-permissions`) and malformed-`--sandbox` cases still reject.
- Codex adapter tests — a `danger-full-access` sandbox now builds an argv (no `ProviderError`); keep a `strict_isolation` preflight test asserting `check_isolation` still flags it (the gate, not the adapter, blocks it).
- Claude adapter tests — `build_claude_argv` with `extra_args: ["--permission-mode", "bypassPermissions"]` now builds an argv (no `ProviderError`); a `strict_isolation` preflight test asserts `check_isolation`/`isolation_reasons` still flags that config (the gate). Keep a test that `--dangerously-skip-permissions` is still rejected.
- Config loader/upgrade tests — a config with `max_budget_usd` loads without an "unknown key" error (tolerated); `upgrade-config` reports it in `removed`; round-trip no longer emits it.
- `tests/install/test_config_writer.py` — provider blocks no longer contain `max_budget_usd`; assert the new explicit `model`/`reasoning` defaults are written.
- Any test asserting `ProviderConfig.max_budget_usd` — delete.

## Docs impact

- `docs/configuration.md` — drop the `max_budget_usd` row; update the `model`/`reasoning` field descriptions and any `model: ""` examples to the new explicit defaults; in the `extra_args` / `strict_isolation` sections, replace "full access is always forbidden" with "permitted only under `strict_isolation: false` (operator owns the risk)" for **both** the Codex `--sandbox danger-full-access` and the Claude `--permission-mode bypassPermissions` cases. The forbidden-example block must move `--sandbox danger-full-access` (and add `--permission-mode bypassPermissions`) from "always forbidden" to "gated by `strict_isolation`", while keeping the `--dangerously*` examples in "always forbidden". (Note: the standalone `extra_args` reference subsection added 2026-06-22 documents the _current_ ban — update it in lock-step when this lands.)
- `.agents/rules/security.md` — rule #10 currently says options that bypass the sandbox/permissions are "forbidden by the configuration validator". Reword to carve out the structured full-access _value/mode_ (Codex `danger-full-access`, Claude `bypassPermissions`): gated by `strict_isolation`, not absolutely forbidden; the `--dangerously*`/`--yolo`/`--ignore-rules` flags remain absolutely forbidden. Rule #3 (strict_isolation) is unchanged and is now the sole gate for full access.
- `docs/functional/` + likec4 — fold the security-policy wording change into the already-pending functional-map `file:line` re-sync pass; no new diagram.
- `src/wastech_orchestrator/worc/decision-guide.md` — if it mentions the full-access ban, note the `strict_isolation: false` opt-in (both providers).

## Risks / out of scope

- **Loosening a security ban (both providers).** #1 narrows an absolute prohibition to a `strict_isolation`-gated one for Codex full-access _and_ Claude permission escalation/bypass. Mitigation: the default stays fail-closed (`strict_isolation: true` rejects full access at preflight before any branch); the opt-in is one explicit, documented config flag; the `--dangerously*`/`--yolo`/`--ignore-rules` bans are untouched (Decision 2). This must be reviewed against `.agents/rules/security.md` rule #10 as part of the change (the rule is being amended, not silently violated).
- **`strict_isolation` gates only provider-level `extra_args`, not flow-node `extra_args`.** Both `isolation_reasons` functions inspect the provider config (`config.sandbox`/`config.extra_args`), not a flow node's `extra_args`; the flow validator only runs `find_forbidden_args` on `node.extra_args`, with no `strict_isolation` check. So after this change a flow node could enable full access even under `strict_isolation: true`. Decide at implementation: (a) accept it — flow nodes are operator-authored, same "operator owns it" stance as the config; or (b) extend the gate so node-level full access is also checked against `strict_isolation` at flow resolution. Recommend (b) for a true global gate; flag explicitly either way.
- **Pinning a model id ages.** Explicit defaults make routing visible and predictable but pin an id that can go stale and change cost. Mitigation: `""`/`null` remains a valid "use CLI default" sentinel (Decision 3); operators who want passthrough blank the field.
- **Behavior change from #3.** Shipping a concrete `model` means new installs pass `--model <id>` where they previously omitted it (CLI/account default). Call this out in the release/upgrade notes; `upgrade-config` does **not** overwrite an operator's existing `model`/`reasoning` (operator values win), so only fresh `init` configs get the new defaults.
- **Out of scope:** allowing the Claude `--dangerously-skip-permissions` _flag_ form (Decision 2 keeps `--dangerously*` banned; the `bypassPermissions` mode is the sanctioned path); any per-stage/per-task budget feature (separate [README.md](README.md) "Per-task budget limit" item); the `config.yaml` `stage_defaults` work (separate follow-up).

## Acceptance

- `ruff`, `mypy`, `pytest` green; no remaining reader/field for `max_budget_usd` (grep clean across `src/` and `tests/`).
- With `security.strict_isolation: false`, a Codex provider selecting `--sandbox danger-full-access` **and** a Claude provider with `extra_args: ["--permission-mode", "bypassPermissions"]` both launch; with `strict_isolation: true` (default) each fails preflight with a clear isolation error before any branch is created.
- `--dangerously-bypass-approvals-and-sandbox`, `--yolo`, `--ignore-rules`, and Claude `--dangerously-skip-permissions` are still rejected at config load.
- A stale config containing `max_budget_usd` loads without error and is stripped by `upgrade-config`; a fresh `init` writes explicit `model`/`reasoning` for both providers and no `max_budget_usd`.

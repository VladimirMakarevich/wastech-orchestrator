# Phase 1 — Contracts and configuration

**Goal:** establish the typed foundation every later phase depends on — the provider contract and
canonical enums, the configuration schema + a fail-closed validator, the task data model, and the
already-shipped `init`/templates surface. No external process is launched in this phase.

**Spec:** §4.3 (contracts), §7.1 (error classes), §11 (configuration), §20 (`init`), §21.4 (footprint
validation). **Rules:** [architecture.md](../rules/architecture.md), [coding-style.md](../rules/coding-style.md).

**Prerequisites:** none — this is the first phase. Baseline already in the repo: `providers/base.py`,
`cli.py` (`init`), and the packaged `templates/`.

---

## Logical blocks

### 1.1 Confirm the provider contract (`providers/base.py`)
Already present; this block ratifies it as the frozen contract for the project.
- `Stage`, `RunStatus`, `ErrorClass` are `StrEnum`s with exactly the spec values (§4.3, §7.1).
- `AgentRunRequest`, `AgentRunResult`, `ProviderHealth`, `NormalizedError` are frozen dataclasses
  with the §4.3 fields and nothing more — no hidden state channels.
- `AgentProvider` is a `runtime_checkable` Protocol: `id`, `preflight() -> ProviderHealth`,
  `run(AgentRunRequest) -> AgentRunResult`.
- `ProviderError(error_class, message)` + `FALLBACK_ELIGIBLE` (the §7.2 unconditional set; the
  conditional `authorization_failed`/`permission_denied` are decided by the Router, not here).
- **Do not** add convenience methods that build CLI strings — that belongs to adapters (P2/P3).

### 1.2 Config schema (`config/schema.py`)
Frozen dataclasses mirroring §11, one per block: root `OrchestratorConfig`,
`OrchestratorRuntimeConfig` (`auto_mode.enabled=false`), `RepoConfig`, `AgentsConfig`
(`allowed`, `max_stage_attempts`, `max_fix_cycles`, `max_total_fix_iterations`,
`DecompositionConfig`, `routing`, `providers`), `ProviderConfig` (per provider — note Codex has
`sandbox`, both have `permission_profile`, `extra_args`, `model`, `timeout_seconds`),
`SecurityConfig`, `ValidationConfig`, `ChecksConfig`, `GitConfig` + `FootprintConfig`,
`TelegramConfig` (present but inert in v1).
- A `RouteConfig` is `{primary, fallback}`; routes are keyed by `Stage`.
- Use the canonical enums for provider ids and stages — no bare strings.

### 1.3 Config loader (`config/loader.py`)
- Read YAML with `yaml.safe_load`; reject a non-mapping root.
- Map into the schema dataclasses. Unknown **top-level** and unknown **route** keys are an error,
  not silently dropped (fail-closed, §11). Surface every problem via a typed
  `ConfigError` (not bare strings) — collect and report all issues, not just the first.
- No side effects at import; loading is an explicit call.

### 1.4 Config validator (`config/validation.py`) — the heart of this phase
Enforce every §11 requirement so an unsafe or contradictory config never reaches the pipeline:
- unknown route keys → error; a route naming a stage outside the canonical set → error.
- `primary`/`fallback` must be in `agents.allowed`; neither may reference a provider absent from
  `agents.providers`.
- `orchestrator.auto_mode.enabled` defaults to `false` and must be a boolean; it controls only
  whether `watch` may pick the next pending task after terminal cleanup.
- `max_total_fix_iterations >= max_fix_cycles` (§8.1 hard-cap invariant).
- `decomposition.max_subtasks >= 2`; decomposition off unless `enabled: true`.
- `extra_args` validated against a provider allowlist; **reject any flag that disables the
  sandbox/permissions** (e.g. Codex `--dangerously-bypass-approvals-and-sandbox` /
  `--sandbox danger-full-access`; Claude `--dangerously-skip-permissions`). This is the
  config-time half of the "security can't be weakened" invariant — the adversarial tests live in P6.
- `git.footprint` (§21.4): reject `external` + `exclude_local|commit` and `in_repo` + `none`; when
  `location: external`, `external_root` must normalize to a path **outside** `repo.local_path`
  (anti-traversal). 
- Legacy Codex-only config → migrate to a Codex route for all agent stages **with a warning** (§11).
- A task-override note for P4: a task may never change a provider's command, `extra_args`, or any
  security setting — encode that as a pure helper here so P4 reuses it.

### 1.5 Task data model (`task/model.py`)
Define the normalized task structures the parser (P5) will populate — the front matter from §5/§19.3:
- `id` (must match `^[a-z0-9][a-z0-9._-]{0,63}$`), `title`, body **Description** (required);
  `refined: bool=false`, `decompose: tri-state (True/False/None)`, `agents` (per-stage route
  override map), `contacts: list[str]` (optional).
- `NormalizedTask` frozen dataclass + the front-matter schema constants (allowed top-level keys,
  required fields). The actual parsing, the §19 gate, and duplicate-id detection are **P5** (they
  need the State Store + ledger); here we only define the shapes and the id-regex constant so both
  phases share one source of truth.

### 1.6 `init` + templates — confirm DoD (already implemented)
Ratify the existing `cli.py` `init` and packaged `templates/` against §20:
- idempotent: a second run is all-skipped and exits 0; **never** overwrites `config.yaml`.
- `--dry-run` writes nothing; `--force` re-copies only `templates/` files, never `config.yaml`.
- each `--git-mode` (`external` / `in_repo_exclude` / `in_repo_commit`) seeds the matching
  `git.footprint.{location,tracking}` defaults.
- templates are read via `importlib.resources` so `init` works from an installed wheel.
- `config.example.yaml` stays in sync with §11 and the schema in 1.2.

---

## Tests (unit only — no processes this phase)

- Config validation: every reject path in 1.4 (unknown route key, forbidden provider, the
  `max_total >= max_fix_cycles` rule, `max_subtasks < 2`, each illegal footprint pairing,
  `external_root` inside `local_path`, a sandbox-bypass `extra_args` flag, a non-boolean
  `orchestrator.auto_mode.enabled`), plus the legacy-config migration warning.
- Schema/loader round-trip: the packaged `config.example.yaml` loads and validates clean.
- Task model: id-regex accepts/rejects the documented cases; tri-state `decompose` parsing.
- `init` idempotency (§20): second run all-skipped & exit 0; `config.yaml` never overwritten;
  `--dry-run` is a no-op; each `--git-mode` writes the right footprint defaults; templates are
  discoverable from a built wheel.

## Definition of Done

- [ ] `providers/base.py` ratified: enums, contract dataclasses, Protocol, `FALLBACK_ELIGIBLE` all
      match §4.3/§7.1; no CLI-string logic leaked in.
- [ ] Config schema dataclasses cover every §11 block, including
      `OrchestratorConfig.orchestrator.auto_mode`, keyed by canonical enums.
- [ ] Loader rejects unknown top-level/route keys and non-mapping roots via typed `ConfigError`.
- [ ] Validator enforces every §11/§21.4 rule, including the `extra_args` sandbox-bypass rejection
      and the footprint pairings; legacy Codex-only config migrates with a warning.
- [ ] Task data model + shared id-regex/field constants defined (parsing deferred to P5).
- [ ] `init` + templates DoD (§20) confirmed by tests; `config.example.yaml` in sync with §11.
- [ ] `ruff check .`, `mypy src`, `pytest` green (`/run-checks`).

## Not in this phase

- Parsing real task files and the §19 validation gate (P5 — needs State Store + ledger).
- Any subprocess launch, redaction, or artifact writing (P2).
- Routing resolution and fallback (P4).

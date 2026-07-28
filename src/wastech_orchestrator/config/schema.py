"""Configuration schema.

Frozen dataclasses mirroring the config.yaml structure, one per block, keyed by the canonical
enums from ``providers.base``. This module holds *shapes only* — no parsing, no validation, and no
CLI syntax. The loader (``config.loader``) maps YAML into these types; the validator
(``config.validation``) enforces the semantic rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from wastech_orchestrator.providers.base import ProviderId

# The config.yaml format version. Bumped only when the *format* changes (not on every release). The
# loader refuses a config whose ``schema_version`` is newer than this (fail-loud); an absent or
# older value is accepted — the older case is the hook for a future migration runner. See the
# spec's "Versioning & compatibility" section.
# v2: added the opt-in ``git.auto_merge*`` keys (auto-merge bypass). Old (v1/absent) configs omit
# them and take the safe ``false`` defaults — no migration flips anything.
# v3: added the optional ``prompts`` block (operator-customizable stage prompts). Old (v1/v2/absent)
# configs omit it and take the safe defaults (packaged templates, append mode) — no migration.
# v4: added the optional ``agents.skip_stages`` / ``agents.allow_review_skip`` keys (stage-skip
# control). Old configs omit them and take the safe defaults (no skips, review-skip disallowed) — no
# migration flips anything.
# v5 (2026-06-14, post-test-run): adds the optional `skills:` block and the
# `checks.discovery.{run_at_task_start,approve_command_changes}` keys. All are
# backward-compatible (absent => safe defaults); `upgrade-config` adds them to an older config.
# v6 (2026-06-14, prompt-templates-simplification): prompt overrides are now auto-detected by file
# presence in `prompts.templates_dir` — the `prompts.overrides` map and `prompts.strict` flag are
# removed, and `prompts.mode` now defaults to `replace`. Legacy `overrides`/`strict` keys are
# tolerated (ignored) on load; `upgrade-config` strips them. Old configs still load fail-open.
# v7 (2026-06-16, worc-home-consolidation): the git footprint collapses to a single canonical
# layout — all runtime files live under the gitignored `<repo>/.worc/` home, while the task file and
# its `<id>.summary.md` stay at the repo root and are audit-committed. The `git.footprint.location`,
# `.tracking`, and `.external_root` keys are removed; `git.footprint` now carries only
# `audit_commit_message` + `audit_on_branch`.
# v8 (2026-06-16, prompt-audit): adds the optional top-level `prompt_audit` flag (default false).
# Old configs omit it and take the safe `false` default — no migration flips anything;
# `upgrade-config` adds it from the packaged template. A per-task `prompt_audit` overrides it.
# v9: the `prompts` block (`templates_dir`/`mode`) is removed — a flow
# node's prompt template is its `role_file`, not a stage-indexed packaged default. `upgrade-config`
# strips an operator's `prompts:` block; old configs still load fail-open (the key is ignored).
# v10 (2026-06-19, flexible-flow stage-skip): the global `agents.skip_stages` list is removed — with
# fully configurable flows, "skip a stage for every task" is redundant (drop the node from the flow,
# or author an operator flow). Per-task `stages.<stage>.enabled: false` survives as a bounded,
# validated toggle, and `agents.allow_review_skip` stays (now gating only the
# per-task review skip). `upgrade-config` strips `agents.skip_stages`; old configs still load
# fail-open (the key is tolerated/ignored).
# v11 (2026-06-19, flow-engine PRE.1/PRE.2): provider routing moves onto the flow node. The
# stage-keyed `agents.routing` block is removed — a node declares its own `provider` (else the
# global primary), and exactly one `agents.providers.<id>.primary: true` marks that global primary
# (the sole infra-fallback target). The per-task auto-merge gate `git.auto_merge_allow_per_task` is
# also removed: a per-task `auto_merge` now wins outright (PRE.2). `upgrade-config` strips both dead
# keys; old configs still load fail-open (the keys are tolerated/ignored).
# v12 (2026-06-22, audit remediation #5): the decorative `agents.decomposition.min_size_signal` and
# `agents.decomposition.commit_per_subtask` keys are removed — neither was ever read (subtask commit
# is unconditional; no prompt consumes the size signal). `upgrade-config` strips both; old configs
# still load fail-open (the keys are tolerated/ignored).
# v13 (2026-06-22, Stage-enum removal): the `Stage` enum is deleted and per-task skip is re-founded
# on flow node ids (`nodes.<node-id>.enabled: false`). `agents.allow_review_skip` is removed — there
# is no `review`-special-case; which nodes are safe to disable is the operator's flow-authoring
# responsibility. `upgrade-config` strips `agents.allow_review_skip`; old configs still load
# fail-open (the key is tolerated/ignored).
# v14 (2026-06-22, provider-config-cleanup): the unused `agents.providers.<p>.max_budget_usd` key
# is removed — declared/parsed but read nowhere (only `max_turns` reaches an argv). `upgrade-config`
# strips it from both provider blocks; old configs still load fail-open (the key is tolerated). (The
# same bump also ships explicit default `model`/`reasoning` and makes each provider's full-access
# mode operator-selectable under `security.strict_isolation: false` — neither is a format change.)
# v15 (2026-06-23, checks-monorepo): a *format* change to the `checks` block. The whole
# `checks.discovery` block (modes / agent-fallback / refresh / approval) is removed and the flat
# `checks.commands` list is replaced by named `checks.command_sets` — each a `paths` glob list +
# optional `timeout_seconds` / `skip_if_unavailable` + structured `commands`, every command gaining
# an optional repo-relative `cwd`. The only check behavior is "operator lists command sets; empty =
# no gate". A stale `discovery` / `commands` key is tolerated (ignored) on load and `upgrade-config`
# strips it, but `command_sets` is operator-authored — never auto-generated (no host inspection).
# v16 (2026-06-25, deletion-approval-allowlist): a *format* add of optional
# `security.deletion_approval_exempt_paths` — a list of repo-relative globs whose deletions/renames
# are exempt from the dangerous-diff approval gate. Default `[]` = today's
# behavior (everything gated). It filters only the deletion classification; dependency manifests are
# never exemptable. Old configs load fail-open (the key defaults to empty) and `upgrade-config`
# adds it from the template.
# v17 (2026-06-26, configurable-tasks-dir): a *format* add of the optional `paths` block with
# `tasks_dir` (default "tasks") — the repo-relative directory holding the pending/done/failed task
# lifecycle. Lets an operator avoid colliding with a repo that already uses `tasks/`.
# Validated repo-relative (no `..`/absolute, never under `.worc/`). Default reproduces today's
# behavior; old configs omit it and take the default, and `upgrade-config` adds it from the
# template.
# v18 (2026-06-26, queue-tag): a *format* add of the optional `orchestrator.queue` (default
# "default") — the instance's queue selector. With several worc instances sharing one
# git-distributed task pool, an instance only picks a pending task when
# `task.queue == orchestrator.queue` (plain string equality, static partitioning, no balancing).
# Validated non-empty. Default reproduces today's behavior; old configs omit it and take "default",
# and `upgrade-config` adds it from the template.
# v19 (2026-06-27, skills-selection-rework): the `skills:` block is replaced outright (greenfield).
# `scan_root`/`exclude` are gone — discovery is automatic and whole-repo (`git ls-files` for tracked
# `**/SKILL.md`), and operators pin skills per flow node instead of denylisting. The block shrinks
# to `dynamic` (the once-per-task supervisor proposal of a node→skills map; on, skip-when-empty) and
# `strict` (whether an unresolved operator pin stops the task). `upgrade-config` strips the two
# removed keys.
# v20 (2026-06-27, transient-provider-failure-recovery): a *format* add of the optional
# `agents.retry` block — `{max_attempts, base_delay_s, max_delay_s, max_blocked_s}` — the bounded
# same-provider transient-retry policy (Option A) plus the B-lite soft-pause ceiling. Absent => safe
# defaults; old configs load fail-open and `upgrade-config` adds it from the template. The values
# v20 shipped were `max_attempts=2, base_delay_s=2.0, max_delay_s=30.0, max_blocked_s=3600.0`;
# `max_blocked_s` was later raised to 21600.0 (6h > a provider's ~5h usage window, so a
# rate-limited task waits out the reset instead of failing an hour in) — `RetryConfig` below is the
# live source for all four.
# v21 (2026-06-27, telegram-step-trace): a *format* add of the optional `telegram.trace` bool
# (default false) — a one-way, best-effort live progress feed that pushes one message per flow node
# finish (`<emoji> <node-id> → <outcome>`, node id + outcome only, no secrets). A no-op when
# Telegram is disabled. Old configs omit it and take false; `upgrade-config` adds it from template.
# v22 adds two fail-closed operator-confirmation gates for full-auto `watch`: optional
# `orchestrator.auto_mode.confirm_next_task` (Telegram approve/deny before claiming a pending task)
# and optional `agents.providers.<id>.max_turns_gate` (a continue/stop prompt when a Claude run hits
# its turn cap). Both default false and require `telegram.enabled` when on (preflight-enforced).
# v23 (2026-06-27, log-management): a *format* add of the optional `logging` block — `level`
# (debug|info|warning|error, default info) persists the operator trace verbosity (the `--log-level`
# flag overrides it) and `artifacts` (minimal|standard|full, default standard) controls which
# per-attempt provider files are kept under `logs/<task-id>/stages/.../<attempt>-<provider>/`
# (minimal=result.json only; standard=+stdout/stderr; full=all). Absent => safe defaults; old
# configs load fail-open and `upgrade-config` adds it from the template.
# v24 (2026-06-30, memory-subsystem foundations): a *format* add of the optional `memory` block — a
# global enable/disable plus bounded knobs (short-term TTL, per-node packet caps, promotion
# thresholds, background-cleanup budget) for the persistent repo-scoped memory subsystem.
# Absent block => disabled (today's behavior exactly: no store, no delta,
# empty packets, CLI no-op, no cleanup); the packaged template ships `enabled: true` for a fresh
# install. Old configs load fail-open with defaults and `upgrade-config` adds it from the template.
# No behavior consumes the knobs yet (phase 01 wires the shape only).
# v25 (2026-07-03, trust-levels-danger-approval): replaces `security.deletion_approval_exempt_paths`
# with the approval-policy knob `security.trust_level` (strict|auto, default `auto` at install; the
# dataclass default is the safe fallback `strict`) plus `security.protected_paths` (repo-relative
# globs that ALWAYS require approval, at any trust_level — the always-ask floor). `strict` keeps the
# old behavior (gate every deletion/rename or dependency-manifest edit); `auto` turns the diff-shape
# gate off so only a `protected_paths` match raises approval. The old key is removed outright
# (greenfield, no back-compat) — a config still carrying it is rejected as an unknown key.
# v26 (2026-07-04, branch-mode): adds the optional `repo.branch_mode` (new|existing|current, default
# `new`) — the instance default for where task git operations point. Absent => `new` (today's
# create-from-base behavior exactly); a per-task `branch_mode` overrides it. Old configs load
# fail-open with the default and `upgrade-config` adds it from the template.
# v27 (2026-07-07, supervisor-provider): adds the optional `supervisor.provider` (codex|claude,
# default null = inherit the global primary). Lets the supervisor layer pin its own provider so its
# `model` reaches a provider that accepts it (fixes claude-model-on-codex under a codex primary);
# validated ∈ `agents.allowed` and for reasoning support, symmetric with flow nodes. Absent =>
# inherit primary (today's behavior exactly). Old configs load fail-open; `upgrade-config` adds it.
# v28 (2026-07-08, custom tool-nodes): adds the optional `tools` block with
# `default_timeout_seconds` (default 3600 = 1h) — the flow-wide default wall-clock timeout for a
# `tool` node whose own `timeout_seconds` is unset. Absent block => 3600s exactly; a per-node
# `timeout_seconds` overrides it. Old configs load fail-open with the default; `config_writer`
# writes the block on a fresh install (discoverability, like `logging`).
# v29 (2026-07-11, agent-native-memory-opt-in): adds the optional Claude-only
# `agents.providers.claude.allow_native_memory` bool (default false) — an operator opt-in that, when
# true, drops the native-memory deny so Claude Code's own auto-memory (`~/.claude/projects/
# <repo>/memory/`) persists across tasks. Off (default/absent) => the deny stays in place (today's
# behavior exactly). It relaxes a security control (that store is unaudited, no redaction
# guarantee), so it is a conscious opt-in: `config_writer` does NOT write it on a fresh install and
# `upgrade-config` does not add it — documented in `config.example.yaml` only. Old configs load
# fail-open with the safe default. Inert on Codex (no deny to gate there).
# v30 (2026-07-14, cleanup-checkout-opt-out): adds the optional tri-state
# `repo.checkout_base_on_cleanup` (bool | null, default null) gating whether cleanup returns
# the tree to `base_branch`. null defers to `branch_mode` (new returns; existing/current
# stay); false never returns (global off); true forces new + existing to return; current always
# stays. Old (absent) configs take null => today's `new`-mode behavior is preserved. `config_writer`
# does NOT write it on a fresh install; documented in `config.example.yaml` only.
# v31: a Codex node's isolation is a generated permission profile driven by
# `permission_profile`; the legacy `agents.providers.codex.sandbox: read-only|workspace-write` is
# rejected by the validator and folded into `permission_profile` by `upgrade-config`. `sandbox`
# survives only as the `danger-full-access` escape (gated by `strict_isolation: false`).
# v32: adds the optional `logging.clean_runs_on_success` (bool, default true) — a successful task
# evicts its own per-task `runs/` subtree (frozen bundles + sealed exchanges). Old (absent) configs
# take the default, so cleanup is on out of the box; set false to retain every run for analysis.
CONFIG_SCHEMA_VERSION = 32


class AuditBranch(StrEnum):
    """Which branch the audit trail is committed onto."""

    TASK = "task"
    SIBLING = "sibling"


class BranchMode(StrEnum):
    """Where a task's git operations point.

    ``new`` (the default) creates a fresh task branch from ``repo.base_branch`` — today's behavior.
    ``existing`` works in a named, already-existing branch (``branch_ref``). ``current`` works in
    whatever branch the working tree is on, without creating, switching, or requiring a clean tree.
    A branch is **orchestrator-owned only in ``new``** — the sole mode where destructive git ops
    (branch delete, remote delete, reset-to-base, force-checkout-away) may run.
    """

    NEW = "new"
    EXISTING = "existing"
    CURRENT = "current"


class PublishScope(StrEnum):
    """Per-task, downgrade-only cap on how far the ``publish`` node goes.

    A *cap*, never an escalation: the effective scope is ``min(flow_policy, task.publish)`` over the
    ranking ``commit < push < pull_request``. ``commit`` stops after the code/audit commits,
    ``push`` stops before the PR, ``pull_request`` is the full sequence. Unset defers to the flow's
    policy. On a flow whose graph has no PR-publishing node it is a no-op (cannot manufacture one).
    """

    COMMIT = "commit"
    PUSH = "push"
    PULL_REQUEST = "pull_request"


@dataclass(frozen=True)
class AutoModeConfig:
    enabled: bool
    # When true, `watch` sends a Telegram approve/deny prompt before claiming each pending task
    # Deny / timeout / no transport stops chaining for that cycle (fail-closed); the task
    # stays pending. Requires `telegram.enabled` (preflight). Gates new claims only — resuming an
    # in-flight task on daemon restart is never gated.
    confirm_next_task: bool = False


@dataclass(frozen=True)
class OrchestratorRuntimeConfig:
    auto_mode: AutoModeConfig
    # Seconds between `watch` ticks; each tick fetch/pulls base_branch to discover tasks pushed to
    # git, then processes pending. 0 = single-pass (no loop, no periodic sync).
    poll_interval_seconds: int
    # This instance's queue selector. Watch only picks a pending task when `task.queue` equals this
    # value — plain string equality, static partitioning across multiple worc instances sharing one
    # git-distributed pool. Non-empty; defaults to "default" (same as an untagged task), so a single
    # untagged instance behaves exactly as before. Overridable per launch with `worc watch --queue`.
    queue: str = "default"


@dataclass(frozen=True)
class RepoConfig:
    url: str
    local_path: str
    base_branch: str
    branch_prefix: str
    # Instance default for where task git operations point. A per-task
    # ``branch_mode`` overrides it. Defaults to ``new`` (create a fresh task branch from
    # ``base_branch``), so an absent key reproduces today's behavior exactly.
    branch_mode: BranchMode = BranchMode.NEW
    # Whether terminal cleanup returns the working tree to ``base_branch`` after a terminal outcome
    # ``None`` (default) defers to the branch mode: ``new`` returns to base,
    # ``existing`` and ``current`` stay on the branch. ``False`` never returns (a global off switch,
    # including ``new``); ``True`` forces ``new`` and ``existing`` to return. ``current`` always
    # stays put regardless, since the operator owns its (possibly dirty) tree.
    checkout_base_on_cleanup: bool | None = None


@dataclass(frozen=True)
class PathsConfig:
    # Repo-relative directory holding the task lifecycle (preparing/pending/done/failed). The
    # default "tasks" reproduces the historical layout; an operator may rename it to avoid a clash
    # with a repo that already uses `tasks/`. `preparing/` is the staging area the watch scanner
    # never reads (compose there, then `promote` into `pending/`). Validated repo-relative — never
    # absolute, no `..`, and never under the gitignored `.worc/` home (that would silently break the
    # git audit trail). The lifecycle subfolder names themselves are not configurable.
    tasks_dir: str = "tasks"


@dataclass(frozen=True)
class DecompositionConfig:
    enabled: bool
    max_subtasks: int


@dataclass(frozen=True)
class RetryConfig:
    """Bounded same-provider transient-retry + soft-pause policy (transient provider recovery).

    ``max_attempts`` is the number of retries *after* the first failed attempt (so ``2`` ⇒ up to 3
    invocations of one provider) and applies independently to *each* provider in the route's
    ``[primary, fallback]`` sequence; it is counted **separately** from ``max_stage_attempts`` (a
    stage hop). Only the ``TRANSIENT_RETRYABLE`` classes (PROVIDER_UNAVAILABLE / NETWORK_UNAVAIL)
    are retried. Backoff is deterministic exponential ``min(base_delay_s * 2**k, max_delay_s)`` with
    no jitter — a single-slot orchestrator has no thundering-herd. ``max_blocked_s`` is the B-lite
    ceiling: once every provider is exhausted a task parks as resumable (not terminal) and is only
    failed if it stays parked longer than this (total parked wall-clock). It bounds BOTH a transient
    outage and a subscription/session rate-limit park — the default (6h) comfortably outlasts a
    provider's ~5h usage window so a rate-limited task waits out the reset and resumes."""

    max_attempts: int = 2
    base_delay_s: float = 2.0
    max_delay_s: float = 30.0
    max_blocked_s: float = 21600.0


@dataclass(frozen=True)
class ProviderConfig:
    command: str
    model: str
    timeout_seconds: int
    permission_profile: str
    extra_args: tuple[str, ...] = ()
    # Codex escape: the sole remaining value is ``danger-full-access`` — the operator's explicit,
    # loudly-unisolated opt-out, gated by ``strict_isolation: false``. The access level
    # (``read-only`` | ``workspace-write``) now lives in the provider-neutral ``permission_profile``
    # above; a legacy ``sandbox: read-only|workspace-write`` is rejected (migrate via
    # ``upgrade-config``). Inert on Claude.
    sandbox: str | None = None
    # Claude turn cap: positive int, or ``None`` = no cap. The loader maps ``"none"``/``"max"``/
    # ``null`` to ``None`` (adapter omits ``--max-turns``); config default 400.
    max_turns: int | None = None
    reasoning: str | None = None  # provider-specific: "minimal" | "low" | "medium" | "high" | ...
    # Exactly one configured provider must set ``primary: true`` — the global primary that runs any
    # flow node with no ``provider`` field, and the single infrastructure-fallback target (PRE.1).
    primary: bool = False
    # Claude-only: when true, a run that exhausts ``max_turns`` (``error_max_turns``)
    # pauses for a durable Telegram continue/stop prompt instead of failing immediately; continue
    # resumes the same agent session with a fresh turn grant. Requires ``telegram.enabled``
    # (preflight). With this on, a low ``max_turns`` (~50–100) is safe — extendable on demand.
    max_turns_gate: bool = False
    # Claude-only opt-in: when true, the adapter DROPS the
    # native-memory deny so Claude Code's own auto-memory (``<config_dir>/projects/<repo>/memory/``)
    # persists across tasks on this repo. Default false keeps the deny in place. RISK: that store is
    # outside the orchestrator's redaction net and audit (an unredacted ``originSessionId`` was once
    # observed leaking there) — a deliberate, operator-owned risk acceptance. Inert on Codex.
    allow_native_memory: bool = False


@dataclass(frozen=True)
class AgentsConfig:
    allowed: tuple[ProviderId, ...]
    max_stage_attempts: int
    max_fix_cycles: int
    max_total_fix_iterations: int
    decomposition: DecompositionConfig
    providers: dict[ProviderId, ProviderConfig]
    # Optional; a default keeps every existing positional/`replace` construction valid. Last field.
    retry: RetryConfig = field(default_factory=RetryConfig)


# Approval-policy levels for the dangerous-diff gate (``security.trust_level`` / per-task override):
# ``strict`` gates any deletion/dependency diff; ``auto`` gates only a ``protected_paths`` match.
# The canonical allowlist — reused by the config loader and the task validation gate (config v25).
TRUST_LEVELS: frozenset[str] = frozenset({"strict", "auto"})


@dataclass(frozen=True)
class SecurityConfig:
    strict_isolation: bool
    allowed_environment: tuple[str, ...]
    denied_read_paths: tuple[str, ...]
    denied_commands: tuple[str, ...]
    # Approval policy for the mid-task dangerous-diff gate. ``strict`` gates any
    # deletion/rename or dependency-manifest edit; ``auto`` turns the diff-shape gate off so only a
    # ``protected_paths`` match raises approval. The dataclass default is the safe fallback
    # ``strict``; a fresh install writes ``auto`` (config_writer).
    trust_level: str = "strict"
    # Operator allowlist (repo-relative globs) of paths that ALWAYS require approval on any change,
    # regardless of ``trust_level`` — the always-ask floor no level can lower. Empty = no floor.
    protected_paths: tuple[str, ...] = ()
    # Operator escape hatch: fully disable READ-isolation for provider runs. When on it
    # restores the provider's native project-instruction/config discovery (Claude re-loads
    # ``CLAUDE.md`` + project settings/hooks/MCP/skills via ``--setting-sources project``; Codex
    # re-reads the user ``config.toml`` and the project ``.codex`` config/hooks/rules) and lifts the
    # private :class:`~wastech_orchestrator.runtime_layout.InternalDenyPolicy` read-deny projection
    # (``.worc``/env-file/provider homes/frozen bundles), at the cost of that isolation. The WRITE
    # side stays: exchange/Git/``tasks/``/instruction write-deny, the commit/staging gates, and the
    # PR control layer. The public ``denied_read_paths`` blacklist also stays enforced. Operator-
    # config ONLY (never a task / ``extra_args`` / flow-node key). Defaults to ``True`` — read-
    # isolation is OFF out of the box: a deliberate deployment-posture choice that departs from the
    # project's own default-safe rule for isolation. Set it ``False`` to keep
    # read-isolation on. ``strict_isolation`` is still the master switch and always wins toward
    # relaxation (see :attr:`read_isolation_off`).
    disable_read_isolation: bool = True
    # Operator master switch for the read-only git-evidence grant. A flow node may declare
    # ``git_evidence: true`` to ask for the read-only git verbs (``log``/``show``/``diff``/… — every
    # one of them reports, none mutates or publishes) so an audit node can cite a commit instead of
    # substituting a changelog grep for delivery history. The declaration alone grants nothing: with
    # this switch off — the default — a declaring flow loads, validates and runs exactly as it does
    # today. That split is what keeps the envelope un-weakenable through a flow: the capability is
    # reachable declaratively, but only the operator can turn it on. Operator-config ONLY (never a
    # task / ``extra_args`` / flow-node key). Enabling it does not make the node writable: Claude
    # confines the shell to those verbs and write-denies the whole clone in its OS sandbox, Codex's
    # read-only sandbox already forbids every mutation, and ``denied_commands`` stays the floor.
    allow_git_evidence: bool = False

    @property
    def read_isolation_off(self) -> bool:
        """Effective read-isolation state for a provider run — the ONE place the formula
        lives, so no adapter recomputes it. Read-isolation is off when the operator explicitly
        disabled it OR strict isolation is off entirely: ``strict_isolation: false`` relaxes
        everything and overrides even an explicit ``disable_read_isolation: false``. Effective
        value: ``disable_read_isolation OR NOT strict_isolation``."""
        return self.disable_read_isolation or not self.strict_isolation


@dataclass(frozen=True)
class ValidationConfig:
    max_task_bytes: int
    max_task_lines: int
    max_line_bytes: int
    max_control_ratio: float
    required_fields: tuple[str, ...]
    reject_unknown_fields: bool
    quarantine_folder: str


@dataclass(frozen=True)
class CheckCommandSpec:
    """One structured check command: a logical ``name``, an explicit argv list (no shell), and an
    optional repo-relative working directory.

    Normalizes to ``checks.model.ResolvedCheck`` at consumption time (the loader stays shapes-only
    and does no ``shlex`` splitting). ``cwd`` (``None`` => the clone root) lets a monorepo set run a
    command inside a subproject; it is validated against path traversal by the config validator.
    """

    argv: tuple[str, ...]
    name: str | None = None
    cwd: str | None = None


@dataclass(frozen=True)
class CommandSet:
    """A named group of check commands for one project in a (poly)repo.

    ``paths`` are repo-relative globs; the runner runs this set when the task diff touches a
    matching path (empty ``paths`` => the set always runs on any non-empty diff).
    ``timeout_seconds`` (``None`` => the global ``checks.timeout_seconds``) overrides the
    per-command timeout for this set. ``skip_if_unavailable`` (default ``False`` = fail-closed) lets
    the set be skipped — loudly, never "passed" — when its toolchain binary is absent. The set name
    is the mapping key in ``checks.command_sets`` (not a field), mirroring ``agents.providers``.
    """

    commands: tuple[CheckCommandSpec, ...]
    paths: tuple[str, ...] = ()
    timeout_seconds: int | None = None
    skip_if_unavailable: bool = False


@dataclass(frozen=True)
class ChecksConfig:
    # Named per-project command sets (monorepo). Empty mapping = no gate (every task passes the
    # checks node — the former empty-`configured` semantics). Keyed by set name, loader-insertion
    # order. The flow never supplies commands (security ceiling); the operator authors this block.
    command_sets: dict[str, CommandSet] = field(default_factory=dict)
    # Global per-command timeout for the Check Runner (the process runner requires a timeout; each
    # command is launched as an argv list, no shell). A set's ``timeout_seconds`` overrides it.
    timeout_seconds: int = 7200


@dataclass(frozen=True)
class FootprintConfig:
    """The audit-trail policy. The orchestrator's runtime files always live under the gitignored
    ``<repo>/.worc/`` home; only the task file and its ``<id>.summary.md`` are committed."""

    audit_commit_message: str
    audit_on_branch: AuditBranch


class MergeStrategy(StrEnum):
    """The ``gh pr merge`` strategy used when ``git.auto_merge`` fires (auto-merge bypass)."""

    MERGE = "merge"
    SQUASH = "squash"
    REBASE = "rebase"


@dataclass(frozen=True)
class GitConfig:
    create_pull_request: bool
    pr_base: str
    footprint: FootprintConfig
    # --- auto-merge bypass (DANGER: skips the human review gate; all default to the safe value) ---
    # When true, every successfully published PR is merged to ``pr_base`` automatically.
    # The mid-pipeline dangerous-diff approval still fires (auto_merge only affects publishing).
    auto_merge: bool = False
    # Strategy passed to ``gh pr merge`` when a merge fires.
    auto_merge_strategy: MergeStrategy = MergeStrategy.SQUASH
    # False: merge immediately (`gh pr merge`). True: arm GitHub-native auto-merge (`--auto`), which
    # merges only after required status checks pass.
    auto_merge_wait_for_checks: bool = False
    # Name of the flow the operator-driven ``merge-task`` runs to resolve base-merge conflicts
    # (``<merge_flow>.yaml`` under ``.worc/flows/`` or packaged). Only invoked when pulling ``base``
    # into the task branch conflicts; a clean merge is mechanical (no flow, no agent). The seam is a
    # single name today; a future path/area-based collection can replace it.
    merge_flow: str = "merge"


@dataclass(frozen=True)
class TelegramConfig:
    """Optional Telegram human-in-the-loop and terminal notifications."""

    enabled: bool
    bot_token_env: str
    chat_id_env: str
    ask_timeout_s: int
    trace: bool = False


@dataclass(frozen=True)
class SkillsConfig:
    """Repo skill selection (skills-selection-rework): automatic discovery + two attachment layers.

    The orchestrator discovers every tracked ``SKILL.md`` in the clone (``git ls-files``, whole-repo
    and ignore-aware), then attaches skills to each flow node from two layers the Core merges
    deterministically: operator ``skills:`` pins on the flow node (static) and — when ``dynamic`` —
    a once-per-task supervisor proposal of a ``node → skills`` map (skipped when the inventory is
    empty). ``strict`` governs only operator pins: an unresolved pin (typo, removed skill, ambiguous
    bare name, missing path) is a warning that is skipped (``False``, fail-open) or stops the task
    in ``manual_action_required`` (``True``). A dynamic proposal naming a missing skill is always
    just filtered, never an error.
    """

    # Off by default: the once-per-task supervisor proposal is opt-in, so an absent ``skills``
    # block (and a fresh ``worc install``) does not pay for a dynamic layer the repo may not need —
    # fail-quiet, symmetric to how ``worc install`` now writes ``dynamic: false`` explicitly.
    dynamic: bool = False
    strict: bool = False


@dataclass(frozen=True)
class SupervisorConfig:
    """The constant supervisor layer — oversight ABOVE any flow, not a node.

    It exists for every task under any flow shape: it observes each completed step read-only through
    its own ``resume_own_lineage`` session (~1 LLM call/step) and synthesizes the summary + advisory
    caveats at whole-task close. Trusted at the ``config.yaml`` level and validated under the same
    ceiling as flow nodes: ``permission_profile`` is forced ``read-only`` in code, ``reasoning`` ∈
    the allowlist (loader), and ``role_file`` is path-contained (validator). The own session is
    in-memory; a durable ``resume_own_lineage`` session is a node-level scope. ``model``/
    ``reasoning`` empty → the
    provider default. ``provider`` empty → the global primary; set it (validated in
    ``agents.allowed``) to pin the layer to a provider — e.g. keep the supervisor on claude while
    the primary is codex, so its ``model`` reaches a provider that accepts it.
    """

    role_file: str = "roles/supervisor.md"
    model: str | None = None
    reasoning: str | None = None
    provider: ProviderId | None = None


@dataclass(frozen=True)
class LoggingConfig:
    """Operator log verbosity + on-disk artifact retention (log-management).

    ``level`` (``debug|info|warning|error``) persists the structured operator trace verbosity; the
    ``--log-level`` CLI flag overrides it when given. ``artifacts`` (``minimal|standard|full``)
    governs which per-attempt provider files survive under
    ``logs/<task-id>/stages/.../<attempt>-<provider>/``: ``minimal`` keeps only ``result.json``
    (even on failure — ``result.json`` records the exit code + error class), ``standard`` adds
    ``stdout.log``/``stderr.log``, ``full`` keeps everything. Prompt-audit is independent (governed
    by ``prompt_audit``); ``rendered-prompt.md`` and task-level artifacts are out of scope.

    ``clean_runs_on_success`` governs the per-task ``runs/`` roots (frozen control/instruction
    bundles and sealed terminal exchanges): on by default, so a task that finishes **successfully**
    evicts its own subtree and the ordinary operator never has to learn those directories exist.
    Turn it off to keep every run's frozen inputs and seals for analysis — ``worc runs clean`` then
    reclaims them on demand, and is available either way. A task that failed, parked, or needs
    manual action is never cleaned automatically, nor is quarantined exchange evidence; per-task log
    dirs are not in scope here (they belong to ``worc logs clean``).
    """

    level: str = "info"
    artifacts: str = "standard"
    clean_runs_on_success: bool = True


@dataclass(frozen=True)
class MemoryConfig:
    """Repo-scoped persistent memory: global toggle + bounded knobs.

    Absent block => ``enabled=False`` — the pre-memory behavior (no store, no candidate delta, empty
    memory packets, ``worc memory`` is a no-op, no background cleanup). This dataclass default
    stays ``False`` as the safe fallback, but a fresh ``worc install`` ships ``enabled: true`` (both
    the packaged ``config.example.yaml`` and the generated ``config.yaml`` via
    ``config_writer.build_config_mapping``), so memory is on out of the box. Every numeric knob
    carries a locked default and is a bounded, runtime-clamped value — none is a
    fatal config error (an odd value is clamped at use, per the "fatal only without a safe fallback"
    rule). The write / read / curation paths consume these knobs at runtime (all phases shipped).
    """

    enabled: bool = False
    # Short-term episodic TTL in days (the intended window is 14–45d). Long-term has no TTL.
    short_term_ttl_days: int = 30
    # Per-node retrieval packet caps — deliberately small (precision over recall); the
    # PacketBuilder enforces them.
    packet_max_lines: int = 120
    packet_max_long_term: int = 3
    packet_max_entity: int = 5
    # Inert: the episodic tier is write-only (never injected into a packet),
    # so this cap is no longer read; kept as the absent-block default to avoid a schema churn for a
    # dead knob (mirrors ``cleanup_promotions_per_pass``).
    packet_max_episodic: int = 3
    # Promotion-to-long-term thresholds. The recurrence gate
    # applies only to ``artifact-backed`` lessons — repo-verified / human-curated / review-verified
    # lessons promote on first sight. A gated lesson clears it when it recurred in
    # >= ``promote_min_tasks`` tasks within ``promote_window_days``.
    promote_min_tasks: int = 2
    promote_window_days: int = 60
    # Background-cleanup budget — bounded autonomy; the CleanupJob honors it.
    cleanup_min_interval_s: int = 300
    cleanup_max_scanned: int = 200
    cleanup_max_edits: int = 50
    cleanup_max_wall_clock_s: float = 5.0
    # Documentation-only invariant (not read at runtime): the never-promote guarantee is
    # structural — `CleanupJob` only demotes / expires / quarantines / merges and has no promote
    # code path, so this stays 0. The knob states the invariant in config; a non-zero value
    # is inert (cleanup still never creates a long-term lesson).
    cleanup_promotions_per_pass: int = 0


# The built-in fallback timeout for a tool node when neither the node's ``timeout_seconds`` nor
# ``tools.default_timeout_seconds`` is set. One hour — long enough for a heavy operator scan, short
# enough to bound a hung binary. The ``ToolsConfig`` default equals this, so an absent ``tools``
# block resolves to the same 3600s.
DEFAULT_TOOL_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True)
class ToolsConfig:
    """Custom tool-node settings: the flow-wide default timeout for ``tool`` nodes.

    Absent block => ``default_timeout_seconds=3600`` (1h). Resolution precedence for a tool node is
    ``node.timeout_seconds`` → ``tools.default_timeout_seconds`` → the built-in
    :data:`DEFAULT_TOOL_TIMEOUT_SECONDS`. The value is a bounded, runtime-resolved default — never a
    fatal config error (an odd value has a safe fallback), consistent with the flow-engine rule.
    """

    default_timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS


@dataclass(frozen=True)
class OrchestratorConfig:
    orchestrator: OrchestratorRuntimeConfig
    repo: RepoConfig
    agents: AgentsConfig
    security: SecurityConfig
    validation: ValidationConfig
    checks: ChecksConfig
    git: GitConfig
    telegram: TelegramConfig
    skills: SkillsConfig = SkillsConfig()
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    # When true, every task records each step's prompt + who-metadata (provider/model/attempt/
    # fallback/status) under `logs/<task-id>/prompt-audit/`. A per-task `prompt_audit` always
    # overrides this (task wins); recording a prompt is not a privilege escalation, so there is no
    # operator gate.
    prompt_audit: bool = False

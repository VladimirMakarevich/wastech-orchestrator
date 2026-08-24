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

# The config.yaml format version, bumped only when the *format* changes — a key added, renamed,
# removed, or given a new shape — never on an ordinary release. The loader refuses a config whose
# ``schema_version`` is newer than this and names the fix (upgrade the orchestrator); an absent or
# lower value loads, since every removed key is either tolerated-and-ignored or reported by name.
# There is no migration runner: an installation that has drifted is repaired by ``upgrade-config``,
# which merges the packaged template over the operator's file and stamps this value.
CONFIG_SCHEMA_VERSION = 39


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


class ObserveMode(StrEnum):
    """How often the supervisor layer spends an LLM turn observing a completed step.

    Ranked by how many calls the mode can produce: ``none`` (0) < ``events`` (only deviations) <
    ``selected`` (the listed nodes) < ``all`` (every observable step). A flow may *narrow* the
    global mode but never widen it; the rank table and that comparison live in
    ``core.observe_cadence``. The whole-task ``finalize`` turn is unaffected by every mode — it is
    seeded by the deterministic ``SupervisorPacket``, not by the observations.
    """

    ALL = "all"
    SELECTED = "selected"
    EVENTS = "events"
    NONE = "none"


#: The closed set of ``observe.mode: events`` triggers. ``rework`` covers an evaluator's rework and
#: its give-up accept (``rework_exhausted``); ``failure`` and ``fallback`` are read from the step's
#: own ``node_runs`` row. Closed on purpose: a new trigger arrives with the facts it needs.
OBSERVE_TRIGGERS: frozenset[str] = frozenset({"rework", "failure", "fallback"})


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
    # The repository this instance publishes to. Beyond cloning it, this is what every ``gh`` call
    # is pinned to with ``--repo``: parsed once into ``[HOST/]OWNER/REPO``, so a planted gh config
    # or an ``insteadOf`` rewrite cannot retarget a pull request. A URL that names no hosted
    # repository (an ssh alias like ``git@ghwork:o/n.git``, a ``file://`` URL, a local path) yields
    # no pin —
    # ``gh`` then infers the repository from the clone, the pull-request reuse probe included.
    # ``worc preflight`` prints that verdict as its own ``gh-repo-pin`` line: fatal when this
    # configuration opens pull requests, a warning otherwise. Prefer the
    # ``https://host/owner/name`` form when the ssh transport is not required.
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
    # Claude turn cap: positive int, or ``None`` = no cap. The loader maps ``"none"``/``"max"``/
    # ``null`` to ``None`` (adapter omits ``--max-turns``); config default 400.
    max_turns: int | None = None
    reasoning: str | None = None  # provider-specific: "minimal" | "low" | "medium" | "high" | ...
    # Exactly one configured provider must set ``primary: true`` — the global primary that runs any
    # flow node with no ``provider`` field, and the single infrastructure-fallback target.
    primary: bool = False
    # Claude-only: when true, a run that exhausts ``max_turns`` (``error_max_turns``)
    # pauses for a durable Telegram continue/stop prompt instead of failing immediately; continue
    # resumes the same agent session with a fresh turn grant. Requires ``telegram.enabled``
    # (preflight). With this on, a low ``max_turns`` (~50–100) is safe — extendable on demand.
    max_turns_gate: bool = False


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
    # The master posture switch, and the only one. ``true`` (the default, and what a fresh install
    # writes) is fail-closed: a provider whose configured isolation cannot be enabled fails
    # preflight.
    #
    # ``false`` is the operator's **advanced mode** — deliberately not a second key, so there is one
    # door rather than a matrix. It means "full freedom for the agent under the operator's
    # responsibility, except the floor": read-isolation is forced off (see
    # :attr:`read_isolation_off`), and the parent environment is forwarded WHOLE to every process
    # run on the agent's behalf — agent CLIs, the check commands, the scanners, the tool nodes — so
    # ``allowed_environment`` is not consulted for them at all. Two things it does not relax:
    # ``extra_environment`` still assigns on top, and the names the orchestrator's own env-file
    # defines are still withheld (the agent is denied reading that file, so forwarding its contents
    # would route around the deny). The orchestrator's own ``git``/``gh`` keep the allowlist —
    # advanced mode widens what the agent may do, and those processes are not the agent.
    #
    # The other three axes, all of them present-tense: no tool allowlist reaches the agent CLI, so
    # every built-in tool exists and EVERY node has a shell, read-only ones included; the agent may
    # WRITE anywhere the sandbox reaches rather than only inside the clone, which includes a
    # toolchain cache under ``$HOME``, the system temp, and equally a directory on ``PATH`` — that
    # last one being the right to replace an executable which later runs OUTSIDE the sandbox; and
    # every node is ONLINE whatever its flow granted, across all three network surfaces (the
    # sandboxed shell, the built-in web tools that do not pass through it, and Codex's backend-side
    # ``web_search``), with no domain filtering.
    #
    # What it does NOT unlock: the provider full-access modes (Codex ``--sandbox danger-full-
    # access``, Claude ``--permission-mode bypassPermissions``) are refused at every value of this
    # key, as are the absolutely-forbidden flags. It is also not a way to skip a proof: the config-
    # legality check and the provider capability probes run at either setting, because the generated
    # permission profile is what the local floor rests on and that matters most here.
    #
    # The floor that survives, in four honest levels. ``worc preflight`` and the run log announce
    # the mode in one line and point at ``guide/config/security.md``, which carries these four for
    # the operator; this comment is the same answer for whoever reads the code.
    # (1) The integrity of the task's own state is held MECHANICALLY: the clone's ``.git`` and
    # private ``.worc`` stay unwritable wherever it can sandbox
    # at all. One qualifier, since the write grant above is volume-wide — what keeps those paths
    # out of it is the carve-out being the more specific rule, which Codex re-proves under its own
    # sandbox before every provider attempt that gets a shell (agent node, evaluator, supervisor
    # turn) and Claude does not, so there this level rests on the tool-level write denies. Nesting a
    # deny inside an allow is a construction Claude's own settings compiler supports (it carries the
    # carve-outs as ``denyWithinAllow``) — read out of the pinned binary, not proven on this host,
    # and ``worc preflight --paid-isolation-probe`` is the one instrument that can answer it here.
    # (2) Publication to this repository's origin is held by DETECTION: a branch or PR
    # appearing without the orchestrator's record parks the task. (3) Publication anywhere else IS
    # HELD BY NOTHING, and is reachable today: the agent has the network, credentials are picked up
    # automatically, and nothing is planned to hold it. (4) Publication AS the orchestrator is held
    # by DETECTION: user git config and the clone's agent-CLI config are fingerprinted, every ``gh``
    # call names its repository, and the launched executables are pinned to the paths resolved at
    # startup (a substitution between runs, and an edit to the installed package's own code, are not
    # covered).
    #
    # Operator-config ONLY — never a task, a flow node, or ``extra_args``. The redaction net widens
    # to compensate: with the name gate gone, secret-named values are scrubbed from logs and
    # artifacts by name alone, so a secret-named variable holding something harmless may print as
    # ``[REDACTED]``.
    strict_isolation: bool
    #: Names forwarded from the parent environment. An entry is an exact name or a prefix
    #: pattern (``DOTNET_*``), resolved by
    #: :func:`~wastech_orchestrator.security.env.expand_allowed_environment`. The list must cover
    #: ``PATH``; on Windows it must also cover ``SystemRoot`` or the launch-critical preflight/run
    #: checks fail on that host.
    allowed_environment: tuple[str, ...]
    denied_read_paths: tuple[str, ...]
    denied_commands: tuple[str, ...]
    # Approval policy for the mid-task dangerous-diff gate. ``strict`` gates any
    # deletion/rename or dependency-manifest edit; ``auto`` turns the diff-shape gate off so only a
    # ``protected_paths`` match raises approval. ``auto`` everywhere — the dataclass default, the
    # loader's absent-key default, and what a fresh install writes (config_writer) — so "the
    # default" has one answer no matter how a config arrives. ``protected_paths`` stays the
    # always-ask floor under either level.
    #
    # What the gate measures, at either level, is the change since the last commit the
    # **orchestrator** made for this task (the task's own start point until it makes one) — never
    # since ``HEAD``, which a commit made inside the run would empty. So a self-commit by an agent
    # or by a node whose class only warns is still asked about, and a decomposed run is not asked
    # twice about the same approved deletion. It is asked at the writing node, at that node's
    # ``hitl`` round-trip, and once more immediately before the publishing commit — the last of
    # which is the only ask a flow with no writing node ever reaches.
    trust_level: str = "auto"
    # Operator allowlist (repo-relative globs) of paths that ALWAYS require approval on any change,
    # regardless of ``trust_level`` — the always-ask floor no level can lower. Empty = no floor.
    protected_paths: tuple[str, ...] = ()
    # Operator escape hatch: fully disable READ-isolation for provider runs. When on it restores the
    # provider's native project-instruction/config discovery (Claude re-loads ``CLAUDE.md`` +
    # project settings/hooks/MCP/skills via ``--setting-sources project``; Codex re-reads the user
    # ``config.toml`` and the project ``.codex`` config/hooks/rules). It does NOT lift the private
    # :class:`~wastech_orchestrator.runtime_layout.InternalDenyPolicy` read-deny projection:
    # ``.worc``, the resolved env-file and the frozen bundles stay ``Read``-denied either way, since
    # native discovery needs nothing from them while opening them handed the agent the
    # orchestrator's own ``.env``. The provider CLIs' own config homes are not part of any deny
    # projection at any setting. The WRITE side
    # stays throughout: exchange/Git/``tasks/``/instruction write-deny, the commit/staging gates,
    # and the PR control layer. The public ``denied_read_paths`` blacklist also stays enforced.
    # Operator- config ONLY (never a task / ``extra_args`` / flow-node key). Defaults to ``True`` —
    # read- isolation is OFF out of the box: a deliberate deployment-posture choice that departs
    # from the project's own default-safe rule for isolation. Set it ``False`` to keep read-
    # isolation on. ``strict_isolation`` is still the master switch and always wins toward
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
    # confines the shell to those verbs and write-denies the whole clone in its OS sandbox, and
    # Codex's read-only sandbox already forbids every mutation. ``denied_commands`` adds no floor
    # under either: it is friction and telemetry (a refusal that shows up in the log), because
    # prefix matching on a command string is walked around by ``bash -c`` or an absolute path.
    allow_git_evidence: bool = False
    # Environment variables the orchestrator **assigns** to agent/check/tool children and to its
    # own git/gh before the publication-retargeting scrub, as ``name -> value``. That scrub is a
    # whitelist over the ``GIT_*``/``GH_*``/``GITHUB_*`` namespace, so an assignment in it reaches
    # git/gh only for ``GIT_CONFIG_GLOBAL`` and the two token names; assignments outside those
    # prefixes reach it as written. The complement of
    # ``allowed_environment``, which only *forwards* a name and
    # therefore inherits whatever the operator's shell happened to export — unset on another
    # machine, different on the next, and a forgotten ``export`` is silently skipped. Toolchain
    # roots and cache paths (``DOTNET_ROOT``, ``NUGET_PACKAGES``, ``npm_config_cache``) need a
    # pinned value, so this key writes it. No default: an absent key is an empty mapping and the env
    # is byte-for-byte what it is today. Applied AFTER forwarding, so a name in both wins here, and
    # key order is deterministic (allowlist order, then this mapping's config order). Validated
    # fail-closed: ``PATH`` (any case) is refused — reassigning it is how you substitute every
    # binary — as is a secret-looking name and a name outside ``[A-Za-z_][A-Za-z0-9_]*``. The
    # assigned path may not overlap the orchestrator control/private roots, Git metadata, the
    # exchange, task lifecycle files, the env-file, or a ``denied_read_paths``
    # target; config validation handles lexical paths and preflight/run resolve host aliases. The
    # **value** cannot be checked for secrecy and sits in plaintext in ``config.yaml``, so
    # credentials never go here; that is a documented contract, not an enforced one.
    extra_environment: dict[str, str] = field(default_factory=dict)

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
class SupervisorTurnConfig:
    """The model + effort for one supervisor phase (``finalize`` or ``handoff``).

    Both empty → the resolved provider's defaults. Shared by the two phases because they need the
    same two knobs; ``observe`` carries the cadence as well, so it has its own shape below.
    """

    model: str | None = None
    reasoning: str | None = None


@dataclass(frozen=True)
class SupervisorObserveConfig:
    """The per-step observation phase: how often it runs, and with what model + effort.

    ``mode`` is the cadence (see :class:`ObserveMode`); the global default is ``events``, so a flow
    that declares nothing still pays only for deviations. ``triggers`` narrows which deviations
    count under ``events`` (⊆ :data:`OBSERVE_TRIGGERS`); ``include_nodes`` lists the node ids
    observed under ``selected``. The pair is the cheap one — this phase is advisory and can fire on
    every step of a deep fix loop, so keep it at or below the producers' tier.
    """

    mode: ObserveMode = ObserveMode.EVENTS
    triggers: tuple[str, ...] = ("rework", "failure", "fallback")
    include_nodes: tuple[str, ...] = ()
    model: str | None = None
    reasoning: str | None = None


@dataclass(frozen=True)
class SupervisorConfig:
    """The supervisor layer — oversight ABOVE any flow, not a node. On by default, removable.

    It exists for every task under any flow shape: it observes completed steps read-only through its
    own ``resume_own_lineage`` session and synthesizes the summary + advisory caveats at whole-task
    close. Trusted at the ``config.yaml`` level and validated under the same ceiling as flow nodes:
    ``permission_profile`` is forced ``read-only`` in code, every ``reasoning`` ∈ the allowlist
    (loader), and ``role_file`` is path-contained (validator).

    Set ``enabled: false`` to remove the layer entirely; the pull-request body is then rendered
    deterministically from the run's own recorded facts, so no run ships without one.

    ``role_file`` and ``provider`` are one-per-layer and stay top-level: ``role_file`` is the
    observe lens, whose flow-local namesake is ``SupervisorBlock.role_file``, and ``provider`` empty
    → the global primary; set it (validated in ``agents.allowed``) to pin the layer to a provider —
    e.g. keep the supervisor on claude while the primary is codex, so its models reach a provider
    that accepts them.

    Model and effort are **per phase** instead: a cheap ``observe`` note and the whole-task
    ``finalize`` synthesis that writes ``summary.md`` (the pull-request body) have opposite cost
    profiles, and one shared pair could not serve both. ``handoff`` is the subtask brief on a
    decompose flow. The once-per-task skill proposal (``skills.dynamic``) uses the ``observe`` pair
    — same cheap, schema-bound shape — independently of ``observe.mode``, which gates observations
    only.
    """

    # The whole layer, not a phase: false and it is never built, so there are no per-step notes, no
    # whole-task synthesis, no subtask handoff brief and no `skills.dynamic` proposal — and every
    # other key here is then inert (the validator says so in one warning). True by default: the
    # constant oversight the rest of this docstring describes.
    enabled: bool = True
    role_file: str = "roles/supervisor.md"
    provider: ProviderId | None = None
    # Defaulted nested blocks, last: a default keeps every existing positional/`replace`
    # construction valid (same convention as ``AgentsConfig.retry``).
    observe: SupervisorObserveConfig = field(default_factory=SupervisorObserveConfig)
    finalize: SupervisorTurnConfig = field(default_factory=SupervisorTurnConfig)
    handoff: SupervisorTurnConfig = field(default_factory=SupervisorTurnConfig)


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
    memory packets, ``worc memory`` is a no-op, no background cleanup). A fresh ``worc install``
    writes ``enabled: false`` explicitly (both the packaged ``config.example.yaml`` and the
    generated ``config.yaml`` via ``config_writer.build_config_mapping``) so the operator finds the
    switch rather than the block's absence; off is the shipped posture because the store is
    unaudited and carries no redaction guarantee, so keeping it is a conscious opt-in. Every numeric
    knob carries a locked default and is a bounded, runtime-clamped value — none is a fatal config
    error (an odd value is clamped at use, per the "fatal only without a safe fallback" rule).
    """

    enabled: bool = False
    # Short-term episodic TTL in days (the intended window is 14–45d). Long-term has no TTL.
    short_term_ttl_days: int = 30
    # Per-node retrieval packet caps — deliberately small (precision over recall); the
    # PacketBuilder enforces them.
    packet_max_lines: int = 120
    packet_max_long_term: int = 3
    packet_max_entity: int = 5
    # Inert: the episodic tier is write-only (never injected into a packet), so nothing reads this
    # cap — like ``cleanup_promotions_per_pass``.
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

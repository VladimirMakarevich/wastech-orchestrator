"""Canonical runtime directory layout — the one seam that names the orchestrator's roots.

The orchestrator writes to three distinct on-disk surfaces that historically all lived under a
single ``<repo>/.worc`` literal reconstructed independently across the CLI, Core, memory, Git,
output-policy, and process-control paths:

* **control_home** — the discoverable operator control plane (``config.yaml``, ``guide/``,
  ``flows/``, ``tools/``, install metadata). Editable by the operator, resolved by config discovery.
* **private_home** — private runtime state the agent must never read (``state.db``, ``logs/``,
  the ledger, the memory store, security reports, HITL records, rejected runtime tasks, the
  per-task ``runs/`` roots, and the pid/stop/children process-control files) plus the default
  ``.env``.
* **exchange_root** — the agent-facing exchange ``<repo>/.worc-io``. Named here only; the exchange
  builders/publisher still take the root as an argument.

:class:`RuntimeLayout` is **provider-neutral** (paths only) and **immutable**. It is constructed
once at the composition/CLI boundary and injected into consumers, so each consumer declares the
surface it owns instead of rebuilding ``repo_root / ".worc"``. Today ``control_home`` and
``private_home`` resolve to the same ``<repo>/.worc`` directory — the split is a seam, not a move,
so that relocating the private home later touches this module only.

This is a stdlib-only leaf module: it imports nothing from the package, so the CLI, composition
root, Core, Git Manager, memory, and even the ``providers`` leaf can all import it without an
import cycle (verified against ``.importlinter``).

All stored/compared/displayed path strings use :meth:`pathlib.Path.as_posix`; filesystem operations
keep the :class:`~pathlib.Path` values themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The in-repo directory names. ``control_home`` and ``private_home`` share ``.worc`` today; the two
# constants are kept distinct so the private home can move without disturbing the control
# footprint. ``git_manager`` reuses these names for its gitignore/exclude footprint.
CONTROL_HOME_DIRNAME = ".worc"
PRIVATE_HOME_DIRNAME = ".worc"
EXCHANGE_HOME_DIRNAME = ".worc-io"

# The one private-home subdirectory that parents every per-task runtime root below. They all share
# the same defining property — private state keyed by task id, written by one run, never
# agent-readable — so they are grouped rather than scattered as siblings of the operator's own
# ``config.yaml`` / ``flows/`` / ``guide/``, which the operator does browse and edit. Grouping also
# gives the deny set a single named entry and retention a single root to reason about. Not
# ``tasks/`` (that name is taken twice over: the committed lifecycle tree at the repo root and
# ``<private_home>/tasks/rejected/``) and not ``bundles/`` (two of the four roots are not bundles).
RUNS_DIRNAME = "runs"

# The per-task frozen control bundles. Each task's immutable control snapshot is
# ``<runs>/<CONTROL_BUNDLE_DIRNAME>/<task-id>/``. The whole ``runs`` parent is a provider deny
# target (see :class:`InternalDenyPolicy.runs_home`); it lives under ``private_home`` so it moves
# out of tree together with the rest of the private runtime state.
CONTROL_BUNDLE_DIRNAME = "control-bundles"

# The per-task frozen instruction bundles: the canonical (unredacted) task packet, selected skill
# packages, and root repository instruction files, plus the composite manifest. Each task's snapshot
# is ``<runs>/<INSTRUCTION_BUNDLE_DIRNAME>/<task-id>/``. The redacted, agent-readable *injection*
# copies go to the exchange, never here.
INSTRUCTION_BUNDLE_DIRNAME = "instruction-bundles"

# Each task's sealed terminal-exchange snapshots. When a task reaches a terminal status the
# orchestrator seals a verified copy of its active ``.worc-io`` exchange into
# ``<runs>/<EXCHANGE_SEAL_DIRNAME>/<task-id>/seal-<NNNNNN>/`` and removes the in-repo exchange;
# ``rerun --continue`` restores the latest verified snapshot. A seal is written at *every* terminal,
# success included — it is the archive of what the agent last saw, not a record of trouble.
EXCHANGE_SEAL_DIRNAME = "exchange-seals"

# Where a mutation-flagged exchange tree is quarantined as tainted evidence. When the tamper check
# reports an agent-side exchange mutation, the tree is moved to
# ``<runs>/<EXCHANGE_QUARANTINE_DIRNAME>/<task-id>/<NNNNNN>/`` together with the parent-held
# expected and observed manifests; it is never sealed and never restore-eligible. Unlike its three
# siblings this root exists only when something went wrong, so nothing may delete it automatically.
EXCHANGE_QUARANTINE_DIRNAME = "exchange-quarantine"


def runs_root(private_home: str | Path) -> Path:
    """The parent of every per-task runtime root: ``<private_home>/runs/``.

    The single seam for that layer, so the per-task roots keep resolving through one named constant
    instead of each consumer re-joining the parent name.
    """
    return Path(private_home) / RUNS_DIRNAME


@dataclass(frozen=True)
class RuntimeLayout:
    """Immutable, provider-neutral resolution of the orchestrator's on-disk roots for one repo.

    Pure path resolution — no I/O happens on construction. Build it with :meth:`default` at the
    composition/CLI boundary and inject it; consumers read the field for the surface they own.
    """

    repo_root: Path
    control_home: Path
    private_home: Path
    exchange_root: Path

    @property
    def runs_home(self) -> Path:
        """The parent of every per-task runtime root (frozen bundles, seals, quarantine).

        Derived rather than a field so it can never drift from ``private_home``: the per-task state
        follows the private home wherever it goes.
        """
        return runs_root(self.private_home)

    @classmethod
    def default(cls, repo_root: str | Path) -> RuntimeLayout:
        """Build the default layout for ``repo_root``, reproducing today's paths exactly.

        ``control_home`` and ``private_home`` are both ``<repo_root>/.worc`` and ``exchange_root``
        is ``<repo_root>/.worc-io``.
        """
        root = Path(repo_root)
        return cls(
            repo_root=root,
            control_home=root / CONTROL_HOME_DIRNAME,
            private_home=root / PRIVATE_HOME_DIRNAME,
            exchange_root=root / EXCHANGE_HOME_DIRNAME,
        )


@dataclass(frozen=True)
class InternalDenyPolicy:
    """The orchestrator's internal set of paths a provider must never read.

    This is **internal provider policy**, deliberately kept separate from the overloaded public
    ``security.denied_read_paths`` config list (which also drives redaction and skill scanning). It
    names the roots and secret sources the agent must not read: the control home, the private home,
    the resolved default/explicit ``--env-file`` (which may live outside ``private_home``), the
    provider-owned auth/config homes (``~/.claude`` / ``$CLAUDE_CONFIG_DIR``, ``$CODEX_HOME``), and
    the per-task runtime root.

    ``runs_home`` is the parent of every per-task private root: the frozen control snapshot, the
    frozen agent inputs (canonical task packet, skill packages, root repository instruction files),
    the sealed terminal exchanges, and the quarantined evidence. It lives under ``private_home`` and
    so is already covered by that deny transitively; naming it explicitly makes the adapters'
    projection deny it by name (not by coincidence of location) and keeps it denied if
    ``private_home`` is ever relocated. The orchestrator reads these to freeze/inject; the provider
    never reads them (it receives only the redacted exchange copies).

    This type only *represents* these targets; the provider-specific projection and enforcement
    live in the adapters.
    """

    control_home: Path
    private_home: Path
    env_file: Path | None
    provider_homes: tuple[Path, ...]
    runs_home: Path | None = None

    @property
    def denied_paths(self) -> tuple[Path, ...]:
        """The full deny set, ordered + de-duplicated (homes, env-file, provider homes, runs)."""
        ordered: list[Path] = [self.control_home, self.private_home]
        if self.env_file is not None:
            ordered.append(self.env_file)
        ordered.extend(self.provider_homes)
        if self.runs_home is not None:
            ordered.append(self.runs_home)
        return _dedupe(ordered)


@dataclass(frozen=True)
class ProviderWriteGuardPolicy:
    """Absolute roots a provider Write/Edit-denies during an attempt that can reach the clone.

    Provider-neutral (paths only): the adapter renders these into its own tool-deny / OS-sandbox
    ``denyWrite`` syntax; the Core never learns that syntax. Resolved *per attempt* by
    :meth:`~wastech_orchestrator.git_manager.GitManager.resolve_control_paths` — the gitdir and
    common dir are only final after branch preparation and differ for a linked worktree — then
    carried on :attr:`~wastech_orchestrator.providers.base.AgentRunRequest.write_guard`. Keyed on
    reach, not on the profile: an attempt gets this set when it has write tools **or** a shell, so a
    read-only node that can run commands (Codex always, every provider in the advanced mode) and the
    supervisor's own read-only turn carry it too.

    ``exchange_root`` stays *readable* (it is the curated agent-facing surface) but must be
    Write/Edit-denied so the agent cannot mutate the curated projection. ``git_dir`` and
    ``git_common_dir`` are both denied: for a linked worktree they differ, and both the per-worktree
    gitdir **and** the shared common dir must be denied to override the Bash sandbox's built-in
    linked-worktree ``.git`` write allowance. ``hooks_dir`` is the effective repository hooks dir
    (its ``core.hooksPath`` or ``<common-dir>/hooks``). ``tasks_dir`` is the committed ``tasks/``
    lifecycle tree — readable, never writable, so an agent can neither corrupt lifecycle bookkeeping
    nor inject a new task file for the daemon to pick up.

    Repository governance/instruction files (``AGENTS.md``/``AGENTS.override.md``/``CLAUDE.md`` and
    ``.agents/rules/**``) are deliberately **not** in this deny set: editing them is ordinary
    repository work. A run that changes them is reported to the operator as a notice, never
    blocked.
    """

    exchange_root: Path | None
    git_dir: Path
    git_common_dir: Path
    hooks_dir: Path
    tasks_dir: Path

    @property
    def denied_write_paths(self) -> tuple[Path, ...]:
        """The Write/Edit-deny set, ordered + de-duped (a normal clone collapses gitdir==common)."""
        ordered: list[Path] = []
        if self.exchange_root is not None:
            ordered.append(self.exchange_root)
        ordered.extend((self.git_dir, self.git_common_dir, self.hooks_dir, self.tasks_dir))
        return _dedupe(ordered)


def _dedupe(paths: list[Path]) -> tuple[Path, ...]:
    """Order-preserving de-duplication for the deny-path properties."""
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return tuple(unique)

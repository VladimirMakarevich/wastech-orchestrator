"""Canonical runtime directory layout (WRI-004) — the one seam that names the orchestrator's roots.

The orchestrator writes to three distinct on-disk surfaces that historically all lived under a
single ``<repo>/.worc`` literal reconstructed independently across the CLI, Core, memory, Git,
output-policy, and process-control paths:

* **control_home** — the discoverable operator control plane (``config.yaml``, ``guide/``,
  ``flows/``, ``tools/``, install metadata). Editable by the operator, resolved by config discovery.
* **private_home** — private runtime state the agent must never read (``state.db``, ``logs/``,
  the ledger, the memory store, security reports, HITL records, rejected runtime tasks, and the
  pid/stop/children process-control files) plus the default ``.env``.
* **exchange_root** — the agent-facing exchange ``<repo>/.worc-io`` (WRI-001). Reserved here; this
  module only names it, the exchange builders/publisher still take the root as an argument.

:class:`RuntimeLayout` is **provider-neutral** (paths only) and **immutable**. It is constructed
once at the composition/CLI boundary and injected into consumers, so each consumer declares the
surface it owns instead of rebuilding ``repo_root / ".worc"``. Today ``control_home`` and
``private_home`` resolve to the same ``<repo>/.worc`` directory — the split is a seam, not a move;
WRI-005 later relocates only ``private_home``.

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
# constants are kept distinct so WRI-005 can move the private home without disturbing the control
# footprint. ``git_manager`` reuses these names for its gitignore/exclude footprint.
CONTROL_HOME_DIRNAME = ".worc"
PRIVATE_HOME_DIRNAME = ".worc"
EXCHANGE_HOME_DIRNAME = ".worc-io"

# The private-home subdirectory that holds the per-task frozen control bundles (WRI-010). Each
# task's immutable control snapshot is ``<private_home>/<CONTROL_BUNDLE_DIRNAME>/<task-id>/``. The
# whole root is a provider deny target (see :class:`InternalDenyPolicy.frozen_control_bundle`); it
# lives under ``private_home`` so WRI-005 relocates it out of tree together with the rest of the
# private runtime state.
CONTROL_BUNDLE_DIRNAME = "control-bundles"

# The private-home subdirectory that holds the per-task frozen instruction bundles (WRI-011): the
# canonical (unredacted) task packet, selected skill packages, and root repository instruction
# files, plus the composite manifest. Each task's snapshot is
# ``<private_home>/<INSTRUCTION_BUNDLE_DIRNAME>/<task-id>/``. Like the control bundle it is a
# provider deny target (see :class:`InternalDenyPolicy.frozen_instruction_bundle`); the redacted,
# agent-readable *injection* copies go to the exchange, never here.
INSTRUCTION_BUNDLE_DIRNAME = "instruction-bundles"


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

    @classmethod
    def default(cls, repo_root: str | Path) -> RuntimeLayout:
        """Build the default layout for ``repo_root``, reproducing today's paths exactly.

        ``control_home`` and ``private_home`` are both ``<repo_root>/.worc`` and ``exchange_root``
        is ``<repo_root>/.worc-io`` — path-for-path identical to the pre-WRI-004 code.
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
    """The orchestrator's internal set of paths a provider must never read (WRI-004 groundwork).

    This is **internal provider policy**, deliberately kept separate from the overloaded public
    ``security.denied_read_paths`` config list (which also drives redaction and skill scanning). It
    names the roots and secret sources the agent must not read: the control home, the private home,
    the resolved default/explicit ``--env-file`` (which may live outside ``private_home``), the
    provider-owned auth/config homes (``~/.claude`` / ``$CLAUDE_CONFIG_DIR``, ``$CODEX_HOME``), and
    the per-task frozen control bundle root (WRI-010).

    ``frozen_control_bundle`` is the root under which WRI-010 writes each task's immutable control
    snapshot (``<private_home>/control-bundles/``); ``frozen_instruction_bundle`` is the WRI-011
    root for each task's frozen agent inputs (``<private_home>/instruction-bundles/`` — the
    canonical task packet, skill packages, and root repository instruction files). Both live under
    ``private_home`` and so are already covered by that deny transitively; naming them explicitly
    makes the WRI-002/003 projection deny them by name (not by coincidence of location) and keeps
    them denied after WRI-005 relocates ``private_home``. The orchestrator reads the bundles to
    freeze/inject; the provider never reads them (it receives only the redacted exchange copies).

    WRI-004 only *represents* these targets; the provider-specific projection/enforcement lands in
    WRI-002/003. Because it has no enforcement consumer yet, the policy is exercised by tests and
    threaded through composition unread.
    """

    control_home: Path
    private_home: Path
    env_file: Path | None
    provider_homes: tuple[Path, ...]
    frozen_control_bundle: Path | None = None
    frozen_instruction_bundle: Path | None = None

    @property
    def denied_paths(self) -> tuple[Path, ...]:
        """The full deny set, ordered + de-duplicated (homes, env-file, provider homes, bundles)."""
        ordered: list[Path] = [self.control_home, self.private_home]
        if self.env_file is not None:
            ordered.append(self.env_file)
        ordered.extend(self.provider_homes)
        if self.frozen_control_bundle is not None:
            ordered.append(self.frozen_control_bundle)
        if self.frozen_instruction_bundle is not None:
            ordered.append(self.frozen_instruction_bundle)
        seen: set[Path] = set()
        unique: list[Path] = []
        for path in ordered:
            if path not in seen:
                seen.add(path)
                unique.append(path)
        return tuple(unique)

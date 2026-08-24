"""Absolute paths for the executables the orchestrator launches as itself.

Every one of these is launched by bare name today, so what actually runs is whatever ``PATH``
resolves at that instant. That is fine while nothing can write to a ``PATH`` directory, and it stops
being fine the moment the agent can — which is what the operator's advanced mode heads toward. Four
of the names are load-bearing in a way that makes a substitute worth more than the repository it
touches:

* ``git`` / ``gh`` — the orchestrator's own publication path. A stand-in commits and pushes wherever
  it likes while reporting whatever the caller expects to read.
* ``ps`` — the process-quiescence barrier's only witness. A stand-in printing an empty table makes
  every attempt look like it left nothing running.
* ``bwrap`` — how the Claude host-capability probe decides a sandbox can exist here. A stand-in
  makes the orchestrator claim a floor that is not there. Probe-only: we never execute it, the
  agent CLI does, so pinning it buys visibility rather than control.

Plus the agent CLIs themselves (an operator-configured ``command``) and the ``worc`` console script
the ``watch`` daemon re-invokes to spawn itself.

What pinning does and does not buy. It **does** remove the window inside a run: a component resolves
once when it is built and uses that path for every later call, so a shim planted while the agent
works does not change what the orchestrator runs, and :meth:`PinnedLaunchers.drift` states outright
when the resolution would have changed. It does **not** cover a swap made between runs (each run
resolves fresh, so a shim already in place is simply what gets pinned) and it does **not** cover an
edit to the installed
package's own code — pinning the launcher answers "which ``worc``", never "whose
``git_manager.py``". Both limits belong in the operator guide next to the floor they qualify, not in
a comment only we read.

Provider-neutral: this module resolves names on ``PATH``. ``which`` is injected everywhere so both
host classes are testable on either.
"""

from __future__ import annotations

import platform
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from wastech_orchestrator.config.schema import OrchestratorConfig

#: ``shutil.which``'s shape, injected so a test can describe a host it is not running on.
Which = Callable[[str], str | None]

#: Names to pin on every host. ``ps`` is POSIX-only; the Windows quiescence proof uses a Job Object
#: and launches nothing. ``bwrap``/``socat`` are Linux-only and probe-only.
#:
#: ``worc`` is here because ТA.1.7 names the daemon launcher among the four classes to pin: it is
#: the one path that hands the *next whole run* to whatever answers.
#: :func:`~wastech_orchestrator.cli_shell.daemon_argv` resolves it for the actual spawn (it also
#: tries ``wastech-orchestrator`` and falls back to ``-m``, which no pin can express); this entry is
#: what puts it under the drift check.
_ALWAYS: tuple[str, ...] = ("git", "gh", "worc")


def _host_names(system: str) -> tuple[str, ...]:
    """The host-specific names to pin, on top of :data:`_ALWAYS`."""
    if system == "Windows":
        return ()
    if system == "Linux":
        return ("ps", "bwrap", "socat")
    return ("ps",)


def resolve_launcher(name: str, *, which: Which = shutil.which) -> str | None:
    """The absolute path *name* resolves to on ``PATH``, or ``None`` when it resolves to nothing.

    A name that is already an absolute path resolves to itself when it is executable, which is what
    makes an operator's ``command: /opt/bin/claude`` (and a test's fake CLI) pass through unchanged.
    """
    return which(name)


@dataclass(frozen=True)
class PinnedLaunchers:
    """The paths resolved once, for the lifetime of a preflight report or a run.

    ``paths`` maps every pinned name to its resolved absolute path, or ``None`` when this host has
    none. ``None`` is kept rather than dropped: a missing ``gh`` is a fact the report has to state,
    and :meth:`launch` still hands the bare name back so the downstream launch failure stays the
    diagnostic it already is instead of becoming an ``Optional`` at every call site.
    """

    paths: Mapping[str, str | None]

    def launch(self, name: str) -> str:
        """The path to execute for *name*: the pinned one, or *name* if it never resolved."""
        return self.paths.get(name) or name

    def missing(self) -> tuple[str, ...]:
        """The pinned names this host could not resolve, in pin order."""
        return tuple(name for name, path in self.paths.items() if path is None)

    def drift(self, *, which: Which = shutil.which) -> tuple[str, ...]:
        """One line per name that no longer resolves where it did when this was pinned.

        Re-resolution, not a digest: the question is which file a bare-name launch would reach now,
        and that is what ``PATH`` order answers. Worth asking at exactly one moment — after the
        agent has finished and before the orchestrator publishes with these binaries — because that
        is the window a planted shim would have to hit, and by then the orchestrator is already
        using the pinned path, so a drift line reports an attempt rather than a breach.
        """
        lines: list[str] = []
        for name, pinned in self.paths.items():
            current = which(name)
            if current == pinned:
                continue
            lines.append(
                f"{name}: resolved to {pinned or '<nothing>'} when this run started and to "
                f"{current or '<nothing>'} now — something changed PATH or replaced the file "
                f"mid-run; the orchestrator kept using the path it pinned"
            )
        return tuple(lines)


def pin_launchers(
    config: OrchestratorConfig,
    *,
    which: Which = shutil.which,
    system: str | None = None,
) -> PinnedLaunchers:
    """Resolve every executable this configuration will launch, once.

    The set is the host-independent pair plus this host's own names plus each configured provider's
    ``command`` — configured, not merely allowed, because an operator who declared a provider will
    want to know which binary it found whether or not a flow node routes to it today.
    """
    names: list[str] = [
        *_ALWAYS,
        *_host_names(system if system is not None else platform.system()),
    ]
    for provider in config.agents.providers.values():
        if provider.command not in names:
            names.append(provider.command)
    return PinnedLaunchers({name: resolve_launcher(name, which=which) for name in names})

"""Launchability probing (backlog: automatic check discovery, §7).

Decides whether a candidate *can be launched* without running the full suite. The probe is
lightweight and tool-specific where it helps: it confirms an executable/path is present and, for a
``python -m <module>`` candidate, that the module imports. A probe launch failure means
``not_launchable`` (an infrastructure signal, never a quality failure). Every launch goes through
the shared safe runner (argv list, allowlisted env, mandatory timeout).
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from wastech_orchestrator.checks.model import CheckCandidate, ProbeStatus
from wastech_orchestrator.providers.process import ProcessResult, run_process
from wastech_orchestrator.security.env import build_child_env

RunProcess = Callable[..., ProcessResult]
Which = Callable[[str], str | None]


class CheckProbeRunner:
    """Classify each candidate as launchable / not launchable / unsupported."""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        allowed_environment: tuple[str, ...],
        run_process: RunProcess = run_process,
        which: Which = shutil.which,
        probe_timeout_seconds: int = 60,
    ) -> None:
        self._root = Path(repo_root)
        self._allowed = allowed_environment
        self._run_process = run_process
        self._which = which
        self._timeout = probe_timeout_seconds

    def probe(self, candidate: CheckCandidate) -> CheckCandidate:
        return replace(candidate, probe_status=self._classify(candidate.argv))

    def _classify(self, argv: tuple[str, ...]) -> ProbeStatus:
        head = argv[0]
        if _is_path(head):
            if not (self._root / head).is_file():
                return ProbeStatus.NOT_LAUNCHABLE
            if len(argv) >= 3 and argv[1] == "-m":
                return self._import_check(head, argv[2])
            return ProbeStatus.LAUNCHABLE
        if self._which(head) is None:
            return ProbeStatus.NOT_LAUNCHABLE
        return ProbeStatus.LAUNCHABLE

    def _import_check(self, python_rel: str, module: str) -> ProbeStatus:
        """Run ``<python> -c "import <module>"`` — proves the module imports (not the suite)."""
        env = build_child_env(self._allowed)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_process(
                [python_rel, "-c", f"import {module}"],
                cwd=self._root,
                env=env,
                timeout_seconds=self._timeout,
                stdout_path=str(Path(tmp) / "probe.out"),
            )
        if result.launch_error is not None or result.timed_out or result.exit_code != 0:
            return ProbeStatus.NOT_LAUNCHABLE
        return ProbeStatus.LAUNCHABLE


def _is_path(token: str) -> bool:
    return "/" in token or "\\" in token

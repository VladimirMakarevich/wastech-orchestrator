"""Runtime startup gates for the CLI (spec §6.7).

These are *runtime* preflight checks run at ``run``/``watch``/``rerun`` startup — distinct from the
read-only environment *detection* in :mod:`wastech_orchestrator.install.detect`. A gate fails fast
with an actionable message rather than letting a missing prerequisite surface deep inside a stage.
"""

from __future__ import annotations

from wastech_orchestrator.install.detect import has_gh


class GhNotAvailableError(OSError):
    """The GitHub CLI (``gh``) is required for PR creation but is not on ``PATH`` (§6.7)."""


def require_gh() -> None:
    """Raise :class:`GhNotAvailableError` unless ``gh`` is on ``PATH`` (hard pre-flight gate).

    The raising counterpart to :func:`~wastech_orchestrator.install.detect.has_gh`, used at
    ``watch``/``run`` startup when PR creation is enabled so a missing GitHub CLI fails fast with an
    actionable message rather than surfacing as a ``GitCommandError`` deep inside the publish stage.
    """
    if not has_gh():
        raise GhNotAvailableError(
            "'gh' (GitHub CLI) is not installed or not on PATH. Install it from "
            "https://cli.github.com/ and run 'gh auth login', or disable PR creation "
            "(git.create_pull_request: false)."
        )

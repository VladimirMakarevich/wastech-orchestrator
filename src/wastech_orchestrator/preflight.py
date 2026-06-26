"""Runtime startup gates for the CLI.

These are *runtime* preflight checks run at ``run``/``watch``/``rerun`` startup — distinct from the
read-only environment *detection* in :mod:`wastech_orchestrator.install.detect`. A gate fails fast
with an actionable message rather than letting a missing prerequisite surface deep inside a stage.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from wastech_orchestrator.install.detect import gh_auth_ok, has_gh

_LOG = logging.getLogger(__name__)

#: Generic, output-free advisory text. We never surface raw ``gh auth status`` output (it carries
#: the account login / token scopes); this fixed message is all the operator gets.
_GH_LOGGED_OUT_MESSAGE = (
    "gh present but not logged in — PR creation will fail at publish; run 'gh auth login'."
)


class GhNotAvailableError(OSError):
    """The GitHub CLI (``gh``) is required for PR creation but is not on ``PATH``."""


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


def warn_if_gh_logged_out(emit: Callable[[str], None] | None = None) -> None:
    """Emit a **non-blocking** advisory when ``gh`` is present but not authenticated.

    The soft auth layer on top of the hard :func:`require_gh` ``PATH`` gate: a logged-out ``gh``
    passes ``require_gh`` and then fails far downstream at ``gh pr create``, so surface it at
    startup instead. Warns only when ``gh`` is on ``PATH`` **and** :func:`gh_auth_ok` returns
    ``False`` (definitely logged out); ``None`` (probe unknown — a transient failure, or env-token
    auth the probe can't see) and ``True`` stay silent. Never raises and never blocks the run; a
    valid ``GH_TOKEN`` or a flaky probe must not stop a task. ``emit`` defaults to a WARNING log.
    """
    emit = emit if emit is not None else _LOG.warning
    if has_gh() and gh_auth_ok() is False:
        emit(_GH_LOGGED_OUT_MESSAGE)


def preflight_gh() -> tuple[bool, str]:
    """Return ``(ok, line)`` for the gh preflight report line.

    Hard-fails when ``gh`` is not on ``PATH``. Auth failure is non-blocking (mirrors
    :func:`warn_if_gh_logged_out`: a valid ``GH_TOKEN`` or a flaky probe must not block a run).
    Only call this when ``git.create_pull_request`` is enabled.
    """
    if not has_gh():
        return False, (
            "gh: FAIL — not on PATH; install from https://cli.github.com/ "
            "or set git.create_pull_request: false"
        )
    if gh_auth_ok() is False:
        return True, "gh: WARN — present but not logged in (run 'gh auth login')"
    return True, "gh: OK"

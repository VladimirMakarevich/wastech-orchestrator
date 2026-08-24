"""Runtime startup gates for the CLI.

These are *runtime* preflight checks run at ``run``/``watch``/``rerun`` startup — distinct from the
read-only environment *detection* in :mod:`wastech_orchestrator.install.detect`. A gate fails fast
with an actionable message rather than letting a missing prerequisite surface deep inside a stage.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from wastech_orchestrator.config.schema import SecurityConfig
from wastech_orchestrator.install.detect import gh_auth_ok, git_version, has_gh

_LOG = logging.getLogger(__name__)

#: The minimum git ``(major, minor)`` for the hook-neutralization: ``core.hooksPath`` (git
#: 2.9, 2016) is the override every orchestrator git command relies on to keep a target-repo hook
#: from executing. Older git silently ignores it, so the control would be ineffective.
_MIN_GIT_VERSION = (2, 9)

#: Generic, output-free advisory text. We never surface raw ``gh auth status`` output (it carries
#: the account login / token scopes); this fixed message is all the operator gets.
_GH_LOGGED_OUT_MESSAGE = (
    "gh present but not logged in — PR creation will fail at publish; run 'gh auth login'."
)


class GhNotAvailableError(OSError):
    """The GitHub CLI (``gh``) is required for PR creation but is not on ``PATH``."""


class GitControlUnavailableError(OSError):
    """Git is too old to enforce the git-control neutralization (needs >= 2.9)."""


class ProviderNotLoggedInError(OSError):
    """An allowed agent provider's CLI reports no stored credentials, so a run must not start.

    Raised by the CLI's startup gate rather than here: the check needs the config and the provider
    composition, which this module deliberately knows nothing about. Only the exception type lives
    beside its siblings, so every startup refusal is handled as one family.
    """


def require_git_control() -> None:
    """Fail fast unless git honors the hook-neutralization (``core.hooksPath``, git 2.9+).

    Every orchestrator git command runs with ``-c core.hooksPath=<private empty dir>`` so a
    target-repo hook can never execute in an orchestrator git process. Git older than 2.9 silently
    ignores that key — leaving repo hooks live — so a git below the floor fails closed here instead
    of running unprotected. An undetectable version (git absent/unusual ``--version``) is left to
    surface at first git use rather than blocked on an unparseable string.
    """
    version = git_version()
    if version is not None and version < _MIN_GIT_VERSION:
        raise GitControlUnavailableError(
            f"git {version[0]}.{version[1]} is too old for orchestrator isolation: every "
            f"orchestrator git command runs with `-c core.hooksPath=<empty dir>` so a hook in the "
            f"target repository can never execute, and git below "
            f"{_MIN_GIT_VERSION[0]}.{_MIN_GIT_VERSION[1]} (2016) silently ignores that key, which "
            f"would leave repository hooks live. Upgrade git."
        )


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


def warn_if_gh_logged_out(
    emit: Callable[[str], None] | None = None, security: SecurityConfig | None = None
) -> None:
    """Emit a **non-blocking** advisory when ``gh`` is present but not authenticated.

    The soft auth layer on top of the hard :func:`require_gh` ``PATH`` gate: a logged-out ``gh``
    passes ``require_gh`` and then fails far downstream at ``gh pr create``, so surface it at
    startup instead. Warns only when ``gh`` is on ``PATH`` **and** :func:`gh_auth_ok` returns
    ``False`` (definitely logged out); ``None`` (probe unknown — a transient failure, or env-token
    auth the probe can't see) and ``True`` stay silent. Never raises and never blocks the run; a
    valid ``GH_TOKEN`` or a flaky probe must not stop a task. ``emit`` defaults to a WARNING log.
    """
    emit = emit if emit is not None else _LOG.warning
    if has_gh() and gh_auth_ok(security) is False:
        emit(_GH_LOGGED_OUT_MESSAGE)


def preflight_gh(security: SecurityConfig | None = None) -> tuple[bool, str]:
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
    if gh_auth_ok(security) is False:
        return True, "gh: WARN — present but not logged in (run 'gh auth login')"
    return True, "gh: OK"

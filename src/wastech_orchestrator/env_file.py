"""Load the orchestrator's own ``.env`` file into the process environment at startup.

The orchestrator reads its secrets (e.g. the Telegram bot token / chat id named by
``telegram.bot_token_env`` / ``telegram.chat_id_env``) from ``os.environ``. To spare the operator
from exporting them in every shell/service, the CLI auto-loads ``<repo>/.worc/.env`` (and honours an
explicit ``--env-file``) before any command runs.

Two invariants:

* **Real environment wins.** Loading uses ``override=False`` — a variable already present in
  ``os.environ`` (an ``export``) is never overwritten by the file. The file only fills gaps.
* **No values are logged.** This module returns a *count* only; the caller logs the count + path.
  Anything secret-shaped that lands in ``os.environ`` is still gated from child processes by the
  ``security.allowed_environment`` allowlist (:mod:`wastech_orchestrator.security.env`) and scrubbed
  from artifacts by the redaction net — loading it here does not weaken either.

This module has no CLI/core knowledge (the CLI owns discovery + the missing-file policy).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

#: Every variable name the loaded file DEFINES — not merely the ones it managed to set. Recorded
#: at load time because this is the only moment the path is in hand: the child-environment builder
#: runs deep inside the adapters, which hold a ``SecurityConfig`` and nothing else. Process-global
#: for the same reason the environment it describes is, and no more surprising: this module already
#: writes to ``os.environ``, so remembering what it wrote there is the smaller of the two effects.
#:
#: Defined rather than newly-set, deliberately. A name the operator also exported is skipped by
#: ``override=False``, so the *value* in play is the shell's — but the orchestrator cannot tell one
#: from the other later, and withholding the name either way costs an operator who genuinely wants
#: it forwarded one explicit ``security.extra_environment`` line. (The two counts are not
#: interchangeable: a bare ``KEY`` line with no ``=`` is defined and never set.)
_DEFINED_NAMES: frozenset[str] = frozenset()


def load_env_file(path: Path) -> int:
    """Load ``KEY=value`` pairs from ``path`` into ``os.environ`` without overriding existing vars.

    The caller guarantees ``path`` exists. Returns the number of variables **newly** set (vars
    already present win, so the file only fills gaps). Never logs names or values. Also records the
    names the file defines, for :func:`env_file_names`.
    """
    global _DEFINED_NAMES
    before = set(os.environ)
    _DEFINED_NAMES = frozenset(dotenv_values(dotenv_path=path))
    load_dotenv(dotenv_path=path, override=False)
    return sum(1 for key in os.environ if key not in before)


def env_file_names() -> frozenset[str]:
    """The names the loaded env-file defines — empty when no file was loaded this process.

    These are the orchestrator's **own** secrets (the Telegram token out of the shipped
    ``.env.example``, whatever else the operator keeps there), and the agent is denied reading the
    file itself. So they are withheld from a child environment even where every other name is
    forwarded: a full pass-through would otherwise hand over the contents of a file the deny policy
    exists to protect. An operator who wants one of them in a child names it in
    ``security.extra_environment``, which is assignment — a decision, not a default.
    """
    return _DEFINED_NAMES


def count_env_file(path: Path) -> int:
    """Count the variables ``path`` defines, without loading them or returning any value.

    Used by the ``preflight`` health report to show the ``.env`` status. Parses keys only (values
    are never read into the environment, returned, or logged), so it preserves this module's
    no-values-logged invariant and is independent of what is already in ``os.environ``. The caller
    guarantees ``path`` exists.
    """
    return len(dotenv_values(dotenv_path=path))

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

from dotenv import load_dotenv


def load_env_file(path: Path) -> int:
    """Load ``KEY=value`` pairs from ``path`` into ``os.environ`` without overriding existing vars.

    The caller guarantees ``path`` exists. Returns the number of variables **newly** set (vars
    already present win, so the file only fills gaps). Never logs names or values.
    """
    before = set(os.environ)
    load_dotenv(dotenv_path=path, override=False)
    return sum(1 for key in os.environ if key not in before)

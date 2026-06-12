"""Project installer (``wastech-orchestrator install``).

A second setup flow alongside ``init``: it binds an existing Git repository to a sibling control
workspace, generates a validated ``config.yaml`` from detected/confirmed settings, and records a
``repo-root -> config.yaml`` binding so the other commands work from anywhere inside the repo
without ``--config``. See docs/backlog/interactive_installer.md and §20 of the canonical spec.

This package owns *installation-time* concerns only (detection, config generation, the registry,
and the wizard). It never commits/pushes, never installs or authorizes the agent CLIs, and never
weakens the security policy of the config it writes.
"""

from __future__ import annotations

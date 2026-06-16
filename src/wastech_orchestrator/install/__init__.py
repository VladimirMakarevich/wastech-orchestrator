"""Project installer (``wastech-orchestrator install``).

Sets up the orchestrator inside an existing Git repository: it generates a validated ``config.yaml``
from detected/confirmed settings and scaffolds the gitignored ``<repo>/.worc/`` runtime home that
holds it. Because the config always lives at ``<repo>/.worc/config.yaml``, the other commands
discover it by walking up to the Git root — no registry or ``--config`` needed. See §20 of the
canonical spec.

This package owns *installation-time* concerns only (detection, config generation, and the wizard).
It never commits/pushes, never installs or authorizes the agent CLIs, and never weakens the security
policy of the config it writes.
"""

from __future__ import annotations

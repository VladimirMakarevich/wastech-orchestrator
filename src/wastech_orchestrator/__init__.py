"""wastech-orchestrator — a lean orchestrator for coding agents (Codex / Claude Code) on top of Git.

Architecture source of truth: docs/implementation_stages/00_orchestrator_final_plan.md.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Single source of truth is the installed distribution metadata (pyproject `version`). The fallback
# only fires when running from a source tree with no installed dist.
try:
    __version__ = _pkg_version("wastech-orchestrator")
except PackageNotFoundError:  # pragma: no cover - source tree without an installed dist
    __version__ = "0.0.0+unknown"

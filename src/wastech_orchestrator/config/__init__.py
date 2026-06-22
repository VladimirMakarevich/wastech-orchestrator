"""Configuration layer: schema, fail-closed loader, and validator.

The loader/validator are the config-time half of the "security can't be weakened" invariant
(.agents/rules/security.md). Loading is an explicit call — no import-time side effects.
"""

from __future__ import annotations

from wastech_orchestrator.config.loader import ConfigError, ConfigLoadResult, load_config
from wastech_orchestrator.config.validation import validate_config

__all__ = [
    "ConfigError",
    "ConfigLoadResult",
    "load_config",
    "validate_config",
]

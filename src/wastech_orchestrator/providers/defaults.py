"""Canonical shipped provider defaults and public model examples.

Runtime model ids remain unrestricted. These values control only fresh generated configuration
and documentation examples; existing operator pins are never rewritten automatically.
"""

from __future__ import annotations

from wastech_orchestrator.providers.base import ProviderId

CANONICAL_CODEX_MODEL_DEFAULT = ""
CURRENT_PUBLIC_CODEX_MODEL_EXAMPLES: tuple[str, ...] = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)

SHIPPED_PROVIDER_DEFAULTS: dict[ProviderId, tuple[str, str]] = {
    ProviderId.CLAUDE: ("claude-sonnet-5", "high"),
    ProviderId.CODEX: (CANONICAL_CODEX_MODEL_DEFAULT, "high"),
}

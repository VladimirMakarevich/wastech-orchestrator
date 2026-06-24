"""Provider capability tables used by config and flow validation.

This module deliberately contains provider-facing capabilities, not CLI syntax. Adapters still own
the concrete flag/config mapping; core validators only ask whether a provider accepts a setting.
"""

from __future__ import annotations

from wastech_orchestrator.providers.base import ProviderId

CODEX_REASONING_ALIASES: dict[str, str] = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    # Legacy orchestrator/Codex compatibility: Codex's practical ceiling is xhigh.
    "max": "xhigh",
}

CLAUDE_REASONING_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})
CODEX_REASONING_LEVELS: frozenset[str] = frozenset(CODEX_REASONING_ALIASES)


def reasoning_levels_for(provider: ProviderId) -> frozenset[str]:
    """Return the configured reasoning values accepted for ``provider``."""
    if provider is ProviderId.CODEX:
        return CODEX_REASONING_LEVELS
    if provider is ProviderId.CLAUDE:
        return CLAUDE_REASONING_LEVELS
    return frozenset()


def all_reasoning_levels() -> frozenset[str]:
    """Broad structural parser allowlist; semantic checks stay provider-specific."""
    levels: set[str] = set()
    for provider in ProviderId:
        levels.update(reasoning_levels_for(provider))
    return frozenset(levels)


def is_reasoning_supported(provider: ProviderId, reasoning: str) -> bool:
    """Whether ``reasoning`` is a valid configured value for ``provider``."""
    return reasoning in reasoning_levels_for(provider)


def normalize_codex_reasoning(reasoning: str) -> str | None:
    """Map a configured Codex reasoning value to Codex's model config value."""
    return CODEX_REASONING_ALIASES.get(reasoning)


def map_reasoning_for_provider_switch(
    from_provider: ProviderId, to_provider: ProviderId, reasoning: str | None
) -> str | None:
    """Translate portable intent when a fallback switches providers.

    Most reasoning pins are provider-specific and must be cleared on cross-provider fallback.
    Codex's ``minimal`` has no Claude equivalent, but the closest lower-bound intent is Claude
    ``low``, so preserve that one case instead of dropping all effort information.
    """
    if (
        from_provider is ProviderId.CODEX
        and to_provider is ProviderId.CLAUDE
        and reasoning == "minimal"
    ):
        return "low"
    return None

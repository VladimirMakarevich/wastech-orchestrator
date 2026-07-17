"""Provider capability tables used by config and flow validation.

This module deliberately contains provider-facing capabilities, not CLI syntax. Adapters still own
the concrete flag/config mapping; core validators only ask whether a provider accepts a setting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from wastech_orchestrator.providers.base import (
    CodexComputeMode,
    CodexMultiAgentMode,
    ProviderId,
)

CODEX_SCALAR_REASONING_ALIASES: dict[str, str] = {
    "minimal": "minimal",
    "light": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
}

CLAUDE_REASONING_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})
CODEX_REASONING_LEVELS: frozenset[str] = frozenset(
    {
        *CODEX_SCALAR_REASONING_ALIASES,
        CodexComputeMode.MAX.value,
        CodexMultiAgentMode.ULTRA.value,
    }
)


@dataclass(frozen=True, slots=True)
class CodexReasoningSelection:
    """Normalized Codex execution controls with mutually exclusive advanced modes."""

    reasoning: str | None = None
    compute_mode: CodexComputeMode | None = None
    multi_agent_mode: CodexMultiAgentMode | None = None

    @property
    def effective(self) -> str | None:
        """Return the one user-visible effective mode represented by this selection."""
        if self.compute_mode is not None:
            return self.compute_mode.value
        if self.multi_agent_mode is not None:
            return self.multi_agent_mode.value
        return self.reasoning


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


def normalize_codex_reasoning(reasoning: str) -> CodexReasoningSelection | None:
    """Normalize aliases while preserving Max and Ultra as distinct typed execution modes."""
    scalar = CODEX_SCALAR_REASONING_ALIASES.get(reasoning)
    if scalar is not None:
        return CodexReasoningSelection(reasoning=scalar)
    if reasoning == CodexComputeMode.MAX:
        return CodexReasoningSelection(compute_mode=CodexComputeMode.MAX)
    if reasoning == CodexMultiAgentMode.ULTRA:
        return CodexReasoningSelection(multi_agent_mode=CodexMultiAgentMode.ULTRA)
    return None


def codex_model_reasoning_issue(model: str | None, reasoning: str | None) -> str | None:
    """Explain a known public model/advanced-mode incompatibility, if one is provable.

    Scalar effort and arbitrary future model ids remain pass-through. Advanced-mode validation is
    deliberately a denylist of known older public families: rejecting every unknown id would make
    the provider incompatible with future and private model rollouts.
    """
    if reasoning not in {CodexComputeMode.MAX, CodexMultiAgentMode.ULTRA} or not model:
        return None
    normalized_model = model.strip().lower()
    if re.match(r"^gpt-5\.[1-5](?:-|$)", normalized_model):
        return (
            f"Codex reasoning mode {reasoning!r} is not supported by known model "
            f"{model!r}; use a GPT-5.6 model, leave model empty for CLI/account selection, "
            "or choose a scalar effort"
        )
    return None


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

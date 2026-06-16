"""Permission-profile strictness ordering (spec §7.2, .agents/rules/security.md).

A small, provider-agnostic ordering over the permission profiles a stage can run under. The Agent
Router (P4) uses it for the **conditional** fallback rule: ``authorization_failed`` /
``permission_denied`` may fall back to another provider only when that provider operates in the
**same or a stricter** profile — never a looser one. Relaxing the policy to make a fallback possible
is prohibited, so an unknown profile is treated as *not* provably same-or-stricter (fail-closed).

Profiles mirror ``ProviderConfig.permission_profile`` (§11): ``read-only`` is the strictest,
``workspace-write`` allows writes within the workspace.
"""

from __future__ import annotations

# Lower rank = stricter (fewer privileges). Keep in sync with the permission_profile values the
# adapters understand (e.g. providers.claude._PROFILE_MAP).
_PROFILE_STRICTNESS: dict[str, int] = {
    "read-only": 0,
    "workspace-write": 1,
}


def is_same_or_stricter(candidate: str, reference: str) -> bool:
    """Return True iff ``candidate`` is at least as strict as ``reference``.

    Fail-closed: an unrecognized profile on either side returns False, because the orchestrator may
    never *relax* the policy to enable a fallback (spec §7.2). With both profiles known, this is
    ``rank(candidate) <= rank(reference)`` (a lower rank is stricter).
    """
    candidate_rank = _PROFILE_STRICTNESS.get(candidate)
    reference_rank = _PROFILE_STRICTNESS.get(reference)
    if candidate_rank is None or reference_rank is None:
        return False
    return candidate_rank <= reference_rank

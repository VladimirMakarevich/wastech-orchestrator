"""Resolved-profile command signature + backward-compatible load (post-test-run)."""

from __future__ import annotations

from wastech_orchestrator.checks.model import CheckSource, ResolvedCheck
from wastech_orchestrator.checks.profile import (
    PROFILE_SCHEMA_VERSION,
    ResolvedCheckProfile,
    commands_signature,
)


def _check(name: str, *argv: str) -> ResolvedCheck:
    return ResolvedCheck(name=name, argv=tuple(argv))


def test_commands_signature_is_order_independent() -> None:
    a = [_check("tests", "pytest"), _check("types", "mypy", "src")]
    b = [_check("types", "mypy", "src"), _check("tests", "pytest")]
    assert commands_signature(a) == commands_signature(b)


def test_commands_signature_changes_with_argv() -> None:
    base = [_check("types", "mypy", "src")]
    changed = [_check("types", "mypy", ".")]
    assert commands_signature(base) != commands_signature(changed)
    assert commands_signature([]) != commands_signature(base)


def _profile(**kw: object) -> ResolvedCheckProfile:
    defaults: dict[str, object] = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "ready": True,
        "source": CheckSource.DETECTED,
        "checks": (_check("tests", "pytest"),),
        "candidates": (),
        "platform": "linux",
        "fingerprint": "fp",
        "created_at": "t",
        "last_validated_at": "t",
    }
    defaults.update(kw)
    return ResolvedCheckProfile(**defaults)  # type: ignore[arg-type]


def test_approval_fields_round_trip() -> None:
    profile = _profile(
        commands_signature="sig", approved=True, approved_at="t", approved_interaction_id="d123"
    )
    loaded = ResolvedCheckProfile.from_json(profile.to_json())
    assert loaded is not None
    assert loaded.approved is True
    assert loaded.commands_signature == "sig"
    assert loaded.approved_interaction_id == "d123"


def test_v1_profile_loads_with_approved_false() -> None:
    # A profile written before lacks the approval fields; it must load with approved=False so
    # the next *change* to the command set triggers an approval (never crashes on the missing keys).
    legacy = _profile().to_json()
    for key in ("commands_signature", "approved", "approved_at", "approved_interaction_id"):
        legacy.pop(key, None)
    legacy["schema_version"] = 1
    loaded = ResolvedCheckProfile.from_json(legacy)
    assert loaded is not None
    assert loaded.approved is False
    assert loaded.commands_signature == ""

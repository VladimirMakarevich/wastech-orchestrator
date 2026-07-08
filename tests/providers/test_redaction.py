"""Tests for secret redaction. All tokens below are fake, never real secrets."""

from __future__ import annotations

import pytest

from wastech_orchestrator.providers.redaction import (
    REDACTED,
    redact_mapping,
    redact_text,
    secret_env_values,
)

# Fake credential-shaped strings (assembled so they are obviously not real secrets).
FAKE_GH = "ghp_" + "0123456789abcdef0123456789"
FAKE_OPENAI = "sk-" + "ABCDEFGHIJKLMNOPQRSTUV"
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_SLACK = "xoxb-" + "1234567890-abcdEFGH"
FAKE_JWT = "eyJhbGciOi.eyJzdWIiOi.s1gnatur3"


@pytest.mark.parametrize(
    "token",
    [FAKE_GH, FAKE_OPENAI, FAKE_AWS, FAKE_SLACK, FAKE_JWT, "Bearer abc123.def-456"],
)
def test_token_shapes_are_masked(token: str) -> None:
    out = redact_text(f"the value is {token} ok")
    assert token not in out
    assert REDACTED in out


def test_literal_extra_secret_never_survives() -> None:
    secret = "super-secret-passphrase-value"
    out = redact_text(f"prompt mentions {secret} inline", extra_secrets=[secret])
    assert secret not in out
    assert REDACTED in out


def test_sensitive_assignment_keeps_name_redacts_value() -> None:
    out = redact_text("OPENAI_API_KEY=sk-shouldNotLeak123456")
    assert "sk-shouldNotLeak123456" not in out
    assert "OPENAI_API_KEY" in out
    assert REDACTED in out


def test_ordinary_text_passes_through_unchanged() -> None:
    text = "Implement the feature in module foo and run the tests."
    assert redact_text(text) == text


def test_short_literal_secret_is_not_redacted() -> None:
    # A 1-3 char "secret" would mangle ordinary text; the guard skips it.
    out = redact_text("a cat sat on a mat", extra_secrets=["a"])
    assert out == "a cat sat on a mat"


def test_sub_floor_literal_is_ignored() -> None:
    # F45: the literal floor is aligned with the harvest floor (8); a shorter value is not treated
    # as a redaction literal (it would mangle ordinary text without protecting a real secret).
    out = redact_text("run the code path now", extra_secrets=["code"])  # len 4 < 8
    assert out == "run the code path now"


def test_literal_secret_redacted_only_on_word_boundary() -> None:
    # F45: an unbounded substring replace corrupted benign text (a short harvested value was cut
    # from the middle of an ordinary word, e.g. a lesson subject). Redact only standalone tokens.
    out = redact_text("the taskflow runs but subtaskflows stay", extra_secrets=["taskflow"])
    assert "subtaskflows" in out  # substring inside a larger word is left intact
    assert f"the {REDACTED} runs" in out  # the standalone occurrence is redacted


def test_literal_redaction_is_deterministic() -> None:
    # F36: identical input redacts identically (no order/randomness dependence).
    secret = "repeatable-secret-token"
    text = f"see {secret} here and {secret} there"
    first = redact_text(text, extra_secrets=[secret])
    assert first == redact_text(text, extra_secrets=[secret])
    assert secret not in first


def test_mapping_sensitive_key_value_fully_redacted() -> None:
    out = redact_mapping({"api_key": "anything-at-all", "model": "gpt-x"})
    assert out == {"api_key": REDACTED, "model": "gpt-x"}


def test_mapping_recurses_into_nested_structures() -> None:
    obj = {
        "prompt": f"use {FAKE_GH} please",
        "nested": {"authorization": "Bearer xyz", "note": "fine"},
        "items": ["plain", f"key={FAKE_OPENAI}"],
    }
    out = redact_mapping(obj)
    assert FAKE_GH not in out["prompt"]
    assert out["nested"]["authorization"] == REDACTED
    assert out["nested"]["note"] == "fine"
    assert FAKE_OPENAI not in out["items"][1]


def test_redact_mapping_does_not_mutate_input() -> None:
    obj = {"api_key": "secret-value-1234", "list": ["secret-value-1234"]}
    snapshot = {"api_key": "secret-value-1234", "list": ["secret-value-1234"]}
    redact_mapping(obj, extra_secrets=["secret-value-1234"])
    assert obj == snapshot


def test_non_string_scalars_are_preserved() -> None:
    out = redact_mapping({"attempt": 1, "ok": True, "ratio": 0.5, "none": None})
    assert out == {"attempt": 1, "ok": True, "ratio": 0.5, "none": None}


def test_usage_counter_keys_are_not_redacted() -> None:
    # 'input_tokens' contains 'token' as a substring but its segment is 'tokens' — not a secret.
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert redact_mapping(usage) == usage


def test_access_token_key_is_redacted() -> None:
    out = redact_mapping({"access_token": "value", "github_token": "value"})
    assert out == {"access_token": REDACTED, "github_token": REDACTED}


def test_secret_env_values_harvests_only_non_allowlisted_secret_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The shared env-secret harvester (used by the provider adapters and the memory write path):
    # collect the value of a secret-named, non-allowlisted, long-enough env var — and nothing else.
    monkeypatch.setenv("MY_API_KEY", "supersecretvalue123")  # secret name, not allowlisted -> kept
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_notarealtoken_value")  # secret name -> kept
    monkeypatch.setenv("PATH_TO_NOWHERE", "/usr/bin")  # not a secret name -> skipped
    monkeypatch.setenv("ALLOWED_SECRET_TOKEN", "exportedonpurpose1")  # allowlisted -> skipped
    monkeypatch.setenv("SHORT_TOKEN", "x")  # secret name but too short -> skipped
    harvested = set(secret_env_values(allowed_environment=("ALLOWED_SECRET_TOKEN", "PATH")))
    assert "supersecretvalue123" in harvested
    assert "ghp_notarealtoken_value" in harvested
    assert "/usr/bin" not in harvested
    assert "exportedonpurpose1" not in harvested
    assert "x" not in harvested

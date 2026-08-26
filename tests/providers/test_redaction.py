"""Tests for secret redaction. All tokens below are fake, never real secrets."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import ProviderConfig, SecurityConfig
from wastech_orchestrator.providers.codex import CodexProvider
from wastech_orchestrator.providers.redaction import (
    REDACTED,
    redact_jsonl,
    redact_mapping,
    redact_text,
    secret_env_values,
)
from wastech_orchestrator.security.env import build_child_env

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
    # The literal floor is aligned with the harvest floor (8); a shorter value is not treated
    # as a redaction literal (it would mangle ordinary text without protecting a real secret).
    out = redact_text("run the code path now", extra_secrets=["code"])  # len 4 < 8
    assert out == "run the code path now"


def test_literal_secret_redacted_only_on_word_boundary() -> None:
    # An unbounded substring replace corrupted benign text (a short harvested value was cut
    # from the middle of an ordinary word, e.g. a lesson subject). Redact only standalone tokens.
    out = redact_text("the taskflow runs but subtaskflows stay", extra_secrets=["taskflow"])
    assert "subtaskflows" in out  # substring inside a larger word is left intact
    assert f"the {REDACTED} runs" in out  # the standalone occurrence is redacted


def test_literal_redaction_is_deterministic() -> None:
    # Identical input redacts identically (no order/randomness dependence).
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


# -- one name policy across text and mappings ---------------------------------


@pytest.mark.parametrize(
    "benign",
    [
        "tokens: thresholdSchema.optional(),",  # the real p9-09 corrupted payload
        "input_tokens: 4447658,",
        '"input_tokens": 4447658,',
        "let apiKeyword = 1",
        "secretName: foo",
        "const TOKENS = countTokens(input)",
        "tokens: { warn: 5, error: 10 },",  # the real p10-05 handoff corruption
    ],
)
def test_benign_identifiers_survive_text_redaction(benign: str) -> None:
    # `_ASSIGNMENT` matched a sensitive word as a SUBSTRING of the name, so the ordinary
    # identifier `tokens` was treated as secret-bearing. That contradicted the policy the same
    # module documents and `is_sensitive_key` implements (`test_usage_counter_keys_are_not_redacted`
    # is the mapping-side pin of the same rule) — and it corrupted source text in artifacts, in the
    # committed report and in the inter-node handoff channel. One policy now serves both paths.
    assert redact_text(benign) == benign


@pytest.mark.parametrize(
    ("secret_bearing", "value"),
    [
        ("GITHUB_TOKEN=", "ghp_notarealtoken_value"),
        ("api_key: ", "hunter2secretvalue"),
        ('"access_token": "', 'abcdefghijklmnop"'),  # JSON-key form, quoted value
        ('"api_key":"', 'abcdefghijklmnop"'),  # no spaces
        ("AWS_SECRET_ACCESS_KEY=", "abcdefghijklmnop"),
        ("password=", "hunter2value"),
        ("PRIVATE_KEY: ", "xyzxyzxyzxyz"),
        ("X-Api-Key: ", "abcdefghijklmnop"),
    ],
)
def test_secret_bearing_names_still_lose_their_value(secret_bearing: str, value: str) -> None:
    # The direction that matters: narrowing the NAME matcher must not un-redact a real secret. The
    # quoted-key rows are the ones to watch — if a JSON key's closing quote ends the match,
    # `"access_token": "…"` goes unredacted despite the module claiming to handle that form.
    out = redact_text(secret_bearing + value)
    assert value.rstrip('"') not in out
    assert REDACTED in out


# -- JSON-lines sinks stay parseable (harm 1) ---------------------------------

# Source-shaped payloads a provider streams back as tool results: each holds an escaped quote right
# next to a sensitive-looking name, which is the shape that breaks a naive character-level scrub.
_JSONL_CORPUS = (
    {"text": '  tokens: "tokens",'},  # the real p9-09 events.jsonl corruption
    {"text": '  password: "hunter2value",'},
    {"text": 'const key = "abcdefghijklmnop";'},
    {"type": "usage", "input_tokens": 4447658, "cache_read_input_tokens": 7490000},
    {"nested": [{"authorization": "Bearer abcdefghij"}, "tokens: { warn: 5 }"]},
    {"unicode": "путь: значение", "escaped": 'a \\ b "c" d'},
)


def test_redact_jsonl_keeps_every_line_parseable() -> None:
    # Redaction ran on the SERIALIZED line, and the value group `[^\s"]+` ate the backslash
    # of an escaped quote — 2 of 14 events.jsonl files in the p9-09 run had an unparsable line, and
    # the payload lost was the tool result behind one of that run's own findings. Decoding first
    # makes it structurally impossible: the escape is gone before any pattern applies.
    stream = "".join(json.dumps(obj) + "\n" for obj in _JSONL_CORPUS)
    out = redact_jsonl(stream)
    for line in out.splitlines():
        json.loads(line)  # raises if redaction broke the line


def test_redact_jsonl_preserves_benign_content_and_redacts_secrets() -> None:
    stream = json.dumps({"text": '  tokens: "tokens",', "api_key": "abcdefghijklmnop"}) + "\n"
    decoded = json.loads(redact_jsonl(stream))
    assert decoded["text"] == '  tokens: "tokens",'  # benign identifier survives byte-identical
    assert decoded["api_key"] == REDACTED  # sensitive key still loses its whole value


def test_redact_jsonl_redacts_a_secret_inside_a_decoded_string() -> None:
    stream = json.dumps({"text": f"exported {FAKE_GH} to the log"}) + "\n"
    decoded = json.loads(redact_jsonl(stream))
    assert FAKE_GH not in decoded["text"]
    assert REDACTED in decoded["text"]


def test_redact_jsonl_falls_back_to_text_on_a_non_json_line() -> None:
    # A provider preamble or a truncated tail must still be scrubbed, never passed through raw.
    out = redact_jsonl("warming up\nGITHUB_TOKEN=" + FAKE_GH + '\n{"ok": true}\n')
    assert FAKE_GH not in out
    assert REDACTED in out
    assert out.splitlines()[0] == "warming up"


@pytest.mark.parametrize(
    "stream",
    ['{"a": 1}\r\n{"b": 2}\r\n', '{"a": 1}\n{"b": 2}\n', '{"a": 1}\n{"b": 2}', "", "\n\n"],
)
def test_redact_jsonl_preserves_line_endings(stream: str) -> None:
    # Cross-platform: a CRLF stream must survive as CRLF, and a missing trailing newline must not
    # gain one — the sink is an audit record, so its line structure is part of the evidence.
    out = redact_jsonl(stream)
    assert out.count("\r\n") == stream.count("\r\n")
    assert out.endswith("\n") == stream.endswith("\n")


def test_redact_jsonl_is_deterministic_and_order_preserving() -> None:
    # Same input, same output. Key order is the provider's, not sorted, so the sink stays
    # diffable against the raw stream.
    stream = json.dumps({"zeta": 1, "alpha": 2, "api_key": "abcdefghijklmnop"}) + "\n"
    first, second = redact_jsonl(stream), redact_jsonl(stream)
    assert first == second
    assert list(json.loads(first)) == ["zeta", "alpha", "api_key"]


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


def test_assigned_variables_never_shrink_the_harvest(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redaction literal set may only grow, never shrink, as the env policy widens.

    The harvester exempts a name **because it is on the forward allowlist** — an allowlisted secret
    is exported on purpose, not a value to scrub. `security.extra_environment` must not buy the same
    exemption: assigning `MY_API_KEY` in `config.yaml` says nothing about the parent process's
    variable of that name, and treating the union as allowlisted would lift redaction off it. Driven
    through the adapter's real call site, because passing the union is the obvious-looking refactor
    and it would not fail a test that calls the harvester directly.
    """
    monkeypatch.setenv("MY_API_KEY", "supersecretvalue123")
    provider = CodexProvider(
        codex_config,
        security=replace(security_config, extra_environment={"MY_API_KEY": "assigned"}),
        artifacts_root=tmp_path,
    )
    assert "supersecretvalue123" in provider._secret_env_values()


@pytest.mark.parametrize(
    "patterns",
    [
        (),
        ("MY_*",),
        ("M*",),
        ("MY_API_KE*",),
        ("MY_*", "AWS_*", "GITHUB_*"),
        ("*",),
    ],
)
def test_prefix_patterns_never_shrink_the_harvest(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patterns: tuple[str, ...],
) -> None:
    """As a property: no set of prefix patterns shrinks the redaction literal set.

    True by construction — the harvester exempts a name by **exact** membership in the allowlist,
    and
    a pattern string is never equal to a variable name, so `NUGET_*` in the list exempts nothing
    while `NUGET_API_KEY` stays under redaction. Pinned anyway, because the one refactor that would
    break it looks like an improvement: expanding the patterns before handing the list over, which
    would quietly lift redaction off every secret a pattern happens to reach.

    A lone `*` is in the parameter set deliberately. The validator refuses it, so no config can hold
    it — but it is the worst case for exactly the mistake above, and this property must not depend
    on
    another layer's validation to hold.
    """
    monkeypatch.setenv("MY_API_KEY", "supersecretvalue123")
    baseline = CodexProvider(codex_config, security=security_config, artifacts_root=tmp_path)
    widened = CodexProvider(
        codex_config,
        security=replace(
            security_config,
            allowed_environment=(*security_config.allowed_environment, *patterns),
        ),
        artifacts_root=tmp_path,
    )
    assert set(baseline._secret_env_values()) <= set(widened._secret_env_values())
    assert "supersecretvalue123" in widened._secret_env_values()


# --- the literal set across the mode, plus the exemption that keeps it holdable ------------------


def test_advanced_mode_never_shrinks_the_harvest(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As a property over the mode itself: the mode's literal set is a superset, never smaller.

    The rule inverts because the strict one collapses: with the parent environment forwarded whole,
    "secret-named AND not allowlisted" matches nothing, so the layer that exists for secrets with no
    recognizable shape would collect an empty set exactly when the environment holds the most of
    them. Driven through the adapter's real call site, because wiring the mode is the part that can
    be forgotten and a direct call to the harvester would not notice.

    `ALLOWLISTED_TOKEN` carries the property: strict isolation excuses it (exported on purpose), and
    the mode does not, so it can only appear on the wider side.
    """
    monkeypatch.setenv("MY_API_KEY", "supersecretvalue123")
    monkeypatch.setenv("ALLOWLISTED_TOKEN", "forwarded-on-purpose-1")
    security = replace(
        security_config,
        allowed_environment=(*security_config.allowed_environment, "ALLOWLISTED_TOKEN"),
    )
    strict = CodexProvider(codex_config, security=security, artifacts_root=tmp_path)
    mode = CodexProvider(
        codex_config,
        security=replace(security, strict_isolation=False),
        artifacts_root=tmp_path,
    )
    assert set(strict._secret_env_values()) <= set(mode._secret_env_values())
    assert "forwarded-on-purpose-1" not in strict._secret_env_values()
    assert "forwarded-on-purpose-1" in mode._secret_env_values()
    assert "supersecretvalue123" in mode._secret_env_values()


def test_env_file_values_stay_in_the_harvest_in_the_mode(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Withholding an env-file name from the CHILD must not drop it from the scrub.

    The two rules pull in opposite directions and meet here. One keeps `.worc/.env` names out of
    the child environment; the harvest still has to scrub their values, because the orchestrator's
    own process holds them and they can reach a log line or an artifact through it. The natural
    implementation — harvest from the environment the child was given — would silently drop them,
    and the superset property above would not catch it: both sides would shrink together. So this is
    pinned separately, as the requirement demands.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-file-loaded-secret-1")
    monkeypatch.setattr(
        "wastech_orchestrator.security.env.env_file_names",
        lambda: frozenset({"TELEGRAM_BOT_TOKEN"}),
    )
    mode = CodexProvider(
        codex_config,
        security=replace(security_config, strict_isolation=False),
        artifacts_root=tmp_path,
    )
    child = build_child_env(mode._security)
    assert "TELEGRAM_BOT_TOKEN" not in child  # withheld from the child …
    assert "env-file-loaded-secret-1" in mode._secret_env_values()  # … and still scrubbed


@pytest.mark.parametrize("strict_isolation", [True, False])
def test_the_working_directory_is_not_harvested_as_a_secret(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strict_isolation: bool,
) -> None:
    """`PWD` matches the `pwd` segment and holds a path, so it was scrubbing every citation.

    A live defect before the mode existed: `PWD` is absent from the default allowlist, so the run's
    own absolute path became a redaction literal and `<repo>/src/foo.py:42` printed as
    `[REDACTED]/src/foo.py:42` in reports and pull-request bodies. Parameterized over both settings
    because the exemption has to hold in both: strict isolation had a workaround (allowlist the
    name) and the mode takes it away, so an exemption on one side only would fix the mode and leave
    the shipped default broken — and the literal set is compared after the exemption, so applying it
    on both sides is also what keeps that property true.
    """
    monkeypatch.setenv("PWD", "/Users/someone/work/orchestrator")
    monkeypatch.setenv("OLDPWD", "/Users/someone/elsewhere")
    monkeypatch.setenv("DB_PWD", "a-real-database-password")
    provider = CodexProvider(
        codex_config,
        security=replace(security_config, strict_isolation=strict_isolation),
        artifacts_root=tmp_path,
    )
    harvested = provider._secret_env_values()
    assert "/Users/someone/work/orchestrator" not in harvested
    assert "/Users/someone/elsewhere" not in harvested
    # Narrow: the exemption is by exact name, so the segment stays useful for what it is there for.
    assert "a-real-database-password" in harvested


def test_a_citation_survives_redaction_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # The defect as an operator saw it, through the real text path rather than the harvester alone.
    monkeypatch.setenv("PWD", "/Users/someone/work/orchestrator")
    literals = secret_env_values(allowed_environment=(), exempt_allowlisted=False)
    quoted = "failing test at /Users/someone/work/orchestrator/src/foo.py:42"
    assert redact_text(quoted, extra_secrets=literals) == quoted

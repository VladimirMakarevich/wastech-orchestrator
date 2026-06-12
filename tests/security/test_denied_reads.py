"""denied_read_paths enforcement (spec §12.4, §6.1).

Covers the redaction content-scan (:func:`read_denied_secrets`) and the Claude ``Read(...)`` deny
pattern builder. The end-to-end "secret never lands in an artifact" assertion lives in the core
seeded-secret test.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.providers.claude import _deny_read_tools_for
from wastech_orchestrator.providers.redaction import read_denied_secrets, redact_text


def test_reads_env_values_excluding_short_ones(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("API_KEY=supersecretvalue123\nDEBUG=true\n", encoding="utf-8")
    secrets = read_denied_secrets(tmp_path, (".env", "secrets/**"))
    assert "supersecretvalue123" in secrets
    assert "true" not in secrets  # 4-char common value stays below the harvest threshold


def test_reads_secrets_dir_recursively(tmp_path: Path) -> None:
    (tmp_path / "secrets" / "nested").mkdir(parents=True)
    (tmp_path / "secrets" / "token.txt").write_text("opaqueLongSecretToken\n", encoding="utf-8")
    (tmp_path / "secrets" / "nested" / "k.json").write_text(
        '{"api_key": "anotherLongSecret999"}\n', encoding="utf-8"
    )
    secrets = read_denied_secrets(tmp_path, ("secrets/**",))
    assert "opaqueLongSecretToken" in secrets
    assert "anotherLongSecret999" in secrets  # bare value pulled out of the JSON line


def test_missing_paths_are_silently_skipped(tmp_path: Path) -> None:
    assert read_denied_secrets(tmp_path, (".env", "secrets/**")) == ()


def test_size_cap_bounds_the_read(tmp_path: Path) -> None:
    big = "X" * 100
    (tmp_path / ".env").write_text(f"K={big}\n", encoding="utf-8")
    secrets = read_denied_secrets(tmp_path, (".env",), max_bytes=10)
    assert big not in secrets  # only the first 10 bytes were read


def test_harvested_secret_feeds_redaction(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=leakedSecretValue42\n", encoding="utf-8")
    secrets = read_denied_secrets(tmp_path, (".env",))
    leaked = "the agent printed leakedSecretValue42 to stdout"
    assert "leakedSecretValue42" not in redact_text(leaked, extra_secrets=secrets)


def test_deny_read_tools_builds_read_patterns() -> None:
    assert _deny_read_tools_for((".env", "secrets/**")) == ["Read(.env)", "Read(secrets/**)"]


def test_deny_read_tools_skips_blank_paths() -> None:
    assert _deny_read_tools_for(("", "   ", ".env")) == ["Read(.env)"]

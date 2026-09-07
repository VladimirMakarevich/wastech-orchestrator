"""Tests for the startup ``.env`` loader and its CLI wiring.

Covers the pure loader (:func:`env_file.load_env_file`), the CLI path resolver
(:func:`cli.resolve_env_file_path`), the fail-closed missing-``--env-file`` exit, the
``.worc/.env`` auto-discovery → ``os.environ`` → Telegram path, and the security guarantee that a
loaded secret is still gated from child processes by the allowlist.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.config.schema import SecurityConfig, TelegramConfig
from wastech_orchestrator.env_file import load_env_file
from wastech_orchestrator.notify.telegram import check_telegram_preflight
from wastech_orchestrator.providers.redaction import is_sensitive_key
from wastech_orchestrator.security.env import build_child_env


@pytest.fixture(autouse=True)
def _restore_environ() -> Iterator[None]:
    """``load_dotenv`` mutates the real ``os.environ``; snapshot and restore around each test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _ns(**kwargs: object) -> argparse.Namespace:
    kwargs.setdefault("env_file", None)
    kwargs.setdefault("config", None)
    return argparse.Namespace(**kwargs)


# --- pure loader -------------------------------------------------------------------------------


def test_loads_key_value_pairs_with_comments_quotes_and_export(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "TELEGRAM_BOT_TOKEN=123:ABC\n"
        "export TELEGRAM_CHAT_ID='-1001234567890'\n"
        'QUOTED="a value"\n',
        encoding="utf-8",
    )
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "QUOTED"):
        os.environ.pop(key, None)

    count = load_env_file(env_file)

    assert count == 3
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "123:ABC"
    assert os.environ["TELEGRAM_CHAT_ID"] == "-1001234567890"
    assert os.environ["QUOTED"] == "a value"


def test_existing_env_var_is_not_overridden(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("WASTECH_DOTENV_PRECEDENCE=from_file\n", encoding="utf-8")
    os.environ["WASTECH_DOTENV_PRECEDENCE"] = "from_export"

    count = load_env_file(env_file)

    # The exported value wins; the file only fills gaps, so nothing was newly set.
    assert os.environ["WASTECH_DOTENV_PRECEDENCE"] == "from_export"
    assert count == 0


# --- CLI path resolution -----------------------------------------------------------------------


def test_resolve_explicit_env_file_is_required(tmp_path: Path) -> None:
    explicit = tmp_path / "secrets.env"
    path, required = cli.resolve_env_file_path(_ns(env_file=str(explicit)))
    assert path == explicit
    assert required is True


def test_resolve_defaults_to_env_beside_config(tmp_path: Path) -> None:
    config_path = tmp_path / ".worc" / "config.yaml"
    path, required = cli.resolve_env_file_path(_ns(config=str(config_path)))
    assert path == tmp_path / ".worc" / ".env"
    assert required is False


def test_resolve_falls_back_to_worc_env_at_git_root(
    git_repo: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = git_repo.clone  # type: ignore[attr-defined]
    monkeypatch.chdir(clone)
    # No config.yaml exists yet, so resolution falls back to <git-root>/.worc/.env.
    path, required = cli.resolve_env_file_path(_ns())
    assert path == clone / ".worc" / ".env"
    assert required is False


# --- main() wiring -----------------------------------------------------------------------------


def test_missing_explicit_env_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.env"
    rc = cli.main(["--env-file", str(missing), "status"])
    assert rc == 2
    assert "env-file not found" in capsys.readouterr().out


def test_load_env_file_is_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("WASTECH_NOTICE_SECRET=topsecretvalue\n", encoding="utf-8")
    os.environ.pop("WASTECH_NOTICE_SECRET", None)

    cli._load_env_file_for(_ns(env_file=str(env_file)))

    captured = capsys.readouterr()
    # Loading is silent now — the .env status is reported by preflight, not on every command.
    assert "loaded" not in captured.out
    assert "loaded" not in captured.err
    assert "topsecretvalue" not in captured.out + captured.err
    # ...but the variable is still loaded so downstream commands see it.
    assert os.environ["WASTECH_NOTICE_SECRET"] == "topsecretvalue"


# --- preflight .env health line (count + path only, never values) ------------------------------


def test_preflight_env_line_reports_count_and_path_never_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\nWASTECH_NOTICE_SECRET=topsecretvalue\n", encoding="utf-8")

    line = cli._env_preflight_line(env_file)

    assert "loaded 2 variable(s)" in line
    assert env_file.as_posix() in line
    assert "topsecretvalue" not in line  # the secret value is never printed


def test_preflight_env_line_when_no_file(tmp_path: Path) -> None:
    assert "no .env file" in cli._env_preflight_line(None)
    assert "no .env file" in cli._env_preflight_line(tmp_path / "absent.env")


def test_preflight_env_line_empty_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# only a comment\n", encoding="utf-8")
    assert "defines no variables" in cli._env_preflight_line(env_file)


# --- integration: .env -> os.environ -> Telegram ----------------------------------------------


class _FakeBotClient:
    def get_me(self) -> str:
        return "smoketestbot"

    def get_chat(self, **_: object) -> str:
        return "project-chat"

    def get_webhook_url(self) -> str:
        return ""

    def check_polling(self) -> None:
        return None


def test_auto_loaded_env_reaches_telegram_preflight(
    git_repo: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = git_repo.clone  # type: ignore[attr-defined]
    (clone / ".worc").mkdir(exist_ok=True)
    (clone / ".worc" / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=123:AAFakeToken\nTELEGRAM_CHAT_ID=987654321\n", encoding="utf-8"
    )
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        os.environ.pop(key, None)
    monkeypatch.chdir(clone)

    cli._load_env_file_for(_ns())

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "123:AAFakeToken"
    cfg = TelegramConfig(
        enabled=True,
        bot_token_env="TELEGRAM_BOT_TOKEN",
        chat_id_env="TELEGRAM_CHAT_ID",
        ask_timeout_s=60,
    )
    # check reads os.environ by default; a fake client avoids any network call.
    ok, line = check_telegram_preflight(cfg, client_factory=lambda _s, _c: _FakeBotClient())
    assert ok is True
    assert "OK" in line


# --- security: a loaded secret is still gated from children ------------------------------------


def test_loaded_secret_is_not_forwarded_to_children(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MY_SERVICE_TOKEN=supersecretvalue123\n", encoding="utf-8")
    os.environ.pop("MY_SERVICE_TOKEN", None)

    load_env_file(env_file)

    assert os.environ["MY_SERVICE_TOKEN"] == "supersecretvalue123"
    # The allowlist (which does not include it) still gates every child process.
    child = build_child_env(
        SecurityConfig(
            strict_isolation=True,
            allowed_environment=("PATH", "HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"),
            denied_read_paths=(),
            denied_commands=(),
        )
    )
    assert "MY_SERVICE_TOKEN" not in child
    # And it is a sensitive-named value, so the redaction net harvests it from os.environ.
    assert is_sensitive_key("MY_SERVICE_TOKEN")

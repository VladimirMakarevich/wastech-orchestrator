"""The read-only `preflight` CLI command (spec §6.7).

Drives ``cmd_preflight`` with in-memory providers so the test is deterministic and free of real
subprocess/CLI concerns (each adapter's real ``preflight()`` is covered by the provider tests). The
focus here is the command's orchestration: health reporting, the isolation verdict, output, and the
exit code.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.observability import logging as obslog
from wastech_orchestrator.providers.base import AgentRunRequest, ProviderHealth, ProviderId


@pytest.fixture(autouse=True)
def _reset_package_logger() -> Iterator[None]:
    pkg = logging.getLogger(obslog.LOGGER_NAME)
    saved = pkg.handlers[:]
    pkg.handlers.clear()
    obslog._configured = False
    yield
    pkg.handlers.clear()
    pkg.handlers.extend(saved)
    obslog._configured = False


class _FakeHealthProvider:
    def __init__(self, provider_id: str, *, healthy: bool = True) -> None:
        self.id = provider_id
        self._healthy = healthy

    def preflight(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.id,
            executable_found=self._healthy,
            version="1.2.3" if self._healthy else None,
            authenticated=self._healthy,
            supports_required_features=self._healthy,
            message="available" if self._healthy else "executable not found",
        )

    def run(self, request: AgentRunRequest) -> object:  # pragma: no cover - never called
        raise NotImplementedError


def _args() -> argparse.Namespace:
    return argparse.Namespace(config="config.yaml", log_level="info")


def _patch_providers(monkeypatch: pytest.MonkeyPatch, config: object, **healthy: bool) -> None:
    monkeypatch.setattr(cli, "_load_config", lambda _path: config)
    providers = {
        ProviderId.CLAUDE: _FakeHealthProvider("claude", healthy=healthy.get("claude", True)),
        ProviderId.CODEX: _FakeHealthProvider("codex", healthy=healthy.get("codex", True)),
    }
    monkeypatch.setattr(cli, "build_providers", lambda _c, *, artifacts_root: providers)


def test_preflight_ready(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "claude: OK" in out
    assert "codex: OK" in out
    assert "isolation: OK" in out
    assert "preflight: ready" in out


def test_preflight_not_ready_when_a_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_providers(monkeypatch, make_git_config(git_repo.clone), codex=False)
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "codex: FAIL" in out
    assert "preflight: NOT ready" in out


def test_preflight_fails_on_isolation(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    monkeypatch.setattr(cli, "check_isolation", lambda _config: ["codex: sandbox is forbidden"])
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "isolation: FAIL" in out
    assert "codex: sandbox is forbidden" in out


def test_preflight_telegram_skip(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    monkeypatch.setattr(
        cli, "check_telegram_preflight", lambda _cfg: (True, "telegram: SKIP (disabled)")
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "telegram: SKIP" in out
    assert "preflight: ready" in out


def test_preflight_telegram_ok(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    monkeypatch.setattr(
        cli,
        "check_telegram_preflight",
        lambda _cfg: (True, "telegram: OK (bot=@mybot, chat_id configured)"),
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "telegram: OK" in out
    assert "preflight: ready" in out


def test_preflight_telegram_fail(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    monkeypatch.setattr(
        cli,
        "check_telegram_preflight",
        lambda _cfg: (False, "telegram: FAIL — env var(s) not set: TG_TOKEN"),
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "telegram: FAIL" in out
    assert "preflight: NOT ready" in out

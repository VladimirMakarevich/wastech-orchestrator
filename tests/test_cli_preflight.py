"""The read-only `preflight` CLI command.

Drives ``cmd_preflight`` with in-memory providers so the test is deterministic and free of real
subprocess/CLI concerns (each adapter's real ``preflight()`` is covered by the provider tests). The
focus here is the command's orchestration: health reporting, the isolation verdict, output, and the
exit code.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.notify import AskResult
from wastech_orchestrator.observability import logging as obslog
from wastech_orchestrator.providers._adapter_base import IsolationCapabilityReport
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AuthProbe,
    AuthState,
    ProviderHealth,
    ProviderId,
)
from wastech_orchestrator.security import env as env_mod

# The default credential answer for a healthy fake, so the ~15 tests that predate the auth probe
# keep printing an OK auth field instead of tripping the logged-out refusal.
_LOGGED_IN = AuthProbe(state=AuthState.LOGGED_IN, method="fake-method", detail="stored credentials")


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
    def __init__(
        self,
        provider_id: str,
        *,
        healthy: bool = True,
        degraded_reasons: tuple[str, ...] = (),
        smoke: IsolationCapabilityReport | None = None,
        auth: AuthProbe | None = _LOGGED_IN,
    ) -> None:
        self.id = provider_id
        self._healthy = healthy
        self._degraded_reasons = degraded_reasons
        self._smoke = smoke
        self._auth = auth

    def preflight(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.id,
            executable_found=self._healthy,
            version="1.2.3" if self._healthy else None,
            supports_required_features=self._healthy,
            message="available" if self._healthy else "executable not found",
            degraded_reasons=self._degraded_reasons,
            # A CLI that could not run makes no credential claim, matching the real adapter.
            auth=self._auth if self._healthy else None,
        )

    def isolation_capability_smoke(self, *, home_dir: object) -> IsolationCapabilityReport | None:
        return self._smoke

    def run(self, request: AgentRunRequest) -> object:  # pragma: no cover - never called
        raise NotImplementedError


def _args() -> argparse.Namespace:
    return argparse.Namespace(config="config.yaml", log_level="info")


def _patch_providers(
    monkeypatch: pytest.MonkeyPatch,
    config: object,
    *,
    gh_result: tuple[bool, str] = (True, "gh: OK"),
    degraded: dict[str, tuple[str, ...]] | None = None,
    smokes: dict[str, IsolationCapabilityReport] | None = None,
    auth: dict[str, AuthProbe | None] | None = None,
    **healthy: bool,
) -> None:
    monkeypatch.setattr(cli, "_load_config", lambda _path: config)
    degraded = degraded or {}
    smokes = smokes or {}
    auth = auth or {}
    providers = {
        ProviderId.CLAUDE: _FakeHealthProvider(
            "claude",
            healthy=healthy.get("claude", True),
            degraded_reasons=degraded.get("claude", ()),
            smoke=smokes.get("claude"),
            auth=auth.get("claude", _LOGGED_IN),
        ),
        ProviderId.CODEX: _FakeHealthProvider(
            "codex",
            healthy=healthy.get("codex", True),
            degraded_reasons=degraded.get("codex", ()),
            smoke=smokes.get("codex"),
            auth=auth.get("codex", _LOGGED_IN),
        ),
    }
    monkeypatch.setattr(cli, "build_providers", lambda _c, *, layout: providers)
    monkeypatch.setattr(cli, "preflight_gh", lambda: gh_result)


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


_LOGGED_OUT = AuthProbe(
    state=AuthState.LOGGED_OUT, method=None, detail="not logged in (run 'codex login')"
)


def test_preflight_fails_when_a_provider_is_logged_out(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # A deliberate inversion of the fallback-aware degradation rule above: a logged-out provider is
    # fatal even though a fallback exists, because "a fallback will cover" is exactly the assumption
    # a dead fallback breaks — and its silence is only discovered at the moment it is needed.
    _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),  # allowed: [claude, codex]
        auth={"codex": _LOGGED_OUT},
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "codex: FAIL — not logged in (run 'codex login')" in out
    # The message must name every lever, including the one an operator will actually hit.
    assert "agents.allowed" in out
    assert "security.allowed_environment" in out
    assert "preflight: NOT ready" in out


def test_preflight_fails_when_the_logged_out_provider_is_the_primary(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # The verdict is role-independent: the same answer whether the provider is a fallback or the one
    # that runs every node.
    base = make_git_config(git_repo.clone)
    codex_primary = replace(
        base,
        agents=replace(
            base.agents,
            providers={
                ProviderId.CLAUDE: replace(base.agents.providers[ProviderId.CLAUDE], primary=False),
                ProviderId.CODEX: replace(base.agents.providers[ProviderId.CODEX], primary=True),
            },
        ),
    )
    _patch_providers(monkeypatch, codex_primary, auth={"codex": _LOGGED_OUT})
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "codex: FAIL — not logged in" in out
    assert "preflight: NOT ready" in out


def test_preflight_warns_when_the_auth_probe_cannot_answer(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # A probe that could not read an answer must not block a run — the same principle that governs
    # the logged-out gh advisory. A drifted CLI is not evidence of a missing credential.
    _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),
        auth={
            "codex": AuthProbe(
                state=AuthState.UNKNOWN, method=None, detail="no recognizable credential answer"
            )
        },
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "codex: WARN — no recognizable credential answer" in out
    assert "preflight: ready" in out


def test_preflight_makes_no_auth_claim_when_nothing_was_probed(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # An adapter with no credential verb makes no claim at all — the defect being fixed was a field
    # that asserted an authentication nothing had checked.
    _patch_providers(
        monkeypatch, make_git_config(git_repo.clone), auth={"claude": None, "codex": None}
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "auth=" not in out
    assert "preflight: ready" in out


def test_preflight_healthy_line_names_the_auth_method(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "auth=logged_in (fake-method)" in out


def test_preflight_degraded_warns_when_fallback_exists(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # An advisory degradation (e.g. codex resume-grammar drift) is a WARNING, not fatal, when a
    # fallback provider is allowed — the fallback covers the degraded nodes. Preflight stays ready.
    _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),  # allowed: [claude, codex]
        degraded={"codex": ("codex exec resume grammar drift",)},
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "codex: WARN — codex exec resume grammar drift (a fallback provider will cover)" in out
    assert "preflight: ready" in out


def test_preflight_degraded_fails_without_fallback(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # The same degradation is FATAL when codex is the sole allowed provider — no fallback to
    # cover the degraded resume nodes, so preflight is NOT ready.
    base = make_git_config(git_repo.clone)
    codex_only = replace(
        base,
        agents=replace(
            base.agents,
            allowed=(ProviderId.CODEX,),
            providers={
                ProviderId.CODEX: replace(base.agents.providers[ProviderId.CODEX], primary=True),
            },
        ),
    )
    _patch_providers(
        monkeypatch, codex_only, degraded={"codex": ("codex exec resume grammar drift",)}
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "codex: FAIL — codex exec resume grammar drift (no fallback provider)" in out
    assert "preflight: NOT ready" in out


def test_preflight_does_not_validate_flows(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # Preflight is a run-surface health gate — it no longer touches flows. Even a malformed operator
    # flow in <repo>/.worc/flows/ does not appear in the output or flip the verdict (flow validation
    # moved to `worc validate-flow`; dispatch-time resolve() is the safety net).
    flows_dir = git_repo.clone / ".worc" / "flows"
    flows_dir.mkdir(parents=True)
    (flows_dir / "rogue.yaml").write_text("flow:\n  name: rogue\n")  # malformed → load error
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "flow" not in out
    assert "preflight: ready" in out


def test_preflight_fails_on_isolation(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    monkeypatch.setattr(
        cli, "check_isolation", lambda _config, _checks: ["codex: sandbox is forbidden"]
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "isolation: FAIL" in out
    assert "codex: sandbox is forbidden" in out


# --- The host-dependent half of the allowed_environment gate ------------------------------------


def _without_systemroot(config):
    kept = tuple(
        name for name in config.security.allowed_environment if name.upper() != "SYSTEMROOT"
    )
    return replace(config, security=replace(config.security, allowed_environment=kept))


def test_preflight_fails_on_windows_when_systemroot_is_missing(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # The platform is substituted, not read off the machine — the check must be exercised on every
    # host. FAIL, not WARN: claude.exe aborts before printing anything, so the run would report
    # nothing but "CLI did not succeed".
    monkeypatch.setattr(env_mod.platform, "system", lambda: "Windows")
    _patch_providers(monkeypatch, _without_systemroot(make_git_config(git_repo.clone)))
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "allowed-environment: FAIL" in out
    assert "SystemRoot" in out
    assert "0xC0000409" in out
    assert "preflight: NOT ready" in out


def test_preflight_ready_on_windows_when_systemroot_is_listed(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(env_mod.platform, "system", lambda: "Windows")
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))  # the fixture lists SYSTEMROOT
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "allowed-environment" not in out
    assert "preflight: ready" in out


def test_preflight_ready_on_linux_without_systemroot(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # The same config that fails above is fine here, and the Windows-only name is never mentioned.
    monkeypatch.setattr(env_mod.platform, "system", lambda: "Linux")
    _patch_providers(monkeypatch, _without_systemroot(make_git_config(git_repo.clone)))
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "SystemRoot" not in out
    assert "preflight: ready" in out


# --- The live no-model Codex isolation capability smoke surfaced in `worc preflight` ------------


def test_preflight_capability_smoke_ok(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),
        smokes={
            "codex": IsolationCapabilityReport(
                ok=True,
                status="passed",
                detail="codex workspace-write sandbox: OS-enforced",
                fatal=False,
            )
        },
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "isolation smoke OK" in out
    assert "preflight: ready" in out


def test_preflight_capability_smoke_policy_leak_is_fatal(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # A proven leak is a non-fallback security result → NOT ready even though claude is a fallback.
    _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),
        smokes={
            "codex": IsolationCapabilityReport(
                ok=False, status="policy-failed", detail="a denied path was readable", fatal=True
            )
        },
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL — isolation smoke" in out
    assert "preflight: NOT ready" in out


def test_preflight_capability_smoke_unsupported_warns_with_fallback(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # An undemonstrable sandbox degrades like a capability gap: WARN when a fallback exists.
    _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),
        smokes={
            "codex": IsolationCapabilityReport(
                ok=False, status="unsupported", detail="sandbox could not run here", fatal=False
            )
        },
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARN — isolation smoke" in out
    assert "preflight: ready" in out


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


def test_preflight_gh_ok_when_pr_enabled(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_providers(monkeypatch, make_git_config(git_repo.clone), gh_result=(True, "gh: OK"))
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "gh: OK" in out
    assert "preflight: ready" in out


def test_preflight_gh_fail_when_missing_and_pr_enabled(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),
        gh_result=(False, "gh: FAIL — not on PATH; install from https://cli.github.com/"),
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "gh: FAIL" in out
    assert "preflight: NOT ready" in out


def test_preflight_gh_warn_when_logged_out_and_pr_enabled(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # Auth failure is non-blocking — preflight stays ready so a GH_TOKEN env var or a flaky probe
    # does not prevent tasks from running.
    _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),
        gh_result=(True, "gh: WARN — present but not logged in (run 'gh auth login')"),
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "gh: WARN" in out
    assert "preflight: ready" in out


def test_telegram_test_success(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = make_git_config(git_repo.clone)
    config = replace(base, telegram=replace(base.telegram, enabled=True))
    monkeypatch.setattr(cli, "_load_config", lambda _path: config)
    monkeypatch.setattr(
        cli,
        "check_telegram_preflight",
        lambda _cfg: (True, "telegram: OK (polling ready)"),
    )
    notifier = SimpleNamespace(ask_human=lambda **_kwargs: AskResult(answered=True, text="ok"))
    monkeypatch.setattr(cli, "build_notifier", lambda _cfg: notifier)
    args = argparse.Namespace(config="config.yaml", log_level="info", timeout_seconds=5)

    rc = cli.cmd_telegram_test(args)

    assert rc == 0
    assert "telegram-test: OK" in capsys.readouterr().out


def test_telegram_test_timeout(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = make_git_config(git_repo.clone)
    config = replace(base, telegram=replace(base.telegram, enabled=True))
    monkeypatch.setattr(cli, "_load_config", lambda _path: config)
    monkeypatch.setattr(
        cli,
        "check_telegram_preflight",
        lambda _cfg: (True, "telegram: OK (polling ready)"),
    )
    notifier = SimpleNamespace(
        ask_human=lambda **_kwargs: AskResult(
            answered=False,
            timed_out=True,
            failure="timeout",
        )
    )
    monkeypatch.setattr(cli, "build_notifier", lambda _cfg: notifier)
    args = argparse.Namespace(config="config.yaml", log_level="info", timeout_seconds=5)

    rc = cli.cmd_telegram_test(args)

    assert rc == 1
    assert "telegram-test: FAIL (timeout)" in capsys.readouterr().out


def test_telegram_test_rejects_disabled_config(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_git_config(git_repo.clone)
    monkeypatch.setattr(cli, "_load_config", lambda _path: config)
    args = argparse.Namespace(config="config.yaml", log_level="info", timeout_seconds=5)

    rc = cli.cmd_telegram_test(args)

    assert rc == 1
    assert "telegram.enabled is false" in capsys.readouterr().out

"""The read-only `preflight` CLI command.

Drives ``cmd_preflight`` with in-memory providers so the test is deterministic and free of real
subprocess/CLI concerns (each adapter's real ``preflight()`` is covered by the provider tests). The
focus here is the command's orchestration: health reporting, the isolation verdict, output, and the
exit code.
"""

from __future__ import annotations

import argparse
import logging
import os
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
        paid: IsolationCapabilityReport | None = None,
        auth: AuthProbe | None = _LOGGED_IN,
    ) -> None:
        self.id = provider_id
        self._healthy = healthy
        self._degraded_reasons = degraded_reasons
        self._smoke = smoke
        self._paid = paid
        self._auth = auth
        self.paid_calls = 0

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

    def paid_isolation_probe(self, *, home_dir: object) -> IsolationCapabilityReport | None:
        self.paid_calls += 1
        return self._paid

    def run(self, request: AgentRunRequest) -> object:  # pragma: no cover - never called
        raise NotImplementedError


def _args(*, paid_isolation_probe: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        config="config.yaml", log_level="info", paid_isolation_probe=paid_isolation_probe
    )


def _patch_providers(
    monkeypatch: pytest.MonkeyPatch,
    config: object,
    *,
    gh_result: tuple[bool, str] = (True, "gh: OK"),
    degraded: dict[str, tuple[str, ...]] | None = None,
    smokes: dict[str, IsolationCapabilityReport] | None = None,
    paid: dict[str, IsolationCapabilityReport] | None = None,
    auth: dict[str, AuthProbe | None] | None = None,
    **healthy: bool,
) -> dict[ProviderId, _FakeHealthProvider]:
    monkeypatch.setattr(cli, "_load_config", lambda _path: config)
    degraded = degraded or {}
    smokes = smokes or {}
    paid = paid or {}
    auth = auth or {}
    providers = {
        ProviderId.CLAUDE: _FakeHealthProvider(
            "claude",
            healthy=healthy.get("claude", True),
            degraded_reasons=degraded.get("claude", ()),
            smoke=smokes.get("claude"),
            paid=paid.get("claude"),
            auth=auth.get("claude", _LOGGED_IN),
        ),
        ProviderId.CODEX: _FakeHealthProvider(
            "codex",
            healthy=healthy.get("codex", True),
            degraded_reasons=degraded.get("codex", ()),
            smoke=smokes.get("codex"),
            paid=paid.get("codex"),
            auth=auth.get("codex", _LOGGED_IN),
        ),
    }
    monkeypatch.setattr(cli, "build_providers", lambda _c, *, layout: providers)
    monkeypatch.setattr(cli, "preflight_gh", lambda: gh_result)
    return providers


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


def _with_allowed(config, *entries: str):
    return replace(config, security=replace(config.security, allowed_environment=entries))


def test_preflight_reports_what_each_prefix_pattern_matched_here(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # AC0.3.2b / Т0.3.8. The width of a pattern is host-specific, so this is the only place it can
    # be shown before it is used — including the zero-match case, which is otherwise
    # indistinguishable from one that worked, and the name the secret filter refused.
    for name in [k for k in os.environ if k.startswith(("DOTNET_", "NUGET_"))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOTNET_ROOT", "/usr/share/dotnet")
    monkeypatch.setenv("DOTNET_NOLOGO", "1")
    monkeypatch.setenv("NUGET_PACKAGES", "/repo/.toolcache/nuget")
    monkeypatch.setenv("NUGET_API_KEY", "oy2-secret")
    config = _with_allowed(
        make_git_config(git_repo.clone), "PATH", "DOTNET_*", "NUGET_*", "WASTECH_NO_SUCH_*"
    )
    _patch_providers(monkeypatch, config)
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0  # a pattern report is never a FAIL on its own
    assert "allowed-environment: 3 prefix pattern(s) — 3 name(s) forwarded" in out
    assert "1 dropped as secret-named" in out
    assert "DOTNET_* \u2192 2 name(s) (DOTNET_NOLOGO, DOTNET_ROOT)" in out
    assert "NUGET_* \u2192 1 name(s) (NUGET_PACKAGES)" in out
    assert "1 dropped as secret-named (NUGET_API_KEY)" in out
    assert "WASTECH_NO_SUCH_* \u2192 0 name(s)" in out
    for value in ("/usr/share/dotnet", "oy2-secret", "/repo/.toolcache/nuget"):
        assert value not in out  # names only, exactly as for the assigned half


def test_preflight_is_silent_when_no_entry_is_a_pattern(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # И-5: the fixture's allowlist holds plain names only, so preflight says nothing new.
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "prefix pattern" not in out


def test_preflight_names_assigned_variables_without_their_values(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # Т0.2.8: an operator reading preflight should see which variables every child process receives.
    # NAMES only — the values are already in their config, and printing them would give a secret
    # that landed there against the guide's advice one more surface to leak from (a CI log).
    config = make_git_config(
        git_repo.clone,
        extra_environment={"NUGET_PACKAGES": "/repo/.toolcache/nuget", "DOTNET_NOLOGO": "1"},
    )
    _patch_providers(monkeypatch, config)
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "extra-environment: 2 assigned" in out
    assert "NUGET_PACKAGES" in out and "DOTNET_NOLOGO" in out
    assert "/repo/.toolcache/nuget" not in out


def _with_assigned(config, **assigned: str):
    return replace(config, security=replace(config.security, extra_environment=dict(assigned)))


def test_preflight_excludes_an_in_clone_cache_from_git(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # The recipe this phase ships: a cache inside the clone is writable by a workspace-write node,
    # but its thousands of files would land in the next task's diff and trip a gate that has nothing
    # to do with caches. Excluding it is the orchestrator's job, not the operator's — the rule goes
    # into `.git/info/exclude`, which the agent cannot reach.
    cache = git_repo.clone / ".toolcache" / "nuget"
    config = _with_assigned(make_git_config(git_repo.clone), NUGET_PACKAGES=str(cache))
    _patch_providers(monkeypatch, config)

    assert cli.cmd_preflight(_args()) == 0
    out = capsys.readouterr().out
    assert "assigned-paths: OK — NUGET_PACKAGES points into the clone and git ignores it" in out
    exclude = (git_repo.clone / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/.toolcache/nuget" in exclude

    # Idempotent: a second run adds no second rule (an operator runs preflight repeatedly).
    assert cli.cmd_preflight(_args()) == 0
    capsys.readouterr()
    reread = (git_repo.clone / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert reread.count("/.toolcache/nuget") == 1


def test_preflight_fails_when_the_exclusion_does_not_take(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # A tracked path cannot be ignored, whatever is appended to the exclude file. That is the honest
    # broken case, and it has to FAIL here: discovering it later means discovering it after the
    # agent has already done its expensive work, on a gate that will blame the diff.
    tracked = next(p for p in git_repo.clone.rglob("*") if p.is_file() and ".git" not in p.parts)
    config = _with_assigned(make_git_config(git_repo.clone), NUGET_PACKAGES=str(tracked))
    _patch_providers(monkeypatch, config)

    assert cli.cmd_preflight(_args()) == 1
    out = capsys.readouterr().out
    assert (
        "assigned-paths: FAIL — NUGET_PACKAGES points into the clone but git still does not" in out
    )
    assert "preflight: NOT ready" in out


def test_preflight_skips_the_exclusion_when_the_clone_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # Preflight runs before any task, so the clone may legitimately not exist yet. A missing clone
    # is nothing to exclude, not a failure — but it is said out loud, because the operator otherwise
    # has no way to know the protection is not in place yet.
    absent = tmp_path / "not-cloned-yet"
    config = make_git_config(tmp_path)
    config = replace(config, repo=replace(config.repo, local_path=str(absent)))
    config = _with_assigned(config, NUGET_PACKAGES=str(absent / ".toolcache" / "nuget"))
    _patch_providers(monkeypatch, config)

    assert cli.cmd_preflight(_args()) == 0
    out = capsys.readouterr().out
    assert (
        "assigned-paths: SKIP — NUGET_PACKAGES points into the clone, which is not on disk" in out
    )


def test_preflight_warns_but_does_not_fail_on_a_cache_outside_the_clone(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The path may be perfectly deliberate, so this is not a refusal — but a sandboxed node cannot
    # write there, and the build failure it produces reads like a broken toolchain rather than a
    # misplaced cache.
    outside = tmp_path / "shared-cache"
    config = _with_assigned(make_git_config(git_repo.clone), NUGET_PACKAGES=str(outside))
    _patch_providers(monkeypatch, config)

    assert cli.cmd_preflight(_args()) == 0
    out = capsys.readouterr().out
    assert "assigned-paths: WARN — NUGET_PACKAGES points outside the clone" in out
    assert "can only write inside the clone" in out
    assert "preflight: ready" in out


def test_preflight_fails_on_a_cache_that_reaches_a_protected_path_through_a_link(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # The half only this side of the gate can decide: the value names an innocent path, and the
    # config validator, which never touches the filesystem, cannot see where it leads.
    worc = git_repo.clone / ".worc"
    worc.mkdir(exist_ok=True)
    link = git_repo.clone / "innocent"
    link.symlink_to(worc)
    config = _with_assigned(make_git_config(git_repo.clone), CARGO_HOME=str(link / "cargo"))
    _patch_providers(monkeypatch, config)

    assert cli.cmd_preflight(_args()) == 1
    out = capsys.readouterr().out
    assert "assigned-paths: FAIL — CARGO_HOME resolves onto" in out
    assert "preflight: NOT ready" in out


def test_the_assigned_path_report_never_prints_a_value(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Same rule as the assigned-variable line itself: the operator reads values in their own config,
    # and a value holding a secret against the guide's advice must not gain a terminal or a CI log
    # to leak from. Every line names the variable; a FAIL also names the protected path it hit —
    # orchestrator-owned, and the one thing the operator cannot infer.
    config = _with_assigned(
        make_git_config(git_repo.clone),
        NUGET_PACKAGES=str(tmp_path / "telltale-outside-cache"),
        CARGO_HOME=str(git_repo.clone / ".toolcache" / "telltale-inside-cache"),
    )
    _patch_providers(monkeypatch, config)

    assert cli.cmd_preflight(_args()) == 0
    out = capsys.readouterr().out
    assert "NUGET_PACKAGES" in out and "CARGO_HOME" in out
    assert "telltale-outside-cache" not in out
    assert "telltale-inside-cache" not in out


def test_preflight_is_silent_about_an_empty_extra_environment(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # No key, no line: preflight reports what is in effect, and the empty default is not a state
    # worth a line of the operator's attention.
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "extra-environment" not in out


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
    assert "isolation probe OK" in out
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
    assert "FAIL — isolation probe" in out
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
    assert "WARN — isolation probe" in out
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


# --- The paid Claude isolation probe: a second, explicit opt-in (Пре-1.2) ----------------------


def test_the_paid_probe_is_not_run_without_its_flag(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # It spends a real model call, so nothing may imply it: neither `worc preflight` on its own
    # nor the installer's auto-preflight.
    providers = _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),
        paid={
            "claude": IsolationCapabilityReport(
                ok=True, status="passed", detail="the write was refused", fatal=False
            )
        },
    )
    rc = cli.cmd_preflight(_args())
    assert rc == 0
    assert all(provider.paid_calls == 0 for provider in providers.values())
    assert "isolation probe OK" not in capsys.readouterr().out


def test_the_paid_probe_runs_and_reports_with_its_flag(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    providers = _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),
        paid={
            "claude": IsolationCapabilityReport(
                ok=True,
                status="passed",
                detail="claude paid isolation probe: the gitdir refused the write",
                fatal=False,
            )
        },
    )
    rc = cli.cmd_preflight(_args(paid_isolation_probe=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert providers[ProviderId.CLAUDE].paid_calls == 1
    assert "claude: isolation probe OK — claude paid isolation probe" in out
    assert "preflight: ready" in out


def test_a_paid_probe_leak_is_fatal_despite_a_fallback_provider(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same severity rule as the free smoke: a proven leak is a non-fallback security result.
    _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),
        paid={
            "claude": IsolationCapabilityReport(
                ok=False,
                status="policy-failed",
                detail="a write LANDED in the Git common dir",
                fatal=True,
            )
        },
    )
    rc = cli.cmd_preflight(_args(paid_isolation_probe=True))
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL — isolation probe" in out
    assert "preflight: NOT ready" in out


def test_an_undemonstrated_paid_probe_warns_with_a_fallback(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # "The agent wrote nothing at all" is undemonstrable, not a pass — and with a fallback provider
    # available that degrades to a warning rather than blocking the run.
    _patch_providers(
        monkeypatch,
        make_git_config(git_repo.clone),
        paid={
            "claude": IsolationCapabilityReport(
                ok=False,
                status="unsupported",
                detail="NOT DEMONSTRATED — no file was created at all",
                fatal=False,
            )
        },
    )
    rc = cli.cmd_preflight(_args(paid_isolation_probe=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARN — isolation probe: NOT DEMONSTRATED" in out

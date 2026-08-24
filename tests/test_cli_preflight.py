"""The no-task `preflight` CLI command.

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
from pathlib import Path
from types import SimpleNamespace

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.config.loader import ConfigError
from wastech_orchestrator.install.config_writer import InstallSpec, build_and_validate
from wastech_orchestrator.notify import AskResult
from wastech_orchestrator.observability import logging as obslog
from wastech_orchestrator.providers import claude as claude_mod
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


@pytest.mark.parametrize(
    "command",
    [
        ("run", "task.md"),
        ("watch", "--poll-seconds", "0"),
        ("rerun", "task-1", "--yes", "--non-interactive"),
    ],
    ids=["run", "watch", "rerun"],
)
def test_task_entry_points_reject_a_config_without_path(
    command: tuple[str, ...], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every work-starting CLI path goes through validated config loading."""
    clone = tmp_path / "clone"
    clone.mkdir()
    text = build_and_validate(
        InstallSpec(
            repo_url="git@example.invalid:owner/repo.git",
            repo_local_path=clone,
            base_branch="main",
            providers=(ProviderId.CODEX,),
            create_pull_request=False,
            auto_mode=False,
        )
    )
    config = tmp_path / "config.yaml"
    config.write_text(text.replace("  - PATH\n", "", 1), encoding="utf-8")

    assert cli.main(["--config", str(config), *command]) == 2
    output = capsys.readouterr().out
    assert "security.allowed_environment" in output
    assert "PATH" in output


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
    monkeypatch.setattr(cli, "preflight_gh", lambda _security=None: gh_result)
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


def test_preflight_warns_but_stays_ready_on_a_host_without_a_floor(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A host that cannot enforce the write floor is announced, never refused: refusing would leave
    # the operator without the guarantee AND without the work. The real formatter runs here — only
    # the host classification is substituted — so the line an operator actually reads is the one
    # under test, and the exit code stays 0.
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    monkeypatch.setattr(
        claude_mod, "default_sandbox_probe", lambda: claude_mod.SandboxCapability.NATIVE_WINDOWS
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "isolation-floor: NONE — claude: " in out
    assert ".git" in out and ".worc" in out
    assert "preflight: ready" in out


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
    # The width of a pattern is host-specific, so this is the only place it can
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
    assert "applies to orchestrator git/gh and strict-mode agent children" in out
    assert "1 dropped as secret-named" in out
    assert "DOTNET_* \u2192 2 name(s) (DOTNET_NOLOGO, DOTNET_ROOT)" in out
    assert "NUGET_* \u2192 1 name(s) (NUGET_PACKAGES)" in out
    assert "1 dropped as secret-named (NUGET_API_KEY)" in out
    assert "WASTECH_NO_SUCH_* \u2192 0 name(s)" in out
    for value in ("/usr/share/dotnet", "oy2-secret", "/repo/.toolcache/nuget"):
        assert value not in out  # names only, exactly as for the assigned half


def test_advanced_mode_pattern_report_names_its_git_only_scope(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DOTNET_ROOT", "/usr/share/dotnet")
    config = _mode(_with_allowed(make_git_config(git_repo.clone), "PATH", "DOTNET_*"))
    _patch_providers(monkeypatch, config)

    assert cli.cmd_preflight(_args()) == 0
    out = capsys.readouterr().out
    assert "gates orchestrator git/gh only" in out
    assert "advanced-mode agent/check/tool children receive the parent environment whole" in out


def test_preflight_is_silent_when_no_entry_is_a_pattern(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fixture's allowlist holds plain names only, so preflight says nothing new.
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "prefix pattern" not in out


def test_preflight_names_assigned_variables_without_their_values(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # An operator reading preflight should see which variables every child process receives.
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


def test_preflight_accepts_an_outside_cache_in_advanced_mode_without_false_warning(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outside = tmp_path / "shared-cache"
    config = _mode(_with_assigned(make_git_config(git_repo.clone), NUGET_PACKAGES=str(outside)))
    _patch_providers(monkeypatch, config)

    assert cli.cmd_preflight(_args()) == 0
    out = capsys.readouterr().out
    assert "assigned-paths: WARN — NUGET_PACKAGES" not in out
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


def test_task_launch_repeats_the_canonical_assigned_path_gate(git_repo, make_git_config) -> None:
    worc = git_repo.clone / ".worc"
    worc.mkdir(exist_ok=True)
    link = git_repo.clone / "innocent-launch-path"
    link.symlink_to(worc)
    config = _with_assigned(make_git_config(git_repo.clone), CARGO_HOME=str(link / "cargo"))

    with pytest.raises(ConfigError) as exc:
        cli.require_launch_environment(config, env_file=None, system="Linux")
    assert "security.extra_environment.CARGO_HOME" in str(exc.value)
    assert "control home" in str(exc.value)


def test_task_launch_repeats_the_windows_systemroot_gate(git_repo, make_git_config) -> None:
    config = _without_systemroot(_mode(make_git_config(git_repo.clone)))
    with pytest.raises(ConfigError) as exc:
        cli.require_launch_environment(config, env_file=None, system="Windows")
    assert "SystemRoot" in str(exc.value)
    assert "git/gh" in str(exc.value)


def test_preflight_labels_an_explicit_env_file_accurately(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "operator.env"
    env_file.write_text("A=1\n", encoding="utf-8")
    config = _with_assigned(make_git_config(git_repo.clone), CARGO_HOME=str(env_file))
    _patch_providers(monkeypatch, config)

    ok, lines = cli.run_preflight(config, env_file=env_file)
    assert not ok
    assert any("orchestrator environment file" in line for line in lines)


def test_an_assigned_path_under_a_provider_home_is_ordinary(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    tmp_path: Path,
) -> None:
    # The provider config homes are in no protected set, so a toolchain cache assigned inside one is
    # ordinary working state — no FAIL, no provider-home label. (Outside the clone it still gets the
    # ordinary advisory treatment.)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    config = _with_assigned(make_git_config(git_repo.clone), CARGO_HOME=str(codex_home / "cache"))
    _patch_providers(monkeypatch, config)

    ok, lines = cli.run_preflight(config, env_file=None)
    assert ok
    assert not any("provider's own config or credential home" in line for line in lines)


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


def test_a_sole_provider_on_a_floorless_host_is_warned_about_by_name(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # Making the host verdict advisory was justified by "a node can fall back to the other
    # provider" — a compensation that does not exist with one allowed provider. Under strict
    # isolation the attempt is then refused mid-run, which is why preflight must say more than
    # `ready` here.
    from wastech_orchestrator.providers import claude as claude_mod

    monkeypatch.setattr(
        claude_mod, "default_sandbox_probe", lambda: claude_mod.SandboxCapability.NATIVE_WINDOWS
    )
    config = make_git_config(git_repo.clone)
    config = replace(config, agents=replace(config.agents, allowed=(ProviderId.CLAUDE,)))
    _patch_providers(monkeypatch, config)
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0  # advisory by decision, not a stop
    assert "isolation-floor: WARN" in out
    assert "is the only allowed provider" in out


def test_an_undemonstrable_sandbox_with_no_fallback_fails_under_strict_isolation(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # The strict half of the rule: under strict isolation the floor is the promise, so a sole
    # provider that cannot demonstrate its sandbox stops the run before it starts.
    config = make_git_config(git_repo.clone)
    config = replace(config, agents=replace(config.agents, allowed=(ProviderId.CODEX,)))
    _patch_providers(
        monkeypatch,
        config,
        smokes={
            "codex": IsolationCapabilityReport(
                ok=False, status="unsupported", detail="sandbox could not run here", fatal=False
            )
        },
    )
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL — isolation probe" in out


def test_an_undemonstrable_sandbox_with_no_fallback_only_warns_in_the_advanced_mode(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # Owner decision 2026-08-20: that host class (native Windows without the elevated Codex backend)
    # is exactly the one the mode exists to keep working, and stopping there proved nothing while
    # making the mode unavailable. The line still says the floor is unproven.
    config = make_git_config(git_repo.clone, strict_isolation=False)
    config = replace(config, agents=replace(config.agents, allowed=(ProviderId.CODEX,)))
    _patch_providers(
        monkeypatch,
        config,
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
    assert "the run continues with the floor unproven" in out
    assert "preflight: ready" in out


def test_a_proven_leak_stays_fatal_in_the_advanced_mode(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # The other half of the same decision: an unclassifiable host is a warning, an enforcement
    # FAILURE is not — the mode relaxes what is asked for, never what was proven to break.
    config = make_git_config(git_repo.clone, strict_isolation=False)
    config = replace(config, agents=replace(config.agents, allowed=(ProviderId.CODEX,)))
    _patch_providers(
        monkeypatch,
        config,
        smokes={
            "codex": IsolationCapabilityReport(
                ok=False, status="policy-failed", detail="a denied path was writable", fatal=True
            )
        },
    )
    rc = cli.cmd_preflight(_args())
    assert rc == 1
    assert "FAIL — isolation probe" in capsys.readouterr().out


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


# --- the gh --repo pin verdict (floor 4) --------------------------------------------------------


def test_preflight_reports_the_gh_repo_pin_it_will_use(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # Floor 4 promises that every gh call names its repository outright. Which repository that is
    # was never printed, so an operator could not tell the promise was even in force.
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "gh-repo-pin: OK (repo.url) — every gh call names example.com/o/r outright" in out


def test_an_unpinnable_repository_fails_preflight_when_prs_are_enabled(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # An ssh alias names no OWNER/REPO, so no call can be pinned and the open-PR probe is not asked
    # at all — two floor guarantees off, silently. This configuration opens pull requests, so it is
    # fatal here rather than at publish time, where the cost is a PR against whatever gh inferred.
    config = make_git_config(git_repo.clone)
    config = replace(config, repo=replace(config.repo, url="git@ghwork:o/n.git"))
    _patch_providers(monkeypatch, config)
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "gh-repo-pin: FAIL" in out
    assert "preflight: NOT ready" in out


def test_an_unpinnable_repository_only_warns_when_no_pr_is_ever_opened(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, capsys: pytest.CaptureFixture[str]
) -> None:
    # Nothing is blocked over a capability this configuration never uses — but the report says
    # plainly which promise does not hold, instead of leaving floor 4 reading as unconditional.
    config = make_git_config(git_repo.clone, create_pr=False)
    config = replace(config, repo=replace(config.repo, url="/srv/repos/local.git"))
    _patch_providers(monkeypatch, config)
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "gh-repo-pin: WARN" in out
    assert "floor 4's 'every gh call names its repository outright' does not hold here" in out
    assert "preflight: ready" in out


# --- The paid Claude isolation probe: a second, explicit opt-in --------------------------------


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
                ok=True,
                status="passed",
                detail="claude paid isolation probe: the write was refused",
                fatal=False,
            )
        },
    )
    rc = cli.cmd_preflight(_args())
    assert rc == 0
    assert all(provider.paid_calls == 0 for provider in providers.values())
    # Assert on the paid probe's OWN wording, not on the "isolation probe OK" prefix: that prefix is
    # shared with the free smoke, so with a smoke configured it would be present for another reason
    # and this negative assert would pass by accident.
    assert "paid isolation probe" not in capsys.readouterr().out


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


# --- the advanced mode's loud line and the pinned executables ------------------------------------


def _mode(config):
    """The same config with `strict_isolation: false` — i.e. advanced mode on."""
    return replace(config, security=replace(config.security, strict_isolation=False))


def test_advanced_mode_is_announced_in_one_line(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The mode is never silent, and never a recital either.

    Printing six relaxation axes and all four floor levels here would be long enough that the rest
    of the preflight scrolls past it, which is how a loud line stops being read. That text lives in
    `guide/config/security.md`; what has to survive here is that the mode is stated at all, that it
    names the key that caused it, and that it points at where the floor is written down. The
    absence assertions are the point of the test, not decoration: they are what stops the recital
    growing back one axis at a time.
    """
    _patch_providers(monkeypatch, _mode(make_git_config(git_repo.clone)))
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert rc == 0  # the operator chose this; reporting it is not refusing it
    assert "advanced-mode: ON (security.strict_isolation=false)" in out
    assert "guide/config/security.md" in out
    # The recital is gone: one line per axis, per floor level, and the pin block.
    for level in ("floor 1 of 4", "floor 2 of 4", "floor 3 of 4", "floor 4 of 4"):
        assert level not in out
    for axis in (
        "forwarded WHOLE",
        "no longer gated by an allowlist",
        "EVERY node gets a shell",
        "EVERY node reaches the whole network",
        "three surfaces",
        "directory on PATH",
        "denyWithinAllow",
    ):
        assert axis not in out
    assert "pinned-executables" not in out
    assert "preflight: ready" in out


def test_read_isolation_off_is_announced_in_one_line(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The relaxation is stated with the key that caused it, and nothing more.

    Two keys can produce it, and which one did is the only part an operator cannot re-derive from
    their own config — so that is the part the line carries. What read-isolation off does and does
    not open is in the guide, not in this line.
    """
    _patch_providers(monkeypatch, _mode(make_git_config(git_repo.clone)))
    cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert "read-isolation: OFF (strict_isolation=false)" in out
    assert "native project-instruction" not in out
    assert "denied_read_paths blacklist" not in out


def test_the_mode_line_is_absent_under_strict_isolation(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The counterweight: a default config's report gains nothing. A floor recital on every run would
    # be noise, and noise is how a loud line stops being read.
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert "advanced-mode" not in out
    assert "floor 1 of 4" not in out
    assert "pinned-executables" not in out


def test_the_mode_warns_when_the_claude_env_scrub_variable_would_shrink_the_write_grant(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One variable in the operator's shell silently narrows the grant this report announces.

    In the CLI's env-scrub branch the settings compiler filters a volume-wide ``allowWrite`` out by
    name, and in this mode the parent environment reaches the agent whole — so it takes no config
    change to arrive. A warning rather than a failure: a narrower write grant is a correctness
    surprise (the toolchain cache stops being writable and the build looks broken), not a hole.
    """
    monkeypatch.setenv("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", "1")
    _patch_providers(monkeypatch, _mode(make_git_config(git_repo.clone)))
    assert cli.cmd_preflight(_args()) == 0
    out = capsys.readouterr().out
    assert "write-grant: WARN — CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is set" in out
    assert "preflight: ready" in out


def test_no_write_grant_warning_without_that_variable_or_without_the_mode(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Both halves of the condition, so neither can rot: unset in the mode says nothing, and set
    # outside the mode says nothing either (there is no volume-wide grant to shrink).
    monkeypatch.delenv("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", raising=False)
    _patch_providers(monkeypatch, _mode(make_git_config(git_repo.clone)))
    cli.cmd_preflight(_args())
    assert "write-grant: WARN" not in capsys.readouterr().out

    monkeypatch.setenv("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", "1")
    _patch_providers(monkeypatch, make_git_config(git_repo.clone))
    cli.cmd_preflight(_args())
    assert "write-grant: WARN" not in capsys.readouterr().out


def test_the_windows_launch_gate_also_protects_git_in_advanced_mode(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Agent children are wide in the mode, but orchestrator-owned git/gh keep the allowlist.

    The launch-critical gate therefore remains a FAIL at both strict-isolation values, with a
    provider-neutral message that names git/gh and retains the observed Node CLI detail as evidence.
    """
    config = _without_systemroot(_mode(make_git_config(git_repo.clone)))
    _patch_providers(monkeypatch, config)
    monkeypatch.setattr(env_mod.platform, "system", lambda: "Windows")
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert "allowed-environment: FAIL" in out
    assert "orchestrator-owned git/gh" in out
    assert rc == 1
    # The strict branch is governed by the same check.
    _patch_providers(monkeypatch, _without_systemroot(make_git_config(git_repo.clone)))
    assert cli.cmd_preflight(_args()) == 1
    assert "allowed-environment: FAIL" in capsys.readouterr().out


# --- provider-binary diagnostic lines ------------------------------------------------------------


def _which_table(table: dict[str, str]):
    return table.get


def test_provider_binary_line_says_the_binary_lies_inside_the_config_home(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, tmp_path: Path
) -> None:
    # The standalone-package layout: the launch path is a symlink whose real file lives INSIDE
    # `$CODEX_HOME` — the one fact that explains why the same build behaves differently on two
    # hosts, and it must not require reading a failed attempt's stderr.
    codex_home = tmp_path / "codex-home"
    real = codex_home / "packages" / "bin" / "codex"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher = tmp_path / "bin" / "codex"
    launcher.parent.mkdir(parents=True)
    try:
        launcher.symlink_to(real)
    except (OSError, NotImplementedError):  # pragma: no cover - host cannot create symlinks
        pytest.skip("host cannot create a symlink fixture")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    config = make_git_config(git_repo.clone)
    lines = cli._provider_binary_lines(config, which=_which_table({"codex": str(launcher)}))
    codex_line = next(line for line in lines if line.startswith("codex-binary:"))
    assert str(launcher) in codex_line
    assert str(real.resolve()) in codex_line
    assert "inside the provider's config home" in codex_line


def test_provider_binary_line_says_the_binary_lies_outside_the_config_home(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    binary = tmp_path / "opt" / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    config = make_git_config(git_repo.clone)
    lines = cli._provider_binary_lines(config, which=_which_table({"claude": str(binary)}))
    claude_line = next(line for line in lines if line.startswith("claude-binary:"))
    assert "outside the provider's config home" in claude_line
    # A direct path (no symlink) carries no "file it runs" clause — nothing was resolved away.
    assert "the file it runs is" not in claude_line


def test_provider_binary_line_when_the_command_does_not_resolve(git_repo, make_git_config) -> None:
    config = make_git_config(git_repo.clone)
    lines = cli._provider_binary_lines(config, which=_which_table({}))
    assert any("does not resolve on PATH" in line for line in lines)
    # Diagnostic only — no line is a verdict, and nothing here can fail preflight.
    assert not any("FAIL" in line for line in lines)


def test_provider_binary_line_survives_an_unresolvable_config_home(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, tmp_path: Path
) -> None:
    def _raiser() -> Path:
        raise RuntimeError("no home directory")

    monkeypatch.setattr(cli, "claude_config_home", _raiser)
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    config = make_git_config(git_repo.clone)
    lines = cli._provider_binary_lines(config, which=_which_table({"claude": str(binary)}))
    claude_line = next(line for line in lines if line.startswith("claude-binary:"))
    assert "config home could not be resolved" in claude_line


def test_provider_binary_lines_are_one_per_configured_provider(git_repo, make_git_config) -> None:
    config = make_git_config(git_repo.clone)
    lines = cli._provider_binary_lines(config, which=_which_table({}))
    assert len(lines) == len(config.agents.providers)


def test_preflight_prints_a_binary_line_per_provider(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Integration: `worc preflight` carries the lines, one per configured provider, and the verdict
    # is untouched by them. Only the subject prefixes are asserted — the host's real PATH decides
    # the rest, and the line must be true on every CI host.
    config = make_git_config(git_repo.clone)
    _patch_providers(monkeypatch, config)
    rc = cli.cmd_preflight(_args())
    out = capsys.readouterr().out
    assert out.count("codex-binary:") == 1
    assert out.count("claude-binary:") == 1
    assert rc == 0

"""Unit tests for the Codex permission-profile canary classifier.

Deterministic: an injected runner returns scripted ``(rc, output)`` per probe (in the fixed probe
order), so no real ``codex sandbox`` is launched. The real-host proof lives in
test_codex_canary_smoke.py.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from wastech_orchestrator.providers.base import ErrorClass
from wastech_orchestrator.providers.codex_canary import (
    CAPABILITY_PASSED,
    CAPABILITY_POLICY_FAILED,
    CAPABILITY_UNSUPPORTED,
    WRITE_GUARD_SENTINEL,
    ExtraProbes,
    build_canary_command,
    build_canary_probes,
    run_codex_canary,
    run_codex_capability_smoke,
    write_guard_probe_paths,
)
from wastech_orchestrator.runtime_layout import ProviderWriteGuardPolicy

# Probe order: private-read, private-shell-read, exchange-read, exchange-write, cli-exec.
_ALL_DENY = [(1, "operation not permitted"), (1, "operation not permitted")]
_EXCHANGE_OK = [(0, "task contents"), (1, "operation not permitted")]
_EXEC_OK = [(0, "codex-cli 0.0.0")]


def _seq_runner(results: list[tuple[int, str]]):
    calls = iter(results)

    def _run(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        return next(calls)

    return _run


def _run(runner, *, exchange: str | None = "/clone/.worc-io/t/task.md"):
    return run_codex_canary(
        command="codex",
        profile_arg="permissions.worc={ }",
        working_directory="/clone",
        private_probe="/clone/.worc/logs/req.json",
        exchange_probe=exchange,
        env={},
        system="Linux",
        runner=runner,
    )


def test_canary_passes_when_denies_hold_and_exchange_read_only() -> None:
    outcome = _run(_seq_runner(_ALL_DENY + _EXCHANGE_OK + _EXEC_OK))
    assert outcome.ok
    assert outcome.error_class is None
    assert len(outcome.evidence) == 5


def test_canary_without_positive_control_is_capability_unavailable() -> None:
    # With no exchange probe (and no repo probe), only deny probes and the exec probe run. A
    # successful exec is NOT a positive control — it proves the exec capability, not the read
    # harness's selectivity — so a broken read harness would still make every denial look enforced.
    # The canary must refuse to certify (CAPABILITY_UNAVAILABLE), never silently pass.
    outcome = _run(_seq_runner(_ALL_DENY + _EXEC_OK), exchange=None)
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CAPABILITY_UNAVAILABLE
    assert len(outcome.evidence) == 3


def test_repo_read_positive_control_satisfies_selective_enforcement() -> None:
    # A repo-read positive control (rc=0) is enough to prove selective enforcement even without
    # an exchange probe. Probe order: private-read, private-shell-read, repo-read, cli-exec.
    outcome = run_codex_canary(
        command="codex",
        profile_arg="permissions.worc={ }",
        working_directory="/clone",
        private_probe="/clone/.worc/logs/req.json",
        exchange_probe=None,
        extra=ExtraProbes(repo_probe="/clone/src/main.py"),
        env={},
        system="Linux",
        runner=_seq_runner([(1, "denied"), (1, "denied"), (0, "print(1)"), *_EXEC_OK]),
    )
    assert outcome.ok
    assert {e["probe"] for e in outcome.evidence} == {
        "private-read-denied",
        "private-shell-read-denied",
        "repo-read-allowed",
        "cli-exec-allowed",
    }


def test_alias_read_that_succeeds_is_a_leak() -> None:
    # A workspace symlink resolving to the private file that READS successfully is a leak.
    outcome = run_codex_canary(
        command="codex",
        profile_arg="permissions.worc={ }",
        working_directory="/clone",
        private_probe="/clone/.worc/logs/req.json",
        exchange_probe=None,
        extra=ExtraProbes(alias_probe="/clone/src/alias", repo_probe="/clone/src/main.py"),
        env={},
        system="Linux",
        # private-read(deny), private-shell(deny), alias-read(LEAK rc=0)
        runner=_seq_runner([(1, "denied"), (1, "denied"), (0, "PRIVATE_SECRET")]),
    )
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CONFIGURATION_ERROR
    assert "private-alias-read-denied" in outcome.message


def test_denied_read_that_succeeds_is_security_configuration_error() -> None:
    # The private read was expected denied but SUCCEEDED — a real leak, non-fallback.
    outcome = _run(_seq_runner([(0, "SECRET LEAKED")]))
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CONFIGURATION_ERROR
    assert "not enforcing" in outcome.message


@pytest.mark.parametrize(
    ("message", "safe_detail"),
    [
        ("windows: refusing to run unsandboxed", "refusing to run unsandboxed"),
        (
            "Restricted read-only access requires the elevated Windows sandbox backend",
            "restricted read-only access requires the elevated windows sandbox backend",
        ),
        ("helper copy failed", "helper copy failed"),
    ],
)
def test_capability_marker_is_capability_unavailable(message: str, safe_detail: str) -> None:
    outcome = _run(_seq_runner([(1, message)]))
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CAPABILITY_UNAVAILABLE
    assert safe_detail in outcome.message


def test_allowed_read_blocked_is_capability_unavailable() -> None:
    # denies hold, but the exchange read (a required read) is blocked → policy undemonstrable.
    outcome = _run(_seq_runner([*_ALL_DENY, (1, "operation not permitted"), (1, "denied")]))
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CAPABILITY_UNAVAILABLE


def test_runner_timeout_is_capability_unavailable() -> None:
    def _boom(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    outcome = _run(_boom)
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CAPABILITY_UNAVAILABLE


def test_evidence_records_paths_not_contents() -> None:
    outcome = _run(_seq_runner(_ALL_DENY + _EXCHANGE_OK + _EXEC_OK))
    for entry in outcome.evidence:
        assert set(entry) == {"probe", "expect_denied", "denied"}
        assert "SECRET" not in str(entry)


def test_a_refused_cli_exec_is_a_configuration_error() -> None:
    # Ам-5 Т5.7: the profile refusing to execute the provider's own binary is OUR break, not a host
    # gap — apply_patch re-execs that binary as its fs sandbox helper, so every patch would fail
    # the same way. Non-fallback, so the router cannot mask it by falling over to Claude.
    outcome = _run(_seq_runner(_ALL_DENY + _EXCHANGE_OK + [(1, "operation not permitted")]))
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CONFIGURATION_ERROR
    assert "cli-exec-allowed" in outcome.message


def test_a_refused_cli_exec_with_sandbox_prose_is_still_a_configuration_error() -> None:
    # The trap this probe exists to close: on macOS the refused exec's own stderr carries
    # seatbelt/sandbox wording, which the capability-marker scan would otherwise reroute into
    # CAPABILITY_UNAVAILABLE — the router falls back, and the break is masked as "Codex is
    # unavailable today" exactly as it was for five runs.
    refusal = (
        "failed to start sandbox: seatbelt deny ... "
        "sandbox-exec: execvp() of '/x/codex' failed: Operation not permitted"
    )
    outcome = _run(_seq_runner(_ALL_DENY + _EXCHANGE_OK + [(1, refusal)]))
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CONFIGURATION_ERROR
    assert "cli-exec-allowed" in outcome.message


def test_an_unrunnable_cli_exec_probe_stays_a_capability_gap() -> None:
    # The one denial that is NOT escalated: the runner's own marker means `codex sandbox` itself
    # failed to launch or timed out — a host verdict, not a profile one.
    outcome = _run(_seq_runner(_ALL_DENY + _EXCHANGE_OK + [(127, "canary-sandbox-unrunnable")]))
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CAPABILITY_UNAVAILABLE


def test_the_exec_probe_command_is_identical_on_every_platform() -> None:
    # The exec probe is an argv pair with no shell and no cmd wrapper, so the platform seam that
    # rewrites read/write probes must leave it alone.
    for system in ("Linux", "Windows", "Darwin"):
        probes = build_canary_probes(
            private_probe="/p", exchange_probe=None, system=system, exec_probe="/abs/codex"
        )
        exec_probe = next(p for p in probes if p.label == "cli-exec-allowed")
        assert exec_probe.command == ["/abs/codex", "--version"], system


def test_build_canary_command_is_no_model_sandbox_invocation() -> None:
    probes = build_canary_probes(private_probe="/p", exchange_probe=None, system="Linux")
    argv = build_canary_command("codex", "permissions.worc={ }", "/clone", probes[0])
    assert argv[:2] == ["codex", "sandbox"]
    assert "-P" in argv and argv[argv.index("-P") + 1] == "worc"
    assert argv[argv.index("-C") + 1] == "/clone"
    assert "exec" not in argv  # never the model subcommand
    assert argv[argv.index("--") + 1 :] == ["/bin/cat", "/p"]


def test_windows_probes_use_cmd_type() -> None:
    probes = build_canary_probes(
        private_probe="C:\\clone\\.worc", exchange_probe=None, system="Windows"
    )
    assert probes[0].command == ["cmd", "/c", "type", "C:\\clone\\.worc"]


def test_windows_probes_keep_a_spaced_path_as_its_own_argv_member() -> None:
    """A path with a space must never be pre-formatted into the `cmd /c` command-line string.

    `cmd /c` re-parses a one-string command line and splits on spaces, so `C:\\First Last\\x` made
    the probe fail as a malformed command. On an `expect_denied=True` probe that is
    indistinguishable from enforcement, so a broken probe would mask a real leak.
    """
    spaced = "C:\\Users\\First Last\\clone\\.worc\\logs\\req.json"
    probes = build_canary_probes(
        private_probe=spaced,
        exchange_probe=spaced,
        system="Windows",
        repo_write_probe=spaced,
    )
    for probe in probes:
        assert spaced in probe.command, probe.label
        # the path is a discrete member, never concatenated into a larger token
        assert not any(spaced in tok and tok != spaced for tok in probe.command), probe.label


def test_windows_probes_normalize_a_posix_shaped_path() -> None:
    """A POSIX-shaped path must reach `cmd` with native separators.

    The exchange probe is `AgentRunRequest.task_path`, which stays POSIX-shaped on Windows. `cmd`
    resolves neither `type C:/a/b` nor `echo x >> C:/a/b` — it reports the file as missing — so an
    unnormalized path fails the read probe as a capability gap and lets the paired write probe
    "pass" while proving nothing.
    """
    posix = "C:/clone/.worc-io/task-1/task.md"
    native = "C:\\clone\\.worc-io\\task-1\\task.md"
    probes = build_canary_probes(
        private_probe="C:\\clone\\.worc",
        exchange_probe=posix,
        system="Windows",
        repo_write_probe=posix,
    )
    for probe in probes:
        assert posix not in probe.command, probe.label
    read = next(p for p in probes if p.label == "exchange-read-allowed")
    assert read.command == ["cmd", "/c", "type", native]
    exchange_write = next(p for p in probes if p.label == "exchange-write-denied")
    assert exchange_write.command == ["cmd", "/c", "echo", "x", ">>", native]


def test_posix_probes_leave_the_path_untouched() -> None:
    probes = build_canary_probes(
        private_probe="/clone/.worc",
        exchange_probe="/clone/.worc-io/task-1/task.md",
        system="Linux",
    )
    read = next(p for p in probes if p.label == "exchange-read-allowed")
    assert read.command == ["/bin/cat", "/clone/.worc-io/task-1/task.md"]


def test_windows_write_probe_redirects_with_a_bare_operator() -> None:
    probes = build_canary_probes(
        private_probe="C:\\clone\\.worc",
        exchange_probe=None,
        system="Windows",
        repo_write_probe="C:\\clone\\src\\main.py",
    )
    write = next(p for p in probes if p.label.startswith("repo-write"))
    assert write.command == ["cmd", "/c", "echo", "x", ">>", "C:\\clone\\src\\main.py"]


def test_private_reads_are_expected_denied_with_no_read_isolation_knob() -> None:
    """The canary asserts the private deny unconditionally — direct, shell-mediated and via alias.

    It used to flip these three to *allowed* whenever read-isolation was off, mirroring a profile
    that downgraded the private set to ``read``. Both halves are gone, so the probe set no longer
    has a configuration in which the orchestrator's own private home is expected to be readable, and
    the canary now proves that deny on every run rather than on the non-default half of them.
    """
    probes = {
        p.label: p.expect_denied
        for p in build_canary_probes(
            private_probe="/p", exchange_probe=None, system="Linux", alias_probe="/repo/alias"
        )
    }
    assert probes["private-read-denied"] is True
    assert probes["private-shell-read-denied"] is True
    assert probes["private-alias-read-denied"] is True
    assert not [label for label in probes if label.endswith("-read-allowed")]


# --- the no-model capability smoke (deterministic; scripted sandbox + inventory) -----------------


def _smoke_runner(*, writable: bool, write_guard_holds: bool = True):
    """A fake ``codex sandbox`` runner keyed on the probe path (order-independent, so it is robust
    whether or not the alias fixture could be created on the host).

    ``write_guard_holds`` models the profile's Git-control carve-outs: with it False a write into a
    write-denied root lands, which is what a profile that never applied its deny rules looks like.
    """

    def _run(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        probe = " ".join(argv[argv.index("--") + 1 :])
        is_write = ">>" in probe
        if probe.endswith("--version"):  # the cli-exec probe: the binary executes under the profile
            return (0, "codex-cli 0.0.0")
        if WRITE_GUARD_SENTINEL in probe:  # a Git-control / lifecycle root write-deny probe
            return (1, "operation not permitted") if write_guard_holds else (0, "ok")
        if ".worc-io" in probe:  # exchange: readable, not writable (check before ``.worc``)
            return (1, "operation not permitted") if is_write else (0, "task")
        if ".worc" in probe or "alias_to_private" in probe:  # private direct / shell / alias
            return (1, "operation not permitted")
        if is_write:  # repo write: allowed only for workspace-write
            return (0, "ok") if writable else (1, "operation not permitted")
        return (0, "print(1)")  # repo read: the positive control, always allowed

    return _run


def _empty_inventory(command: str, env: Mapping[str, str]) -> tuple[bool, str]:
    return True, "No MCP servers configured yet."


@pytest.mark.parametrize(
    ("system", "sandbox_uses_operator_home"),
    [("Windows", True), ("Linux", False), ("Darwin", False)],
)
def test_capability_smoke_uses_platform_appropriate_codex_home(
    tmp_path: Path, system: str, sandbox_uses_operator_home: bool
) -> None:
    calls: list[tuple[list[str], str, dict[str, str]]] = []
    sandbox_runner = _smoke_runner(writable=True)

    def _recording_runner(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        calls.append((argv, cwd, dict(env)))
        if argv[1:3] == ["mcp", "list"]:
            return 0, "No MCP servers configured yet."
        return sandbox_runner(argv, cwd, env)

    operator_env = {"CODEX_HOME": "operator-codex-home", "UNCHANGED": "yes"}
    report = run_codex_capability_smoke(
        command="codex",
        home_dir=tmp_path,
        env=operator_env,
        permission_profile="workspace-write",
        system=system,
        runner=_recording_runner,
    )

    assert report.status == CAPABILITY_PASSED
    sandbox_calls = [call for call in calls if call[0][1] == "sandbox"]
    assert sandbox_calls
    for _, _, probe_env in sandbox_calls:
        assert probe_env["UNCHANGED"] == "yes"
        if sandbox_uses_operator_home:
            assert probe_env == operator_env
        else:
            assert probe_env["CODEX_HOME"] != operator_env["CODEX_HOME"]
            assert Path(probe_env["CODEX_HOME"]).name.startswith("worc-codexhome-")

    inventory_calls = [call for call in calls if call[0][1:3] == ["mcp", "list"]]
    assert len(inventory_calls) == 1
    _, inventory_cwd, inventory_env = inventory_calls[0]
    assert inventory_env["UNCHANGED"] == "yes"
    assert inventory_env["CODEX_HOME"] == inventory_cwd
    assert inventory_env["CODEX_HOME"] != operator_env["CODEX_HOME"]
    assert Path(inventory_env["CODEX_HOME"]).name.startswith("worc-mcp-home-")


def test_capability_smoke_workspace_write_passes(tmp_path: Path) -> None:
    report = run_codex_capability_smoke(
        command="codex",
        home_dir=tmp_path,
        env={},
        permission_profile="workspace-write",
        system="Linux",
        runner=_smoke_runner(writable=True),
        inventory_probe=_empty_inventory,
    )
    assert report.status == CAPABILITY_PASSED
    assert report.ok
    assert "empty MCP inventory" in report.detail
    labels = {e["probe"] for e in report.evidence}
    assert {"private-read-denied", "repo-read-allowed", "mcp-inventory"} <= labels


def test_capability_smoke_proves_the_profile_the_advanced_mode_will_actually_launch(
    tmp_path: Path,
) -> None:
    """ТA.9.2: the smoke must not quietly prove a stricter profile than the one that runs.

    It generates its own profile rather than receiving one, so the operator's ``strict_isolation``
    has to reach it — otherwise in the advanced mode this check certifies a floor nobody runs under,
    which is the exact failure the requirement exists to prevent. Asserted on the argv the runner
    receives (the profile is rendered into it), because the report carries probe verdicts and not
    the profile. The real-CLI counterpart lives in ``test_codex_canary_smoke.py``.
    """
    seen: list[list[str]] = []
    sandbox_runner = _smoke_runner(writable=True)

    def _recording(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        seen.append(argv)
        return sandbox_runner(argv, cwd, env)

    report = run_codex_capability_smoke(
        command="codex",
        home_dir=tmp_path,
        env={},
        permission_profile="workspace-write",
        strict_isolation=False,
        system="Linux",
        runner=_recording,
        inventory_probe=_empty_inventory,
    )
    assert report.status == CAPABILITY_PASSED  # the carve-outs still hold under the wide grant
    profiles = {token for argv in seen for token in argv if token.startswith("permissions.worc=")}
    assert profiles, seen
    assert all('"/" = "write"' in profile for profile in profiles)
    assert all('"network" = { "enabled" = true }' in profile for profile in profiles)


def test_capability_smoke_probes_the_git_control_roots_it_created(tmp_path: Path) -> None:
    # Пре-1.2: the fixture stands up real `.git`, hooks and `tasks/` targets, so the smoke actually
    # demonstrates the floor instead of inferring it from writes that failed for want of a parent.
    report = run_codex_capability_smoke(
        command="codex",
        home_dir=tmp_path,
        env={},
        permission_profile="workspace-write",
        system="Linux",
        runner=_smoke_runner(writable=True),
        inventory_probe=_empty_inventory,
    )
    assert report.status == CAPABILITY_PASSED
    write_guard_probes = [e for e in report.evidence if str(e["probe"]).startswith("write-guard-")]
    assert len(write_guard_probes) == 3  # exchange root, `.git` (== common dir), `tasks/`
    assert all(e["denied"] for e in write_guard_probes)


def test_capability_smoke_fails_when_a_git_control_write_lands(tmp_path: Path) -> None:
    # The same fixture with a profile whose deny rules are not in force: the write into `.git` goes
    # through and the smoke reports a policy failure (a non-fallback CONFIGURATION_ERROR upstream),
    # which is the case that used to have no probe at all on either provider.
    report = run_codex_capability_smoke(
        command="codex",
        home_dir=tmp_path,
        env={},
        permission_profile="workspace-write",
        system="Linux",
        runner=_smoke_runner(writable=True, write_guard_holds=False),
        inventory_probe=_empty_inventory,
    )
    assert report.status == CAPABILITY_POLICY_FAILED
    assert "write-guard" in report.detail


def test_capability_smoke_read_only_denies_repo_write(tmp_path: Path) -> None:
    report = run_codex_capability_smoke(
        command="codex",
        home_dir=tmp_path,
        env={},
        permission_profile="read-only",
        system="Linux",
        runner=_smoke_runner(writable=False),
        inventory_probe=_empty_inventory,
    )
    assert report.status == CAPABILITY_PASSED


def test_capability_smoke_reports_policy_leak(tmp_path: Path) -> None:
    # A runner that lets the private read succeed → a leak → policy-failed (CONFIGURATION_ERROR).
    def _leaky(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        return 0, "PRIVATE_SECRET"

    report = run_codex_capability_smoke(
        command="codex",
        home_dir=tmp_path,
        env={},
        system="Linux",
        runner=_leaky,
        inventory_probe=_empty_inventory,
    )
    assert report.status == CAPABILITY_POLICY_FAILED


def test_capability_smoke_reports_a_profile_that_blocks_the_cli_exec(tmp_path: Path) -> None:
    # Ам-5 Т5.7 live-probe #2's deterministic half: `worc preflight` (which runs this smoke) now
    # demonstrates the exec capability without a model call, and a profile that blocks it reports
    # as policy-failed — never as an unsupported host.
    healthy = _smoke_runner(writable=True)

    def _exec_refused(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        probe = " ".join(argv[argv.index("--") + 1 :])
        if probe.endswith("--version"):
            return 1, "sandbox-exec: execvp() of 'codex' failed: Operation not permitted"
        return healthy(argv, cwd, env)

    report = run_codex_capability_smoke(
        command="codex",
        home_dir=tmp_path,
        env={},
        permission_profile="workspace-write",
        system="Linux",
        runner=_exec_refused,
        inventory_probe=_empty_inventory,
    )
    assert report.status == CAPABILITY_POLICY_FAILED
    assert "cli-exec" in report.detail


def test_capability_smoke_unsupported_when_sandbox_cannot_run(tmp_path: Path) -> None:
    def _unrunnable(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        return 1, "refusing to run unsandboxed"

    report = run_codex_capability_smoke(
        command="codex",
        home_dir=tmp_path,
        env={},
        system="Linux",
        runner=_unrunnable,
        inventory_probe=_empty_inventory,
    )
    assert report.status == CAPABILITY_UNSUPPORTED


# --- write-guard probes (Пре-1 / AC1.1–AC1.4) -------------------------------------------------


def _write_guard(tmp_path: Path, *, linked_worktree: bool = False) -> ProviderWriteGuardPolicy:
    """A policy over directories that really exist, in the two shapes production produces."""
    repo = tmp_path / "repo"
    common = repo / ".git"
    (common / "hooks").mkdir(parents=True)
    (repo / "tasks").mkdir(parents=True)
    (repo / ".worc-io").mkdir(parents=True)
    git_dir = common / "worktrees" / "wt" if linked_worktree else common
    git_dir.mkdir(parents=True, exist_ok=True)
    return ProviderWriteGuardPolicy(
        exchange_root=repo / ".worc-io",
        git_dir=git_dir,
        git_common_dir=common,
        hooks_dir=common / "hooks",
        tasks_dir=repo / "tasks",
    )


def test_every_declared_write_deny_root_is_probed_or_accounted_for(tmp_path: Path) -> None:
    # AC1.1: `.git` immutability is the product's central claim and no probe tested it. Each
    # declared root now either gets its own probe or is explicitly collapsed into a probed ancestor
    # — the one thing it may never be is silently absent.
    guard = _write_guard(tmp_path)
    targets = write_guard_probe_paths(guard.denied_write_paths)
    probed = {label for label, _ in targets.probes}
    assert len(probed) == 3  # exchange root, .git (gitdir == common here), tasks/
    # hooks/ lives inside the probed .git, so it is covered rather than probed again — and the
    # coverage is recorded with the ancestor that provides it.
    assert [root.endswith("hooks") for root, _ in targets.covered] == [True]
    assert targets.covered[0][1].endswith(".git")
    assert targets.missing == ()
    # Every root from the policy is accounted for exactly once.
    accounted = len(targets.probes) + len(targets.covered) + len(targets.missing)
    assert accounted == len(guard.denied_write_paths)


def test_a_linked_worktree_gets_distinguishable_gitdir_and_common_dir_probes(
    tmp_path: Path,
) -> None:
    # AC1.2: a linked worktree's per-worktree gitdir and shared common dir are different
    # directories, and the Bash sandbox has a built-in linked-worktree `.git` write allowance to
    # override — so one probe covering "the .git" would pass while the other root stayed open.
    guard = _write_guard(tmp_path, linked_worktree=True)
    targets = write_guard_probe_paths(guard.denied_write_paths)
    labels = [label for label, _ in targets.probes]
    paths = [path for _, path in targets.probes]
    assert len(set(labels)) == len(labels)  # no two probes share a label
    assert any("worktrees-wt" in label for label in labels)
    assert any(path.endswith(f"wt/{WRITE_GUARD_SENTINEL}") for path in paths)
    assert any(str(guard.git_common_dir / WRITE_GUARD_SENTINEL) == path for path in paths)


def test_a_missing_root_is_reported_rather_than_probed(tmp_path: Path) -> None:
    # AC1.4: writing into a directory that does not exist fails for want of a parent, and that
    # failure is indistinguishable from an enforced deny. A root with no directory therefore yields
    # no probe at all — it is named as undemonstrable instead of quietly certified.
    guard = _write_guard(tmp_path)
    absent = ProviderWriteGuardPolicy(
        exchange_root=guard.exchange_root,
        git_dir=guard.git_dir,
        git_common_dir=guard.git_common_dir,
        hooks_dir=guard.hooks_dir,
        tasks_dir=tmp_path / "repo" / "no-such-lifecycle-dir",
    )
    targets = write_guard_probe_paths(absent.denied_write_paths)
    assert targets.missing == ((tmp_path / "repo" / "no-such-lifecycle-dir").as_posix(),)
    assert not any("no-such-lifecycle-dir" in path for _, path in targets.probes)


def test_a_write_guard_probe_that_succeeds_is_a_leak_and_its_file_is_removed(
    tmp_path: Path,
) -> None:
    # П1.3: an unexpected pass fails the attempt closed before the model — and the file the probe
    # created inside the operator's `.git` is the orchestrator's litter, so it is removed.
    guard = _write_guard(tmp_path)
    targets = write_guard_probe_paths(guard.denied_write_paths)

    def _writes_land(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        # The private reads stay denied and the positive control reads fine; the writes all land,
        # which is what a profile whose deny rules never applied looks like. The file is created
        # where the probe actually points, so cleanup is judged against the real path.
        joined = " ".join(argv)
        if joined.endswith("--version"):  # the cli-exec probe executes fine here
            return 0, "codex-cli 0.0.0"
        if "printf" in joined:
            target = Path(shlex.split(argv[-1])[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
            return 0, ""
        if "main.py" in joined:
            return 0, "print(1)"
        return 1, "operation not permitted"

    outcome = run_codex_canary(
        command="codex",
        profile_arg="permissions.worc={ }",
        working_directory="/clone",
        private_probe="/clone/.worc/logs/req.json",
        exchange_probe=None,
        extra=ExtraProbes(repo_probe="/clone/src/main.py", write_guard_probes=targets.probes),
        env={},
        system="Linux",
        runner=_writes_land,
    )
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CONFIGURATION_ERROR
    assert "write-guard" in outcome.message
    assert "removed the file the probe created" in outcome.message
    # Nothing the probes created is left behind, in any of the declared roots.
    assert not any(Path(path).exists() for _, path in targets.probes)


def test_the_probe_count_grows_with_the_write_guard_roots(tmp_path: Path) -> None:
    # The count is pinned so growth cannot pass unnoticed: five probes without a write guard
    # (private read + shell read + exchange read/write + cli exec), and one more per probed root.
    guard = _write_guard(tmp_path)
    targets = write_guard_probe_paths(guard.denied_write_paths)
    base = build_canary_probes(
        private_probe="/p", exchange_probe="/x", system="Linux", exec_probe="/bin/codex"
    )
    with_guard = build_canary_probes(
        private_probe="/p",
        exchange_probe="/x",
        system="Linux",
        write_guard_probes=targets.probes,
        exec_probe="/bin/codex",
    )
    assert len(base) == 5
    assert base[4].label == "cli-exec-allowed"
    assert base[4].expect_denied is False
    assert base[4].cleanup_path is None
    assert base[4].command == ["/bin/codex", "--version"]
    assert len(with_guard) == 5 + len(targets.probes)
    assert len(with_guard) >= 7
    assert all(p.expect_denied for p in with_guard[5:])
    assert all(p.cleanup_path is not None for p in with_guard[5:])


def test_a_deny_that_covers_only_the_gitdir_fails_on_the_common_dir_probe(tmp_path: Path) -> None:
    # AC1.2: in a linked worktree the per-worktree gitdir and the shared common dir are different
    # directories, and the sandboxes have a built-in linked-worktree `.git` allowance to override. A
    # profile that closed only one of them must fail on the other — one probe covering "the .git"
    # would have passed while the common dir stayed writable.
    guard = _write_guard(tmp_path, linked_worktree=True)
    targets = write_guard_probe_paths(guard.denied_write_paths)
    common_sentinel = str(guard.git_common_dir / WRITE_GUARD_SENTINEL)
    gitdir_sentinel = str(guard.git_dir / WRITE_GUARD_SENTINEL)
    # The labels are derived from the roots, so name the two from the fixture rather than by hand:
    # what this test is about is that they are DIFFERENT probes.
    common_label = next(label for label, path in targets.probes if path == common_sentinel)
    gitdir_label = next(label for label, path in targets.probes if path == gitdir_sentinel)
    assert common_label != gitdir_label

    def _every_root_denied_except_the_common_dir(
        argv: list[str], cwd: str, env: Mapping[str, str]
    ) -> tuple[int, str]:
        joined = " ".join(argv)
        if joined.endswith("--version"):  # the cli-exec probe executes fine here
            return 0, "codex-cli 0.0.0"
        if WRITE_GUARD_SENTINEL in joined:
            # Only the shared common dir accepts the write — including the per-worktree gitdir,
            # which is what a profile that closed "the .git" and nothing else would look like.
            return (0, "") if common_sentinel in joined else (1, "operation not permitted")
        if "main.py" in joined:
            return 0, "print(1)"
        return 1, "operation not permitted"

    outcome = run_codex_canary(
        command="codex",
        profile_arg="permissions.worc={ }",
        working_directory="/clone",
        private_probe="/clone/.worc/logs/req.json",
        exchange_probe=None,
        extra=ExtraProbes(repo_probe="/clone/src/main.py", write_guard_probes=targets.probes),
        env={},
        system="Linux",
        runner=_every_root_denied_except_the_common_dir,
    )
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CONFIGURATION_ERROR
    # The named probe is the common dir's own — not the gitdir's, and not the first root in the set:
    # every other probe was refused, so execution reached this one and it is the one that failed.
    failed = outcome.message.split("'")[1]
    assert failed == common_label
    assert failed != gitdir_label

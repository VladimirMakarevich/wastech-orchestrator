"""Unit tests for the Codex permission-profile canary classifier (WRI-003).

Deterministic: an injected runner returns scripted ``(rc, output)`` per probe (in the fixed probe
order), so no real ``codex sandbox`` is launched. The real-host proof lives in
test_codex_canary_smoke.py.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from wastech_orchestrator.providers.base import ErrorClass
from wastech_orchestrator.providers.codex_canary import (
    CAPABILITY_PASSED,
    CAPABILITY_POLICY_FAILED,
    CAPABILITY_UNSUPPORTED,
    ExtraProbes,
    build_canary_command,
    build_canary_probes,
    run_codex_canary,
    run_codex_capability_smoke,
)

# Probe order: private-read, private-shell-read, exchange-read, exchange-write.
_ALL_DENY = [(1, "operation not permitted"), (1, "operation not permitted")]
_EXCHANGE_OK = [(0, "task contents"), (1, "operation not permitted")]


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
    outcome = _run(_seq_runner(_ALL_DENY + _EXCHANGE_OK))
    assert outcome.ok
    assert outcome.error_class is None
    assert len(outcome.evidence) == 4


def test_canary_without_positive_control_is_capability_unavailable() -> None:
    # H4: with no exchange probe (and no repo probe), only deny probes run — there is no positive
    # control, so a broken probe harness would make every denial look enforced. The canary must
    # refuse to certify (CAPABILITY_UNAVAILABLE), never silently pass.
    outcome = _run(_seq_runner(_ALL_DENY), exchange=None)
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CAPABILITY_UNAVAILABLE
    assert len(outcome.evidence) == 2


def test_repo_read_positive_control_satisfies_selective_enforcement() -> None:
    # H4: a repo-read positive control (rc=0) is enough to prove selective enforcement even without
    # an exchange probe. Probe order: private-read, private-shell-read, repo-read.
    outcome = run_codex_canary(
        command="codex",
        profile_arg="permissions.worc={ }",
        working_directory="/clone",
        private_probe="/clone/.worc/logs/req.json",
        exchange_probe=None,
        extra=ExtraProbes(repo_probe="/clone/src/main.py"),
        env={},
        system="Linux",
        runner=_seq_runner([(1, "denied"), (1, "denied"), (0, "print(1)")]),
    )
    assert outcome.ok
    assert {e["probe"] for e in outcome.evidence} == {
        "private-read-denied",
        "private-shell-read-denied",
        "repo-read-allowed",
    }


def test_alias_read_that_succeeds_is_a_leak() -> None:
    # H4: a workspace symlink resolving to the private file that READS successfully is a leak.
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
    outcome = _run(_seq_runner(_ALL_DENY + _EXCHANGE_OK))
    for entry in outcome.evidence:
        assert set(entry) == {"probe", "expect_denied", "denied"}
        assert "SECRET" not in str(entry)


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


def test_private_readable_flips_reads_and_adds_write_deny() -> None:
    # VF-6: read-isolation OFF → private reads become positive controls (allowed) and a private
    # WRITE-denied probe is added to prove the control plane stays immutable.
    default = {
        p.label: p.expect_denied
        for p in build_canary_probes(private_probe="/p", exchange_probe=None, system="Linux")
    }
    assert default["private-read-denied"] is True
    readable = {
        p.label: p.expect_denied
        for p in build_canary_probes(
            private_probe="/p", exchange_probe=None, system="Linux", private_readable=True
        )
    }
    assert readable["private-read-allowed"] is False
    assert readable["private-shell-read-allowed"] is False
    assert readable["private-write-denied"] is True
    assert "private-read-denied" not in readable


# --- H4/H7/WRI-006: the no-model capability smoke (deterministic; scripted sandbox + inventory) ---


def _smoke_runner(*, writable: bool):
    """A fake ``codex sandbox`` runner keyed on the probe path (order-independent, so it is robust
    whether or not the alias fixture could be created on the host)."""

    def _run(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        probe = " ".join(argv[argv.index("--") + 1 :])
        is_write = ">>" in probe
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

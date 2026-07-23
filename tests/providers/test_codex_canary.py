"""Unit tests for the Codex permission-profile canary classifier (WRI-003).

Deterministic: an injected runner returns scripted ``(rc, output)`` per probe (in the fixed probe
order), so no real ``codex sandbox`` is launched. The real-host proof lives in
test_codex_canary_smoke.py.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping

from wastech_orchestrator.providers.base import ErrorClass
from wastech_orchestrator.providers.codex_canary import (
    build_canary_command,
    build_canary_probes,
    run_codex_canary,
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


def test_canary_passes_with_no_exchange_probe() -> None:
    outcome = _run(_seq_runner(_ALL_DENY), exchange=None)
    assert outcome.ok
    assert len(outcome.evidence) == 2


def test_denied_read_that_succeeds_is_security_configuration_error() -> None:
    # The private read was expected denied but SUCCEEDED — a real leak, non-fallback.
    outcome = _run(_seq_runner([(0, "SECRET LEAKED")]))
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CONFIGURATION_ERROR
    assert "not enforcing" in outcome.message


def test_capability_marker_is_capability_unavailable() -> None:
    outcome = _run(_seq_runner([(1, "windows: refusing to run unsandboxed")]))
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CAPABILITY_UNAVAILABLE


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

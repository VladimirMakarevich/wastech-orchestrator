"""Adapter output sinks are redacted before they are written (spec §12.6, §6.4).

Proves the gap-fix: ``stdout.log`` and ``events.jsonl`` (not just ``stderr.log``) are redacted, and
a secret seeded into a ``denied_read_paths`` file in the workspace is scrubbed from every sink. Also
checks the Claude ``Read(...)`` deny patterns reach the argv.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import ProviderConfig, SecurityConfig
from wastech_orchestrator.providers.base import AgentRunRequest, RunStatus
from wastech_orchestrator.providers.claude import ClaudeCodeProvider, build_claude_argv
from wastech_orchestrator.providers.codex import CodexProvider
from wastech_orchestrator.providers.process import ProcessResult

_TOKEN_SECRET = "ghp_" + "C" * 20  # token-shaped → caught by pattern redaction
_FILE_SECRET = "plainOpaqueSecret12345"  # only caught via denied_read_paths content-scan


def _clone_with_env(tmp_path: Path) -> str:
    clone = tmp_path / "clone"
    clone.mkdir(parents=True, exist_ok=True)
    (clone / ".env").write_text(f"APP_SECRET={_FILE_SECRET}\n", encoding="utf-8")
    return str(clone)


def _fake_proc(stdout_text: str) -> Callable[..., ProcessResult]:
    def run(argv: Any, *, stdout_path: str, **_: Any) -> ProcessResult:
        Path(stdout_path).write_text(stdout_text, encoding="utf-8")
        return ProcessResult(
            exit_code=0,
            timed_out=False,
            launch_error=None,
            duration_seconds=0.0,
            stdout_path=str(stdout_path),
            stderr_text=f"warning leaked {_TOKEN_SECRET} and {_FILE_SECRET}",
        )

    return run


def _assert_no_secrets(*paths: str) -> None:
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        assert _TOKEN_SECRET not in text, f"token secret leaked into {path}"
        assert _FILE_SECRET not in text, f"file secret leaked into {path}"


def _attempt_sinks(result: object) -> tuple[str, ...]:
    """Every written attempt artifact, including request.json (a named §6.4 sink)."""
    stdout_path = result.stdout_path  # type: ignore[attr-defined]
    request_json = str(Path(stdout_path).parent / "request.json")
    return (
        stdout_path,
        result.event_log_path,  # type: ignore[attr-defined]
        result.stderr_path,  # type: ignore[attr-defined]
        request_json,
    )


def test_claude_sinks_are_redacted(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    clone = _clone_with_env(tmp_path)
    stdout = (
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": f"done {_TOKEN_SECRET} {_FILE_SECRET}",
                "session_id": "s",
            }
        )
        + "\n"
    )
    provider = ClaudeCodeProvider(
        claude_config,
        security=security_config,
        artifacts_root=str(tmp_path / "art"),
        run_process=_fake_proc(stdout),
    )
    result = provider.run(make_request(working_directory=clone))

    assert result.status is RunStatus.SUCCEEDED
    assert result.final_message is not None
    assert _TOKEN_SECRET not in result.final_message
    assert _FILE_SECRET not in result.final_message
    _assert_no_secrets(*_attempt_sinks(result))


def test_codex_sinks_are_redacted(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    clone = _clone_with_env(tmp_path)
    stdout = (
        json.dumps({"type": "message", "text": f"done {_TOKEN_SECRET} {_FILE_SECRET}"})
        + "\n"
        + json.dumps({"type": "result", "status": "success"})
        + "\n"
    )
    provider = CodexProvider(
        codex_config,
        security=security_config,
        artifacts_root=str(tmp_path / "art"),
        run_process=_fake_proc(stdout),
    )
    result = provider.run(make_request(working_directory=clone))

    assert result.status is RunStatus.SUCCEEDED
    _assert_no_secrets(*_attempt_sinks(result))


def test_build_claude_argv_denies_reads_and_commands(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = build_claude_argv(
        claude_config,
        make_request(),
        denied_commands=("git commit", "git push"),
        denied_read_paths=(".env", "secrets/**"),
    )
    tools = argv[argv.index("--disallowedTools") + 1]
    assert "Read(.env)" in tools
    assert "Read(secrets/**)" in tools
    assert "Bash(git commit:*)" in tools
    assert "Bash(git push:*)" in tools

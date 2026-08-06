"""Unit tests for ClaudeCodeProvider.run() / preflight() with an injected process runner.

No real Claude binary and no subprocess: ``run_process`` is replaced by a deterministic fake that
writes a canned stdout and returns a chosen :class:`ProcessResult`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from wastech_orchestrator.config.schema import ProviderConfig, SecurityConfig
from wastech_orchestrator.providers.base import (
    FALLBACK_ELIGIBLE,
    AgentProvider,
    AgentRunRequest,
    AuthState,
    ErrorClass,
    ProviderError,
    RunStatus,
    build_effective_prompt,
)
from wastech_orchestrator.providers.claude import ClaudeCodeProvider, SandboxCapability
from wastech_orchestrator.providers.process import ProcessResult, QuiescenceResult
from wastech_orchestrator.runtime_layout import InternalDenyPolicy

FIXED_TIME = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
FAKE_GH_TOKEN = "ghp_" + "abcdef0123456789abcdef0123"


def _success_stream(*, is_error: bool = False, subtype: str = "success") -> str:
    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-99"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}},
        {
            "type": "result",
            "subtype": subtype,
            "is_error": is_error,
            "result": "Implemented the feature.",
            "session_id": "sess-99",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "structured_output": {"summary": "ok"},
        },
    ]
    return "\n".join(json.dumps(e) for e in events)


@dataclass
class FakeRun:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    launch_error: str | None = None
    # The containment quiescence result the runner reports. Default proven so existing
    # tests are unaffected; a test sets an unproven result to exercise the fail-closed gate.
    quiescence: QuiescenceResult | None = field(
        default_factory=lambda: QuiescenceResult(proven=True, detail="fake")
    )
    calls: int = 0
    captured: dict[str, Any] = field(default_factory=dict)

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Any,
        env: Any,
        timeout_seconds: int,
        stdout_path: Any,
        stdin_text: str | None = None,
        monotonic: Any = None,
        recorder: Any = None,
    ) -> ProcessResult:
        self.calls += 1
        self.captured = {"argv": list(argv), "stdin_text": stdin_text, "env": dict(env)}
        Path(stdout_path).write_text(self.stdout, encoding="utf-8")
        code = None if (self.timed_out or self.launch_error is not None) else self.exit_code
        return ProcessResult(
            exit_code=code,
            timed_out=self.timed_out,
            launch_error=self.launch_error,
            duration_seconds=0.5,
            stdout_path=str(stdout_path),
            stderr_text=self.stderr,
            quiescence=self.quiescence,
        )


def _provider(
    config: ProviderConfig,
    security: SecurityConfig,
    artifacts_root: Path,
    fake: FakeRun,
    *,
    capability: SandboxCapability = SandboxCapability.MACOS,
) -> ClaudeCodeProvider:
    # Inject a sandbox-available host by default so a workspace-write run builds on any CI host
    # (a bwrap-less Linux CI would otherwise raise CAPABILITY_UNAVAILABLE pre-model).
    return ClaudeCodeProvider(
        config,
        security=security,
        artifacts_root=artifacts_root,
        clock=lambda: FIXED_TIME,
        run_process=fake,
        sandbox_probe=lambda: capability,
    )


def _attempt_dir(root: Path) -> Path:
    return root / "logs" / "task-001" / "stages" / "planning" / "run-000001" / "1-claude"


def test_implements_agent_provider_protocol(
    claude_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    provider = _provider(claude_config, security_config, tmp_path, FakeRun())
    assert isinstance(provider, AgentProvider)
    assert provider.id == "claude"


def test_successful_run(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream())
    provider = _provider(claude_config, security_config, tmp_path, fake)
    result = provider.run(make_request())

    assert result.status is RunStatus.SUCCEEDED
    assert result.error is None
    assert result.session_id == "sess-99"
    assert result.final_message == "Implemented the feature."
    assert result.structured_output == {"summary": "ok"}
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}

    attempt = _attempt_dir(tmp_path)
    for name in ("request.json", "stdout.log", "stderr.log", "events.jsonl", "result.json"):
        assert (attempt / name).exists(), name


def test_clean_run_with_error_result_returns_failed_not_raised(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream(is_error=True, subtype="error_during_execution"))
    provider = _provider(claude_config, security_config, tmp_path, fake)
    result = provider.run(make_request())
    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.error_class is ErrorClass.TASK_FAILURE
    # task_failure is never fallback-eligible (it goes to the fixing stage, not another provider).
    assert ErrorClass.TASK_FAILURE not in FALLBACK_ELIGIBLE


def test_nonzero_exit_with_terminal_error_event_is_task_failure_not_crash(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # Claude exits non-zero on ``error_max_turns`` but still emits a terminal result event. That is
    # an agent OUTCOME (turn budget exhausted), not a crash: it must be a returned TASK_FAILURE with
    # the subtype surfaced — never a raised PROCESS_CRASHED.
    fake = FakeRun(stdout=_success_stream(is_error=True, subtype="error_max_turns"), exit_code=1)
    provider = _provider(claude_config, security_config, tmp_path, fake)
    result = provider.run(make_request())
    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.error_class is ErrorClass.TASK_FAILURE
    assert "error_max_turns" in result.error.message
    # Surfaced structurally so the flow layer detects the gate trigger without substring-matching.
    assert result.error.failure_subtype == "error_max_turns"


def test_unproven_quiescence_fails_closed_before_parsing_output(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # Even with a perfectly valid success stream and exit 0, an unproven process-tree
    # quiescence result makes run() fail closed with the non-fallback CONTAINMENT_UNVERIFIED BEFORE
    # the output is parsed or trusted — an unknown descendant may still be writing.
    fake = FakeRun(
        stdout=_success_stream(),
        exit_code=0,
        quiescence=QuiescenceResult(
            proven=False, detail="posix: survivors=[999]", survivors=(999,)
        ),
    )
    provider = _provider(claude_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.CONTAINMENT_UNVERIFIED
    assert "999" in str(exc.value)  # secret-free diagnostic reaches the message
    # Never fallback-eligible: a live unknown writer must not trigger a fresh agent on the other
    # provider (and never an auto-resumable park either).
    assert ErrorClass.CONTAINMENT_UNVERIFIED not in FALLBACK_ELIGIBLE
    # The failed attempt is still persisted for the audit trail.
    assert (_attempt_dir(tmp_path) / "result.json").exists()


def test_nonzero_exit_without_terminal_event_is_process_crashed(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # A non-zero exit with NO parseable terminal event is a genuine abnormal termination
    # (e.g. killed mid-run) and stays PROCESS_CRASHED.
    fake = FakeRun(stdout="", exit_code=137)
    provider = _provider(claude_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.PROCESS_CRASHED


def test_timeout_raises_and_writes_result(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(timed_out=True)
    provider = _provider(claude_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.TIMEOUT
    assert exc.value.is_fallback_eligible is True
    result_json = json.loads((_attempt_dir(tmp_path) / "result.json").read_text(encoding="utf-8"))
    assert result_json["error"]["error_class"] == "timeout"


def test_missing_binary_raises_binary_not_found(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(launch_error="could not launch 'claude'")
    provider = _provider(claude_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.BINARY_NOT_FOUND


def test_rate_limit_stderr_raises_rate_limited(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(exit_code=1, stderr="Error: rate limit exceeded (429)")
    provider = _provider(claude_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.RATE_LIMITED


def test_invalid_output_raises_invalid_output(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout="this is not stream-json at all", exit_code=0)
    provider = _provider(claude_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.INVALID_OUTPUT


def test_configuration_error_raises_before_launch(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    bad = replace(claude_config, extra_args=("--dangerously-skip-permissions",))
    fake = FakeRun(stdout=_success_stream())
    provider = _provider(bad, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    assert fake.calls == 0  # never launched
    assert (_attempt_dir(tmp_path) / "request.json").exists()


def test_capability_unavailable_raises_before_launch(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # A strict workspace-write attempt on a supported host whose Bash sandbox deps are
    # missing raises CAPABILITY_UNAVAILABLE PRE-MODEL — nothing is launched, the request artifact is
    # still written, and it is deliberately not an unconditional fallback (the Router gates it).
    fake = FakeRun(stdout=_success_stream())
    provider = _provider(
        claude_config,
        security_config,
        tmp_path,
        fake,
        capability=SandboxCapability.LINUX_MISSING_DEPS,
    )
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request(permission_profile="workspace-write"))
    assert exc.value.error_class is ErrorClass.CAPABILITY_UNAVAILABLE
    assert exc.value.is_fallback_eligible is False
    assert fake.calls == 0  # never launched — no paid model call
    assert (_attempt_dir(tmp_path) / "request.json").exists()


def test_prompt_is_delivered_via_stdin_not_argv(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    sentinel = "UNIQUE-PROMPT-SENTINEL-7788"
    fake = FakeRun(stdout=_success_stream())
    provider = _provider(claude_config, security_config, tmp_path, fake)
    provider.run(make_request(prompt=sentinel))
    assert sentinel in fake.captured["stdin_text"]
    assert all(sentinel not in token for token in fake.captured["argv"])


def test_request_json_prompt_includes_context_footer(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    """``request.json``'s ``"prompt"`` must match what was actually piped to stdin — the
    context-files footer, not just the bare Core-rendered template (audit-trail parity)."""
    fake = FakeRun(stdout=_success_stream())
    provider = _provider(claude_config, security_config, tmp_path, fake)
    request = make_request(
        prompt="Do the thing.",
        task_path="/t/task.md",
        plan_path="/t/plan.md",
    )
    provider.run(request)
    request_json = json.loads((_attempt_dir(tmp_path) / "request.json").read_text())

    effective_prompt = build_effective_prompt(request)
    assert "Context files (read them as needed" in effective_prompt
    assert request_json["prompt"] == effective_prompt
    assert request_json["prompt"] == fake.captured["stdin_text"]


def test_request_json_context_paths_includes_skill_reference_paths(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream())
    provider = _provider(claude_config, security_config, tmp_path, fake)
    provider.run(make_request(skill_reference_paths=("/skills/foo/SKILL.md",)))
    request_json = json.loads((_attempt_dir(tmp_path) / "request.json").read_text())
    assert request_json["context_paths"]["skill_reference_paths"] == ["/skills/foo/SKILL.md"]
    assert "/skills/foo/SKILL.md" in request_json["prompt"]


def test_denied_commands_reach_argv_as_disallowed_tools(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream())
    provider = _provider(claude_config, security_config, tmp_path, fake)
    provider.run(make_request())
    argv = fake.captured["argv"]
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert "Bash(git commit:*)" in disallowed
    assert "Bash(git push:*)" in disallowed


def test_stderr_is_redacted_in_artifact(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream(), stderr=f"warning: token leaked {FAKE_GH_TOKEN}")
    provider = _provider(claude_config, security_config, tmp_path, fake)
    provider.run(make_request())
    stderr_log = (_attempt_dir(tmp_path) / "stderr.log").read_text(encoding="utf-8")
    assert FAKE_GH_TOKEN not in stderr_log
    assert "[REDACTED]" in stderr_log


def test_request_json_redacts_prompt_secret(
    claude_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream())
    provider = _provider(claude_config, security_config, tmp_path, fake)
    provider.run(make_request(prompt=f"here is a token {FAKE_GH_TOKEN} do not leak"))
    request_json = (_attempt_dir(tmp_path) / "request.json").read_text(encoding="utf-8")
    assert FAKE_GH_TOKEN not in request_json


def test_workspace_write_run_with_deny_policy_writes_sandbox_settings(
    claude_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    # The other run tests use deny_policy=None (an UNISOLATED config). Prove the isolated path
    # end-to-end: with an InternalDenyPolicy on a sandbox-capable host, a workspace-write run writes
    # the OS Bash-sandbox settings file and passes `--settings`, with the private home in denyRead.
    deny = InternalDenyPolicy(
        control_home=tmp_path / ".worc",
        private_home=tmp_path / ".worc",
        env_file=None,
        provider_homes=(),
    )
    fake = FakeRun(stdout=_success_stream())
    provider = ClaudeCodeProvider(
        claude_config,
        security=security_config,
        artifacts_root=tmp_path,
        clock=lambda: FIXED_TIME,
        run_process=fake,
        sandbox_probe=lambda: SandboxCapability.MACOS,
        deny_policy=deny,
    )
    provider.run(_make_ws_request(tmp_path))

    argv = fake.captured["argv"]
    assert "--settings" in argv
    settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
    sandbox = settings["sandbox"]
    assert sandbox["enabled"] is True and sandbox["failIfUnavailable"] is True
    assert (tmp_path / ".worc").as_posix() in sandbox["filesystem"]["denyRead"]


def _make_ws_request(tmp_path: Path) -> AgentRunRequest:
    return AgentRunRequest(
        task_id="task-001",
        node_id="implementation",
        working_directory=str(tmp_path / "clone"),
        prompt="do it",
        permission_profile="workspace-write",
        timeout_seconds=7200,
        attempt=1,
        node_run_id=1,
    )


def test_preflight_reports_version_when_binary_runs(
    claude_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    fake = FakeRun(stdout="1.2.3 (Claude Code)\n", exit_code=0)
    provider = _provider(claude_config, security_config, tmp_path, fake)
    health = provider.preflight()
    assert health.executable_found is True
    assert health.version == "1.2.3"
    assert health.provider_id == "claude"


def test_preflight_missing_binary(
    claude_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    fake = FakeRun(launch_error="not found")
    provider = _provider(claude_config, security_config, tmp_path, fake)
    health = provider.preflight()
    assert health.executable_found is False
    assert health.version is None


# --- Claude capability/degraded preflight (claude --help flag-drift guard) ---------------------

# The isolation-critical + resume flags a healthy Claude CLI advertises (2.1.x surface).
_FULL_CLAUDE_HELP = (
    "  --permission-mode <mode>       Permission mode to use for the session\n"
    "  --setting-sources <sources>    Comma-separated list of setting sources\n"
    "  --strict-mcp-config            Only use MCP servers from --mcp-config\n"
    "  --tools <tools...>             Specify the list of available tools\n"
    "  --allowedTools <tools...>      Tools allowed without prompting\n"
    "  -r, --resume [value]           Resume a conversation by session ID\n"
)


class _ProbingClaudeRun:
    """A fake runner answering ``--version``, ``--help`` and ``auth status`` by argv (preflight)."""

    def __init__(self, *, help_text: str, auth_answer: str = '{"loggedIn": true}') -> None:
        self._help_text = help_text
        self._auth_answer = auth_answer
        self.argvs: list[list[str]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Any,
        env: Any,
        timeout_seconds: int,
        stdout_path: Any,
        stdin_text: str | None = None,
        monotonic: Any = None,
        recorder: Any = None,
    ) -> ProcessResult:
        self.argvs.append(list(argv))
        if "--version" in argv:
            out = "2.1.217 (Claude Code)\n"
        elif "auth" in argv:
            out = self._auth_answer
        else:
            out = self._help_text
        Path(stdout_path).write_text(out, encoding="utf-8")
        return ProcessResult(
            exit_code=0,
            timed_out=False,
            launch_error=None,
            duration_seconds=0.1,
            stdout_path=str(stdout_path),
            stderr_text="",
        )


def _probing_provider(
    config: ProviderConfig, security: SecurityConfig, tmp_path: Path, fake: _ProbingClaudeRun
) -> ClaudeCodeProvider:
    return ClaudeCodeProvider(
        config,
        security=security,
        artifacts_root=tmp_path,
        clock=lambda: FIXED_TIME,
        run_process=fake,
        sandbox_probe=lambda: SandboxCapability.MACOS,
    )


def test_preflight_passes_when_help_advertises_isolation_flags(
    claude_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    fake = _ProbingClaudeRun(help_text=_FULL_CLAUDE_HELP)
    health = _probing_provider(claude_config, security_config, tmp_path, fake).preflight()
    assert health.supports_required_features is True
    assert health.degraded_reasons == ()
    assert any("--help" in argv for argv in fake.argvs)  # the capability probe ran


def test_preflight_fails_when_isolation_flag_missing(
    claude_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    # A CLI that dropped --setting-sources changed the read-isolation surface: block BEFORE a paid
    # model call rather than failing mid-run with "unknown option".
    help_text = _FULL_CLAUDE_HELP.replace(
        "  --setting-sources <sources>    Comma-separated list of setting sources\n", ""
    )
    fake = _ProbingClaudeRun(help_text=help_text)
    health = _probing_provider(claude_config, security_config, tmp_path, fake).preflight()
    assert health.supports_required_features is False
    assert "--setting-sources" in health.message


def test_preflight_degrades_when_resume_flag_missing(
    claude_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    # Losing --resume degrades resume nodes but not fresh runs → advisory (fallback-aware upstream),
    # not a hard capability failure.
    help_text = _FULL_CLAUDE_HELP.replace(
        "  -r, --resume [value]           Resume a conversation by session ID\n", ""
    )
    fake = _ProbingClaudeRun(help_text=help_text)
    health = _probing_provider(claude_config, security_config, tmp_path, fake).preflight()
    assert health.supports_required_features is True  # isolation-critical flags still present
    assert any("--resume" in reason for reason in health.degraded_reasons)


# --- Claude credential probe (claude auth status) -----------------------------------------------


def test_preflight_auth_reports_logged_in_without_copying_any_identity(
    claude_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    # The real answer carries the account email and the organization id and name beside the two keys
    # this probe wants. They are dropped at the parse boundary, so no later format string can leak
    # them into a preflight line, a log record or a report — assert on the whole record at once.
    email, org_id, org_name = "someone@example.com", "org_12345", "Example Org"
    fake = _ProbingClaudeRun(
        help_text=_FULL_CLAUDE_HELP,
        auth_answer=json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "email": email,
                "orgId": org_id,
                "orgName": org_name,
                "subscriptionType": "team",
            }
        ),
    )
    health = _probing_provider(claude_config, security_config, tmp_path, fake).preflight()
    assert health.auth is not None
    assert health.auth.state is AuthState.LOGGED_IN
    assert health.auth.method == "claude.ai"
    rendered = repr(health)
    assert not any(secret in rendered for secret in (email, org_id, org_name))
    # ``--json`` is passed explicitly rather than relying on it staying the CLI's default.
    assert ["claude", "auth", "status", "--json"] in fake.argvs


def test_preflight_auth_reports_logged_out(
    claude_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    # The verb exits 0 whether or not credentials exist, so the payload is the only honest signal.
    fake = _ProbingClaudeRun(
        help_text=_FULL_CLAUDE_HELP,
        auth_answer=json.dumps({"loggedIn": False, "authMethod": "none"}),
    )
    health = _probing_provider(claude_config, security_config, tmp_path, fake).preflight()
    assert health.auth is not None
    assert health.auth.state is AuthState.LOGGED_OUT
    assert health.auth.method is None  # a logged-out answer names no mechanism worth reporting
    assert "claude auth login" in health.auth.detail


@pytest.mark.parametrize(
    "answer",
    [
        "Logged in as someone@example.com\n",  # a human-readable mode, not the object
        '{"apiProvider": "firstParty"}',  # an object that does not answer the question
        "",
    ],
)
def test_preflight_auth_is_unknown_when_the_answer_is_unreadable(
    claude_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path, answer: str
) -> None:
    # An unreadable answer is not evidence in either direction, so it makes no claim.
    fake = _ProbingClaudeRun(help_text=_FULL_CLAUDE_HELP, auth_answer=answer)
    health = _probing_provider(claude_config, security_config, tmp_path, fake).preflight()
    assert health.auth is not None
    assert health.auth.state is AuthState.UNKNOWN


def test_preflight_missing_binary_makes_no_credential_claim(
    claude_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    # A CLI that could not run has nothing to probe, so it asserts nothing — the whole point of
    # replacing a boolean that read true on every path where the version check happened to exit 0.
    fake = FakeRun(launch_error="not found")
    provider = _provider(claude_config, security_config, tmp_path, fake)
    assert provider.preflight().auth is None

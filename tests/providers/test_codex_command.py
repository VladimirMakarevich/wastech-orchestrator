"""Unit tests for the Codex command/prompt builders."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.providers.base import AgentRunRequest, ErrorClass, ProviderError
from wastech_orchestrator.providers.codex import (
    build_codex_argv,
    build_context_footer,
    build_effective_prompt,
)
from wastech_orchestrator.providers.codex_profile import PROFILE_NAME
from wastech_orchestrator.runtime_layout import InternalDenyPolicy, ProviderWriteGuardPolicy

LAST_MSG = "/logs/task/stages/planning/1-codex/last-message.txt"


def _argv(
    config: ProviderConfig,
    request: AgentRunRequest,
    *,
    schema: str | None = None,
    deny_policy: InternalDenyPolicy | None = None,
    denied_read_paths: Sequence[str] = (),
    strict_isolation: bool = True,
) -> list[str]:
    return build_codex_argv(
        config,
        request,
        output_schema_path=schema,
        last_message_path=LAST_MSG,
        deny_policy=deny_policy,
        strict_isolation=strict_isolation,
        denied_read_paths=denied_read_paths,
    )


def _config_values(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv[:-1]) if token in ("-c", "--config")]


def _profile_arg(argv: list[str]) -> str:
    matches = [v for v in _config_values(argv) if v.startswith(f"permissions.{PROFILE_NAME}=")]
    assert len(matches) == 1, f"expected one permission-profile -c value, got {matches}"
    return matches[0]


def _assert_reasoning_config(argv: list[str], value: str) -> None:
    assert f'model_reasoning_effort="{value}"' in _config_values(argv)


def test_disables_project_doc_discovery(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # WRI-011: live AGENTS.md project-doc discovery is disabled (the frozen instructions are
    # injected on stdin instead). This is distinct from the project *trust* control (WRI-003).
    argv = _argv(codex_config, make_request())
    assert "project_doc_max_bytes=0" in _config_values(argv)
    assert "--reasoning-effort" not in argv


def test_argv_is_codex_exec_reading_from_stdin(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    request = make_request(working_directory="/clone")
    argv = _argv(codex_config, request)
    assert argv[:4] == ["codex", "--ask-for-approval", "never", "exec"]
    assert argv[-1] == "-"  # prompt comes from stdin
    assert "--cd" in argv and argv[argv.index("--cd") + 1] == "/clone"
    assert argv.index("--ask-for-approval") < argv.index("exec")
    assert "--json" in argv
    assert argv[argv.index("--output-last-message") + 1] == LAST_MSG
    # WRI-003: isolation is a generated permission profile, NOT the legacy --sandbox mode flag.
    assert "--sandbox" not in argv
    assert f'default_permissions="{PROFILE_NAME}"' in _config_values(argv)


def test_profile_selected_and_user_config_ignored(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # WRI-003: the generated profile is injected as ONE inline-table -c value and selected as
    # default_permissions; the operator's base config.toml is ignored (auth still uses CODEX_HOME).
    argv = _argv(codex_config, make_request(working_directory="/clone"))
    profile = _profile_arg(argv)
    assert '"extends" = ":workspace"' in profile
    assert '"/clone" = "write"' in profile
    assert "--ignore-user-config" in argv


def test_project_marked_untrusted(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request(working_directory="/clone"))
    assert 'projects."/clone".trust_level="untrusted"' in _config_values(argv)


def test_tool_surfaces_disabled(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request())
    disabled = {argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == "--disable"}
    assert {"hooks", "multi_agent", "computer_use", "plugins"} <= disabled


def test_deny_policy_and_write_guard_projected_into_profile(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    deny = InternalDenyPolicy(
        control_home=Path("/clone/.worc"),
        private_home=Path("/clone/.worc"),
        env_file=None,
        provider_homes=(Path("/home/op/.codex"),),
    )
    wg = ProviderWriteGuardPolicy(
        exchange_root=Path("/clone/.worc-io"),
        git_dir=Path("/clone/.git"),
        git_common_dir=Path("/clone/.git"),
        hooks_dir=Path("/clone/.git/hooks"),
        tasks_dir=Path("/clone/tasks"),
    )
    argv = _argv(
        codex_config,
        make_request(working_directory="/clone", write_guard=wg),
        deny_policy=deny,
        denied_read_paths=(".env", "secrets/**"),
    )
    profile = _profile_arg(argv)
    assert '"/clone/.worc" = "deny"' in profile  # private/control home denied
    assert '"/home/op/.codex" = "deny"' in profile  # provider auth home denied
    assert '"/clone/.env" = "deny"' in profile  # denied_read_paths projected
    assert '"/clone/secrets" = "deny"' in profile
    assert '"/clone/.worc-io" = "read"' in profile  # exchange readable, write-denied
    assert '"/clone/tasks" = "read"' in profile


def test_no_prompt_text_is_interpolated_into_argv(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    request = make_request(prompt="SECRET PROMPT CONTENT do-not-leak")
    argv = _argv(codex_config, request)
    assert all("SECRET PROMPT CONTENT" not in token for token in argv)


def test_no_legacy_sandbox_network_override(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # WRI-003: the old workspace-write network override is gone; network lives in the profile.
    for networked in (True, False):
        argv = _argv(codex_config, make_request(network_access=networked))
        assert "sandbox_workspace_write.network_access=true" not in _config_values(argv)
        assert '"network" = { "enabled" = false }' in _profile_arg(argv)


def test_web_search_disabled_when_offline(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # F5: an offline node must also deny the host web_search tool (backend-side, outside the profile
    # network policy) so network_access=false is truly offline.
    argv = _argv(codex_config, make_request())
    assert 'web_search="disabled"' in _config_values(argv)


def test_web_search_not_disabled_when_network_granted(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # An online node keeps its web_search grant; the profile still keeps the shell sandbox offline.
    argv = _argv(codex_config, make_request(network_access=True))
    assert 'web_search="disabled"' not in _config_values(argv)


def test_output_schema_flag_only_when_requested(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    no_schema = _argv(codex_config, make_request())
    assert "--output-schema" not in no_schema
    with_schema = _argv(codex_config, make_request(), schema="/path/schema.json")
    assert with_schema[with_schema.index("--output-schema") + 1] == "/path/schema.json"


def test_model_flag_uses_request_override_then_config(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # No model anywhere -> no flag.
    assert "--model" not in _argv(codex_config, make_request())
    # Config model used when set.
    cfg = replace(codex_config, model="cfg-model")
    argv_cfg = _argv(cfg, make_request())
    assert argv_cfg[argv_cfg.index("--model") + 1] == "cfg-model"
    # Request override wins over config.
    argv_req = _argv(cfg, make_request(model="req-model"))
    assert argv_req[argv_req.index("--model") + 1] == "req-model"


def test_forbidden_extra_args_are_rejected(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    bad = replace(codex_config, extra_args=("--dangerously-bypass-approvals-and-sandbox",))
    with pytest.raises(ProviderError) as exc:
        _argv(bad, make_request())
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    assert exc.value.is_fallback_eligible is False


def test_forbidden_extra_args_in_request_are_rejected(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    with pytest.raises(ProviderError) as exc:
        _argv(codex_config, make_request(extra_args=["--yolo"]))
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "flag",
    ["-c", "--config", "-p", "--profile", "-P", "--sandbox", "--add-dir", "--ignore-user-config"],
)
def test_reserved_authority_extra_args_are_rejected(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest], flag: str
) -> None:
    # WRI-003: an operator cannot select/replace the owned permission, config, or tool authority.
    cfg = replace(codex_config, extra_args=(flag, "x"))
    with pytest.raises(ProviderError) as exc:
        _argv(cfg, make_request())
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    # inline form is caught too
    with pytest.raises(ProviderError):
        _argv(replace(codex_config, extra_args=(f"{flag}=x",)), make_request())


def test_benign_extra_args_are_appended(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(codex_config, extra_args=("--image", "/tmp/diagram.png"))
    argv = _argv(cfg, make_request())
    assert "--image" in argv and "/tmp/diagram.png" in argv


def test_danger_full_access_escape_builds_legacy_sandbox_argv(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The full-access escape (danger-full-access, non-strict) makes NO isolation claim:
    # it emits the legacy --sandbox flag and no permission profile. strict_isolation gates it at
    # preflight (tests/security/test_isolation.py), not the argv builder.
    full = replace(codex_config, sandbox="danger-full-access")
    argv = _argv(full, make_request())
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert not any(v.startswith(f"permissions.{PROFILE_NAME}=") for v in _config_values(argv))
    assert "--ignore-user-config" not in argv


def test_read_only_request_extends_read_only_profile(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(
        codex_config, make_request(working_directory="/clone", permission_profile="read-only")
    )
    profile = _profile_arg(argv)
    assert '"extends" = ":read-only"' in profile
    assert '"/clone" = "read"' in profile
    assert "--sandbox" not in argv


def test_context_footer_lists_only_present_paths(
    make_request: Callable[..., AgentRunRequest],
) -> None:
    request = make_request(task_path="/logs/t/task.md", plan_path="/logs/t/plan.md")
    footer = build_context_footer(request)
    assert "/logs/t/task.md" in footer
    assert "/logs/t/plan.md" in footer
    assert "diff" not in footer  # diff_path is None


def test_context_footer_includes_human_input_path(
    make_request: Callable[..., AgentRunRequest],
) -> None:
    footer = build_context_footer(make_request(human_input_path="/logs/t/hitl/planning.json"))
    assert "human_input: /logs/t/hitl/planning.json" in footer


def test_context_footer_empty_when_no_paths(
    make_request: Callable[..., AgentRunRequest],
) -> None:
    assert build_context_footer(make_request()) == ""


def test_effective_prompt_appends_footer(make_request: Callable[..., AgentRunRequest]) -> None:
    request = make_request(prompt="Do the thing.", task_path="/logs/t/task.md")
    effective = build_effective_prompt(request)
    assert effective.startswith("Do the thing.")
    assert "/logs/t/task.md" in effective


def test_reasoning_request_level_adds_reasoning_effort(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request(reasoning="high"))
    _assert_reasoning_config(argv, "high")


def test_reasoning_minimal_passes_through(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request(reasoning="minimal"))
    _assert_reasoning_config(argv, "minimal")


def test_reasoning_xhigh_passes_through(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request(reasoning="xhigh"))
    _assert_reasoning_config(argv, "xhigh")


def test_reasoning_max_clamped_to_xhigh(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request(reasoning="max"))
    _assert_reasoning_config(argv, "xhigh")


def test_reasoning_config_level_adds_reasoning_effort(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(codex_config, reasoning="medium")
    argv = _argv(cfg, make_request())
    _assert_reasoning_config(argv, "medium")


def test_reasoning_request_beats_config(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(codex_config, reasoning="low")
    argv = _argv(cfg, make_request(reasoning="high"))
    _assert_reasoning_config(argv, "high")


def test_no_reasoning_means_no_reasoning_effort_flag(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request())
    assert "--reasoning-effort" not in argv
    assert not any(value.startswith("model_reasoning_effort=") for value in _config_values(argv))


def test_session_id_builds_exec_resume_with_isolation_before_resume(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Durable sessions: ``codex exec [exec-options] resume <ID>``. codex 0.144.x grammar: exec-level
    # options (--cd/--json/--output-schema, the profile/config-isolation -c and
    # --ignore-user-config/--disable) MUST precede ``resume``; only -m/--model and -c/--config
    # (model_reasoning_effort) follow it. This keeps fresh/resume isolation identical.
    argv = _argv(
        codex_config,
        make_request(session_id="sess-123", model="gpt-5.4", reasoning="high"),
        schema="/logs/schema.json",
    )
    resume = argv.index("resume")
    assert argv[resume + 1] == "sess-123"  # id positional right after resume
    for flag in (
        "--cd",
        "--json",
        "--output-last-message",
        "--output-schema",
        "--ignore-user-config",
    ):
        assert argv.index(flag) < resume, f"{flag} must precede resume"
    # the permission-profile + default_permissions + project-trust -c values are all exec-level
    for i, tok in enumerate(argv):
        if tok in ("-c", "--config") and (
            argv[i + 1].startswith(f"permissions.{PROFILE_NAME}=")
            or argv[i + 1].startswith("default_permissions=")
            or argv[i + 1].startswith("projects.")
        ):
            assert i < resume, f"exec-level -c {argv[i + 1]!r} must precede resume"
    # resume-compatible options follow the subcommand.
    assert argv.index("--model") > resume
    assert argv.index('model_reasoning_effort="high"') > resume
    assert argv[-1] == "-"  # prompt still comes from stdin
    assert "--ask-for-approval" in argv  # security flags preserved


def test_no_session_id_has_no_resume(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    assert "resume" not in _argv(codex_config, make_request())

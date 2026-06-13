"""Unit tests for the Claude Code command/prompt builders (spec §4.4, §12)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace

import pytest

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.providers.base import AgentRunRequest, ErrorClass, ProviderError
from wastech_orchestrator.providers.claude import (
    build_claude_argv,
    build_context_footer,
    build_effective_prompt,
    map_permission,
)

DENIED = ("git commit", "git push", "gh pr create")


def _argv(config: ProviderConfig, request: AgentRunRequest, **kwargs: object) -> list[str]:
    return build_claude_argv(config, request, denied_commands=kwargs.get("denied", ()))


def test_argv_is_claude_print_with_stream_json(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request())
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv


def test_workspace_write_maps_to_accept_edits(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(permission_profile="workspace-write"))
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_read_only_maps_to_plan(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(permission_profile="read-only"))
    assert argv[argv.index("--permission-mode") + 1] == "plan"


def test_map_permission_rejects_full_access_and_unknown() -> None:
    with pytest.raises(ProviderError) as exc:
        map_permission("danger-full-access")
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    with pytest.raises(ProviderError):
        map_permission("anything-goes")


def test_denied_commands_become_disallowed_tools(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(), denied=DENIED)
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert "Bash(git commit:*)" in disallowed
    assert "Bash(git push:*)" in disallowed
    assert "Bash(gh pr create:*)" in disallowed


def test_no_prompt_text_is_interpolated_into_argv(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    request = make_request(prompt="SECRET PROMPT CONTENT do-not-leak")
    argv = _argv(claude_config, request)
    assert all("SECRET PROMPT CONTENT" not in token for token in argv)


def test_model_flag_uses_request_override_then_config(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    assert "--model" not in _argv(claude_config, make_request())
    cfg = replace(claude_config, model="cfg-model")
    argv_cfg = _argv(cfg, make_request())
    assert argv_cfg[argv_cfg.index("--model") + 1] == "cfg-model"
    argv_req = _argv(cfg, make_request(model="req-model"))
    assert argv_req[argv_req.index("--model") + 1] == "req-model"


def test_max_turns_flag_only_when_configured(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    assert "--max-turns" not in _argv(claude_config, make_request())
    cfg = replace(claude_config, max_turns=5)
    argv = _argv(cfg, make_request())
    assert argv[argv.index("--max-turns") + 1] == "5"


def test_output_schema_is_passed_as_json_schema(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }
    argv = _argv(claude_config, make_request(output_schema=schema))

    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema


def test_forbidden_extra_args_are_rejected(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    bad = replace(claude_config, extra_args=("--dangerously-skip-permissions",))
    with pytest.raises(ProviderError) as exc:
        _argv(bad, make_request())
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    assert exc.value.is_fallback_eligible is False


def test_forbidden_extra_args_in_request_are_rejected(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    with pytest.raises(ProviderError) as exc:
        _argv(claude_config, make_request(extra_args=["--yolo"]))
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR


def test_weaker_permission_mode_override_is_rejected(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # workspace-write maps to acceptEdits; bypassPermissions would relax isolation.
    bad = replace(claude_config, extra_args=("--permission-mode", "bypassPermissions"))
    with pytest.raises(ProviderError) as exc:
        _argv(bad, make_request(permission_profile="workspace-write"))
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR


def test_weaker_permission_mode_override_inline_form_is_rejected(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # read-only maps to plan; acceptEdits is more permissive than plan.
    bad = replace(claude_config, extra_args=("--permission-mode=acceptEdits",))
    with pytest.raises(ProviderError) as exc:
        _argv(bad, make_request(permission_profile="read-only"))
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR


def test_safe_extra_args_are_appended(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(claude_config, extra_args=("--add-dir", "../lib"))
    argv = _argv(cfg, make_request())
    assert "--add-dir" in argv and "../lib" in argv


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


def test_effective_prompt_appends_footer(make_request: Callable[..., AgentRunRequest]) -> None:
    request = make_request(prompt="Do the thing.", task_path="/logs/t/task.md")
    effective = build_effective_prompt(request)
    assert effective.startswith("Do the thing.")
    assert "/logs/t/task.md" in effective


def test_reasoning_request_level_adds_effort_flag(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(reasoning="xhigh"))
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_reasoning_config_level_adds_effort_flag(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(claude_config, reasoning="high")
    argv = _argv(cfg, make_request())
    assert argv[argv.index("--effort") + 1] == "high"


def test_reasoning_request_beats_config(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(claude_config, reasoning="low")
    argv = _argv(cfg, make_request(reasoning="max"))
    assert argv[argv.index("--effort") + 1] == "max"


def test_no_reasoning_means_no_effort_flag(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request())
    assert "--effort" not in argv


def test_session_id_adds_resume_flag(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(session_id="abc-123"))
    assert argv[argv.index("--resume") + 1] == "abc-123"


def test_no_session_id_means_no_resume_flag(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request())
    assert "--resume" not in argv


def test_effort_before_max_turns(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(claude_config, max_turns=10, reasoning="high")
    argv = _argv(cfg, make_request())
    assert argv.index("--effort") < argv.index("--max-turns")

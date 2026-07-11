"""Unit tests for the Claude Code command/prompt builders."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

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


def test_read_only_maps_to_default_with_readonly_allowlist(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # F21: `default` mode (not `plan`) so a clarification goes through the role's structured
    # `human_input` field, not the CLI's plan-mode UX; Edit/Write absent from the allowlist is the
    # actual mutation gate.
    argv = _argv(claude_config, make_request(permission_profile="read-only"))
    assert argv[argv.index("--permission-mode") + 1] == "default"
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "Edit" not in allowed and "Write" not in allowed


def test_map_permission_rejects_full_access_and_unknown() -> None:
    with pytest.raises(ProviderError) as exc:
        map_permission("danger-full-access")
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    with pytest.raises(ProviderError):
        map_permission("anything-goes")


def test_network_access_off_by_default_no_web_tools(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    allowed = _argv(claude_config, make_request())[
        _argv(claude_config, make_request()).index("--allowedTools") + 1
    ]
    assert "WebFetch" not in allowed and "WebSearch" not in allowed


def test_network_access_allows_web_tools_when_granted(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(network_access=True))
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "WebFetch" in allowed and "WebSearch" in allowed
    # The filesystem permission mode is unchanged — network is the only thing added.
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_denied_commands_become_disallowed_tools(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(), denied=DENIED)
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert "Bash(git commit:*)" in disallowed
    assert "Bash(git push:*)" in disallowed
    assert "Bash(gh pr create:*)" in disallowed


def _native_memory_glob(config_dir: Path) -> str:
    # Mirrors claude._native_memory_deny_tools: //-anchored, symlink-canonicalized, POSIX slashes.
    return "//" + config_dir.resolve().as_posix().lstrip("/") + "/**"


def test_native_memory_paths_denied_for_custom_config_dir(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # F37: the spawned agent must not read/inject/leak Claude Code's native project memory, which
    # lives under the config dir OUTSIDE the target working tree. Write/Edit/Read are denied there,
    # honoring a custom CLAUDE_CONFIG_DIR. No denied_commands passed — the deny applies on its own,
    # by default (allow_native_memory off).
    config_dir = tmp_path / "isolated-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    argv = _argv(claude_config, make_request())
    disallowed = argv[argv.index("--disallowedTools") + 1]
    glob = _native_memory_glob(config_dir)
    assert f"Write({glob})" in disallowed
    assert f"Edit({glob})" in disallowed
    assert f"Read({glob})" in disallowed


def test_native_memory_deny_defaults_to_home_claude(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Absent CLAUDE_CONFIG_DIR, the deny covers the ~/.claude default.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    argv = _argv(claude_config, make_request())
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert f"Write({_native_memory_glob(Path.home() / '.claude')})" in disallowed


def test_allow_native_memory_drops_the_deny_but_keeps_command_denies(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # agent-native-memory-opt-in: with the Claude-only opt-in on, the F37 native-memory deny is
    # dropped so the agent may use its own auto-memory — but the command/read denies are untouched.
    config_dir = tmp_path / "isolated-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    opted_in = replace(claude_config, allow_native_memory=True)
    argv = _argv(opted_in, make_request(), denied=DENIED)
    disallowed = argv[argv.index("--disallowedTools") + 1]
    glob = _native_memory_glob(config_dir)
    assert f"Write({glob})" not in disallowed
    assert f"Edit({glob})" not in disallowed
    assert f"Read({glob})" not in disallowed
    # The publish blacklist is unaffected by the memory opt-in.
    assert "Bash(git commit:*)" in disallowed


def test_allow_native_memory_with_no_other_denies_omits_disallowed_flag(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # With the opt-in on and no command/read denies passed, there is nothing left to deny, so the
    # flag is omitted entirely (rather than emitted empty).
    opted_in = replace(claude_config, allow_native_memory=True)
    argv = build_claude_argv(opted_in, make_request())
    assert "--disallowedTools" not in argv


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


def test_bypass_permission_mode_override_builds_argv(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Full access is operator-selectable (no absolute ban): build_claude_argv no longer raises on a
    # --permission-mode escalation. The override is appended AFTER the orchestrator's own
    # --permission-mode so the CLI's last-wins resolution applies. The strict_isolation preflight
    # gate (not the adapter) blocks it by default — see tests/security/test_isolation.py.
    cfg = replace(claude_config, extra_args=("--permission-mode", "bypassPermissions"))
    argv = _argv(cfg, make_request(permission_profile="workspace-write"))
    # The orchestrator's own mode (acceptEdits) comes first; the operator override comes last.
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[-2:] == ["--permission-mode", "bypassPermissions"]


def test_permission_mode_override_inline_form_builds_argv(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The inline (flag=value) form also passes through now (gated by strict_isolation, not banned).
    cfg = replace(claude_config, extra_args=("--permission-mode=bypassPermissions",))
    argv = _argv(cfg, make_request(permission_profile="read-only"))
    assert argv[argv.index("--permission-mode") + 1] == "default"  # orchestrator's own, first
    assert argv[-1] == "--permission-mode=bypassPermissions"


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

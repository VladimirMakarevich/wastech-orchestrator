"""Unit tests for the Codex command/prompt builders (spec §4.4, §12.5)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.providers.base import AgentRunRequest, ErrorClass, ProviderError
from wastech_orchestrator.providers.codex import (
    build_codex_argv,
    build_context_footer,
    build_effective_prompt,
)

LAST_MSG = "/logs/task/stages/planning/1-codex/last-message.txt"


def _argv(config: ProviderConfig, request: AgentRunRequest, **kwargs: object) -> list[str]:
    return build_codex_argv(
        config, request, output_schema_path=kwargs.get("schema"), last_message_path=LAST_MSG
    )


def test_argv_is_codex_exec_reading_from_stdin(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    request = make_request(working_directory="/clone")
    argv = _argv(codex_config, request)
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert argv[-1] == "-"  # prompt comes from stdin
    assert "--cd" in argv and argv[argv.index("--cd") + 1] == "/clone"
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--ask-for-approval") + 1] == "never"
    assert "--json" in argv
    assert argv[argv.index("--output-last-message") + 1] == LAST_MSG


def test_no_prompt_text_is_interpolated_into_argv(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    request = make_request(prompt="SECRET PROMPT CONTENT do-not-leak")
    argv = _argv(codex_config, request)
    assert all("SECRET PROMPT CONTENT" not in token for token in argv)


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


def test_danger_full_access_sandbox_is_rejected(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    bad = replace(codex_config, sandbox="danger-full-access")
    with pytest.raises(ProviderError) as exc:
        _argv(bad, make_request())
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR


def test_safe_extra_args_are_appended(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(codex_config, extra_args=("--config", "model_reasoning=high"))
    argv = _argv(cfg, make_request())
    assert "--config" in argv and "model_reasoning=high" in argv


def test_context_footer_lists_only_present_paths(
    make_request: Callable[..., AgentRunRequest],
) -> None:
    request = make_request(task_path="/logs/t/task.md", plan_path="/logs/t/plan.md")
    footer = build_context_footer(request)
    assert "/logs/t/task.md" in footer
    assert "/logs/t/plan.md" in footer
    assert "diff" not in footer  # diff_path is None


def test_context_footer_empty_when_no_paths(
    make_request: Callable[..., AgentRunRequest],
) -> None:
    assert build_context_footer(make_request()) == ""


def test_effective_prompt_appends_footer(make_request: Callable[..., AgentRunRequest]) -> None:
    request = make_request(prompt="Do the thing.", task_path="/logs/t/task.md")
    effective = build_effective_prompt(request)
    assert effective.startswith("Do the thing.")
    assert "/logs/t/task.md" in effective

"""Tests for the VF-7 Core-owned orchestrator security preamble (defense-in-depth)."""

from __future__ import annotations

from wastech_orchestrator.core.flow.instruction_bundle import REPO_INSTRUCTION_NAMES
from wastech_orchestrator.core.flow.security_preamble import build_orchestrator_security_preamble
from wastech_orchestrator.core.prompts import ALLOWED_PROMPT_VARS
from wastech_orchestrator.runtime_layout import CONTROL_HOME_DIRNAME, EXCHANGE_HOME_DIRNAME


def test_baseline_present_in_both_isolation_states() -> None:
    # The always-on baseline: advisory framing + the write-side rules the read-deny does not cover.
    for off in (True, False):
        text = build_orchestrator_security_preamble(read_isolation_off=off)
        assert text.startswith("[Orchestrator security contract")
        assert "does not replace the sandbox" in text  # advisory, not enforcement
        assert "git commit/push/merge" in text
        assert "publishing is the orchestrator's job" in text


def test_reinforcement_only_when_read_isolation_off() -> None:
    marker = "Read-isolation is relaxed for this run"
    isolated = build_orchestrator_security_preamble(read_isolation_off=False)
    relaxed = build_orchestrator_security_preamble(read_isolation_off=True)
    assert marker not in isolated
    assert marker in relaxed
    # The reinforcement is strictly additive — the baseline is unchanged, only appended to.
    assert relaxed.startswith(isolated)


def test_path_tokens_are_emitted_from_layout_constants() -> None:
    # Anti-drift: the private/exchange dir names and the instruction filenames come from the same
    # constants the deny policies use, so the text cannot diverge from enforcement.
    text = build_orchestrator_security_preamble(read_isolation_off=True)
    assert f"`{CONTROL_HOME_DIRNAME}/`" in text  # .worc
    assert f"`{EXCHANGE_HOME_DIRNAME}/`" in text  # .worc-io
    for name in REPO_INSTRUCTION_NAMES:  # AGENTS.md / AGENTS.override.md / CLAUDE.md
        assert f"`{name}`" in text


def test_preamble_is_a_pure_function_of_read_isolation_only() -> None:
    # Its only input is the effective-read-isolation bool — nothing task/flow/extra_args-derived.
    assert build_orchestrator_security_preamble(
        read_isolation_off=True
    ) == build_orchestrator_security_preamble(read_isolation_off=True)
    assert build_orchestrator_security_preamble(
        read_isolation_off=True
    ) != build_orchestrator_security_preamble(read_isolation_off=False)


def test_preamble_has_no_secret_or_unrendered_variable() -> None:
    # Orchestrator-owned constant text: no prompt variable feeds it, so no ``{...}`` slots remain
    # and there is nothing to redact.
    for off in (True, False):
        text = build_orchestrator_security_preamble(read_isolation_off=off)
        assert "{" not in text and "}" not in text


def test_preamble_field_is_not_a_prompt_variable() -> None:
    # It cannot be reached/overridden from a task or flow role via the prompt renderer.
    assert "security_preamble" not in ALLOWED_PROMPT_VARS

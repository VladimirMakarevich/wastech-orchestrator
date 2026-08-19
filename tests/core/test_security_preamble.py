"""Tests for the Core-owned orchestrator security preamble (defense-in-depth)."""

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


def test_governance_files_declared_editable_not_read_only() -> None:
    # The preamble must no longer call the instruction files "read-only this run"; it states
    # they are ordinary, editable repository files (a change is reported, not blocked).
    for off in (True, False):
        text = build_orchestrator_security_preamble(read_isolation_off=off)
        assert "read-only this run" not in text
        assert "ordinary repository files" in text


def test_preamble_is_a_pure_function_of_its_three_config_flags() -> None:
    # Its only inputs are config-derived booleans — nothing task/flow/extra_args-derived, so a task
    # can never soften what the agent is told.
    assert build_orchestrator_security_preamble(
        read_isolation_off=True
    ) == build_orchestrator_security_preamble(read_isolation_off=True)
    assert build_orchestrator_security_preamble(
        read_isolation_off=True
    ) != build_orchestrator_security_preamble(read_isolation_off=False)
    for flag in ("advanced_mode", "no_write_floor"):
        assert build_orchestrator_security_preamble(
            read_isolation_off=True, **{flag: True}
        ) != build_orchestrator_security_preamble(read_isolation_off=True)


def test_the_mode_paragraph_appears_only_in_the_mode_and_still_forbids_publishing() -> None:
    """ТA.6.4: the mode gets its own paragraph, and it does not soften the two hard rules.

    Advisory by design and labelled as such — but this is the configuration where the advisory layer
    is closest to being all there is, so the paragraph has to be explicit about which two rules
    survive rather than leaving the reader to infer them from the baseline above.
    """
    off = build_orchestrator_security_preamble(read_isolation_off=True)
    on = build_orchestrator_security_preamble(read_isolation_off=True, advanced_mode=True)
    assert "maximum freedom" not in off
    assert on.startswith(off)  # additive: the baseline and the read-restraint text are unchanged
    tail = on[len(off) :]
    assert "Do not publish anything" in tail
    assert f"`{CONTROL_HOME_DIRNAME}/`" in tail and "`.git/`" in tail


def test_the_no_sandbox_paragraph_is_withheld_where_a_sandbox_exists() -> None:
    """ТA.9.3: say "nothing enforces this" only where nothing does.

    Rendered on every run it would be false on most of them, and a block that overstates once is
    discounted from then on — which costs exactly the hosts where it is the only thing left.
    """
    with_floor = build_orchestrator_security_preamble(read_isolation_off=True, advanced_mode=True)
    without = build_orchestrator_security_preamble(
        read_isolation_off=True, advanced_mode=True, no_write_floor=True
    )
    assert "no operating-system sandbox available" not in with_floor
    assert without.startswith(with_floor)
    assert "no operating-system sandbox available" in without[len(with_floor) :]


def test_preamble_has_no_secret_or_unrendered_variable() -> None:
    # Orchestrator-owned constant text: no prompt variable feeds it, so no ``{...}`` slots remain
    # and there is nothing to redact.
    for off in (True, False):
        text = build_orchestrator_security_preamble(read_isolation_off=off)
        assert "{" not in text and "}" not in text


def test_preamble_field_is_not_a_prompt_variable() -> None:
    # It cannot be reached/overridden from a task or flow role via the prompt renderer.
    assert "security_preamble" not in ALLOWED_PROMPT_VARS

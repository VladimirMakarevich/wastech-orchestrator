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
    """The mode gets its own paragraph, and it does not soften the two hard rules.

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
    # With the network and credentials the CLI picks up by itself, "that is the orchestrator's
    # job" no longer implies WHERE — so the ban names any address and any route outright, and the
    # paragraph says what the run actually got rather than only what it must not do with it.
    assert "not to any other address" in tail and "by any route" in tail
    assert "you have the network" in tail and "write outside this clone" in tail


def test_no_configuration_forbids_reading_the_exchange_it_hands_the_node() -> None:
    """The exchange is read-granted in every configuration; only writing it is banned.

    E2E-trial finding F9. At the trial's own configuration — read-isolation off plus advanced mode,
    both true together — the block said three things about `.worc-io/` at once: read only the paths
    you are given, do not read "any orchestrator-private file", and (in the mode paragraph)
    literally "do not read or write `.worc-io/`". Reviewers resolved that contradiction by refusing
    to review, filing a blocking finding that the task/plan/diff they were handed were forbidden
    reading — on the first review of two separate tasks. So the grant has to be explicit and no
    later sentence may take it back.
    """
    for kwargs in (
        {"read_isolation_off": False},
        {"read_isolation_off": True},
        {"read_isolation_off": True, "advanced_mode": True},
        {"read_isolation_off": True, "advanced_mode": True, "no_write_floor": True},
    ):
        text = build_orchestrator_security_preamble(**kwargs)
        assert f"`{EXCHANGE_HOME_DIRNAME}/` is your read-only input context" in text
        assert "are yours to read" in text
        # No sentence anywhere may forbid *reading* the exchange.
        for sentence in text.replace("\n", " ").split(". "):
            if EXCHANGE_HOME_DIRNAME not in sentence:
                continue
            assert "do not read or write " + f"`{EXCHANGE_HOME_DIRNAME}/`" not in sentence, kwargs
            assert "read no other path" not in sentence.lower() or "yours to read" in sentence
        # The blanket that a reader could fold the exchange into is gone.
        assert "any orchestrator-private file" not in text


def test_the_exchange_write_ban_survives_in_every_configuration() -> None:
    # The other half of F9: relaxing the read wording must not relax the write wording. The
    # exchange is the immutable surface a post-node fingerprint checks, so a node writing it is a
    # containment event — that ban is the same in all four renders.
    for kwargs in (
        {"read_isolation_off": False},
        {"read_isolation_off": True},
        {"read_isolation_off": True, "advanced_mode": True},
        {"read_isolation_off": True, "advanced_mode": True, "no_write_floor": True},
    ):
        text = build_orchestrator_security_preamble(**kwargs)
        assert "never create, modify, move, or delete anything there" in text
    mode = build_orchestrator_security_preamble(read_isolation_off=True, advanced_mode=True)
    assert f"do not write `{EXCHANGE_HOME_DIRNAME}/`" in mode


def test_the_private_home_read_ban_is_not_weakened_by_the_exchange_grant() -> None:
    # F9's fix must not become a hole: `.worc/` stays read-banned and write-banned in the baseline,
    # in the read-restraint paragraph, and in the mode paragraph.
    base = build_orchestrator_security_preamble(read_isolation_off=False)
    assert f"`{CONTROL_HOME_DIRNAME}/` is the orchestrator's private runtime" in base
    assert "do not read it and do not write it" in base
    relaxed = build_orchestrator_security_preamble(read_isolation_off=True)
    assert f"read nothing under `{CONTROL_HOME_DIRNAME}/`" in relaxed
    assert "no credential or environment file" in relaxed
    mode = build_orchestrator_security_preamble(read_isolation_off=True, advanced_mode=True)
    assert f"do not read or write `{CONTROL_HOME_DIRNAME}/`, `.git/` or `tasks/`" in mode


def test_the_no_sandbox_paragraph_is_withheld_where_a_sandbox_is_in_force() -> None:
    """Say "nothing enforces this" only where nothing does.

    Rendered on every run it would be false on most of them, and a block that overstates once is
    discounted from then on — which costs exactly the runs where it is the only thing left.

    The paragraph now speaks about the RUN rather than the host, and the wording carries that: a
    macOS host in the advanced mode has a sandbox available and does not raise one, so "no
    operating-system sandbox available" would have been false in exactly the configuration the
    paragraph became necessary for. What decides it is still `no_write_floor` alone, which the
    orchestrator derives from `describe_host_floor`.
    """
    with_floor = build_orchestrator_security_preamble(read_isolation_off=True, advanced_mode=True)
    without = build_orchestrator_security_preamble(
        read_isolation_off=True, advanced_mode=True, no_write_floor=True
    )
    assert "sandbox is in force" not in with_floor
    assert without.startswith(with_floor)
    tail = without[len(with_floor) :]
    assert "No operating-system sandbox is in force for this run" in tail
    # And it does not claim the machine lacks one — that was true of the two host classes only.
    assert "available" not in tail


def test_preamble_has_no_secret_or_unrendered_variable() -> None:
    # Orchestrator-owned constant text: no prompt variable feeds it, so no ``{...}`` slots remain
    # and there is nothing to redact.
    for off in (True, False):
        text = build_orchestrator_security_preamble(read_isolation_off=off)
        assert "{" not in text and "}" not in text


def test_preamble_field_is_not_a_prompt_variable() -> None:
    # It cannot be reached/overridden from a task or flow role via the prompt renderer.
    assert "security_preamble" not in ALLOWED_PROMPT_VARS


def test_the_no_sandbox_paragraph_covers_the_nodes_that_only_read() -> None:
    """The honest statement got wider when every node gained a shell.

    Before this phase a read-only node had no shell on any host, so "nothing keeps a write out"
    was a statement about writers. It now applies to every node in the run, and a paragraph that
    still read as if it were about writers would leave the reader of an audit node believing the
    warning was not addressed to them.
    """
    text = build_orchestrator_security_preamble(
        read_isolation_off=True, advanced_mode=True, no_write_floor=True
    )
    assert "every step of this run, including the ones whose job is only to read" in text
    assert "not a second shell" in text

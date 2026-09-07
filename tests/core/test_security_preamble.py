"""Tests for the Core-owned orchestrator security preamble (defense-in-depth)."""

from __future__ import annotations

from wastech_orchestrator.core.flow.instruction_bundle import REPO_INSTRUCTION_NAMES
from wastech_orchestrator.core.flow.security_preamble import build_orchestrator_security_preamble
from wastech_orchestrator.core.prompts import ALLOWED_PROMPT_VARS
from wastech_orchestrator.runtime_layout import CONTROL_HOME_DIRNAME, EXCHANGE_HOME_DIRNAME


def test_the_block_is_one_unconditional_constant() -> None:
    """One block, no inputs: nothing about a run can soften what the agent is told.

    The conditional paragraphs (read-restraint, advanced mode, no-write-floor) are gone with their
    flags — a contract that varied by configuration had to be read against the configuration to be
    trusted, and the run where it mattered most was the one where it said the most.
    """
    text = build_orchestrator_security_preamble()
    assert text == build_orchestrator_security_preamble()
    assert text.startswith("[Orchestrator security contract")
    # The advisory framing stays; the sandbox caveat that used to qualify it does not.
    assert "defense in depth" in text
    assert "does not replace the sandbox" not in text


def test_the_publication_ban_is_unconditional_and_names_every_route() -> None:
    """The widest form of the ban is in the baseline, not in a mode-only paragraph.

    The remote half of the floor is never mechanically prevented — a node with the network and
    credentials the CLI picks up by itself could publish anywhere — so "that is the orchestrator's
    job" does not say WHERE. The ban names any address by any route, and it is stated on every run
    rather than only on the one that relaxed the local enforcement.
    """
    text = build_orchestrator_security_preamble()
    assert "Do not publish anything" in text
    assert "no commit, push, merge, tag or pull request" in text
    assert "not to any other address" in text and "by any route" in text
    assert "publishing is the orchestrator's job" in text
    # Git control state keeps its own rule beside the publication one.
    assert "Do not touch Git control state" in text


def test_path_tokens_are_emitted_from_layout_constants() -> None:
    # Anti-drift: the private/exchange dir names come from the same constants the deny policies
    # use, so the text cannot diverge from enforcement.
    text = build_orchestrator_security_preamble()
    assert f"`{CONTROL_HOME_DIRNAME}/`" in text  # .worc
    assert f"`{EXCHANGE_HOME_DIRNAME}/`" in text  # .worc-io


def test_the_instruction_files_are_not_mentioned() -> None:
    # They are ordinary repository files, governed by the repo's own rules and by the write guard
    # (which deliberately does not cover them) — the contract says nothing about them either way.
    text = build_orchestrator_security_preamble()
    for name in REPO_INSTRUCTION_NAMES:  # AGENTS.md / AGENTS.override.md / CLAUDE.md
        assert name not in text


def test_the_exchange_is_read_granted_and_write_banned() -> None:
    """The exchange grant is explicit and no later sentence takes it back.

    E2E-trial finding F9: wording that folded `.worc-io/` into the read ban made reviewers refuse
    to review, filing a blocking finding that the task/plan/diff they were handed were forbidden
    reading — and that refusal then travelled to `fixing` as if it were rework.
    """
    text = build_orchestrator_security_preamble()
    assert f"`{EXCHANGE_HOME_DIRNAME}/` is your read-only input context" in text
    assert "are yours to read" in text
    assert "never create, modify, move, or delete anything there" in text
    for sentence in text.replace("\n", " ").split(". "):
        if EXCHANGE_HOME_DIRNAME not in sentence:
            continue
        assert "do not read or write " + f"`{EXCHANGE_HOME_DIRNAME}/`" not in sentence
        assert "read no other path" not in sentence.lower() or "yours to read" in sentence
    assert "any orchestrator-private file" not in text


def test_the_private_home_stays_read_and_write_banned() -> None:
    # The other side of F9's fix: relaxing the exchange wording must not relax `.worc/`.
    text = build_orchestrator_security_preamble()
    assert f"`{CONTROL_HOME_DIRNAME}/` is the orchestrator's private runtime" in text
    assert "do not read it and do not write it" in text
    assert "Never read credential/environment files" in text


def test_preamble_has_no_secret_or_unrendered_variable() -> None:
    # Orchestrator-owned constant text: no prompt variable feeds it, so no ``{...}`` slots remain
    # and there is nothing to redact.
    text = build_orchestrator_security_preamble()
    assert "{" not in text and "}" not in text


def test_preamble_field_is_not_a_prompt_variable() -> None:
    # It cannot be reached/overridden from a task or flow role via the prompt renderer.
    assert "security_preamble" not in ALLOWED_PROMPT_VARS

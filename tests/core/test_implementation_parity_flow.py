"""The implementation parity fixture loads, validates, and has the expected pipeline shape.

The golden-harness dual-run consumes it in step B.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.flow.snapshot import FlowSnapshot, load_flow
from wastech_orchestrator.core.flow.validator import validate_flow

_FIXTURE = Path(__file__).parent / "flows" / "implementation_parity.yaml"


def _snapshot() -> FlowSnapshot:
    snap = load_flow(_FIXTURE)
    validate_flow(snap)  # must not raise
    return snap


def test_parity_flow_validates_with_expected_nodes() -> None:
    snap = _snapshot()
    assert set(snap.nodes_by_id) == {
        "refinement",
        "planning",
        "implementation",
        "testing",
        "review",
        "fixing",
        "summary",
        "publish",
    }
    # Deliberately NO supervisor / testing_quality node in this fixture.
    assert "supervise_impl" not in snap.nodes_by_id
    assert "testing_quality" not in snap.nodes_by_id


def test_parity_flow_fix_loops_are_declared() -> None:
    snap = _snapshot()
    by = {(e.from_node, e.to, e.outcome): e for e in snap.doc.edges}
    assert by[("testing", "fixing", "fail")].loop == "test_fix"
    assert by[("review", "fixing", "rework")].loop == "review_fix"
    # fixing returns to testing unconditionally (review-driven fix re-tests; cf. _after_edit_target)
    assert ("fixing", "testing", None) in by


def test_parity_flow_entry_is_refinement() -> None:
    snap = _snapshot()
    incoming = dict.fromkeys(snap.nodes_by_id, 0)
    for edge in snap.doc.edges:
        incoming[edge.to] += 1
    entries = [nid for nid, n in incoming.items() if n == 0]
    assert entries == ["refinement"]


def test_parity_flow_terminal_is_publish() -> None:
    snap = _snapshot()
    sources = {e.from_node for e in snap.doc.edges}
    terminals = set(snap.nodes_by_id) - sources
    assert terminals == {"publish"}


def test_parity_flow_decomposition_sub_flow() -> None:
    snap = _snapshot()
    dec = snap.doc.decomposition
    assert dec is not None
    assert dec.proposed_by == "planning"
    assert dec.sub_flow == ("implementation", "testing", "review", "fixing")
    assert dec.shared_budget == "global_fix_iterations"


def test_the_packaged_review_prompt_is_reached_by_the_subtask_spec() -> None:
    """The packaged `review` node runs per subtask and its prompt must say which one.

    F1's mechanism half is the evaluator runner publishing the decomposition variables; this is the
    half that makes it change anything for the shipped flow. `review` is in
    `decomposition.sub_flow`, so it runs once per subtask — and while it held only the root task
    file and the shared plan it charged every not-yet-implemented part of the whole task against
    whichever subtask was under review (3 false blocking findings on subtask 1 of 5, each demanding
    work that subtask's own spec forbade).

    Asserted against the shipped prompt rather than a fixture, because a variable published by the
    runner and referenced by nobody is not a fix. The `{?…}{/…}` delimiters matter too: without
    them a whole-task run renders "subtask None of None".
    """
    prompt = (
        Path(__file__).parents[2]
        / "src"
        / "wastech_orchestrator"
        / "packaged"
        / "flows"
        / "implementation"
        / "review.md"
    ).read_text(encoding="utf-8")
    assert "{?subtask_spec_path}" in prompt and "{/subtask_spec_path}" in prompt
    for name in ("{subtask_order}", "{subtask_count}", "{subtask_spec_path}"):
        assert name in prompt, name
    # The boundary is the point: judging against the subtask's own criteria, and not charging it
    # with a later subtask's work.
    assert "not the whole task" in prompt
    assert "not missing from this one" in prompt


def test_the_packaged_review_prompt_stands_down_on_docs_only_for_code_diffs_only() -> None:
    """F2: the "documentation runs later, do not flag it as missing" clause needs a condition.

    Correct for a code task — the `documentation` node runs after `review` and would make the
    finding moot — and wrong for a docs-only deliverable, where the docs are the entire product and
    the sentence invites the reviewer to stand down on the only thing worth reviewing. The trial
    queued two such deliverables. Recorded honestly: it was tested once and did not fire, so the
    runtime risk is unproven; the sentence is unconditional in the text, which is the defect.
    """
    prompt = (
        Path(__file__).parents[2]
        / "src"
        / "wastech_orchestrator"
        / "packaged"
        / "flows"
        / "implementation"
        / "review.md"
    ).read_text(encoding="utf-8")
    clause = "do not flag those as missing"
    assert clause in prompt
    tail = prompt[prompt.index(clause) + len(clause) :]
    # The exemption follows the instruction it qualifies, in the same sentence.
    assert tail.split("\n", 1)[0].startswith(" — unless the diff is **only** documentation")

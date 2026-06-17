"""The implementation parity fixture loads, validates, and has the expected pipeline shape (P1.4).

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
    # Deliberately NO supervisor / testing_quality (those are P2 target features).
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

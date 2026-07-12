"""Offline replay harness (05.5): the metric machinery + a recorded baseline (AC-O1..O4).

The orchestrator is greenfield — there is no corpus of historical production tasks to replay yet —
so these fixtures are **synthetic** recorded runs that exercise the harness's aggregation, the
AC-O verdicts, and the report renderer. The real baseline replaces these numbers once real runs
accrue; the approach (and the thresholds) are what is locked here.
"""

from __future__ import annotations

from pathlib import Path

from tests.eval.harness import (
    MODE_OFF,
    MODE_ON,
    MODE_ON_NO_ENTITY,
    TaskMetrics,
    build_baseline,
    compare_modes,
    render_baseline_markdown,
    summarize_mode,
)


def _off() -> list[TaskMetrics]:
    # memory-off: more tokens, lower first-pass on the repeated hotspot.
    return [
        TaskMetrics(
            "t1",
            tokens=12000,
            wall_clock_s=300.0,
            first_pass_pass=False,
            repeated_repo=True,
            hotspot=True,
            active_records=0,
        ),
        TaskMetrics(
            "t2",
            tokens=11000,
            wall_clock_s=280.0,
            first_pass_pass=True,
            repeated_repo=True,
            hotspot=True,
            active_records=0,
        ),
        TaskMetrics("t3", tokens=9000, wall_clock_s=200.0, first_pass_pass=True, active_records=0),
    ]


def _on() -> list[TaskMetrics]:
    # memory-on: ~17% fewer tokens on repeated tasks, both hotspots pass first time, clean safety.
    return [
        TaskMetrics(
            "t1",
            tokens=10000,
            wall_clock_s=250.0,
            first_pass_pass=True,
            repeated_repo=True,
            hotspot=True,
            active_records=20,
        ),
        TaskMetrics(
            "t2",
            tokens=9000,
            wall_clock_s=240.0,
            first_pass_pass=True,
            repeated_repo=True,
            hotspot=True,
            active_records=20,
        ),
        TaskMetrics("t3", tokens=8800, wall_clock_s=195.0, first_pass_pass=True, active_records=20),
    ]


def test_summarize_mode_aggregates_the_metric_stack() -> None:
    summary = summarize_mode(MODE_OFF, _off())
    assert summary.tasks == 3
    assert summary.mean_tokens == (12000 + 11000 + 9000) / 3
    assert summary.repeated_mean_tokens == (12000 + 11000) / 2  # AC-O1 subset only
    assert summary.hotspot_first_pass_rate == 0.5  # 1 of 2 hotspot tasks passed first time
    assert summary.stale_contradiction_rate == 0.0


def test_comparison_meets_ac_o_targets_when_memory_helps() -> None:
    comp = compare_modes(summarize_mode(MODE_OFF, _off()), summarize_mode(MODE_ON, _on()))
    assert comp.token_reduction_pct >= 0.10  # ~17%
    assert comp.meets_ac_o1  # token reduction clears AC-O1
    assert comp.meets_ac_o2  # hotspot first-pass 50% → 100% (+50pp)
    assert comp.meets_ac_o3  # no leaks, no external promotions, 0 stale rate
    assert comp.measured_lift  # AC-O4 gate opens for the next phase


def test_no_lift_keeps_the_ac_o4_gate_closed() -> None:
    # memory-on identical to memory-off → no measured lift → V2/V3/V4 stay gated (AC-O4).
    flat = compare_modes(summarize_mode(MODE_OFF, _off()), summarize_mode(MODE_ON, _off()))
    assert not flat.meets_ac_o1 and not flat.meets_ac_o2
    assert not flat.measured_lift


def test_safety_counters_fail_ac_o3() -> None:
    leaky = [
        TaskMetrics(
            "t1",
            tokens=1,
            wall_clock_s=1.0,
            first_pass_pass=True,
            secret_leaks=1,
            active_records=10,
        )
    ]
    comp = compare_modes(summarize_mode(MODE_OFF, _off()), summarize_mode(MODE_ON, leaky))
    assert not comp.meets_ac_o3  # a single planted-secret leak fails the safety gate


def test_build_baseline_and_render_report(tmp_path: Path) -> None:
    baseline = build_baseline({MODE_OFF: _off(), MODE_ON: _on(), MODE_ON_NO_ENTITY: _on()})
    assert len(baseline.modes) == 3
    assert baseline.comparison is not None and baseline.comparison.measured_lift
    report = render_baseline_markdown(baseline)
    assert "# Memory eval baseline" in report
    assert MODE_ON_NO_ENTITY in report
    assert "AC-O4 measured-lift gate" in report
    # The report is deterministic (same inputs → same bytes) — a recordable artifact.
    assert render_baseline_markdown(build_baseline({MODE_OFF: _off(), MODE_ON: _on()})) == (
        render_baseline_markdown(build_baseline({MODE_OFF: _off(), MODE_ON: _on()}))
    )

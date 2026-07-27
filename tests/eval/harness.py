"""Offline replay harness — the gate for the memory roadmap.

Deterministic, **model-free** metric machinery: given per-task metrics *recorded* from replaying
historical tasks on fixed models/prompts in each mode (memory-off / memory-on / memory-on-without-
entity-cards), it summarizes the metric stack, compares memory-off vs memory-on,
and renders a baseline report. Running the models to produce those records is **out of scope** here
(offline replay over fixed models/prompts); this is the aggregation + gate layer,
which is what stays deterministic and unit-testable.

The AC-O thresholds live here as constants so the baseline sets the targets and the same code that
records the baseline also decides whether a future phase has earned its measured lift.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Success thresholds. Provisional — locked once a real baseline exists.
AC_O1_MIN_REDUCTION = 0.10  # ≥10% fewer tokens OR less wall-clock on repeated-repo tasks
AC_O2_MIN_IMPROVEMENT = 0.10  # ≥10pp better first-pass review/test success on repeated hotspots
AC_O3_MAX_STALE_CONTRADICTION = 0.05  # stale-contradiction rate < 5%

MODE_OFF = "memory-off"
MODE_ON = "memory-on"
MODE_ON_NO_ENTITY = "memory-on-without-entity"


@dataclass(frozen=True)
class TaskMetrics:
    """One replayed task's recorded metrics, in one mode.

    ``repeated_repo`` / ``hotspot`` flag the efficiency / first-pass measurement subsets. The
    safety counters
    (``secret_leaks`` / ``external_only_promotions`` / ``stale_contradictions``) feed the safety
    verdict;
    ``active_records`` is the denominator for the stale-contradiction rate.
    """

    task_id: str
    tokens: int
    wall_clock_s: float
    first_pass_pass: bool
    fix_cycles: int = 0
    repeated_repo: bool = False
    hotspot: bool = False
    secret_leaks: int = 0
    external_only_promotions: int = 0
    stale_contradictions: int = 0
    active_records: int = 0


@dataclass(frozen=True)
class ModeSummary:
    """Aggregated metrics for one mode across its replayed tasks."""

    mode: str
    tasks: int
    mean_tokens: float
    mean_wall_clock_s: float
    first_pass_rate: float
    repeated_mean_tokens: float
    repeated_mean_wall_clock_s: float
    hotspot_first_pass_rate: float
    secret_leaks: int
    external_only_promotions: int
    stale_contradiction_rate: float


@dataclass(frozen=True)
class Comparison:
    """memory-off vs memory-on deltas + the verdicts. ``measured_lift`` is the roadmap gate."""

    token_reduction_pct: float
    wall_clock_reduction_pct: float
    hotspot_first_pass_improvement_pp: float
    meets_ac_o1: bool
    meets_ac_o2: bool
    meets_ac_o3: bool

    @property
    def measured_lift(self) -> bool:
        """Roadmap gate: a future phase ships only on a measured efficiency/quality win."""
        return self.meets_ac_o1 or self.meets_ac_o2


@dataclass(frozen=True)
class Baseline:
    """The recorded baseline: a summary per mode plus the off-vs-on comparison (when both exist)."""

    modes: tuple[ModeSummary, ...]
    comparison: Comparison | None


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize_mode(mode: str, tasks: Sequence[TaskMetrics]) -> ModeSummary:
    """Aggregate one mode's recorded tasks into a :class:`ModeSummary` (pure)."""
    repeated = [t for t in tasks if t.repeated_repo]
    hotspots = [t for t in tasks if t.hotspot]
    active = sum(t.active_records for t in tasks)
    return ModeSummary(
        mode=mode,
        tasks=len(tasks),
        mean_tokens=_mean([t.tokens for t in tasks]),
        mean_wall_clock_s=_mean([t.wall_clock_s for t in tasks]),
        first_pass_rate=_mean([1.0 if t.first_pass_pass else 0.0 for t in tasks]),
        repeated_mean_tokens=_mean([t.tokens for t in repeated]),
        repeated_mean_wall_clock_s=_mean([t.wall_clock_s for t in repeated]),
        hotspot_first_pass_rate=_mean([1.0 if t.first_pass_pass else 0.0 for t in hotspots]),
        secret_leaks=sum(t.secret_leaks for t in tasks),
        external_only_promotions=sum(t.external_only_promotions for t in tasks),
        stale_contradiction_rate=(
            sum(t.stale_contradictions for t in tasks) / active if active else 0.0
        ),
    )


def _reduction(before: float, after: float) -> float:
    return (before - after) / before if before else 0.0


def compare_modes(off: ModeSummary, on: ModeSummary) -> Comparison:
    """Compute the AC-O verdicts for memory-on relative to memory-off (pure)."""
    token_reduction = _reduction(off.repeated_mean_tokens, on.repeated_mean_tokens)
    wall_reduction = _reduction(off.repeated_mean_wall_clock_s, on.repeated_mean_wall_clock_s)
    hotspot_improvement = on.hotspot_first_pass_rate - off.hotspot_first_pass_rate
    return Comparison(
        token_reduction_pct=token_reduction,
        wall_clock_reduction_pct=wall_reduction,
        hotspot_first_pass_improvement_pp=hotspot_improvement,
        # A reduction in tokens OR wall-clock clears the bar.
        meets_ac_o1=max(token_reduction, wall_reduction) >= AC_O1_MIN_REDUCTION,
        meets_ac_o2=hotspot_improvement >= AC_O2_MIN_IMPROVEMENT,
        # Safety is a property of the memory-on run itself (its own safety counters).
        meets_ac_o3=(
            on.stale_contradiction_rate < AC_O3_MAX_STALE_CONTRADICTION
            and on.secret_leaks == 0
            and on.external_only_promotions == 0
        ),
    )


def build_baseline(
    modes: Mapping[str, Sequence[TaskMetrics]], *, off: str = MODE_OFF, on: str = MODE_ON
) -> Baseline:
    """Summarize every mode and, when both ``off`` and ``on`` are present, compare them."""
    summaries = tuple(summarize_mode(mode, tasks) for mode, tasks in modes.items())
    by_mode = {s.mode: s for s in summaries}
    comparison = (
        compare_modes(by_mode[off], by_mode[on]) if off in by_mode and on in by_mode else None
    )
    return Baseline(modes=summaries, comparison=comparison)


def render_baseline_markdown(baseline: Baseline) -> str:
    """Render the baseline as deterministic markdown (one table row per mode + the verdicts)."""
    lines = [
        "# Memory eval baseline",
        "",
        "| Mode | Tasks | Mean tokens | Mean wall-clock (s) | First-pass | Secret leaks |",
        "| --- | --: | --: | --: | --: | --: |",
    ]
    for s in baseline.modes:
        lines.append(
            f"| {s.mode} | {s.tasks} | {s.mean_tokens:.0f} | {s.mean_wall_clock_s:.1f} | "
            f"{s.first_pass_rate:.0%} | {s.secret_leaks} |"
        )
    comp = baseline.comparison
    lines.append("")
    if comp is None:
        lines.append("_No memory-off vs memory-on comparison (a required mode is missing)._")
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            "## memory-off vs memory-on",
            "",
            f"- token reduction (repeated-repo): {comp.token_reduction_pct:.0%}",
            f"- wall-clock reduction (repeated-repo): {comp.wall_clock_reduction_pct:.0%}",
            f"- first-pass improvement (hotspots): {comp.hotspot_first_pass_improvement_pp:+.0%}",
            f"- efficiency (≥10% tokens or wall-clock): {'PASS' if comp.meets_ac_o1 else 'FAIL'}",
            f"- first-pass (≥10pp on hotspots): {'PASS' if comp.meets_ac_o2 else 'FAIL'}",
            (
                f"- safety (stale<5%, 0 leaks, 0 external promotions): "
                f"{'PASS' if comp.meets_ac_o3 else 'FAIL'}"
            ),
            (
                f"- measured-lift gate (next phase unlocked): "
                f"{'YES' if comp.measured_lift else 'NO'}"
            ),
        ]
    )
    return "\n".join(lines) + "\n"

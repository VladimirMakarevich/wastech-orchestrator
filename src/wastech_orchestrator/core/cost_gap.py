"""What the recorded cost does **not** cover — named, never estimated.

Only one of the two shipped providers reports a dollar figure: Claude's stream-json terminal event
carries ``total_cost_usd``, and the Codex CLI emits no USD at all. So ``usage_cost`` is NULL for
every Codex attempt, and a total summed from that column understates the run in exact proportion to
how much of the flow ran on Codex — which is worst precisely where it matters most, since a flow
routes its heaviest analytical nodes there on purpose.

The answer is a stated gap, not a filled one. There is deliberately **no price table and no
estimate**: a price list in the adapter is a number that goes stale between releases and is then
read as fact, while "we do not know what these 13 attempts cost, and here is how big they were"
cannot be wrong. So every surface that reports a cost total reports this beside it.

A pure function over already-persisted attempt rows, like
:mod:`~wastech_orchestrator.core.supervisor_usage` next to it: the sentence is testable without a
run and cannot disagree with the database it came from.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from wastech_orchestrator.state_store import ProviderAttemptRow


@dataclass(frozen=True)
class CostGap:
    """One provider's unpriced attempts: how many there were, and how big they were.

    ``input_tokens`` is the summed normalized input of the unpriced attempts, or ``None`` when none
    of them reported usage either (a result-less fallback attempt, a killed run reconciled at a
    terminal). ``None`` rather than ``0``: a zero would read as "measured, and it was nothing",
    which is the same mistake as a silent zero cost.
    """

    provider: str
    attempts: int
    input_tokens: int | None


def cost_gaps(attempts: Sequence[ProviderAttemptRow]) -> tuple[CostGap, ...]:
    """The providers with attempts carrying no recorded cost, in provider-name order.

    Grouped per provider because that is what the operator can act on — the gap names a CLI that
    reports no USD, not an accounting error. An attempt is counted whenever ``usage_cost`` is NULL,
    which covers both halves of the same ignorance: the provider reported no dollars, or the
    per-run delta could not carry a cost forward. ``()`` when every attempt is priced, so a
    single-provider Claude run says nothing at all.
    """
    by_provider: dict[str, list[ProviderAttemptRow]] = {}
    for row in attempts:
        if row.usage_cost is None:
            by_provider.setdefault(row.provider, []).append(row)
    return tuple(
        CostGap(
            provider=provider,
            attempts=len(rows),
            input_tokens=_input_total(rows),
        )
        for provider, rows in sorted(by_provider.items())
    )


def render_cost_gap(gaps: Sequence[CostGap]) -> str | None:
    """The one-line operator sentence for *gaps*, or ``None`` when there is nothing to say.

    ``"Codex cost not accounted (13 attempts, 8.4 M input tokens)"`` — the provider that was not
    priced, what it ran, and how much of the run it was, so the number beside it is read as the
    partial figure it is. Several providers are joined with ``"; "``.
    """
    parts = [
        f"{gap.provider.capitalize()} cost not accounted "
        f"({gap.attempts} {'attempt' if gap.attempts == 1 else 'attempts'}, "
        f"{_tokens(gap.input_tokens)})"
        for gap in gaps
    ]
    return "; ".join(parts) if parts else None


def gap_json(gaps: Sequence[CostGap]) -> list[dict[str, object]]:
    """The same gaps as plain JSON for ``summary.json``: one object per provider."""
    return [
        {"provider": gap.provider, "attempts": gap.attempts, "input_tokens": gap.input_tokens}
        for gap in gaps
    ]


def _input_total(rows: Sequence[ProviderAttemptRow]) -> int | None:
    present = [row.usage_input_total for row in rows if row.usage_input_total is not None]
    return sum(present) if present else None


def _tokens(count: int | None) -> str:
    """``"8.4 M input tokens"`` — magnitude, not precision; ``"input tokens not measured"``.

    The sentence exists to convey scale (is this rounding error or a quarter of the run?), and a
    nine-digit exact figure conveys it worse than one decimal place does.
    """
    if count is None:
        return "input tokens not measured"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f} M input tokens"
    if count >= 1_000:
        return f"{count / 1_000:.1f} k input tokens"
    return f"{count} input tokens"

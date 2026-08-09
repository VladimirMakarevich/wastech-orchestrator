# P1.6 — make the cited line authoritative, and route the citation report to the verifier

Priority: **P1** Status: **implemented** (2026-07-26) Date: 2026-07-25 Source: [postmortem.md](postmortem.md) DR-5

## Implemented

All five items. Step 1 took the `weak` option rather than dropping the `or` outright — the decision was made explicitly against the alternative, whose cost is concrete: the `citation_check → synthesis` fail edge has `budget: 1`, so one imprecise line number in one of N citations spends the single repair round and parks the whole run in `manual_action_required`. `weak` also gives the measurement needed to decide later whether it should gate; the flip is a one-line change. The reason string names the line the snippet is actually on, so a repair is possible in one round.

Three refinements the document did not specify:

- **A snippet with no `line` stays `verified`.** Such an entry claims nothing about _where_ in the file, so there is no location to mis-attribute and "this quote is in this file" is the whole claim. Only a cited line that disagrees with the snippet is `weak`.
- **Multi-line snippets match a window as tall as the snippet**, not `lines[line - 1]`. Quoting a three-line signature is ordinary citation craft, and a single-line match would have made every multi-line quote a mis-attribution.
- **A non-`int` `line` (e.g. `"12"`) reads as "no line cited"** and is echoed back as `null`, rather than silently skipping both the bounds check and the on-line check as before.

`citation.json` entries now also carry `path` and `line`, because the whole point of routing the file to the verifier is that it can act on an entry without recovering the location from prose. `verifier.md`'s false assurance is replaced (P1.6 half only — the full rewrite is [P1.5](p1-5-research-role-prompts.md)) and it now addresses `{checks_path}` and owns the `weak`/`uncheckable` entries. `synthesis.md` is updated too: the `line` must be the line the snippet is on, and an entry with no snippet cannot be verified at all.

## Problem

`citation_check` does real work — it catches a nonexistent file, an out-of-range line, and a snippet absent from the cited file — but the line number is decorative and the claim is never validated. It cannot catch the hallucination mode that matters in an audit: a true snippet attached to a false claim.

Separately, its 5 403-byte verdict file reaches nobody, and the verifier's prompt asserts a guarantee based on it that the verifier cannot audit.

## Evidence

[`core/flow/checkers/citation.py:140-141`](../../../../src/wastech_orchestrator/core/flow/checkers/citation.py):

```python
on_line = isinstance(line_no, int) and snippet.strip() in lines[line_no - 1]
if not (on_line or snippet.strip() in text):
```

The `or` means the line number is only bounds-checked. A fabrication battery run through the real `validate_citations()`:

| Fabrication | Result |
| --- | --- |
| correct snippet, wrong in-range line (cited 3, actually 205) | `verified` |
| `path` + `line`, no snippet | `verified` |
| real snippet + real line, claim entirely fabricated | `verified` |
| snippet `"import"`, claim "the engine is fully async end to end" | `verified` |
| snippet from a different file than the cited path | `broken` |
| line number out of range | `broken` |
| fabricated external URL | `uncheckable` |

It did not bite on `p9-09` — all 39 in-repo citations re-resolve byte-exactly at the cited line — but that is a property of the synthesis node's care, not of the gate.

The routing half: `checks.py:150-151` writes `citation.json` privately, and `checks_path` is set only on the failure path (`_publish_first_failure_log`, `checks.py:120-137`). On a pass it stays `None`, so `build_path_context` omits it. `fact_verification`'s `context_paths` in `request.json` is `{task_path, diff_path}` only — while its prompt claims the check "has already confirmed" the locations.

## Change

1. **Make the cited line authoritative.** Drop the `or` fallback, or keep it and emit a distinct `weak` status when the snippet is present in the file but not on the cited line. A `weak` status is the safer first move: it surfaces mis-attribution without failing runs whose citations are merely imprecise, and it gives the verifier something concrete to chase.
2. **A missing snippet is `uncheckable`, not `verified`.** `citation.py:138` currently passes an entry that carries only `path` + `line`. It has not been checked; label it accordingly.
3. **Publish `citation.json` and set `checks_path` on both outcomes**, so the verifier receives the per-entry verdicts it is told to rely on. `checks.py:160` already registers it as an artifact; only the pass-path publish is missing.
4. **Stop hardcoding the manifest filename.** [`checks.py:147`](../../../../src/wastech_orchestrator/core/flow/nodes/checks.py) resolves `(report_dir or checks_dir) / "sources.json"` — a literal. A flow whose synthesis node names its manifest anything else gets `uncheckable: sources.json missing` and a gate that silently does nothing. Make it a checks-node field defaulting to `sources.json`, so an operator-authored flow can point the checker at its own manifest. (Added from the [P2.9](p2-9-deliverable-containment.md) review: this is the only place in the engine where a deliverable's filename genuinely must be known, and it is the whole declarative surface this area needs.)
5. Leave the `claim` field unvalidated by the deterministic checker — validating a claim is the verifier's job, not a checker's. But once (3) lands, the verifier can be told which entries are `weak`/`uncheckable` and made responsible for them ([P1.5](p1-5-research-role-prompts.md) items 1–3).

## Acceptance

- A citation whose snippet exists in the file but not at the cited line is reported as `weak` (or `broken`, per the decision in step 1), not `verified`.
- A citation with no snippet is `uncheckable`.
- `fact_verification`'s `context_paths` contains a `checks_path` pointing at `citation.json` on a passing run.
- The verifier prompt's description of the guarantee matches what the checker returns.

## Test

Unit table over `validate_citations()` covering the seven battery rows above, pinned as regressions. Integration: a passing `citation_check` sets `checks_path`, and the next evaluator's rendered prompt contains the pointer.

## Scope / risk

Orchestrator default, all flows using the `citation` checker. Risk: tightening the line rule could fail existing well-intentioned reports whose line numbers drifted by a line or two. That is the argument for the `weak` status over a hard `broken` — ship `weak` first, measure how often it fires, then decide whether it should gate.

## Depends on

Nothing hard. Pairs with [P1.5](p1-5-research-role-prompts.md), which is where the verifier learns to act on the new statuses.

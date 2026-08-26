Investigate the **plan of record and the test suite** of the project at `{repo}`: whatever defines what "correct" and "done" mean here — a roadmap, requirements, design decisions, a glossary, the user-facing guide — and what the tests actually exercise.{?refinement_path} Work from the refined brief at {refinement_path} — cover every sub-question in it that falls inside this remit.{/refinement_path}

This is the **last of three** analysis passes over disjoint surfaces: core → entry points and adapters → plan of record and tests (yours). Your job is the plan-versus-implementation and plan-versus-tests comparison; the code-level walk of the other two surfaces is already done.{?analysis_core_path} The core pass reported at {analysis_core_path}.{/analysis_core_path}{?analysis_surfaces_path} The surfaces pass reported at {analysis_surfaces_path}.{/analysis_surfaces_path} Read both before you start and build on them instead of repeating them.

## Coverage is measured, not assumed

A gate downstream re-derives this remit's file list from the repository and compares it against your report, so:

1. **Enumerate first.** `Glob` the remit's files before reading any of them, and keep that list — it is your denominator. Per-unit index pages and decision records are usually where the exit criteria actually live, and they are the easiest set to skim past.
2. **Open what you enumerated**, largest and least familiar first. A file you never opened supports no finding — and it supports no "no findings" either. Skipping one is allowed; skipping it silently is not.
3. **One traced property per unit.** For each unit in scope (phase / milestone / epic / requirement group), name something you actually traced: an exit criterion followed to the code that satisfies it, a requirement followed to its implementation, a stated coverage priority compared against the tests that exist. A bare "walked, no findings" label is an unfinished pass, not a result.

Record an exact `path:line` for every observation you intend to make a claim about — these become the citations the synthesis has to anchor, so they must point at text that is really there.

**Every finding is a pattern, not an instance.** Before you record one, grep the corpus for the whole class and record every site — same defect shape, sibling file, second call site, other implementation of the same rule. This flow's first production run filed one inert-option defect while a worse one sat in a sibling file, one duplicate-detection bug while exact duplicates sat in another rule, and one unsafe-regex defect 170 lines from the correct escaping helper. A finding that names one site when five exist understates its own severity and lets four of them ship.

## What to look for

1. **Exit-criteria coverage.** For each unit, read its stated exit criteria and confirm the corresponding code exists and behaves as specified. Note anything marked done in the plan but thin, stubbed, or missing in code.
2. **Requirement conformance.** Trace each cited requirement to its implementation. Note silent divergences and undocumented scope cuts.
3. **Cross-unit gaps.** Follow whatever dependency chains the project tracks between units, and look for the specific failure mode: an earlier unit that needed something from a later one, was completed with a placeholder or partial seam, and was never revisited once the later unit shipped.
4. **Test adequacy — both directions.** Compare each unit's stated coverage priorities against what the tests actually exercise, and also look for a load-bearing claim with no test at all. A test that asserts the mock rather than the behaviour counts as absent.
5. **Documentation drift.** A documented flag, default, path or workflow that the code no longer implements — and the reverse, shipped behaviour the plan never records.
6. **Delivery evidence.** Where the history is reachable **with the tools you were actually granted** (`git log` / `git show` need a shell), a unit marked complete by a change that did less than it claims is a prime finding. Whether you have one depends on how this run is configured, so check rather than assume: if you do not, say so and drop the claim — do not grep a changelog and present that as history.

{?review_path}

## Gaps to close on this pass

A coverage gate reviewed an earlier analysis round; its findings are at {review_path}. Close every gap it names that falls inside this remit — those files and properties first, ahead of anything else — and do not re-derive what the earlier round already covered.{/review_path}

Read only; do not edit code or write files. **Your report is your final message** — it is persisted as this node's output and is all that later nodes and the coverage gate receive, so it must carry the whole analysis plus a closing `## Coverage` section: what you enumerated, what you opened, what you deliberately skipped and why, and the traced property per unit.

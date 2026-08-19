Investigate the **core** of the project at `{repo}` for evidence bearing on the research question: the modules that implement its central logic, the rules and invariants it claims to uphold, its configuration and data model, and the internal wiring (graph / pipeline / dispatch) that joins them.{?refinement_path} Work from the refined brief at {refinement_path} — cover every sub-question in it that falls inside this remit.{/refinement_path}

This is the **first of three** analysis passes over disjoint surfaces: core (yours) → entry points and adapters → plan of record and tests. The remit is mandatory and narrow: do not audit the command-line entry points, the servers/adapters, generated schemas, requirement documents, or the test suite. A later pass owns each of those, and budget spent there is depth the surface that needs it never gets.

## Coverage is measured, not assumed

A gate downstream re-derives this remit's file list from the repository and compares it against your report, so:

1. **Enumerate first.** `Glob` the remit's files before reading any of them, and keep that list — it is your denominator.
2. **Open what you enumerated**, largest and least familiar first. A file you never opened supports no finding — and it supports no "no findings" either. Skipping one is allowed; skipping it silently is not.
3. **One traced property per subsystem.** For each subsystem in the remit, name a property you actually followed through the code: an invariant checked against the code that is supposed to uphold it, an ordering or determinism claim traced, an error path walked. A bare "walked, no findings" label is an unfinished pass, not a result.

Record an exact `path:line` for every observation you intend to make a claim about — these become the citations the synthesis has to anchor, so they must point at text that is really there.

**Every finding is a pattern, not an instance.** Before you record one, grep the corpus for the whole class and record every site — same defect shape, sibling file, second call site, other implementation of the same rule. This flow's first production run filed one inert-option defect while a worse one sat in a sibling file, one duplicate-detection bug while exact duplicates sat in another rule, and one unsafe-regex defect 170 lines from the correct escaping helper. A finding that names one site when five exist understates its own severity and lets four of them ship.

## What to look for

- **Invariants.** Where the project documents its own rules (`.agents/rules/`, `CLAUDE.md`, `AGENTS.md`, an architecture document), check whether the core code actually upholds each one rather than assuming it does.
- **Weak code.** Fragile logic, non-deterministic ordering, unhandled error paths, TODO/FIXME markers, and abstractions built ahead of a concrete need.
- **Configuration and data model.** Defaults that are unsafe or unreachable, validation that does not validate, a declared shape the rest of the code contradicts.
- **Delivery evidence.** Where the history is reachable **with the tools you were actually granted** (`git log` / `git show` need a shell), a change that did less than it claims is a prime finding. Whether you have one depends on how this run is configured, so check rather than assume: if you do not, say so and drop the claim — do not grep a changelog and present that as history.

{?review_path}

## Gaps to close on this pass

A coverage gate reviewed an earlier analysis round; its findings are at {review_path}. Close every gap it names that falls inside this remit — those files and properties first, ahead of anything else — and do not re-derive what the earlier round already covered.{/review_path}

Read only; do not edit code or write files. **Your report is your final message** — it is persisted as this node's output and is all that later nodes and the coverage gate receive, so it must carry the whole analysis plus a closing `## Coverage` section: what you enumerated, what you opened, what you deliberately skipped and why, and the traced property per subsystem.

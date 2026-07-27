Investigate the **surfaces** of the project at `{repo}` — everywhere it meets something outside itself: command-line entry points and their argument/exit contracts, public APIs and servers, packaging and installation (how the executable is resolved once installed), adapters and integrations with external tools, discovery/registration, and any generated or declared schema.{?refinement_path} Work from the refined brief at {refinement_path} — cover every sub-question in it that falls inside this remit.{/refinement_path}

This is the **second of three** analysis passes over disjoint surfaces: core → entry points and adapters (yours) → plan of record and tests. The remit is mandatory and narrow: the core logic, the requirement documents, and the test suite belong to the other two passes.{?analysis_core_path} Do not re-read what the core pass already walked — its report is at {analysis_core_path}.{/analysis_core_path}

## Coverage is measured, not assumed

A gate downstream re-derives this remit's file list from the repository and compares it against your report, so:

1. **Enumerate first.** `Glob` the remit's files before reading any of them, and keep that list — it is your denominator. Entry points hide in package manifests, `bin` declarations, install scripts, and generated artifacts, not only in source directories.
2. **Open what you enumerated**, largest and least familiar first. A file you never opened supports no finding — and it supports no "no findings" either. Skipping one is allowed; skipping it silently is not.
3. **One traced property per subsystem.** For each surface in the remit, name a property you actually followed end to end: an invocation traced from the declared entry point to the code that answers it, an input followed from the boundary to where it is validated, a schema compared against the code that produces and consumes it. A bare "walked, no findings" label is an unfinished pass, not a result.

Record an exact `path:line` for every observation you intend to make a claim about — these become the citations the synthesis has to anchor, so they must point at text that is really there.

**Every finding is a pattern, not an instance.** Before you record one, grep the corpus for the whole class and record every site — same defect shape, sibling file, second call site, other implementation of the same rule. This flow's first production run filed one inert-option defect while a worse one sat in a sibling file, one duplicate-detection bug while exact duplicates sat in another rule, and one unsafe-regex defect 170 lines from the correct escaping helper. A finding that names one site when five exist understates its own severity and lets four of them ship.

## What to look for

- **Does the documented invocation actually work?** Follow the whole path an installed user takes — the manifest's declared entry point, the file it resolves to, its shebang/executable bit, what it exports and what a launcher would execute, the exit codes. A declared entry point that silently does nothing is the kind of defect that survives every test suite.
- **Boundary input.** Every path, glob, pattern, identifier or free text that crosses in from outside: where is it validated, and what happens when it is hostile — a path escaping the intended root, an unbounded read, text interpolated into a command line or a regular expression.
- **Contract conformance.** A generated or declared schema versus the code producing and consuming it; a documented flag or option versus the parser.
- **Adapter agreement.** Where two adapters or backends implement the same capability, differences that make behaviour depend on which one runs.
- **Delivery evidence.** Where the history is reachable **with the tools you were actually granted** (`git log` / `git show` need a shell), a change that did less than it claims is a prime finding. With no shell, say so and drop the claim — do not grep a changelog and present that as history.

{?review_path}

## Gaps to close on this pass

A coverage gate reviewed an earlier analysis round; its findings are at {review_path}. Close every gap it names that falls inside this remit — those files and properties first, ahead of anything else — and do not re-derive what the earlier round already covered.{/review_path}

Read only; do not edit code or write files. **Your report is your final message** — it is persisted as this node's output and is all that later nodes and the coverage gate receive, so it must carry the whole analysis plus a closing `## Coverage` section: what you enumerated, what you opened, what you deliberately skipped and why, and the traced property per surface.

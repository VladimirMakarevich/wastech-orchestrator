# Content-flow token hygiene (session-scope cost docs + packaged content-flow defaults)

**Status:** open **Priority:** P1 **Source:** [2026-07-16 token analysis](../analysis/2026-07-16-blog-review-happy-in-my-misfortunes-4-token-analysis.md) (F2 doc-follow-up, F3, F5)

Two small, low-risk changes that reduce raw token growth on content flows without touching the engine. Both are safe to ship independently.

## Part A — warn about the token cost of resumed sessions in the docs

The session-scope docs describe `session_scope` / `lineage_affinity` / `resume` purely as a context-continuity feature and contain **no** warning that resuming a session inherits the full accumulated history and grows input tokens on every subsequent model turn. This is exactly what surprised the operator in the analyzed run: `polish` (declared `lineage_affinity: revise`) re-ran a 31–37k-token transcript four times for a one-word edit.

- Add an explicit token-cost / history-growth note to the session-scope docs: `resume`, `editing_lineage` and `lineage_affinity` carry the shared session's history into the next stage — it preserves context but increases input token usage on every following model turn, so a shared session should be used deliberately, especially between semantically different stages.
- Locations: `docs/flow-authoring.md` ("Editing sessions and lineages", ~124-141) and the shipped operator guide `src/wastech_orchestrator/packaged/guide/flows/reference.md` (session_scope / lineage_affinity rows) + `packaged/guide/flows/README.md`.

## Part B — fix the packaged `blog_article_revise` defaults

`blog_article_revise` is a **packaged built-in** (`src/wastech_orchestrator/packaged/flows/blog_article_revise.yaml`), so its defaults ship from this repo. Two field-safe tweaks:

1. **Break the `revise → polish` lineage.** `polish` is the terminal independent editor; it already receives the diff and reviewer findings as artifacts and has no downstream rework, so it has no reason to own a durable editing lineage. Change its `session_scope` to `fresh_disposable` (drop `lineage_affinity: revise`). Keep `lineage_affinity` only where a rework loop genuinely benefits (e.g. `fixing`). Estimated saving on a comparable pass: ~60–80k raw input tokens (exact number needs A/B, since a fresh session may re-read mandatory rules).
2. **Add an operational read/patch budget to the role prompts.** The packaged role prompts emphasize scope minimalism but carry no operational budget; the run showed the agent re-reading full documents across six model turns for four edits. Add compact guidance to the `blog_article_revise` `revise`/`polish` role prompts (`packaged/flows/blog_article_revise/*.md`): read the brief, target article and findings in one parallel batch; do not re-read a file already present in the session; apply one focused patch, inspect one final diff, then stop. This is guidance, not a hard token limit.

## Acceptance criteria

- [ ] Session-scope docs (`flow-authoring.md` + packaged guide) explicitly state that resume/`editing_lineage`/`lineage_affinity` inherit session history and grow per-turn input tokens.
- [ ] Packaged `blog_article_revise.yaml` `polish` node is `fresh_disposable` (no `lineage_affinity`), and this is reflected in any flow reference docs.
- [ ] Packaged `blog_article_revise` `revise`/`polish` role prompts carry the read-once / one-patch / one-diff operational budget.
- [ ] Docs and packaged copies stay in sync (`/sync-docs`); prose is prettier-formatted (`proseWrap: never`).

## Out of scope

- The operator's already-installed copy in the target repo's `.worc/` — changing the packaged default only affects **future** installs / re-seeds; the copy that actually ran the analyzed task lives in `WastimeApp/.worc/` and is operator-owned. Note this in the change.
- model/effort tuning (`revise xhigh→high`, cheaper Claude roles) — a configuration/A-B decision on the target flow, not a packaged-default code change here.
- Conditional / machine-readable `needs_research` branching — needs new engine machinery; explicitly not part of this task.

## Likely implementation areas

- `docs/flow-authoring.md`
- `src/wastech_orchestrator/packaged/guide/flows/{reference.md,README.md}`
- `src/wastech_orchestrator/packaged/flows/blog_article_revise.yaml`
- `src/wastech_orchestrator/packaged/flows/blog_article_revise/*.md`

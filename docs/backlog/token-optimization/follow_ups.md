# Token optimization campaign — follow-ups

The campaign's running list of things that outlive the item that produced them: open decisions, watch items, operational consequences, and deliberate non-goals. Each item's own document stays the record of what it did and why; this file exists so the residue does not have to be rediscovered by re-reading five documents.

**Empty on creation.** The two shipped items (`normalized-usage-accounting`, `content-flow-token-hygiene`) pre-date this file and recorded their residue in their own documents; seed this one from the next item to land — P0, whose acceptance review (P0-D1…D7) already names several forks that will leave something behind.

How to use it:

- Add an entry when an item lands and leaves something behind that a later reader would otherwise have to rediscover. Name the item it came from, in parentheses, in the heading — `(P0)` — so the source is one click away.
- Keep an entry only while it can still cost something. **Delete it when it is done or has become untrue**, rather than marking it resolved — this is a live list, not a changelog.
- Per-item detail that is too long for a shared list goes in a sibling `<item-id>/follow_ups.md`, with a short pointer here.

Sections:

- [Needs a decision](#needs-a-decision) — someone has to answer before it can close
- [Watch items](#watch-items) — nothing to do unless the world changes
- [Operational consequences](#operational-consequences) — true of every target repo, not of the code
- [Carried into `main`](#carried-into-main) — the derived-docs refresh backlog
- [Deliberate non-goals](#deliberate-non-goals) — decided against; recorded so they are re-decided, not re-discovered

## Needs a decision

_Nothing yet._

## Watch items

_Nothing yet._

Note for whoever writes the first entry here: this campaign's headline numbers are **measured**, so its watch items are mostly "does this number still hold". The A/B all of them are gated on (see the README) has still not been run — that is an open task in the campaign, not residue, and belongs in the item docs rather than here.

## Operational consequences

_Nothing yet._

Likely first entries, once items land: whether a target repo running its own `.worc/flows/` copy or its own `config.yaml` supervisor block picks up the new defaults, or keeps its old cadence silently.

## Carried into `main`

The `docs/` tree is reconstructed on `main` from the merged `dev` diff as a separate task. Doc-impact notes accumulate here so that reconstruction has breadcrumbs rather than a bare diff.

_Nothing yet._

## Deliberate non-goals

Decided against, not overlooked. Both the acceptance review of 2026-07-26 and the campaign's own invariant — the supervisor stays **advisory only**, it never routes — will generate entries here; record the specific shape that was refused, not the invariant again.

_Nothing yet._

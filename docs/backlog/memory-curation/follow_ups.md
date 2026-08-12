# Memory curation campaign — follow-ups

The campaign's running list of things that outlive the item that produced them: open decisions, watch items, operational consequences, and deliberate non-goals. Each item's own document stays the record of what it did and why; this file exists so the residue does not have to be rediscovered by re-reading nine documents.

**Empty by design right now** — every item (P0.1…P3.9) is still `proposed`, so nothing has landed to leave residue behind. Seed it from the first item that merges.

How to use it:

- Add an entry when an item lands and leaves something behind that a later reader would otherwise have to rediscover. Name the item it came from, in parentheses, in the heading — `(P0.1)` — so the source is one click away.
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

## Operational consequences

_Nothing yet._

Likely first entries, once items land: what an existing `.worc/memory/` store needs done to it (the audit found the damage happens at **write** time, so already-written records do not repair themselves), and whether `worc memory compact` has to be re-run against it.

## Carried into `main`

The `docs/` tree is reconstructed on `main` from the merged `dev` diff as a separate task. Doc-impact notes accumulate here so that reconstruction has breadcrumbs rather than a bare diff.

_Nothing yet._

## Deliberate non-goals

Decided against, not overlooked. The campaign-wide constraint in [README.md](README.md) — the model proposes, deterministic code decides, and nothing runs inside an active task — is the source of most entries this section will collect; record the specific shape that was refused, not the constraint again.

_Nothing yet._

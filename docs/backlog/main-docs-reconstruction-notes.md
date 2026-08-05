# Doc-impact notes for the `main` docs reconstruction

Status: **empty — awaiting the next `dev` cycle's notes** Date: 2026-08-05 Owner: Vladimir Makarevich

`dev` deliberately carries no derived documentation: the descriptive documents (`worc_architecture.md`, `configuration.md`, `cookbook.md`, `glossary.md`, `operations.md`, the site) live on `main` and are reconstructed there from the merged `dev` diff as a separate task ([.agents/rules/git-workflow.md](../../.agents/rules/git-workflow.md) §A). AGENTS.md therefore asks each `dev` change to leave a one-line doc-impact note instead of creating those files.

This is where those notes accumulate. It exists because a bare diff does not say _which page now contradicts the code_, and because the campaign folders that collected the notes are deleted once their items land — the reconstruction task runs later than that.

**How to use it:** work top-down, and delete a section once the reconstruction has consumed it. A note that survives its reconstruction is worse than no note.

## Consumed

Every note accumulated through 2026-08-04 was consumed by the 2026-08-05 reconstruction — the `deep_research` post-mortem campaign (P0.1 … P3.10) and its follow-ups walkthrough, runtime artifact retention + the private-home footprint, the supervisor off switch + the one deterministic PR body, the `tool`-node UTF-8 stdout contract, and the four critical defects from the P11/P12 post-mortem (C1–C4).

Add the next cycle's notes below this line, one section per campaign or dated change.

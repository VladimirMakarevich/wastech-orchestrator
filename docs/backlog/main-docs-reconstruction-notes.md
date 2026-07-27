# Doc-impact notes for the `main` docs reconstruction

Status: **inventory** Date: 2026-07-27 Owner: Vladimir Makarevich

`dev` deliberately carries no derived documentation: the descriptive documents (`worc_architecture.md`, `configuration.md`, `cookbook.md`, `glossary.md`, `operations.md`, the site) live on `main` and are reconstructed there from the merged `dev` diff as a separate task ([.agents/rules/git-workflow.md](../../.agents/rules/git-workflow.md) §A). AGENTS.md therefore asks each `dev` change to leave a one-line doc-impact note instead of creating those files.

This is where those notes accumulate. It exists because a bare diff does not say _which page now contradicts the code_, and because the campaign folders that collected the notes are deleted once their items land — the reconstruction task runs later than that.

**How to use it:** work top-down, and delete a section once the reconstruction has consumed it. A note that survives its reconstruction is worse than no note.

## `deep_research` post-mortem campaign (P0.1 … P3.10, 2026-07-25 → 2026-07-27)

- **P1.4 — audit coverage gate.** The node-output channel now spans evaluators (`configuration.md`, the flow-authoring page's prompt-variable table); `deep_research`'s graph gained three nodes and a gate (`worc_architecture.md`, `cookbook.md`).
- **P1.4a — read-only git evidence.** A new `security.*` key and a new per-node flow field (`configuration.md`, the flow-authoring page); and **the `read-only` permission profile no longer implies "no shell"** (`worc_architecture.md`, `glossary.md`) — that last one is a definition change, not an addition.
- **P2.8 — node output handoff.** A new per-node flow field `output_file` (`configuration.md`, the flow-authoring page's node-field table); the node-output channel can now carry a produced document rather than the node's message (`worc_architecture.md`'s handoff description, `glossary.md` if it defines the channel); `citation.json` gained `manifest_path` (`configuration.md`'s checker section).
- **P2.9 — deliverable containment.** No engine change, but the `repository_document` story changes in prose: the structuring node writes nothing and the deliverable directory holds only the deliverable (`cookbook.md`, `worc_architecture.md`).
- **P3.10 — flow and config hygiene.** `deep_research`'s graph gained a `command_profile` gate and lost `refinement`'s predicate (`worc_architecture.md`, `cookbook.md`); and the two `when:` facts are now documented as what they actually resolve (`configuration.md` — the one an operator is most likely to misread).

### From the 2026-07-27 follow-ups walkthrough

One behavior change and four shipped-doc clarifications. The derived tree currently **contradicts** the first one, so it is not merely missing:

- **WRI-009 no longer always parks.** Git control-state drift on a `read-only` node holding the git-evidence grant now warns and continues; every other profile still parks. `worc_architecture.md`'s WRI-009 description states the terminal outcome unconditionally, and `glossary.md` must not list control-state drift as an unqualified `manual_action_required` trigger. The signal is `NodeOutcome.read_only_git_drift` (carrying the redacted aspect summary, not a bool) and a third synthetic trace label `TRACE_READ_ONLY_GIT_DRIFT` joined `TRACE_REWORK_EXHAUSTED` / `TRACE_READ_ONLY_WRITE` — wherever the ⚠️ trace labels are enumerated, that list is now short one.
- **`security.allow_git_evidence` is a grant switch, not a kill switch.** With it off, a Codex `read-only` node still reads git history, so the provider asymmetry persists (`configuration.md`'s security section).
- **`skip_if_unavailable` is not an escape hatch.** Skipping the only selected set parks the task exactly as a launch failure would; disabling the node per task is the escape (`configuration.md`'s checks section).
- **The single-root "one catch-all set" recommendation is incomplete** once a flow produces documents: the catch-all fires on a Markdown-only diff too (`configuration.md`, and `cookbook.md` wherever it shows a first `command_sets`).
- **The `--allowedTools` deny-direction fact is now written down for operators** — verified `claude` version, the two probes to repeat on a new major, and the sandbox-only fallback (the flow-authoring page's git-evidence section, mirroring the shipped `guide/flows/reference.md`).

Critically review the report at `{repo}/docs/research/{task_id}/report.md` for gaps, weak reasoning, missing alternatives, and overstated conclusions. You keep your own session across rounds, so do not repeat a point you already raised — track what was addressed and focus on what remains.

For a **plan-vs-implementation audit**, press hardest on completeness and calibration:

- **Coverage.** Was every unit in scope (phase/milestone/epic) actually examined, or did the report go deep on a few and wave at the rest? Were each unit's exit criteria and cited requirements checked, not just its code skimmed?
- **Cross-unit gaps.** The whole point of this audit is to catch earlier work left partial because it depended on later work and was never revisited. Were the dependency chains the project tracks actually traversed, or asserted without tracing?
- **Invariant drift.** If the project documents its own architecture invariants, were they each considered, or silently skipped?
- **Calibration.** Is any conclusion stronger than its evidence? Are remediations concrete and pointed at a real location, or vague? Is anything the plan explicitly deferred or scoped out being reported as a defect?
- **Actionability.** Could a maintainer act on each finding without re-doing the investigation?

Read only; do not edit. Return findings in the output schema — severity medium or high marks a substantive weakness that should be reworked. This is a non-blocking pass: when your remaining concerns are minor or the rework budget is spent, accept and let them carry into the report's Open questions.

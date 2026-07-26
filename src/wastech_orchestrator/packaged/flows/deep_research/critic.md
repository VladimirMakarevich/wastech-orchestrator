Critically review the report at `{repo}/docs/research/{task_id}/report.md` for gaps, weak reasoning, missing alternatives, and overstated conclusions. You keep your own session across rounds, so do not repeat a point you already raised — track what was addressed and focus on what remains.

For a **plan-vs-implementation audit**, press hardest on completeness and calibration:

- **Coverage.** Was every unit in scope (phase/milestone/epic) actually examined, or did the report go deep on a few and wave at the rest? Were each unit's exit criteria and cited requirements checked, not just its code skimmed?
- **Cross-unit gaps.** The whole point of this audit is to catch earlier work left partial because it depended on later work and was never revisited. Were the dependency chains the project tracks actually traversed, or asserted without tracing?
- **Invariant drift.** If the project documents its own architecture invariants, were they each considered, or silently skipped?
- **Calibration.** Is any conclusion stronger than its evidence? Are remediations concrete and pointed at a real location, or vague? Is anything the plan explicitly deferred or scoped out being reported as a defect?
- **Actionability.** Could a maintainer act on each finding without re-doing the investigation?

Read only; do not edit. Return findings in the output schema, each with an honest `severity` (blocking / critical / high / medium / low) reflecting how serious the weakness is. You do not author the verdict — the flow decides which severities force another round, so do not inflate or downplay to force an outcome. File everything you find at its true severity: a finding below the gate is not discarded, it is carried to the operator in the run summary and the pull-request body. This is a non-blocking pass, so a spent rework budget means the flow accepts and continues with your open findings recorded — it never parks the task. A report you have no substantive concerns about returns an empty `findings` array.

Verify the report at `{repo}/docs/research/{task_id}/report.md` against its evidence. The deterministic citation check has already confirmed that cited locations exist; your job is to judge whether each claim is actually **supported** by what it cites and whether the conclusions follow.

For a **plan-vs-implementation audit**, a claim usually asserts that shipped code diverges from — or falls short of — the plan of record. Verify _both_ sides against `{repo}`:

- The **code** side: open the cited `path:line` and confirm it really says what the finding claims, in the way the finding claims.
- The **plan** side: open the cited plan document (requirement, decision, invariant, or roadmap entry) and confirm the standard the finding measures against is stated there and read correctly — not paraphrased into something stronger.

Watch for: severity inflated beyond what the evidence supports; a "gap" that a later unit or a recorded deferral actually closes or intentionally scopes out; and a suspected issue presented as confirmed.

Read only; do not edit. Return findings in the output schema, each with an honest `severity` (blocking / critical / high / medium / low) reflecting how badly the claim is unsupported, misstated, or misclassified. You do not author the verdict — the flow decides which severities force another round, so do not inflate or downplay to force an outcome. File everything you find at its true severity: a finding below the gate is not discarded, it is carried to the operator in the run summary and the pull-request body. This is a non-blocking pass, so an unresolved concern belongs in `findings`, never in prose — and a spent rework budget means the flow accepts and continues rather than parking the task. A report whose claims all hold up returns an empty `findings` array.

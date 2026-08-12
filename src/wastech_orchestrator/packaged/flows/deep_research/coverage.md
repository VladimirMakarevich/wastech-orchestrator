Judge whether the repository analysis actually **covered** the scope this task declared. You are not reviewing whether its findings are right — a later pass does that. You measure the audit rather than reading it, and you are the only step that does.

The analysis ran as up to three passes over disjoint surfaces. Read every report you were handed:

{?analysis_core_path}- core (central logic, rules and invariants, configuration, internal wiring): {analysis_core_path}
{/analysis_core_path}{?analysis_surfaces_path}- entry points and adapters (command line, APIs, packaging, integrations, generated schemas): {analysis_surfaces_path}
{/analysis_surfaces_path}{?analysis_docs_tests_path}- plan of record and the test suite: {analysis_docs_tests_path}
{/analysis_docs_tests_path}
A pass whose report is missing was skipped or did not run: say so as a finding and judge the rest.

The **declared scope** is what the task states (read it at {task_path}) — the subsystems, files, phases or components it names — plus whatever the reports themselves claim to have covered. Judge against what is declared and nothing more: a task that scopes itself narrowly is not penalised for it, while a report that declares a subsystem and then does not cover it is exactly what you are here for.

For every declared subsystem, check two things:

1. **It was really read.** Re-derive its file set from `{repo}` yourself (`Glob`) and compare it with what the report says it opened. A file named verbatim in the task and never opened is the failure this gate exists to catch; so is a subsystem graded "walked" with no per-file reads on record, and so is a coverage section that reports a total without naming what it skipped.
2. **It shows a traced property.** Something followed through the code end to end — an invariant checked against the code that upholds it, an ordering or determinism claim traced, an input followed from a boundary to its validation, an exit criterion followed to the code that satisfies it. A bare "no findings", a restatement of what the code is for, or a summary of the file layout is not a traced property.

File one finding per subsystem that fails either check. Name the subsystem and the specific file set or property that is missing, so the next round has something bounded to do — "coverage is uneven" is not actionable, "these 9 files under the CLI package were enumerated and never opened, and no entry-point invocation was traced" is. A subsystem that passes both checks needs no finding, and a scope that was genuinely covered end to end returns an empty `findings` array.

Do not use this pass to re-litigate the analysis: a finding you disagree with, a severity you would rate differently, or a conclusion you would word another way all belong to the later review, not here. Missing coverage is your only subject.

Read only; do not edit. Return findings in the output schema, each with an honest `severity` reflecting how much of the declared scope is unexamined — a declared subsystem with no traced property is a substantive hole in the audit, not a nit. You do not author the verdict — the flow decides which severities force another round, so do not inflate or downplay to force an outcome. File everything you find at its true severity: a finding below the gate is not discarded, it is carried to the operator in the run summary and the pull-request body. This is a non-blocking pass, so a spent rework budget means the flow accepts and continues with your open findings recorded — it never parks the task.

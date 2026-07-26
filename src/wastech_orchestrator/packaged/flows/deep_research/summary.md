You are closing out a **research** task, not a code change. The deliverable is a document and its citation manifest; there is no diff to explain. Write the handoff a reader gets instead of reading the report — enough to decide whether to trust it and what to do next.

Cover, in this order:

- **The question.** What was actually asked, in the task's own terms, and how the run scoped it.
- **The answer.** What the deliverable concludes — the headline finding and the two or three that carry the most weight, each in one line.
- **The evidence base, and how much of the scope it covers.** What was examined and, just as importantly, what was declared in scope and left unexamined. An audit that opened a fifth of its scope can be entirely accurate and still unsafe to act on, so a reader must be able to tell a thin pass from a thorough one from this section alone, without opening the artifacts.
- **What the gates said.** Each verification step and its recorded verdict, including the findings of any that accepted with findings still open.
- **What remains open.** Unverified claims, citations nobody could resolve, questions the run could not settle, and the concrete next step for each.

Two rules on how you write it:

**Claim nothing you did not do.** You are a read-only observer of the pipeline; describe what the *pipeline* did, in the third person. Never write that you re-opened, spot-checked, re-derived, or independently confirmed anything — not even loosely — and never name a file as one you inspected. If a claim of independent verification belongs in the summary, it belongs there because a node in the flow performed it and its verdict is on record; attribute it to that node.

**No number, verdict or count you were not given.** "All citations passed", "every finding was verified", "all gates passed" are the exact shape of claim that has been wrong before: they were written from memory while the recorded results said otherwise. If you do not have the figure, describe what happened qualitatively instead of inventing precision, and prefer "the citation check recorded some entries as unresolved" over a total you cannot source.

Keep it concise and specific — this becomes the pull-request body, and it is the only part of a long, expensive run most readers will ever see.

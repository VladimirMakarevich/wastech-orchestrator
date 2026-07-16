Produce a full, implementation-ready plan from the task and its enriched spec. Do not edit code. Return the typed structured result the output schema requires.

## What To Produce

- The files you expect to touch (repository-relative paths), the change at each, and the order to make them in.
- The load-bearing design decisions and their trade-offs, and the hardest or most uncertain part of the change — name it so the implementer starts there, not last.
- The risks, the cross-cutting invariants the change must preserve, and the tests you expect to add or update.
- Keep it concrete and no longer than an implementer needs to execute without re-deriving the approach.

## Explore Before You Plan

Ground the plan in the code as it exists today, not an idealized version of it:

- Read the files named in the task and the enriched spec first, then follow them into the modules they touch.
- Find the conventions and patterns this change must follow, and name a similar existing feature to model the work on rather than inventing a new shape. Reuse existing primitives instead of rewriting them; do not fork a parallel implementation of something the codebase already owns.
- Trace the relevant code paths end to end — real call sites, types, and module boundaries — so the plan never assumes an interface that isn't there. Verify every path you cite against the current tree.
- When you enumerate a product surface a downstream author will reference (CLI commands, flags, option values, output fields, public API surfaces), bind each item to the specific command or type that owns it and cite the source line. A flat list of names lets the implementer attach a flag to the wrong command.
- If the plan departs from an existing pattern, say so and justify the departure instead of quietly diverging.

## Clarification And Approval

- Use `human_input` only for a material clarification or approval of a risky change — state the precise risk and use repository-relative paths.
- If a `human_input` context file is already present, apply that answer and do not repeat the same request.

## Decomposition

- If decomposition is enabled and the task is large, return ordered subtasks with explicit dependencies.
- If the task already supplies operator-authored subtasks, that split is fixed: produce only the shared implementation plan and do not propose your own subtasks.

## Testing To Plan For

- A test per new or changed behavior, following the project's existing test conventions and locations.
- A focused scenario test when the behavior is user-visible.

## Additional Project Context

{?memory_path}A brief of repository memory relevant to this task — distilled lessons, conventions, known-fragile areas, and entity notes from prior runs — is at {memory_path}. Read it first and let it inform the plan; treat it as advisory and verify each point against the current code (it can be stale).{/memory_path}

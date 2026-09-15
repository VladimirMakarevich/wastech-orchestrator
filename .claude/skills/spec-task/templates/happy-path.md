<!-- The task made concrete for a NON-technical reader: what the operator does, sees, and gets.
     Plain language, Before/After, and small Mermaid diagrams. If you can't tell this story simply,
     the requirements aren't clear yet — go back. -->

# Happy path — {{TASK_TITLE}}

## In one sentence

<!-- "An operator who <situation> can now <do the thing> and gets <result>." -->

## Before / After

|  | Before (today) | After (this task) |
| --- | --- | --- |
| What the operator types |  |  |
| What the run does |  |  |
| What lands (branch / PR / artifacts / state) |  |  |

## Example 1 — `<short scenario name>`

<!-- Walk one real run through it, step by step, in plain language. Name the actual command, the flow
     and the nodes it passes, and what the operator sees at the end. -->

1. …
2. …
3. …

## Example 2 — `<another scenario, e.g. the failure the operator hits today>`

1. …

## Run flow

```mermaid
flowchart TD
    A[Operator runs worc ...] --> B{<decision the engine makes>}
    B -->|yes| C[<node that runs>]
    B -->|no| D[<alternative branch>]
    C --> E[<what lands>]
```

## Before → After (sequence)

```mermaid
sequenceDiagram
    actor O as Operator
    participant W as worc CLI
    participant E as Flow engine
    participant P as Provider CLI
    O->>W: <command>
    W->>E: <task claimed / node scheduled>
    E->>P: <launch with prompt + argv>
    P-->>E: <artifact / exit class>
    E-->>O: <status, ledger, what landed>
```

<!-- Keep diagrams small and honest. Two clear diagrams beat one sprawling one. -->

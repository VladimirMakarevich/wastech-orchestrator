---
name: adr
description: Think through an idea or design question together and write it up as a backlog document in docs/backlog/. Use when you have a raw thought, a design question, or a deferred decision that needs to be explored and recorded.
---

# adr

This skill turns a raw idea into a **recorded design decision** — a focused backlog document in `docs/backlog/`. It has two parts: a structured conversation to think the idea through, and then writing the result down.

Take `args` as the raw idea/thought. If none given, ask the user for it immediately before starting.

Speak in the **user's language** (default to the language they wrote in). Keep answers and questions tight — this is a conversation, not a form.

## Part 1 — Think it through

The goal is to go from a vague thought to a clear decision. Work through these in order. **Ask one question at a time** — wait for the answer before moving to the next. Use the `AskUserQuestion` tool with options where possible so the user can pick rather than compose from scratch.

### 1. Restate the idea

Paraphrase the idea in one or two sentences. Ask: "Did I understand this right?" Catch any misalignment early.

### 2. The actual problem

Ask: what breaks, is missing, or is painful _today_ without this? If the problem isn't clear, no design can be right. Push past "it would be nice" to the real friction.

### 3. What's in scope, what's not

Explicitly name what the idea does NOT cover. For deferred features ask: "Is this for now, or a stake-in-the-ground for later?" In this project, bias toward "later" (greenfield MVP, no migration machinery).

### 4. Alternatives considered

Name 2–3 alternatives (including "do nothing" and the simplest possible approach). Ask: "Did you consider X? Why is that worse?" If the user hasn't thought about alternatives, propose them.

### 5. Constraints and invariants to respect

Pull in the relevant constraints from [architecture.md](../../../.agents/rules/architecture.md) and [security.md](../../../.agents/rules/security.md) that bear on this idea. Surface any that the current design might violate.

### 6. The decision and its tradeoffs

Name the chosen direction and the cost of not picking the alternatives. One clean sentence: "We do X because Y; the cost is Z." If there are still open sub-decisions, list them explicitly.

### 7. Implementation handle (if applicable)

Where does this land in the code? Name the key modules, seams, or config keys affected — just enough for "where to start" when someone picks it up. Don't over-spec.

---

After these rounds, produce a **short summary** (5–8 lines) covering: problem, decision, what's excluded, key tradeoffs, open questions. Ask: "Shall I write this up?" Do not write the file until the user confirms.

## Part 2 — Write the backlog document

Once confirmed, write a backlog file to `docs/backlog/<slug>.md` following the format below.

### File format

```markdown
# <Title>

Status: **proposed** (YYYY-MM-DD) Date: YYYY-MM-DD Owner: Vladimir Makarevich

<One-paragraph description of the idea.>

## The problem

<What breaks or is painful today without this.>

## Constraints

<Relevant architecture/security invariants that bound the solution.>

## Alternatives considered

<Table or bullet list: option, why rejected.>

## Decision

<Chosen direction and its core rationale. Cost of alternatives.>

## Open questions

<Anything still unresolved — name each one explicitly.>

## Implementation notes

<Key modules/seams/config keys affected. Brief pointer, not a full spec.>
```

Rules for writing:
- Status is always `**proposed**` unless the user explicitly says "accepted" or "locked".
- Filename is a short kebab-case slug derived from the title (e.g. `operator-driven-merge.md`).
- Prose is one paragraph per line, no manual mid-paragraph line breaks (Prettier `proseWrap: never`).
- Do not repeat the problem in the decision section — each section adds new information.
- If the idea is exploratory/early (a stake-in-the-ground, not a spec), say so in the opening paragraph.

### After writing the file

1. Check if the idea belongs in the **Open backlog** table in [`docs/backlog/README.md`](../../../docs/backlog/README.md). If yes, add a one-line entry (Item | Summary | Source/constraint).
2. Tell the user: file path, whether README was updated, and any open questions that still need an answer before this can move to "accepted".

## What not to do

- Don't write the file before the user confirms the summary.
- Don't invent constraints that aren't in the code or `.agents/rules/`.
- Don't spec the implementation in detail — that belongs in a task file or a follow-up, not the backlog doc.
- Don't force an "accepted" status — proposed is the right default for a conversation that just happened.
- Don't create a boilerplate-heavy document just to fill sections — if a section has nothing to say, omit it.

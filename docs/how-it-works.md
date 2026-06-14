# How wastech-orchestrator works

A plain-English overview of what the orchestrator is and how the coding agents fit in. This page is
just the mental model — for the full technical detail see the architecture and configuration docs.

## It's a pipeline, not an AI "supervisor"

It is tempting to picture the orchestrator as a smart AI manager that watches everything and decides
what to do next. That is **not** how it works.

The orchestrator is ordinary, predictable software. It is not an AI, it has **no master prompt**, and
nothing "supervises" the run by reasoning about it. It simply moves a task through a fixed list of
steps, in the same order every time:

**refinement → planning → implementation → testing → review → fixing → summary → publishing**

Choosing which step comes next, enforcing the quality checks, and doing all the Git work (committing,
pushing a branch, opening the pull request) is fixed program logic. The AI never makes those
decisions.

## Who does each step

The steps fall into three kinds:

- **Thinking steps** (refinement, planning, implementation, review, fixing, summary) — for each of
  these the orchestrator starts a coding agent (Codex or Claude Code) and gives it that step's
  instructions.
- **Testing** — this is **not** an agent. The orchestrator just runs the project's own test and lint
  commands and checks whether they pass.
- **Publishing** — also not an agent. The orchestrator does the Git work itself.

So the only "AI" in the loop is the coding agent that runs during the thinking steps. Everything
around it is plain automation.

## Agents are short-lived — started fresh each step

A coding agent does **not** stay alive for the whole task. Each time a thinking step runs, the
orchestrator starts a **new** agent, lets it do that one step, and lets it finish.

This matters when a change needs fixing. If testing fails, the task loops: testing → fixing →
testing → fixing → … until it passes (or a safety limit stops it). In that loop:

- **Testing** just re-runs the checks — there is no agent to recreate, so "the 5th test run" is
  simply the commands running a 5th time.
- **Fixing** starts a **brand-new agent** every time. Five fix attempts means five fresh agents, one
  after another — not one agent kept alive across all of them.

## A fresh agent doesn't start blind

Even though each agent is new, it is not starting from nothing. The orchestrator hands every agent
the relevant files to read — the task itself, the plan, the current changes, the test output, and the
review notes. So a fixing agent can see exactly what failed and address it.

On top of that, with Claude the orchestrator can **resume the same conversation** from one step to the
next, so the new agent picks up where the last one left off. Codex does not resume a conversation, so
it relies on reading those files instead — but either way the agent always has the context it needs.

One caveat: the "resume the conversation" link only lasts while the orchestrator keeps running. If the
orchestrator is stopped and started again, agents begin a fresh conversation — but they still receive
all the files, so no real context is lost.

## In short

- The orchestrator is predictable software running a fixed pipeline — there is no AI supervisor and
  no master prompt.
- Agents appear only during the thinking steps; testing runs your checks, and publishing does the Git
  work.
- Agents are created fresh for every step (and every retry), not kept alive for the whole task.
- A new agent always gets the files it needs, and with Claude it can also resume the previous
  conversation — so "fresh" does not mean "starting over."

# How wastech-orchestrator works

A plain-English overview of what the orchestrator is and how the coding agents fit in. This page is just the mental model — for the full technical detail see the architecture and configuration docs.

## It's a pipeline, not an AI "supervisor"

It is tempting to picture the orchestrator as a smart AI manager that watches everything and decides what to do next. That is **not** how it works today.

The orchestrator is ordinary, predictable software. It has **no master prompt**, and nothing "supervises" the run by reasoning about it. It simply moves a task through a fixed list of steps, in the same order every time. Choosing which step comes next, enforcing the quality checks, and doing all the Git work (committing, pushing a branch, opening the pull request) is fixed program logic. The AI never makes those decisions.

## The steps, in order

Every task goes through the same line of steps:

1. **Refinement** — if the task is vague, the agent first fills in the gaps: a clear description, what is in and out of scope, and what "done" means. A task that is already clear skips this step.
2. **Planning** — the agent works out _how_ it will make the change before touching any code. (A very large task can optionally be split into smaller ordered pieces here.)
3. **Implementation** — the agent makes the actual code changes.
4. **Testing** — the project's own test and lint commands are run to see whether they pass.
5. **Review** — an agent reads the change like a code reviewer and flags problems.
6. **Fixing** — only when testing or review found something. An agent fixes it, and then the change is tested (and reviewed) again. More on this loop below.
7. **Summary** — an agent writes a short, plain-language explanation of the change. This becomes the description on the pull request.
8. **Publishing** — the change is committed, pushed, and a pull request is opened.

Before any of this, the orchestrator does a quick sanity check on the task file. If the task is broken or unusable, it is set aside immediately and never starts.

## Who does each step

The steps fall into two kinds:

- **Thinking steps, done by a coding agent** (Codex or Claude Code): refinement, planning, implementation, review, fixing, and summary. By default Claude Code does most of them and Codex does the review, but that is configurable.
- **Plain automation, with no agent**: the sanity check at the start, **testing** (the orchestrator just runs your test/lint commands and checks whether they pass), and **publishing** (the orchestrator does the Git work itself).

So the only "AI" in the loop is the coding agent during the thinking steps. Everything around it is ordinary automation.

## What happens when tests or review find problems

This is where the order can loop:

- If **testing** fails, the task goes to **fixing**, then back to **testing** — over and over until it passes.
- If **review** finds blocking problems, the task goes to **fixing**, then back through **testing** and **review** again.

This cannot loop forever. There is a safety limit on how many fix attempts are allowed. If the limit is reached, the orchestrator stops on its own, writes a short report of what was still wrong, and leaves the task for a human to look at.

A couple of other things can pause a run for a person:

- If a change does something **risky** — like deleting tracked files or changing dependencies — the orchestrator asks a human to approve it before continuing.
- If something genuinely cannot be launched (for example a test command whose program is missing), that is treated as a setup problem, not something the agent can fix by editing code.

## The agent keeps its context — it does not start over

Even though each step is a separate run, the agent does **not** start from a blank slate each time. Two things keep it informed:

1. It is always handed the relevant files — the task, the plan, the current changes, the test output, and the review notes. So a fixing agent can see exactly what failed.
2. With **Claude**, the orchestrator also keeps the **same conversation going** from one step to the next. In practice this means the agent that fixes the code is literally continuing the conversation of the agent that wrote it — it remembers everything it did and saw, including across the back-and-forth between testing and fixing. (Codex does not continue a conversation, so it relies on the files instead — either way the agent always has the context it needs.)

One caveat: that "same conversation" link lasts only while the orchestrator keeps running. If it is stopped and started again, the agent begins a fresh conversation — but it still receives all the files, so nothing important is lost.

## How a task can end

A task always finishes in one of three ways:

- **Done** — testing passed and review was clean. The orchestrator commits the change, pushes the branch, and opens a pull request.
- **Stopped for a human** — too many fix attempts were used up, or a risky change needs approval, or the situation is unclear. The work so far is kept, with a report.
- **Rejected up front** — the task file was broken or unusable at the sanity check, so the orchestrator never started on it.

After a task ends, the orchestrator tidies up (returns the working copy to the main branch) before it will pick up another one.

## In short

- The orchestrator is predictable software running a fixed pipeline — there is no AI supervisor and no master prompt.
- A coding agent only appears in the thinking steps; testing runs your own checks, and publishing does the Git work.
- When testing or review fails, the task loops through fixing, bounded by a safety limit that stops for a human.
- The agent keeps its context between steps — and with Claude it continues one ongoing conversation across the whole run, so "a new step" does not mean "starting over."

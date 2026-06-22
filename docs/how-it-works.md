# How wastech-orchestrator works

A plain overview of what the orchestrator is and how the coding agents fit in. This page is just the mental model — for the full technical detail see the architecture and configuration docs.

## It's a deterministic pipeline, with a thin watcher on top

It is tempting to picture the orchestrator as a smart AI manager that decides what to do next. The important part is what the AI is _not_ allowed to do.

The orchestrator is ordinary, predictable software. It moves a task through a fixed graph of steps — called a **flow** — in the same order every time. Choosing which step comes next, enforcing the quality gates, and doing all the Git work (committing, pushing a branch, opening the pull request) is fixed program logic. The coding agent never makes those decisions.

There _is_ one AI layer that watches the whole run — the **supervisor** — but it is deliberately powerless. It reads each finished step and, at the end, writes the plain-language summary that becomes the pull-request description. It cannot change the route, redo a step, or override a gate. So the run stays predictable: the supervisor only observes and explains, it never steers.

## The steps, in order

A normal coding task goes through the same line of steps (this is the default flow — see "Different kinds of task" below):

1. **Refinement** — if the task is vague, the agent first fills in the gaps: a clear description, what is in and out of scope, and what "done" means. A task that is already clear skips this step automatically.
2. **Planning** — the agent works out _how_ it will make the change before touching any code. (A very large task can optionally be split into smaller ordered pieces here.)
3. **Implementation** — the agent makes the actual code changes.
4. **Testing** — the project's own test and lint commands are run to see whether they pass.
5. **Review** — an agent reads the change like a code reviewer and flags problems.
6. **Fixing** — only when testing or review found something. An agent fixes it, and then the change is tested (and reviewed) again. More on this loop below.
7. **Publishing** — the change is committed, pushed, and a pull request is opened. By default the orchestrator stops there and leaves the pull request for a person to review and merge. An operator can optionally turn on **auto-merge** (off by default), in which case the orchestrator also merges the pull request once its checks pass — it still respects the repository's branch-protection rules and never forces a merge.

Before any of this, the orchestrator does a quick sanity check on the task file. If the task is broken or unusable, it is set aside immediately and never starts.

Throughout the run the supervisor quietly reads each finished step, and once the work is done — just before publishing — it writes the short, plain-language summary of the change. That summary becomes the body of the pull request.

## Who does each step

The steps fall into three kinds:

- **Thinking steps, done by a coding agent** (Codex or Claude Code): refinement, planning, implementation, review, and fixing. By default Claude Code does most of them and Codex does the review, but that is configurable per step.
- **Watching and explaining, done by the supervisor** (also a coding agent, but strictly read-only): it observes every finished step and writes the end-of-task summary. It never edits code and never changes the route.
- **Plain automation, with no agent**: the sanity check at the start, **testing** (the orchestrator just runs your test/lint commands and checks whether they pass), and **publishing** (the orchestrator does the Git work itself).

So the "AI" in the loop is the coding agent during the thinking steps, plus the read-only supervisor on top. Everything that decides _what happens next_ is ordinary automation.

## What happens when tests or review find problems

This is where the order can loop:

- If **testing** fails, the task goes to **fixing**, then back to **testing** — over and over until it passes.
- If **review** finds blocking problems, the task goes to **fixing**, then back through **testing** and **review** again.

This cannot loop forever. There is a safety limit on how many fix attempts are allowed. If the limit is reached, the orchestrator stops on its own, writes a short report of what was still wrong, and leaves the task for a human to look at.

A couple of other things can pause a run for a person:

- If a change does something **risky** — like deleting tracked files or changing dependencies — the orchestrator asks a human to approve it before continuing.
- If something genuinely cannot be launched (for example a test command whose program is missing), that is treated as a setup problem, not something the agent can fix by editing code.

## Splitting a big task into smaller pieces (decomposition)

Some tasks are too large to do well in one pass. The orchestrator can optionally break such a task into a short, ordered list of **subtasks** and carry them out one after another. This is **off by default** — an operator turns it on in the configuration.

The decision is shared on purpose, and not left to the AI alone:

- During **planning**, the agent may _propose_ a split — a numbered list of subtasks, each with its own "done" criteria.
- The orchestrator then _decides_ whether to accept that proposal, by fixed rules the agent cannot bend. It accepts a split only when it has between two and a configured maximum number of subtasks (eight by default) and forms a simple ordered chain — each subtask may build on earlier ones, never on a later one. Anything outside those rules is rejected and the task just runs as a single unit.

So the agent can suggest, but it can never force a split, exceed the limit, or reorder the chain. If it proposes nothing — or proposes something malformed — nothing breaks; the task simply runs whole.

When a split is accepted, each subtask goes through the normal implementation → testing → review → fixing steps and is **committed on its own**, all on the same branch. The whole parent task still ends in **one pull request**, not one per subtask.

Two practical benefits come from those per-subtask commits:

- **Cleaner history** — the branch reads as a sequence of self-contained steps instead of one giant change.
- **Safe resume** — if a run is interrupted partway, restarting continues from the first _unfinished_ subtask; the ones already committed are never redone. And if the run gets stuck, the report names exactly which subtask (k of n) failed and which were already committed.

**What it is not.** Subtasks always run in a straight line, one at a time — there is no parallel work and no branching between them. And only flows that include the per-subtask region support splitting at all: the default coding flow does; the research and audit flows do not.

## The agent keeps its context — it does not start over

Even though each step is a separate run, the agent does **not** start from a blank slate each time. Two things keep it informed:

1. It is always handed the relevant files — the task, the plan, the current changes, the test output, and the review notes. So a fixing agent can see exactly what failed.
2. The orchestrator also keeps the **same editing conversation going** from one step to the next. The agent that fixes the code is literally continuing the conversation of the agent that wrote it — it remembers everything it did and saw, including across the back-and-forth between testing and fixing. This works for **both** Codex and Claude Code now (each supports resuming its session), and the link is **durable**: it is saved with the task's state, so even if the orchestrator is stopped and restarted mid-task, the agent picks the conversation back up rather than starting over.

(The supervisor watching on top keeps its own separate read-only view of the run; it does not touch the editing conversation.)

## Different kinds of task (flows)

The list of steps above is the **default** flow, used for ordinary coding tasks. The orchestrator picks which flow to run from the task's `task_type`:

- **implementation** (the default) — the coding pipeline that ends in a pull request.
- **deep_research** — researches a question and produces a documentation pull request, with a citation gate and permission to reach the network.
- **security_audit** — an advisory audit that writes a private report instead of changing code.

All flows use the same machinery — the same gates, the same fix loops, the same read-only supervisor. They differ only in the steps and the kind of output. An operator can also drop a flow file into the project to add a new task type or override a built-in one.

## How a task can end

A task always finishes in one of three ways:

- **Done** — testing passed and review was clean. The orchestrator commits the change, pushes the branch, and opens a pull request, which by default is left for a person to merge (unless auto-merge has been turned on).
- **Stopped for a human** — too many fix attempts were used up, or a risky change needs approval, or the situation is unclear. The work so far is kept, with a report.
- **Rejected up front** — the task file was broken or unusable at the sanity check, so the orchestrator never started on it.

After a task ends, the orchestrator tidies up (returns the working copy to the main branch) before it will pick up another one.

## In short

- The orchestrator is predictable software running a flow — the route, the gates, and the Git work are fixed program logic, not AI choices.
- A coding agent does the thinking steps; a strictly read-only supervisor watches every step and writes the summary, but it can never change what happens next.
- When testing or review fails, the task loops through fixing, bounded by a safety limit that stops for a human.
- A big task can optionally be split into a linear chain of subtasks (off by default); the agent proposes the split, the orchestrator decides by fixed rules, and each subtask is committed on its own — still one pull request.
- The agent keeps its context between steps — it continues one ongoing editing conversation across the whole run (for both Codex and Claude), and that conversation survives a restart.

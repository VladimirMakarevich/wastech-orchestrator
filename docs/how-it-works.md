# How wastech-orchestrator works

A plain overview of what the orchestrator is and how the coding agents fit in. This page is just the mental model — for the full technical detail see the [architecture overview](worc_architecture.md) and the [configuration reference](configuration.md).

## It's a deterministic pipeline, with a thin watcher on top

It is tempting to picture the orchestrator as a smart AI manager that decides what to do next. The important part is what the AI is _not_ allowed to do.

The orchestrator is ordinary, predictable software. It moves a task through a fixed graph of steps — called a **flow** — in the same order every time. Choosing which step comes next, enforcing the quality gates, and doing all the Git work (committing, pushing a branch, opening the pull request) is fixed program logic. The coding agent never makes those decisions.

There _is_ one AI layer that watches the whole run — the **supervisor** — but it is deliberately powerless. It reads finished steps and, at the end, writes the plain-language summary that becomes the pull-request description. It cannot change the route, redo a step, or override a gate. So the run stays predictable: the supervisor only observes and explains, it never steers.

By default it does not comment on _every_ step — that would cost a lot on a long run for little gain — it comments only when something **deviated**: a step sent work back, a step failed, or a step had to fall back to the other agent. An operator can widen that to every step, point it at a named list of steps, or switch the commentary off entirely and keep just the closing summary. They can also remove the layer altogether; the pull-request description is then written mechanically from what the run recorded, with the same sections and none of the interpretation.

## The steps, in order

A normal coding task goes through the same line of steps (this is the default flow — see "Different kinds of task" below):

1. **Refinement** — if the task is vague, the agent first fills in the gaps: a clear description, what is in and out of scope, and what "done" means. A task that is already clear skips this step automatically.
2. **Planning** — the agent works out _how_ it will make the change before touching any code. (A very large task can optionally be split into smaller ordered pieces here.)
3. **Implementation** — the agent makes the actual code changes.
4. **Testing** — the project's own test and lint commands are run to see whether they pass. Which of them run is decided from the change itself, not by the agent: each command set declares the paths it covers, and only the sets the diff actually touches are run.
5. **Review** — an agent reads the change like a code reviewer and flags problems.
6. **Fixing** — only when testing or review found something. An agent fixes it, and then the change is tested (and reviewed) again. More on this loop below.
7. **Documentation** — once the code is accepted, an agent updates the project's own documentation to match what just shipped. Its edits join the same change the orchestrator commits, so the docs and the code land together. It runs once per task, at the end — not once per piece of a split task.
8. **Publishing** — the change is committed, pushed, and a pull request is opened. By default the orchestrator stops there and leaves the pull request for a person to review and merge. An operator can optionally turn on **auto-merge** (off by default), in which case the orchestrator merges the pull request itself — straight away, or only once the required checks have passed if they ask for that. Either way it respects the repository's branch-protection rules and never forces a merge past them; a merge the repository blocks stops the task for a human and leaves the pull request open.

Before any of this, the orchestrator does a quick sanity check on the task file. If the task is broken or unusable, it is set aside immediately and never starts.

Throughout the run the supervisor quietly reads the steps that deviated, and once the work is done — just before publishing — it writes the short, plain-language summary of the change. That summary becomes the body of the pull request. Under it the pull request also carries a short technical-debt list: the loose ends the supervisor noticed, plus any review findings the gate let through because they sat below its blocking threshold — so a nit that was waved past is still written down somewhere. If the summary cannot be written (or comes out so short it is plainly not a summary), the orchestrator falls back to a mechanical report of what the run actually did, and says so at the top — so a stub is never mistaken for the real write-up.

## Who does each step

The steps fall into three kinds:

- **Thinking steps, done by a coding agent** (Codex or Claude Code): refinement, planning, implementation, review, fixing, and documentation. Out of the box all of them run on the _same_ agent — the instance's single **primary**, which `install` sets to Claude Code whenever it is installed. Nothing in the shipped flows pins an agent to a particular step; a flow can pin one (and a model, and a reasoning effort) per step, but that is a choice the operator makes, not the default.
- **Watching and explaining, done by the supervisor** (also a coding agent, but strictly read-only): it observes finished steps and writes the end-of-task summary. It never edits code and never changes the route.
- **Plain automation, with no agent**: the sanity check at the start, **testing** (the orchestrator just runs your test/lint commands and checks whether they pass), and **publishing** (the orchestrator does the Git work itself). An operator can also plug in their **own program** as a step — a bespoke prose linter, a data producer, a router — which the orchestrator runs like any other gate; the packaged writing flows use two such tools.

So the "AI" in the loop is the coding agent during the thinking steps, plus the read-only supervisor on top. Everything that decides _what happens next_ is ordinary automation.

## What happens when tests or review find problems

This is where the order can loop:

- If **testing** fails, the task goes to **fixing**, then back to **testing** — over and over until it passes.
- If **review** finds blocking problems, the task goes to **fixing**, then back through **testing** and **review** again.

This cannot loop forever. There is a safety limit on how many fix attempts are allowed — by default 15 for any one loop and 30 across the whole task, whichever is reached first. If a limit is reached, the orchestrator stops on its own, writes a short report of what was still wrong, and leaves the task for a human to look at.

A few things can pause a run for a person:

- During **planning** (and, more rarely, refinement), if a genuinely material decision cannot be settled from the repository, the agent can pause to ask one clarifying question — and planning can also ask for an approval — before continuing. A well-specified task avoids this.
- If a change touches a path the operator marked **protected** (and, on the stricter setting, if it deletes or renames a tracked file, or edits a dependency manifest or lockfile), the orchestrator asks a human to approve it before continuing. By default routine in-repo changes are not gated — they are Git-reversible and the pull request is still the review backstop — but an operator can raise the bar per repository or per task.
- If something genuinely cannot be launched (for example a test command whose program is missing), that is treated as a setup problem, not something the agent can fix by editing code.

## Splitting a big task into smaller pieces (decomposition)

Some tasks are too large to do well in one pass. The orchestrator can optionally break such a task into a short, ordered list of **subtasks** and carry them out one after another. Letting the _agent_ propose the split is **off by default** — an operator turns it on in the configuration. An operator who already knows the breakdown can skip the proposal entirely and write the subtasks out by hand in the task file; that path needs no configuration switch, and it is held to exactly the same rules below.

The decision is shared on purpose, and not left to the AI alone:

- During **planning**, the agent may _propose_ a split — a numbered list of subtasks, each with its own "done" criteria.
- The orchestrator then _decides_ whether to accept that proposal, by fixed rules the agent cannot bend. It accepts a split only when it has between two and a configured maximum number of subtasks (eight by default) and forms a simple ordered chain — each subtask may build on earlier ones, never on a later one. Anything outside those rules is rejected and the task just runs as a single unit.

So the agent can suggest, but it can never force a split, exceed the limit, or reorder the chain. If it proposes nothing — or proposes something malformed — nothing breaks; the task simply runs whole.

When a split is accepted, each subtask goes through the normal implementation → testing → review → fixing steps and is **committed on its own**, all on the same branch. The whole parent task still ends in **one pull request**, not one per subtask.

A subtask that builds on an earlier one does not start blind: it is handed a short brief about each of its predecessors first — the files that subtask changed, its commit, what it was meant to achieve, and the supervisor's reading of how it actually went. Each subtask also gets a fresh fix-loop budget while the whole-task cap keeps accruing, so one awkward piece cannot quietly spend the next one's attempts.

Two practical benefits come from those per-subtask commits:

- **Cleaner history** — the branch reads as a sequence of self-contained steps instead of one giant change.
- **Safe resume** — if a run is interrupted partway, restarting continues from the first _unfinished_ subtask; the ones already committed are never redone. And if the run gets stuck, the report names exactly which subtask (k of n) failed and which were already committed.

**What it is not.** Subtasks always run in a straight line, one at a time — there is no parallel work and no branching between them. And only flows that include the per-subtask region support splitting at all: the default coding flow does; the research and audit flows do not.

## The agent keeps its context — it does not start over

Even though each step is a separate run, the agent does **not** start from a blank slate each time. Three things keep it informed:

1. It is always handed the relevant files — the task, the plan, the current changes, the test output, and the review notes. So a fixing agent can see exactly what failed.
2. The orchestrator also keeps the **same editing conversation going** from one step to the next. The agent that fixes the code is literally continuing the conversation of the agent that wrote it — it remembers everything it did and saw, including across the back-and-forth between testing and fixing. This works for **both** Codex and Claude Code now (each supports resuming its session), and the link is **durable**: it is saved with the task's state, so even if the orchestrator is stopped and restarted mid-task, the agent picks the conversation back up rather than starting over.
3. Across tasks, the orchestrator **can** carry lessons forward, but this part is **experimental and off by default** — a fresh install does not turn it on, and it is best left off unless you are deliberately experimenting. When enabled it keeps a small store in the repository of what earlier runs established — how the project is laid out, conventions that were confirmed, mistakes that were made — and hands a step a short, capped extract of the parts relevant to it. It is deliberately modest: no model decides what goes into the extract, entries are checked against the actual code before they are trusted (an unverifiable one is set aside rather than repeated), and a step whose role has no use for memory is handed nothing at all. The honest caveat is why it stays off: the store is unaudited, it carries no redaction guarantee, and its shape can change without a migration path.

(The supervisor watching on top keeps its own separate read-only view of the run; it does not touch the editing conversation.)

## Different kinds of task (flows)

The list of steps above is the **default** flow, used for ordinary coding tasks. The orchestrator picks which flow to run from the task's `task_type`:

- **implementation** (the default) — the coding pipeline that ends in a pull request.
- **deep_research** — researches a question and produces a documentation pull request, with a citation gate and permission to reach the network. Its repository reading is split into three passes over separate parts of the codebase, each with a narrow remit, behind a gate that measures whether they actually covered what the question named.
- **security_audit** — an advisory audit that writes a private report instead of changing code.
- **content_chapter / content_translate** — long-form book content-authoring flows: edit a chapter, or adapt a chapter into English, each gated by the deterministic `check_chapter` prose tool (delivered to `.worc/tools/` by `install`).
- **blog_article / blog_article_revise** — authorial blog-post flows: write a new article from scratch, or revise an existing one in place, each gated by the deterministic `check_length` minimum-size floor (delivered to `.worc/tools/` by `install`) and a tone/style critic.

There is one more built-in that tasks never select: **merge**. It comes into play only when an operator merges a finished task's pull request with `worc merge-task` and pulling the base branch into the task branch hits a conflict — an agent resolves the markers, the checks re-run, and then the orchestrator finishes the merge itself. A conflict-free merge is purely mechanical: no flow, no agent.

All flows use the same machinery — the same gates, the same fix loops, the same read-only supervisor. They differ only in the steps and the kind of output. None of them is hidden away in the tool, either: `install` copies every built-in flow into the project's own `.worc/flows/`, and that copy is what actually runs — so an operator edits a built-in in place, or adds a file there to introduce a new task type. See [Flow authoring](flow-authoring.md).

## How a task can end

A task always finishes in one of three ways:

- **Done** — testing passed and review was clean. The orchestrator commits the change, pushes the branch, and opens a pull request, which by default is left for a person to merge (unless auto-merge has been turned on).
- **Stopped for a human** — too many fix attempts were used up, or a risky change needs approval and the approval did not come, or the situation is unclear. Publishing itself can stop here too: if the remote branch moved on and the quality gate fails over the combination, or the merge conflicts, nothing is sent and the work waits on disk. The work so far is kept, with a report.
- **Failed** — most often a rejection up front: the task file was broken or unusable at the sanity check, so the orchestrator never started on it and the file was set aside. It also covers a run that hit a wall no code change could get past — an agent that could not be launched at all, even after falling back to the other one.

The task file follows the outcome: it moves into the `done/` or `failed/` folder, while a task stopped for a human stays where it is until the operator resolves it. Then the orchestrator tidies up before it will pick up another one — under the default branch mode that means putting the working copy back on the base branch; on the modes where the operator owns the branch, it is left alone.

## In short

- The orchestrator is predictable software running a flow — the route, the gates, and the Git work are fixed program logic, not AI choices.
- A coding agent does the thinking steps; a strictly read-only supervisor watches the run and writes the summary, but it can never change what happens next — and an operator can dial its commentary down, or remove it and get a mechanical report instead.
- When testing or review fails, the task loops through fixing, bounded by a safety limit that stops for a human.
- A big task can optionally be split into a linear chain of subtasks (off by default); the agent proposes the split, the orchestrator decides by fixed rules, and each subtask is committed on its own — still one pull request.
- The agent keeps its context between steps — it continues one ongoing editing conversation across the whole run (for both Codex and Claude), and that conversation survives a restart. Carrying lessons **across** tasks is an experimental extra, off by default.
- Publishing does not assume it is the only thing that ever touched the branch. If the remote has moved on, those commits are merged in locally, the quality gate runs again over the combination, and only then does anything go out — and the pull request says which commits it took on.

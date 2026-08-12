---
name: handoff
description: Assemble a HANDOFF PROMPT — one self-contained prompt from which a fresh agent session continues the current task from almost the same place, without ever seeing this chat. Works out what the work was about, checks the session's agreements and plans against the actual repository state (`git status`, staged and unstaged `git diff`, recent commits, the files themselves) and treats the files as the truth rather than promises made in conversation. Splits the work into done, started, and open; records decisions with their reasons, the user's requirements, known issues, and the places that need re-checking, tagging every item as fact, decision, assumption, or open work. It does not continue the task and changes no files; the reply is the finished prompt and nothing else, and inside it the new session is required to reconcile the handoff against the repository, give the user a CONTEXT CHECK, and wait for permission. Use when asked for a "handoff prompt", "prepare a prompt for a new session", "I'm running out of context, hand the work over", "record the state of the task", "let's continue in another chat", and for the same requests in Russian ("сделай handoff", "передай работу в новую сессию").
---

# handoff

Packs the current work into a single prompt for a new session. The unit of work is **one task** — the one being worked on right now. The result is text the user copies wholesale into a new chat.

The key idea: **the new session will not see this conversation, but it will see the repository.** So a handoff does not retell the discussion — it explains the state of the files: what is already in them, why it looks the way it does, and what to do with it next. Anything readable from the repository only needs to be named. Anything that lives only in the conversation — decisions, rejected alternatives, the user's requirements, reasons — is preserved in full, because otherwise it disappears.

The second principle: **the files are the source of truth.** If a plan from the conversation disagrees with what is on disk, the disk goes into the handoff, and the discrepancy is called out separately.

## What this is NOT

- **Not a continuation of the task.** The skill writes nothing, edits nothing, stages nothing, commits nothing. The only side file allowed is one the user explicitly asks for (see "Output").
- **Not a chat summary.** The course of the discussion, discarded phrasings, and internal reasoning do not go into the prompt. Conclusions do.
- **Not a work report.** The prompt is written for an agent, not for a human: no self-praise, no "we managed to", no grading.
- **Not a TODO list.** The open-work list is one section among several; without the decisions, requirements, and an explanation of the current state it is useless.

## How it works

Do not skip steps 2–4. Without them the handoff turns into a retelling of the chat, and the new session starts with the wrong picture.

1. **Identify the task.** What exactly is being handed over. If several unrelated tasks ran in this session, hand over the one worked on last and tell the user so in one line before emitting the prompt. If it is unclear which one is current — ask before assembling.
2. **Capture the repository state** with the commands in the table below. Read them, don't recall them.
3. **Read the files themselves** — the ones the task touched, plus the task's own source-of-truth files (the backlog item, the rules it depends on). A hunk in `git diff` shows what changed, but not whether the thought is finished; only the file shows that.
4. **Reconcile with the agreements.** Walk through the session's decisions and plans and verify each one against the files. Confirmed — a fact. Diverging — goes into the prompt as a discrepancy flagged for re-checking. Not verifiable from the repository — an assumption.
5. **Split the work** into done, started, and not started. The completion criterion is the state of the file, not an intention from the conversation.
6. **Fill in the template** below. Do not change the sections or their order, and do not drop empty ones: write "none" or "unknown" instead of content.
7. **Emit the prompt** as a single block, with not one word before or after (see "Output").

## What gets read from the repository

All read-only. If git is unavailable or a command fails, say so in the prompt rather than filling the gap with guesses.

| Command | What it gives |
| --- | --- |
| `git branch --show-current` | The branch the work sits on — and, in this repo, which documents exist at all (see below) |
| `git status --short` | The full picture of uncommitted work, including new and untracked files |
| `git diff --stat` and `git diff --stat --staged` | The size of the edits per file. **Staged must be inspected** — part of the work is often already indexed |
| `git diff` on the files that matter | What actually changed where it affects how the work continues |
| `git log --oneline -15` | Where the current state came from; the wording of the recent commits |
| `git log --oneline origin/dev..HEAD` | What this branch already committed on top of `dev` — work that is done and must not be redone |
| `git stash list` | Shelved work the new session would otherwise never learn about |

Beyond git, read the task's own service files: the backlog item it implements (`docs/backlog/<slug>.md`), the rule files the task depends on, the current `TodoWrite` list, and the PR description if one exists (`gh pr view`).

## Four kinds of information

The new session must see at a glance what to trust without checking and what to verify. So every non-obvious item is tagged in the text itself:

- **Fact** — verified against the files or commands just now. Written as a plain statement, untagged.
- **Decision** — chosen deliberately; reversing it breaks the task. Tagged `Decision:` with a short "why".
- **Assumption** — probably true, not confirmed by the repository. Tagged `Assumption (needs checking):`.
- **Open** — work not done, or only partly done. Lives in `PARTIALLY COMPLETED` and `TODO`; needs no separate tag.

Never mix these four in one item: that mixture is exactly how the new session takes an assumption for a fact.

## What must be preserved

This is what a context switch destroys first and what is hardest to reconstruct.

- **The user's requirements, especially the small ones.** Output format, checkpoints, a ban on some action, "ask me first", limits, language. An item like "show me the plan before each stage" outweighs half the architectural decisions.
- **Rejected alternatives, and why.** Otherwise the new session will earnestly propose what was already turned down, or silently reinstate what was cancelled.
- **The reasons behind non-obvious decisions.** A decision without a reason looks accidental and gets redone.
- **The boundaries of the task.** What was deliberately left out, so the new session does not widen the work.
- **Process agreements.** Who decides at which step, where the user's answer is required, what happens only on confirmation.

## What never goes into the prompt

- Large chunks of code or text. Use the file path and the place in it instead. A short excerpt is fine when the item is incomprehensible without it.
- References to invisible history: "as we discussed above", "in the previous message", "as I already did". The new session saw none of it.
- Internal reasoning, option-weighing, draft versions.
- The session's chronology for its own sake. Order of events matters only where it explains the current state of the files.
- Guesses about anything that is neither in the repository nor stated by the user. No information — say so.

## Language and formatting

- **Section headings — exactly as in the template, in English.** The new session recognizes them; renaming breaks the recognition.
- **The body — in the user's language** (default to the language of this chat, per [AGENTS.md](../../../AGENTS.md)). The three protocol sections are carried over verbatim; translate them too only if the user asks for the whole prompt in another language.
- **Paths — exact, relative to the repository root**, with forward slashes (`src/wastech_orchestrator/providers/claude.py`), as plain text. The string the new session will feed to a tool matters more here than the link markup.
- **Names — verbatim**: files, directories, functions, classes, config keys, node ids, statuses, commands, branches, commit hashes.
- Lists short, one fact per item. An agent reads this prompt; it does not need to be pretty, it needs to be unambiguous.

## Template

Below is the ready frame. Sections `CONTEXT` … `RELEVANT FILES` are filled in for the task. Sections `NEXT STEPS`, `STARTUP PROTOCOL`, and `WORKING INSTRUCTIONS` are carried over word for word: that is the protocol for the new session and it is always the same.

```
# CONTEXT

<What the project is, which part of it the work is in, which task is being solved. The branch. 3–6 sentences, no discussion history.>

# ORIGINAL GOAL

<The full end goal of the task, not the last step taken. What counts as "done". If the goal is split into phases — name them all and say which one the work is on. If the task implements a backlog item, name the file instead of restating it.>

# CURRENT REPOSITORY STATE

Branch: <branch> (cut from <base>)
Last commit: <hash> <subject>
Uncommitted: <N files> (staged: <N>, unstaged: <N>, untracked: <N>)
Stash: <yes/no, what is in it>

<The significant changed and created files: path — what changed in it and why. Only what belongs to the task; runtime residue does not count as work.>

<If the state of the files diverges from earlier plans, say it here plainly and state what is true now.>

Large fragments of code and text are deliberately omitted: the repository is read directly.

# COMPLETED

<What is finished and verified against the files. Each item names the file or the place where it can be seen.>

# PARTIALLY COMPLETED

<Started and unfinished. For each item: what already exists, what is missing, exactly where it stopped.>

# TODO

<Not started yet. Ordered the way it makes sense to do it.>

# IMPORTANT DECISIONS

<Decisions taken: architecture, data models, naming, behavior, file layout, constraints, deliberately rejected alternatives. For each non-obvious one, a short reason so it does not get reversed by accident. If a hard invariant is involved, say which one and how the current code satisfies it.>

# REQUIREMENTS AND CONSTRAINTS

<Requirements that must keep holding. Separately and explicitly — the user's requirements that are easy to lose across a context switch: format, checkpoints, bans, limits, language, "ask first".>

<The project rules this task is bound by, as paths to the rule files.>

# KNOWN ISSUES / RISKS

<Known bugs, failing checks, temporary workarounds, potential regressions, doubtful and unverified places, everything that needs re-checking. Each item tagged with its kind: fact / assumption (needs checking).>

<Which checks were run and what they returned. If none were run, say so plainly.>

# RELEVANT FILES

<A compact list of files and directories: path — role in the task. Grouped by meaning: rules, task source of truth, working files, tests, config.>

# NEXT STEPS

The first stage is restoring context:

1. Inspect the current state of the repository.
2. Read the relevant files.
3. Check `git status`.
4. Check `git diff`, including staged and unstaged changes, if Git is available.
5. If needed, look at recent Git history to understand where the current state came from.
6. Reconcile the actual state of the code against this handoff.
7. Determine which COMPLETED / PARTIALLY COMPLETED / TODO items really match the current state of the repository.
8. Form your own understanding of what has to be done next.

After that, DO NOT START implementing.

First give the user a short report in this format:

## CONTEXT CHECK

**What I understood**
A brief statement of the task's end goal, in 2–5 sentences.

**Current state**
Briefly:

* what is already implemented;
* what is partly implemented;
* what is left to do.

**What needs to happen next**
A short ordered list of the next main steps.

**Discrepancies with the handoff**
State the discrepancies found between the handoff and the actual state of the repository.

If there are none, say so explicitly.

**Plan for the first stage**
Briefly describe where you intend to start once the user allows you to continue.

Then STOP and wait for a separate instruction from the user to continue.

Until that instruction arrives:

* do not modify files;
* do not create new files;
* do not delete files;
* do not run autofixes;
* do not run migrations;
* do not git commit;
* do not start implementing the TODO.

Only read-only actions are allowed — the ones needed to restore context and inspect the state of the project.

# STARTUP PROTOCOL

This protocol is mandatory for the new session.

On your first reply after receiving this handoff:

1. DO NOT start implementing the task.
2. DO NOT modify the repository.
3. First investigate the actual state of the project.
4. Use the handoff as a map of the context, not as an absolute source of truth.
5. Reconcile the handoff against the code.
6. Determine the real current state of the task.
7. Give the user a `CONTEXT CHECK`.
8. Wait for an explicit instruction from the user to continue.

Even if the next step looks obvious, run this protocol first.

# WORKING INSTRUCTIONS

Once the user allows you to continue:

* Do not treat this handoff as an absolute source of truth for the state of the code. The current state of the repository outranks it.
* Do not revert existing changes just because you do not understand where they came from.
* Do not rewrite working parts without a reason.
* Investigate the existing implementation first and follow the patterns already established in the project.
* Preserve existing behavior outside the task unless the task explicitly requires otherwise.
* Run the relevant tests and checks after making changes.
* If you find a discrepancy between the handoff and the repository, go with the repository and adapt the plan.
* Do not ask the user about things that can be reliably determined from the repository.
* If a TODO item turns out to be already done in the current state of the repository, do not do it again.
* Keep working until the ORIGINAL GOAL is reached or an objective blocker is hit.
```

## Specifics of this repository

- **The branch decides what exists.** Always state the branch and the base it was cut from. On `dev` there is no derived `docs/` tree — only `docs/backlog/` — so a new session that goes looking for `configuration.md` or `worc_architecture.md` in the checkout burns a turn on nothing; those live on `main` and are read by absolute URL. Never suggest merging `main` into `dev`. See [git-workflow.md](../../../.agents/rules/git-workflow.md) §A.
- **Do not restate the rules or the ADR** — both live in the repository and change. `REQUIREMENTS AND CONSTRAINTS` carries paths: [AGENTS.md](../../../AGENTS.md), the [.agents/rules/](../../../.agents/rules/) files this task actually depends on, and the backlog item ([docs/backlog/README.md](../../../docs/backlog/README.md)). The new session reads them itself.
- **Name the invariant the task brushes against.** If the change touches one of the hard invariants — the core knowing no CLI syntax, only the orchestrator committing/pushing, fallback only for provider infrastructure errors, the security envelope, no secrets, argv instead of a shell string, cross-platform — say which one and how the current code satisfies it. That is the knowledge lost first, and violating it fails silently.
- **Untracked runtime residue is not task work.** A working checkout shows `config.yaml`, `state.db`, `logs/`, `workspace/`, `tasks/`, `.venv/` in `git status`. Do not list them as changes; if one of them really is part of the task, say so explicitly.
- **The state of the gates is a fact worth recording.** Say which of `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest`, `python tools/mdlint.py` were actually run in this session and what they returned — see [/run-checks](../run-checks/SKILL.md). "Not run" is said plainly; never imply green. If `WASTECH_MDLINT_HOME` is unset, the Markdown hook passes without checking anything — that too is worth one line.
- **Docs are half of Done.** If behavior, the CLI, config, or the architecture changed, say whether the docs on this branch were synced ([/sync-docs](../sync-docs/SKILL.md)) — the rules, `README.md`, `docs/backlog/`, and above all the shipped operator docs under `src/wastech_orchestrator/packaged/` (the `guide/` quickstarts, `config.example.yaml`, the built-in flows and role prompts), the half that is forgotten most often. If a doc-impact note for the `main`-side refresh was drafted, carry it over verbatim.
- **Name the skills.** If the work ran through [/implement](../implement/SKILL.md), say which phase it reached. Carry the outcomes of [/clarify-task](../clarify-task/SKILL.md), [/assess-refactor](../assess-refactor/SKILL.md), [/simplify-task](../simplify-task/SKILL.md), and [/simplify-review](../simplify-review/SKILL.md) into `IMPORTANT DECISIONS` instead of leaving them to be re-derived.
- **Uncommitted work is normal — but explain it.** Work in progress on a `feat/…` branch usually sits uncommitted, and a commit is often deliberately withheld until the checks are green or the user confirms. Say which it is, or the new session will "tidy up" the index or commit early. Staging here is always scoped to explicit paths, never `git add .`.

## Output

- The final message contains **only the prompt** — not a line before, not a line after. Anything you need to say in your own voice (for instance, which of several tasks is being handed over) is said before assembling, not in the final message.
- The prompt is emitted as one block in four backticks, so that inner blocks and commands do not break the markup and the whole thing copies in one go.
- No file is created. If the user asks to save the handoff, write it to the path they name or to the session scratchpad — **not** under `docs/`: on `dev` a CI guard rejects any new path there outside `backlog/` and `research/`.

## Common mistakes

- **Retelling the chat instead of the state of the files.** The prompt must explain why the repository looks the way it does.
- **Forgetting staged.** `git diff` without `--staged` loses part of the work, and it lands in `TODO` as undone.
- **Putting a promise into `COMPLETED`.** Done means confirmed by a file.
- **Losing the user's requirements.** A small "show me first" disappears first and costs the most.
- **Smoothing over uncertainty.** "Seems done" and "probably works" become facts inside a prompt. Doubt is tagged as needing verification.
- **Starting the task.** This skill stops the work: assemble the prompt, and that is all.
- **Pasting a wall of code.** The new session reads the files itself; the prompt's budget goes to what is not in the files.
- **Dropping an "empty" section.** A section marked "none" carries information: checked, nothing to report. A missing section looks forgotten.

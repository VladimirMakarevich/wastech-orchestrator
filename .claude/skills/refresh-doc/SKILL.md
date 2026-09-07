---
name: refresh-doc
description: Bring ONE named Markdown document back in sync with reality by auditing it against the codebase — the only source of truth. Takes a document (README, worc_architecture.md, glossary.md, configuration.md, a rules file, a packaged operator-guide page), inventories every checkable claim in it (CLI surface, config keys and defaults, paths, node ids, statuses, error classes, limits, examples), verifies each one against the code that decides it, fixes what drifted, adds what the document's own charter covers but misses, and reports every edit with a `file:line` citation. Use when the user points at a specific document and wants it actualized. Document-driven; for diff-driven syncing of many docs right after a change use `/sync-docs`.
---

# refresh-doc

Take **one** document and make every statement in it true again. The unit of work is a document, not a diff: you walk the doc claim by claim, prove or disprove each claim **against the code**, and rewrite only what the code contradicts.

**The codebase is the only source of truth.** Not another document, not the backlog, not the git log, not this document's own previous claims, not your recollection of the project. If a statement cannot be traced to code, it is not a fact yet — see [Source-of-truth discipline](#source-of-truth-discipline).

Compared with [/sync-docs](../sync-docs/SKILL.md): that skill is **diff-driven** (a change just happened → which docs does it touch), runs across many files, and makes small targeted edits. This one is **document-driven** (this doc may have drifted over many changes → what in it is now false), runs on a single file, and audits it exhaustively. They are complements; neither replaces the other.

## Step 0 — resolve the target document

The argument is a document locator: a path, or a bare name (`readme`, `architecture`, `glossary`, `config reference`, `git-workflow`, or any other md file). Resolve it, then **state the resolved path** before doing anything else.

1. If the argument is a path that exists, use it.
2. Otherwise match the name against the tracked corpus: `git ls-files '*.md'`. Common names:
   - `readme` → [README.md](../../../README.md) · `agents` → [AGENTS.md](../../../AGENTS.md)
   - a rule → [.agents/rules/](../../../.agents/rules/): `architecture`, `coding-style`, `git-workflow`, `security`, `testing`
   - the operator guide → `src/wastech_orchestrator/packaged/guide/…`: `guide` (its `README.md`), `best-practices`, `decision-guide`, `footprint`, `config/reference`, `config/best-practices`, `flows/reference`, `flows/roles`, `flows/prompt-variables`, a `skills/worc-*/SKILL.md`
   - a derived doc (**`site` only**, see step 1) → `docs/`: `architecture` → `worc_architecture.md`, `configuration`, `cookbook`, `glossary`, `operations`, `how-it-works`, `how-to`, `index`, `telegram`, `task-authoring`, `flow-authoring`
   - a role prompt → `src/wastech_orchestrator/packaged/flows/<flow>/<node>.md`
3. Two or more plausible matches, or a name that resolves to nothing → **ask the user**; do not guess. `architecture` is genuinely ambiguous (the rule vs. the derived architecture doc) — always confirm that one.
4. The target must be **tracked**: verify with `git ls-files -- <path>` / `git check-ignore -v <path>`. A gitignored `.md` (e.g. under `.archive/`) is retired content and out of bounds — refuse and say why.

**Not valid targets** (refuse, explain, and stop):

| Document | Why not |
| --- | --- |
| `docs/backlog/**` | A plan, not a description of code. Refreshing it "against the code" would erase intent that is not built yet. If the user wants to know whether an item is implemented, do that as an analysis and report — do not rewrite the item. |
| [BRANCHING_MODEL.md](../../../BRANCHING_MODEL.md) | The design record for the branch model — why it has its shape, not how to operate it. `.agents/rules/git-workflow.md` §A is the operating manual. |
| `tests/**/*.md` | Role-prompt fixtures whose shape is asserted by the suite; change the test, not the fixture. |

## Step 1 — establish the scope: which branch are you on?

The repository carries two documentation shapes ([git-workflow.md](../../../.agents/rules/git-workflow.md) §A). Detect by **the marker file**, never by branch name — that stays correct in a worktree and in detached HEAD:

```bash
test -f docs/worc_architecture.md && echo site-scope || echo code-scope
```

- **`code-scope`** — the derived `docs/` tree is absent. Valid targets: [README.md](../../../README.md), [AGENTS.md](../../../AGENTS.md), [.agents/rules/](../../../.agents/rules/), `.claude/skills/`, and everything under `src/wastech_orchestrator/packaged/`. If the user named a derived doc, **stop**: it lives on `site` only, `branch-guard` rejects new `docs/` paths here, and creating a local copy is the one thing that breaks the branch split. Say so and offer to either run this on `site`, or produce the findings as a doc-impact note (step 7).
- **`site-scope`** — the derived tree exists; `docs/*.md` are the targets. **Do not edit shared files here** ([AGENTS.md](../../../AGENTS.md), `.agents/rules/`, `.claude/skills/`, `README.md`, anything under `src/`) — those edits flow through `dev`, or the branches diverge in content and conflict on every merge. If the user named one, stop and point at `dev`.

## Step 2 — inventory the claims

Read the target **in full** — every line, including tables, code blocks, and captions. Then build a claim inventory: a list of the doc's individually checkable assertions, each with its location in the doc and its type. Work from this list; do not audit by impression.

What counts as a claim: a CLI command, subcommand, flag, or exit code · a config key, its default, its allowed values, its validation · a file or directory path, on disk or under `.worc/` · a module, class, or function name · a task status, front-matter key, or lifecycle folder · a flow node id, node kind, edge, or gating condition · a provider id, error class, or fallback rule · a prompt variable · a schema/version number · any number at all (timeout, budget, limit, retry count) · a command in a code block · a stated invariant or guarantee · every relative link and anchor.

Prose that is rationale, motivation, or trade-off is **not** a claim about code and is not yours to rewrite — leave the doc's voice alone unless the thing it reasons about no longer exists.

## Step 3 — find the authority in code for each claim

Every claim type has one place that decides it. Cite that place, not a doc that agrees with you.

| Claim type | Authority |
| --- | --- |
| CLI commands, flags, exit codes | `cli.py`, `cli_shell.py` — plus `worc --help` and `worc <cmd> --help` (read-only, and the surface the operator actually sees) |
| Config keys, defaults, validation, migration | `config/schema.py` (shape), `config/loader.py` (defaults + resolution), `config/validation.py` (what is rejected), `config/upgrade.py` (`schema_version`), `install/config_writer.py` + `packaged/config.example.yaml` (what `install` writes) |
| Security envelope, forbidden flags, env allowlist | `security/forbidden_args.py`, `security/profiles.py`, `security/env.py`, `security/isolation.py`, `security/injection.py` |
| Task language: statuses, front matter, lifecycle folders | `task/model.py`, `task/parser.py`, `task/validation_gate.py`, `core/state_machine.py` |
| Flow graph: node kinds, schema, edges, `when`, per-node overrides | `core/flow/schema.py`, `core/flow/validator.py`, `core/flow/nodes/`, `core/flow/registry.py`, `core/node_overrides.py`, and the shipped `packaged/flows/*.yaml` |
| Prompt composition and variables | `core/flow/prompt.py`, `core/flow/prompt_vars.py`, `core/prompts.py`, `providers/_adapter_base.py` |
| Provider ids, capabilities, error classes, routing, fallback | `providers/base.py`, `providers/errors.py`, `providers/capabilities.py`, `providers/claude.py`, `providers/codex.py`, `routing/router.py` |
| Checks and command sets | `check_runner.py`, `checks/model.py`, `checks/resolver.py`, `checks/selection.py` |
| `.worc/` layout, logs, artifacts, retention | `runtime_layout.py`, `observability/logging.py`, `core/flow/recorder.py`, `runs_retention.py`, `ledger.py`, `providers/artifacts.py` |
| `state.db` tables and `user_version` | `state_store.py` |
| Exchange, seals, memory | `providers/exchange.py`, `core/flow/exchange_seal.py`, `core/flow/control_bundle.py`, `memory/` |
| Decomposition, HITL, supervisor, loop control, recovery | `core/decomposition.py`, `core/hitl.py`, `core/supervisor*.py`, `core/loop_control.py`, `core/recovery.py`, `core/follow_ups.py` |
| Git behavior: branches, commits, PRs, preflight | `git_manager.py`, `preflight.py` |
| Notifications | `notify/interface.py`, `notify/telegram.py` |
| Install and packaged assets | `install/wizard.py`, `install/detect.py`, `install/config_writer.py`, `core/skills.py` |

Paths above are relative to `src/wastech_orchestrator/`. `tests/` mirrors that layout and is **corroborating** evidence — a test pins behavior, so it is a good witness, but where a test and the code disagree the code is what ships.

## Step 4 — verify each claim

Prefer **executing** the surface over reading about it, and reading code over inferring:

- `worc --help`, `worc <cmd> --help` for the CLI surface; `git grep -n '<symbol>'` for existence; read the module for defaults and resolution order.
- Executing is allowed only for **read-only** things. Never run a command that writes to `.worc/`, moves a task between lifecycle folders, or launches a provider.
- `pytest tests/<area> -k <name>` when a behavioral claim is pinned by a test and you want it confirmed rather than argued.
- Values that are computed (a default that falls back through provider config, a path assembled at runtime) must come from the code that computes them, **not** from `config.example.yaml` — the example is itself a doc and can be stale too.

Give every claim one verdict:

- **ok** — code confirms it; leave the text alone.
- **stale** — was true, code moved. Fix it.
- **wrong** — never true, or contradicts an invariant. Fix it.
- **missing** — the code has a surface this doc's own charter says it covers, and the doc omits it. Add it (step 5).
- **unverifiable** — no code decides it (a claim about the roadmap, an external tool, a human process). Leave the text as it is and list it in the report; do not quietly delete it and do not invent a citation for it.

## Step 5 — the reverse pass: what the doc should cover and doesn't

Auditing only what is written finds wrong statements, never absent ones. For any document that enumerates a surface, enumerate that surface **from the code** and diff it against the doc:

- a CLI doc → the subcommand list from `worc --help`
- a config doc → the key set from `config/schema.py` (and each default from `config/loader.py`)
- a flows doc → `git ls-files 'src/wastech_orchestrator/packaged/flows/*.yaml'` and the node ids inside each
- a role/prompt doc → the variable set from `core/flow/prompt_vars.py`
- a glossary → the code's own vocabularies: statuses, node kinds, provider ids, error classes
- an operations doc → schema/version constants (`config` `schema_version`, `state.db` `user_version`, registry `version`)

Add only what belongs to **this** document's charter. Drift that belongs to a different document is a finding to report, not an edit to make here.

## Step 6 — rewrite

- **Minimal, surgical edits.** Change the sentence that is false; keep the doc's structure, ordering, headings, and voice. A refresh is not a rewrite, and never a restructure.
- **No claim without a citation.** If you cannot name the `file:line` behind a sentence you are writing, do not write it.
- **Examples come from the tree**, copied from packaged files or tests and re-validated — never composed from memory.
- **Links**: relative paths must resolve on this branch; never link a document that is not on this branch — name it in plain text instead. Anchors must match a real heading slug.
- **Formatting**: Markdown here is not hard-wrapped — one paragraph per line. Run `npx prettier@3 --write <path>` afterwards, **except** for anything under `src/` (all of it is `.prettierignore`d): match the existing one-paragraph-per-line style there by hand.
- **Size budgets are ratchets** ([wastech-mdlint.config.json](../../../wastech-mdlint.config.json)). Role prompts are the tightest — they are paid for on every node run of every task. A refresh that grows a doc past its budget is a refresh that did too much; cut before you raise a threshold, and never raise one to silence a finding. `AGENTS.md`/`CLAUDE.md` additionally carry the eager-import budget every agent pays.
- **Never edit code in this skill.** If the code is what is wrong, say so in the report and let the user decide; do not "fix" the source to match the doc.

## Step 7 — verify, then report

Run `python tools/mdlint.py` (links, anchors, reachability, size budgets) and `npx prettier@3 --check <path>` where Prettier applies. If the doc embeds examples the suite loads, run `/run-checks`.

Report concisely, in the user's language:

- **Target** — the resolved path and the scope you worked in.
- **Verdict** — how many claims were checked, and the counts per verdict.
- **What changed** — one line per edit: the claim, what it said, what it says now, and the `file:line` that decided it.
- **Unverifiable** — claims no code decides, left as-is, so the user can rule on them.
- **Findings that belong elsewhere** — drift you saw in other docs, and (in `code-scope`) each derived `site` doc the audit implicates, as a one-line doc-impact note for the refresh task on `site`.
- **Code smells surfaced** — places where the code, not the doc, looks wrong. Report; do not fix.
- **Nothing to change is a valid result.** "Audited 41 claims, all confirmed" is a good outcome — say it plainly rather than manufacturing churn.

## Source-of-truth discipline

The point of this skill. The code decides; everything else is a hint at best.

**Not authoritative — never the basis for a change:**

- **Another document.** Two docs agreeing means the error was copied. Go to the code.
- **The backlog** (`docs/backlog/`). An ADR is intent. A doc describing something only the ADR promises is **wrong** until the code has it.
- **Git history, commit messages, PR descriptions.** They say what someone meant to do.
- **The doc's own previous text.** It is the thing under audit. Its age is not evidence.
- **Your prior knowledge of this project**, including anything recalled from memory or an earlier session. Re-verify against the tree in front of you.
- **Comments and TODOs promising future behavior.** A comment is not an implementation.
- **Docstrings**, where they describe more than the code does — good hints, weak witnesses.

**Authoritative:** the code that executes, in this checkout, on this branch. Tests corroborate it. That is the whole list.

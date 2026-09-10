# Out of scope — Tracker connector

Scope fence for [requirements.md](requirements.md). Two axes are easy to confuse and are deliberately kept apart here: the **tracker** (where work items live) and the **code host** (where worc pushes and opens pull requests). This task is about the tracker axis only.

## Not in this task

- **Code-host adapters inside worc** (GitLab merge requests, Azure Repos pull requests, Bitbucket) — worc publishes through `gh` only, so a GitLab Issues + GitLab MR setup needs a publisher adapter in the orchestrator. That is an orchestrator item on the other axis; it gets its own backlog row ("code host adapters") when this folder lands and is not designed here. A GitLab / Azure / Jira **tracker** adapter with the code on GitHub is in scope of the connector's interface and works without it.
- **A tracker-facing HITL channel** — routing the `refinement` / `planning` node's human question to the issue author instead of Telegram would need a `Notifier` implementation inside worc (`notify/interface.py`). Not here; without triage the connector cannot ask the author anything, and that is stated in [happy-path.md](happy-path.md).
- **Webhooks or a GitHub App** — push-based intake needs a publicly reachable endpoint; the connector polls. Revisit if polling latency or API budget ever matters.
- **Running the connector in CI (GitHub Actions cron)** — the agents' CLI logins and worc's host-based sandbox make a hosted runner the wrong place; the connector is a host process next to `worc watch`.
- **A second agent runtime** — the connector never launches Claude Code or Codex itself. Anything that needs a model runs as a worc task inside worc's sandbox (that is what optional triage is).
- **The agent authoring the task that goes to the queue** — with triage on, the agent produces a _report_; deterministic connector code builds the task from it. No path exists in which model output is promoted unreviewed by code.
- **Ingesting issue comments into the task body** — v1 carries the item's own text only. Which comments to trust (the author's? gated maintainers'?) is a policy question logged in [questions.md](questions.md).
- **Multi-repository connectors** — one instance, one tracker repository, one worc clone. Several repositories are several instances (they also need several worc installs today: "Multi-repo/project binding" is its own backlog row).
- **Changing worc's auto-merge policy** — the connector only _sets_ `auto_merge` per task when the operator configured it; the policy, its guard rails and its warnings stay exactly as worc defines them.
- **The triage flow's prompts and nodes** — the switch, the two-step path and the convergence on one task builder are designed here; the flow's actual content (analysis nodes, a reproduction node writing a failing test, the four verdicts) is deferred to its own phase and may change once the connector mechanics have run on a real repository.
- **Per-task budgets, time limits, or model selection driven from tracker labels** — a label→`nodes.<id>.model` mapping is tempting and cheap, but it hands an issue author a lever over cost. Not in v1; if ever, gated the same way as the trigger.

## Deferred (maybe later, not now)

- **Remote connector** (different host or clone) — handing off by committing the task file to the base branch, which `worc watch` already discovers on its tick. Deferred because it needs push rights to a protected branch and a second clone; revisit when an operator actually runs worc and the tracker on different machines.
- **Connector as a worc plugin** (`worc connect …` subcommand, or a hook on the watch tick) — deferred; a separate CLI keeps the install optional and the failure domains apart. Revisit if two processes on one clone prove awkward in practice.
- **GitLab, Azure DevOps and Jira adapters** — the interface is built for them; the adapters follow once the GitHub one has run for real.
- **Connector-side rate-limit and ETag handling** — `gh` already handles auth and pagination; conditional requests matter only at a polling frequency v1 does not use.
- **Attaching the triage report or the failing test to the tracker item** — a comment with a summary is enough for v1; artifacts stay in the repository.

## Explicit non-goals

- We will **not** store connector state inside worc's `state.db` or `.worc/` — the connector owns its own home and its own store; worc's internals are not a contract.
- We will **not** add tracker-specific knowledge to worc — `references:` is a list of opaque strings, `pr_url` is a URL worc already has. worc does not learn what `Fixes #142` means.
- We will **not** add new worc config keys for the connector — the connector is configured by its own file; worc's `config.yaml` is untouched.
- We will **not** add migration or back-compat scaffolding for the `worc list --format json` shape — greenfield; adding a field is the whole story, and the guide names the shape from that release on.
- We will **not** generalize the label state machine into a configurable workflow engine — one fixed set of connector states, mapped by each adapter to its tracker's vocabulary.

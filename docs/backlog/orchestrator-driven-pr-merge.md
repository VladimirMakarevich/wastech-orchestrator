# Orchestrator-driven PR merge (operator-triggered, conflict-resolving)

Status: **accepted** (2026-06-23 — operator-triggered merge + agent-assisted conflict resolution + safety-only gate + flat CLI; see [§ Decision (locked)](#decision-locked)) Date: 2026-06-23 Owner: Vladimir Makarevich

Detail file for the new backlog item _"Operator-driven PR merge — let the operator tell the orchestrator to merge a reviewed, orchestrator-created PR: pull the base branch in, resolve any conflicts (agent-assisted), then merge; plus a CLI surface to see which tasks have open PRs awaiting merge."_ It records how merging works today, the building blocks that already exist, the design of the new operator-triggered merge, and the **locked decision** with its implementation plan ([§ План реализации](#план-реализации)).

## The idea

Today the operator's only ways to land an orchestrator-created PR are (a) turn on `git.auto_merge` and let the orchestrator merge **without review**, or (b) merge by hand on GitHub. The request is a third, human-in-the-loop path: the orchestrator opens the PR, the operator reviews it on GitHub and approves, and then the operator tells the orchestrator to finish the job — pull the base branch into the task branch, **resolve any conflicts (the orchestrator launches a coding agent to do this)**, re-run the checks, and merge. Concretely, an operator-facing command set roughly like:

```
worc prs                 # which orchestrator PRs are open and awaiting merge?
worc merge-task <id>     # operator's go-ahead: update branch w/ base, resolve, merge
```

…backed by a place that already tracks every task and its state, surfaced to the operator.

## TL;DR (findings)

1. **The merge primitive already exists and is safe.** `GitManager.merge_pr` ([git_manager.py:657-696](../../src/wastech_orchestrator/git_manager.py#L657-L696)) merges via `gh pr merge` with a fixed argv, **never `--admin`** (branch protection stays the real gate), no force-push, one attempt, and is idempotent via the `pr_merge` publish op. It is just **not reachable by an operator** — only `_auto_merge` ([orchestrator.py:1609-1640](../../src/wastech_orchestrator/core/orchestrator.py#L1609-L1640)) calls it, and only when `git.auto_merge` resolves true.
2. **`auto_merge` runs in-pipeline, so it never needs to update the branch.** It fires immediately after the PR is created, when the task branch is still fresh off `base_branch` and cannot conflict. The operator merge happens **later**, after `base_branch` has moved on — so **pulling base into the branch and resolving conflicts is the genuinely new behavior**; merging itself is reuse.
3. **State and PR identity are already persisted.** The `tasks` table carries `status`/`branch`; the recorded PR URL lives in the `pr` publish op (`GitManager.recorded_pr_url` — [git_manager.py:300-305](../../src/wastech_orchestrator/git_manager.py#L300-L305)); read-only PR-state probes already exist (`verify_pr_state` MERGED/OPEN/CLOSED — [git_manager.py:307-317](../../src/wastech_orchestrator/git_manager.py#L307-L317); `pr_merge_state` — [git_manager.py:319-338](../../src/wastech_orchestrator/git_manager.py#L319-L338)). "Where do we track tasks and their state?" is already answered — what is missing is a **multi-task read surface** and the **merge action**.
4. **There are clean precedents for both halves.** `status` is the read-only, no-network, DB-only reporter ([cli.py:1225-1282](../../src/wastech_orchestrator/cli.py#L1225-L1282)) — but it shows only the active/latest task. `finalize`/`rerun` ([cli.py:267-321](../../src/wastech_orchestrator/cli.py#L267-L321)) are the operator-invoked, post-terminal, `--dry-run`/`-y`-gated commands that already mutate a finished task's git state. The new commands slot directly beside them.
5. **Orchestrator-level agent runs already exist.** The supervisor calls a provider outside the flow graph via its own durable lineage ([supervisor.py:156-187](../../src/wastech_orchestrator/core/supervisor.py#L156-L187)) — precedent for running the conflict-resolution agent as an **orchestrator routine**, not a new flow node.
6. **It unblocks the dependency graph.** `depends_on` scheduling already treats a task as ready only once its PR is **MERGED** (`pr_merge_state` → `MERGED`, see [task-dependencies.md](task-dependencies.md)). An operator-driven merge is the human-in-the-loop event that flips a dependency to ready — today only `auto_merge` or a manual GitHub merge can.

Net: ~80% of this is wiring existing primitives (`merge_pr`, the PR probes, the publish-op idempotency, `status`-style reporting, `finalize`-style command ergonomics) into an operator command. The one genuinely new capability is **update-branch-with-base + agent-assisted conflict resolution**.

## How merging works today (traced)

1. **PR creation.** The `publish` node opens the PR (`create_pr` — [git_manager.py:626-655](../../src/wastech_orchestrator/git_manager.py#L626-L655)); the URL is recorded in the `pr` publish op. With `git.auto_merge` **off** (the default — [schema.py:222-234](../../src/wastech_orchestrator/config/schema.py#L222-L234)), the task goes terminal `DONE` with the PR left open. That `DONE`-with-open-PR state is exactly the population this feature acts on.
2. **Auto-merge (the only existing merge path).** When `auto_merge` resolves true, `_auto_merge` calls `merge_pr(strategy=git.auto_merge_strategy, wait_for_checks=git.auto_merge_wait_for_checks)`. A **blocked** merge (branch protection / pending checks / conflict) raises `GitCommandError`, which `_auto_merge` converts to `ManualActionRequired` — the task ends `manual_action_required` with the PR **left open for a human** ([orchestrator.py:1636-1637](../../src/wastech_orchestrator/core/orchestrator.py#L1636-L1637)). So a conflict today is a dead-end the operator must finish by hand. **That dead-end is precisely what this feature picks up.**
3. **No branch-update-with-base op exists.** `prepare_branch` checks out base, `pull --ff-only`, then the task branch ([git_manager.py:240-255](../../src/wastech_orchestrator/git_manager.py#L240-L255)); `refresh_base` ff-pulls base only when HEAD is on base ([git_manager.py:356-369](../../src/wastech_orchestrator/git_manager.py#L356-L369)). Neither merges `origin/<base>` **into** a task branch. This is new.
4. **No multi-task list.** `status` reports one task (the given id, else active, else latest — [cli.py:1238-1245](../../src/wastech_orchestrator/cli.py#L1238-L1245)). There is no "show me all open PRs awaiting merge."

## Constraints that bound any solution

From [.agents/rules/architecture.md](../../.agents/rules/architecture.md) and the code as it stands:

1. **Only the orchestrator does commit / push / PR / merge** — never the agent. The conflict-resolution agent may **edit files**; the orchestrator stages, makes the merge commit, and runs `gh pr merge`. (Identical to the whole pipeline: agents edit, the orchestrator commits.)
2. **The core does not know CLI syntax.** All `git`/`gh` lives in `GitManager` (fixed argv, no shell). The new branch-update + merge-commit + abort helpers go there; the orchestration (decide mechanical vs agent, sequence the steps) is core logic that calls `GitManager` + the provider interface — no provider-specific knowledge in the core.
3. **No secrets in logs/DB/artifacts.** Reuse the existing `_run` stderr redaction (`merge_pr` already redacts — [git_manager.py:692](../../src/wastech_orchestrator/git_manager.py#L692)).
4. **Bounded + audited.** The resolution agent run is bounded (timeout + a small attempt cap) and every git/merge op is recorded (`publish_operations`, `provider_attempts`).
5. **Single processing slot.** Merge work touches the one shared clone, so it must not race an active task: refuse when a task is in flight (like the operator commands already assume an idle slot).
6. **`merge_pr` must stay `--admin`-free.** Branch protection / required checks remain the real merge gate — this is how "safety-only" gating (below) is enforced for free.

## Design

### CLI surface (flat, beside `status`/`finalize`/`rerun`)

- **`worc prs`** — list every task whose orchestrator PR is **open and un-merged** (a completed `pr` publish op with no completed `pr_merge`). DB-only and read-only by default, exactly like `status` (`open_readonly`, no providers/git/network — [cli.py:1226-1236](../../src/wastech_orchestrator/cli.py#L1226-L1236)). Columns: `task_id`, `title`, `status`, `branch`, `pr_url`. Optional `--check` enriches each row with **live** GitHub state (`verify_pr_state` / `pr_merge_state`: open? mergeable? conflicting?) — the only mode that touches the network.
- **`worc merge-task <id>`** — the operator's go-ahead (see § Merge routine). Flags mirror `finalize`/`rerun`: `--strategy {merge,squash,rebase}` (default `git.auto_merge_strategy`), `--wait-for-checks/--no-wait-for-checks` (default `git.auto_merge_wait_for_checks`), `--no-resolve` (on conflict, do **not** launch the agent — abort and report; the mechanical-only path on demand), `--dry-run` (print the plan — PR URL, whether the branch is behind, whether a clean base-merge is possible — and write/merge nothing), `-y/--yes` (skip the confirmation; merging is consequential, so confirm by default).
- **`worc tasks` (optional, adjacent)** — list **all** tasks with `status`/`branch` (`--status` filter), the general "see every task and its state" view the idea also asks for. Thin: a store query + a `status`-style print loop. Included if cheap; otherwise a follow-up.

`prs`/`merge-task` are the canonical flat forms of the sketch's `--open-pr list` / `--merge-task` (decision: flat CLI, to match `worc status`/`finalize`/`rerun`).

### Merge routine (orchestrator-level)

`merge-task` is an **orchestrator routine** (not a flow node — see § Why a routine), beside `_auto_merge`:

1. **Resolve + gate.** Refuse if a task is active (single slot). Load the task; resolve the PR via `recorded_pr_url` (refuse if none). Probe `verify_pr_state`: `MERGED` → idempotent success (record + return 0); `CLOSED`/gone → refuse; `OPEN` → proceed. **Safety-only gate** (locked): running the command **is** the go-ahead — we do **not** inspect review state or require approval. The only hard gates are "PR still open" and, implicitly, branch protection (`merge_pr` never uses `--admin`, so a red required check simply blocks the merge and is surfaced).
2. **Update branch with base.** New `GitManager.update_branch_with_base(branch, base)` → checkout the branch, `fetch origin`, `git merge origin/<base>` (a merge commit, **not** a rebase — no history rewrite of reviewed commits, no force-push).
3. **Clean merge → mechanical (the common case).** No conflicts → `push` the updated branch ([git_manager.py:595-620](../../src/wastech_orchestrator/git_manager.py#L595-L620)), then `merge_pr` (reusing `_auto_merge`'s call shape). Record, done. No agent involved.
4. **Conflicts → agent-assisted resolution (locked).** With the conflict markers left in the tree, run **one** conflict-resolution agent via the provider interface (workspace-write, a dedicated role prompt, bounded by timeout + a small attempt cap), the same orchestrator-level provider-call mechanism the supervisor uses ([supervisor.py:156-187](../../src/wastech_orchestrator/core/supervisor.py#L156-L187)). Then **the orchestrator** verifies no markers remain, commits the merge (new `commit_merge_resolution`, finalizing `MERGE_HEAD` — distinct from `commit_code` because a merge stages base's changes too), and **re-runs the checks** (reuse the Check Runner). Checks pass → `push` + `merge_pr` + record. Checks fail or markers remain → a tightly bounded fixing turn (cap 1), then re-test; still failing → bail.
5. **Bail / cleanup.** Any failure (agent can't resolve, checks fail after the cap, exception) → `git merge --abort` (new `merge_abort`) to restore the tree, leave the PR open, exit non-zero with a clear report. The whole conflict path is transactional (abort in `finally`); a stale in-progress merge from a crash is aborted at routine entry and on startup recovery (probe `MERGE_HEAD`). **A `DONE` task is never downgraded to `FAILED`** — the pipeline genuinely succeeded; only the post-hoc merge didn't.

### State machine & status (no new status)

`merge-task` targets any task with an **open, un-merged recorded PR** — typically terminal `DONE`, or `manual_action_required` (e.g. `auto_merge` was blocked by a conflict, § How it works today #2). The gate is "has an open PR," not a specific status.

- **Success:** record the `pr_merge` publish op. If the task was `manual_action_required` **because its merge was blocked**, flip it to `DONE` via `finalize`'s existing terminal-record path (the blocking reason is now resolved). A `DONE` task stays `DONE`.
- **Failure:** PR stays open, task status unchanged, non-zero exit + report; optionally a ledger note for audit.

Following the precedent set by [transient-provider-failure-recovery.md](transient-provider-failure-recovery.md) and the project's posture, **no new task status** is introduced. `merge-task` is a synchronous, attended operator command that holds the slot for its duration and cleans up after itself — it needs no "merging" state, exactly as `finalize`/`rerun` need none.

### Persistence & operator visibility

Reuse what exists — **no schema bump**. `prs` is a new read-only query, `StateStore.find_open_pr_tasks()` (tasks with a completed `pr` op and no completed `pr_merge` op), beside `find_active_tasks`/`latest_task` ([state_store.py:604-615](../../src/wastech_orchestrator/state_store.py#L604-L615)). The merge outcome already persists in the `pr_merge` publish op (the idempotency + audit record `merge_pr` writes). That is the "track every task and surface it to the operator" requirement, satisfied by the existing `tasks` + `publish_operations` tables plus the new read views.

### Why a routine, not a merge flow (the one judgment call)

Two ways to host the agent-assisted path were weighed:

- **A — orchestrator routine (recommended, locked).** A `merge-task` routine beside `_auto_merge` that calls the provider directly for the single resolution run, commits, re-runs checks, merges. **Reuses** `merge_pr`, the PR probes, `push`, `publish_operations`, the Check Runner, and the supervisor's orchestrator-level-provider-call pattern. The common (clean-merge) case needs no agent at all. No new flow YAML, no new publish policy, no terminal-task re-entry semantics.
- **B — a packaged `merge` flow** (`conflict_resolution` agent → `testing` checks, with a bounded fix loop), re-entered like `rerun --continue`, merged as an orchestrator post-flow step. More architecturally "pure" (the engine runs agent work, operators could tune the flow), and the fix-loop/checkpoint/resume come free — **but** it adds a new flow + the dirty-tree-entry + terminal-re-entry-onto-a-different-flow machinery for a single, bounded, mostly-rare step.

**A wins on YAGNI**: conflict resolution is one bounded agent task, not a multi-node graph; interruption is handled by `merge --abort` + re-run (resume is unnecessary); and the supervisor already proves orchestrator-level provider calls are sanctioned. **Revisit B only if** operators need to author/tune the resolution flow — that would justify making it a real operator flow.

## Decision (locked)

**Locked 2026-06-23.** Add an operator-driven merge: **`worc prs`** (read-only list of open, un-merged orchestrator PRs; `--check` for live state) and **`worc merge-task <id>`** (the operator's go-ahead). The routine updates the branch with base; a **clean** base-merge is mechanical (push + reuse `merge_pr`); a **conflicting** one launches **one bounded conflict-resolution agent** (the orchestrator commits the merge and re-runs checks), then merges — all as an **orchestrator routine** beside `_auto_merge`, not a flow. Gating is **safety-only**: the command is the go-ahead; the only hard gates are "PR open" and branch protection (`merge_pr` stays `--admin`-free). **No new task status, no schema bump.** This is the human-in-the-loop counterpart to `auto_merge`, and the event that flips a `depends_on` dependency to ready.

Deliberately **out of scope** (YAGNI / greenfield-MVP): a packaged operator-authorable `merge` flow (Option B — revisit if tuning is needed); a require-GitHub-approval gate (the invocation is the go-ahead); git-worktree isolation for the merge (single clone + slot-guard suffices); batch/multi-PR merge; merging non-orchestrator PRs; a new `blocked`/`merging` status.

## План реализации

Раздел на русском по просьбе владельца. Зафиксированное решение — **operator-driven merge: `prs` + `merge-task`, рутина оркестратора (не флоу), agent-assisted разрешение конфликтов, safety-only гейт, без нового статуса и без bump схемы**. Деление на «Фазы» ниже — логическая структура работ, а не отдельные итерации/мержи. **Проверки и документация — один раз в самом конце**, после всех фаз (`/run-checks`, затем `/sync-docs` и `prettier` по докам) — см. [§ Проверки и документация](#проверки-и-документация).

Целевой сквозной сценарий: оркестратор открыл PR → оператор отревьюил и одобрил на GitHub → `worc merge-task <id>` → подтянуть базовую ветку в задачную → если конфликтов нет — запушить и смержить (механически); если есть — запустить агента-резолвера, оркестратор коммитит мерж и перезапускает проверки → смерж. Просмотр очереди — `worc prs`.

### Зафиксированные решения (ответы на форки)

1. **Глубина разрешения конфликтов — agent-assisted сразу.** При реальных (пересекающихся) конфликтах оркестратор запускает кодинг-агента (Codex/Claude) через провайдер-интерфейс, тот же механизм оркестраторного вызова провайдера, что у супервизора ([supervisor.py:156-187](../../src/wastech_orchestrator/core/supervisor.py#L156-L187)). Чистый авто-мерж базы агента не требует (общий случай).
2. **Гейт — safety-only.** Запуск команды = «добро». Жёсткие гейты только «PR открыт» и protection ветки (`merge_pr` без `--admin`). Review-state и аппрув не проверяем; локальные проверки гоняем только как часть разрешения конфликта.
3. **CLI — плоский** (`worc prs`, `worc merge-task <id>`), рядом с `status`/`finalize`/`rerun`.
4. **Рутина, не флоу** (Вариант A) — см. [§ Why a routine](#why-a-routine-not-a-merge-flow-the-one-judgment-call).
5. **Без нового статуса задачи и без bump схемы** — переиспользуем `tasks` + `publish_operations` и read-вью.

### Фаза 1 — read-surface: `worc prs` (и опционально `worc tasks`)

- **[state_store.py](../../src/wastech_orchestrator/state_store.py)** — `find_open_pr_tasks() -> list[TaskRow]` (задачи с завершённым `pr`-publish-op и без завершённого `pr_merge`), рядом с `find_active_tasks`/`latest_task` ([state_store.py:604-615](../../src/wastech_orchestrator/state_store.py#L604-L615)). Только чтение, без bump схемы.
- **[cli.py](../../src/wastech_orchestrator/cli.py)** — сабпарсер `prs` (по образцу `status`, [cli.py:248-249](../../src/wastech_orchestrator/cli.py#L248-L249)) + `cmd_prs` (`open_readonly`, DB-only печать; флаг `--check` обогащает живым состоянием через `verify_pr_state`/`pr_merge_state`). Диспетч в `main()`. Опционально: сабпарсер `tasks` + `cmd_tasks` (вся таблица, фильтр `--status`).
- **Тесты:** `find_open_pr_tasks` (с `pr`/без `pr_merge` → в списке; с `pr_merge` → нет; без PR → нет); `cmd_prs` DB-only печать и ветка `--check` с фейковым `gh`-раннером.

### Фаза 2 — `GitManager`: подтянуть базу, коммит мержа, abort

- **[git_manager.py](../../src/wastech_orchestrator/git_manager.py)** — новые методы (фиксированный argv, без shell):
  - `update_branch_with_base(branch, base) -> bool` — checkout ветки, `fetch origin`, `git merge origin/<base>`; вернуть, есть ли конфликты (по коду возврата / `MERGE_HEAD` + наличие маркеров);
  - `merge_in_progress() -> bool` — `git rev-parse -q --verify MERGE_HEAD` (для guard и проверки маркеров);
  - `commit_merge_resolution(task_id, message) -> str | None` — застейджить разрешённые пути и финализировать мерж-коммит (отличается от `commit_code` тем, что мерж включает изменения базы; идемпотентность через `publish_operations` как у `_commit`, [git_manager.py:503-541](../../src/wastech_orchestrator/git_manager.py#L503-L541));
  - `merge_abort()` — `git merge --abort` (идемпотентно: no-op без `MERGE_HEAD`).
- **Тесты** (через временный git-репо в существующих git-фикстурах): чистый мерж базы; конфликтный мерж → маркеры → `merge_abort` восстанавливает дерево; идемпотентность `commit_merge_resolution`.

### Фаза 3 — рутина `merge-task` в оркестраторе + CLI

- **[core/orchestrator.py](../../src/wastech_orchestrator/core/orchestrator.py)** — `merge_task(task_id, *, strategy, wait_for_checks, resolve, dry_run)` рядом с `_auto_merge` ([orchestrator.py:1609-1640](../../src/wastech_orchestrator/core/orchestrator.py#L1609-L1640)):
  - refuse, если есть активная задача (один слот); resolve PR через `recorded_pr_url`; `verify_pr_state`: `MERGED` → идемпотентный успех, `CLOSED`/нет → refuse, `OPEN` → дальше;
  - `dry_run` → напечатать план (PR, отставание ветки, возможен ли чистый мерж), ничего не писать;
  - `update_branch_with_base`; **чисто** → `push` + `merge_pr` (форма вызова из `_auto_merge`) + запись;
  - **конфликт** и `resolve` → один агент-резолвер (workspace-write, выделенный role-prompt, лимит по timeout + малый cap попыток), затем оркестратор проверяет отсутствие маркеров, `commit_merge_resolution`, перезапуск Check Runner; pass → `push`+`merge_pr`+запись; fail/маркеры → ограниченный fixing-проход (cap 1) → ре-тест → всё ещё fail → bail;
  - `--no-resolve` или bail → `merge_abort`, PR оставить открытым, не-ноль; **`DONE` не понижать в `FAILED`**; если задача была `manual_action_required` из-за заблокированного мержа и мерж удался — финализировать в `DONE` через путь `finalize`;
  - guard на старте рутины и в восстановлении: `merge_in_progress()` без активной задачи → `merge_abort` (зачистка после краша). Весь конфликтный путь транзакционный (`merge_abort` в `finally`).
- **Role-prompt резолвера** — выделенный пакетный промпт (роль «разрешить конфликты мержа, ничего лишнего не менять»), доставляемый редактируемой копией под `.worc/` согласованно с [[install-seeds-flows-and-prompts]].
- **[cli.py](../../src/wastech_orchestrator/cli.py)** — сабпарсер `merge-task` (по образцу `finalize`, [cli.py:292-321](../../src/wastech_orchestrator/cli.py#L292-L321)): `task_id`, `--strategy {merge,squash,rebase}` (дефолт `git.auto_merge_strategy`), `--wait-for-checks/--no-wait-for-checks` (дефолт `git.auto_merge_wait_for_checks`), `--no-resolve`, `--dry-run`, `-y/--yes`. `cmd_merge_task` + диспетч.
- **Тесты:** интеграция через фейковые CLI (скилл `fake-cli`): (а) чистый мерж базы → смерж; (б) конфликт → агент резолвит → проверки зелёные → смерж; (в) конфликт → агент не справился/проверки красные → `merge_abort`, PR открыт, не-ноль, статус не понижен; (г) идемпотентность (PR уже `MERGED` → успех без повторного мержа); (д) refuse при активной задаче; (е) `--dry-run` ничего не пишет; (ж) `--no-resolve` на конфликте сразу abort.

### Инварианты (соблюдены)

- **Коммит/пуш/PR/merge — только оркестратор**: агент лишь правит файлы при конфликте; мерж-коммит и `gh pr merge` делает оркестратор.
- **Ядро не знает синтаксис CLI**: весь `git`/`gh` — в `GitManager`; рутина зовёт `GitManager` + провайдер-интерфейс.
- **Без `--admin`** (защита ветки — реальный гейт); fixed argv, без shell; stderr редактируется (`_run`).
- **Bounded + audited**: лимит агент-рана; каждый git/merge-op в `publish_operations`/`provider_attempts`. Идемпотентность мержа уже у `merge_pr` (`pr_merge`).
- **Один слот**: рутина refuse'ит при активной задаче и зачищает незавершённый мерж.

### Связи и хвосты

- **Питает `depends_on`**: успешный `merge-task` → `pr_merge_state` = `MERGED` → зависимые задачи становятся eligible (см. [task-dependencies.md](task-dependencies.md)). Сегодня это умеет только `auto_merge` или ручной мерж на GitHub.
- **Hardening (отдельный хвост, уже в трекере):** добавить `--admin` в `security/forbidden_args.py` — централизованный запрет на обход protection, покрывающий и новый путь мержа (строка про forbid-`--admin` в [follow_ups.md](follow_ups.md)).
- **Пересечение с `rerun`**: `rerun --allow-done` (хвост в [follow_ups.md](follow_ups.md)) и `merge-task` оба действуют на терминальные задачи — наименования веток/PR не трогаем (используем сохранённую task branch, default `worc/<id>-<slug>` либо task `branch_name`).
- **Возможные хвосты:** общий `worc tasks` (если не вошёл в Фазу 1); опциональный `git.merge_strategy`/`git.merge_wait_for_checks` отдельно от `auto_merge_*`, если семантика разойдётся (пока переиспользуем дефолты `auto_merge_*` с override во флагах); Вариант B (флоу) при потребности в операторской настройке резолва.

### Проверки и документация

Всё — **один раз после всех фаз**.

- **Проверки:** `ruff check .`, `mypy src`, `pytest` (через `/run-checks`); затем `npx prettier@3 --write "**/*.md"` по затронутым докам.
- **Документация (`/sync-docs`):** [Functional Map](../functional/index.md) (блоки публикации/мержа B07/B08 — добавить операторский merge-путь рядом с `_auto_merge`; B01/B06 — новые CLI-команды и рутина); при изменении топологии — модель C4 в [docs/likec4](../likec4); [docs/operations.md](../operations.md) — операторская заметка про `worc prs`/`worc merge-task` (review→go-ahead→resolve→merge) и взаимодействие с `auto_merge`/`depends_on`; [docs/configuration.md](../configuration.md) — отметить переиспользование `git.auto_merge_strategy`/`auto_merge_wait_for_checks` как дефолтов флагов (bump схемы не нужен).
- **Отложенные хвосты** — в [follow_ups.md](follow_ups.md) (см. § Связи и хвосты).

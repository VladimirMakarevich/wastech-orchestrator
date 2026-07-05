# Разбор прогона: `p4-05-impact-analysis`

## STATUS

- **Задача:** `p4-05-impact-analysis` — «P4.05 — Impact analysis (getImpactSet / classifyImpact)». Четвёртый шаг chain-теста: `branch_mode: existing` + `branch_ref: feat/p4-graph-chain` + `publish: push` (downgrade-only cap) + намеренно невалидный `nodes.implementation.provider: gemini`.
- **Final status:** `done` · **`pr_url: null` в ledger** (правильно — `publish: push` пропускает `create_pr`) · ветка `feat/p4-graph-chain` · **`fix_iterations=1`** (первый fix-цикл во всей P4-кампании).
- **finished_at:** 2026-07-04T21:54:46Z.
- **Провайдеры:** невалидный `gemini`-оверрайд на `implementation` — soft-skip, откат на claude (flow-дефолт); codex на `review` упал (5-е и 6-е подряд идентичных падения, F24), fallback claude оба раза.

## Короткий вывод

Самый насыщенный прогон цепочки: три независимых механизма отработали штатно одновременно. (1) **Невалидный per-node `provider: gemini`** корректно пойман на старте с explicit warning и откатом на flow-провайдера, без abort. (2) **Review реально нашёл настоящий HIGH-баг** (`relativizeImpact` портит топологический `readingOrder` лишней алфавитной сортировкой) — первый живой fix-цикл кампании: `rework` → `fixing` (исправлено за 1 итерацию, с объясняющим комментарием) → повторный `review` → `accept`. (3) **`publish: push` cap сработал буквально**: `publish_operations` содержит `code_commit`+`audit_commit`+`push`, **PR-операции нет вообще** — `pr_url=null` в ledger, но PR #9 всё равно показывает новый коммит на GitHub (потому что PR — это динамическое представление head-ветки, а не снэпшот; `create_pr` просто не звался).

**Главный вывод по данным:** ничего нового по F24 (6/6 падений codex, детерминированность железно подтверждена — больше не отслеживаю). Новых orchestrator-находок нет — все три протестированные механики сработали ТОЧНО как задокументировано в ADR.

## Как прошёл прогон

| Узел | attempt/status | длительность |
| --- | --- | --- |
| planning | claude, 1/succeeded | 219s |
| implementation | claude (gemini-оверрайд отклонён), 1/succeeded | 236s |
| testing #1 | passed | 7s |
| **review #1** | codex crash(F24)→claude, **`rework`** (1 HIGH + 1 MEDIUM) | 101s |
| fixing | claude, 1/succeeded | 89s |
| testing #2 | passed | 7s |
| **review #2** | codex crash(F24)→claude, **`accept`** (2 LOW) | 123s |
| documentation | claude | 119s |
| publish (push-only, без PR) | — | 59s |

### Невалидный provider-override

`level=warning task_id=p4-05-impact-analysis detail="node 'implementation': provider 'gemini' not in agents.allowed ['claude', 'codex']; using the flow's provider" msg="task node override skipped"` — на самом старте, до планирования. `implementation` route-resolved с `primary=claude` сразу после — подтверждает откат на flow-declared провайдер, задача НЕ аборчена. Это первое живое подтверждение §12 checklist-пункта «Fallback на невалидный override — структурный warning + skip + откат, задача не аборится».

### Первый fix-цикл кампании — реальный баг, не косметика

`evaluations.findings_json` (review #1, verdict=`rework`):

> **HIGH** · `relativizeImpact` re-sorts `readingOrder` alphabetically with `.sort(byPath)`. `readingOrder` — топологический порядок из `topologicalSort` (AC3), не path-sorted массив. Лишняя сортировка тихо переписывает топологический порядок алфавитным, когда они расходятся. `packages/core/src/graph/impact-analysis.ts:486`. **MEDIUM** · Тесты используют только цепочки, где топо-порядок совпадает с алфавитным — баг невидим для сьюта.

Фикс проверен на коде ветки (`git show feat/p4-graph-chain:packages/core/src/graph/impact-analysis.ts`): `readingOrder: impactResult.readingOrder.map(relativize)` — сортировка убрана, добавлен объясняющий комментарий (строки 110-113) «readingOrder is topological, not lexical, so it is only mapped — re-sorting it would silently overwrite the topo-sort's reading order with an alphabetical one». `directlyAffected`/`transitivelyAffected` по-прежнему корректно сортируются — фикс точечный, не сломал соседнее поведение. Повторный review нашёл только 2 LOW (то же «phase-doc не обновлён», что и в p4-04, + непокрытый error-path) → `accept`.

### `publish: push` — commit+push без PR

`publish_operations` (`state.db`): `code_commit` + `audit_commit`(noop) + `push` — **никакого `pr`-op**. Ledger `pr_url: null`. Но `gh pr view 9 --repo .../wastech-mdlint` теперь показывает **4 коммита** (p4-02..p4-05) — потому что GitHub PR отслеживает голову ветки динамически; `create_pr` для p4-05 никогда не вызывался, а коммит всё равно «внутри» PR #9 чисто по факту общей ветки. Это ключевой нюанс для интерпретации `publish: push`: PR-метаданные не создаются/не касаются, но видимость коммита в уже открытом PR той же ветки — побочный эффект git, не действие оркестратора.

## Находки

Новых F-номеров нет — все три механики (invalid-provider fallback, fix-loop, publish:push) сработали как задокументировано. F24 (6/6) больше не отслеживается отдельно.

## Что уже хорошо

- **Review — не rubber-stamp, ловит реальные корректностные баги** (первое прямое подтверждение на живом баге, не только на верифицируемых-но-косметических находках из p4-02/p4-03).
- **Fix-loop сошёлся за 1 итерацию** с содержательным объясняющим комментарием в фиксе.
- **`publish: push` — чистый downgrade, ничего лишнего**: нет второго PR, нет попытки создать/обновить PR, только commit+push.
- **Invalid per-node provider override — fail-soft, не fail-fatal**, с чётким диагностическим warning.

## Сводная таблица

| Наблюдение | Причина | Рычаг | Зона |
| --- | --- | --- | --- |
| `provider: gemini` отклонён с warning, задача продолжилась на claude | `node_overrides.resolve_node_overrides` best-effort validated overlay | `core/node_overrides.py` — работает как задумано | orchestrator (подтверждение §12) |
| review нашёл реальный HIGH-баг (readingOrder corruption), fixing починил за 1 итерацию | штатный evaluator+fixing цикл | `core/flow/nodes/evaluator.py` / `fixing` node — работает как задумано | orchestrator (подтверждение) + target (сам баг) |
| `pr_url=null`, нет `pr`-op, но PR #9 показывает коммит p4-05 | `publish: push` останавливается после push; GitHub PR — динамический вид ветки | `git_manager.py` (branch-mode ADR publish cap) — работает как задумано | orchestrator (подтверждение) |
| codex review упал 6/6 подряд идентично | F24 (не новое) | `core/flow/nodes/evaluator.py:57-78` | orchestrator |

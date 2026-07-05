# Разбор прогона: `p4-03-query-layer`

## STATUS

- **Задача:** `p4-03-query-layer` — «P4.03 — Unified graph query layer». Второй шаг branch-mode chain-теста: `branch_mode: existing` + `branch_ref: feat/p4-graph-chain` (продолжает ветку, созданную `p4-02`, вместо своей).
- **Final status:** `done` · **PR: переиспользован** — [#9](https://github.com/VladimirMakarevich/wastech-mdlint/pull/9) (тот же самый, НЕ новый) · ветка `feat/p4-graph-chain`.
- **finished_at:** 2026-07-04T20:47:26Z · `fix_iterations=0`, `decomposed=false`, `attempt=1`.
- **Провайдеры:** claude opus-4-8/high (planning/implementation/documentation); codex gpt-5.4/xhigh на `review` — упал идентично p4-02 (см. F24), fallback claude.
- **Побочки:** PR #9 теперь содержит коммиты ДВУХ задач (`p4-02`+`p4-03`); не смержен.

## Короткий вывод

Второй и главный по значимости для этого прохода результат — **`branch_mode: existing` и PR-reuse работают штатно**: `p4-03` продолжил ту же ветку `feat/p4-graph-chain` без форка новой, и `create_pr` корректно нашёл уже открытый PR #9 и переиспользовал его вместо создания второго. Это подтверждает ключевой сценарий ADR «chain of tasks on one branch». Единственный найденный нюанс — PR-метаданные (title/body) не обновляются при reuse и остаются от первой задачи цепочки (F27, LOW). Отдельно живьём подтверждён предсказанный конфликт F26 (depends_on-merge-gate не знает о chain-continuation) и повторно — F24 (codex-evaluator-crash, 2/2 идентично).

**Главный рычаг этого прохода:** нет нового P0 — F24 (из p4-02) остаётся единственным блокирующим по влиянию; сюда нового P0 не добавилось. Новое здесь — низкоприоритетный F27.

## Как прошёл прогон

| # | Узел | kind | провайдер / модель | attempt / статус | длительность |
| --- | --- | --- | --- | --- | --- |
| — | refinement | agent | — | skipped | — |
| 1 | planning | agent | claude / opus-4-8 / high | 1 / succeeded | 137s |
| 2 | implementation | agent | claude / opus-4-8 / high | 1 / succeeded | 144s |
| 3 | testing | checks | typecheck/lint/test/build | passed | 7s |
| 4 | review | evaluator | codex→**crashed** → claude fallback | 2 / succeeded → `accept` | 93s (~5s crash + fallback) |
| 5 | documentation | agent | claude / opus-4-8 / high | 1 / succeeded | 78s |
| 6 | publish | publish | — | published → **PR #9 переиспользован** | 60s |

- **`branch_mode: existing` работал как задокументировано:** `prepare_branch` зашёл на `feat/p4-graph-chain` без ошибок (`preparing→running` за <1с — ветка уже существовала локально после p4-02, fetch/checkout прошли тихо); ledger `branch=feat/p4-graph-chain`.
- **PR-reuse подтверждён на реальном GitHub:** `gh pr view 9` → `commits`: `feat(p4-02-graph-algorithms)` + `feat(p4-03-query-layer)`, `title`/`body` — буквально от p4-02 (не обновлены, см. F27).
- **F24 повторился идентично** (`process_crashed`, ~5с, тот же `invalid_json_schema`) — второе из двух наблюдений, детерминированность подтверждена, доп. анализ не требуется (см. `p4-02-graph-algorithms-run-analysis.md`).
- **F26 подтверждён живьём ДО правки task-файла:** `worc run` с исходным `depends_on: [p4-02-graph-algorithms]` (PR #9 открыт) → `error: refusing to run p4-03-query-layer: dependency 'p4-02-graph-algorithms' PR is OPEN (unmerged)`, exit 2.

## Находки

Полные записи — [TEST-FINDINGS.md](../../TEST-FINDINGS.md) F26 (депендс-он не интегрирован с chain), F27 (PR-reuse не обновляет метаданные). F24 (codex-схема) повторно наблюдён, без нового контента к уже записанному.

## Пробелы в данных

Нет новых — та же наблюдаемость, что и в прогоне p4-02 (prompt-audit присутствует).

## Что уже хорошо

- **`existing`-mode + PR-reuse — оба работают штатно с первого раза**, без ручного вмешательства: ветка продолжена, PR не задублирован.
- **Fallback на codex-крах отработал стабильно во второй раз подряд** — предсказуемое, а не хрупкое поведение.
- **Explicit-run merge-gate отказал корректно и с понятным (хоть и неполным по диагностике) сообщением** до каких-либо побочных эффектов.

## План исправлений

### P1

- **F27 — обновлять PR title/body при reuse (best-effort).** [git_manager.py:1012-1015](../../src/wastech_orchestrator/git_manager.py#L1012). Не блокирует функциональность, но улучшает читаемость PR для ревьюера на длинной цепочке.

### P2

- **F26 — задокументировать взаимоисключаемость `depends_on` и chain-`branch_mode`** в `branch-mode.md`/`task-authoring.md` (см. полную запись в TEST-FINDINGS.md).

## Сводная таблица

| Наблюдение | Причина | Рычаг (file:line) | Зона |
| --- | --- | --- | --- |
| PR #9 переиспользован для p4-03 (не создан #10) | `_find_open_pr` находит открытый head→base PR, `create_pr` возвращает его URL | `git_manager.py:992-1015` — работает как задумано | orchestrator |
| PR #9 title/body всё ещё «P4.02» после добавления p4-03 | reuse-путь не вызывает `gh pr edit` | `git_manager.py:1012-1015` | orchestrator |
| `worc run p4-03` отказал на `depends_on` при открытом PR предыдущей chain-задачи | merge-gate не знает о branch_mode chain-continuation (см. F26 полностью) | `core/orchestrator.py:745-763` | orchestrator |
| codex review упал повторно, идентично p4-02 | `_FINDINGS_SCHEMA` без `additionalProperties:false` (F24, не новое) | `core/flow/nodes/evaluator.py:57-78` | orchestrator |

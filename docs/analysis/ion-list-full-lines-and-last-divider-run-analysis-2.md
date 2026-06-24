# Ре-ран после фикса: ion-list-full-lines-and-last-divider (версия 0.5.4a1)

STATUS: VERIFIED. Повторный сквозной прогон ранее падавшей задачи на той же конфигурации **прошёл узел `review` и дошёл до зелёного PR**. Финальный статус `done`, PR создан: https://github.com/VladimirMakarevich/argudebate/pull/2 (`auto_merged=false` — мерджа нет, как и задумано). Это проверочный прогон обновлённого оркестратора; код/конфиг/флоу оркестратора не менялись.

Этот отчёт — продолжение [ion-list-full-lines-and-last-divider-run-analysis.md](ion-list-full-lines-and-last-divider-run-analysis.md) (прогон-«было», версия 0.5.3a1, `failed` на `review`). Здесь — «стало» и сравнение находка-за-находкой.

## Короткий вывод

Та же задача на той же целевой конфигурации (узел `review` приколочен к `codex` / `gpt-5.5` / `reasoning: high`) на версии **0.5.4a1** отработала полностью: `refinement(skip) → planning → implementation → testing(5/5) → review(accept) → documentation → publish`, статус `done`, PR #2. Прежние ошибки `unsupported_version` и `process_crashed`/`model_not_found` **не повторились ни разу**.

Главное отличие первопричины «было → стало»: оркестратор больше **не** шлёт codex несуществующий флаг `--reasoning-effort`. Теперь reasoning уходит как `-c model_reasoning_effort="high"` — этот вариант codex 0.139.0 принимает. В результате codex-ревью **успешно выполнилось напрямую** (182.6 с, exit 0), и цепочка прошлого падения оборвалась на самом первом звене: раз codex не падает, fallback на claude вообще не запускается, а значит и утечка codex-модели в claude (находка 1) физически не может произойти. Иными словами, прогон спасла находка 2 (флаг reasoning), а не находка 1 (сброс модели при fallback) — последняя в коде присутствует, но в этом прогоне в рантайме не задействовалась.

## Условия проверки (что и как воспроизводилось)

- **Версия:** `wastech-orchestrator 0.5.4a1` (прошлый прогон — 0.5.3a1). Проверяем именно её.
- **Конфиг:** `upgrade-config` — `already up to date (schema_version 15)`; правок схемы не потребовалось.
- **Узел `review` приколочен к codex/gpt-5.5 — намеренно.** В активной копии целевого флоу узел `review` к моменту проверки оказался **откреплён** (прошлая фикс-сессия вернула его на глобальный primary `claude` и задокументировала причину). Это противоречило исходной посылке задачи. По решению оператора узел перевозвращён к прежней падавшей конфигурации (`provider: codex`, `model: gpt-5.5`, `reasoning: high`), чтобы прогон реально проходил через узел, на котором всё падало. После проверки этот пин по решению оператора **оставлен** (codex теперь отрабатывает review штатно), то есть в финальном состоянии узел `review` остаётся приколоченным к codex/gpt-5.5.
- **Сверка состояния перед ре-раном.** `rerun` берёт исходник из записанного в `state.db` `source_path` (= `tasks/failed/<id>.md`), а файл после прошлой фикс-сессии лежал в `tasks/pending/`. Чтобы `rerun` нашёл исходник, файл перемещён `tasks/pending → tasks/failed` и закоммичен локально на `master` (коммит `5fb6923`, не запушен) — это сверка жизненного цикла задачи, не правка кода/конфига. Заодно это сохранило аномалию «файл задачи трекается в базовой ветке» — то самое условие, что вскрывало баг грязного дерева (находка 6). **После проверки этот коммит снят с `master`** (`git reset --hard origin/master`): локальный `master` восстановлен в исходное `259b178` (== `origin/master`), дерево чистое, файл задачи вернулся в `tasks/pending/`. Коммит `5fb6923` остаётся только в истории PR #2 (его первый коммит) — это безвредно: итоговый file-дифф по файлу задачи — обычное перемещение `pending → done`.
- **Свежий ре-ран (не `--continue`):** `worc … --heartbeat-seconds 60 rerun --force-reset-remote -y ion-list-full-lines-and-last-divider`. Флаг `--force-reset-remote` (одобрен оператором) удалил устаревшую удалённую ветку прошлого падения (открытого PR на ней не было); `-y` — для неинтерактивного фонового запуска.
- **Preflight:** зелёный (codex 0.139.0 и claude 2.1.186 доступны и аутентифицированы; флоу `implementation` валиден и с пином codex/gpt-5.5). Важно: preflight не гоняет боевой `codex exec`, поэтому подтверждением фикса служит сам прогон, а не preflight.

## Как прошёл новый прогон (факты)

- **Финал:** `final_status=done`, `attempt=2`, `rerun_of=ion-list-full-lines-and-last-divider`, `pr_url=…/pull/2`, `auto_merged=false`, `fix_iterations=0`, `terminal_cleanup=completed`, `failure_report=null`, `cleanup_safe=true`.
- **Путь по флоу (node_runs):** `refinement(skipped) → planning✓(×2) → implementation✓ → testing✓(pass) → review✓(accept) → documentation✓ → publish(published)`. Достигнуты и `documentation`, и `publish`, которых прошлый прогон не видел.
- **Провайдеры/модели:** `planning`/`implementation`/`documentation` — `claude` (глобальный primary). `review` — `route_primary=codex, route_fallback=claude, source=flow_node, provider_used=codex` (пин сработал). `supervisor` — `claude`.
- **Узел `review` (главное):** один-единственный attempt `1-codex`, `status=succeeded`, `exit_code=0`, `error_class` пуст, `stderr.log` = 0 байт. Каталога `2-claude` **нет** — fallback не запускался. Вердикт `accept`.
- **Тайминги (≈):** planning#1 326.6 с, **ожидание HITL ≈ 11.7 мин**, planning#2 239.4 с, implementation 236 с, testing ≈48 с (lint 16.1 / build 14.7 / audit:i18n 0.34 / audit:styles 0.30 / test:ci 15.7), review (codex) 182.6 с, documentation 108.9 с; шесть вызовов supervisor по 15–43 с. Полное «настенное» время ≈ 33 мин (из них ≈12 мин — пауза на ответ человеку); активные вычисления ≈21 мин.
- **Проверки:** все 5 (`lint`, прод-`build`, `audit:i18n`, `audit:styles`, `test:ci`) — `passed`, exit 0, как и в прошлый раз. Подтянулся только набор `mobile`.
- **Дифф/PR:** 14 файлов в diffstat (+61/−18): 11 исходников шаблонов/компонентов (`safety-resources`, `completion-history`, `conversation-notes`, `conversation-list-item` .html+.ts, `preparing-section-item` .html+.ts, `conversations.page`, `hidden-conversations-modal`, `sync-conflicts`, `topic-backlog`) + раздел в `mobile/docs/ionic-angular-best-practices.md` (узел `documentation`) + перемещение файла задачи + `summary.md`. **Без** правки `.gitignore`.

## Было → стало (сводка по находкам прошлого разбора)

| Находка прошлого разбора | Было (0.5.3a1) | Стало (0.5.4a1) | Статус |
| --- | --- | --- | --- |
| **2. codex `--reasoning-effort` не поддержан** | codex упал мгновенно: `unexpected argument '--reasoning-effort'`, exit 2, `unsupported_version` | argv: `--model gpt-5.5 -c model_reasoning_effort="high"`; codex отработал 182.6 с, exit 0 | **Закрыто, подтверждено в рантайме** |
| **1. Cross-provider fallback тащит чужую `model`** | fallback codex→claude → claude получил `gpt-5.5` → 404 `model_not_found` → `process_crashed` | fallback не запускался (codex не падал); сброс `model`/`reasoning`/`extra_args`/`session_id` присутствует в коде | **Закрыто в коде; в рантайме не задействовано** (триггер исчез) |
| **3. Инфра-падение evaluator фатально** | `NodeInfraError` → `_fail` → `failed`, зелёный дифф утоплен, PR нет | evaluator не падал (review прошёл). В коде: `EvaluatorInfraError` → `MANUAL_ACTION_REQUIRED` (ветка сохраняется) | **Закрыто в коде; в рантайме не задействовано** |
| **6. Грязное базовое дерево (висящий `D`)** | после прогона на `master` болталось неустейдженное удаление `tasks/pending/<id>.md` | после прогона `git status` чист, `cleanup_safe=true` | **Закрыто** (см. оговорку ниже) |
| **4. Надзор supervisor: коллизия путей, пустые заметки** | 1 из 3 наблюдений; все писали в `run-000000`; 2 падали мгновенно с пустой заметкой | 6/6 вызовов успешны; уникальные пути `run-000031…035` + summary; содержательный `summary.md` | **Закрыто/существенно улучшено** |
| **5. Нет `failure_report.json`/`stuck.md` у `failed`** | у терминального `failed` отчёт не сгенерирован | прогон успешен, `failure_report=null` — корректно; путь инфра-падения в рантайме не воспроизводился | **Не воспроизводилось** (нет падения) |
| **7. Посторонняя правка `.gitignore` в диффе** | `.gitignore` менялся в коммите реализации | `.gitignore` в диффе PR отсутствует | **Не повторилось** |
| **8. Набор проверок шире ТЗ** | прошло, вреда нет | прошло, вреда нет | без изменений |

## Подробно: что подтверждено в рантайме

### Находка 2 (`--reasoning-effort`) — ЗАКРЫТА, главный фикс этого прогона

Прямое доказательство из `stages/review/run-000034/1-codex/request.json`:

```
codex --ask-for-approval never exec --cd <repo> --sandbox read-only --json
  --output-last-message <…>/last-message.txt --model gpt-5.5 -c model_reasoning_effort="high" -
```

`--reasoning-effort` в argv **отсутствует**; reasoning передан как `-c model_reasoning_effort="high"`. Соответствующий рычаг в оркестраторе — [providers/codex.py:179](../../src/wastech_orchestrator/providers/codex.py#L179) (`argv += ["-c", f'model_reasoning_effort="{effort}"']`) плюс preflight-проба `codex exec --help` на наличие `-c/--config` ([providers/codex.py:276–291](../../src/wastech_orchestrator/providers/codex.py#L276-L291)). codex 0.139.0 этот вызов принял; ревью выполнилось (182.6 с, exit 0). Заодно выяснилось, что `gpt-5.5` для codex 0.139.0 — валидная модель (хотя в конфиге codex стоит `gpt-5.4`): codex её принял и отработал, а не вернул ошибку модели.

### Находка 6 (грязное дерево) — ЗАКРЫТА

После прогона `git status -s` на `master` пуст, `cleanup_safe=true`, висящего `D` нет (в прошлый прогон тут болталось неустейдженное удаление файла задачи). Единственным делтой оставался намеренный локальный сверочный коммит `5fb6923` (+1, не запушен) — он снят после проверки (`git reset --hard origin/master`), `master` восстановлен в `259b178`. Рычаг — [git_manager.py:575–583](../../src/wastech_orchestrator/git_manager.py#L575-L583): в pathspec audit-коммита теперь входят и исходные состояния — `for state in ("done", "failed", "pending", "processing")`, поэтому удаление файла из исходной папки при перемещении жизненного цикла стейджится.

Оговорка о честности теста: в этом прогоне файл задачи на `master` лежал в `tasks/failed/` (сверочный коммит), а перемещение `failed → done` оркестратор делал на ветке агента и закоммитил audit-коммитом. На `master` ничего не менялось, поэтому после `checkout master` дерево тривиально чистое. Это подтверждает **итог** (чистая база после прогона), но воспроизводит чуть иной механизм, чем прошлое падение (`pending → failed`). Сам фикс кортежа состояний при этом адресует именно прошлый сценарий.

### Находка 4 (наблюдаемость supervisor) — УЛУЧШЕНО

В отличие от прошлого прогона (все наблюдения писали в `run-000000`, 2 из 3 падали), здесь шесть вызовов supervisor успешны и пишут в **уникальные** каталоги `stages/supervisor/run-000031…035` (по числу проверяемых шагов) + финальный whole-task summary. `summary.md` — содержательный: фиксирует решение HITL «исключить dev-sandbox/demos», «нулевые fix-циклы», отсутствие out-of-scope правок в финальном диффе и отдельно отмечает откат более раннего переусердствовавшего черновика.

## Что НЕ проверено в рантайме (присутствует в коде, но триггер не наступил)

- **Находка 1 (сброс `model`/`reasoning` при cross-provider fallback).** Рычаг присутствует — [routing/router.py:403–412](../../src/wastech_orchestrator/routing/router.py#L403-L412): при смене провайдера `_build_request` обнуляет `model`, переразрешает `reasoning` через `map_reasoning_for_provider_switch`, чистит `extra_args` и `session_id`. Но в этом прогоне fallback **не наступил**: codex не падал, замены провайдера не было. Это закономерное следствие закрытия находки 2 — у прошлой утечки исчезло первое звено цепочки. Чтобы реально прогнать путь codex→claude, нужна конфигурация, где codex именно падает (а с фиксом флага и валидной `gpt-5.5` это уже не так).
- **Находка 3 (инфра-падение evaluator → `MANUAL_ACTION_REQUIRED`).** Рычаг присутствует — [core/orchestrator.py:1203–1213](../../src/wastech_orchestrator/core/orchestrator.py#L1203-L1213) ловит `EvaluatorInfraError` и деградирует в `MANUAL_ACTION_REQUIRED` вместо `failed`. В рантайме не сработал: review выполнился штатно.
- **Находка 5 (`failure_report.json`/`stuck.md` на инфра-падениях).** Прогон успешен (`failure_report=null` корректно), путь не воспроизводился.

## Новые наблюдения этого прогона

1. **HITL-уточнение на `planning` (недетерминированно).** На этот раз planning сам задал вопрос через Telegram (`@w_orc_bot`, `message_id=14`): включать ли в нормализацию внутренние dev-sandbox/demos-списки. Ответ — `EXCLUDE dev-sandbox/demos` (совпал с рекомендацией самого агента; артефакт `.worc/logs/<id>/hitl/planning.json`). Оркестратор корректно поставил прогон на паузу, доставил вопрос, применил ответ и перезапустил `planning` (run-000030 → run-000031) с контекстом ответа — это и дало два запуска planning и паузу ≈12 мин. Это **не баг**, а штатный HITL; в прошлый прогон агент вопрос не задавал. Замечание для безнадзорных запусков: прогресс зависит от своевременного ответа в Telegram (есть дедлайн-механика).
2. **Двойной `planning` = доп. время/токены.** Из-за HITL planning отработал дважды (326.6 с + 239.4 с). Терпимо, но это стоимость уточнения.
3. **Уже скоуп, чем прошлый прогон.** Прошлый дифф — 19 файлов, включая dev-поверхности (demos, firebase/remote-config-debug, logs, performance-monitor). Этот — ~11 исходников user-facing-списков, dev-поверхности исключены (решение HITL), классифицированы как «оставлено как есть». Критерии задачи выполнены, но **дифф намеренно отличается** от прошлого; человеко-визуальная приёмка остаётся главным остаточным риском (см. caveat в `summary.md`).
4. **Артефакты, внесённые ради проверки (для прозрачности, не часть продукта):** пин `review` → codex/gpt-5.5 (по решению оператора оставлен в финале — codex отрабатывает); локальный сверочный коммит `5fb6923` на `master` (перемещение файла задачи, не запушен) — **снят после проверки** (`git reset --hard origin/master`), локальный `master` восстановлен в `259b178`; удаление устаревшей удалённой ветки прошлого падения (`--force-reset-remote`). Коммит `5fb6923` остаётся только в истории PR #2 (его первый коммит) — безвреден, итоговый file-дифф по файлу задачи — обычное `pending → done`.

## Доказательная база

- Леджер: `.worc/logs/completed.jsonl` (последняя запись: `final_status=done`, `pr_url=…/pull/2`, `attempt=2`).
- `state.db`: `node_runs` (`review`: `succeeded/accept/codex`; `publish: published`), `provider_attempts` (node_run 34: codex/attempt 1/succeeded/exit 0, единственный), `tasks.status=done`, `node_lineage` (`__supervisor__`).
- Узел review: `stages/review/run-000034/1-codex/{request.json,result.json,events.jsonl}`, `stderr.log` = 0 байт; каталога `2-claude` нет.
- HITL: `.worc/logs/<id>/hitl/planning.json` (`answer="EXCLUDE dev-sandbox/demos"`, `delivered=true`).
- Supervisor: `stages/supervisor/run-000031…035` + summary; `summary.md`.
- Git: `git status` чист; PR https://github.com/VladimirMakarevich/argudebate/pull/2 (open).
- Кросс-ссылки на рычаги оркестратора: `routing/router.py:403–412`, `providers/codex.py:179` и `:276–291`, `core/orchestrator.py:1203–1213`, `git_manager.py:575–583`.

## Итог проверки

Обновлённый оркестратор (0.5.4a1) на прежней (ранее падавшей) целевой конфигурации **доводит задачу до зелёного PR**. Подтверждённо закрыты в рантайме: находка 2 (флаг reasoning для codex) и находка 6 (чистое базовое дерево); существенно улучшена находка 4 (наблюдаемость supervisor); не повторилась находка 7 (`.gitignore`). Находки 1, 3 и 5 присутствуют в коде, но в этом прогоне их триггеры не наступили — прогон спасён на более раннем звене (находка 2), поэтому путь fallback/фатального evaluator не задействовался. Новых дефектов оркестратора не выявлено; единственное новое поведение — недетерминированный HITL-вопрос на `planning`, отработавший штатно.

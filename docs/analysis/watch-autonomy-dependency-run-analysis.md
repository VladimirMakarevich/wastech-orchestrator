# Разбор автономного watch-прогона цепочки задач (depends_on + auto_merge) и план улучшений

Анализируемый прогон: автономный `watch`-демон оркестратора (`wastech-orchestrator 0.5.4a1`) на реальном репозитории `debates` / `argudebate` (`/Users/a1234/Documents/GitHub/debates`). Прогонялась цепочка из двух зависимых задач под `mobile/`:

- **A** = `unify-input-alert-styling-and-buttons` (без `depends_on`) — пошла первой;
- **B** = `redesign-form-controls-to-match-demo` (`depends_on: [unify-input-alert-styling-and-buttons]`) — ждала мержа A.

Конфиг под тест (только два ключа): `orchestrator.auto_mode.enabled: true`, `git.auto_merge: true` (стратегия `squash`, `auto_merge_wait_for_checks: false`). `poll-seconds` переопределён флагом на запуске = 60. `security.denied_commands` не тронут (агенту по-прежнему запрещены `gh pr create` / `gh pr merge` — мержит сам оркестратор). Дополнительно оператор по ходу теста сменил `repo.branch_prefix: agent → worc` (учтено; на механизмы не влияет, ветки идут с префиксом `worc/`).

---

## STATUS

Цель теста — три механизма автономной оркестрации. Все три **подтверждены в рантайме**.

| Механизм | Вердикт | Чем доказано (кратко) |
| --- | --- | --- |
| (1) watch: демон сам подхватывает `tasks/pending`, между тиками `fetch` + ff-only `pull` base | **PASS** | A и B подхвачены автономно; на тике 2 локальный `master` уехал на merge-коммит A `a34f67f` (refresh_base сделал fetch+pull) |
| (2) depends_on (merge-gated): зависимая ждёт, пока зависимость не **MERGED**, и пропускается non-blocking | **PASS** | B пропущена на тике 1 (WAITING), первый node_run B (18:39:54Z) **позже** мержа A (18:38:49Z); ложного BROKEN/цикла нет |
| (3) auto_merge: оркестратор сам мержит PR зависимости → разблокирует зависимую | **PASS** | PR #3 (A) сквош-смержен немедленно за 5.08 с, SHA `a34f67f`, GitHub `MERGED`; ветка B построена прямо поверх `a34f67f` |

| Задача | Финальный статус | PR / merge | Циклы фиксов | Итог |
| --- | --- | --- | --- | --- |
| **A** | `done` | PR #3 → `MERGED` (`a34f67f`), `auto_merged=true` | 0 | **SOLVED** — чисто, в скоупе, summary содержательный |
| **B** | `failed` (узел `implementation`) | нет PR; частичная работа закоммичена+запушена на ветку B | 0 | **FAILED, но не из-за механизмов** — `error_max_turns` (исчерпан бюджет ходов), ошибочно классифицирован как `process_crashed` |

---

## Короткий вывод

Сами три механизма автономности отработали безупречно: демон сам провёл A до зелёного PR и **сам его смержил**, на следующем тике подтянул обновлённый `master` и автономно взял в работу B на правильной базе. Гейт зависимости отработал ровно по инварианту `_dependency_merged` — B физически не стартовала, пока PR A не оказался `MERGED`. С точки зрения теста — **полный PASS по всем трём пунктам**.

Провалилась B, но **не на оркестрации, а на исполнении**: широкая задача «привести в порядок ВСЕ form-controls приложения» не уложилась в бюджет `--max-turns 50` на единственном узле `implementation` (claude-opus-4-8, xhigh) — CLI вернул штатный терминальный результат `error_max_turns` на 51-м ходу. Оркестратор же из-за того, что claude при этом выходит с кодом `1`, **классифицировал чистое исчерпание ходов как `process_crashed` («процесс завершился аварийно»)** и уронил задачу без ретрая, потеряв в логе и отчёте реальную причину.

Единственный главный рычаг: в адаптере провайдера разбирать терминальное событие потока **до** ветки «ненулевой код выхода = инфра-сбой», чтобы `error_max_turns` (и прочие `is_error`-подтипы) классифицировались по своей сути, а не сваливались в `process_crashed`. Рычаг — [providers/\_adapter_base.py:336-345](../../src/wastech_orchestrator/providers/_adapter_base.py#L336-L345) + [providers/claude.py:318-322](../../src/wastech_orchestrator/providers/claude.py#L318-L322). Вторично — для широких задач включать декомпозицию или поднимать `max_turns`.

Важно: автономность оказалась **не на 100% безлюдной** — планирование B осознанно задало оператору в Telegram вопрос про скоуп (auth/dev-sandbox), и демон ~8 мин ждал ответа человека. Это штатный HITL (узел `planning` имеет `allow_question: true`), агент задал хороший вопрос, человек ответил — но в операторском логе пауза **никак не видна** (просто провал в 8 минут без строк).

---

## Как прошёл прогон (факты)

### Окружение и префлайт

- Версия: `wastech-orchestrator 0.5.4a1`; провайдеры `codex 0.139.0` и `claude 2.1.186` — авторизованы; `strict_isolation` — enforced; флоу `implementation`/`deep_research`/`security_audit` — валидны; Telegram-бот `@w_orc_bot` готов. Префлайт зелёный (exit 0).
- Защита ветки `master`: репозиторий **приватный без GitHub Pro**, поэтому classic branch protection и rulesets отдают `403` («Upgrade to GitHub Pro»). То есть защиту включить нельзя → `master` не защищён → немедленный `gh pr merge --squash` (без `--admin`) проходит. Это благоприятный для теста исход; риск «защищённый master → merge заблокирован» в этом репозитории не реализуется.
- Тулчейн на месте: node v24.8, npm 11.6, dotnet, `mobile/node_modules` установлен. Поэтому проверки реально **выполняются** (не «skipped»), и блокировка авто-мержа по `task_had_skipped_checks` ([orchestrator.py:1537](../../src/wastech_orchestrator/core/orchestrator.py#L1537)) не срабатывает.

### Канва watch-тиков (poll = 60 с)

- **Тик 1 (20:18:49 / 18:18:49Z):** `refresh_repo` (старт на `master`, по сути no-op). `watch_once` сканирует `pending` в алфавитном порядке → `redesign…` (B) идёт первой → проверка `depends_on` → A ещё не запускалась → **WAITING-skip** (`"task redesign-form-controls-to-match-demo waiting: dependency 'unify-input-alert-styling-and-buttons' is pending (not yet run)"`), `continue`. Затем `unify…` (A) → нет зависимостей → `run_task(A)`. A проходит весь пайплайн до `done`+merge (≈20 мин). Так как B уже была пропущена раньше в этом же цикле for, повторно она в тике 1 не берётся.
- **Тик 2 (≈20:39:51 / 18:39:51Z):** `refresh_base` делает `fetch origin` + `pull --ff-only` → локальный `master` уезжает на `a34f67f` (merge-коммит A). `watch_once`: в `pending` осталась только B → `_dependency_merged(A)` видит `DONE` + PR `MERGED` → **ELIGIBLE** → `run_task(B)`. B стартует на обновлённой базе.
- **Тик 3 (≈19:14:52Z, после падения B):** файл B снова оказался в `tasks/pending/`, демон пере-сканировал его и отклонил с `validation_reason: duplicate_task_id` (id уже терминален в сторе). Безвредно, но шумно (см. находку 3).
- Дальше тики вхолостую (в `pending` пусто), пока демон не остановлен явно: `worc stop` → `stop: watcher 9473 stopped` (exit 0), завершился штатно по SIGTERM между тиками.

### Задача A — `unify-input-alert-styling-and-buttons` (done)

- Путь по флоу: `refinement(skip)` → `planning✓` → `implementation✓` → `testing✓(pass)` → `review✓(accept)` → `documentation✓` → `publish(published)` → **auto-merge**. Циклов фиксов: 0 (`test_fix_cycles=0`, `review_fix_cycles=0`).
- Модели/reasoning: `planning`/`implementation` — `claude-opus-4-8`, `xhigh`; `documentation` — `claude`, `medium` (переопределение в флоу); `review` — `codex`/`gpt-5.5`/`high` (пин узла, `source=flow_node`); `supervisor` — `claude`, `medium`.
- Тайминги (UTC): planning 18:18:51→18:22:55 (≈244 с), implementation 18:23:35→18:25:55 (≈140 с), testing 18:26:15→18:27:02 (≈47 с), review 18:27:26→18:29:19 (≈113 с), documentation 18:29:36→18:30:57 (≈81 с), затем supervisor пишет финальный summary (≈18:37:52→18:38:18), publish 18:38:18→18:38:46, auto-merge 18:38:46→18:38:51 (5.08 с). Полное время A ≈ 20 мин.
- Проверки: набор **только `mobile`** (5/5: lint, build 14.8 с, audit:i18n 0.4 с, audit:styles 0.3 с, test:ci 16.4 с — все exit 0). Бэкенд-набор `.NET` **не подтянулся** (диф чисто `mobile/`, «ничьих» путей нет) — спираль из разбора task-023 не повторилась.
- `review`: единственный attempt `1-codex`, `accept`, **без fallback на claude** (каталога `2-claude` нет) — фикс codex-reasoning из 0.5.4a1 держится, утечки модели нет.
- Диф (по merge-коммиту `a34f67f`, файл `current.diff` = 6115 байт): `mobile/src/theme/feedback.scss` (+29/-…) + три call-site (`conversation-thread.page.ts`, `conversation-notes.page.ts`, `remote-config-debug.page.ts`) + один docs-файл (узел documentation) + перемещение файла задачи + `summary.md`. **Строго в скоупе, без расползания.**
- Соответствие критериям приёмки: класс `conversation-notes-text-alert` обобщён в `app-input-alert` (input+textarea, токены), у `editPersonalLabel()` добавлен `app-input-alert-split` с раскладкой Cancel+Clear слева / Save справа, `maxlength:100` сохранён, аудит всех `inputs`-алертов выполнен (radio-алерты корректно исключены). Единственный объективно непокрытый пункт — **визуальная проверка светлой/тёмной темы** (её не делает ни один гейт; супервайзер это честно отметил).
- Стоимость (реальная, не подписка): planning $1.28 (8 ходов), implementation $0.80 (19 ходов), documentation $0.91 (13 ходов) + codex-review + ~6 вызовов supervisor → A ≈ **$3–4**, результат — смерженный PR.
- PR #3: `state=MERGED`, `mergedAt=2026-06-24T18:38:49Z`, `mergeCommit.oid=a34f67f…`, `baseRefName=master`. Леджер: `final_status=done, auto_merged=true, merge_outcome=a34f67f…, fix_iterations=0, attempt=1, cleanup_safe=true`.

### Задача B — `redesign-form-controls-to-match-demo` (failed)

- Путь по флоу: `refinement(skip)` → `planning✓ #1` → **[HITL-пауза ≈8 мин]** → `planning✓ #2` → `implementation✗ (process_crashed)` → терминальный `failed`. До `testing`/`review`/`publish` не дошла.
- Тайминги (UTC): planning #1 18:39:56→18:49:01 (≈545 с, 8 ходов, $3.00), пауза на ответ человека ≈18:49→18:57, planning #2 18:57:12→19:01:08 (≈236 с, 7 ходов, $1.20), implementation 19:01:34→19:13:45 (≈731 с, claude, **51 ход**, $5.25) → `error_max_turns`. Полное время B ≈ 34 мин. Стоимость B ≈ **$9.45 и ноль PR** (вся работа осталась частичной).
- HITL: на planning агент через `human_input` задал оператору вопрос (Telegram message_id 20, `kind=question`): «Входят ли auth-страницы (login/signup/forgot-password) в скоуп? Они оборачивают `ion-input` в `ion-item` (нарушение критерия #4), но это намеренный glass-morphism-дизайн, исключённый из `audit-styles.js`. Рекомендация: ИСКЛЮЧИТЬ». Человек ответил: «no, auth pages need to skip and as well as dev-sandbox pages. EXCLUDE them» — то есть расширил исключение и на dev-sandbox. Доказательство: `logs/redesign-…/hitl/planning.json` (`status: answered`). Агент исключение **соблюл** (в частичном дифе нет ни auth/, ни dev-sandbox/).
- Падение: `implementation`, единственный attempt claude, `error_class=process_crashed`, `exit_code` в сторе пуст. Реальная причина — в потоке событий: `{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":51,"duration_ms":729407,"total_cost_usd":5.25,"output_tokens":56305}` (`stages/implementation/run-000047/1-claude/events.jsonl`, хвост). `stderr.log` = 0 байт. То есть claude **штатно** сообщил «исчерпан лимит ходов», а оркестратор это расценил как аварию.
- Частичная работа НЕ потеряна: на ветке `worc/redesign-…` два коммита — `7d98539 "failed attempt — Redesign…"` (частичный рефакторинг) + `37f845d` (audit trail), ветка **запушена** на origin; PR не открыт (`pr_url=null`, `gh pr list` → «No Pull Requests»). Диф ветки: 19 файлов, +222/−170 (completion, edit-window-modal, pause-sheet, topic-change-modal, structured-message-form, create-conversation, sync-conflict-detail, topic-backlog-add). Прервался посреди `sync-conflicts`.
- `failure_report.json`: `loop: infra`, `limit_exhausted: "agent node 'implementation': no provider could complete it (process_crashed)"`, но **`final_diff: ""`** — пусто, хотя реальный частичный диф закоммичен на ветке.
- Леджер по B: две записи — `failed` (реальное падение, есть `failure_report`) и через ~63 с ещё `failed` с `validation_reason: duplicate_task_id` (пере-скан на тике 3).

---

## Находки по убыванию влияния

### 1. `error_max_turns` ошибочно классифицируется как `process_crashed` — ПОДТВЕРЖДЕНО

- **Категория:** infra / провайдер. **Серьёзность:** высокая. **Уверенность:** высокая.
- **Доказательство:** `stages/implementation/run-000047/1-claude/events.jsonl` (терминальное событие `subtype:"error_max_turns", is_error:true, num_turns:51`) против `result.json` (`error_class: "process_crashed", message: "the provider process exited abnormally"`) и `failure_report.json` (`no provider could complete it (process_crashed)`). `stderr.log` = 0 байт.
- **Корневая причина:** claude CLI при `error_max_turns` выходит с кодом `1`, а причину пишет в JSON-поток (не в stderr). В адаптере [providers/\_adapter_base.py:336-345](../../src/wastech_orchestrator/providers/_adapter_base.py#L336-L345) ветка «`exit_code != 0` → инфра-сбой → `classify(exit_code, stderr…)`» срабатывает **раньше**, чем разбирается поток событий (строка 348). Пустой stderr + код 1 не дают совпадения сигнатур → дефолт `PROCESS_CRASHED` ([providers/errors.py:31](../../src/wastech_orchestrator/providers/errors.py#L31), enum [providers/base.py:33](../../src/wastech_orchestrator/providers/base.py#L33)). Терминальное событие с `subtype:error_max_turns`, лежащее в stdout, **не читается вовсе**. (Если бы поток разобрался, ветка `succeeded=False` дала бы хотя бы `TASK_FAILURE`, см. [\_adapter_base.py:367-373](../../src/wastech_orchestrator/providers/_adapter_base.py#L367-L373) — но до неё не доходит.) Парсер claude к тому же схлопывает любой не-success-подтип в один булев `succeeded` ([providers/claude.py:318-322](../../src/wastech_orchestrator/providers/claude.py#L318-L322)), теряя различие подтипов.
- **Рекомендуемая правка (рычаг):** в `_adapter_base.py` при `exit_code != 0` сперва пытаться разобрать терминальное событие потока и, если оно есть, классифицировать по его `subtype`/`is_error`, а к `classify()` по коду+stderr откатываться только когда терминального события нет (истинная авария/таймаут/launch-error). Подтип `error_max_turns` завести как отдельный, **нефатальный** и потенциально продолжаемый исход (бюджет ходов исчерпан — не «краш»). В `claude.py:318-322` сохранять `subtype`, а не только `succeeded`.
- **Зона:** дефолт оркестратора (на все репозитории). **Эффект:** правдивая диагностика; перестаёт выглядеть как случайная инфра-авария; открывает дорогу к корректной обработке исчерпания ходов (находка 2).

### 2. Нет щадящей обработки исчерпания бюджета ходов на широкой задаче — ПОДТВЕРЖДЕНО

- **Категория:** flow / spec / config. **Серьёзность:** высокая. **Уверенность:** высокая.
- **Доказательство:** B — широкая задача («аудит и рефактор ВСЕХ form-controls, `ion-select`/`ion-input`/`ion-textarea`/`ion-checkbox`/`ion-toggle`/`ion-button` по всему приложению»). `decomposition.enabled=false` (config). Единственный узел `implementation` с `--max-turns 50` (argv в `request.json`), упёрся в 51-й ход на 19+ файлах. `node_runs`: `implementation` — один failed-ран, ретрая нет, хотя `agents.max_stage_attempts=3`.
- **Корневая причина:** масштаб задачи не бьётся с одношаговым `implementation` при `max_turns=50`, а декомпозиция выключена. Ретрай того же провайдера тем же бюджетом ничего бы не дал (упёрся бы снова), а продолжение сессии (`editing_lineage`) на исчерпании ходов кодом не предусмотрено — `error_max_turns` ведёт прямо в терминальный `failed`.
- **Рекомендуемая правка (рычаг):** (а) на `error_max_turns` — авто-продолжение в той же `editing_lineage`-сессии новым окном ходов (узел реально не «упал», работа закоммичена) либо терминал `manual_action_required` с понятной причиной «turn budget exhausted; rerun --continue», а не `failed`; рычаги — `core/flow/*` (раннер узла) + классификация из находки 1. (б) Для широких задач — поднять `agents.providers.claude.max_turns` ([config/loader.py:343](../../src/wastech_orchestrator/config/loader.py#L343), [config/schema.py:124](../../src/wastech_orchestrator/config/schema.py#L124)) в целевом конфиге и/или включить `decomposition.enabled` (или оформить B как operator-decomposed задачу с под-спеками).
- **Зона:** (а) — дефолт оркестратора; (б) — целевой репо / авторство задачи. **Эффект:** широкая задача доводится до конца или честно просит продолжения вместо потери $5+ и нулевого PR.

### 3. «Грязное» базовое дерево после падения B + пере-скан `duplicate_task_id` — ПОДТВЕРЖДЕНО

- **Категория:** infra / git. **Серьёзность:** средняя. **Уверенность:** высокая.
- **Доказательство:** после прогона на `master` висит `D tasks/pending/redesign-form-controls-to-match-demo.md` (`git status --porcelain`), при этом файл трекается в `master` (`git ls-files tasks/`), а на диске его нет (в `tasks/failed/` тоже — папки нет). В леджере — вторая запись B с `validation_reason: duplicate_task_id` (тик через ~63 с после падения). При этом прогон залогировал `cleanup_safe=true`.
- **Корневая причина:** файлы задач A и B изначально закоммичены в `master` (как и в прошлых прогонах). На падении `_relocate_task_file` ([orchestrator.py:1834-1866](../../src/wastech_orchestrator/core/orchestrator.py#L1834-L1866)) перемещает трекаемый файл `pending→failed` обычным `src.replace(dest)`, а code-commit намеренно исключает `tasks/` из pathspec ([git_manager.py:483-490](../../src/wastech_orchestrator/git_manager.py#L483-L490)). На failed-пути перемещение не фиксируется в базовой ветке устойчиво → после возврата на `master` остаётся повисшее удаление `D`; на следующем тике файл (восстановленный checkout-ом в `pending`) пере-сканируется и отклоняется как дубликат. Это рецидив находки про «грязное дерево» из разборов task-023 (нахождение 4) и ion-list (нахождение 6): прежний фикс закрыл успешный (`done`) путь, **failed-путь остался непокрыт**. Также `cleanup_safe=true` расходится с фактически грязным деревом.
- **Рекомендуемая правка (рычаг):** распространить устойчивое стейджинг-перемещение файла задачи (как на `done`-пути) на failed-путь — в audit-коммите по ветке агента фиксировать и удаление из исходной папки (`git add -A -- tasks/<state>/<id>*` либо явный `git rm`), а терминальная очистка после возврата на базовую ветку должна гарантировать чистоту дерева (`git checkout -- tasks/…` для затронутых путей) и приводить `cleanup_safe` в соответствие с реальностью. Рычаги: [orchestrator.py:1834](../../src/wastech_orchestrator/core/orchestrator.py#L1834) (relocate) + audit-коммит/terminal-cleanup в `git_manager.py`. Процессно: не коммитить файлы `tasks/` в базовую ветку вручную.
- **Зона:** дефолт оркестратора. **Эффект:** после прогонов (в т.ч. упавших) `master` остаётся чистым; нет ложного `duplicate_task_id`-шума.

### 4. HITL-пауза невидима в операторском логе — ПОДТВЕРЖДЕНО

- **Категория:** HITL / наблюдаемость. **Серьёзность:** средняя. **Уверенность:** высокая.
- **Доказательство:** в `watch-run.log` между `planning #1 ... status=succeeded` (18:49:01) и `planning ... route resolved` #2 (18:57:12) — **ни одной строки** ~8 минут. Факт вопроса/ответа виден только в `logs/redesign-…/hitl/planning.json`, не в логе.
- **Корневая причина:** постановка вопроса в Telegram и ожидание ответа (`ask_timeout_s=28800` = 8 ч) не эмитят операторских лог-строк уровня info («awaiting human input», message_id, дедлайн). Для автономного демона это означает: при включённом HITL прогон может «молча» простаивать до 8 часов, и со стороны это неотличимо от зависшего узла.
- **Рекомендуемая правка (рычаг):** логировать постановку и снятие HITL-запроса (узел, kind, message_id, дедлайн, факт ответа) на уровне info; путь HITL — `core/hitl.py` + место вызова `ask_timeout_s` ([orchestrator.py:1182](../../src/wastech_orchestrator/core/orchestrator.py#L1182)). Опционально — таймаут HITL по умолчанию короче 8 ч для watch-режима.
- **Зона:** дефолт оркестратора. **Эффект:** видно, что демон ждёт человека, а не завис; автономность становится наблюдаемой.

### 5. `failure_report.final_diff` пуст при наличии закоммиченной частичной работы — ПОДТВЕРЖДЕНО

- **Категория:** infra / отчётность. **Серьёзность:** низкая-средняя. **Уверенность:** высокая.
- **Доказательство:** `failure_report.json` → `final_diff: ""`, тогда как на ветке `worc/redesign-…` лежит реальный частичный диф (19 файлов, +222/−170) в коммите `7d98539`.
- **Корневая причина:** сборщик `failure_report` берёт диф из (уже очищенного / не того) источника, а не `base..HEAD` ветки агента — та же природа, что у «пустого summary» в разборе task-023.
- **Рекомендуемая правка (рычаг):** считать `final_diff` как `base..HEAD` ветки агента (или из зафиксированного коммита) — сборщик failure-report в `core/orchestrator.py` (путь `_fail`/finalize, [orchestrator.py:1702-1714](../../src/wastech_orchestrator/core/orchestrator.py#L1702-L1714)).
- **Зона:** дефолт оркестратора. **Эффект:** в отчёте о падении видно, что успел сделать агент — облегчает решение про `rerun --continue`.

### 6. Спавненные агенты наследуют глобальные хуки Claude Code оператора (RTK) — ПОДТВЕРЖДЕНО

- **Категория:** infra / изоляция. **Серьёзность:** низкая. **Уверенность:** средняя.
- **Доказательство:** в `summary.md` задачи A (caveat 2): «the rtk proxy mis-rewrote `npm run lint` into a bare `eslint` call, so the runner substituted `ng lint`/`ng build` directly». То есть внутри сессии агента команда `npm run lint` была переписана RTK-хуком.
- **Корневая причина:** агент — это `claude` CLI, а `HOME` в `security.allowed_environment` (нужен для OAuth-логина, см. отдельный фикс), и спавненный claude читает глобальный `~/.claude/settings.json` оператора с RTK-хуками → они переписывают команды агента. Авторитетные проверки оркестратора (узел `testing`) запускаются прямым subprocess-argv и **не** затронуты (5/5 прошли) — задеты только самопроверки агента.
- **Рекомендуемая правка (рычаг):** для спавненных агентов изолировать конфиг Claude Code (например, задавать узлам собственный `CLAUDE_CONFIG_DIR`, отличный от оператора, или явно отключать наследование хуков), не теряя авторизацию. Рычаг — построение окружения провайдера в `providers/_adapter_base.py` / политика env в `security`.
- **Зона:** дефолт оркестратора. **Эффект:** среда исполнения агента детерминирована и не зависит от личных хуков оператора.

---

## Отдельный блок: три механизма

### Механизм (1) — watch (демон + fetch/ff-only pull базы) — ПОДТВЕРЖДЁН

- Демон сам подхватил `tasks/pending` без `worc run`: тик 1 взял A, тик 2 — B. Логика: `watch_loop` ([cli.py:723-762](../../src/wastech_orchestrator/cli.py#L723-L762)) каждый тик зовёт `refresh_repo` → `git.refresh_base` ([git_manager.py:356-369](../../src/wastech_orchestrator/git_manager.py#L356-L369)): no-op, если HEAD не на base; иначе `fetch origin` + `pull --ff-only`.
- Доказательство работы pull между тиками: на тике 2 локальный `master` уехал с `131167f` на `a34f67f` (merge-коммит A), и ветка B построена поверх него (`git merge-base HEAD a34f67f` = `a34f67f`).
- Слот не простаивал зря: на тике 1 пока A занимала слот, B корректно пропускалась (non-blocking), а не блокировала очередь. Холостые тики (после падения B) делали только fetch/pull — это ожидаемо.
- **Тонко:** WAITING-скип в этом прогоне случился ровно **один раз** (A заняла весь тик 1, и B взялась уже на тике 2 как ELIGIBLE) — это структурно правильно, но «доказательная масса» по скипам тонкая (один тик).

### Механизм (2) — depends_on (merge-gated, non-blocking) — ПОДТВЕРЖДЁН

- Инвариант сверен в исходниках до запуска: `_dependency_merged` ([orchestrator.py:585-597](../../src/wastech_orchestrator/core/orchestrator.py#L585-L597)) — зависимость `DONE` считается выполненной только если её PR `MERGED` (иначе WAITING). Скип non-blocking: `watch_once` ([cli.py:703-713](../../src/wastech_orchestrator/cli.py#L703-L713)) на WAITING делает `_LOG.info(... waiting ...)` + `continue`.
- Эволюция причины WAITING как доказательство гейта: на тике 1 — `"is pending (not yet run)"` (ветка `dep in pending`, [orchestrator.py:573-574](../../src/wastech_orchestrator/core/orchestrator.py#L573-L574)); если бы A не смержилась, дальше было бы `"PR is OPEN (unmerged)"`.
- **Доказательство порядка (ключевое):** A смержена `2026-06-24T18:38:49Z`; первый node_run / `preparing` B — `18:39:54Z` (≈65 с позже, ровно следующий poll-тик). То есть B **физически не стартовала**, пока PR A не стал `MERGED`. Ложного `BROKEN`/цикла не было (`_in_cycle` корректно вернул false), терминальных ошибок гейта нет.

### Механизм (3) — auto_merge (squash, немедленный) — ПОДТВЕРЖДЁН

- A дошла до `publish` и оркестратор сам смержил PR #3: лог `"[AUTO-MERGE] merging PR without human review" strategy=squash wait_for_checks=false`, `auto-merge completed duration=5.08s`. Код: `_auto_merge_on` ([orchestrator.py:2010-2021](../../src/wastech_orchestrator/core/orchestrator.py#L2010)) → `_auto_merge` ([orchestrator.py:1622-1653](../../src/wastech_orchestrator/core/orchestrator.py#L1622)) → `git.merge_pr` ([git_manager.py:679-718](../../src/wastech_orchestrator/git_manager.py#L679)).
- Немедленный merge, не «armed»: `wait_for_checks=false` → argv `gh pr merge <url> --squash` без `--auto` и **без `--admin`** (защита ветки уважается, [git_manager.py:704](../../src/wastech_orchestrator/git_manager.py#L704)). SHA записан: `publish_operations.pr_merge = completed, result_ref = a34f67f`; на GitHub PR #3 `state=MERGED, mergeCommit.oid=a34f67f`. Леджер A: `auto_merged=true, merge_outcome=a34f67f`.
- Связка merge → разблокировка B: после merge A на тике 2 `prepare_branch` ([git_manager.py:240-255](../../src/wastech_orchestrator/git_manager.py#L240-L255)) сделал `fetch → checkout master → pull --ff-only → checkout -b`, и ветка B выросла прямо из `a34f67f`. Полная цепочка «auto_merge зависимости → pull обновлённой базы → запуск зависимой на ней» отработала.
- **Тонко:** собственный PR B мы не увидели смерженным — B упала до `publish`, так что второго auto-merge в прогоне не было. Но механизм (3) в постановке теста — это «оркестратор мержит **зависимость**, чтобы разблокировать зависимую», и он подтверждён через A→B полностью.

---

## Пробелы в данных

- Денежная стоимость codex-`review` для A в `events.jsonl` в том же формате `total_cost_usd` не отражена (codex считает иначе) — оценка стоимости A по claude-узлам + supervisor; порядок ($3–4) корректен.
- Точная семантика «почему `max_stage_attempts=3` не сделал ретрай `implementation`» по артефактам видна как факт (1 attempt, нет fallback-провайдера у узла), но точка решения о (не)ретрае `process_crashed` в раннере узла глубоко не вскрывалась — рекомендация в находке 2 не зависит от этого.
- `current.diff` для B отсутствует (задача упала до publish, где он пишется) — частичную работу оценивал по дифу ветки `base..HEAD`.
- `prompt-audit` присутствует (`timeline.jsonl`), пробела по аудиту промптов нет.

## Что уже хорошо

- **Все три механизма автономности работают** — это главный позитив прогона: демон сам провёл, смержил и разблокировал по зависимости без участия человека в самой оркестрации.
- A — чистый эталонный прогон: 0 циклов фиксов, диф строго в скоупе, **содержательный summary** (`supervisor_final.summary_written=true`) — фикс «пустого summary» из task-023 держится.
- Проверки выбрались **только по дифу** (`mobile`), бэкенд-`.NET`-спираль из task-023 не повторилась (P0-фикс по выбору наборов + ужесточённая роль `fixing` на месте: в `roles/fixing.md` уже есть запрет «чинить» несовместимый тулчейн и лезть вне скоупа).
- `review` на codex/gpt-5.5 отработал штатно без fallback — фиксы из 0.5.4a1 (codex reasoning-флаг, сброс модели при cross-provider fallback) держатся.
- **HITL сработал по делу:** агент задал точный вопрос про скоуп (auth/dev-sandbox), оператор ответил, агент исключение соблюл — это сильная сторона, а не дефект.
- Auto-merge безопасен: `squash`, без `--admin`, защита ветки уважается, SHA фиксируется, идемпотентно через publish-op.
- Частичная работа упавшей B **не потеряна** — закоммичена и запушена на ветку, доступна для `rerun --continue`.
- Дерево после **успешной** A осталось консистентным (A в `tasks/done` через merge), грязь возникла только на failed-пути B.

---

## План исправлений

Приоритеты: P0 — наибольший эффект, P1 — важно, P2 — приятно иметь. Для каждого пункта — рычаг (что и где) и зона (целевой репо / дефолт оркестратора).

### P0. Правдивая классификация терминальных исходов провайдера (не «всё подряд = process_crashed»)

**Причина:** находка 1. **Рычаг:** [providers/\_adapter_base.py:336-345](../../src/wastech_orchestrator/providers/_adapter_base.py#L336-L345) — при `exit_code != 0` сначала пытаться распарсить терминальное событие потока и классифицировать по его `subtype`/`is_error`; к `classify()` по коду+stderr откатываться только при отсутствии терминального события. Завести `error_max_turns` отдельным нефатальным подтипом; в [claude.py:318-322](../../src/wastech_orchestrator/providers/claude.py#L318-L322) сохранять `subtype`. **Зона:** дефолт оркестратора. **Эффект:** диагностика перестаёт врать; разблокирует P1 ниже.

### P1. Щадящая обработка исчерпания бюджета ходов

**Причина:** находка 2. **Рычаг:** (а) на `error_max_turns` — продолжение в той же `editing_lineage`-сессии новым окном ходов либо терминал `manual_action_required` с причиной «turn budget exhausted; rerun --continue» вместо `failed` (раннер узла в `core/flow/*`); (б) целевой конфиг — поднять `agents.providers.claude.max_turns` ([config/schema.py:124](../../src/wastech_orchestrator/config/schema.py#L124)) и/или `decomposition.enabled: true`, либо оформить B как operator-decomposed. **Зона:** (а) дефолт; (б) целевой репо. **Эффект:** широкие задачи доходят до конца или честно просят продолжить.

### P1. Чистое базовое дерево после падений (файл задачи)

**Причина:** находка 3. **Рычаг:** распространить устойчивую фиксацию перемещения файла задачи на failed-путь — `_relocate_task_file` ([orchestrator.py:1834](../../src/wastech_orchestrator/core/orchestrator.py#L1834)) + audit-коммит/terminal-cleanup в `git_manager.py` (стейджить и удаление из исходной папки; после возврата на базу гарантировать чистоту дерева и корректный `cleanup_safe`). **Зона:** дефолт оркестратора. **Эффект:** `master` чист после любых прогонов; нет ложного `duplicate_task_id`.

### P2. Наблюдаемость HITL и прочее

1. Логировать постановку/снятие HITL-запроса (узел, kind, message_id, дедлайн, ответ) на info; опционально — более короткий HITL-таймаут для watch. Рычаг: `core/hitl.py`, [orchestrator.py:1182](../../src/wastech_orchestrator/core/orchestrator.py#L1182). Зона: дефолт. (находка 4)
2. `failure_report.final_diff` считать как `base..HEAD` ветки агента. Рычаг: сборщик failure-report в `_fail`/finalize ([orchestrator.py:1702-1714](../../src/wastech_orchestrator/core/orchestrator.py#L1702-L1714)). Зона: дефолт. (находка 5)
3. Изолировать конфиг Claude Code спавненных агентов от глобальных хуков оператора (отдельный `CLAUDE_CONFIG_DIR`). Рычаг: построение env провайдера/`security`. Зона: дефолт. (находка 6)

---

## Сводная таблица «наблюдение → причина → рычаг → зона»

| Наблюдение | Причина | Рычаг | Зона |
| --- | --- | --- | --- |
| 1. `error_max_turns` показан как `process_crashed` | Ветка «exit≠0=инфра-сбой» срабатывает до разбора потока; пустой stderr → дефолт PROCESS_CRASHED | `_adapter_base.py:336-345`, `claude.py:318-322` | дефолт |
| 2. B упала, исчерпав 50 ходов; нет щадящей обработки | Широкая задача + один `implementation` + `max_turns=50` + декомпозиция off | раннер `core/flow/*`; `config` (`max_turns`/`decomposition`) | дефолт + целевой |
| 3. Грязное дерево `D` + `duplicate_task_id` после B | Перемещение трекаемого файла задачи на failed-пути не фиксируется в базе | `orchestrator.py:1834` + audit/cleanup в `git_manager.py` | дефолт |
| 4. HITL-пауза невидима в логе | Постановка/ожидание HITL не эмитят info-строк | `core/hitl.py`, `orchestrator.py:1182` | дефолт |
| 5. `failure_report.final_diff` пуст | Диф берётся не из `base..HEAD` ветки | сборщик failure-report (`_fail`/finalize) | дефолт |
| 6. RTK переписал команды агента | Агент наследует `~/.claude` оператора через `HOME` | env провайдера / `CLAUDE_CONFIG_DIR` | дефолт |

---

## Следующие шаги

1. **Применить главный рычаг + тест:** реализовать P0 (правдивая классификация `error_max_turns`) в `_adapter_base.py`/`claude.py` и накрыть тестом провайдера (fake-CLI сценарий с терминальным `error_max_turns` + exit 1 → ожидать НЕ `process_crashed`). Затем P1 (продолжение/мягкий терминал по исчерпанию ходов).
2. **Занести находки в трекер:** добавить пункты P0/P1/P2 в [docs/backlog/follow_ups.md](../backlog/follow_ups.md).
3. **Восстановить целевой репозиторий** (см. блок ниже) — вернуть конфиг и почистить рабочее дерево; по желанию `rerun --continue redesign-form-controls-to-match-demo`, чтобы довести B до PR (частичная работа уже на ветке) — это заодно проверит P1 после фикса.

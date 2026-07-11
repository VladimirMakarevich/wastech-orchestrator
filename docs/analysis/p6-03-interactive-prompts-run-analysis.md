# Разбор прогона: p6-03-interactive-prompts (фаза 6, проход 23)

## STATUS: DONE ✅ (первый оператор-driven HITL за кампанию; PR #12 переиспользован)

- **task_id:** `p6-03-interactive-prompts`
- **final_status:** `done` (worc status + ledger)
- **finished_at:** 2026-07-09T20:43:25Z (~2ч12м — самый долгий в фазе; из них ~9.5 мин — ожидание HITL-ответа)
- **PR:** [#12](https://github.com/VladimirMakarevich/wastech-mdlint/pull/12) — переиспользован (+1 коммит, ветка `feat/p6-init`)
- **fix_iterations:** 5 (review сошёлся на 6-м проходе)
- **branch_mode:** `existing` + `branch_ref: feat/p6-init` (поверх p6-02)

## Короткий вывод

Прогон **успешный**, и его главное событие — **первый за всю кампанию (P4→P6) оператор-driven HITL-гейт**, причём образцовый: planning-агент поймал реальное противоречие в спеке (промпт `language` перечислен в задаче, но нигде не определён), задал уточняющий вопрос через telegram, оператор ответил (выкинуть `language`, делать 3 промпта), planning **возобновил ту же сессию** и построил корректную версию. Всё остальное — как в фазе: 5 прогрессивных review-циклов, checks 24/24, 0 фоллбэков, supervisor на claude дёшев (подтверждение F47/F46).

**Единственного «главного рычага» нет — багов прогон не выявил.** Ценность: (1) валидирован HITL round-trip с session-resume и telegram-доставкой; (2) в третий раз подтверждён F47 (supervisor на claude дёшев). Наблюдение target-стороны: задача p6-03 содержала неопределённый промпт `language` — качество спеки задачи, не оркестратора; HITL это корректно вскрыл.

## Как прошёл прогон (фактический путь)

```
planning#1(claude, 807s) → HITL question ("language" undefined?) → [ожидание ~9.5 мин] → оператор ответил (telegram msg 250)
 → planning#2(claude, 75s, ВОЗОБНОВЛЕНА сессия 7cf6f793c227) → supervisor
 → implementation(claude, 1075s)
 → [ testing(4/4) → review(codex/high) = REWORK → fixing(claude sonnet-5) ] × 5
 → testing(4/4) → review(codex) = ACCEPT (findings=[])
 → documentation(claude) → finalize(supervisor claude) → publish(#12 reuse) → done
```

Провайдеры: planning/implementation/fixing/documentation — claude; review — codex/gpt-5.4/high; **supervisor — claude/opus-4-8/medium** (новый baseline с p6-02). Все узлы 1 попытка, **0 фоллбэков/ретраев/крашей** (`node_runs.error_class` пуст).

**Counters:** `fix_iterations=5`, `review_fix_total=5`, `review_fix_cycles=0` (обнулён при сходе), `test_fix_total=0`. Checks **24/24 passed**, 0 timeouts. Evaluations: in_flow_verdict=6 (5 rework + accept id 91 `[]`), supervisor_step=20, supervisor_final=1, memory_write=6.

**Токены (input):** review 6 406 417 (6, codex — доминирует) · fixing 25 120 (5) · implementation 6 252 (1) · **supervisor 5 460 (21, claude — дёшев)** · planning 5 398 (2) · documentation 5 261 (1).

## Находки по убыванию влияния

### Наблюдение №1 (положительное, milestone) — HITL round-trip + session-resume работают end-to-end

**Доказательство.** `logs/.../hitl/planning.json`: `kind=question`, `risk=clarification`, `telegram_message_id=250`, `status=consumed`. Вопрос (сокр.): «task-файл перечисляет промпт `language`, но он нигде не определён (нет в glossary, в тест-фикстурах P6.05, в шагах скилла P8.02), не мапится на config-ключ. Что делать? (a) мёртвый текст, выкинуть [дефолт агента]; (b) `settings.siteRouter.defaultLocale`; (c) иное». Ответ оператора: «Option (a) … три промпта: include patterns, rule categories, confirmation … без поля siteRouter». Лог: `20:45:24 awaiting human input` → heartbeat'ы 60s → `20:54:55 status=answered` (не timeout). Session-resume: planning#1 `result.session_id=session:7cf6f793c227`, planning#2 (после ответа) **тот же** `session:7cf6f793c227`, длительность 75s vs 807s → диалог продолжен, не переплан. Оба claude (провайдер-гейт консистентен).

**Значение.** Первый **оператор-driven** HITL за кампанию (раньше — только auto-resolve `kind=approval` в autonomous, прогон 5). Подтверждает §4 (session resume на HITL re-entry, `result.session_id` совпал), §14 (telegram-доставка вопроса и приём ответа), 8h-timeout с 60s-heartbeat. Качество: агент не стал молча строить неопределённую фичу — уточнил (planning autonomy работает по назначению).

**Рычаг.** Не требуется — работает как задумано. (Побочно, target-сторона: удалить неопределённый промпт `language` из формулировок p6-03/roadmap, раз он подтверждённо мёртвый; это правка спеки задачи, не оркестратора.)

### F47/F46 (третье подтверждение) — supervisor на claude дёшев

**Доказательство.** supervisor 21 вызов = **5 460** input (claude/medium), против 38 753 207 на p6-01 (codex/xhigh). Стабильно с p6-02. Подтверждает: смена supervisor на claude-дефолт держит стоимость advisory-слоя близко к нулю; codex-resume ре-ингест (F47) — реальный фактор только на codex.

### F42 (рецидив) — глубина блокирующего review

5 rework-циклов (p6-01=6, p6-02=3). Findings прогрессивные, горячая область — фидлистая логика existing-config overwrite/merge/skip и prompter (`init-prompter.ts`/`init-command.ts`): #1 confirmDraft/resolveExistingConfig/readExistingRuleIds → #2 formatDraftSummary → #3 isTty/rulesField/select → #4 findConfig → #5 resolveExistingConfigAction/choosePackageManager → accept. Явного «двигания ворот» (F43) нет; апстрим-рычаг прежний (edge-hardening в implementation).

### F43 (НЕ воспроизвёлся, 2-й прогон подряд) — thrash остаётся эпизодом p6-01

Findings разные каждый цикл, без противоречий/откатов. Подтверждает: F43 — эпизодический, не систематический.

## Пробелы в данных

- В `request.json` planning#2 поля `resume_session_id`/`session_id` не выставлены на верхнем уровне (resume для claude передаётся иначе); факт resume подтверждён совпадением `result.session_id` — прямое доказательство исхода, но не самого argv.
- Долларовая стоимость не считалась (смешанные вендоры; цены не гадаю).

## Что уже хорошо

- **HITL образцов:** реальное противоречие спеки → уточняющий вопрос → ответ оператора → session-resume → корректная сборка (3 промпта, без `language`).
- **Supervisor на claude дёшев** (5 460 input), advisory-функция сохранена — лечение F46/F47 держится.
- **0 инфраструктурных сбоев**, checks 24/24, память 6 записей, finalize-summary написан.
- **PR reuse (F27)** третий коммит в #12; **branch_mode existing** построил поверх p6-02.
- **Скоуп diff чистый:** 12 файлов, +1503 — prompter + init-command + program + `@inquirer/prompts` (**declared** в задаче → не «undeclared dep») + e2e-тест + docs; фича собрана ровно по ответу оператора.

## План исправлений

**P1/P2 (наследуется, не про этот прогон):** F44 (preflight-регрессия content-флоу, P0), F47 (investigation codex-resume; supervisor держать на claude — уже так), F42/F43 (edge-hardening + locked-decision guardrail) — статус OPEN, к синтезу в конце фазы.

**Новых оркестраторных P0/P1 из p6-03 нет.** Target-сторона (не наша дорожка): вычистить неопределённый промпт `language` из спеки P6.03.

## Сводная таблица

| Наблюдение | Причина | Рычаг | Зона |
| --- | --- | --- | --- |
| planning выполнился дважды | HITL `kind=question` → ответ оператора → session-resume (тот же sid) | — (штатный HITL round-trip) | orchestrator (положительно) |
| промпт `language` не определён | спека задачи перечисляет неопределённый термин | убрать из p6-03/roadmap | target-spec |
| supervisor 5 460 input (дёшев) | claude-resume дельтой (vs codex replay) | `supervisor.provider: claude` (уже) | orchestrator (F47/F46) |
| 5 review-циклов | фидлистый existing-config merge; edge-hardening не фронт-лоаден | `packaged/flows/implementation/implementation.md` | orchestrator (F42) |
| thrash не повторился | F43 эпизодичен | — | orchestrator (F43) |

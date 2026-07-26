# Supervisor P0: детерминированный SupervisorPacket → fresh finalize → пропуск tool/checks

**Статус:** proposal **Приоритет:** P0 (самый крупный и самый безопасный резерв экономии в content-pipeline) **Источник:** [2026-07-16 варианты оптимизации supervisor](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-16-supervisor-token-optimization-options.md) (§7 SupervisorPacket, §8 P0, Варианты A/E/F). Смежные задачи: [normalized-usage-accounting.md](normalized-usage-accounting.md) (мерная подложка для A/B), [content-flow-token-hygiene.md](content-flow-token-hygiene.md).

**Дорожная карта:** **P0 (этот документ)** → [P1 — управляемый cadence](supervisor-observation-cadence-p1.md) → [P2 — разделение обязанностей и telemetry](supervisor-responsibility-split-p2.md).

## Проблема

Supervisor — самый тяжёлый потребитель Claude-контекста. На исследованном прогоне `blog-review-happy-in-my-misfortunes-4` его семь вызовов потратили 480 293 input-токенов и $0.77 — это 70% всего Claude-input задачи. Шесть из семи вызовов — промежуточные наблюдения, а supervisor по контракту advisory-only: он не читается движком для роутинга, у него нет `route`/`rework` (`core/supervisor.py`, подтверждено в анализе).

Главная тонкость, из-за которой «просто урезать наблюдения» — это баг, а не оптимизация: **finalize сегодня не получает детерминированный пакет фактов.** Он опирается на тёплую сессию, которая по ходу прогона читала diff и копила наблюдения, а на revive — на digest этих наблюдений (`_finalize_digest`, `core/supervisor.py:638`). Встроенный finalize-промпт прямо просит синтез «grounded in the actual committed change» и «caveats you noted across the steps» (`_BUILTIN_FINALIZE`, `core/supervisor.py:160-164`). Если перевести finalize на fresh-сессию (Вариант E) или убрать наблюдения (Вариант F / `observation_mode: none`) **до** появления пакета, то на чистом прогоне digest пуст, и finalize остаётся с одним `role_file + task_title` — он либо идёт заново читать репозиторий (медленно, съедает всю экономию), либо пишет тонкий summary. Заодно деградируют `follow_ups` и `memory_delta`, ценность которых именно в том, что supervisor наблюдал прогон.

Отсюда обязательный порядок внедрения, который и фиксирует эта задача.

## Требуемый результат

Единый детерминированный `SupervisorPacket`, собираемый из уже существующих durable-источников, который делает finalize независимым от живой тёплой сессии. Только после этого finalize переводится на fresh-сессию по умолчанию, а наблюдения детерминированных нод (`tool`, `checks`) отключаются. Итог: экономия input-токенов без потери полноты summary; normal-путь и revive-путь finalize становятся одинаковыми и воспроизводимыми.

## Решения (обязательный порядок)

**Порядок P0.1 → P0.2 → P0.3 нельзя нарушать. Нельзя менять cadence раньше, чем появится пакет.**

- **P0.1 — сначала пакет.** `finalize` всегда собирает `SupervisorPacket` из уже имеющихся фактов (`node_runs`, `evaluations`, `current.diff`, findings, checks) + компактный digest материальных наблюдений (`_finalize_digest`). Новых данных собирать не нужно — всё уже лежит на диске и в `state.db`.
- **P0.2 — затем fresh finalize по умолчанию.** Убираем ветку «тёплый resume vs digest»: и normal, и revive идут одним путём — fresh-сессия (`resume_session=False`), засеянная пакетом. Механизм fresh-из-digest уже существует как recovery (`_finalize_digest` + `resume=False`), делаем его основным.
- **P0.3 — только теперь пропуск наблюдений `tool`/`checks`.** Правка одного условия в post-node hook (`core/orchestrator.py:2902`, текущее условие `node.kind != "publish"`): `node.kind not in {"tool", "checks", "publish"}`. Безопасно: пропущенные/недетерминированные ноды и так не наблюдаются, а на стейт-машину cadence не влияет (advisory). **Замечание по порядку (актуализация 2026-07-23):** сам по себе пропуск `tool`/`checks` НЕ обесточивает finalize даже до пакета — материальные наблюдения (`revise`/`tone_style`/`polish`) продолжают идти, а тёплая finalize-сессия видит их контекст. Поэтому P0.3 можно выделить в отдельный маленький PR и слить раньше пакета, банкуя ~44k (`length`) немедленно; строгий порядок «сначала пакет» обязателен только для `observation_mode: none`/`events` из P1, которые убирают уже _все_ наблюдения.

Дополнительно:

- **Наблюдения (кроме `tool`/`checks`) в P0 продолжают идти как сейчас.** Это переходное состояние: они по-прежнему кормят `material_observations` в пакете, поэтому качество `follow_ups`/`memory_delta` не проседает до того, как пакет будет проверен на A/B. Понижение cadence (`observation_mode: events | none`) — это P1, не P0.
- **Пакет ссылается на полные артефакты и содержит только bounded-выжимку.** Полный diff встраиваем только если он мал; иначе — changed paths + diff stat + путь к `current.diff` (§7 анализа).
- **Актуализация 2026-07-23 — пакет оформляется как frozen-exchange артефакт по пути, а не инлайн-JSON.** После этого анализа приземлился **WRI-011**: finalize уже читает задачу не инлайном, а из замороженного exchange-пакета через context-footer (`_finalize_prompt`, `core/supervisor.py:1013` — «the task reaches the turn as the frozen exchange packet … never inline title/body»), и `finalize` уже прокидывает `task_path`/`repository_instructions_path` (`core/supervisor.py:505-550`). Значит `SupervisorPacket` надо оформлять тем же паттерном: записывать как замороженный read-only артефакт в exchange-layout (два корня, WRI-001) и передавать finalize по пути (ещё один `*_path` в context-footer), а не встраивать JSON в текст промпта. Это переиспользует уже существующую машинерию и не конфликтует с изоляцией (WRI-001…012), которой на момент исходного анализа ещё не было.

## В объёме P0

1. Тип `SupervisorPacket` и его сборка из durable-состояния: `task {id,title,type}`, `flow {name,final_status}`, `changes {paths, diff_path, diff_stats}`, `steps [{node,outcome,message(bounded)}]`, `checks {passed,failed}`, `findings_path`, `material_observations` (из `_finalize_digest`).
2. `finalize` всегда строит пакет и всегда запускается на fresh-сессии, засеянной пакетом; ветка тёплого resume удаляется. `summary.json` по-прежнему пишется всегда; deterministic-фолбэк оркестратора при неудаче turn сохраняется без изменений.
3. Пропуск `observe` для `node.kind ∈ {tool, checks}` в post-node hook.
4. Тесты (см. раздел ниже).
5. Синхронизация доков, которые физически есть на `dev` (решение X2, 2026-07-26): `src/wastech_orchestrator/packaged/guide/flows/roles.md:63` утверждает, что supervisor «observes **each step** and writes the final summary» — после P0 это неверно, фразу нужно переписать под «наблюдаются исполненные ноды, кроме `tool`/`checks`/`publish`» и под packet-first finalize. Схема config в P0 не меняется, поэтому `packaged/config.example.yaml` и `guide/config/reference.md` не трогаем. Derived `docs/` на `dev` не существует — вместо правки в описании PR оставляем строку doc-impact («затронуты finalize + cadence supervisor; вероятно влияет на `worc_architecture.md` и `configuration.md`») как хлебную крошку для реверс-инжиниринга на `main`.

Ожидаемый эффект на исследованном прогоне: пропуск `length` снимает минимум 44 107 input-токенов; fresh finalize из компактного пакета ориентировочно уменьшает финальный вызов (сейчас 104 567 input-токенов) на 65–85 тыс. Точные числа — по A/B (см. критерии).

## Критерии приёмки

- [ ] `tool`/`checks` нода не порождает supervisor provider-request (нет observe-turn), но задача завершается штатно.
- [ ] Обычный (non-revive) finalize запускается на fresh-сессии: turn не получает warm session id, вход — `SupervisorPacket`.
- [ ] `SupervisorPacket`, собранный на обычном прогоне и после restart/revive, идентичен (детерминизм из durable-состояния) — воспроизводимость summary.
- [ ] `SupervisorPacket` содержит changed paths + diff stats + путь к `current.diff`; полный diff встраивается только при малом размере (bounded).
- [ ] `SupervisorPacket` передаётся finalize как путь к замороженному exchange-артефакту через context-footer (паттерн WRI-011), а не инлайн-JSON в тексте промпта; никакой секрет/сырой diff в промпт не попадает.
- [ ] `follow_ups` (когда flow включил `emit_follow_ups`) и `memory_delta` (когда `memory.enabled`) по-прежнему производятся тем же одним finalize-turn — без доп. LLM-вызовов.
- [ ] Полнота summary не хуже baseline по четырём пунктам (что изменено / почему / какие проверки прошли / какие caveats). A/B на повторном `blog_article_revise`: supervisor input < 60 000 (baseline 480 293), при 0 пропущенных blocking-issue (их держит `tone_style`).
- [ ] Supervisor остаётся read-only и advisory; handoff и skill-proposal работают независимо от изменений.

## Вне объёма P0 (следующие фазы)

- `observation_mode: all | selected | events | none`, event-триггеры, flow-local narrowing, раздельные `observe`/`finalize` model+reasoning, бюджеты `max_calls`/`max_digest_tokens` — **[P1](supervisor-observation-cadence-p1.md)** (Варианты B/C/D/H/I). Для content-flow — `none`, для implementation — `events` (там `emit_follow_ups: true`, `none` просадил бы follow_ups/память).
- Разделение монолита на `StepRecorder`/`ObservationAdvisor`/`TaskFinalizer`/`SubtaskHandoff`/`SkillProposer` и per-function telemetry — **[P2](supervisor-responsibility-split-p2.md)** (§6, §8 P2).
- Полностью deterministic summary без LLM — опционально (Вариант G), не default.
- Изменения handoff/skill-proposer — не входят в P0.

## Тесты под замену/добавление

Текущие тесты в `tests/core/test_supervisor.py` жёстко пинят старый контракт «warm resume finalize» и «observe на каждом completed step» — их нужно переписать под fresh-from-packet и новый cadence.

- `test_supervisor_observes_each_completed_step` (`:129`) — переписать: `tool`/`checks` больше не наблюдаются; агент/эвалюатор — да.
- `test_finalize_warm_session_resumes_without_digest` (`:501`) — **инвертируется**: обычный finalize теперь НЕ резюмится на тёплую сессию, а идёт fresh из пакета.
- `test_finalize_reseeds_from_digest_when_session_not_live` (`:481`) — из revive-only становится обычным путём finalize.
- `test_finalize_digest_skips_failed_and_empty_notes` (`:519`), `test_finalize_digest_none_when_no_usable_observations` (`:531`) — остаются валидны, но digest теперь часть пакета (`material_observations`).
- **Новый:** `SupervisorPacket` детерминирован и идентичен на normal-прогоне и после restart.
- **Новый:** пакет содержит diff stats + путь к `current.diff` и bounded step-messages (не весь diff при большом размере).
- `tests/core/test_flow_engine.py` — post-node lifecycle: `tool`/`checks` не вызывает observe; прочие executed-ноды вызывают.

## Вероятные области реализации

- `src/wastech_orchestrator/core/supervisor.py` — `SupervisorPacket`, всегда-fresh finalize, сборка пакета из `_finalize_digest` + durable-фактов.
- `src/wastech_orchestrator/core/orchestrator.py` — условие пропуска `tool`/`checks` в post-node hook (`~:2902`); прокидывание фактов задачи (changed paths / diff / findings / checks) в `finalize`.
- `tests/core/test_supervisor.py`, `tests/core/test_flow_engine.py` — см. выше.
- `src/wastech_orchestrator/packaged/guide/flows/roles.md` — единственный присутствующий на `dev` doc-файл, который описывает это поведение: cadence-фраза про «each step» (`:63`) и packet-first finalize. Derived `docs/worc_architecture.md` / `docs/configuration.md` на этой ветке отсутствуют — только doc-impact note в PR (X2).

# Разбор прогона: p6-02-rule-inference (фаза 6, проход 22)

## STATUS: DONE ✅ (успешный прогон, чище p6-01; PR #12 переиспользован)

- **task_id:** `p6-02-rule-inference`
- **final_status:** `done` (worc status + ledger)
- **finished_at:** 2026-07-09T17:46:12Z (~65 мин)
- **PR:** [#12](https://github.com/VladimirMakarevich/wastech-mdlint/pull/12) — **переиспользован** (F27: тот же head/base `feat/p6-init`→`main`, +1 коммит p6-02; title/body остались от p6-01)
- **fix_iterations:** 3 (вдвое меньше p6-01; review сошёлся на 4-м проходе)
- **branch_mode:** `existing` + `branch_ref: feat/p6-init` (построено поверх коммита p6-01 `b1f8cad`)

## Короткий вывод

Прогон **успешный и заметно чище p6-01**: модуль `discovery/rule-inference.ts` + экспорт + тесты + docs реализованы в скоуп, checks 16/16 зелёные, review сошёлся за **3 rework-цикла** с **прогрессивными** findings (без «двигания ворот» — **F43 не воспроизвёлся**), 0 фоллбэков/крашей. `depends_on` не задан, но концептуальная зависимость от p6-01 удовлетворена через общую ветку (код скана уже там) — dependency-гейт корректно не сработал.

**Единственный главный рычаг (уже применён и подтверждён):** между p6-01 и p6-02 конфиг supervisor сменён с `codex/gpt-5.4/xhigh` на packaged-дефолт `claude/opus-4-8/medium`. Это **свернуло стоимость supervisor-слоя в ~13 000×** (38 753 207 → 2 922 input-токенов) — потому что **codex при session-resume ре-ингестит весь растущий контекст, а claude резюмит дельтой** (новый provider-урок, кандидат **F47**). Рычаг F46 фактически применён; p6-02 его валидирует.

## Как прошёл прогон (фактический путь)

Флоу `implementation`, `refinement` снова не выполнялся (задача хорошо специфицирована). Провайдеры: primary claude; review codex/gpt-5.4/high; **supervisor теперь claude/opus-4-8/medium** (изменение конфига, см. ниже).

```
planning(claude) → implementation(claude)
 → [ testing(4/4 ✅) → review(codex/high) = REWORK → fixing(claude sonnet-5/xhigh) ] × 3
 → testing(4/4 ✅) → review(codex) = ACCEPT (findings=[])
 → documentation(claude) → finalize(supervisor claude, summary 65.5s) → publish(PR #12 reuse) → done
```

Все узлы 1 попытка, `route_fallback` не срабатывал, `error_class` пуст — **0 фоллбэков/ретраев/крашей**.

**Циклы/counters:** `fix_iterations=3`, `review_fix_total=3`, `review_fix_cycles=0` (обнулён при сходе), `test_fix_total=0`. Checks 16/16 passed, 0 timeouts.

**Evaluations (по `kind`):** in_flow_verdict=4 (3 rework + 1 accept, id 58 `findings_json=[]`), supervisor_step=14, supervisor_final=1 (summary написан), **memory_write=6** (память писала; меньше p6-01's 11 — пересекающаяся область файлов).

**Токены (input по узлам):**

| Узел | input | output | вызовов | Комментарий |
| --- | --: | --: | --: | --- |
| review | 4 035 787 | 59 426 | 4 | codex/high — теперь доминирует |
| planning | 4 297 | 62 703 | 1 | claude |
| implementation | 4 442 | 49 818 | 1 | claude |
| fixing | 3 931 | 52 645 | 3 | claude sonnet-5 |
| **supervisor** | **2 922** | 17 957 | 15 | **claude/medium — было 38.75M на codex/xhigh (p6-01)** |
| documentation | 1 131 | 17 357 | 1 | claude |

## Находки по убыванию влияния

### F47 (MEDIUM, provider-поведение) — codex при session-resume ре-ингестит весь растущий контекст; claude резюмит дельтой · зона orchestrator (provider-adapter / выбор провайдера для resume-тяжёлых ролей)

**Доказательство (реальные артефакты, обе задачи).** supervisor использует одну durable-сессию (resume=True на каждом шаге). Per-call `usage.input_tokens`:

- p6-01 (`stages/supervisor/*/1-**codex**/`, session `cec99ca35f90`): 3 599 990 / 69 051 / 278 744 / 322 503 / 366 849 / 509 479 … — **растёт** с историей; итого 24 вызова = **38 753 207** input.
- p6-02 (`stages/supervisor/*/1-**claude**/`, session `fba9df1ac646`): 3 / 2 777 / 1 / 1 / 1 … — **дельта**; итого 15 вызовов = **2 922** input.

**Корневая причина.** Codex resume пере-отправляет накопленный разговор (input растёт квадратично по числу шагов), тогда как claude resume держит контекст серверно и шлёт только новое. Для **постоянного supervisor-слоя** (вызов на КАЖДОМ шаге, усилен фикс-циклами) это даёт взрывной рост стоимости именно на codex.

**Рычаг.** Для resume-тяжёлых высокочастотных ролей (supervisor per-step; при желании — editing-lineages на длинных фикс-петлях) предпочитать claude (как и packaged-дефолт `supervisor.provider: claude`). Либо исследовать в `providers/codex.py`, можно ли при resume не пере-отправлять весь контекст. Не нарушает provider-абстракцию (это выбор провайдера в конфиге, не CLI-логика в ядре).

**Влияние.** ~13 000× разница в input-токенах supervisor между провайдерами на сопоставимых задачах. Прямо влияет на стоимость любого длинного/петляющего прогона с codex-supervisor.

### F46 (обновление — рычаг применён и ВАЛИДИРОВАН) · зона target-config

**Что изменилось.** Конфиг target `.worc/config.yaml` `supervisor` между p6-01 и p6-02 сменён `codex/gpt-5.4/xhigh` → **`claude/opus-4-8/medium`** (packaged-дефолт). Тест-дорожка этого не делала — внешнее изменение (параллельная работа оператора / реакция на F46).

**Результат.** supervisor-стоимость упала 38.75M → 2 922 input (см. F47 — корневая причина в провайдере, не только в reasoning). Исходная гипотеза F46 («advisory-слой дорог, снизить reasoning») уточнена: доминирующий множитель — **провайдер resume-поведение (codex vs claude)**, а не только уровень reasoning. Лечится сменой supervisor на claude-дефолт. **Статус F46: закрыт сменой конфига на дефолт (root cause вынесен в F47).**

### F43 (НЕ воспроизвёлся на p6-02) · подтверждение, что thrash был инцидентом p6-01

**Доказательство.** review findings p6-02 по циклам РАЗНЫЕ и прогрессивные, без противоречия/отката: #1 `tallyPatterns`/`findSampleCyclePair` → #2 `detectAdrSections`/`PATTERN_GATES["GRP-001"]` → #3 `cluster.include`-обёртка + low glossary → accept. Ни одного «двигания ворот» (в отличие от p6-01 #4↔#5 mdx-fallback). F43 (locked-decision не нерушима) — реальный, но **эпизодический**, не систематический. Рекомендация по рычагу F43 остаётся (профилактика), приоритет — низкий.

### F42 (рецидив, слабее) · глубина блокирующего review

3 rework-цикла (против 6 у p6-01, 7 у p5-04) — все прогрессивные реальные баги в rule-inference (паттерн-детекция, gates, cluster-обёртка). Конвергенция в бюджете. Апстрим-рычаг прежний: проактивное покрытие краёв в роли implementation. Тенденция: глубина петли снижается по мере накопления модулей (p6-02 строит на готовом скане p6-01).

## Пробелы в данных

- Причина смены конфига supervisor не зафиксирована в артефактах прогона (внешнее изменение файла) — установлено сравнением с конфигом на начало сессии. Уточнить у оператора (было ли намеренно).
- Долларовая стоимость не считалась (смешанные вендоры; цены не гадаю). Приведены токены.

## Что уже хорошо

- **Чистая быстрая конвергенция:** 3 прогрессивных цикла, без thrash; review ловит реальные баги в паттерн-детекции.
- **0 инфраструктурных сбоев** за все вызовы; checks 16/16.
- **PR reuse (F27) работает:** p6-02 добавлен в #12 без второго PR; цепочка фазы 6 в одном PR.
- **branch_mode `existing` + `branch_ref`** корректно построил поверх p6-01; концептуальная зависимость через ветку без `depends_on`-гейта.
- **Скоуп diff чистый:** +1064, ровно `rule-inference.ts` + тесты + экспорт + docs; без крипа.
- **finalize + память отработали:** summary.md 6.8K, 6 memory-записей.
- **F46-лечение подтверждено в бою:** claude-supervisor на medium дёшев (F47) при сохранении advisory-функции.

## План исправлений

**P1:** **F47** — задокументировать/закрепить, что supervisor (и resume-тяжёлые роли) на claude дёшевы, а codex-resume ре-ингестит контекст; держать `supervisor.provider: claude` дефолтом (уже так в packaged). Опционально — исследовать codex-resume в `providers/codex.py`.

**P2:** **F43** (профилактика locked-decision guardrail в review/fixing), **F42** (implementation edge-hardening) — как на p6-01; приоритет ниже, т.к. p6-02 сошёлся чисто.

**Наследуется из p6-01 (не про этот прогон):** **F44** (preflight-регрессия content-флоу, P0), **F45** (preflight прячет нарушение, P2) — статус OPEN, ждут конца фазы.

## Сводная таблица

| Наблюдение | Причина | Рычаг | Зона |
| --- | --- | --- | --- |
| supervisor 38.75M (p6-01) → 2 922 (p6-02) | codex resume ре-ингестит контекст; claude — дельта | `supervisor.provider: claude` (дефолт); `providers/codex.py` | orchestrator (F47) |
| supervisor-конфиг сменился между прогонами | внешнее изменение (оператор/parallel) | подтвердить у оператора | target-config (F46 закрыт) |
| 3 rework-цикла, findings прогрессивные | богатый углами домен; implementation не хардит края заранее | `packaged/flows/implementation/implementation.md` | orchestrator (F42) |
| thrash НЕ повторился | F43 эпизодичен | — (профилактика по желанию) | orchestrator (F43) |
| PR #12 reuse, +1 коммит | F27 same head/base | — (штатно) | ожидаемое |

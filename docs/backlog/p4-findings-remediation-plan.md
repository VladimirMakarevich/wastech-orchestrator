# P4 — план исправления открытых находок (F24–F37)

План устранения всех **OPEN**-находок из [TEST-FINDINGS.md](../../TEST-FINDINGS.md), выявленных в свежих проходах 6, 7, 8 и cross-run-синтезе (проход 14). Закрытые находки (F19, F20, F22 — RESOLVED; F23 — RESOLVED-BY-TASK; F21 — RESOLVED с остаточным live-follow-up) в план не входят; F21-follow-up вынесен в аппендикс.

Формат пункта: **Цель · Рычаг (file:line) · Шаги · Тест · Зависимость/порядок**. Порядок секций = рекомендованная последовательность выполнения. Приоритеты определяются связкой «серьёзность + разблокировка других находок + риск».

## Рекомендованный сквозной порядок

1. **A1 (F24)** — HIGH, разблокирует весь ревью-гейт, минимальная правка. Делать первым.
2. **C1 (F37)** — HIGH, пробой изоляции спауна (безопасность).
3. **B1 → B2 → B3 (F29 → F30 → F36)** — снять два «замка» на пустой `long_term/`, затем гигиена путей.
4. **A2 → A3 → A4 (F28 → F32 → F31)** — ревью-конвейер: F28 верифицируется только после A1; F31 осмыслен только после B1/B2.
5. **D1 → D2 → D3 (F25 → F26 → F27)** — зависимости и цепочки задач.
6. **E1 (F35, оркестраторный гейт)** — наблюдаемость control-байтов.
7. **F1 → F2 → F3 (F33 → F34 → F35 target-код)** — дрифт кастомизированных промптов и код в target-репо.

---

## Секция A — Ревью-гейт (evaluator / review)

### A1 · F24 (HIGH · OPEN) — `_FINDINGS_SCHEMA` без `additionalProperties:false` → детерминированный краш codex

**Цель.** Убрать 100%-детерминированный краш codex на любом evaluator-узле (review/verifier/critic/testing_quality) и вернуть в строй кросс-провайдерное ревью.

**Рычаг.** [evaluator.py:57-78](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L57) (`_FINDINGS_SCHEMA`).

**Шаги.**

1. Проставить `"additionalProperties": false` на **обоих** object-уровнях `_FINDINGS_SCHEMA` — верхний object и вложенный `items`-object — по образцу уже соблюдённого паттерна в [hitl.py:35](../../src/wastech_orchestrator/core/hitl.py#L35).
2. После правки убедиться, что итоговый JSON, реально пишущийся в `stages/<node>/run-*/1-codex/output-schema.json`, содержит `additionalProperties:false` на каждом уровне.

**Тест.** Регрессионный юнит-тест, который **рекурсивно** обходит каждую константу-схему в кодовой базе (`_FINDINGS_SCHEMA`, `_HUMAN_INPUT_SCHEMA`, `_SUBTASK_SCHEMA` и результат `typed_output_schema(...)`) и падает, если на любом `type:object`-узле отсутствует `additionalProperties:false`. Smoke-тест ADR F19 использовал упрощённую схему-пример и не поймал этот регресс — новый тест должен валидировать буквально боевые константы.

**Зависимость.** Блокирует A2. Делать первым во всей кампании.

---

### A2 · F28 (MEDIUM · OPEN) — кросс-вендорное ревью не исполнилось ни разу (следствие F24)

**Цель.** Подтвердить, что после A1 codex-ревью реально бежит (gpt-5.4), а не молча уходит в claude-fallback того же вендора, что имплементер; устранить рассинхрон «декларация ↔ факт».

**Рычаг.** Первично — A1 ([evaluator.py:57-78](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L57)); флоу-пин `review` на codex в target [.worc/flows/implementation.yaml:93](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation.yaml).

**Шаги.**

1. После A1 прогнать один evaluator-узел на codex вживую; подтвердить по `provider_attempts`, что attempt=1 (codex) = `succeeded`, а не `process_crashed`.
2. Принять решение по fallback-семантике: сейчас fallback берёт claude-конфиг-дефолт (`--model claude-opus-4-8`), потому что declared `gpt-5.4` неприменима к claude. Варианты: (а) считать допустимым, но честно задокументировать, что fallback-ревью = same-vendor; (б) помечать прогон явным флагом «cross-provider независимость не достигнута», чтобы декларация в flow не вводила в заблуждение.
3. Побочно (шум из F24): разобраться с попыткой codex поднять неавторизованный Figma-MCP (`rmcp::transport::worker … AuthRequired(mcp.figma.com)`) — изолировать MCP-конфиг спауна либо задокументировать как безвредный шум ~1-2с.

**Тест.** После A1 — интеграционная проверка (fake-CLI codex, отдающий валидный `{"findings":[…]}`), что evaluator принимает codex-вывод и НЕ уходит в fallback.

**Зависимость.** Только после A1.

---

### A3 · F32 (MEDIUM · OPEN) — вход ревью (`{diff_path}`) кумулятивен (chain) и pre-documentation

**Цель.** Давать ревью (и документации) инкрементальный дифф именно текущей задачи, а не `<base>..worktree`; убрать ложные scope-находки, повторяющийся ложный «phase-doc не обновлён» и нерезолвимые line-refs.

**Рычаг.** Код — [git_manager.py:1173](../../src/wastech_orchestrator/git_manager.py#L1173) (`write_current_diff`); промпт — target [.worc/flows/implementation/review.md](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/review.md).

**Шаги.**

1. Код: строить дифф для ревью/документации из набора файлов/диапазона коммитов **этой** задачи, а не `<base>..worktree`. В chain-режиме (общая неслитая ветка) `base=main` даёт кумулятивный дифф всех предыдущих задач — ревью p4-07 видело 35 файлов при ~5 изменённых. Инкрементальный дифф снимает три наблюдавшихся следствия: ложную scope-находку, ложный «doc не обновлён» (ревью бежит до documentation), нерезолвимые line-refs.
2. Промпт: в `review.md` оговорить, что дифф может быть кумулятивным/pre-documentation — судить по плану задачи, не флагать код прежних задач как scope-drift и обновления доков; цитировать `source-path + symbol`, а не diff-офсеты.

**Тест.** Юнит/интеграция на `write_current_diff` в chain-сценарии: две задачи на одной ветке — дифф второй содержит только её файлы, не файлы первой.

**Зависимость.** Независим от A1/A2; логически относится к ревью-конвейеру.

---

### A4 · F31 (LOW-MEDIUM · OPEN) — узел `review` не получает пакет памяти; блок `{memory_path}` в `review.md` мёртв

**Цель.** Прокинуть пакет памяти в evaluator-раннер, чтобы reviewer-preference-ранжирование `packet.py` (`_REVIEWER_PREF_NODES={review,fixing}`) работало и блок `{?memory_path}` в `review.md` не был мёртвым.

**Рычаг.** [evaluator.py:289-300](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L289) (`_prompt_variables`); образец — agent-раннер [nodes/agent.py:534,596-600](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L534); [memory/packet.py:41](../../src/wastech_orchestrator/memory/packet.py#L41).

**Шаги.**

1. В `_prompt_variables` evaluator-раннера добавить ключ `memory_path` и построить пакет через `build_packet` (как в agent-раннере).
2. Альтернатива, если решено памятью review не кормить, — убрать мёртвый `{?memory_path}`-блок из `review.md`. Предпочтителен вариант 1: ревью — узел, которому «recurring reviewer expectations» полезнее всего.

**Тест.** Юнит: evaluator-раннер прокидывает `memory_path` и рендерит непустой memory-блок, когда пакет непуст.

**Зависимость.** Код независим, но наблюдаемый эффект появляется только после B1/B2 (сейчас память пуста). Делать после B1/B2.

---

## Секция B — Управляемая память (`.worc/memory`)

### B1 · F29 (MEDIUM-HIGH · OPEN) — рассинхрон словаря `evidence.type`: `file`/`commit` не распознаются trust-классификатором

**Цель.** Устранить главную причину пустоты `long_term/`: 18/21 репо-обоснованных уроков навсегда деградируют до `agent-inferred` только из-за нераспознанных токенов `file`/`commit`.

**Рычаг.** [memory/lifecycle.py:24-54](../../src/wastech_orchestrator/memory/lifecycle.py#L24) (`assign_trust`, классы `_REPO`/`_ARTIFACT`); [memory/delta.py:119](../../src/wastech_orchestrator/memory/delta.py#L119) (`DELTA_OUTPUT_SCHEMA`, свободная строка `evidence.type`); роль-промпт target `summary.md`.

**Шаги.**

1. Код (первично): в `assign_trust` добавить `file → _REPO`, `commit → _ARTIFACT` (или нормализовать токены к существующим классам).
2. Опционально: enum-ограничить `evidence.type` в `DELTA_OUTPUT_SCHEMA` и задать словарь в `summary.md`, чтобы супервайзер писал только известные классификатору токены.

**Тест.** Юнит на `assign_trust`: `{"type":"file"}` и `{"type":"commit"}` дают durable-класс, а не `agent-inferred`. Регресс-проверка полного словаря токенов против классов.

**Зависимость.** Первый из двух «замков» на `long_term/`; делать до A4.

---

### B2 · F30 (MEDIUM · OPEN) — рекуррентность ключуется по дословному `subject` → реальный повтор не промоутится

**Цель.** Дедуплицировать семантически один урок при дрейфе формулировки `subject`, чтобы реальный 3× повтор (prettier-baseline-drift в p4-01/06/07) накапливал `recurrence` и промоутился.

**Рычаг.** [memory/service.py:562](../../src/wastech_orchestrator/memory/service.py#L562) (`_derive_id`); [memory/lifecycle.py:79](../../src/wastech_orchestrator/memory/lifecycle.py#L79) (`normalize_subject` = только lower+trim); `should_promote` [lifecycle.py:84-107](../../src/wastech_orchestrator/memory/lifecycle.py#L84) корректен — до него не доходит накопленный повтор.

**Шаги.**

1. Сделать ключ дедупа/рекуррентности устойчивым к дрейфу формулировки: например `kind` + нормализованные `scope.paths` (а не сырой `subject`), либо fuzzy-match `subject`.
2. Убедиться, что `seen_task_ids` теперь накапливается по разным формулировкам одного урока и `recurrence` достигает `promote_min_tasks`.

**Тест.** Юнит: три записи одного урока с разными `subject`, но общим `scope.paths`, дают один `memory_id` и `recurrence=3`.

**Зависимость.** Второй «замок»; вместе с B1 разблокирует непустой `long_term/`.

---

### B3 · F36 (LOW · OPEN) — абсолютные host-пути в эпизодах памяти + невоспроизводимая редакция

**Цель.** Хранить в эпизодах `.worc`-относительный POSIX-путь (как декларирует `records.py`), устранить недетерминизм редакции host-путей (в 6 из 8 записан как есть, в 2 — `[REDACTED]`).

**Рычаг.** [core/orchestrator.py:2117](../../src/wastech_orchestrator/core/orchestrator.py#L2117) (построение эпизода с абсолютным путём); харвест redaction-литералов [orchestrator.py:2047](../../src/wastech_orchestrator/core/orchestrator.py#L2047) (`_memory_extra_secrets`).

**Шаги.**

1. Хранить `artifact_paths` эпизода как `.worc`-относительный путь, а не абсолютный `/Users/...`.
2. Разобраться с невоспроизводимой редакцией: набор redaction-литералов харвестится из преходящего состояния процесса (env-секреты + `.env`/`secrets/**`), из-за чего в 2 прогонах какой-то литерал совпал с префиксом безобидного пути. После relativize абсолютный префикс исчезает и проблема снимается; отметить как сигнал для security-чокпоинта.

**Тест.** Юнит: эпизод строится с repo-relative POSIX-путём; редакция идентичных данных детерминирована.

**Зависимость.** Гигиена; после B1/B2.

---

## Секция C — Нативная память Claude Code (изоляция, безопасность)

### C1 · F37 (HIGH · OPEN) — теневая нативная память: спаунящиеся агенты читают/пишут `~/.claude/projects/<target>/memory/` вне изоляции, редакции и аудита

**Цель.** Закрыть пробой изоляции: спаунящийся `claude` пишет durable-файлы в `~/.claude/` оператора (вне рабочего дерева, `current.diff`, коммита и аудита оркестратора), утекает нередактированный `originSessionId`, и рядом с управляемой `.worc/memory/` работает вторая, неуправляемая память со снятыми poisoning-защитами.

**Рычаг.** [providers/claude.py](../../src/wastech_orchestrator/providers/claude.py) (конфигурация спауна; `--allowedTools Read,Glob,Grep,Edit,Write,Bash`, `--disallowedTools` запрещает только `.env`/`secrets/**` и git/gh); `CLAUDE_CONFIG_DIR` в `security.allowed_environment` прокидывается в домашний конфиг.

**Шаги.**

1. Отключить нативную память Claude Code для спаунящихся агентов: изолированный `CLAUDE_CONFIG_DIR`/settings, чтобы memory-система не инъектировалась и memory-директория не подхватывалась по `cwd`.
2. И/или конфайнить `Write`/`Edit` рабочим деревом: запретить запись по путям вне репо (`--disallowedTools` на внешних путях / контур `--add-dir`).
3. Проверить кросс-платформенно (Windows/Linux/macOS): пути и переменные окружения различаются — не завязываться на `~/.claude` буквально.

**Тест.** Интеграция (fake-CLI/фикстура): спаун claude не читает и не пишет вне рабочего дерева; `CLAUDE_CONFIG_DIR` спауна изолирован от домашнего.

**Зависимость.** HIGH; делать сразу после A1. Не зависит от секции B, но концептуально связана: обе про память.

---

## Секция D — Зависимости и цепочки задач (`depends_on` / branch-mode)

### D1 · F25 (MEDIUM · OPEN) — `depends_on` не переживает переименование зависимости при abandon+retry-под-новым-id

**Цель.** Не оставлять зависимые pending-задачи в вечном `WAITING`/`refuse` без внятной диагностики, когда зависимость заброшена и перезапущена под новым task id.

**Рычаг.** [core/orchestrator.py:722-743](../../src/wastech_orchestrator/core/orchestrator.py#L722) (`_resolve_dependency` резолвит буквально по строке id); опц. `worc list`/`worc status`.

**Шаги.** Выбрать один-два из вариантов (не автосвязывать — слишком неявно):

1. `_resolve_dependency` при статусе зависимости `abandoned` ищет в ledger более позднюю запись с тем же `title` и статусом `done`/PR merged и **предупреждает** «id `X` заброшен, возможно вы имели в виду `<related>-v2`».
2. `worc list`/`worc status` показывают advisory «N pending-задач ссылаются на заброшенный id X».
3. Задокументировать в task-authoring, что abandon+retry-под-новым-id требует ручной правки `depends_on` у всех зависимых задач (самый дешёвый, но не защищает от забывчивости).

**Тест.** Юнит: `_resolve_dependency` на `abandoned`-зависимость выдаёт диагностику с подсказкой замены.

**Зависимость.** Независим.

---

### D2 · F26 (MEDIUM · OPEN, design gap) — `depends_on`-merge-gate не совместим с `branch_mode: existing/current` chain-continuation

**Цель.** Разрешить конфликт двух механизмов выражения порядка: merge-gate (раздельные PR) vs физическое продолжение одной неслитой ветки. Сейчас интра-chain `depends_on` виснет навсегда, т.к. общий PR по определению открыт до конца цепочки.

**Рычаг.** [core/orchestrator.py:745-763](../../src/wastech_orchestrator/core/orchestrator.py#L745) (`_dependency_merged`); ADR `archive/done/branch-mode.md`.

**Шаги.** Осознанно не чинили в кампании (workaround: убрали `depends_on`-на-соседей из p4-03..p4-08). Варианты:

1. Документировать в `branch-mode.md`/`task-authoring.md`: порядок внутри branch-mode-цепочки выражать ЛИБО через `depends_on` (тогда раздельные PR/мержи), ЛИБО через `branch_mode: existing/current` (тогда intra-chain `depends_on` убрать — порядок гарантирует последовательность запуска), но не оба.
2. Код: `_dependency_merged` дополнительно резолвит ELIGIBLE, если у зависимости и текущей задачи совпадает эффективный working branch (код уже физически доступен независимо от merge-статуса общего PR).

**Тест.** Если выбран вариант 2 — юнит: две задачи на общей ветке, вторая `ELIGIBLE` при открытом общем PR.

**Зависимость.** Независим; решение (док vs код) принять до реализации.

---

### D3 · F27 (LOW · OPEN) — PR-reuse не обновляет title/body: переиспользованный PR несёт метаданные первой задачи цепочки

**Цель.** Чтобы заголовок/описание PR не вводили ревьюера в заблуждение об объёме (PR #9 весь прогон p4-02..p4-08 имел title/body от p4-02).

**Рычаг.** [git_manager.py:992-1015](../../src/wastech_orchestrator/git_manager.py#L992) (`create_pr`, путь `reused is not None` возвращает URL без `gh pr edit`); [git_manager.py:1012-1015](../../src/wastech_orchestrator/git_manager.py#L1012).

**Шаги.** Рекомендация из обсуждения 2026-07-05 — **вариант 2 (append-секция, keyed по task id)**: дешёвый, идемпотентный, не рискует затереть ручные правки оператора.

1. На пути `reused is not None` дописывать в body секцию `## <task_id> — <title>` под существующим содержимым.
2. Идемпотентность: секция keyed по `task_id`, чтобы `rerun` той же задачи не дублировал запись.
3. Трейд-офф — лишний `gh pr edit` на каждую задачу цепочки (мелкий API-write); принять осознанно.

**Тест.** Юнит: повторный reuse дописывает секцию нового `task_id`; повторный reuse того же `task_id` не дублирует.

**Зависимость.** Независим; косметика/наблюдаемость.

---

## Секция E — Наблюдаемость диффа (control-байты, оркестратор)

### E1 · F35 (LOW · OPEN, оркестраторный угол) — committed control-байты (NUL) не ловятся гейтом; ревью не видит NUL даже с `--text`

**Цель.** Дать оркестратору защиту от committed control-байтов, которая работает для любого репо (фикс F20 `--text` рендерит файл текстом, но NUL остаётся невидимым в диффе → ревью его не ловит).

**Рычаг.** preflight / `checks`-подсистема (гейт на control-байты в диффе). Target-код — см. F3.

**Шаги.**

1. Добавить опциональную preflight/`checks`-проверку на control-байты (NUL и пр.) в диффе задачи; при обнаружении — предупреждение/фейл гейта с указанием файла.
2. Цель — сделать рецидивы (F23 → F35) видимыми на уровне оркестратора, а не только вручную через `git show`.

**Тест.** Юнит: дифф с NUL-байтом флагается гейтом.

**Зависимость.** Независим; парная target-часть — F3 (F35 target-код).

---

## Секция F — Дрифт кастомизированных промптов и кода в target-репо (wastech-mdlint)

Правки в `.worc/flows/` и коде target-репо `wastech-mdlint`, а не в оркестраторе. Симптом отсутствия проверки актуальности кастомизированных промптов.

### F1 · F33 (LOW-MEDIUM · OPEN) — инвариант «sort every output array» без исключения для упорядоченных последовательностей

**Цель.** Убрать провокацию over-sorting осмысленных последовательностей (единственный blocking-баг кампании p4-05: `readingOrder.map(relativize).sort(byPath)` затёр топологический порядок алфавитным).

**Рычаг.** target [.worc/flows/implementation/implementation.md:15](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/implementation.md) (## Hard Invariants) + [review.md:23](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/review.md) (зеркалит правило в blocking-списке).

**Шаги.** Добавить в формулировку инварианта оговорку: сортировать path-keyed массивы, но НЕ трогать осмысленные последовательности (topological / reading / ranked order). Синхронно поправить `implementation.md` и `review.md`.

**Тест.** Проверка кампании — отсутствие рецидива over-sort на задачах с упорядоченными выходами.

**Зависимость.** Независим (target-репо).

---

### F2 · F34 (LOW · OPEN) — planning-промпт ссылается на несуществующие «core primitives»

**Цель.** Убрать латентный misdirect: `planning.md` перечисляет к переиспользованию `graph/build.ts`, `markdown/parse.ts`, `llm/budget.ts` — 3 из 4 путей не существуют (фактически `parse-document.ts`, `build-context-graph.ts`; директории `llm/` нет).

**Рычаг.** target [.worc/flows/implementation/planning.md](/Users/a1234/Documents/GitHub/wastech-mdlint/.worc/flows/implementation/planning.md) (секция Roadmap And Architecture). Packaged `planning.md` — generic, этих путей не содержит.

**Шаги.** Заменить список на реально существующие пути v2-монорепо либо сделать секцию generic (как packaged).

**Тест.** Проверка, что перечисленные пути существуют в репо (можно как lightweight-проверку актуальности кастом-промптов).

**Зависимость.** Независим (target-репо).

---

### F3 · F35 (LOW · OPEN, target-код) — рецидив NUL-делимитеров в `graph-algorithms.ts` и `graph.e2e.test.ts`

**Цель.** Убрать committed NUL-байты, делающие файлы git-binary (2 из 47 файлов PR #9, включая весь e2e-тест p4-08, не ревьюятся через `git diff`/GitHub).

**Рычаг.** target-код: `packages/core/src/graph/graph-algorithms.ts:42` (`` `${edge.from}\x00${edge.to}` ``) и `graph.e2e.test.ts` (`edgeSortKey`). Образец корректного паттерна — `query.ts:62` (пробел вместо NUL).

**Шаги.** Заменить NUL-делимитер на пробел (или иной печатный разделитель) в ключах join, по образцу `query.ts`. Ключи самосогласованы — функционально безвредно, но подрывает человеко-ревью и merge/diff-инструменты.

**Тест.** Существующие e2e-тесты graph должны остаться зелёными после смены делимитера.

**Зависимость.** Парная оркестраторная часть — E1 (гейт на control-байты). Независим (target-репо).

---

## Аппендикс — остаточный follow-up закрытой находки

### F21-follow-up (RESOLVED, остаток) — live-подтверждение allowlist-гейта planning под `default`-режимом

F21 закрыта: `providers/claude.py` переведён `read-only → ("default", ("Read","Glob","Grep"))`, юнит-тест подтверждает отсутствие Edit/Write в `--allowedTools`. Остаётся живая проверка, что реальный `claude`-процесс под этим режимом действительно отказывает в записи (см. [follow_ups.md](follow_ups.md) 2026-07-04). Не блокирует статус находки; выполнить при ближайшем живом прогоне planning.

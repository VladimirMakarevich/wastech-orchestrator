# Разбор прогона: p6-01-repo-scan-detection (фаза 6, проход 21)

## STATUS: DONE ✅ (успешный прогон, PR #12 открыт, не смержен)

- **task_id:** `p6-01-repo-scan-detection`
- **final_status:** `done` (подтверждено `worc status` + ledger `completed.jsonl`)
- **finished_at:** 2026-07-09T16:19:04Z
- **PR:** [#12](https://github.com/VladimirMakarevich/wastech-mdlint/pull/12) (против `main`, ветка `feat/p6-init`, 1 коммит `b1f8cad`, `auto_merged: false`)
- **fix_iterations:** 6 (review сошёлся на 7-м проходе; потолок `max_fix_cycles: 15` не достигнут)
- **Версия оркестратора:** main (со всеми влитыми мержами, вкл. content-flows PR #25; `DB_SCHEMA_VERSION=15`)

## Короткий вывод

Прогон прошёл **успешно и качественно**: код-модуль `discovery/` (repo-scan + workspace-packages + package-manager + константы), экспорты, тесты — реализованы в скоуп задачи, все 28 механических проверок (typecheck/lint/test/build ×7 раундов) зелёные, конвергенция review достигнута в пределах бюджета, без единого фоллбэка/ретрая/краша. Блокирующий codex-review сделал реальную работу — за 6 rework-циклов поймал 13+ настоящих edge-case багов (пустые `workspaces`, переписывание glob-loader'ом, негативные globs, вложенные пакеты, неэкранированные glob-метасимволы, inline-комменты в `pnpm-workspace.yaml`, директория/симлинк как lockfile).

**Единственный главный рычаг (для эффективности будущих прогонов):** фронт-лоадить edge-case-самоаудит в роль **implementation** (`implementation/implementation.md`) — тот же перечень краёв, что review перечисляет в своей роли (empty input, unusual paths, error handling, glob-метасимволы, симлинки, monorepo-углы). Сейчас implementation делает «минимальную фокусную правку» и передаёт дальше, а review вскрывает края по одному за 6 раундов. Проактивное покрытие свернуло бы петлю с 6 до ~2-3 циклов — и, как следствие, срезало бы доминирующую стоимость supervisor-слоя (он вызывается на каждом шаге).

**Отдельно — самая серьёзная находка по оркестратору (не влияет на этот прогон, но ломает preflight у всех операторов):** F44 — packaged content-флоу (PR #25) валятся на preflight/install в любом не-контентном репозитории.

## Как прошёл прогон (фактический путь по флоу)

Дефолтный флоу `implementation` (задача без `flow:` → дефолт). **`refinement` не выполнялся** (`tasks.refinement_ran` пусто) — задача хорошо специфицирована (детальная эвристика в теле), refinement условный и был пропущен. Провайдеры: **primary = claude** (смена относительно codex-primary кампании P5); review и supervisor — codex.

Фактическая цепочка (из `node_runs`, все узлы — 1 попытка, `route_fallback` = сконфигурированный резерв, **ни разу не срабатывал**):

```
planning(claude opus-4-8/high, 578s)
 → implementation(claude opus-4-8/high, 562s)
 → [ testing(4/4 ✅) → review(codex gpt-5.4/high) = REWORK → fixing(claude sonnet-5/xhigh) ] × 6
 → testing(4/4 ✅) → review(codex) = ACCEPT (findings=[])
 → documentation(claude opus-4-8/high, 236s)
 → task-finalize (supervisor codex summary 112s)
 → publish (PR #12) → done
```

Роли/модели по узлам (per-node override в активном флоу):

| Узел | Провайдер / модель / reasoning | Прогонов |
| --- | --- | --- |
| planning | claude / opus-4-8 / high (global default) | 1 |
| implementation | claude / opus-4-8 / high | 1 |
| review | codex / gpt-5.4 / **high** (override, снижен с xhigh — наследие тюнинга P5 F42) | 7 |
| fixing | claude / **sonnet-5 / xhigh** (override, не opus!) | 6 |
| documentation | claude / opus-4-8 / high | 1 |
| supervisor (constant layer) | codex / gpt-5.4 / **xhigh** (target-override дефолта medium) | 24 |

**Циклы фиксов:** `review_fix_total=6`, `review_fix_cycles=0` (обнулён при сходе — корректно), `test_fix_total=0` (checks фикс не гоняли). **Счётчик review_fix теперь персистится** через `review_fix_total` (колонка v14/F49) — это закрывает аудит-пробел, отмеченный в F42.

**Стоимость (токены, агрегировано по `result.json`):**

| Узел | input | из них cached | output | reasoning | вызовов |
| --- | --: | --: | --: | --: | --: |
| **supervisor** | **38 753 207** | 35 487 616 | 348 469 | 125 090 | **24** |
| review | 5 329 360 | 4 654 720 | 104 942 | 84 178 | 7 |
| fixing | 8 339 | 0 | 109 481 | 0 | 6 |
| implementation | 4 452 | 0 | 48 583 | 0 | 1 |
| planning | 4 372 | 0 | 49 683 | 0 | 1 |
| documentation | 1 131 | 0 | 14 772 | 0 | 1 |
| **ИТОГО** | **44 100 861** | 40 142 336 | 675 930 | 209 268 | 40 |

Supervisor = **~88% всего input-объёма** прогона (advisory-слой, не может делать rework), усилен фикс-циклами: 24 вызова на 24 завершённых шага (6 фикс-циклов = 18 «лишних» шагов). Память включена: **11 `memory_write`** записей.

## Находки по убыванию влияния

### F44 (HIGH) — packaged content-флоу ломают preflight/install на любом не-контентном репо · зона orchestrator

**Доказательство.** `worc preflight` на mdlint-конфиге: `flow content_book: FAIL`, `content_chapter: FAIL`, `content_translate: FAIL` → `preflight: NOT ready`. Точное нарушение (получено воспроизведением `FlowRegistry.validate_all()`): `[config] node 'constraints': tool 'check_journey' not found under '.../.worc/tools' (expected an executable file at .worc/tools/check_journey)`. У mdlint нет ни `.worc/tools/`, ни блока `tools:`.

**Корневая причина.** Контент-флоу влиты в **packaged built-ins** (коммит `3fb23ad`, PR #25). Preflight/install фатально валидируют **все** packaged-флоу через `validate_all()` ([cli.py:2126-2132](../../src/wastech_orchestrator/core/flow/../../wastech_orchestrator/cli.py)). Контент-флоу требуют tool-узел `check_journey`, доставляемый `worc install` только для контент-репозитория. Гейт «unregistered tool = fatal» корректен для флоу самого оператора, но теперь применяется к packaged-флоу в каждом репо.

**Рычаг.** `src/wastech_orchestrator/core/flow/validator.py` (`validate_flow_against_config`) / `registry.py` (`validate_all`): не проваливать фатально packaged-флоу, чей `task_type` в этом репо недостижим/не выбирается, **или** для packaged-флоу трактовать отсутствующий repo-специфичный tool как WARN (не FAIL), **или** гейтить контент-флоу наличием `tools:`/`.worc/tools/`. Зона — orchestrator (влияет на **всех** операторов, обновивших оркестратор после PR #25).

**Влияние.** Preflight и `worc install` падают на любом репо, где стоит свежий оркестратор и нет `check_journey`. `worc run` при этом НЕ гейтит (`validate_all` вызывается только в preflight/install), поэтому прогон p6-01 удалось провести, но операторская проверка «готов ли инструмент» сломана.

### F46 (MEDIUM, стоимость) — supervisor constant-layer доминирует по токенам · зона target-config (+ дизайн-заметка)

**Доказательство.** Агрегат токенов (таблица выше): supervisor 38.75M input (88% прогона), 24 вызова, advisory-only. Target-конфиг переопределяет supervisor на `provider: codex / model: gpt-5.4 / reasoning: xhigh`, тогда как packaged-дефолт ([config.example.yaml:234-238](../../src/wastech_orchestrator/packaged/config.example.yaml)) = `claude / opus-4-8 / reasoning: medium`.

**Корневая причина.** Supervisor — постоянный слой, проверяющий КАЖДЫЙ завершённый шаг ([[supervisor-constant-layer]]), с ре-ингестом растущего контекста задачи. При xhigh reasoning на codex и глубокой review-петле (24 шага) это даёт доминирующую стоимость при том, что слой advisory (не может делать rework).

**Рычаг.** Target `.worc/config.yaml` → `supervisor.reasoning: medium` (вернуть к packaged-дефолту) — самый дешёвый выигрыш без изменения кода. Дизайн-опция (orchestrator, обсуждаемо): не запускать supervisor на детерминированных `testing`-узлах / снижать частоту проверки на длинных фикс-петлях. Зона — прежде всего target-config.

**Влияние.** Снижение reasoning supervisor + сокращение числа фикс-циклов (см. главный рычаг) кратно срезают токен-стоимость прогона.

### F43 (MEDIUM) — review→fix «двигание ворот»: locked-decision не трактуется как нерушимая · зона orchestrator (role-prompts)

**Доказательство.** `evaluations`: review #4 (eval id 16) потребовал сделать fallback MDX-aware (`**/*.md` неполон, т.к. сканер собирает `.md`+`.mdx`), предложив два варианта — один из них меняет спеко-константу. fixing #4 выбрал спеко-нарушающий → `**/*.{md,mdx}`. review #5 (eval id 20) откатил: «fallback больше не соответствует контракту P6.01 — требуется буквально `**/*.md`». Задача (`task.normalized.json`) дословно: «`**/*.md` stays the fallback». Аналогично review #2→#6 по `noiseDirNames`: #2 попросил учитывать tunable → fixing добавил публичный параметр → #6 (low) пометил его как утечку внутреннего knob в public API.

**Корневая причина.** Роль review ([implementation/review.md](../../src/wastech_orchestrator/packaged/flows/implementation/review.md)) якорится на acceptance-критериях (стр.17, 19), но при формулировке fix НЕ обязывает сохранять locked-константы задачи; роль fixing ([implementation/fixing.md](../../src/wastech_orchestrator/packaged/flows/implementation/fixing.md)) буквально исполнила review-совет, противоречащий спеке, вместо того чтобы предпочесть спеку. Это НОВОЕ относительно F42, которая утверждала «loop прогрессировал, не двигание ворот» — здесь goal-moving зафиксирован.

**Рычаг.** review-роль: когда находка касается спеко-locked-константы, требуемый fix обязан сохранять locked-decision (а не менять её ради консистентности). fixing-роль: при конфликте review-совета с явной locked-decision задачи — спека выигрывает, конфликт surface'ить, а не тихо чинить в пользу review. Обе роли: packaged `src/wastech_orchestrator/packaged/flows/implementation/{review,fixing}.md` (все репо) или target `.worc/flows/implementation/{review,fixing}.md` (только mdlint). Зона — orchestrator.

**Влияние.** Устранение ~1-2 «холостых» циклов из 6 (те, что вызваны туда-обратно, а не новыми багами).

### F42 (рецидив, LOW-MEDIUM) — глубина блокирующего codex-review · зона orchestrator (калибровка)

**Подтверждение на p6-01.** 6 rework-циклов при review=`high` (на p5-04 было 7 при xhigh). БОЛЬШИНСТВО раундов — прогрессивные (новые реальные баги в объективно богатом углами домене glob/workspace/monorepo), сошлось в бюджете. Это тот же феномен, что F42; **главный рычаг (implementation edge-hardening) — апстрим-причина глубины**. Аудит-пробел из F42 (review_fix counter не персистится) **закрыт** колонкой `review_fix_total` (v14). Новый оттенок goal-moving вынесен в F43.

### F45 (LOW-MEDIUM, DX) — preflight прячет текст нарушения флоу · зона orchestrator

**Доказательство.** `worc preflight` печатает `flow content_book: FAIL — flow validation failed (1 violation(s)):` без самой строки нарушения. Причина: [cli.py:2132](../../src/wastech_orchestrator/cli.py) — `f"flow {name}: FAIL — {error.splitlines()[0]}"` берёт только первую строку сообщения валидатора, а реальные нарушения идут ПОСЛЕ `\n` ([validator.py:91](../../src/wastech_orchestrator/core/flow/validator.py)).

**Рычаг.** [cli.py:2132](../../src/wastech_orchestrator/cli.py): печатать все строки нарушения (с отступом), а не `.splitlines()[0]`. Зона — orchestrator (DX/операторская диагностика).

## Пробелы в данных

- **`prompt-audit/timeline.jsonl`** — `config.prompt_audit: true` выставлен, каталог `prompt-audit/` присутствует; отдельный поимённый таймлайн не использовался (реконструкция велась по `node_runs`/`provider_attempts`/лог-файлу — их хватило). Не пробел.
- **Долларовая стоимость** не считалась: смешанные вендоры (codex gpt-5.4 для review/supervisor, claude opus/sonnet для остальных), а гадать цены запрещено. Приведены токены.
- Содержимое review #7-accept — `findings_json = []` (пусто), подтверждает чистый accept.

## Что уже хорошо

- **Ноль инфраструктурных сбоев:** ни одного фоллбэка/ретрая/краша за 40 вызовов провайдера; `error_class` пуст везде. claude-primary + codex-review/supervisor работают согласованно.
- **Механические проверки надёжны:** 28/28 passed, 0 timeouts, на каждом из 7 раундов.
- **Review находит реальные баги:** это не шум — 13+ настоящих edge-case дефектов в фидлистом домене; итоговый код заметно надёжнее одно-проходного.
- **Конвергенция в бюджете:** сошлось на 7-м review (6 < 15), не упёрлось в потолок; per-node override (review=high, fixing=sonnet-5) отрабатывает.
- **Скоуп diff чистый:** +1163/−5, ровно модуль `discovery/` + тесты + минимальные docs; без скоуп-крипа (единственная пограничная правка — редактирование исходного spec-doc, допустимо как doc-sync).
- **state.db v15** пересоздан корректно после greenfield-сброса; память записала 11 записей; `review_fix_total` фиксирует глубину петли.

## План исправлений

**P0 (сделать первым — ломает всех операторов):**

- **F44** — починить фатальную валидацию packaged content-флоу при отсутствии repo-специфичного tool (`validator.py`/`registry.py`). До фикса preflight/install красный на каждом не-контентном репо.

**P1 (эффективность/стоимость будущих прогонов):**

- **Главный рычаг** — усилить роль `implementation` edge-case-самоаудитом (перечень краёв из review-роли), чтобы свернуть review-петлю с 6 до ~2-3 циклов.
- **F46** — target `supervisor.reasoning: xhigh → medium` (дешёвый выигрыш) ± дизайн-опция «не гонять supervisor на детерминированных testing-узлах».
- **F43** — в роли review/fixing закрепить «locked-decision задачи нерушима» (убирает goal-moving-циклы).

**P2 (DX/полировка):**

- **F45** — печатать полный текст нарушения флоу в preflight ([cli.py:2132](../../src/wastech_orchestrator/cli.py)).

## Сводная таблица

| Наблюдение | Причина | Рычаг (file) | Зона |
| --- | --- | --- | --- |
| preflight NOT ready на 3 контент-флоу | packaged content-флоу требуют `check_journey`, `validate_all` фатально валидирует все packaged | `core/flow/validator.py`, `registry.py` | orchestrator (F44) |
| supervisor = 88% input-токенов | advisory-слой на каждом шаге, codex/xhigh, усилен 6 фикс-циклами | target `.worc/config.yaml` `supervisor.reasoning` | target-config (F46) |
| 1-2 холостых review-цикла (mdx-fallback, noiseDirNames) | locked-decision не нерушима для review/fixing | `packaged/flows/implementation/{review,fixing}.md` | orchestrator (F43) |
| 6 rework-циклов (глубина) | implementation не покрывает края проактивно → review вскрывает по одному | `packaged/flows/implementation/implementation.md` | orchestrator (F42/главный рычаг) |
| «1 violation(s):» без причины | `error.splitlines()[0]` роняет строки нарушений | `cli.py:2132` | orchestrator (F45) |
| task-файл p6-01 исчез из base tree | untracked-файл перемещён на ветке, checkout на main не восстанавливает | восстановить с ветки `feat/p6-init` (b1f8cad); для `done` не требуется | ожидаемое поведение |

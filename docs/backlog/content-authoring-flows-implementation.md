# План реализации: контент-флоу Wastime Journey как встроенные (packaged) флоу + tool-нода с доставляемым исполняемым файлом

> Реализационный план к анализу [worc-flow-content-plan.md](../analysis/worc-flow-content-plan.md). Все утверждения про код проверены по исходникам (ссылки на файлы:строки внизу секций).

## 0. Что изменилось относительно анализа (два override'а владельца)

Анализ описывал вариант **без единой строчки кода в оркестраторе**: флоу и промпты живут в репозитории контента (`<content-repo>/.worc/flows/`), а детерминированный гейт — это скрипт `scripts/check_journey.py` в том же репозитории контента, запускаемый штатным чекером `command_profile`. Владелец сознательно меняет два решения (цель — финальное продакшен-реди решение, а не пилотный минимум):

1. **Все новые флоу переезжают в `src/wastech_orchestrator/packaged/`** и доставляются `worc install` во все репозитории (как `implementation`/`deep_research`/`security_audit`). Это делает контент-флоу встроенными, а не операторскими.
2. **Вместо `scripts/check_journey.py` в репо контента — настоящая нода `kind: tool`** (механизм P5, ветка `feat/p5-custom-tool-nodes`), а её исполняемый файл тоже **доставляется оркестратором при установке** в `.worc/tools/`.

Оба override'а превращают вариант «нулевой код» в реальное изменение оркестратора — доставляемое продакшен-реди решение. Ниже — что для этого нужно, с фазировкой и тестами.

## 1. Что уже есть vs что надо построить (по коду)

| Требование | Готово в коде? | Что делаем |
| --- | --- | --- |
| Встроенный флоу = `packaged/flows/<name>.yaml` + `packaged/flows/<name>/*.md` | ✅ механизм есть | Добавить YAML+промпты, **кода в инсталлере не надо** — `_copy_packaged_flows` копирует дерево обходом (`cli.py:667-688`) |
| Резолв флоу по имени файла (operator-first, packaged-fallback) | ✅ `registry.py` (`_PACKAGED_DIR`:44) | `flow.task_type` обязан совпадать с именем файла |
| `output_policy: code_change`, `publishing: documentation_pull_request` | ✅ enum'ы (`contracts.py:67-82`) | Использовать как есть |
| Нода `kind: tool` (exit-code/JSON-контракт, `{<id>_path}`) | ✅ P5 (`nodes/tool.py`, `tools_registry.py`) | Использовать вместо `checks: command_profile` |
| **Резолв tool-имени кросс-платформенно** (одно имя в packaged-флоу → и POSIX, и Windows) | ⛔ **НЕТ** — `resolve()` берёт ровно `tools_dir/<name>`, без перебора суффиксов; Windows-файл без суффикса `.exe/.bat/.cmd/.com` не проходит проверку исполняемости (`tools_registry.py:51-92`) | **Код: расширить резолвер перебором лаунчер-суффиксов** (см. §4.1) |
| **Доставка исполняемого файла в `.worc/tools/` при install** | ⛔ **НЕТ** — нет `packaged/tools/`, инсталлер не копирует исполняемые файлы, `config_writer` только пишет config | **Код: `packaged/tools/` + `_copy_packaged_tools` + `+x` на POSIX + backup/reconfigure** (см. §4.2) |
| Валидация tool-ноды при preflight (резолв против реестра) | ✅ `validator.py:557-568` | Следствие: tool **обязан** доставляться во все репо (см. §5) |
| Findings tool-ноды → цикл fix | ⛔ record-only (P5, отложено) | Не блокер: маршрутизация идёт по `pass`/`fail`, фиксер читает `{constraints_path}` (см. §4.3) |

Вывод: **требование 1 — почти чистая конфигурация** (YAML+промпты в `packaged/`), инсталлер их подхватывает сам. **Требование 2 — реальный код**: (а) кросс-платформенный резолв tool-имени и (б) доставка исполняемого файла на install. Это два главных объёма работы.

## 2. Требование 1 — упаковать флоу в `packaged/flows/`

Механически: под `src/wastech_orchestrator/packaged/flows/` кладём три YAML и три каталога промптов:

```
packaged/flows/
  content_chapter.yaml
  content_chapter/{context,revise,product_accuracy,story_critic,style,fixing,supervisor,summary}.md
  content_book.yaml
  content_book/{assemble,book_critic,book_fixing,supervisor,summary}.md
  content_translate.yaml
  content_translate/{adapt_en,en_critic,fixing,supervisor,summary}.md
```

Правила (из проверки кода):

- Имя файла = `flow.task_type` (реестр требует совпадения, `registry.py:95-100`).
- Каждый `role_file` — путь **внутри** каталога флоу (traversal наружу фатален, `prompt.py:26-38`, `validator.py:620`).
- `_copy_packaged_flows` копирует всё дерево обходом (`cli.py:677-688`) — **новый инсталлерный код не нужен**; `install` и `install --reconfigure` подхватят новые флоу автоматически. Плейн-реран пропускает существующие файлы, но **дописывает недостающие** — то есть на уже установленных репо новые флоу появятся и без `--reconfigure` (недостающие файлы дозаписываются).
- Бампа `CONFIG_SCHEMA_VERSION` **не требуется** — новые enum'ы/kind'ы не вводятся (`config/schema.py:149`).

Форма графов — по §3 анализа (флоу A/B/C). Отличие от анализа: узел детерминированного гейта — не `checks: command_profile`, а `kind: tool` (см. §4.3, §6).

## 3. Требование 2, часть А — исполняемый файл `check_journey`

Один параметризуемый исполняемый файл на все три флоу; режим выбирается через `args`:

- `args: {mode: ru}` — флоу A: ≤1 Title на страницу; валидная иерархия `##`→`###`→`####`; наличие `Purpose`/`Emotional point`; regex-скан AI-паттернов (`не … , а …` + список клише); запрет служебных ярлыков в заголовках. **Лимит символов на RU не применяется.**
- `args: {mode: en}` — флоу C: всё выше + длина `####` 500–800 символов (жёсткий максимум 800), ≤3 абзаца.
- `args: {mode: book}` — флоу B: глобально — ни одна страница не превышает лимиты.

Контракт исполняемого файла (по `nodes/tool.py`):

- **Вход — только stdin (JSON)**: `{task_id, node_id, subtask_order, paths:{repo, task_path, plan_path, diff_path, checks_path, review_path}, args:{mode: ...}}`. Ни секретов, ни полного окружения, ни git-кредов (`tool.py:168-177`, `context_paths.py:30-37`).
- **Область проверки**: изменённые главы. Берём из `paths.diff_path` (артефакт диффа), фолбэк — файлы, названные в `paths.task_path`. Так избегаем шума от нетронутых глав. (Открытый вопрос точности — §9.)
- **Выход**: печатает JSON `{"outcome":"pass"|"fail", "data":{...нарушения по файлам...}}` **и/или** просто выходит с кодом (0 → pass, ≠0 → fail). JSON `outcome` авторитетнее кода (`tool.py:206-230`). `data` кладётся в `structured_output`; stdout доступен ниже по графу как `{constraints_path}` — фиксер читает его, чтобы понять, что чинить.
- Файл **ничего не пишет в репозиторий** — это чистый валидатор (у tool-ноды нет git-кредов; findings record-only).

Реализация — Python (машина, где крутится worc, уже имеет Python). Файл самодостаточен, без сторонних зависимостей.

## 4. Требование 2, часть Б — доставка исполняемого файла и кросс-платформенность (главный код)

### 4.1. Кросс-платформенный резолв tool-имени (код в P5-резолвере)

Проблема: `ToolRegistry.resolve("check_journey")` берёт **ровно** `.worc/tools/check_journey` и проверяет исполняемость: POSIX — бит `+x`; Windows — суффикс в `{.exe,.bat,.cmd,.com}`. Перебора суффиксов нет (`tools_registry.py:51-92`). Значит **одно** имя `tool: check_journey` в packaged-флоу не может резолвиться и на POSIX (файл без расширения с `+x`), и на Windows (там файл без суффикса не «исполняемый»). Так как packaged-флоу — один и тот же файл на всех ОС, имя ноды фиксировано.

Единственный способ сохранить один packaged-флоу И кросс-платформенность И tool-ноду — **небольшое расширение резолвера**: при неудаче с голым именем на Windows перебирать `name + suffix` по `_WINDOWS_EXECUTABLE_SUFFIXES`. Каждый кандидат проходит те же проверки containment/existence/executability (fail-closed сохраняется). Это по сути закрывает кросс-платформенный пробел самого P5 (tool-нода, запускающая скрипт, сегодня не переносима).

- POSIX: `resolve("check_journey")` находит `check_journey` (`+x`, shebang) — как сейчас.
- Windows: голое имя не проходит → перебор находит `check_journey.cmd`.

Правило репо «branch platform differences explicitly and test both» — тест на обе ветки `os.name`.

> Альтернатива (НЕ рекомендуется): POSIX сейчас, Windows — follow-up. Нарушает жёсткий инвариант «cross-platform mandatory for every feature» (CLAUDE.md). Поэтому резолвер-перебор входит в поставку сразу, а не откладывается.

### 4.2. `packaged/tools/` + доставка на install (код в `cli.py`)

Зеркалим механизм флоу:

- `packaged/tools/` — новый каталог package-data. Содержит:
  - `check_journey` — самодостаточный Python-скрипт (shebang `#!/usr/bin/env python3`, вся логика §3);
  - `check_journey.cmd` — Windows-обёртка: `@python "%~dp0check_journey" %*` (интерпретатору расширение файла безразлично).
- `_tools_root()` — по образцу `_flows_root()` (`cli.py:652-664`), через `importlib.resources` (работает из дерева и из wheel). Убедиться, что package-data включает `packaged/tools/**` (проверить `MANIFEST.in`/`pyproject` `package-data`/`force-include`).
- `_copy_packaged_tools(dest_root, *, overwrite, dry)` — по образцу `_copy_packaged_flows` (`cli.py:667-688`): обход файлов → `.worc/tools/<rel>`; skip-existing если не `overwrite`; **после копирования на POSIX выставить `+x`** (`os.chmod(dest, mode | 0o111)`); на Windows — no-op (исполняемость по суффиксу). `dry` ничего не пишет.
- `_backup_tools_dir(worc_home)` — по образцу `_backup_flows_dir` (`cli.py:691-703`): перед `--reconfigure` снапшот `.worc/tools/` в `tools.bak-<UTC>`.
- Вызвать оба в `cmd_install` рядом с копированием флоу (`cli.py:3242-3257`): backup при `--reconfigure`, затем copy. `.worc/` уже целиком в gitignore (`append_runtime_excludes`, `cli.py:3262`) — исполняемые файлы не коммитятся, а пере-доставляются `install` на каждой машине.
- Доставка **per-machine**: `install` пишет в локальный `.worc/tools/`, а реестр читает его же (`orchestrator.py:466`), поэтому доставленный лаунчер всегда соответствует ОС установки. Кросс-машинная переносимость самого файла не нужна — нужен только резолвер-перебор из §4.1 (имя в YAML фиксировано).

### 4.3. tool-нода в графе (замена `checks`-гейта)

```yaml
- id: constraints
  kind: tool
  tool: check_journey # → .worc/tools/check_journey (POSIX +x / Windows .cmd)
  args: { mode: ru } # только плоские скаляры
  timeout_seconds: 600 # иначе config.tools.default_timeout_seconds (3600)
# edges:
#   constraints --pass--> product_accuracy
#   constraints --fail--> fixing   (fail-ребро ОБЯЗАНО нести loop/budget)
```

Отличия от `checks: command_profile`, которые надо учесть:

- **Плюс**: не нужен `checks.command_sets` в config репо контента; не нужен native-чекер; переносимость через доставку файла.
- **Минус/нюанс**: findings tool-ноды **record-only** (P5, `tool.py` docstring:19-24) — они не вливаются автоматически в цикл fix. Маршрутизация всё равно работает через `pass`/`fail`, а фиксер читает детали из `{constraints_path}` — этого достаточно для полноценной работы гейта. Авто-подача findings в fix — общий follow-up P5, вне этого плана.
- **Инфра-сбой/таймаут** tool-ноды → `manual` (парковка), не `fail`, и не тратит fix-итерацию (`tool.py:120-126`) — так же, как у `command_profile`. Это правильное fail-closed поведение.
- Все fail-рёбра замыкаются на `fixing`, `fixing → constraints` (как `fixing → testing` в implementation-флоу), чтобы фикс не сломал лимиты.

## 5. Следствия для валидации/preflight (важно)

`validate_all()` при preflight грузит и валидирует **каждый** packaged-флоу в **любом** репо, а config-aware валидация **резолвит каждую tool-ноду против реестра** (`validator.py:557-568`, `registry.py:106-167`). Отсюда жёсткая связка:

1. Раз контент-флоу упакованы, их валидируют при preflight **везде** — значит `check_journey` **обязан доставляться install во все репо безусловно** (иначе preflight падает у всех, кто поставил worc). Это ровно то, что просит владелец («доставлять при установке»), так что связка непротиворечива, но её надо зафиксировать: доставка tool — не опция, а часть корректности preflight.
2. Провайдеры/reasoning/ceiling/бюджеты контент-флоу должны проходить дефолтный packaged-config (тот, что пишет `install`). Используем те же, что `implementation.yaml` (claude/codex, workspace-write, стандартные reasoning) — тогда `validate_flow_against_config` зелёный.
3. Тесты, которые перечисляют набор packaged-флоу (если есть), надо обновить (см. §7).

## 6. Скелеты флоу (packaged)

Формы — по §3/§10 анализа, но гейт = tool-нода. Кратко:

- **`content_chapter`** (рабочая лошадка): `context (agent, ro)` → `revise (agent, editing_lineage, ww)` → `constraints (tool, mode: ru)` → `product_accuracy (evaluator: verifier, blocking, max_rework 2)` → `story_critic (evaluator: critic, blocking, max_rework 3)` → `style (agent, ww, reasoning: medium)` → `publish (documentation_pull_request | none)`; `fixing (agent, ww, lineage_affinity: revise)`; все rework/fail → `fixing` → `constraints`. Бюджеты: `global_fix_iterations`, `constraint_fix`, `accuracy_fix`, `critic_fix`.
- **`content_book`** (раз в конце): `assemble (agent, ww)` → `book_critic (evaluator: critic, blocking)` →(rework)→ `book_fixing` → `assemble`; `constraints (tool, mode: book)` → `publish`.
- **`content_translate`** (позже): `adapt_en (agent, ww)` → `constraints_en (tool, mode: en)` →(fail)→ `fixing_en` → `en_critic (evaluator: critic)` → `publish`.

Промпты ролей — по §4 анализа (контекст-разведчик, редактор-переработчик, валидатор продукта, критик истории, стиль-редактор, фиксер, супервизор/summary). `Story Bible` (`_wastime_journey.md`) роли читают по `{repo}`-относительному пути прямо в промпте (канон — файл, не память; §7 анализа). `emit_follow_ups` в супервизоре контент-флоу **не включаем** (код-ориентированная опция). Калибровка блокирующих циклов (F42): потолок `max_rework_per_stage` на критиках, стилистику держать вне блокирующего эвалуатора (§8.2 анализа) — чисто конфигом.

## 7. Тесты

- **Резолвер (§4.1)**: юнит-тесты `ToolRegistry.resolve` с перебором суффиксов, обе ветки `os.name` (monkeypatch): POSIX находит голое имя, Windows находит `.cmd`; containment/traversal по-прежнему fail-closed для каждого кандидата. Расширить `tests/core/test_flow_tools_registry.py`.
- **Доставка (§4.2)**: тест `_copy_packaged_tools` — копирует дерево, ставит `+x` на POSIX, skip-existing, `--reconfigure` бэкапит; интеграция в `cmd_install`. Рядом с тестами install/flows-seeding.
- **Исполняемый файл (§3)**: юнит-тесты `check_journey` как чистой функции/через stdin: mode ru/en/book, каждый вид нарушения → `outcome: fail` + `data`; чистый вход → `pass`. Фикстуры — примеры глав.
- **tool-нода в контент-флоу**: по образцу `tests/core/test_flow_tool_runner.py` — fake-исполняемый в temp `.worc/tools/`, прогон `ToolNodeRunner`, проверка `pass`/`fail`, `{constraints_path}`, редактирование артефактов, env-allowlist.
- **Валидация флоу**: `validate_all` зелёный с новыми packaged-флоу против дефолтного config; обновить любые тесты, перечисляющие набор packaged-флоу.
- Прогон `/run-checks` (ruff + mypy + pytest) перед каждым коммитом фазы.

## 8. Doc-sync (в том же изменении)

Правило репо: доксинк включает **shipped, operator-facing** доки под `src/wastech_orchestrator/packaged/` — `guide/`, `config.example.yaml`, встроенные флоу/промпты. Конкретно:

- `packaged/guide/flows/README.md` — описать новые встроенные флоу (`content_chapter`/`content_book`/`content_translate`) и tool-гейт.
- Документировать `.worc/tools/` как доставляемый install каталог и контракт `check_journey` (mode/вход-stdin/выход).
- `docs/` — где перечислены встроенные флоу и install-артефакты; отметить кросс-платформенный резолв tool-имён.
- Записать отложенное в [follow_ups.md](follow_ups.md) (напр. native `prose_constraints`-чекер §8.1 анализа — только если появится второй репо-книга; авто-подача findings tool-ноды в fix).
- Markdown без ручного переноса; `npx prettier@3 --write "**/*.md"` (кроме `packaged/guide/`, оно в `.prettierignore`).

## 9. Фазировка (one-commit-per-phase)

Правило репо — один squashed-коммит на фазу. Порядок так, чтобы каждая фаза была зелёной сама по себе. **Реализация пока не запускается — это план; владелец стартует отдельно (напр. `/implement`) после ревью.**

1. **Фаза 1 — инфраструктура tool-доставки (код)**: резолвер-перебор (§4.1) + `packaged/tools/` с `check_journey`/`.cmd` + `_copy_packaged_tools`/`_backup_tools_dir` + wiring в `cmd_install` + package-data + тесты (§7 первые три пункта). Отдельно от флоу — самодостаточно и проверяемо.
2. **Фаза 2 — все три флоу сразу** (решение владельца 2026-07-09): `content_chapter` (tool-гейт `mode: ru`), `content_book` (`mode: book`), `content_translate` (`mode: en`) — YAML + промпты всех ролей в `packaged/`, тесты флоу/валидации (`validate_all` зелёный), doc-sync. Одним изменением, т.к. флоу дешёвы (YAML+промпты) и делят один исполняемый файл.

Пилот (одна слабая Bonus-глава, §9 анализа) — это **прогон** первой главы через `content_chapter` после сборки, не отдельная фаза сборки; разбор — скилл `/analyze-task-run`. Подкрутки — промпты и `max_rework_per_stage` (не код).

## 10. Решения владельца (зафиксировано 2026-07-09)

1. **Кросс-платформенный резолвер (§4.1)** — ✅ **расширяем P5-резолвер** перебором лаунчер-суффиксов (сохраняет один packaged-флоу и Windows; закрывает пробел P5). Не POSIX-only.
2. **Объём** — ✅ **все три флоу сразу** (см. §9, фаза 2).
3. **Дальнейший шаг** — ✅ **пока только план**; реализацию владелец запускает отдельно после ревью.

Остаются к уточнению при реализации:

- **Область проверки `check_journey`** — диф-скоуп (`paths.diff_path`, тише) **или** все главы (проще, но шумит на пред-существующих нарушениях). Рекомендация — диф-скоуп с фолбэком на `task_path`.
- **Публикация** — `documentation_pull_request` (PR на главу) **или** `none`/`local_artifact` (локально). Из анализа §11; для одиночной работы локально проще.

## 11. Явные отказы (YAGNI, из анализа)

- **Native-чекер `prose_constraints`** — не сейчас (только если появится второй репо-книга; §8.1 анализа). tool-нода + доставляемый файл покрывают потребность переносимо.
- **Новый `when`-факт `config.adapt_en`** — не нужен: EN — отдельный флоу C.
- **Новый вид узла / политика публикации / output_policy** — не нужны: хватает `agent`/`evaluator`/`tool`/`publish` и существующих enum'ов.
- **Гибкий деливери-путь `repository_document`** — не нужен: `code_change` правит главы на месте, диф = деливери.

---

### Ссылки на код (проверено)

- Доставка флоу/шаблонов: `cli.py:611,652-703,3200-3267`; `install/config_writer.py`.
- Резолв флоу (operator-first, packaged-fallback): `core/flow/registry.py:44,77-167`.
- tool-нода: `core/flow/nodes/tool.py`, `core/flow/tools_registry.py:51-92`, `core/flow/context_paths.py:30-37`.
- Валидация tool против реестра при preflight: `core/flow/validator.py:198,230-231,557-568`.
- Schema/enum'ы: `core/flow/schema.py:110-134,154,229-230` (`ToolNode`, `FlowDoc`); `core/flow/contracts.py:32-82` (роли/политики); `config/schema.py:149,517-534` (версия/`tools`-блок).
- Промпт-переменные: `core/prompts.py:21-52`; `core/flow/prompt_vars.py:25-46` (`{<id>_path}`, `{?var}`).
- Образец графа: `packaged/flows/implementation.yaml`.

# План исправлений по TEST-FINDINGS (реальный прогон на wastech-mdlint)

Статус: **backlog / план работ.** Дата: 2026-07-03. Владелец: Vladimir Makarevich. Источник: [TEST-FINDINGS.md](../../TEST-FINDINGS.md) (кампания p0 на `wastech-mdlint`, прогоны 1–5).

Это последовательный план устранения дефектов, найденных в ходе реального прогона оркестратора. Находки сгруппированы **по подсистемам** (внутри подсистемы — по severity). Каждый пункт несёт: **проблема · рычаг (file:line) · изменение · тесты · docs/config · критерий готовности · severity**. Раздел A — worc-side (orchestrator default, чинится в этом репо). Раздел B — per-repo промпты/роли (чинятся в `wastech-mdlint`, не здесь). Раздел C — уже закрытое и наблюдения (контекст, действий не требуют).

Ссылки на код даны по состоянию репо на дату плана (ветка `fix/staged-pathspec-gitignored-tasks`, после коммита `1984180`). Все правки следуют инвариантам [CLAUDE.md](../../CLAUDE.md) и правилам [.agents/rules/](../../.agents/rules/): git трогает только оркестратор, кросс-платформенность обязательна, no-secrets, argv-без-shell. По каждой правке — обновить тесты и docs в том же изменении (`/sync-docs`), прогнать `/run-checks`.

## Легенда статусов

- 🔴 **CRITICAL** — блокирует автономный publish; чинить первым.
- 🟠 **HIGH** — теряется value (summary/follow-ups/память) или ломается штатный recovery-цикл.
- 🟡 **MEDIUM** — наблюдаемость, полнота аудита, UX/дефолты.
- ⚪ **LOW** — косметика, наблюдения.

## Рекомендуемый порядок выполнения

Подсистемы независимы, но по влиянию на автономность разумный порядок такой:

1. **S1 — publish-commit кластер** (F18 CRITICAL, затем F12 → F14 → F13). Разблокирует автономный publish для relocate/rename и корне-трогающих задач (p0-07 CI-релокации под риском). Начинать здесь.
2. **S2 — supervisor/finalize** (F7b, F16, F10). Возвращает читаемый PR-body и убирает утечку машинных артефактов.
3. **S3 — memory** (F9). Закрывает частичный промах AC-SF3.
4. **S4 — install/config-дефолты** (F1, F2, F3). Одна правка `config_writer` + схема; дешёвая, независимая.
5. **S5 — CLI** (F4) и **S6 — validation gate** (F5a, F6). UX/скриптинг и штатный re-submit-цикл.
6. **S7 — watch/audit** (F11). Полнота аудита merge-событий.

---

# Раздел A — worc-side (orchestrator default)

## S1. git_manager / publish-commit кластер

Общий корень кластера — хрупкость `git_manager._commit → git add -- <changed_code_paths>` к состоянию индекса и немота ошибки. F15 (первый триггер) уже закрыт; здесь — второй триггер и сопутствующая наблюдаемость/recovery.

### F18 — `git add` падает exit 128 на staged-deleted путях 🔴 CRITICAL

- **Проблема:** когда агент сам застейджил удаление/переезд файла (`git rm`/`git mv`, git не счёл это rename'ом), `changed_code_paths()` возвращает старый путь, отсутствующий в рабочем дереве и уже полностью в индексе. `git add -- src/cli.ts` → `fatal: pathspec 'src/cli.ts' did not match any files` (exit 128) → `GitCommandError` → падает **весь** commit (add all-or-nothing) → `manual_action_required`. Изолированно доказано на замороженном дереве p0-05; `-A` не спасает при явном pathspec.
- **Рычаг:** [git_manager.py:545-559](../../src/wastech_orchestrator/git_manager.py#L545-L559) (`changed_code_paths` читает `git status --porcelain`, берёт `line[3:]`, не различает staged-only deletion) и [git_manager.py:695-698](../../src/wastech_orchestrator/git_manager.py#L695-L698) (`_commit` → `git add -- *staged_pathspec(paths)`).
- **Изменение (рекомендация — вариант (a) из находки):** отфильтровать из pathspec пути, которые **уже полностью в индексе и отсутствуют в рабочем дереве** (staged deletions), — они и так войдут в коммит из индекса, добавлять их нечего. Технически: в `changed_code_paths` (или в новом хелпере, используемом `_commit`) смотреть двухсимвольный XY-код porcelain — фильтровать записи со staged-статусом в колонке 0 и пустой колонкой 1 для удалений (`D ` → путь отсутствует в worktree). Порядок предпочтения из находки: (a) фильтр pathspec по «есть unstaged/untracked-изменение» ▸ предпочтительно; (b) ловить exit 128 «did not match» — хрупко, отклонить; (c) коммитить из состояния индекса + добавлять только worktree-дельту — крупнее по объёму. **Выбрать (a).** Сохранить идемпотентность `_commit` (restart не должен падать).
- **Тесты:** регресс по образцу F15 — `test_commit_code_when_agent_staged_deletion` (репозиторий-фикстура: агент сделал `git rm` файла и создал новый untracked; `commit_code` должен пройти, коммит содержит и удаление, и новый файл). Плюс unit на фильтр pathspec: staged-deleted путь исключён, обычные modified/untracked — остаются. Кросс-платформенно (пути через `Path.as_posix()`, porcelain-парсинг устойчив к `\`-разделителям — уже нормализуется в `_is_artifact_path`).
- **Docs/config:** нет config-изменений. Обновить docstring `changed_code_paths`/`_commit` (описать инвариант «staged-only deletions не попадают в pathspec»); отметить в [docs/backlog/follow_ups.md](follow_ups.md) закрытие F18.
- **Критерий готовности:** relocate/rename-задача, где агент сам стейджит удаление, проходит publish **автономно** (`final_status=done`, PR открыт, без `manual_action_required`). `/run-checks` зелёный.

### F12 — ошибка publish-узла проглатывается (немой git-stderr) 🟠 HIGH

- **Проблема:** при падении `commit_code` git-stderr нигде не виден: в daemon-логе только info finalize, `completed.jsonl.failure_report=null`, `node_runs.error_class=publish_failed` (без текста). Единственная зацепка — `terminal-cleanup.json` (последствие, не причина). Именно немота F12 два прогона маскировала первопричины F15/F18.
- **Рычаг:** [core/flow/nodes/publish.py:64-82](../../src/wastech_orchestrator/core/flow/nodes/publish.py#L64-L82) (`except GitCommandError`). Текст ошибки уже есть в `GitCommandError` (из `git_manager._git_checked`, [git_manager.py:233-239](../../src/wastech_orchestrator/git_manager.py#L233-L239)).
- **Изменение:** в ветке `except GitCommandError` логировать `exc` (git stderr) на WARNING/ERROR в daemon-лог **и** прокидывать текст в `failure_report`/node-artifact (не только `error_class`). Проверить, что stderr не содержит секретов перед записью (redaction-инвариант) — git stderr путей/pathspec безопасен, но прогнать через существующий редактор артефактов, если он есть на этом шве.
- **Тесты:** unit — при `GitCommandError` в publish `failure_report` непуст и содержит git-сообщение; проверка, что daemon-лог получает WARNING/ERROR-строку. Секрет-редакция покрыта существующим тестом артефактов (расширить, если нужно).
- **Docs/config:** обновить раздел publish/recovery в docs (что теперь видно в логе/failure_report при падении publish).
- **Критерий готовности:** при принудительно-провоцируемом git-фейле publish оператор видит ПРИЧИНУ (git stderr) в логе и failure_report, не только `publish_failed`. Транзиент отличим от детерминированного без ручного репро.

### F14 — `rerun --continue` не восстанавливает commit-фейл publish (гейт «unaccounted changes») 🟠 HIGH

- **Проблема:** `publish.py` обещает «resumable via `rerun --continue`», но когда упал сам `commit_code`, дерево осталось грязным (staged-незакоммичено), и `plan_rerun` **отвергает** любой rerun гейтом «working tree has unaccounted changes». Штатный recovery недостижим ровно в сценарии, ради которого задуман; выход только ручной commit (нарушает инвариант «git — только оркестратор») либо выброс работы.
- **Рычаг:** [core/orchestrator.py:827-832](../../src/wastech_orchestrator/core/orchestrator.py#L827-L832) (`plan_rerun` → `unaccounted_dirty_paths()`), [git_manager.py:1025-1041](../../src/wastech_orchestrator/git_manager.py#L1025-L1041) (`unaccounted_dirty_paths`), [core/flow/nodes/publish.py:50-92](../../src/wastech_orchestrator/core/flow/nodes/publish.py#L50-L92) (publish-регион).
- **Изменение (рекомендация — вариант (a) из находки):** при `--continue`, когда `current_node` в publish-регионе, НЕ считать незакоммиченные **code**-changes «unaccounted» — это ожидаемый вход publish-узла (commit_code докоммитит их идемпотентно; ветка `if not paths: return HEAD` делает no-op на чистом дереве). Гейт должен различать fresh-rerun (чистое дерево ожидаемо) от `--continue`-в-publish (грязное = незакоммиченная работа). Артефакты (`.worc/`, tasks/) остаются исключены как раньше. Альтернатива (b) — `commit_code` коммитит ДО записи манёвра финализации, чтобы дерево было чистым к manual-стопу — крупнее, отложить.
- **Тесты:** сценарий — publish упал на `commit_code` (замокать `GitCommandError`), дерево грязное staged; `plan_rerun(--continue)` должен ПРОЙТИ гейт и довести publish (commit_code no-op/докоммит → push → PR). Проверить, что fresh-rerun с грязным деревом ВНЕ publish по-прежнему отвергается (регресс на существующее поведение). Все режимы (`--continue`, fresh, `--force-reset-remote`) покрыть.
- **Docs/config:** обновить описание recovery в docs (rerun --continue теперь проходит при незакоммиченной code-работе в publish-регионе).
- **Критерий готовности:** после симулированного publish-commit-фейла `worc rerun <id> --continue -y` доводит задачу до PR **без ручного commit-обхода**.
- **Примечание (смежное):** на revived-задаче finalize-supervisor-turn падает мгновенно (durable-сессия недоступна на rerun) → summary деградирует в fallback. Это отдельное качество recovery; зафиксировать как low-follow-up, не блокирует F14.

### F13 — publish git-операции не имеют bounded-retry (общий пробел) 🟡 MEDIUM

- **Проблема:** publish-git-операции (`commit_code`/`commit_audit`/`push`) не обёрнуты в retry, в отличие от провайдер-ошибок (`TRANSIENT_RETRYABLE`, config v20). Изначальная транзиентная трактовка F13 **отменена** (первопричина p0-02/03 — детерминированный F15), но наблюдение «publish не ретраится» остаётся валидным как defense-in-depth (реальные транзиенты: контенция `.git/index.lock`, сетевые сбои push).
- **Рычаг:** [providers/base.py:67-72](../../src/wastech_orchestrator/providers/base.py#L67-L72) (`TRANSIENT_RETRYABLE`), RetryConfig [config/schema.py:186-201](../../src/wastech_orchestrator/config/schema.py#L186-L201); publish-git-операции в [git_manager.py:672-709](../../src/wastech_orchestrator/git_manager.py#L672-L709) (`_commit`) и push-путь.
- **Изменение:** после F18/F14 — обернуть push (и опционально commit) в bounded-retry с коротким backoff, **только для истинно транзиентных git-фейлов** (не для детерминированных pathspec-ошибок — те должны падать явно, чтобы F12 их показал). Переиспользовать retry-механику провайдеров или локальный guard в `_commit`/push. Осторожно: не маскировать детерминированные баги ретраями (иначе регресс к F13-заблуждению).
- **Тесты:** push падает транзиентно 1 раз → ретрай → успех; push падает детерминированно → без бесконечных ретраев, явный фейл с stderr (F12).
- **Docs/config:** если вводится отдельный knob (напр. `publish.retry`) — schema-версия +1, обновить `config.example.yaml`/`config_writer`/docs. Оценить, нужен ли knob или переиспользовать существующий RetryConfig.
- **Критерий готовности:** одноразовый транзиентный push-сбой самолечится; детерминированный — падает громко. **Приоритет ниже F18/F14** — делать после них (их фикс убирает основной триггер).

## S2. supervisor / finalize (structured-turn)

Общий шов — finalize-turn под структурной схемой `_finalize_schema` и извлечение structured-output. F7a (config-обход `reasoning: high`) закрыт; здесь — worc-side устойчивость и качество вывода.

### F16 — сырой structured-output утекает в `summary.md` / тело PR 🟠 HIGH

- **Проблема:** на p0-03 модель выдала не чистый tool-call, а текст с псевдо-тегами `<summary>…</summary><follow_ups>[JSON]</follow_ups><memory_delta>[JSON]</memory_delta><lessons>[JSON]</lessons>` — и весь дамп (machine-артефакты) уехал ДОСЛОВНО в `summary.md` (= тело PR). Плюс рассинхрон: на `rerun` finalize упал и `_write_summary_json` перезаписал `summary.json` ПУСТЫМ, а `summary.md` не тронул. Эмиссия недетерминирована (p0-01 чисто).
- **Рычаг:** [core/supervisor.py:381-420](../../src/wastech_orchestrator/core/supervisor.py#L381-L420) (`finalize` builder), [core/supervisor.py:742-775](../../src/wastech_orchestrator/core/supervisor.py#L742-L775) (`_write_summary_json`), путь извлечения structured-output [providers/_adapter_base.py:380-424](../../src/wastech_orchestrator/providers/_adapter_base.py#L380-L424) (`structured_output` на line 76).
- **Изменение:** (a) в `summary.md` писать ТОЛЬКО поле `summary` (проза); `follow_ups` рендерить отдельной секцией (как в p0-01); `memory_delta`/`lessons` — НИКОГДА в summary.md, только в тиры памяти. Если `summary` сам содержит теги `</summary>`/`<memory_delta>`/`<lessons>` — санитизировать (обрезать по первому такому тегу) или отклонить turn с ретраем. (b) `finalize()` при failed-finalize (в т.ч. на rerun) НЕ должен перезаписывать существующий непустой `summary.json` пустышкой — симметрично тому, что он не трогает `summary.md` (guard: писать json только при успешном structured-output).
- **Тесты:** unit — structured-output с псевдо-тегами в `summary` → `summary.md` содержит только чистую прозу, теги/JSON вырезаны; `memory_delta`/`lessons` в summary.md отсутствуют. Unit — failed-finalize при существующем непустом `summary.json` → json не затирается пустым. Синхрон `.md`/`.json`.
- **Docs/config:** обновить описание finalize/summary в docs (контракт «summary.md = только проза + follow-ups секция»).
- **Критерий готовности:** тело PR никогда не содержит сырых `<memory_delta>`/`<lessons>`/`<follow_ups>`-дампов; `.md` и `.json` не расходятся при неудачном rerun-finalize.

### F7b — структурный finalize-turn хрупок к высокому reasoning (worc-side robustness) 🟠 HIGH

- **Проблема:** при `reasoning: xhigh` provider не смог выдать валидный structured-output под схему `{summary, follow_ups}` за N ретраев (`error_max_structured_output_retries`), тогда как free-text observe-turn'ы (без схемы, тот же xhigh) проходили. Каскад: пустой summary + потерянные follow_ups + пустая память. F7a (обход `reasoning: high`) закрыт, но **код остаётся хрупким** — дефолт не должен быть ломким.
- **Рычаг:** [core/supervisor.py:145-161](../../src/wastech_orchestrator/core/supervisor.py#L145-L161) (`_finalize_schema`), finalize turn [core/supervisor.py:381-420](../../src/wastech_orchestrator/core/supervisor.py#L381-L420), извлечение structured-output/классификация failure_subtype [providers/_adapter_base.py:380-424](../../src/wastech_orchestrator/providers/_adapter_base.py#L380-L424) (найти, где рождается `error_max_structured_output_retries` — вероятно из терминального события провайдера, line 397-401).
- **Изменение:** сделать схемный finalize-turn устойчивым к высокому reasoning: варианты (не взаимоисключающие) — (1) извлекать structured-output, игнорируя `thinking`-блоки (парсить tool-call даже при обильном thinking); (2) при `error_max_structured_output_retries` — один ретрай со **сниженным** reasoning (xhigh→high) специально для схемных turn'ов; (3) кэпать reasoning для схемных turn'ов на уровне оркестратора (target-only summary не нуждается в max-reasoning). Скоординировать с F16 (санитизация того же вывода).
- **Тесты:** сложно без реального провайдера — использовать fake-CLI (skill `/fake-cli`): фикстура, где провайдер отдаёт «прозу+обрезанный JSON» на первой попытке под схемой, затем валидный на пониженном reasoning → finalize восстанавливается (summary+follow_ups+memory_delta непусты). Классификация `error_max_structured_output_retries` покрыта.
- **Docs/config:** описать в docs устойчивость схемного finalize (что происходит при провале structured-output). Связь с F2 (дефолтный supervisor.reasoning не должен быть хрупким).
- **Критерий готовности:** finalize под `xhigh` не теряет summary/follow_ups/memory_delta детерминированно (либо восстанавливается, либо деградирует управляемо с логом), без тихого пропадания value.

### F10 — структурный finalize сплющивает summary в абзац-простыню, нет H1 🟡 MEDIUM

- **Проблема:** при включённых memory + `emit_follow_ups` finalize идёт под структурной схемой, где `summary` = `{"type":"string","minLength":1}` **без `description`/guidance** → модель кладёт весь синтез в один плоский JSON-string (0 `\n`), а билдер не префиксит `# {task_title}` → безголовый документ с подзаголовками «вверх ногами». Воспроизведено на p0-02 и p0-04.
- **Рычаг:** [core/supervisor.py:145-161](../../src/wastech_orchestrator/core/supervisor.py#L145-L161) (`_finalize_schema` — `summary` без description), [core/supervisor.py:381-420](../../src/wastech_orchestrator/core/supervisor.py#L381-L420) (`finalize()` builder, тело summary.md), [core/supervisor.py:657-679](../../src/wastech_orchestrator/core/supervisor.py#L657-L679) (`_finalize_prompt` — где добавляется `## Task under review`).
- **Изменение:** (a) добавить `description` полю `summary` в `_finalize_schema` (напр. «Markdown: короткая строка-заголовок + 2–4 подсекции с переносами строк»); (c) детерминированно префиксить `# {task_title}` в `summary.md` в `finalize()`, чтобы документ не был безголовым. Часть (b) — flow-промпт — вынесена в Раздел B (per-repo).
- **Тесты:** unit — `_finalize_schema` содержит `description` у `summary`; `finalize()` префиксит `# <title>` (документ начинается с H1, подсекции под ним). Snapshot/парс summary.md.
- **Docs/config:** обновить описание формата summary/PR-body в docs.
- **Критерий готовности:** тело PR открывается H1-заголовком; проза структурирована (не одна простыня). **Приоритет ниже F16/F7b** (те про потерю/утечку данных; F10 — про читаемость).

## S3. memory

### F9 — audit-строки памяти пишутся с пустым `rationale` 🟡 MEDIUM

- **Проблема:** после успешного finalize `.worc/memory/audit/log.jsonl` содержит строки с `rationale: ""` — включая `action: quarantine` (оператор через `worc memory show/validate` не видит, ПОЧЕМУ хорошо-evidenced кандидаты удержаны). Хэш-цепочка цела, но человекочитаемая причина отсутствует — частичный промах AC-SF3.
- **Рычаг:** [memory/service.py:466-478](../../src/wastech_orchestrator/memory/service.py#L466-L478) (`_put_pending`), [memory/service.py:334-354](../../src/wastech_orchestrator/memory/service.py#L334-L354) (`append`), [memory/service.py:517-532](../../src/wastech_orchestrator/memory/service.py#L517-L532) (`_replace_rows`) — `AuditContext.rationale` не заполняется; [memory/service.py:163-187](../../src/wastech_orchestrator/memory/service.py#L163-L187) (`_ingest_lesson`, line ~177 `rationale=cand.rationale`); источник — [memory/delta.py:36-45](../../src/wastech_orchestrator/memory/delta.py#L36-L45) (`CandidateLesson.rationale: str|None=None`) и [memory/delta.py:233-251](../../src/wastech_orchestrator/memory/delta.py#L233-L251) (`_parse_lesson`, line ~247 `rationale=_opt_str(raw,"rationale")`, приходит None).
- **Изменение:** (1) заполнять `AuditContext.rationale` конкретной причиной мутации на каждом шве: для quarantine — «quarantined: non-durable trust `agent-inferred`»; для append/replace — краткая причина операции. (2) чтобы кандидаты несли rationale: либо finalize-схема/промпт супервизора обязывают поле `rationale` в кандидате (schema `required`), либо при отсутствии — детерминированно синтезировать причину из решения trust/staleness. Минимум — детерминированная causa для quarantine (не зависит от промпта).
- **Тесты:** unit — после quarantine audit-строка несёт непустой rationale с причиной; после append/replace — тоже. Хэш-цепочка остаётся целостной (регресс). AC-SF3 расширить.
- **Docs/config:** сверить с обещанием docstring [memory/audit.py](../../src/wastech_orchestrator/memory/audit.py) («строка несёт rationale») и разделом памяти в docs.
- **Критерий готовности:** каждая мутация памяти даёт audit-строку с pre/post-хэшами **и** непустым rationale (полный AC-SF3). Через `worc memory show/validate` видно, почему кандидат заквантинен.

## S4. install / config-дефолты

Три находки — одна правка [install/config_writer.py](../../src/wastech_orchestrator/install/config_writer.py) `build_config_mapping` (строки 89–195) + точечно схема. Делать вместе, один коммит. Сверить с [packaged/config.example.yaml](../../packaged/config.example.yaml) и [docs/configuration.md](../../docs/configuration.md) — устранить рассинхрон writer↔example.

### F1 — `skills.dynamic` должен по умолчанию быть `false` при установке 🟡 MEDIUM

- **Проблема:** install пишет `skills: {dynamic: true, strict: false}`; динамический слой добавляет once-per-task supervisor-turn даже когда репо-скиллов нет — лишняя стоимость/шум. Безопаснее fail-quiet: оператор осознанно включает `dynamic: true`.
- **Рычаг:** [install/config_writer.py:177-179](../../src/wastech_orchestrator/install/config_writer.py#L177-L179) (`dynamic: True`); дефолт dataclass [config/schema.py:356-370](../../src/wastech_orchestrator/config/schema.py#L356-L370) (`SkillsConfig.dynamic: bool = True`).
- **Изменение:** writer пишет `dynamic: false`. Решить про dataclass-дефолт: по паттерну memory-подсистемы (memory.enabled — дефолт False как absent-block fallback, install пишет true) здесь симметрично — **дефолт dataclass оставить False (fail-quiet при отсутствии блока), install писать false явно.** Проверить, не сломает ли смена dataclass-дефолта тесты/conftest (как было с memory.enabled) — если сломает, менять только writer.
- **Тесты:** тест `build_config_mapping` — блок skills содержит `dynamic: false`. Прогнать существующие install/skills-тесты.
- **Docs/config:** сверить с `config.example.yaml` (там значение должно совпасть) и docs skills. Оценить config-version note.
- **Критерий готовности:** свежий `worc install` пишет `skills.dynamic: false`; авто-предложение скиллов не запускается без явного opt-in.

### F2 — `supervisor.model`/`reasoning` должны заполняться конкретными значениями 🟡 MEDIUM

- **Проблема:** install пишет `supervisor: {model: null, reasoning: null}` — «унаследовать от primary» неявно/непрозрачно; оператор не видит, какой моделью/усилием работает надзорный слой (а он пишет summary/follow-ups/memory-delta/advisory — цена реальная). Связь с F7b: дефолт не должен быть хрупким (xhigh ломал finalize).
- **Рычаг:** [install/config_writer.py:181-184](../../src/wastech_orchestrator/install/config_writer.py#L181-L184) (`model: None, reasoning: None`); [config/schema.py:373-388](../../src/wastech_orchestrator/config/schema.py#L373-L388) (`SupervisorConfig.model/reasoning = None`).
- **Изменение:** writer резолвит конкретные значения: `model` = модель primary-провайдера, `reasoning` = осознанный дефолт. **Важно (F7b):** дефолтный reasoning НЕ должен быть `xhigh` (ломает схемный finalize) — брать `high` или `medium`. Dataclass-дефолт `None` оставить как absent-block fallback («унаследовать»); заполняет именно writer.
- **Тесты:** тест `build_config_mapping` — блок supervisor имеет непустые `model`/`reasoning`, `reasoning` не `xhigh`. Резолв primary-модели покрыт.
- **Docs/config:** сверить с `config.example.yaml` и разделом supervisor в docs.
- **Критерий готовности:** свежий install пишет видимые конкретные `supervisor.model`/`reasoning`; поведение надзорного слоя прозрачно и не хрупко из коробки.

### F3 — `telegram.trace` отсутствует в конфиге, который пишет install 🟡 MEDIUM

- **Проблема:** install пишет блок `telegram` без ключа `trace` (реальный задокументированный knob, schema v21) — оператор не знает о фиче. Доставляемый конфиг должен быть полным self-документирующим срезом.
- **Рычаг:** [install/config_writer.py:171-176](../../src/wastech_orchestrator/install/config_writer.py#L171-L176) (блок telegram без `trace`); схема уже имеет [config/schema.py:345-352](../../src/wastech_orchestrator/config/schema.py#L345-L352) (`TelegramConfig.trace: bool = False`, line 352) — **schema-изменение не требуется**, только writer.
- **Изменение:** добавить `trace: false` в блок telegram в `build_config_mapping`.
- **Тесты:** тест `build_config_mapping` — блок telegram содержит `trace: false`.
- **Docs/config:** сверить с `config.example.yaml` (ключ, вероятно, уже там — устранить рассинхрон writer↔example) и docs telegram.
- **Критерий готовности:** свежий install пишет `telegram.trace: false` как часть блока; оператор видит knob без чтения исходников.

## S5. CLI — `worc list`

### F4 — `worc list --format ids` игнорирует секционные флаги (только state.db) 🟡 MEDIUM

- **Проблема:** `worc list --pending --format ids` печатает пусто, хотя table-вид показывает 10 задач. `--format ids` читает только `store.all_tasks()` (state.db) и игнорирует `--pending`/pending-файлы на диске; свежие pending ещё не имеют строк в БД. Флаги молча не композируются.
- **Рычаг:** [cli.py:2742-2769](../../src/wastech_orchestrator/cli.py#L2742-L2769) (`cmd_list` — диспатч format/scope), [cli.py:2722-2739](../../src/wastech_orchestrator/cli.py#L2722-L2739) (`_list_ids` — DB-only), [cli.py:2697-2719](../../src/wastech_orchestrator/cli.py#L2697-L2719) (`_list_sections` — диск+БД), [cli.py:1006-1021](../../src/wastech_orchestrator/cli.py#L1006-L1021) (`scan_pending_sorted`).
- **Изменение:** когда секционный флаг (`--pending`/`--recent`/`--all`) задан ВМЕСТЕ с `--format ids`, брать id из того же источника, что и table (`_list_sections`/`scan_pending_sorted`), а не только из БД. Т.е. `_list_ids` должен уметь принимать секционный источник. Как минимум (если решим не расширять) — явно задокументировать/предупредить, что `--format ids` DB-only и не композируется. Рекомендация — **сделать композицию** (машиночитаемые id очереди pending нужны для скриптинга).
- **Тесты:** unit — pending-задачи на диске без строк в БД: `list --pending --format ids` печатает их id; `--recent`/`--all --format ids` согласованы с table. Регресс на чистый `--format ids` (без секции) — DB-derived как раньше.
- **Docs/config:** обновить docs CLI `worc list` (композиция секций с `--format ids`).
- **Критерий готовности:** `worc list --pending --format ids` печатает id очереди pending, согласованно с table-видом.

## S6. validation gate

### F5a — injection-скан отклоняет бэктики в display-поле `title`; консоль не показывает detail 🟡 MEDIUM

- **Проблема:** задача с `title` содержащим `` `code` `` отклоняется гейтом `injection_suspected` (скан всех front-matter значений на `INJECTION_SUBSTRINGS`), хотя docstring модуля признаёт `title` free-text «cannot designate a path» (контент доходит до провайдеров только как пути — structural guarantee), т.е. бэктик в title безвреден для argv. Плюс на консоли виден только `reason`, без `detail` (поле/причина — только в JSON-артефакте).
- **Рычаг:** [security/injection.py:34](../../src/wastech_orchestrator/security/injection.py#L34) (`INJECTION_SUBSTRINGS`), [security/injection.py:58-80](../../src/wastech_orchestrator/security/injection.py#L58-L80) (`scan_value`), [security/injection.py:44-46](../../src/wastech_orchestrator/security/injection.py#L44-L46) (`InjectionFinding.detail`), [task/validation_gate.py:244](../../src/wastech_orchestrator/task/validation_gate.py#L244) (`_rej(INJECTION_SUSPECTED, finding.detail)` — detail уже прокидывается в reason, но на консоли не отображается).
- **Изменение:** (a) исключить provably-non-argv display-поля (`title`, `contacts`) из подстрочного argv-скана (оставив reject для полей, которые могли бы попасть в argv/путь), ЛИБО — если решим не ослаблять — явно задокументировать «front-matter значения — только простой текст». **Рекомендация: (a) с явным allowlist'ом display-полей** (title/contacts), т.к. structural guarantee их обезвреживает. (b) surface `finding.detail` в консольной строке reject (не только `reason`) — оператору видно поле+причину без лезть в JSON.
- **Тесты:** unit — `title` с бэктиком/`;`/`|` проходит gate (не reject); поле, реально попадающее в argv/путь, с теми же символами — по-прежнему reject. Consumer-тест — консольный вывод reject содержит detail. Осторожно: не ослабить защиту для path-полей (security-инвариант).
- **Docs/config:** обновить docs validation/security (какие поля display-only и почему исключены из argv-скана).
- **Критерий готовности:** естественный markdown-заголовок с бэктиками не уходит тихо в rejected; при любом reject оператор видит поле+причину на консоли. **Требует security-ревью** (`/security-review`) перед мержем.

### F6 — задача, отклонённая на валидации, «занимает» свой id в ledger; rerun/status её не видят 🟡 MEDIUM

- **Проблема:** после gate-reject (до claim, без tasks-row) повторная подача того же id → `duplicate_task_id` (проверка против ledger, `_ledger_has_task_id`), но `worc status` → «no task found», а `rerun` (требует tasks-row/ветку) восстановить не может. Асимметрия: ledger достаточно чтобы **заблокировать**, но недостаточно чтобы **увидеть**. Штатный цикл «зареджектили → поправил → подал снова под тем же id» сломан.
- **Рычаг:** [task/validation_gate.py:228-232](../../src/wastech_orchestrator/task/validation_gate.py#L228-L232) (duplicate-check: `_store_has_task_id OR _ledger_has_task_id`), [task/validation_gate.py:112-123](../../src/wastech_orchestrator/task/validation_gate.py#L112-L123) (инъекция `_ledger_has_task_id`); `plan_rerun` [core/orchestrator.py:790](../../src/wastech_orchestrator/core/orchestrator.py#L790), `cmd_rerun` [cli.py:1314](../../src/wastech_orchestrator/cli.py#L1314).
- **Изменение (рекомендация — вариант (a) из находки):** НЕ считать дублем id, чей единственный ledger-след — gate-reject (никогда не был claimed, нет tasks-row): валидационный reject не должен навсегда резервировать id. Проверять: если ledger-запись для id имеет `final_status=failed` c `validation_reason` И нет tasks-row → разрешить повторную подачу. Альтернативы (b) научить rerun восстанавливать из ledger/pending-файла, (c) команда очистки одной ledger-записи — крупнее; (a) минимальна и чинит штатный цикл.
- **Тесты:** unit — задача reject'нута на gate (ledger-строка, нет tasks-row) → повторная подача исправленной под тем же id проходит (не duplicate). Регресс: реальный дубль (claimed, tasks-row есть) по-прежнему отклоняется.
- **Docs/config:** обновить docs validation/re-submit (поведение re-submit после gate-reject).
- **Критерий готовности:** «зареджектили на валидации → поправил → подал снова под тем же id» работает без смены id и ручной правки ledger.

## S7. watch / audit

### F11 — под watch merge-gated `pr_merge` audit-op не пишется никогда 🟡 MEDIUM

- **Проблема:** демон авто-продвигает зависимую задачу по ЖИВОЙ проверке PR (хороший UX), но `pr_merge`-op в `publish_operations` не пишет; а `worc prs --sync --yes` (единственный путь записать `pr_merge`) отказывает при живом демоне («stop it first»). Итог: audit-ledger merge-событий неполон для merge-gated задач. Прогрессия работает — страдает только полнота аудита.
- **Рычаг:** reconcile-шов демона в [core/orchestrator.py](../../src/wastech_orchestrator/core/orchestrator.py) / `recovery.py` (где определяется eligibility по live-проверке PR); гейт `running_daemon_pid` в `prs --sync` (в `cli.py`).
- **Изменение (рекомендация — вариант (a) из находки):** демон при обнаружении смердженного dep-PR сам пишет `pr_merge`-op (reconcile seam) — тогда audit полон без остановки демона. Альтернатива (b) — разрешить `prs --sync` в read-then-record форме, безопасной при живом демоне. Минимум — задокументировать, что в watch-driven flow `pr_merge` ops не пишутся. Рекомендация — **(a)**.
- **Тесты:** unit/интеграция — демон обнаружил смердженный dep-PR → `publish_operations` содержит `pr_merge`-op для той задачи. Идемпотентность (не двойная запись при повторном polling).
- **Docs/config:** обновить docs watch/merge-gated (кто пишет `pr_merge`).
- **Критерий готовности:** в чисто watch-driven merge-gated режиме `pr_merge` audit-op фиксируется без остановки демона. **Низкий приоритет** — функциональность не страдает, только полнота аудита.

---

# Раздел B — per-repo промпты/роли (чинятся в `wastech-mdlint`, не в этом репо)

Эти пункты — target-only файлы в `.worc/flows/` целевого репо (operator-authored), НЕ канонические packaged-узлы оркестратора. Они дополняют worc-side фиксы того же findings, но правятся в mdlint. Здесь — для полноты плана.

- **F5b (docstring/контракт):** если по F5a решим НЕ ослаблять argv-скан, а документировать — добавить в контракт авторинга задач правило «front-matter значения (`title` и др.) — только простой текст, без `` ` `` `;` `|` `$(`». (Смежно worc-side F5a.)
- **F10b (finalize-prompt):** в `.worc/flows/implementation/summary.md` (mdlint) явно велеть авторить `summary` как markdown с заголовком/секциями (дополняет worc-side F10 (a)/(c) — schema-description + H1-префикс).
- **F17a (documentation-роль):** в `.worc/flows/roles/documentation.md` (mdlint) добавить явный запрет запускать install/сборку/пакетные мутации и transient-эксперименты — доки read-only к коду и build-state. (На p0-04 doc-узел прогнал `workspace:*` + `npm install` + откат — scope-drift, integrity-риск, live network; вреда не было только потому, что модель сама аккуратно откатилась.)

## F17b — per-node network/write-policy (worc-side, defense-in-depth) 🟡 MEDIUM → отдельная оценка

- **Проблема:** worc полагается на prompt-adherence для скоупа узла, а не на capability-политику — у doc-agent есть Bash+сеть, ничто не мешает `npm install` (F17, p0-04).
- **Рычаг:** механика output/network policies из P3 уже существует (см. memory `[[flow-engine-p3-build]]`).
- **Изменение:** рассмотреть выдачу документационному узлу `network: none`, чтобы live-install был физически невозможен. **Это отдельная фича-оценка, не мелкий фикс** — вынести в отдельный ADR (`/adr`), если владелец захочет defense-in-depth поверх per-repo промпта F17a. В текущий remediation-план включён как пометка, не как обязательный шаг.

---

# Раздел C — уже закрытое и наблюдения (контекст, действий не требуют)

- **F15 — ✅ RESOLVED (live-validated p0-04):** `staged_pathspec` исключает `:(exclude){tasks_dir}/` только когда `tasks_dir` НЕ gitignored ([git_manager.py:623-651](../../src/wastech_orchestrator/git_manager.py#L623-L651), кэш `_tasks_dir_ignored`). Регресс-тест `test_commit_code_root_file_when_tasks_dir_gitignored` + unit `test_staged_pathspec_conditional_on_tasks_dir_ignore`. Коммит `1984180`. Отменяет транзиентную трактовку F13. **Первый триггер publish-кластера закрыт; F18 — второй триггер, см. S1.**
- **F7a — ✅ VERIFIED FIXED (config-обход):** `supervisor.reasoning: xhigh → high` подтверждён на прогоне 2 (finalize `succeeded`, structured_output полон, память записана). **Код остаётся хрупким — worc-side robustness = F7b (S2), OPEN.**
- **F8 — ⚪ NOTED (наблюдение, не баг):** Opus+xhigh везде дорог для тривиальных задач; per-node более дешёвые модели/reasoning (§12 override уже работает — наблюдён на p0-04) дали бы кратную экономию. Действие не требуется; связано с F2 (видимые supervisor-дефолты) и per-node override design `[[task-node-model-override-design]]`.

---

# Общие замечания по всем правкам

- **Инварианты:** ни одна правка не должна дать провайдеру право на git/commit/push/PR (только оркестратор); argv-без-shell; no-secrets в логах/артефактах (особо F12 — редактировать git-stderr перед записью); кросс-платформенность (F18/F14 — porcelain-парсинг и pathspec устойчивы к Windows-путям, `Path.as_posix()`).
- **config-version:** F1 меняет семантику дефолта — оценить, нужен ли version-note (по паттерну «tolerate+strip»). F3 — только writer (схема уже несёт `trace`). F13, если введёт knob, — schema +1. Остальные — без bump.
- **docs-sync:** каждая worc-side правка меняет поведение/CLI/config → обновить docs в том же изменении (`/sync-docs`), Stop docs-sync gate это проверит. Прогнать `/run-checks` (ruff + mypy + pytest) перед каждым коммитом.
- **security-ревью:** F5a требует `/security-review` (ослабление injection-скана) перед мержем.
- **Верификация:** после S1 (F18) — прогнать реальную relocate/rename-задачу (напр. p0-05 повторно или p0-07 CI-релокации) и подтвердить автономный publish, как это сделали для F15 на p0-04.

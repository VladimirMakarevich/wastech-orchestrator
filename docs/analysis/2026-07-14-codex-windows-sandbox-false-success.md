# Post-mortem: codex draft-нода, «ложный успех» на Windows-песочнице

**Задача:** `blog-happy-in-my-misfortunes-2` (ветка `docs/blog-happy-in-my-misfortunes-2`)
**Целевой репозиторий:** `C:\Users\Vladimir Makarevich\Obsidian\WastimeApp`
**Прогон:** run-000011 (stage `draft`, нода `1-codex`), плюс `fixing` run-000013 / run-000015
**Финальный статус:** `manual_action_required`, `fix_iterations: 2`, decomposition: нет
**Дата прогона:** 2026-07-14 18:58–19:14 (UTC+? локально; UTC 17:04–17:14)
**Анализ:** только чтение артефактов + исходников оркестратора. Правки не вносились.

---

## Вердикт (кратко)

Прогон провалился не из-за качества текста и не из-за промптов. Причина — инфраструктурная: на этом
Windows-хосте песочница codex (`--sandbox workspace-write`) не может запустить дочерний процесс —
**каждая** команда и `apply_patch` падают с `windows sandbox: CreateProcessWithLogonW failed: 2`
(и `1056`). Codex не смог ни прочитать исходники, ни записать файл — но завершился с `exit_code=0`,
и оркестратор засчитал ноду как `succeeded`.

Самое важное: **в оркестраторе уже есть защита ровно от этого случая** —
`_post_success_infra_error` в [`codex.py`](../../src/wastech_orchestrator/providers/codex.py#L422),
которая должна превратить «ложный успех» в инфраошибку и заставить Router переключиться на Claude.
Она не сработала только потому, что её регэксп ловит три конкретные строки
(`orchestrator_helper_launch_failed`, `codex-windows-sandbox-setup.exe`,
`setup refresh failed to launch helper`), а реальная ошибка выглядит иначе —
`CreateProcessWithLogonW failed`. **Единственная строчка-регэксп** отделяет провал от
самопочинки: при её расширении draft автоматически падает на Claude (который на этом хосте отработал
все ноды без единой ошибки), и задача доходит до конца.

Приоритет №1 — расширить сигнатуру в codex-адаптере (одно место, чинит и nonzero-exit-путь, и
post-success-гард). Всё остальное — следствия того же сбоя.

---

## Что произошло (таймлайн)

Из [`daemon.log`](../../../../Obsidian/WastimeApp/.worc/logs/daemon.log) (второй прогон) и
per-node артефактов:

| Нода | Провайдер | Итог по логу | Что произошло на самом деле |
|---|---|---|---|
| context | claude | ✅ 115.6s | Норма, `context.out.md` собран |
| supervisor | claude | ✅ | Норма |
| research | claude | ✅ 191.4s | Норма, `research.out.md` собран |
| supervisor | claude | ✅ | Норма |
| **draft** | **codex gpt-5.4 xhigh** | **✅ exit 0, 439s** | **Все чтения и запись упали в песочнице; файл не создан; статья ушла только в `last-message.txt`** |
| supervisor | claude | ✅ | Норма |
| **length** | check_length (tool) | **❌ fail** | Мерил **не те файлы** (см. F3) |
| **fixing** | **codex** | **✅ exit 0, 40s** | **Ничего не сделал: та же ошибка песочницы, попросил прислать файлы** |
| supervisor / length / **fixing** (run-15) | claude / codex | ✅ / ❌ / ✅ 26s | Второй фикс-цикл — снова вхолостую |
| supervisor ×N | claude | ✅ | Норма |
| terminal cleanup | — | → `manual_action_required` | Задача припаркована |

Ключевые доказательства:

- **Каждая** команда в
  [`draft/run-000011/1-codex/events.jsonl`](../../../../Obsidian/WastimeApp/.worc/logs/blog-happy-in-my-misfortunes-2/stages/draft/run-000011/1-codex/events.jsonl)
  падает: `execution error: Io(Custom { kind: Other, error: "windows sandbox: CreateProcessWithLogonW failed: 2" })`.
  Codex перебрал обходные пути (MCP-ресурсы, `Get-Location`, `Get-Date`, ~9 web-поисков), затем сдался
  и **вложил всю статью в финальное сообщение**.
- Запись тоже упала (stderr.log:22):
  `apply_patch verification failed: Failed to read …blog-happy-in-my-misfortunes.md: fs sandbox helper failed with status exit code: 1: windows sandbox failed: CreateProcessWithLogonW failed: 2`.
- [`result.json`](../../../../Obsidian/WastimeApp/.worc/logs/blog-happy-in-my-misfortunes-2/stages/draft/run-000011/1-codex/result.json):
  `"status": "succeeded"`, `"exit_code": 0`, `"error": null`.
- [`current.diff`](../../../../Obsidian/WastimeApp/.worc/logs/blog-happy-in-my-misfortunes-2/current.diff)
  — **пустой**: за весь прогон на диск не легло ничего.
- Тот же самый сбой воспроизвёлся идентично ~17 часов раньше в первом прогоне
  `blog-happy-in-my-misfortunes` (daemon.log 01:29–01:42, тоже `manual_action_required`,
  `fix_iterations: 2`) — отсюда «опять» в постановке задачи.

---

## Находки (по убыванию влияния)

### F1 — «Ложный успех» codex не распознаётся → нет fallover → фантомный черновик
- **Категория:** infra / providers · **Severity:** высокая · **Confidence:** высокая (подтверждено кодом + логами)
- **Симптом:** codex вышел с `exit 0`, оркестратор поверил успеху, хотя 100% инструментов упали и файл не создан. Fallback на Claude (`draft primary=codex fallback=claude`) не сработал, потому что переключение происходит **только** по поднятой инфра-`ProviderError`, а её не было.
- **Доказательства:**
  - Гард уже существует: [`codex.py:422-430`](../../src/wastech_orchestrator/providers/codex.py#L422) `_post_success_infra_error`, вызывается на success-пути в [`_adapter_base.py:457-467`](../../src/wastech_orchestrator/providers/_adapter_base.py#L457).
  - Он читает `proc.stderr_text`, а нужная строка **именно там** ([`stderr.log:2-22`](../../../../Obsidian/WastimeApp/.worc/logs/blog-happy-in-my-misfortunes-2/stages/draft/run-000011/1-codex/stderr.log)): `ERROR codex_core::exec: exec error: windows sandbox: CreateProcessWithLogonW failed: 2`.
  - Но регэксп [`_HELPER_LAUNCH_FAILED_PATTERN` (codex.py:86-91)](../../src/wastech_orchestrator/providers/codex.py#L86) матчит только `orchestrator_helper_launch_failed | codex-windows-sandbox-setup\.exe | setup refresh failed to launch helper`. Ни один вариант не совпадает с `CreateProcessWithLogonW failed` / `fs sandbox helper failed` / `windows sandbox failed`.
- **Корневая причина:** незакрытая сигнатура stderr. Гард спроектирован верно, но не знает про этот конкретный текст ошибки runtime-хелпера.
- **Рычаг (точечно, одно место):** расширить `_HELPER_LAUNCH_FAILED_PATTERN` в [`codex.py`](../../src/wastech_orchestrator/providers/codex.py#L86). Константа переиспользуется и в PERMISSION_DENIED-сигнатуре (codex.py:144), и в post-success-гарде (codex.py:426) — одна правка чинит оба пути. Предлагаемое дополнение:
  ```python
  _HELPER_LAUNCH_FAILED_PATTERN = (
      r"orchestrator_helper_launch_failed"
      r"|codex-windows-sandbox-setup\.exe"
      r"|setup refresh failed to launch helper"
      r"|CreateProcessWithLogonW failed"       # runtime-сбой seclogon-запуска дочернего процесса
      r"|fs sandbox helper failed"              # тот же сбой на пути apply_patch (запись)
      r"|windows sandbox(?: failed)?:"          # общий префикс сообщений песочницы codex
  )
  ```
  Добавить регресс-тест: терминальный `success` + такой stderr → `ProviderError(PERMISSION_DENIED)`.
- **Scope:** дефолт оркестратора (любой репозиторий/хост).
- **Ожидаемый эффект:** draft автоматически переключается на Claude (см. Router: `permission_denied` — fallback-eligible, а профили draft у codex и claude оба `workspace-write` → same-or-stricter → переключение разрешено, [`router.py:50,60-74`](../../src/wastech_orchestrator/routing/router.py#L60)). Claude в этом прогоне отработал все свои ноды без ошибок и использует собственные файловые инструменты, а не OS-песочницу codex, — так что запись черновика ожидаемо проходит. Фантомные «успехи», холостые фикс-циклы и ложный отчёт length-гейта исчезают как класс.

> Примечание: рядом есть более общий предохранитель `_produced_no_work` → `AGENT_NO_PROGRESS`
> ([`_adapter_base.py:442-455`](../../src/wastech_orchestrator/providers/_adapter_base.py#L442)), но
> он не ловит этот случай: codex **сгенерировал** много токенов (18 725 output — вся статья ушла в
> сообщение), поэтому «работа не выполнена» по метрике токенов = false. Единственный надёжный сигнал
> «workspace не тронут» — это как раз stderr песочницы, ради чего post-success-гард и существует.

### F2 — Хостовый дефект: песочница codex не запускает дочерний процесс
- **Категория:** infra / окружение (не код оркестратора) · **Severity:** высокая · **Confidence:** высокая по симптому, средняя по точной Win32-причине
- **Симптом:** `CreateProcessWithLogonW failed: 2` (ERROR_FILE_NOT_FOUND) и `1056` на всех вызовах shell и на `apply_patch`. Затрагивает только codex; Claude не использует этот механизм.
- **Корневая причина (наиболее вероятная):** `CreateProcessWithLogonW` (служба Secondary Logon / `seclogon`) запускает дочерний процесс под альтернативным токеном. Два кандидата, оба про окружение:
  1. дочерний shell — это **WindowsApps-алиас** `pwsh.exe` (`…\Microsoft\WindowsApps\pwsh.exe` виден в командах в events.jsonl); execution-alias из `WindowsApps` — это reparse-заглушка, которая не резолвится под вторичным logon-токеном → ERROR_FILE_NOT_FOUND (2);
  2. состояние службы Secondary Logon (`seclogon`) — если она отключена/в нестандартном состоянии, вызов падает (первый параллельный вызов дал `1056`).
- **Важно:** preflight-проверка хелпера [`_windows_sandbox_helper_error` (codex.py:432)](../../src/wastech_orchestrator/providers/codex.py#L432) прошла — сам `codex-windows-sandbox-setup.exe` **обнаружим**. Проблема не в «не нашли хелпер», а в том, что хелпер в рантайме не может выполнить `CreateProcessWithLogonW`. Поэтому preflight не спас.
- **Рычаги (хостовые, вне кода оркестратора):**
  1. Проверить службу **Secondary Logon** (`seclogon`): `Get-Service seclogon` → `Startup=Manual/Automatic`, запущена.
  2. Настроить codex на реальный бинарь оболочки вместо WindowsApps-алиаса (например, `C:\Program Files\PowerShell\7\pwsh.exe` или `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`) — через конфиг codex/переменные окружения профиля запуска.
  3. Переустановить/обновить standalone-пакет codex (его `codex-resources` + sandbox-helper).
  4. Как обходной путь на этом хосте — до устранения — держать авторские ноды (`draft`/`polish`/`fixing`) на Claude (см. F-note ниже).
- **Scope:** этот хост.
- **Ожидаемый эффект:** восстанавливает codex. Но при исправленном F1 конвейер устойчив, даже если F2 повторится: сбой честно приводит к fallover, а не к «успеху».

### F3 — Холостой фикс-цикл + ложный вердикт length-гейта
- **Категория:** checks / flow · **Severity:** средняя · **Confidence:** высокая
- **Симптом:** после фантомного draft `length`-гейт упал, показав нарушения на **посторонних** файлах, и запустились два бессмысленных фикс-прохода codex.
- **Доказательства:**
  - [`length/run-000012/stdout.txt`](../../../../Obsidian/WastimeApp/.worc/logs/blog-happy-in-my-misfortunes-2/stages/length/run-000012/stdout.txt): измерены `blog/20260206-моя-история-(EN).md` (4649, ok), `_wastime-app/_idea-app.md` (1062 — fail), `.rules/signature-phrases.md` (2198 — fail). **Целевого файла среди них нет.**
  - `fixing` [run-000013 result.json](../../../../Obsidian/WastimeApp/.worc/logs/blog-happy-in-my-misfortunes-2/stages/fixing/run-000013/1-codex/result.json) и run-000015 last-message: агент прямо пишет, что не может ничего прочитать/поправить, и просит прислать файлы. Оба — `"status": "succeeded"`, каждый сжёг ~630K input и ~20K output + ~12.5K reasoning токенов **вхолостую**.
- **Корневая причина:** каскад от F1 (нет черновика → `current.diff` пуст). Скрипт [`.worc/tools/check_length`](../../../../Obsidian/WastimeApp/.worc/tools/check_length) резолвит scope «diff-first, затем task-fallback»: при пустом diff срабатывает `_named_in_task`, который меряет **все .md, упомянутые в тексте задачи** (там как раз фигурируют idea-app и signature-phrases как справочные файлы). Отсюда «не те файлы».
- **Рычаги:**
  - Основной — F1 (нет фантомного draft → у гейта есть реальная цель, diff-путь отработает корректно).
  - Вторичное усиление в целевом `.worc/tools/check_length` (`_named_in_task`): сузить fallback до **декларированного файла-результата** задачи, а не любых цитируемых .md; либо возвращать «vacuous pass», если задекларированный deliverable не найден на диске (тогда гейт не падает на справочных файлах). Замаскировано в здоровых прогонах — проявляется только когда запись провалилась.
- **Scope:** целевой репозиторий (это операторский tool, не код оркестратора).
- **Ожидаемый эффект:** нет вводящих в заблуждение падений гейта и нет холостого расхода фикс-бюджета/токенов.

### F4 — Draft пишет по неверному целевому пути (роль впрыскивает `{task_path}` вместо «пиши, куда говорит задача»)
- **Категория:** prompt / flow · **Severity:** средняя · **Confidence:** высокая
- **Симптом:** роль велит писать статью в **сам файл задачи**, а не в задекларированный deliverable.
- **Доказательства:**
  - Роль [`.worc/flows/blog_article/draft.md:1`](../../../../Obsidian/WastimeApp/.worc/flows/blog_article/draft.md#L1): «…as a new Markdown file at the target path the task names (`{task_path}`)».
  - В [`rendered-prompt.md:1`](../../../../Obsidian/WastimeApp/.worc/logs/blog-happy-in-my-misfortunes-2/stages/draft/run-000011/rendered-prompt.md) `{task_path}` = `tasks/pending/blog-happy-in-my-misfortunes.md`.
  - А [`task.normalized.json`](../../../../Obsidian/WastimeApp/.worc/logs/blog-happy-in-my-misfortunes-2/task.normalized.json) в acceptance criteria требует `blog/20260714-happy-in-my-misfortunes-(EN).md` и «Create only the new article file under `blog/`; do not touch existing posts».
- **Корневая причина (важно, не «агент не умеет в пути»):**
  - Набор переменных пути ([`context_paths.py:30-37`](../../src/wastech_orchestrator/core/flow/context_paths.py#L30)): `{repo}`, `{task_path}`, `{plan_path}`, `{diff_path}`, `{checks_path}`, `{review_path}`. **Переменной «путь к результату» нет.** `{task_path}` — это путь файла-задачи, а не статьи. Автор роли за неимением нужной переменной подставил `{task_path}` и подписал её «the target path the task names» — что фактически неверно: реальный путь (`blog/…`) лежит в теле задачи, которое агент читает. Агенту дали конфликтующие сигналы.
  - **Контракт между узлами — это git diff, а не заранее объявленный путь.** Code-флоу ([`implementation.md:1`](../../src/wastech_orchestrator/packaged/flows/implementation/implementation.md#L1)) вообще не впрыскивает путь: агент сам решает, что создать/поменять (хоть 50 файлов), downstream видит результат через `{diff_path}`. Блог-флоу тоже уже работает так: `check_length` — diff-first, `polish` — «Edit the article in place», `fixing` — по diff/той же сессии. Значит впрыснутый `{task_path}` не только неверный, но и **лишний**.
  - «Не те файлы» в length-гейте (F3) случились только потому, что draft не записал ничего (diff пустой). При рабочей записи diff подхватил бы файл независимо от пути.
- **Рычаг (НЕ хардкод пути):** сделать draft как implementation — **убрать впрыск `{task_path}` и велеть агенту записать статью туда, куда указывает тело задачи**; diff донесёт результат до length/tone/polish/publish. Масштабируется на любое число файлов, убирает конфликт в промпте. Вторично и по желанию: если нужен машинно-проверяемый путь результата до записи — завести настоящую переменную `{deliverable_path}` из объявленного в задаче пути (более тяжёлая правка `context_paths.py` + рендер; вероятно, YAGNI, раз diff уже решает задачу).
- **Scope:** целевой flow-роль `draft.md` (правка текста); при желании ввести `{deliverable_path}` — и оркестратор.
- **Ожидаемый эффект:** статья попадает туда, где её ждут задача и diff-контракт; никакого пути на задачу не хардкодится; работает и для многофайловых результатов.

### F5 — Оффлайн-нода всё равно вышла в интернет через хостовый web_search codex
- **Категория:** security / isolation · **Severity:** средняя · **Confidence:** высокая
- **Симптом:** нода `draft` помечена `network_access: false` («every writing node stays offline» в шапке флоу), но codex выполнил **9 web-поисков**, включая попытки вытащить приватные файлы репозитория из веба: `site:github.com WastimeApp "_idea-app.md"`, `"Vladimir Makarevich" Wastime` (events.jsonl, items 16-24).
- **Доказательства:**
  - `build_codex_argv` добавляет сетевой флаг только при `request.network_access` ([`codex.py:255-259`](../../src/wastech_orchestrator/providers/codex.py#L255)) — то есть `network_access` управляет **сетью песочницы** (`sandbox_workspace_write.network_access`), а это про локальный shell.
  - Хостовый `web_search` исполняется на стороне бэкенда OpenAI и обходит ограничение песочницы. Grep по исходникам оркестратора: `web_search` не упоминается **нигде** (0 совпадений) — инструмент никак не учтён.
- **Корневая причина:** серверный инструмент codex не покрывается локальным сетевым тумблером песочницы; флоу считает ноду оффлайновой, а она не оффлайновая.
- **Рычаг:** в codex-адаптере отключать хостовые web-инструменты для нод с `network_access=false` (проверить соответствующий ключ конфигурации codex `exec`), либо явно учесть это в security-профиле/preflight и в описании флоу. Отдельно: при исправленном F1 draft вообще прервался бы до веб-поисков.
- **Scope:** дефолт оркестратора.
- **Ожидаемый эффект:** оффлайн-ноды действительно оффлайн; приватный контекст не ищется во внешнем вебе.

---

## Пробелы в данных

- `prompt_audit: true` — аудит промптов **включён**, полные rendered-промпты доступны (это плюс, не пробел).
- Первый прогон (`blog-happy-in-my-misfortunes`, без `-2`) детально не перечитывался — вывод об идентичности сделан по `completed.jsonl` + `daemon.log` (тот же `manual_action_required`, те же `fix_iterations: 2`, тот же паттерн draft-codex «succeeded»).
- Точная Win32-причина `CreateProcessWithLogonW failed: 2/1056` по логам однозначно не выводится — нужна проверка на хосте (состояние службы `seclogon`; какой именно бинарь оболочки использует codex). В F2 приведены наиболее вероятные причины.
- Наличие у codex `exec` конфиг-ключа для отключения хостового `web_search` не проверялось по самому CLI — рычаг в F5 сформулирован как «проверить/выяснить», а не как готовый флаг.

## Что уже сделано хорошо (это проверено)

- **Архитектура fallover — правильная.** Router переключается только по инфра-`ProviderError`, `permission_denied` — fallback-eligible, у `draft` уже прописан `fallback=claude`, профили совпадают. Поэтому исправление F1 — это расширение одной сигнатуры, а не переделка.
- **Гард под этот класс сбоя уже есть** (`_post_success_infra_error`), как и preflight-проверка хелпера, общий no-work-предохранитель и rate-limit-fallover. Каркас на месте — не хватает одной строки в регэкспе.
- **Claude-ноды отработали безупречно** (context, research, все supervisor-проходы — exit 0, реальные артефакты).
- **Качество промпта/роли draft — высокое** (чёткий бриф, tone-of-voice, запреты AI-паттернов, опора на scout/research). Провал не про промпт.
- **prompt_audit включён**, `logging.level: debug`, `artifacts: full` — диагностика была полной.

---

## Рекомендованный порядок действий

1. **F1** (оркестратор, 1 место): расширить `_HELPER_LAUNCH_FAILED_PATTERN` + регресс-тест. Это чинит устойчивость на всех хостах и делает F2 не-фатальным.
2. **F2** (хост): включить/проверить `seclogon`; увести codex с WindowsApps-алиаса оболочки на реальный `pwsh.exe`/`powershell.exe`; при необходимости переустановить пакет codex. До устранения — можно временно держать авторские ноды на Claude.
3. **F4** (целевой flow-роль `draft.md`): убрать впрыск `{task_path}`, велеть агенту писать по пути из тела задачи (как implementation) — diff донесёт результат дальше.
4. **F3** (целевой tool): сузить fallback `_named_in_task` в `check_length` до задекларированного deliverable.
5. **F5** (оркестратор): закрыть хостовый `web_search` для оффлайн-нод либо явно учесть его в изоляции.

*Готов оформить эти пункты в `docs/backlog/follow_ups.md` и/или внести любую отдельную правку по запросу.*

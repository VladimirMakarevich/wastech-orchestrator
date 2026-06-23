# ADR — Проверки в монорепе: удаление discovery + `command_sets`

Статус: **РЕАЛИЗОВАНО (2026-06-23).** Первый заход (Р1–Р5 + Р3a) выполнен полностью; suite зелёный (ruff/mypy/pytest). Дата: 2026-06-23 Владелец: Vladimir Makarevich

Решение по тому, как `checks` работают в полиглот-монорепе (например, `.NET`-бэкенд в `backend/` + Angular-фронтенд в `mobile/` + прочие проекты), и что делать с подсистемой автоматического обнаружения проверок (`checks.discovery`).

## Контекст и проблема

Подсистема `checks.discovery` (инспектор → детектор → агент-фолбэк → пробер → резолвер) спроектирована **под один корень репозитория** и для монорепы непригодна по трём причинам, подтверждённым в коде:

- **Инспектор читает только корень.** [`checks/inspect.py`](../../src/wastech_orchestrator/checks/inspect.py) проверяет `(self._root / name).is_file()` без рекурсии и без понятия «подпроект». Манифесты в `backend/src/` и `mobile/` не видны; в корне монорепы их нет.
- **.NET не поддержан вообще.** Ни в маркерах/фингерпринте ([`checks/fingerprint.py`](../../src/wastech_orchestrator/checks/fingerprint.py)), ни в детекторе ([`checks/detect.py`](../../src/wastech_orchestrator/checks/detect.py)) нет `.sln`/`.csproj`/`dotnet`.
- **У команды нет рабочей директории.** `CheckCommandSpec` — это `argv + name` ([`config/schema.py`](../../src/wastech_orchestrator/config/schema.py)); раннер всегда запускает `cwd=clone_dir` ([`check_runner.py`](../../src/wastech_orchestrator/check_runner.py)). `npm test` физически выполнится в корне и упадёт.

Дополнительно discovery несёт риск «гейт переписывает сам себя» и поэтому тащит защитный обвес (re-resolve только по infra-proof, `approve_command_changes`, mutation-guard) — сложность ради фичи, которая в целевом сценарии не работает. Проект greenfield, нигде не задеплоен — совместимость не ограничивает (см. [follow_ups.md](follow_ups.md) и историю в backlog).

## Решение

### Р1. Удалить `checks.discovery` целиком; единственное поведение — «оператор перечисляет команды»

Режимы `auto`/`deterministic`/`disabled` и весь агент-ассист удаляются. Остаётся одно поведение, эквивалентное прежнему `configured`: **гейт = то, что оператор перечислил в config; пустой список = гейта нет** (профиль `ready`, всё проходит — текущая семантика пустого `configured`). Режим `disabled` отдельно не нужен — он схлопывается в «пустой список команд».

Удаляется: `checks/{inspect,detect,probe,agent,fingerprint,schema_validate}.py`, режимы и кэш-резолв в `checks/resolver.py`, блок `CheckDiscoveryConfig` и enum'ы режимов в `config/schema.py`, поле `ChecksNode.discovery` + `ChecksDiscovery` и их ветка в валидаторе, `propose_default_commands` (автозасев `init`), путь `check_reresolve` в оркестраторе и хук re-resolve в checks-узле.

**Что НЕ удаляется** (это свойства самого гейта, а не discovery): argv-без-shell, env-allowlist, обязательный timeout, launch-vs-quality split, mutation-guard. Остаются ровно как есть.

Хвост: `wastech-orchestrator init` больше не автозасевает команды — сеет закомментированный плейсхолдер, оператор заполняет руками. Приемлемо.

### Р2. Монорепа = именованные наборы команд (`command_sets`) с `cwd`, на уровне config

Власть над тем, «что значит прошло», остаётся у оператора в `config.yaml` — это сохраняет инвариант ceiling: **flow никогда не задаёт команды проверок** ([`core/flow/nodes/checks.py`](../../src/wastech_orchestrator/core/flow/nodes/checks.py): «The flow never supplies commands»). Flow не трогаем.

```yaml
checks:
  command_sets:
    backend:
      paths: ["backend/**"] # отбор по diff (Р3): набор гоняется, если изменён матчащий путь
      commands:
        - { name: backend-tests, argv: [dotnet, test], cwd: backend/src }
    frontend:
      paths: ["mobile/**"]
      commands:
        - { name: fe-lint, argv: [npm, run, lint], cwd: mobile }
        - { name: fe-tests, argv: [npm, test], cwd: mobile }
    ios:
      paths: ["ios/**"]
      timeout_seconds: 2400 # Р3a: перекрывает глобальный (xcodebuild — десятки минут)
      skip_if_unavailable: true # Р4: нет xcodebuild на хосте → skip с warning, не падение
      commands:
        - {
            name: ios-tests,
            argv: [xcodebuild, test, "-scheme", App],
            cwd: ios,
          }
  timeout_seconds: 7200 # глобальный дефолт пер-команда; перекрывается per-set
```

Новое поле `cwd` (repo-relative, валидируется от traversal — переиспользовать санитайзер `_safe_scope_paths` из бывшего `inspect.py`); раннер запускает каждую проверку с `cwd = clone_dir / check.cwd`. Это единственное реальное изменение в исполнении.

Р2 один уже закрывает «как разделять / собирать / вызывать» в монорепе: разделение — именованные наборы; сборка — их объединение; вызов — каждый в своём `cwd`.

### Р3. Детерминированный отбор наборов по diff

«Какой поднабор гонять» решает **чистая детерминированная функция**, не LLM: `git diff --name-only <base>..HEAD` (база и клон у оркестратора есть — [`core/orchestrator.py`](../../src/wastech_orchestrator/core/orchestrator.py), `diff_path` уже считается) → префиксный/glob-матч изменённых путей против `paths` каждого набора → гоняется **объединение** совпавших наборов.

Консервативные дефолты (это и есть защита от транзитивной поломки, см. риски):

- изменены пути вне всех `paths` (корень, общие либы, codegen) → гоняем **все** наборы (fail-safe to full);
- у набора нет `paths` → гоняется всегда;
- фильтрация — **оптимизация**, честно меняющая полноту на скорость; по умолчанию консервативная.

### Р3a. Per-set timeout

`command_sets.<name>.timeout_seconds` перекрывает глобальный `checks.timeout_seconds`. Один порог не натянуть на гетерогенные тулчейны (`ruff` — секунды, `xcodebuild`/`gradle` — десятки минут). Дефолт — глобальное значение.

### Р4. Отсутствующий тулчейн: `skip_if_unavailable` (явное, узкое послабление)

На одном хосте все тулчейны (iOS=macOS+Xcode, Android=SDK, .NET=dotnet) держать нереально, поэтому нужен осознанный escape-hatch — иначе оркестратор непригоден для кросс-платформенной монорепы. Но **пропустить проверку ≠ проверка прошла**: наивный флаг даёт «тихий зелёный» (тронул `ios/`, нет Xcode, проверки молча скипнуты, PR зелёный, сломанный iOS уехал) — ровно тот провал, от которого защищается вся подсистема. Поэтому послабление сделано так:

- **Per-set opt-in, а не глобальный рубильник.** `command_sets.<name>.skip_if_unavailable: true` (дефолт `false` = fail-closed). Глобальное «пропускай всё, чего не нашёл» замаскировало бы и случайную поломку (забыли поставить `pytest` → Python молча скипнулся). Per-set — осознанное операторское «iOS необязателен на хосте без Xcode».
- **Доступность определяется детерминированно**: `shutil.which(argv[0])` перед запуском. Нет бинаря И набор `skip_if_unavailable` → **skip**; нет бинаря И набор обязательный → **launch-failure → manual** (текущее поведение).
- **Skip громкий, никогда не `passed`.** Пишется как `skipped (toolchain absent)` в `check_runs` и node-outcome, попадает в summary/PR.
- **«Всё пропущено» → эскалация, не авто-pass.** Если для затронутых путей не отработала **ни одна** проверка (всё скипнуто) → гейт не отработал вообще → `manual` (не отдаём зелёным непроверенный код). И при `git.auto_merge` любой skip = неполный гейт → авто-мердж не выполняется, отдаём человеку.

### Р5. Прогон всех проверок (без fail-fast) + агрегированный исход

Раннер меняется со «стоп на первом падении» ([`check_runner.py`](../../src/wastech_orchestrator/check_runner.py)) на «прогнать всё, агрегировать»: полная картина для человека и — бонус — `fixing` видит все падения сразу и чинит за один цикл, а не whack-a-mole (упал backend → почини → реран → упал android → …).

Прецеденс исхода при смешанных результатах в одном прогоне:

- собрать по каждой проверке `{passed | failed | launch_failed | skipped}`;
- любые **quality-fail** → один агрегированный лог → `fixing`;
- любой **обязательный** тулчейн отсутствовал (гейт неполный) → исход `manual`, **даже если что-то прошло** — на частичном гейте код не отдаём; quality-результаты при этом всё равно показываем человеку.

## Отвергнутые альтернативы

| Вариант | Почему отвергнут |
| --- | --- |
| **A. Команды inline в flow YAML** (свой набор в check-узле каждого `<project>-<type>.yaml`) | Ломает инвариант ceiling (автор flow начинает определять «что значит прошло»); N типов × M проектов = N×M дублей списка команд (правка lint → во всех файлах); комбинаторный взрыв `task_type` (реестр делает плоский lookup по имени файла). |
| **B. Агент фильтрует проверки перед запуском** | «Какие файлы изменились» + «какому проекту принадлежит путь» — детерминированная функция (`git diff` + префикс). LLM здесь добавляет стоимость, задержку, недетерминизм и **дыру «тихого пропуска»**: галлюцинация уронит нужную проверку → регрессия уедет зелёной. Это ровно тот риск «LLM решает, что значит прошло», от которого подсистема и защищается. |
| **C. Фильтрация через supervisor** | [`core/supervisor.py`](../../src/wastech_orchestrator/core/supervisor.py) **advisory by construction**: «never reworks, reopens, or routes», провайдер форсится `read-only`, оркестратор «persists it but never consumes it to route». Гейтить/фильтровать он по контракту не может; правка сломала бы его главный инвариант и снова посадила LLM в путь решения. |

## Риски и допущения

- **Транзитивная поломка между проектами** (Р3): изменение в backend ломает frontend через общий контракт/codegen, а frontend-проверки пропущены, т.к. «менялся только backend». Митигируется консервативным дефолтом «пути вне наборов → гоняем всё» и тем, что Р3 — опциональная оптимизация. Корректность маппинга `paths` — на операторе.
- **Граница доверия `.worc/flows/`** (подтверждена и обязана сохраниться): flow авторит только оператор; кодящий агент работает в отдельном клоне `./workspace/repo` и до `.worc/` (рантайм-дом оркестратора на хосте) не дотягивается. Инвариант «команды задаёт config, не flow» (Р2) держится именно на этом — но даже при inline-варианте безопасность опиралась бы на ту же границу.
- **Кросс-проектные задачи**: модель Р2/Р3 рассчитана на то, что наборы перечислены глобально и гоняются вместе (Р2) или по diff (Р3) — задача может затрагивать несколько проектов без выбора «одного flow на проект». Отдельный `task_type` на проект не требуется.
- **Бамп версии config**: удаление `discovery` + добавление `command_sets`/`cwd` — изменение формата → `schema_version` 14 → 15.

## Touchpoints

- [`config/schema.py`](../../src/wastech_orchestrator/config/schema.py) — `ChecksConfig`: добавить `command_sets` (набор = `paths` + `timeout_seconds?` + `skip_if_unavailable?` + `commands[]`); `CheckCommandSpec`: добавить `cwd`; удалить `CheckDiscoveryConfig`/`CheckDiscoveryMode`/`CheckRefreshPolicy`.
- [`config/`](../../src/wastech_orchestrator/config/) загрузчик + `schema_version` 15 + `templates/config.example.yaml` и корневой `config.example.yaml`.
- [`check_runner.py`](../../src/wastech_orchestrator/check_runner.py) — `cwd` на проверку; (Р3) выбор наборов по diff; (Р3a) per-set timeout; (Р4) `which(argv[0])`-проба + skip vs launch-fail; (Р5) прогон-всего вместо стопа на первом падении.
- [`checks/model.py`](../../src/wastech_orchestrator/checks/model.py) — `ResolvedCheck` несёт `cwd`/`skip_if_unavailable`/per-set timeout; нормализация наборов. `CheckRunResult`/`CheckOutcome`: статус `skipped` + агрегированный исход (quality-fail → fixing; обязательный тулчейн отсутствует или всё скипнуто → manual).
- [`checks/resolver.py`](../../src/wastech_orchestrator/checks/resolver.py) — свести к тривиальному «нормализуй command_sets»; удалить режимы/кэш/фингерпринт/агент-путь.
- Удалить: `checks/{inspect,detect,probe,agent,fingerprint,schema_validate}.py` и их тесты; `discovery_factory.py`.
- [`core/flow/schema.py`](../../src/wastech_orchestrator/core/flow/schema.py) + [`core/flow/validator.py`](../../src/wastech_orchestrator/core/flow/validator.py) — удалить `ChecksDiscovery`/`ChecksNode.discovery` и ветку валидации.
- [`core/flow/nodes/checks.py`](../../src/wastech_orchestrator/core/flow/nodes/checks.py) — убрать re-resolve хук; обновить текст инварианта (команды по-прежнему из config, теперь как наборы; выбор набора в Р3); обработка агрегированного исхода (Р5): `manual` при неполном гейте / «всё скипнуто» (Р4); skip-результаты в `check_runs` и summary.
- [`core/orchestrator.py`](../../src/wastech_orchestrator/core/orchestrator.py) — убрать `resolve`/`reresolve`/`check_reresolve`; (Р3) прокинуть changed-paths в раннер; (Р4) при любом skip не выполнять `git.auto_merge` (неполный гейт → человек).
- `cli.py` — убрать автозасев команд в `init`.
- Документация: `docs/configuration.md`, `docs/functional/`, `docs/cookbook.md`, follow_ups.

## Масштабирование на N гетерогенных проектов

Стресс-проверка: монорепа из 5 проектов — `.NET` (backend), Angular (web), Python (`ml`), iOS, Android.

- **Конфиг и отбор растут сложением, O(проектов), не O(проектов × типов задач).** Каждый проект = один набор; diff-отбор (Р3) гоняет объединение затронутых наборов. Ни правок flow, ни роста `task_type`. Это и есть главная причина выбрать `command_sets` вместо «команд во flow».
- **Настоящее узкое место — тулчейны на одном хосте, а не подсистема checks.** Оркестратор однохостовый: проверки запускаются локальным subprocess'ом в клоне ([`check_runner.py`](../../src/wastech_orchestrator/check_runner.py)), удалённого CI-делегирования нет. Чтобы гонять проверки всех проектов, на хосте должны быть тулчейны (iOS ⇒ macOS + Xcode; Android ⇒ SDK/JDK; .NET ⇒ dotnet SDK). По умолчанию отсутствие бинаря — это **launch failure** → задача встаёт в manual (fail-closed). **Р4 (`skip_if_unavailable`)** даёт оператору узкий escape-hatch на конкретный набор (нет Xcode на хосте → iOS-набор скипается с warning, не валит задачу), а **Р3** дополнительно избавляет от попыток (Python-задача не дёрнет `xcodebuild`). Делегирование сборки во внешний CI/раннеры — отдельный backlog-пункт, ортогональный этому ADR.
- **Решённые в этом ADR боли масштаба**: грубый единый timeout → per-set `timeout_seconds` (Р3a); fail-fast скрывает падения соседних наборов → прогон-всего + агрегат (Р5); жёсткий стоп на отсутствующем тулчейне → `skip_if_unavailable` (Р4).
- **Оставлено на потом** (отдельные пункты): exclude-globs (правка `ios/docs/**` не должна запускать сборку iOS); per-command timeout (сейчас гранулярность — набор); делегирование сборки во внешний CI.

## Фазировка

Первый заход — Р1 (удаление discovery) + Р2 (`command_sets` + `cwd`) + Р3 (отбор по diff) + Р3a (per-set timeout) + Р4 (`skip_if_unavailable`) + Р5 (прогон-всего + агрегат): монорепа-проверки работают, гоняются только для затронутых проектов, дают полную картину падений и не валят задачу из-за отсутствующего на хосте тулчейна. Оставлено на потом отдельными пунктами: exclude-globs, per-command timeout, делегирование сборки во внешний CI.

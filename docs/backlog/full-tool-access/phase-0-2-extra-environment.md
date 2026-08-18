# Фаза 0.2 — `security.extra_environment`

Status: **implemented 2026-08-18** (AC0.2.1–AC0.2.6, AC0.5.1, AC0.6.1–AC0.6.2 зелёные; AC0.2.7 — живая проба **не доказана**, запись в разделе поправок [README.md](README.md)) Date: 2026-08-13 Owner: Vladimir Makarevich Требования: Т0.2, Т0.5 и Т0.6 из [requirements-step-0.md](requirements-step-0.md) `schema_version`: 36 (фаза поехала первой из тех, что меняют формат) Зависимости: нет, но осмысленна после [0.1](phase-0-1-allowed-environment.md)

**Поправка к пункту 6 при реализации.** Фраза для гайда записана здесь как «значение печатается в конфиге **и в аудите прогона**» — вторая половина неверна и противоречит Т0.2.6/Н0.8 того же контракта: окружение не пишется ни в один артефакт попытки, и фаза этого не меняет. В шипнутых доках сказано только то, что верно: значение лежит открытым текстом в `config.yaml`, а `worc preflight` печатает **имена**. Не «починить» обратно.

Несущая фаза Шага 0. Форвардинг (`allowed_environment`) даёт только **имя**: значение приходит из окружения оператора, нигде не зафиксировано и на другой машине другое, а забытый `export` молча пропускается — `build_child_env` отбрасывает имя, отсутствующее у родителя ([`env.py:101`](../../../src/wastech_orchestrator/security/env.py)). Для корней тулчейнов и путей кэша нужен второй механизм: не «пробросить», а «присвоить».

```yaml
security:
  extra_environment: # значения задаются явно, а не наследуются из окружения
    DOTNET_CLI_TELEMETRY_OPTOUT: "1"
    DOTNET_NOLOGO: "1"
    NUGET_PACKAGES: "/Users/u/repos/target/.toolcache/nuget"
    LANG: "C.UTF-8"
```

## Что делаем

**1. Схема и загрузчик.** Поле в `SecurityConfig` ([`schema.py:390`](../../../src/wastech_orchestrator/config/schema.py)), дефолт — пустой маппинг. В `_build_security` ([`loader.py:542`](../../../src/wastech_orchestrator/config/loader.py)) имя ключа добавляется в набор `_check_keys` — загрузчик fail-closed, и без этого ключ будет отвергнут как неизвестный. Значения обязаны быть строками: `DOTNET_NOLOGO: 1` — ошибка с подсказкой закавычить, не молчаливое приведение.

**2. Построение окружения.** Правится `build_child_env` ([`env.py:89`](../../../src/wastech_orchestrator/security/env.py)). **Решение владельца 2026-08-13: функция принимает `SecurityConfig` целиком** вместо списка имён — тогда call-site физически не может построить env без extras. `security/` уже импортирует `config.schema` в соседнем модуле ([`isolation.py`](../../../src/wastech_orchestrator/security/isolation.py)), а `config.schema` ничего из `security/` не тянет ([`schema.py:14`](../../../src/wastech_orchestrator/config/schema.py) — единственный внутренний импорт там `providers.base`), так что цикла не возникает и контракты import-linter не затрагиваются. Вариант «обязательный второй позиционный параметр» рассмотрен и отклонён: он всё ещё позволяет новому call-site'у передать пустой маппинг.

Все шесть call-site'ов правятся в этом же изменении: [`check_runner.py:140`](../../../src/wastech_orchestrator/check_runner.py), [`git_manager.py:583`](../../../src/wastech_orchestrator/git_manager.py), [`orchestrator.py:2307`](../../../src/wastech_orchestrator/core/orchestrator.py) (сканеры `dependency_scan`), [`codex.py:713`](../../../src/wastech_orchestrator/providers/codex.py), [`_adapter_base.py:351`](../../../src/wastech_orchestrator/providers/_adapter_base.py) и [`_adapter_base.py:485`](../../../src/wastech_orchestrator/providers/_adapter_base.py). Хук `_augment_child_env` для доставки не используется: по контракту он меняет значения уже разрешённых имён и не добавляет новых ([`_adapter_base.py:296`](../../../src/wastech_orchestrator/providers/_adapter_base.py)).

**3. Валидация.** В `_validate_security` ([`validation.py:326`](../../../src/wastech_orchestrator/config/validation.py)): `PATH` в любом регистре запрещён как ключ; секретное имя запрещено — через `is_sensitive_key` ([`redaction.py:117`](../../../src/wastech_orchestrator/providers/redaction.py)), без своего списка масок; имя обязано соответствовать `[A-Za-z_][A-Za-z0-9_]*`; два имени, различающиеся только регистром, — ошибка. Пустая строка как значение **разрешена**.

**3a. Пин локали в Git Manager (Т0.6).** Git Manager классифицирует результаты по английским строкам в выводе — повтор `push` по `_TRANSIENT_GIT_STDERR_MARKERS` ([`git_manager.py:66`](../../../src/wastech_orchestrator/git_manager.py)), `leftover conflict marker` ([`git_manager.py:1731`](../../../src/wastech_orchestrator/git_manager.py)), `_ALREADY_MERGED_MARKERS` ([`git_manager.py:2077`](../../../src/wastech_orchestrator/git_manager.py)) — а git локализован, поэтому `LANG: "ru_RU.UTF-8"` в новом ключе сделал бы временный сбой сети окончательным отказом задачи, молча. **Решение владельца 2026-08-13: Git Manager пиннит `LC_ALL=C` для своих процессов.** Новой машинерии не нужно: `_GIT_HARDENING_ENV` ([`git_manager.py:143`](../../../src/wastech_orchestrator/git_manager.py)) уже накладывается поверх `build_child_env` и уже покрывает и `git`, и `gh` — пин добавляется туда одной строкой и по построению не перекрывается конфигом. Обязательная страховка: тесты на кириллическое коммит-сообщение и кириллическое имя файла; если `LC_ALL=C` даст регресс на не-ASCII, пин сужается до `LC_MESSAGES=C` и отдельно проверяется на Windows.

**4. Раздача и видимость.** `CONFIG_SCHEMA_VERSION` +1; ключ в [`config.example.yaml`](../../../src/wastech_orchestrator/packaged/config.example.yaml) (пустым, с объяснением) — оттуда его подсеет `upgrade-config`; ключ в [`config_writer.py`](../../../src/wastech_orchestrator/install/config_writer.py), чтобы свежая установка о нём знала. При непустом ключе `worc preflight` печатает строку с **именами** — значения не печатаются.

**5. Что фаза сознательно НЕ делает** (решения владельца 2026-08-13): не печатает **значения** в preflight — только имена; не пишет эффективное окружение в артефакты попытки (сегодня его там нет — в артефакте запроса только `argv`, промпт и пути, [`_adapter_base.py:710`](../../../src/wastech_orchestrator/providers/_adapter_base.py) — и Шаг 0 это не меняет); не добавляет значения ключа в литералы редакции; не учит ключ удалять унаследованные переменные.

**6. Доки.** Таблица ключей и абзац replace-not-extend в [`guide/config/reference.md`](../../../src/wastech_orchestrator/packaged/guide/config/reference.md) (новый ключ **не** replace-not-extend — он вообще не имеет дефолта, это стоит сказать явно), [`guide/config/best-practices.md`](../../../src/wastech_orchestrator/packaged/guide/config/best-practices.md) и [`guide/config/README.md`](../../../src/wastech_orchestrator/packaged/guide/config/README.md) — там уже стоит «имена, а не значения», и туда добавляется отдельная фраза: значение из этого ключа **печатается** в конфиге и в аудите прогона, поэтому креденшелы в него не заносятся ни при каких условиях.

## Тесты

- По одному тесту на каждый из шести call-site'ов: заданная переменная доходит до процесса. Удаление доставки в любом из них обязано ронять ровно один тест — это и есть проверка Т0.2.2.
- Паритет Т0.5: окружение агентской попытки и окружение набора checks равны при одном конфиге.
- Приоритет: одноимённая переменная из `extra_environment` побеждает проброшенную.
- Отказы: `PATH`/`path`/`Path`, секретное имя, имя не по грамматике, пара имён с разным регистром, не-строковое значение. Пустое значение — **не** отказ.
- Т0.6: при `LANG: "ru_RU.UTF-8"` в конфиге процессы Git Manager всё равно получают `LC_ALL=C`, и классификация transient-ошибок срабатывает; коммит с кириллическим сообщением и файл с кириллическим именем проходят весь путь без изменений.
- И-5: конфиг без ключа даёт словарь, равный сегодняшнему, включая порядок.
- И-4: набор литералов редакции при непустом `extra_environment` не меньше, чем при пустом (`secret_env_values` продолжает получать имена `allowed_environment`, а не объединение — иначе расширение сузило бы вычистку).
- `tests/config/test_upgrade.py` — `upgrade-config` печатает `+ security.extra_environment` и не трогает остальное; `tests/config/test_config_schema_version.py` — bump.

## Живая проба (часть DoD)

Реальный прогон на тестовой задаче с непустым `extra_environment`: узел печатает своё окружение, набор checks печатает своё, значения совпадают и равны заданным. Это доказывает то, чего не докажет fake-CLI, — что паритет держится на живом пути, а не только в юнит-харнессе.

## Риск и откат

Главный риск — частичная доставка: агент видит переменную, а Check Runner нет, и задача падает на гейте качества **после** успешной работы агента. Он снимается формой сигнатуры (Т0.2.2), а не тестами: тесты ловят сегодняшние шесть мест, тип ловит седьмое, которое кто-нибудь добавит завтра. Второй риск — секрет в значении: технически он не ловится ничем, поэтому закрывается прямой фразой в гайде и запретом секретных **имён**. Третий — тот, ради которого фаза несёт Т0.6: переменная, безобидная для агента, ломает разбор вывода на пути публикации. Откат — пустой ключ (поведение возвращается к сегодняшнему), конфиг при этом остаётся валидным; пин `LC_ALL=C` откату не подлежит и остаётся в любом случае — он чинит зависимость, которая существовала и до этой фазы.

## DoD

AC0.2.1–AC0.2.7, AC0.5.1 и AC0.6.1–AC0.6.2 зелёные; полный набор проверок зелёный; доки ветки и шаблоны обновлены тем же изменением; `install` и `upgrade-config` дают ключ; doc-impact строка в PR.

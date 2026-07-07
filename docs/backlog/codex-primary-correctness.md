# Codex-primary correctness: resume-argv contract + supervisor provider/model consistency

Status: **implemented** (2026-07-07, branch `feat/codex-primary-correctness`) Date: 2026-07-07 Owner: Vladimir Makarevich

Две связанные корректностные проблемы, вскрытые первым прогоном с codex как глобальным `primary` (задача `p5-01-classify-nodes`, отчёт `docs/analysis/p5-01-classify-nodes-run-analysis.md`, находки F38/F39 в `TEST-FINDINGS.md`). Обе делают режим «codex-primary» скрытно неисправным: работа, которая должна идти на codex, молча уезжает на claude-fallback, а при единственном разрешённом провайдере codex — падает совсем. ADR фиксирует решение по обеим.

## The problem

**F38 (HIGH) — codex `exec resume` собирается с флагами, которые codex 0.142.5 отвергает.** Адаптер строит resume-инвокацию как `codex --ask-for-approval never exec resume <SESSION_ID> --cd <dir> --sandbox <mode> --json --output-last-message <file> --model <m> -c model_reasoning_effort=...`. В codex 0.142.5 грамматика — `codex exec [OPTIONS] resume [SESSION_ID] [PROMPT]`: `--cd`/`--sandbox`/`--json`/`--output-last-message` суть опции **родительского `codex exec`** и должны стоять **до** подкоманды `resume`; сама `resume` принимает лишь `-c/--config`, `-m/--model`, `--image`, `--last`, `--ephemeral` и т.п. Первый же дописанный после `resume <ID>` флаг (`--cd`) отвергается парсером (`error: unexpected argument '--cd' found`, exit 2). Итог: **все resume-узлы падают на codex** — supervisor-оверсайт (на каждом шаге), documentation, а при rework и fixing — и держатся только на claude-fallback. При `agents.allowed: [codex]` (нет fallback) — hard-fail на каждом resume. Fresh-сессии codex (planning, implementation) не затронуты — там нет подкоманды `resume`. Комментарий в коде утверждает «verified on codex-cli 0.139.0» — допущение о грамматике устарело к 0.142.5.

**F39 (MEDIUM) — supervisor уводит claude-модель на codex.** Блок `supervisor` в config.yaml несёт `model: claude-opus-4-8`, `reasoning: high`, но **без `provider`**. При глобальном `primary=codex` резолвинг наследует provider=codex и отправляет `--model claude-opus-4-8` на codex. Сейчас это замаскировано тем, что resume-инвокация supervisor падает раньше на F38 (`--cd`), но после починки F38 supervisor-codex падал бы уже на невалидной модели. Для flow-узлов согласованность model↔provider ловит `validate_flow_against_config`; для supervisor-слоя такой проверки нет.

## Constraints

- **Ядро не знает синтаксиса CLI.** Вся провайдер-специфика argv живёт только в `providers/`; F38 чинится строго в `providers/codex.py` (сборка argv) и в codex-preflight-probe — ядро не трогаем.
- **Fail-fast только при отсутствии безопасного runtime-фолбэка.** По принципу из `docs/backlog/` (P4): делать проверку фатальной, лишь когда безопасного отката нет. Для version-guard это означает: несовместимость грамматики resume → **fatal**, если codex — единственный разрешённый провайдер (fallback'а нет); иначе — **warning** (claude подхватит, но оператор предупреждён о деградации).
- **Кросс-платформенность.** Изменение — только переупорядочивание списка аргументов; платформенных допущений не вносит.
- **Провайдеры не делают fallback и не меняют state-machine** — остаётся в силе; правка чисто в построении argv и в конфиг-валидации.

## Alternatives considered

| Вариант | Почему отклонён |
| --- | --- |
| **F38: только переупорядочить argv, без version-guard** | Чинит сегодняшнюю 0.142.x, но следующий апгрейд codex, снова сменивший грамматику, опять всплывёт молчаливым runtime-fallback'ом — той же тихой деградацией, что и сейчас. Нужен активный контроль контракта версии. |
| **F38: закрепить версию codex через `==`/probe без починки argv** | Не решает саму проблему — resume всё равно сломан на установленной 0.142.5. |
| **F39: инференс провайдера из вендора модели (`claude-*`→claude, `gpt-*`→codex)** | «Магия», хрупкая к именам моделей; неявно, тяжело отлаживать; расходится с явной flow-node-моделью, где provider задаётся прямо. |
| **F39: только fail-fast валидация, без поля `provider`** | Заставляет оператора вручную дописывать provider или менять модель; не даёт супервизору собственного явного провайдера, тогда как flow-узлы его имеют — асимметрия. |
| **Do nothing / жить на fallback** | Режим codex-primary остаётся фиктивным (оверсайт и documentation де-факто на claude), а single-provider=codex — неработоспособен. Прямо противоречит цели прогонов. |

## Decision

**F38.** Переупорядочить codex-argv так, чтобы exec-level опции (`--cd`, `--sandbox`, `--json`, `--output-last-message`, `--output-schema`, network `-c`) шли сразу после `exec` и **до** опциональной подкоманды `resume <SESSION_ID>`; resume-совместимые `--model` и `-c model_reasoning_effort` — после неё (в fresh-пути «после resume» = «после exec-опций», т.к. подкоманды нет). Дополнительно — расширить существующий codex capability-probe (`_preflight_capability_error`, `providers/codex.py:297-313`) проверкой грамматики `codex exec resume --help`: подтверждать, что resume существует и принимает ожидаемую форму; вердикт — fatal при отсутствии fallback-провайдера, иначе warning. Делаем так, потому что это чинит установленную 0.142.5 И ловит будущий дрейф грамматики на preflight, а не молчаливым runtime-fallback'ом; цена — чуть больше кода probe и одна дополнительная `--help`-инвокация на preflight.

**F39.** Дать `SupervisorConfig` собственное опциональное поле `provider` (по умолчанию — глобальный primary, как сейчас) и добавить валидацию согласованности model↔provider для supervisor-слоя, симметрично flow-узлам. При несовместимой паре (например claude-модель при codex-провайдере) — ошибка на preflight с внятным сообщением. Делаем так, потому что это явно, предсказуемо и убирает асимметрию с flow-узлами; цена — один новый конфиг-ключ и переиспользование существующей проверки совместимости.

## Open questions

- **Точный вердикт version-guard (F38):** подтвердить правило «fatal ⇔ нет fallback-провайдера, иначе warning». Нужно свериться, что у codex-preflight есть доступ к `agents.allowed`/resolved-fallback в точке probe, иначе вынести решение в общий preflight-слой, а не в сам адаптер (ядро не должно знать CLI, но знать «есть ли fallback» — может).
- **Форма probe (F38):** достаточно ли грепать `codex exec resume --help` на наличие подкоманды и `[SESSION_ID] [PROMPT]`, или проверять конкретные принимаемые опции. Склоняемся к лёгкому греп-контракту (как текущий `-c/--config` probe), а не к полному разбору.
- **Дефолт `supervisor.provider` в install (F39):** оставлять пустым (наследовать primary) или писать явный `claude` в свежесгенерированный config.yaml, раз supervisor.model по умолчанию claude-специфичен. Скорее явный `claude`, чтобы дефолтная установка была согласованной из коробки.

## Implementation notes

- **F38 argv:** `providers/codex.py:113-182` (`build_codex_argv`) — перенести блок exec-опций (`--cd`/`--sandbox`/`--json`/`--output-last-message`, `--output-schema`, network `-c`) выше `if request.session_id: argv += ["resume", session_id]`; `--model` и `-c model_reasoning_effort` оставить после. Тест: fake-CLI resume-сценарий (`/fake-cli`) на порядок argv — assert, что `--cd` предшествует `resume`.
- **F38 probe:** `providers/codex.py:297-313` (`_preflight_capability_error`) — добавить проверку `codex exec resume --help`; вердикт с учётом наличия fallback (возможно, вынести на общий preflight-слой, `security/isolation.py`/preflight-агрегатор, где известен `agents.allowed`).
- **F39:** `config/schema.py` (`SupervisorConfig`) — добавить `provider: ProviderId | None`; `config/loader.py` — парсинг; точка резолвинга supervisor-провайдера (где сейчас берётся глобальный primary) — учесть новое поле; валидацию совместимости переиспользовать из `validate_flow_against_config` (или общий хелпер `is_reasoning_supported`/model-vendor-check). `install/config_writer.py` + `packaged/config.example.yaml` — дефолтный `supervisor.provider`. Bump `CONFIG_SCHEMA_VERSION` (текущий 26) при добавлении ключа с tolerate+strip для отсутствия блока.
- **Проверка по существу:** после фикса перепрогнать `p5-01`-подобную задачу под codex-primary и убедиться, что supervisor и documentation реально выполняются на codex (`state.db provider_attempts` без `unsupported_version`, без fallback на claude).

## Implementation outcome (2026-07-07)

Реализовано на ветке `feat/codex-primary-correctness` (suite/ruff/mypy зелёные). Ключевые точки:

- **F38 argv** — блок exec-опций перенесён выше `resume`-вставки в `build_codex_argv` ([providers/codex.py](../../src/wastech_orchestrator/providers/codex.py)); `--model`/`-c model_reasoning_effort` — после. Тест порядка argv в `tests/providers/test_codex_command.py`.
- **F38 probe** — вынесен **отдельный** advisory-канал, а не расширение `_preflight_capability_error` (тот — безусловный hard-block): новый hook `_preflight_degraded_reasons` в базовом адаптере + поле `ProviderHealth.degraded_reasons`; `CodexProvider` грепает `codex exec resume --help` на `-m/--model` и `-c/--config`. Вердикт fatal⇔нет-fallback вынесен в `cli.run_preflight` (там виден `agents.allowed`), как и предполагала open question.
- **F39 — важное уточнение против буквы Decision:** проверки «model↔provider» в кодовой базе **нет нигде** (модель намеренно непрозрачна — `core/node_overrides.py`), а инференс вендора из имени модели сам ADR отверг в Alternatives. Поэтому реализована **истинно симметричная flow-узлам** валидация: `supervisor.provider ∈ agents.allowed` + reasoning-support через **резолвнутый** провайдер (`supervisor.provider` или primary). Model-vendor-check НЕ добавлялся (решение оператора). Пара «claude-модель + codex-провайдер» ловится не на preflight, а как runtime-ошибка провайдера с fallback — ровно как у flow-узлов. Дефолт install: `supervisor.provider = primary_pid` (согласован с `model`, который тоже = дефолт primary), а не хардкод `claude`.
- **Схема:** `CONFIG_SCHEMA_VERSION` 26→27 (аддитивный опциональный ключ; отсутствие блока — fail-open, `upgrade-config` дописывает из шаблона; strip не нужен).
- **Отложено (см. follow_ups):** exit-2 argparse-ошибки codex всё ещё классифицируются как `unsupported_version` (маскировка bad-argv) — теперь для resume-пути неактуально, но латентная ловушка; и живой перепрогон под codex-primary на реальном codex 0.142.5 (владелец).

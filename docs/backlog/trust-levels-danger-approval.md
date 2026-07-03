# Уровни доверия и approval-политика danger-действий (`trust_level`)

Status: **implemented** (2026-07-03, config v25) Date: 2026-07-03 Owner: Vladimir Makarevich

> **Resolution (as built).** Three decisions settled the open questions and one draft premise, deviating from this proposal on purpose: (1) **Two levels, not three** — `auto` gates nothing beyond the `protected_paths` floor, so `full` would be indistinguishable from `auto` (this doc's own YAGNI check) and was dropped; the allowlist is `strict`/`auto`, default `auto`. (2) **The gate never auto-proceeds.** The draft premise that the current gate "auto-resolves per `ask_timeout_s` → proceed" was wrong: today it **fails closed** (deny/timeout/no-notifier → `manual_action_required`), and it resolves instantly in autonomous mode (a `NullNotifier` `transport_error`, not an 8h wait). So `trust_level` changes only _which_ diffs raise a gate; a raised gate resolves exactly as before, and `protected_paths` behaves like any gated path w.r.t. timeout. No `notify/telegram.py` change was needed. (3) **Orthogonal to providers** — `trust_level` is purely a worc approval-policy knob; `permission_profile`/`--sandbox` are untouched. Implementation: `security.trust_level`/`protected_paths` (schema/loader/validation, config v25; `upgrade-config` strips the old key), per-task `trust_level` override, `evaluate_diff_gate` in [core/dangerous_diff.py](../../src/wastech_orchestrator/core/dangerous_diff.py) resolving `(trust_level, diff-shape, protected_paths)`.

Ввести операторский knob `trust_level` (approval-политика) с тремя уровнями — `strict` / `auto` / `full`, дефолт `auto` при установке — который двигает **порог подтверждения** для mid-task dangerous-diff гейта. Решение «спрашивать ли человека» принимает **детерминированный** движок (форма git-диффа), а не LLM-суждение супервизора. Уровень доверия меняет только порог approval'а — он НЕ трогает hard security-ceiling (env-allowlist, запрет `bypassPermissions`/`--dangerously-*`, `cwd`=корень клона). По образцу этой находки — из [TEST-FINDINGS.md](../../TEST-FINDINGS.md) (комментарий оператора по итогам p0-кампании) и связанных F17.

## Проблема

Сейчас dangerous-diff guard на `workspace-write` agent-узлах ([core/hitl.py](../../src/wastech_orchestrator/core/hitl.py), `_gate_dangerous_diff`) поднимает `approval`-interaction, когда в диффе есть удаления файлов или правки манифестов зависимостей. Но это ловит **обычную работу в любом репозитории**: старую логику удалили, новую добавили, файл пересоздали — никаких реально опасных действий там нет. Само название «dangerous» вводит в заблуждение. В autonomous гейт авто-резолвится по `telegram.ask_timeout_s` (дефолт 8h) — чистый шум и лишняя задержка; в attended — раздражает без пользы. Наблюдено на p0-05 (implementation поднял approval-гейт, авто-резолв за 7s). При этом обратной возможности — сказать «а вот эти пути спрашивай ВСЕГДА, даже при высоком доверии» — сегодня нет.

Ключевое наблюдение: внутрирепозиторные удаления/переносы **git-обратимы** (всегда можно откатить), поэтому спрашивать про них по умолчанию — избыточно. Реальная опасность — эффекты **вне** репо, но их этот гейт и так не видит (см. Constraints).

## Constraints

- **Ядро не знает синтаксис CLI.** Резолв уровня в провайдер-специфичные флаги (`--permission-mode`, `--sandbox`) живёт только в `providers/`; ядро оперирует abstract-политикой.
- **Hard-ceiling неотключаем никаким уровнем доверия.** `--dangerously-skip-permissions`/`bypassPermissions`/`--yolo` запрещены навсегда ([providers/claude.py](../../src/wastech_orchestrator/providers/claude.py#L190), `find_forbidden_args`); env-allowlist, `shell=False`, `cwd`=корень клона ([providers/process.py](../../src/wastech_orchestrator/providers/process.py)) остаются при любом `trust_level`. `full` = «не спрашивать в рамках потолка», а не «снять потолок».
- **Супервизор `advisory by construction`** ([core/supervisor.py](../../src/wastech_orchestrator/core/supervisor.py)) — никогда не reworks/reroutes/blocks, форсирован `read-only`. Danger-решение НЕ отдаём ему, чтобы не ломать этот инвариант.
- **Containment сегодня мягкий и этим ADR не меняется.** Запись ограничена только `cwd`=корень клона; агент через Bash с абсолютным путём технически может тронуть файлы вне репо, и git-дифф этого не видит — на всех уровнях, включая нынешний. У Codex `--sandbox workspace-write` частично закрывает это OS-sandbox'ом, у Claude — нет. Важно: dangerous-diff гейт **никогда** не покрывал out-of-repo эффекты, поэтому `trust_level` эту экспозицию не ухудшает (см. принятый риск в Decision).
- **Greenfield, миграции нет** ([[greenfield-mvp-no-migration]]) — старый knob можно удалить без back-compat-машинерии.

## Alternatives considered

| Вариант | Почему отклонён |
| --- | --- |
| Do nothing (оставить гейт + `deletion_approval_exempt_paths`) | Гейт срабатывает на обычной работе; нет способа ослабить порог целиком и нет способа задать always-ask пути. |
| Супервизор-LLM судит каждое danger-действие | +1 LLM-турн на действие, недетерминированно, ломает `advisory`-инвариант супервизора. Детерминированный движок (форма диффа) бесплатен, предсказуем, fail-safe. |
| Простой boolean `disable_danger_approval` | Не масштабируется в codex/claude-стиль уровней; оператор хотел именно лестницу доверия. |
| Два списка: `exempt` для strict + новый `protected` для auto/full | Инверсия смысла между режимами = налог на понимание; `exempt` устаревает, т.к. его работу («не спрашивать про внутрирепо») при дефолте `auto` выполняет сам уровень. |
| Per-node `trust_level` (через `NodeOverride`) | YAGNI для V1: глобал + per-task достаточно. Шов `NodeOverride` ([task/model.py](../../src/wastech_orchestrator/task/model.py#L120)) остаётся — добавить per-node позже механически, если появится нужда. |

## Decision

Вводим `trust_level` (approval-политика mid-task dangerous-diff гейта), задаётся **глобально в `config.yaml` + per-task override**, дефолт `auto` при установке:

| Уровень | Поведение dangerous-diff гейта |
| --- | --- |
| `strict` | Гейт на **любую** danger-diff-находку (любое удаление / правку манифеста зависимостей) — текущее поведение. |
| `auto` _(дефолт)_ | Авто-approve обычной внутрирепо-работы (удаления/переносы/пересоздание кода); гейт только на узкий риск-подсет (какой именно — open question). |
| `full` | Гейт выключен полностью — не спрашивает никогда. |

**Движок решения — детерминированный** (форма git-диффа), супервизор остаётся advisory. Поскольку гейт видит только внутрирепо-дифф, критерий `auto` — это дифф-эвристики (какие внутрирепо-изменения «достаточно рискованны, чтобы всё же спросить»), а НЕ граница репо; out-of-repo — вне области этого гейта.

**Удаляем `security.deletion_approval_exempt_paths`, добавляем `security.protected_paths`** — инвертированный по смыслу список: пути, которые гейтятся **ВСЕГДА, при любом `trust_level`** (пол approval-политики, который уровень не может опустить). Семантика — «всегда требовать approval» (человек решает), НЕ hard-deny «никогда не менять» (это security-ceiling, при нужде — отдельный флаг позже).

**Поведение `protected_paths` в autonomous:** сначала спросить (Telegram); если оператор ответил — по ответу; блокировать в `manual_action_required` **только если оператор не ответил и не уложился в таймаут** (в отличие от обычного гейта, где no-answer+timeout → авто-proceed). Это единственное место, где авто-proceed недопустим — смысл protected именно в том, что человек обязан увидеть.

**Принятый остаточный риск.** На `auto`/`full` (как и на `strict` сегодня) out-of-repo Bash-эффекты не гейтятся и git их не откатит — осознанный выбор оператора, документируем. Этот ADR экспозицию не увеличивает (гейт и так её не покрывал); частично закрыто OS-sandbox'ом у Codex, у Claude — нет.

Цена: `strict`-операторы теряют точечный `exempt`-список (нишевая поблажка), но получают взамен `auto` как глобальный дефолт и `protected_paths` как явный always-ask пол.

## Open questions

- **Точный набор дифф-сигналов для `auto`.** Что именно `auto` всё же гейтит из внутрирепо-изменений: правки манифестов/lock-файлов зависимостей? массовые удаления выше порога? перезапись untracked-файлов? Или ничего — тогда `auto` ≡ `full` в рамках dangerous-diff, и лестницу стоит свести к двум уровням (`strict`/`off`). Проверить при реализации (YAGNI-чек: не плодить `full`, если он не отличим от `auto`).
- **Взаимодействие с provider `permission_profile`.** `trust_level` — про approval-политику worc, `permission_profile` — про capability CLI. Нужно ли их связывать (напр. `full` подразумевает `workspace-write`), или это ортогональные оси, резолвящиеся независимо.

## Implementation notes

- **Конфиг:** `SecurityConfig` в [config/schema.py](../../src/wastech_orchestrator/config/schema.py#L240) — добавить `trust_level: str` (allowlist `strict|auto|full`), удалить `deletion_approval_exempt_paths` (строка 248), добавить `protected_paths: tuple[str, ...]`. Bump `CONFIG_SCHEMA_VERSION` (сейчас 24 → 25). Дефолт dataclass — безопасный fallback (`strict`), а `auto` пишет `install/config_writer.py` в `build_config_mapping` (по образцу [[memory-enabled-out-of-the-box]]). Обновить `packaged/config.example.yaml` и `docs/configuration.md`.
- **Per-task override:** расширить парсинг задачи (task model) полем `trust_level`, валидировать fail-closed против allowlist; overlay поверх глобального дефолта.
- **Движок решения:** детерминированный классификатор диффа рядом с `_gate_dangerous_diff` в [core/hitl.py](../../src/wastech_orchestrator/core/hitl.py) — резолвит `(trust_level, diff-shape, protected_paths)` → `{approve | gate | block}`. `protected_paths` проверяется первым (пол), затем уровень.
- **Autonomous-ветка protected:** таймаут-логика в [notify/telegram.py](../../src/wastech_orchestrator/notify/telegram.py#L92) — для protected-путей no-answer+timeout ведёт в `manual_action_required`, а не авто-proceed.
- **Провайдеры:** если решим связывать с capability (open question 2) — резолв в `providers/claude.py` / `providers/codex.py`, ядро не учит флаги.
- **Тесты:** матрица `trust_level × diff-shape × protected_paths` (unit); интеграция — attended (Telegram approve/deny) и autonomous (timeout → proceed для обычного гейта, → manual_action_required для protected).

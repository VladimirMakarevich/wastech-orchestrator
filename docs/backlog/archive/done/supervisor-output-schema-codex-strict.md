# Supervisor finalize output-schemas must be OpenAI-strict (F41)

Status: **implemented** (2026-07-07, branch `feat/output-schema-codex-strict`; empirical codex re-run pending — owner) Date: 2026-07-07 Owner: Vladimir Makarevich

Привести структурированные output-схемы supervisor-finalize (`DELTA_OUTPUT_SCHEMA`, `_FOLLOW_UPS_SCHEMA`) к строгому режиму OpenAI structured-output, чтобы supervisor-finalize мог выполняться на codex, а не только на claude. Плюс guard-тест, проверяющий, что ВСЕ codex-bound output-схемы оркестратора strict-совместимы (защита от рецидива класса F24/F41).

## The problem

При codex-primary с supervisor на codex (`provider: codex`, `model: gpt-5.4`) финальный supervisor-summary (finalize) падает на codex, тогда как per-step наблюдения проходят. Прогон `p5-03-describe-rules` (Проход 17), `stages/supervisor/run-000000/1-codex/stdout.log`: `invalid_request_error / invalid_json_schema — Invalid schema for response_format 'codex_output_schema': context=('properties','memory_delta','properties','lessons','items','properties','scope'), 'required' is required to be an array including every key in properties. Missing 'paths'`, status 400 → `turn.failed`, `exit_code=1 error_class=process_crashed`. OpenAI structured-output в strict-режиме требует, чтобы у КАЖДОГО объекта `required` перечислял ВСЕ ключи `properties`. Нарушают: `scope`-объект без `required` ([memory/delta.py:110-118](../../../../src/wastech_orchestrator/memory/delta.py#L110-L118)); родительский lesson-объект ([delta.py:122](../../../../src/wastech_orchestrator/memory/delta.py#L122), `required` без `rationale`/`scope`/`evidence`/`trust_hint`); `_FOLLOW_UPS_SCHEMA` ([core/supervisor.py:98-113](../../../../src/wastech_orchestrator/core/supervisor.py#L98), `required` без `paths`/`action_hint`). Дефект не всплывал раньше, потому что supervisor-finalize всегда шёл на claude (claude strict-правило не применяет). Это прямой родственник F24 (там codex-evaluator падал на отсутствии `additionalProperties: false`).

Сегодня замаскировано fallback'ом на claude (задача доходит до `done`, summary/memory_delta пишет claude). Но: при `agents.allowed: [codex]` или недоступном claude finalize-summary + memory_delta + follow_ups **всегда** проваливались бы (нет PR-body и записи памяти); на codex-supervisor каждый прогон жжёт одну codex-попытку на finalize + латентность fallback; memory_delta от codex-супервизора не пишется вовсе (его пишет claude-fallback).

## Constraints

- **Провайдер-контракт в схеме, не в ядре, но strict — это OpenAI-специфика.** Output-схемы объявлены как общий контракт (`memory/delta.py`, `core/supervisor.py`), а strict-совместимость нужна одному провайдеру (codex). Решение вариант A держит схему совместимой для обоих провайдеров (strict-схема с nullable валидна и для claude), не заводя провайдер-специфику в ядро.
- **Опциональность полей сохранить.** `required` = все ключи заставляет модель эмитить каждое поле; чтобы `rationale`/`scope`/`paths`/`action_hint`/`trust_hint` остались необязательными, их типы делаются nullable (`["string","null"]`, `["array","null"]`). Значение может прийти как `null` — парсеры (`parse_delta`, `parse_follow_ups`) уже толерантны к отсутствию, надо убедиться, что толерантны и к `null`.
- **Не ломать F24-совместимость.** Все объекты уже несут `additionalProperties: false` — это сохранить; добавляем только полноту `required` + nullable.
- **KISS / greenfield.** Точечная правка двух схем + guard-тест, без нового рантайм-слоя трансформации.

## Alternatives considered

| Вариант | Почему (не) выбран |
| --- | --- |
| **A — точечный фикс схем + guard-тест (выбран)** | Consistent с тем, как чинили F24 (руками, per-schema); минимально, без нового слоя, без риска изменить контракт значений (nullable сохраняет опциональность). Guard-тест ловит будущие несовместимые схемы. Минус — теоретический whack-a-mole, снимается guard-тестом. |
| **B — общий strict-ify трансформер в codex `_write_output_schema`** | Закрыл бы весь класс раз-и-навсегда, но: (1) новый рекурсивный слой; (2) чтобы сохранить опциональность, трансформер обязан конвертировать non-required→nullable, меняя контракт эмитируемых значений (null vs отсутствие) для ВСЕХ схем разом → риск для парсеров; (3) расходится с установленным в репо per-schema паттерном (F24). Оставлено как возможная будущая консолидация, если схем станет много. |
| **Do nothing (жить на claude-fallback)** | supervisor-finalize навсегда привязан к claude; single-provider=codex не может писать summary/память; тихая трата codex-попытки каждый прогон. Противоречит цели codex-primary. |

## Decision

Привести `DELTA_OUTPUT_SCHEMA` и `_FOLLOW_UPS_SCHEMA` (и все вложенные объекты) к OpenAI-strict: в каждом объекте `required` = полный список ключей `properties`, а прежде-опциональные поля сделать nullable, чтобы модель не была обязана их наполнять. Делаем вариантом A (per-schema, как F24), потому что это минимально, держит схему валидной для обоих провайдеров и не меняет контракт значений сверх nullable; цена — надо не забыть будущие схемы, что закрывается guard-тестом. Дополнительно добавить тест, который проходит по всем output-схемам, уходящим в codex (`DELTA_OUTPUT_SCHEMA`, `_FOLLOW_UPS_SCHEMA`, `_FINDINGS_SCHEMA`, node-schemas), и проверяет strict-инварианты (`additionalProperties:false` + `required` = все ключи на каждом объекте) — регрессионный барьер для класса F24/F41.

## Open questions (resolved at implementation)

- **Толерантность парсеров к `null` — подтверждено.** `parse_delta` (все helpers `_opt_str`/`_str_tuple`/`_parse_scope`/`_parse_evidence` через `.get()` + `isinstance`), `parse_follow_ups`, evaluator `_to_finding`/`_findings_or_none` и `_render_handoff_brief` трактуют `null` идентично отсутствию. Правок парсеров не потребовалось.
- **Полный инвентарь codex-bound схем — покрыт.** Нарушали strict-инвариант и приведены: `DELTA_OUTPUT_SCHEMA`, `_FOLLOW_UPS_SCHEMA`, `_HANDOFF_SCHEMA` (`required: []`), `_finalize_schema` (root), `_FINDINGS_SCHEMA`. Уже были strict (без правок): `_SKILL_MAP_SCHEMA`, `_EVIDENCE_SCHEMA`, HITL-схемы (`_HUMAN_INPUT_SCHEMA`/`_SUBTASK_SCHEMA`/`typed_output_schema`). Guard-тест (`tests/core/test_output_schema_strictness.py`) ходит по общему `_OUTPUT_SCHEMAS`-инвентарю и проверяет оба инварианта. Вне досягаемости guard'а: flow-authored `node.output_schema` (оператор-YAML, не Python-литерал) — отмечено в docstring теста.
- **observe-turn — подтверждено.** Per-step supervisor-observe идёт free-text (`output_schema is None`), поэтому strict-правило к нему не применяется; отдельная схема отсутствует.

## Implementation notes

- [memory/delta.py:96-165](../../../../src/wastech_orchestrator/memory/delta.py#L96) (`DELTA_OUTPUT_SCHEMA`): в `scope` добавить `required: [paths, symbols, nodes]`; в lesson-объекте `required` дополнить до всех ключей; типы опциональных полей → nullable. Аналогично для `failures`/`entities`-веток той же схемы.
- [core/supervisor.py:98-113](../../../../src/wastech_orchestrator/core/supervisor.py#L98) (`_FOLLOW_UPS_SCHEMA`): `required` = `[title, rationale, paths, evidence, severity, action_hint]`; `paths`/`action_hint` nullable.
- Guard-тест: собрать все codex-bound output-схемы, рекурсивно проверить инвариант «у каждого object-узла `additionalProperties:false` и `required` ⊇ ключи `properties`». Разместить рядом с существующими schema-тестами.
- Проверка по существу: перепрогнать codex-primary задачу с supervisor на codex и убедиться, что finalize-summary проходит на codex (`stages/supervisor/run-000000/1-codex` succeeded, без fallback), memory_delta пишется codex-супервизором.
- Связь: тот же класс, что F24 ([evaluator.py:57-82](../../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L57)) — при желании guard-тест закрывает оба. Находки F41 (и F24) — [TEST-FINDINGS.md](../../../../TEST-FINDINGS.md).

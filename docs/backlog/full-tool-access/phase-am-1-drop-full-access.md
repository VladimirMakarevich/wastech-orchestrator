# Фаза Ам-1 — провайдерские full-access режимы удаляются

Status: **ready to implement** Date: 2026-08-17 Owner: Vladimir Makarevich Требования: ТA.1.5 из [requirements-advanced-mode.md](requirements-advanced-mode.md) `schema_version`: +1 (удаление поля `sandbox` — формат-изменение) Зависимости: нет. Фаза только удаляет код и может идти хоть первой в кампании

Единственная фаза кампании, которая уменьшает кодовую базу. `sandbox: danger-full-access` и `--permission-mode bypassPermissions` сегодня не запрещены абсолютно — они заперты условием «отвергать, если `strict_isolation: true`» ([`forbidden_args.py:77`](../../../src/wastech_orchestrator/security/forbidden_args.py)). После решений по вопросам 2 и 3 условие «можно включить режим» и условие «можно включить full-access» стали одним и тем же условием, а режим с ними несовместим — значит разрешающей конфигурации не остаётся, и весь слой их гейтинга существует впустую.

Терять при этом нечего: утилит они не добавляют, запись, сеть и инструменты режим даёт сам, отключённые поверхности Codex включает [Ам-3](phase-am-3-tools-and-shell.md), а эксклюзивом у них остаётся ровно снятие выреза `.git` и пропуск канарейки.

## Что делаем

**1. Оба селектора переезжают в абсолютный запрет** — туда же, где `--dangerously*`, `--yolo` и `--ignore-rules` ([`forbidden_args.py:52`](../../../src/wastech_orchestrator/security/forbidden_args.py)). Отвергаются при любом значении любых ключей, на трёх поверхностях: провайдерский конфиг, `extra_args` конфига, `extra_args` флоу-узла.

**2. Удаляется код, существующий только ради их гейтинга:** `find_full_access_args` ([`forbidden_args.py:77`](../../../src/wastech_orchestrator/security/forbidden_args.py)), таблица `ISOLATION_CHECKS` в композиционном корне, ветка full-access во флоу-валидаторе ([`validator.py:593`](../../../src/wastech_orchestrator/core/flow/validator.py)), `_check_sandbox_field` ([`validation.py:44`](../../../src/wastech_orchestrator/config/validation.py)), ранний `return` на `danger-full-access` в Codex ([`codex.py:373`](../../../src/wastech_orchestrator/providers/codex.py)) и поле `sandbox` в `ProviderConfig`.

**3. Хостовая часть проверки изоляции сохраняется и меняет смысл.** Внутри `isolation_reasons` есть вторая, более ценная половина — **может ли этот хост вообще обеспечить изоляцию** (Linux без `bwrap`/`socat`, нативная Windows, [`claude.py:731`](../../../src/wastech_orchestrator/providers/claude.py)). Она выносится в отдельную функцию, перестаёт зависеть от `strict_isolation` и переформулируется как «может ли на этом хосте существовать пол». Что делать при отрицательном ответе — задаёт [Ам-3](phase-am-3-tools-and-shell.md) (WARN, решение владельца по вопросу 4).

**4. Совместимость.** Удаление ключа — формат-изменение: абзац истории версий, `CONFIG_SCHEMA_VERSION` +1 и запись в `_REMOVED_KEYS` ([`upgrade.py:54`](../../../src/wastech_orchestrator/config/upgrade.py)), чтобы существующий конфиг с `sandbox: danger-full-access` не падал на загрузке, а вычищался через `upgrade-config`.

## Тесты

Каждый из двух селекторов отвергается при **любом** значении `strict_isolation`, в обеих формах (`--flag value` и `--flag=value`), на всех трёх поверхностях. Конфиг с удалённым полем `sandbox` грузится; `upgrade-config` печатает удаление и вычищает ключ. Хостовая проверка изоляции срабатывает независимо от `strict_isolation`.

Инвертируются или удаляются закрепляющие старое поведение тесты: `test_danger_full_access_escape_builds_legacy_sandbox_argv`, `test_full_access_escape_skips_canary`, четыре теста в `tests/security/test_isolation.py`, `tests/core/test_flow_threat_model.py:406`/`:423`, `tests/config/test_validation.py:53`. Каждый либо удаляется, либо превращается в «отвергается всегда» — оставлять их в старом виде нельзя, они пинят ровно то, что фаза убирает.

## Живая проба (часть DoD)

Не требуется: фаза ничего не открывает и не полагается на поведение ОС. Достаточно того, что argv обоих провайдеров при попытке протащить селектор не строится вовсе.

## Риск и откат

Риск один и он документационный: [`analysis/strict-isolation-map.md`](../analysis/strict-isolation-map.md) целиком описывает старую семантику ключа и после фазы становится неверным документом — его надо переписать или снять в том же изменении. Откат — вернуть удалённый код; поскольку оркестратор нигде не развёрнут, живых конфигов с `sandbox: danger-full-access` не существует.

## DoD

Тесты выше зелёные; `ruff`, `mypy`, `lint-imports`, `pytest`, `vulture` зелёные (последний важен: фаза удаляет код, и остатки должны быть удалены полностью); `schema_version` поднят с абзацем истории; `strict-isolation-map.md` приведён в соответствие или снят; шипнутый гайд и `config.example.yaml` больше не упоминают full-access как возможность.

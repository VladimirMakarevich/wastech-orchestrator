# Аудит реализации Codex read-isolation — 2026-07-23

**Статус:** audit only, исправления не вносились. **Проверенный срез:** `2b4dc74b3748e00e4939e19daa05e9540a929e6f` (текущий `HEAD`). **Фокус:** WRI-003 и её стыки с WRI-006, WRI-009, WRI-011 и WRI-012. Источник требований — [decision record](README.md), [WRI-003](wri-003-codex-permission-profile-isolation.md) и [WRI-006](wri-006-cross-platform-isolation-verification.md).

## Итог

Основная проводка Codex реализована: адаптер генерирует attempt-scoped профиль, выбирает его через `default_permissions`, проецирует внутренние deny-пути и write-guards, оставляет shell-сеть выключенной, отключает обнаружение live `AGENTS.md`, имеет одинаковые изоляционные аргументы для fresh/resume и запускает no-model canary до `codex exec`. Ошибки canary до модели обрабатываются fail-closed. Но строгая гарантия для extension/config/rule surface и полная матрица per-attempt canary не соответствуют заявленному контракту. Поэтому считать Milestone 1 полностью доказанным для Codex нельзя.

## Подтверждённые находки

### C-1 — непустой или непроверяемый MCP-инвентарь всё равно сертифицирует strict isolation

**Серьёзность:** критическая.

`run_codex_capability_smoke` добавляет результат `codex mcp list` в evidence, но при успешной файловой canary возвращает `CAPABILITY_PASSED` независимо от `inv_ok` и `inv_empty`: [codex_canary.py:482-503](../../../src/wastech_orchestrator/providers/codex_canary.py). В результате `worc preflight` печатает `isolation smoke OK` и завершается успешно даже с текстом `MCP inventory NOT confirmed empty`; [cli.py:2553-2569](../../../src/wastech_orchestrator/cli.py).

Это прямо противоречит decision record: MCP/apps/plugins/computer-use должны быть отключены или строгий preflight обязан завершиться ошибкой, если поверхность не доказана безопасной. WRI-006 также требует отсутствия неразрешённой filesystem-capable поверхности, а не просто её записи в диагностике. В тестах есть только пустой инвентарь; кейсов `mcp list` с сервером или ошибкой инвентаризации нет.

**Как исправлять:** в strict mode считать непустой либо непроверяемый inventory ошибкой capability/policy, пока каждый конкретный сервер не прошёл явную безопасную allowlist-проверку. Проверку нужно запускать над той же эффективной конфигурацией, что и агент, и добавить тесты на непустой, нераспознанный и аварийный inventory.

### H-1 — проверяется пустой временный `CODEX_HOME`, а не effective config/rules surface реального запуска

**Серьёзность:** высокая.

Инвентаризация MCP запускает `codex mcp list` с новым временным `CODEX_HOME`; [codex_canary.py:369-383](../../../src/wastech_orchestrator/providers/codex_canary.py). В то же время реальный агент использует операторский `CODEX_HOME` и изолируется флагом `--ignore-user-config` плюс project trust override; [codex.py:369-380](../../../src/wastech_orchestrator/providers/codex.py). Поэтому smoke не доказывает ни оставшиеся system/team/managed слои, ни фактические project trust/feature decisions конкретного запуска.

Отдельно локальный `codex-cli 0.144.4` подтверждает, что `--ignore-user-config` отключает только `$CODEX_HOME/config.toml`, а отключение user/project execpolicy `.rules` является отдельным `--ignore-rules` флагом. Адаптер его не передаёт и не имеет альтернативной active-rules inventory/validation; более того, он резервирует этот флаг в `extra_args`. Значит утверждение, что `--ignore-user-config` само нейтрализует external allow-rule layers, не доказано. Это нарушает WRI-003/WRI-006 требование явно учесть каждый remaining config/rule layer и fail-close при неразрешённой поверхности.

**Как исправлять:** сначала согласовать поддерживаемый контракт для user/project/system/managed `.rules` без запрещённого shortcut, затем реализовать именно его: либо детерминированно обнаруживать и отклонять такие layers, либо применять проверяемую adapter-owned policy. Smoke должен сохранять secret-free evidence о проверенных слоях, trust decision, features и active rule locations, а strict mode должен отвергать неизвестный результат.

### H-2 — per-attempt canary доказывает лишь четыре условия и не покрывает обязательную матрицу текущего запуска

**Серьёзность:** высокая.

Перед каждым `codex exec` `_pre_launch_check` вызывает `run_codex_canary` только с private `request.json` и, если он существует, exchange task file; [codex.py:631-673](../../../src/wastech_orchestrator/providers/codex.py). Он не передаёт `ExtraProbes`, хотя именно они содержат repo-read, alias и repo-write проверки; [codex_canary.py:84-100](../../../src/wastech_orchestrator/providers/codex_canary.py). Таким образом, реальная попытка проверяет только direct/shell read одного файла private home и read/write exchange.

Не проверяются: доступ через workspace alias; прямой/косвенный read actual `CODEX_HOME`; каждый relevant `security.denied_read_paths`/internal secret source; разрешённый repo read; соответствие repo write `read-only`/`workspace-write`; отсутствие сети. Полная батарея существует только в generic fixture smoke, который запускается вручную через `worc preflight`, а не обязателен перед каждой попыткой, и использует не фактические deny paths задачи. Это не выполняет обещание WRI-003 «before every `codex exec`» и пункты 2–8 capability matrix WRI-006.

**Как исправлять:** расширить per-attempt probe plan до минимального набора реальных существующих fixture/path, полученных из `AgentRunRequest`, `InternalDenyPolicy`, write guard и resolved network grant. Когда нужное доказательство безопасно построить нельзя, strict mode должен возвращать `CAPABILITY_UNAVAILABLE`, а не сокращать матрицу. Generic host smoke оставить дополнительным доказательством платформы, а не заменой проверки конкретной policy.

### M-1 — reserved Codex `extra_args` отклоняются позднее, а не валидируются в config/flow preflight

**Серьёзность:** средняя; прямого обхода не подтверждено.

`build_codex_argv` надёжно отклоняет provider и request `extra_args` до model call; [codex.py:406-411](../../../src/wastech_orchestrator/providers/codex.py). Но config validator и flow validator вызывают только provider-neutral `find_forbidden_args`/`find_full_access_args`; [validation.py:34-42](../../../src/wastech_orchestrator/config/validation.py), [validator.py:441-445](../../../src/wastech_orchestrator/core/flow/validator.py). Они не вызывают `_find_reserved_codex_args`, поэтому, например, `--profile`, `--disable`, `--add-dir` или `--output-schema` могут пройти config/flow validation и остановят задачу только после подготовки её runtime surface.

Это не ослабляет профиль: adapter boundary остаётся последней fail-closed защитой. Однако расходится с WRI-003 («reserve/reject provider and flow `extra_args`») и с operator docs, где такое отклонение обещано на config time. Кроме того, причина ошибки появляется позднее, чем нужно для безопасного preflight.

**Как исправлять:** зарегистрировать provider-owned validation hook в config-aware validation path, не передавая Codex syntax в core. Добавить таблицу acceptance-тестов для config и flow `extra_args`, подтверждающую, что все authority-bearing формы отклоняются до создания task branch/exchange.

## Быстрая проверка остальных стыков

Проверены без полного повторного аудита все реализации WRI-001/004/007/008/009/010/011/012: typed layout передаёт `InternalDenyPolicy` в Codex; write guard передаёт exchange, Git и `tasks/` как более узкие read carve-outs; frozen instructions отключают live project-doc discovery; provider errors из canary возникают до model call; cross-platform matrix в CI присутствует. Новых подтверждённых расхождений на этих стыках не найдено.

## Выполненные проверки

- Установленный CLI: `codex-cli 0.144.4`; прочитан `codex exec --help` и подтверждена отдельная семантика `--ignore-user-config`/`--ignore-rules`.
- `./.venv/bin/pytest -q` — завершён успешно; отдельно запущены целевые наборы Codex profile/canary/adapter, isolation, config validation, router/preflight. Real-host no-model smoke не был skipped, так как `codex` доступен.
- `./.venv/bin/ruff check` для затронутых Codex-модулей и их тестов — успешно.
- Рабочее дерево было чистым перед созданием этого отчёта; реализация не изменялась.

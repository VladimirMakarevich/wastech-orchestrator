# Архитектурные правила (инварианты)

Источник истины — [orchestrator_final_plan.md](../../orchestrator_final_plan.md). Эти инварианты нарушать нельзя.

## Слои и зависимости

- **Orchestrator Core** управляет последовательностью стадий, лимитами попыток, переходами state machine и условиями публикации. Core вызывает **только** интерфейс `AgentProvider` и **не формирует** provider-specific команды.
- **Provider-адаптеры** (`CodexProvider`, `ClaudeCodeProvider`) — единственное место, где живёт синтаксис конкретного CLI. Они **не выполняют fallback** и **не меняют state machine**.
- **Agent Router** решает primary/fallback на стадию, источник маршрута (global config / task override) и доступность по allowlist.
- **Git Manager / Check Runner / State Store / Artifact Store** — отдельные компоненты с узкой ответственностью.

Направление зависимостей: `core → router → provider(interface)`. Провайдеры не зависят от core.

## Контракты (см. спек §4.3)

- `AgentProvider`: `id`, `preflight() -> ProviderHealth`, `run(AgentRunRequest) -> AgentRunResult`.
- Каждый запуск стадии **независим** и получает весь контекст через файлы/артефакты и prompt — vendor-сессия **не** источник истины.
- Структуры `AgentRunRequest` / `AgentRunResult` / `ProviderHealth` — как в §4.3. Не добавляй скрытых каналов состояния помимо них.

## Стадии и маршрутизация

- Стадии: `planning`, `implementation`, `testing`, `review`, `fixing`, `publishing`.
- `testing` исполняет Check Runner; `publishing` — Git Manager. Остальные — агентные.
- Маршрут по умолчанию: planning/implementation/fixing → primary `claude`, fallback `codex`; review → primary `codex`, fallback `claude`.
- Task override допустим **только**: для известных стадий, провайдером из `agents.allowed`, без изменения security/command/credentials, после полной валидации задачи до создания ветки.

## Fallback

- Разрешён **только** для инфраструктурных классов ошибок (см. спек §7.2).
- **Запрещён** для: проваленных тестов/линтеров, замечаний ревью, неполного выполнения требований при успешном CLI, ошибок Git, невалидной задачи/конфига, исчерпания fix-циклов, нарушения security. Эти случаи → `fixing` / `failed` / `manual_action_required`.
- Частичные изменения после инфраструктурной ошибки не откатываются автоматически: сохраняется snapshot+diff, fallback получает текущий diff и проходит полный набор проверок.

## State machine и идемпотентность

- Переходы транзакционны; повторный запуск **не** создаёт второй commit/push/PR.
- После рестарта возобновляется незавершённый этап либо безопасно сверяется его результат.
- Публикация — только при успешных checks и отсутствии blocking findings.
- Любой автоматический цикл (attempts, fix cycles) имеет настраиваемый предел.

## Чего нельзя делать

- Завязывать core на конкретный CLI.
- Давать провайдеру право на commit/push/PR.
- Делать fallback на ошибке качества.
- Менять provider route задним числом для уже начатой стадии.
- Продолжать работу при обнаружении несогласованного состояния ветки (→ `manual_action_required`).

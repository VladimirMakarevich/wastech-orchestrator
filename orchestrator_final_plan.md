# Финальный план мультиагентного Git-оркестратора

Дата: 2026-06-11

## 1. Цель

Доработать архитектуру консольного Git-оркестратора так, чтобы он мог выполнять этапы задачи через два взаимозаменяемых CLI-агента:

- OpenAI Codex CLI;
- Anthropic Claude Code CLI.

Оркестратор остается владельцем процесса: принимает задачу, управляет Git-веткой, выбирает агента для этапа, запускает проверки, сохраняет состояние, выполняет commit/push и при необходимости создает Pull Request.

Агенты работают только с содержимым репозитория. Они не управляют жизненным циклом Git-задачи и не принимают решения о публикации результата.

## 2. Границы первой версии

В первую версию входят:

- запуск Codex и Claude Code как дочерних CLI-процессов;
- выбор primary и fallback-провайдера отдельно для каждого этапа;
- переопределение маршрута на уровне задачи через allowlist;
- единый формат входа, результата и ошибок провайдера;
- последовательный pipeline `planning -> implementation -> testing -> review -> fixing`;
- ограниченные retry/fix-циклы;
- восстановление после перезапуска;
- аудит запусков, команд и артефактов;
- публикация результата только после успешных проверок.

В первую версию не входят:

- Claude Agent SDK или OpenAI API;
- одновременная работа нескольких агентов в одной рабочей копии;
- автоматический merge Pull Request;
- Web UI;
- динамический выбор агента моделью;
- автоматическая установка или авторизация CLI;
- перенос активной vendor-сессии между Codex и Claude Code.

## 3. Основные принципы

1. Ядро оркестратора не должно знать синтаксис конкретного CLI.
2. Каждый этап является независимым запуском и получает весь необходимый контекст через файлы и prompt.
3. Fallback разрешен только для инфраструктурных ошибок провайдера.
4. Ошибки реализации, тестов и замечания ревью обрабатываются через этап `fixing`.
5. Commit, push и создание PR выполняет только оркестратор.
6. Любой автоматический цикл имеет настраиваемый предел.
7. Все решения о маршрутизации и смене провайдера сохраняются в state store и логах.
8. Следующая стадия разработки начинается только после выполнения DoD предыдущей.

## 4. Компоненты

```text
Task Source
    |
    v
Orchestrator Core
    |-- Task Parser
    |-- State Machine
    |-- Agent Router
    |     |-- CodexProvider
    |     `-- ClaudeCodeProvider
    |-- Git Manager
    |-- Check Runner
    |-- Artifact Store
    `-- State Store
```

### 4.1. Orchestrator Core

Управляет последовательностью этапов, лимитами попыток, переходами состояния и условиями публикации. Core вызывает только интерфейс `AgentProvider` и не формирует provider-specific команды.

### 4.2. Agent Router

Для каждого этапа определяет:

- primary-провайдера;
- fallback-провайдера;
- источник маршрута: global config или task override;
- доступность провайдеров по allowlist;
- следующую допустимую попытку.

### 4.3. AgentProvider

Общий контракт для Codex и Claude Code:

```text
AgentProvider
  id
  preflight() -> ProviderHealth
  run(AgentRunRequest) -> AgentRunResult
```

`ProviderHealth` содержит:

- наличие executable;
- обнаруженную версию;
- статус авторизации;
- поддержку обязательных CLI-возможностей;
- диагностическое сообщение без секретов.

`AgentRunRequest` содержит:

- `task_id`;
- `stage`;
- `working_directory`;
- `prompt`;
- пути к task, plan, diff, check и review artifacts;
- `permission_profile`;
- `timeout_seconds`;
- `attempt`;
- `output_schema`;
- provider-specific model и безопасные дополнительные параметры.

`AgentRunResult` содержит:

- `status`: `succeeded` или `failed`;
- `provider`;
- `stage`;
- `attempt`;
- `exit_code`;
- `started_at` и `finished_at`;
- `final_message`;
- `structured_output`;
- `usage`, если CLI его сообщил;
- `session_id`, только для аудита;
- пути к stdout/stderr/raw event log;
- нормализованную ошибку.

### 4.4. Provider-адаптеры

`CodexProvider` отвечает за:

- preflight через доступные команды Codex CLI;
- построение безопасного вызова `codex exec`;
- JSONL/structured output;
- сопоставление sandbox и permission profile;
- нормализацию exit code и событий.

`ClaudeCodeProvider` отвечает за:

- preflight через доступные команды Claude Code CLI;
- построение безопасного вызова `claude -p`;
- `stream-json`/structured output;
- сопоставление permission mode, allowed/denied tools и sandbox;
- нормализацию exit code и событий.

Provider-адаптеры не выполняют fallback и не изменяют state machine.

## 5. Этапы и маршрутизация

Поддерживаются этапы:

```text
planning
implementation
testing
review
fixing
publishing
```

`testing` выполняется Check Runner, `publishing` выполняется Git Manager. Для агентных этапов маршрут по умолчанию:

| Этап | Primary | Fallback |
|---|---|---|
| `planning` | Claude Code | Codex |
| `implementation` | Claude Code | Codex |
| `review` | Codex | Claude Code |
| `fixing` | Claude Code | Codex |

Маршрут может быть переопределен в задаче только:

- для известных этапов;
- провайдером из `agents.allowed`;
- без изменения security policy, command path или credentials;
- после полной валидации задачи до создания ветки.

Пример YAML front matter:

```yaml
---
id: task-001
agents:
  planning: claude
  implementation: codex
  review: claude
  fixing: codex
---
```

Для JSON-задачи используется объект `agents` с теми же ключами.

## 6. Контекст между этапами

Vendor-сессия не является источником истины. Каждый новый запуск получает контекст из artifacts:

- исходная задача;
- нормализованный task manifest;
- утвержденный план;
- текущий `git diff`;
- результаты тестов и линтеров;
- findings предыдущего ревью;
- описание предыдущей ошибки или частично выполненной попытки.

Это позволяет выполнить следующий этап другим провайдером и восстановить pipeline после перезапуска.

## 7. Fallback и ошибки

### 7.1. Классы ошибок

`ProviderError` нормализуется в один из классов:

- `binary_not_found`;
- `unsupported_version`;
- `authentication_failed`;
- `authorization_failed`;
- `rate_limited`;
- `network_unavailable`;
- `provider_unavailable`;
- `timeout`;
- `process_crashed`;
- `invalid_output`;
- `permission_denied`;
- `configuration_error`;
- `task_failure`.

### 7.2. Ошибки, допускающие fallback

Fallback разрешен для:

- `binary_not_found`;
- `unsupported_version`;
- `authentication_failed`;
- `rate_limited`;
- `network_unavailable`;
- `provider_unavailable`;
- `timeout`;
- `process_crashed`;
- `invalid_output`.

`authorization_failed` и `permission_denied` допускают fallback только тогда, когда отказ относится к конкретному провайдеру и резервный провайдер работает в том же или более строгом permission profile. Ослабление security policy запрещено.

### 7.3. Ошибки без fallback

Fallback не применяется к:

- проваленным тестам или линтерам;
- замечаниям code review;
- неполному выполнению требований задачи при успешном завершении CLI;
- ошибкам Git;
- невалидной задаче или конфигурации;
- исчерпанию fix-циклов;
- нарушению security policy.

Такие случаи направляются в `fixing`, `failed` или требуют ручного вмешательства в зависимости от state machine.

### 7.4. Частичные изменения

Перед агентным запуском сохраняются:

- текущий commit SHA;
- `git status --porcelain`;
- checksum diff;
- список существующих artifacts.

Если primary-провайдер завершился инфраструктурной ошибкой после изменения файлов:

1. Оркестратор не откатывает изменения автоматически.
2. Сохраняется post-attempt snapshot и diff.
3. Fallback получает текущий diff и сообщение о частично выполненной попытке.
4. Результат fallback проходит полный набор checks.
5. Оба запуска остаются в аудите.

## 8. State machine

Статусы задачи:

```text
new
validated
preparing
planning
implementing
testing
reviewing
fixing
ready_to_publish
committing
pushing
creating_pr
done
failed
manual_action_required
```

Основные переходы:

```text
new -> validated -> preparing -> planning -> implementing
implementing -> testing
testing(success) -> reviewing
testing(failure) -> fixing -> testing
reviewing(success) -> ready_to_publish
reviewing(blocking findings) -> fixing -> testing
ready_to_publish -> committing -> pushing -> creating_pr -> done
any active stage -> failed
any active stage -> manual_action_required
```

Условия:

- переход выполняется транзакционно;
- повторный запуск не создает второй commit, push или PR;
- после рестарта возобновляется незавершенный этап либо выполняется безопасная сверка его результата;
- публикация разрешена только при успешных checks и отсутствии blocking findings;
- число agent attempts и fix cycles ограничено конфигурацией.

## 9. State Store

SQLite остается достаточным для первой версии. Помимо `tasks` нужны сущности:

```text
tasks
stage_runs
provider_attempts
check_runs
artifacts
publish_operations
```

Минимально сохраняются:

- идентификаторы задачи, этапа и попытки;
- выбранные primary/fallback и фактически использованный provider;
- статус и класс ошибки;
- timestamps и exit code;
- commit SHA до и после этапа;
- пути к artifacts;
- fingerprint операции commit/push/PR;
- счетчики retries и fix cycles.

Секреты, access tokens и полное окружение процесса в SQLite не сохраняются.

## 10. Артефакты и логи

```text
logs/
  <task-id>/
    task.normalized.json
    plan.md
    current.diff
    checks/
      <run-id>.log
    review/
      findings.json
      summary.md
    stages/
      <stage>/
        <attempt>-<provider>/
          request.json
          stdout.log
          stderr.log
          events.jsonl
          result.json
          before.diff
          after.diff
    publish/
      commit.json
      push.json
      pull-request.json
```

Правила:

- все пути относительны к task artifact directory;
- логи не перезаписываются;
- request artifact хранит redacted-представление запуска;
- machine-readable result отделен от human-readable summary;
- artifacts регистрируются в SQLite с checksum.

## 11. Конфигурация

Целевая структура:

```yaml
repo:
  url: "git@github.com:OWNER/REPO.git"
  local_path: "./workspace/repo"
  base_branch: "main"
  branch_prefix: "agent"

agents:
  allowed:
    - claude
    - codex

  max_stage_attempts: 2
  max_fix_cycles: 3

  routing:
    planning:
      primary: claude
      fallback: codex
    implementation:
      primary: claude
      fallback: codex
    review:
      primary: codex
      fallback: claude
    fixing:
      primary: claude
      fallback: codex

  providers:
    claude:
      command: "claude"
      model: ""
      timeout_seconds: 1800
      max_turns: 50
      max_budget_usd: null
      permission_profile: "workspace-write"
      extra_args: []
    codex:
      command: "codex"
      model: ""
      timeout_seconds: 1800
      sandbox: "workspace-write"
      permission_profile: "workspace-write"
      extra_args: []

security:
  strict_isolation: true
  allowed_environment:
    - "PATH"
    - "HOME"
    - "USERPROFILE"
    - "CODEX_HOME"
    - "CLAUDE_CONFIG_DIR"
  denied_read_paths:
    - ".env"
    - "secrets/**"
  denied_commands:
    - "git commit"
    - "git push"
    - "gh pr create"

checks:
  commands:
    - "npm test"
    - "npm run lint"

git:
  create_pull_request: true
  pr_base: "main"
```

Требования к конфигурации:

- неизвестные ключи маршрута считаются ошибкой;
- primary и fallback не могут ссылаться на запрещенного провайдера;
- task override не может менять provider command, extra args или security;
- `extra_args` валидируются по provider allowlist и не допускают отключения sandbox/permissions;
- legacy Codex-only конфиг мигрирует в маршрут Codex для всех агентных этапов с предупреждением.

## 12. Security model

1. Рабочая область ограничивается отдельным clone/worktree.
2. Агентам запрещены commit, push, merge и создание PR.
3. Оркестратор передает только allowlisted environment variables.
4. Секретные файлы исключаются из чтения и логирования.
5. CLI запускаются без shell-интерполяции пользовательских строк.
6. Task ID, branch name и пути проходят строгую нормализацию.
7. Опции обхода sandbox/permissions запрещены валидатором конфигурации.
8. При `strict_isolation: true` невозможность включить требуемую изоляцию завершает preflight ошибкой.
9. Git credentials и credentials агентов настраиваются вне оркестратора.
10. Pull Request и CI остаются обязательным контрольным слоем.

## 13. Восстановление и идемпотентность

При старте оркестратор:

1. Находит задачи в активных статусах.
2. Сверяет SQLite, task files, рабочую ветку и artifacts.
3. Проверяет, завершился ли внешний процесс и существует ли валидный result artifact.
4. Повторяет только незавершенную идемпотентную операцию.
5. Для commit/push/PR использует сохраненный fingerprint и проверяет удаленное состояние.
6. При неоднозначном состоянии переводит задачу в `manual_action_required`.

Нельзя автоматически:

- повторно публиковать неизвестный commit;
- удалять частичные изменения;
- менять provider route задним числом для уже начатого этапа;
- продолжать после обнаружения несогласованного состояния ветки.

## 14. Проверки и приемка

### Unit

- validation конфигурации и task overrides;
- route resolution и allowlist;
- command builder обоих providers;
- parsing structured output;
- error classification;
- переходы state machine;
- redaction и нормализация путей;
- retry/fallback limits.

### Integration

Используются fake CLI executables для сценариев:

- успешный запуск;
- отсутствующий binary;
- неуспешная авторизация;
- rate limit;
- timeout;
- process crash;
- malformed output;
- инфраструктурная ошибка после изменения файлов;
- успешный fallback;
- запрет fallback для quality failure.

### End-to-end

На временном Git-репозитории проверить:

- Claude Code выполняет planning и implementation;
- Codex выполняет review;
- failed checks запускают fixing;
- успешный результат приводит к одному commit, push и PR;
- рестарт не дублирует публикацию;
- исчерпание attempts переводит задачу в `failed`.

## 15. Стадии реализации

Стадии выполняются строго последовательно:

1. [Контракты и конфигурация](implementation_stages/01_contracts_and_config.md)
2. [Провайдерный слой и Codex-адаптер](implementation_stages/02_provider_layer.md)
3. [Claude Code-адаптер](implementation_stages/03_claude_code_adapter.md)
4. [Маршрутизация и fallback](implementation_stages/04_routing_and_fallback.md)
5. [Pipeline и восстановление](implementation_stages/05_pipeline_and_recovery.md)
6. [Безопасность и наблюдаемость](implementation_stages/06_security_and_observability.md)

Переход к следующей стадии разрешен только после документированного выполнения всех пунктов DoD текущей стадии.

## 16. Финальный Definition of Done

Проект считается завершенным, когда:

- Codex и Claude Code доступны только через общий `AgentProvider`;
- маршрут Claude для planning/implementation/fixing и Codex для review работает по умолчанию;
- task-level overrides ограничены allowlist;
- инфраструктурный fallback работает и полностью аудируется;
- quality failures не вызывают смену провайдера;
- state machine восстанавливается после контролируемого перезапуска;
- commit, push и PR выполняются только оркестратором и не дублируются;
- security policy нельзя ослабить через task или `extra_args`;
- unit, integration и end-to-end тесты проходят;
- документация эксплуатации описывает установку, preflight, авторизацию и диагностику обоих CLI.

## 17. Официальные справочные материалы

- [OpenAI Codex CLI Reference](https://developers.openai.com/codex/cli/reference)
- [Claude Code CLI Reference](https://code.claude.com/docs/en/cli-reference)
- [Claude Code Settings](https://code.claude.com/docs/en/settings)
- [Claude Code Security](https://code.claude.com/docs/en/security)
- [GitHub CLI: `gh pr create`](https://cli.github.com/manual/gh_pr_create)

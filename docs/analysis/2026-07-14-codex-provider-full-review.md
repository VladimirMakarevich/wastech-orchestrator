# Полное ревью Codex-провайдера оркестратора

**Дата:** 2026-07-14
**Область:** Codex provider, общий provider/process spine, security ceiling, конфигурация,
маршрутизация, артефакты и тесты
**Проверенная локальная версия Codex CLI:** 0.142.5
**Результат ревью:** request changes

## 1. Резюме

Архитектурная основа Codex-провайдера качественная: provider-specific CLI-синтаксис
изолирован от core, процессы запускаются argv-массивом без shell-интерполяции, prompt
передаётся через stdin, окружение ограничено allowlist, поддержаны durable resume,
structured output, Windows sandbox helper, timeout и остановка дерева процессов.

Однако реализация пока не обеспечивает заявленный security contract и не поддерживает
современную модель reasoning Codex целиком. Обнаружены четыре блокирующих класса проблем:

1. security.denied_commands и security.denied_read_paths не применяются как запреты к Codex;
2. provider/node extra_args могут расширить полномочия поверх security ceiling;
3. network_access=false отключает web search, но не гарантирует отсутствие других внешних
   инструментов из user config, apps, MCP, browser, plugins и hooks;
4. structured output и промежуточные файлы могут содержать неотредактированные секреты.

Также текущая reasoning-модель не поддерживает диапазон Light–Ultra:

- Light не принимается;
- Max молча понижается до xhigh;
- Ultra отклоняется;
- Ultra ошибочно нельзя моделировать ещё одним значением model_reasoning_effort, поскольку
  это multi-agent execution mode.

На дату ревью публичная актуальная линейка OpenAI называется GPT-5.6 Sol / Terra / Luna.
Публичного контракта GPT-6 в официальной документации не обнаружено. Произвольные model ID
CodexProvider передаёт без allowlist, поэтому GPT-5.6 Sol/Terra/Luna механически совместимы,
но packaged defaults и документация репозитория всё ещё закрепляют GPT-5.4, а местами GPT-5.5.

## 2. Критерии и методика

Ревью выполнено относительно:

- [архитектуры оркестратора](../worc_architecture.md);
- правил из [.agents/rules](../../.agents/rules);
- hard invariants из AGENTS.md;
- исходного кода CodexProvider и общего provider adapter spine;
- конфигурации, security validation, router и artifact pipeline;
- provider/config/routing tests;
- полного набора project quality gates;
- официальной документации OpenAI и фактического интерфейса локального Codex CLI 0.142.5.

Изменения реализации в рамках ревью не выполнялись. Этот документ является единственным
добавленным артефактом.

## 3. Что реализовано хорошо

### 3.1. Архитектурная изоляция

- Core не знает синтаксис Codex CLI.
- Сборка argv находится в
  [providers/codex.py](../../src/wastech_orchestrator/providers/codex.py).
- Общая orchestration-логика живёт в provider-agnostic
  [_adapter_base.py](../../src/wastech_orchestrator/providers/_adapter_base.py).
- Import contracts прошли: 5 контрактов сохранены, нарушений нет.
- Codex и Claude adapters не импортируют друг друга.

### 3.2. Безопасный запуск процесса

- CLI запускается списком аргументов, а не shell-строкой.
- Пользовательский prompt передаётся через stdin.
- Working directory передаётся отдельным аргументом.
- Окружение формируется через allowlist.
- Заданы обязательные timeout и stop semantics.
- На POSIX создаётся отдельная process group; на Windows используется tree termination.

### 3.3. Codex-specific correctness

- Approval policy фиксируется в never.
- Permission profile не может повысить read-only node до workspace-write.
- network_access=false явно добавляет web_search="disabled".
- Поддержаны --json, --output-last-message и --output-schema.
- Для текущего Codex JSONL structured output корректно восстанавливается из last-message,
  если terminal event не содержит output.
- Resume argv построен с корректным порядком exec options и resume-specific options.
- Реализованы preflight и runtime-проверки Windows sandbox helper.
- False-success с фатальной Windows sandbox ошибкой на stderr переводится в infra failure,
  позволяя Router выполнить fallback.

### 3.4. Тестирование

Provider, config и routing tests содержат большой набор детерминированных fake CLI scenarios:

- fresh и resume runs;
- output schema;
- error classification;
- network policy;
- Windows helper discovery;
- false-success guard;
- redaction sinks;
- cross-provider fallback;
- timeout/cancellation.

Полный pytest suite завершился на 100% с exit code 0.

## 4. Сводка замечаний

| ID | Приоритет | Проблема | Итог |
|---|---:|---|---|
| CXP-01 | P0 | denied_commands/denied_read_paths не запрещают действия Codex | Security invariant нарушен |
| CXP-02 | P0 | extra_args позволяют расширить sandbox/network/config authority | Security ceiling обходится |
| CXP-03 | P0 | Offline node наследует внешние возможности Codex | network_access=false не является полным offline |
| CXP-04 | P0/P1 | structured_output сохраняется без redaction | Возможна запись секретов в artifacts/SQLite |
| CXP-05 | P1 | Сырые stdout/last-message сначала пишутся в durable attempt dir | Crash window оставляет секреты |
| CXP-06 | P1 | Light/Max/Ultra моделируются неверно или не поддерживаются | Актуальный reasoning contract не выполнен |
| CXP-07 | P1 | Packaged model defaults устарели и рассинхронизированы | Новая установка получает GPT-5.4 |
| CXP-08 | P1/P2 | Нет typed image input и capability policy для новых инструментов | Современные возможности недоступны или небезопасны |
| CXP-09 | P1 | Preflight выставляет authenticated=true без auth probe | Health report может ложно быть healthy |
| CXP-10 | P2 | Capability preflight проверяет слишком мало CLI-контрактов | Несовместимость обнаруживается только во время run |
| CXP-11 | P2 | JSONL parser частично ориентирован на старые event shapes | Ошибки диагностируются неточно |
| CXP-12 | P2 | Windows helper regex слишком широкий | Возможен ложный infra fallback |
| CXP-13 | P1/P2 | Два обязательных quality gate красные | Definition of Done не выполнен |

## 5. Детальные замечания

### CXP-01 — security deny policy не применяется к Codex

**Приоритет:** P0
**Статус:** блокирует production-ready verdict

SecurityConfig содержит:

- denied_commands;
- denied_read_paths.

Loader по умолчанию запрещает git commit, git push, gh pr create и gh pr merge:
[config/loader.py](../../src/wastech_orchestrator/config/loader.py#L61).

ClaudeProvider преобразует эти значения в disallowed tool patterns:
[providers/claude.py](../../src/wastech_orchestrator/providers/claude.py#L162) и
[providers/claude.py](../../src/wastech_orchestrator/providers/claude.py#L457).

CodexProvider при построении argv их не использует:
[providers/codex.py](../../src/wastech_orchestrator/providers/codex.py#L213).

Общий adapter применяет denied_read_paths только для чтения известных secret values и
последующей редактуры вывода:
[_adapter_base.py](../../src/wastech_orchestrator/providers/_adapter_base.py#L586).
Это уменьшает последствия утечки в логах, но не запрещает агенту читать файл.

**Последствия:**

- агент может выполнить git commit, хотя commit/push/PR принадлежат только orchestrator;
- агент может прочитать .env и secrets/**;
- при разрешённой сети секрет может быть отправлен наружу до redaction;
- конфигурация и документация создают ложное ощущение enforcement.

**Тестовый пробел:** redaction tests подтверждают очистку результата, но для Codex нет теста,
что запрещённая команда или чтение файла действительно блокируются.

**Рекомендация:** генерировать для каждого run контролируемые Codex execpolicy rules с
decision="forbidden", либо реализовать эквивалентный fail-closed command/read policy. Rules
должны формироваться orchestrator-ом, не зависеть от user config и проверяться smoke-тестом
на реальном CLI.

### CXP-02 — extra_args обходят security ceiling

**Приоритет:** P0
**Статус:** блокирует production-ready verdict

Текущий validator запрещает:

- --dangerously*;
- --yolo;
- --ignore-rules;
- malformed sandbox selector;
- явный danger-full-access через sandbox selector.

См. [security/forbidden_args.py](../../src/wastech_orchestrator/security/forbidden_args.py#L27).

Однако provider-level и node-level extra_args объединяются и добавляются после security
overrides:
[providers/codex.py](../../src/wastech_orchestrator/providers/codex.py#L229) и
[providers/codex.py](../../src/wastech_orchestrator/providers/codex.py#L291).

Текущий Codex CLI принимает, среди прочего:

- --add-dir DIR — дополнительная writable directory;
- -c sandbox_permissions=[...] — изменение доступа к диску;
- -c sandbox_workspace_write.network_access=true;
- -c web_search="live";
- --profile;
- --enable FEATURE;
- MCP/apps/hooks/plugin-related config overrides.

Поэтому task или config могут расширить доступ поверх node permission/network policy.

**Рекомендация:**

1. заменить свободный Codex extra_args типизированной конфигурацией;
2. разрешать только известный allowlist не-security аргументов;
3. разбирать каждую форму -c/--config и отклонять authority-expanding keys;
4. запретить --add-dir, profiles, user-selected config layers и произвольные feature flags;
5. повторять проверку и при config load, и непосредственно перед process spawn.

### CXP-03 — network_access=false не гарантирует offline

**Приоритет:** P0
**Статус:** блокирует строгую offline-гарантию

CodexProvider корректно добавляет web_search="disabled", когда node не получил network grant:
[providers/codex.py](../../src/wastech_orchestrator/providers/codex.py#L261).

Но CLI по умолчанию продолжает загружать user config и доступные аккаунту/окружению
инструменты. В локальном Codex CLI 0.142.5 как stable и включённые обнаружены:

- apps;
- browser_use и browser_use_external;
- computer_use;
- hooks;
- image_generation;
- multi_agent;
- plugins;
- tool_call_mcp_elicitation.

App/MCP calls имеют отдельную модель доступа и не эквивалентны sandbox network toggle.
Следовательно, отключение web search является необходимой, но недостаточной частью offline.

**Рекомендация:** запускать orchestrated Codex через выделенный, сгенерированный config layer:

- --ignore-user-config;
- controlled CODEX_HOME/config/rules;
- explicit disable для внешних capabilities на offline nodes;
- отдельный typed grant для apps/MCP/browser/computer use;
- fail-closed preflight, подтверждающий фактический capability set.

Auth при этом можно продолжать получать из CODEX_HOME: текущий CLI документирует, что
--ignore-user-config не отключает auth storage.

### CXP-04 — structured_output не редактируется

**Приоритет:** P0/P1

В общем adapter spine final_message и usage проходят redaction:
[_adapter_base.py](../../src/wastech_orchestrator/providers/_adapter_base.py#L427).

Но AgentRunResult получает исходный parsed.structured_output:
[_adapter_base.py](../../src/wastech_orchestrator/providers/_adapter_base.py#L482).

Затем result.json записывается после удаления session ID, но без рекурсивной очистки
structured output:
[_adapter_base.py](../../src/wastech_orchestrator/providers/_adapter_base.py#L501).

Structured output может далее попасть в evaluator findings, state и дополнительные artifacts.

**Рекомендация:** перед созданием AgentRunResult применять redact_mapping к structured_output,
с тем же extra_secrets, который используется для message и usage. Добавить тесты на секреты
в nested dict/list structures и на все downstream sinks.

### CXP-05 — crash window для сырых artifacts

**Приоритет:** P1

Process runner сразу стримит stdout в paths.stdout_path:
[providers/process.py](../../src/wastech_orchestrator/providers/process.py#L109).

Redaction выполняется только после завершения process runner:
[_adapter_base.py](../../src/wastech_orchestrator/providers/_adapter_base.py#L376).

Codex CLI также самостоятельно пишет --output-last-message непосредственно в attempt dir,
а провайдер редактирует файл только во время parse:
[providers/codex.py](../../src/wastech_orchestrator/providers/codex.py#L530) и
[providers/codex.py](../../src/wastech_orchestrator/providers/codex.py#L549).

При hard kill, падении процесса orchestrator-а или I/O exception сырое содержимое остаётся на
диске.

**Рекомендация:** писать stdout и last-message в private scratch directory, после run создавать
redacted durable artifacts атомарным rename/write. Альтернатива — streaming redactor с корректной
обработкой секретов, пересекающих границы chunks.

### CXP-06 — reasoning Light–Ultra

**Приоритет:** P1

Текущий mapping:
[providers/capabilities.py](../../src/wastech_orchestrator/providers/capabilities.py#L11).

| Пользовательское значение | Текущее поведение | Требуемое поведение |
|---|---|---|
| minimal | передаётся как minimal | валидировать по выбранной модели |
| light | configuration error | alias к low |
| low | low | оставить |
| medium | medium | оставить |
| high | high | оставить |
| extra high | только xhigh | принять alias extra-high/extra_high/xhigh |
| max | молча преобразуется в xhigh | не понижать; использовать документированный Max contract |
| ultra | configuration error | отдельный multi-agent execution mode |

Ключевой дефект — silent downgrade max → xhigh. Пользователь получает меньше reasoning, чем
запросил, без ошибки или предупреждения.

Ultra, согласно текущей модели Codex, использует subagents и не является ещё одним scalar effort.
Ему требуются отдельные semantics:

- execution mode;
- лимит и изоляция subagents;
- concurrency budget;
- token/time budget;
- объединение outputs;
- session/audit representation;
- cancellation и stop-tree ownership;
- capability reporting.

На дату ревью официальный стабильный non-interactive ключ для Ultra через
model_reasoning_effort не подтверждён. Поэтому нельзя просто отправлять
model_reasoning_effort="ultra". Реализация должна feature-detect документированный exec contract
и fail closed, если конкретный CLI/account его не предоставляет.

### CXP-07 — устаревшие и рассинхронизированные model defaults

**Приоритет:** P1

CodexProvider не имеет model allowlist и корректно передаёт любое непустое значение через
--model:
[providers/codex.py](../../src/wastech_orchestrator/providers/codex.py#L279).

Поэтому актуальные публичные IDs:

- gpt-5.6-sol;
- gpt-5.6-terra;
- gpt-5.6-luna

механически поддерживаются при наличии entitlement.

Но shipped defaults остаются на gpt-5.4:

- [packaged/config.example.yaml](../../src/wastech_orchestrator/packaged/config.example.yaml#L86);
- [install/config_writer.py](../../src/wastech_orchestrator/install/config_writer.py#L61);
- [docs/configuration.md](../configuration.md#L236);
- комментарии packaged flows.

В functional documentation местами уже упоминается gpt-5.5, то есть документация не образует
единого source of truth.

**Рекомендация:**

- либо оставить пустой model и следовать CLI/account default;
- либо выбрать gpt-5.6-sol как осознанный packaged default;
- держать примеры моделей в одном source of truth;
- не вводить жёсткий allowlist model IDs;
- добавить optional preflight model capability/smoke probe;
- валидировать reasoning относительно выбранной модели, а не только provider ID.

Для оркестратора безопаснее пустой default, если нет политики reproducibility/pinning.

### CXP-08 — современные возможности не представлены типизированно

**Приоритет:** P1/P2

Текущий CLI поддерживает -i/--image как для fresh, так и для resume execution. Модели Sol/Terra/Luna
поддерживают image input, но AgentRunRequest не имеет attachments/image_paths:
[providers/base.py](../../src/wastech_orchestrator/providers/base.py#L127).

Передача image через extra_args небезопасна, поскольку позволяет task указать произвольный host
path и обойти path policy.

Также отсутствует осознанная orchestration-модель для:

- model verbosity;
- reasoning summary;
- personality;
- fast/service tier;
- native multi-agent/Ultra;
- apps/connectors;
- MCP;
- browser/computer use;
- image generation;
- hooks/plugins;
- feature enable/disable.

Не все эти возможности должны быть включены. Требуется typed capability registry:

- capability request от flow/node;
- global security ceiling;
- provider-specific projection;
- preflight negotiation;
- explicit grants и safe defaults;
- observability фактически предоставленных capabilities.

### CXP-09 — preflight не проверяет auth

**Приоритет:** P1

Общий preflight запускает command --version, но после успешной проверки выставляет
authenticated=True без auth probe:
[_adapter_base.py](../../src/wastech_orchestrator/providers/_adapter_base.py#L227).

Локальный CLI 0.142.5 поддерживает codex login status. На проверенной машине команда вернула
Logged in using ChatGPT.

При отсутствии auth текущий preflight способен показать healthy, а реальный node упадёт только
при запуске модели.

**Рекомендация:** добавить provider hook для безопасной auth-проверки без вывода токена и
возвращать authenticated=false/unknown отдельно от installed/version-compatible.

### CXP-10 — capability preflight слишком слабый

**Приоритет:** P2

Codex-specific probe в основном проверяет наличие -c в help и часть resume options. Не
проверяются все элементы реально формируемого argv:

- --ask-for-approval;
- exec;
- --cd;
- --sandbox;
- --json;
- --output-last-message;
- --output-schema;
- resume grammar;
- используемые config keys;
- strict config behavior.

CLI может пройти preflight и упасть при первом run с unsupported_version/configuration error.

**Рекомендация:** проверять contract по feature flags/help output, а не только версию. Для
управляемого config layer применять --strict-config, чтобы неизвестные устаревшие ключи не
игнорировались молча.

### CXP-11 — JSONL parser не полностью соответствует текущему event schema

**Приоритет:** P2

Parser распознаёт несколько flat/legacy форматов:
[providers/codex.py](../../src/wastech_orchestrator/providers/codex.py#L315).

Текущий CLI обычно выдаёт:

- thread.started;
- turn.started;
- item.completed с вложенным item;
- turn.completed.

Happy path работает, потому что final message читается из last-message. Но вложенные
agent_message, turn.failed и structured error events почти не используются. Если stderr пуст,
точная ошибка может быть потеряна и нормализована как process_crashed.

**Рекомендация:** вести versioned/current event parser, разбирать item.completed, turn.failed,
error codes и usage, хранить contract fixtures от поддерживаемых CLI versions.

### CXP-12 — широкий Windows false-success regex

**Приоритет:** P2

Post-success guard необходим и исправляет реальный инцидент, описанный в
[codex-windows-sandbox-false-success](2026-07-14-codex-windows-sandbox-false-success.md).

Но pattern включает одно упоминание имени codex-windows-sandbox-setup.exe и общий префикс
windows sandbox:
[providers/codex.py](../../src/wastech_orchestrator/providers/codex.py#L88).

Безобидная diagnostic-строка с именем helper теоретически может превратить настоящий success в
permission_denied и запустить fallback.

**Рекомендация:** матчить error code/контекст запуска helper, а не само имя файла. Добавить
negative tests с диагностикой, где helper упоминается без failure.

### CXP-13 — Definition of Done сейчас не зелёный

**Приоритет:** P1/P2

Результаты команд:

| Проверка | Результат |
|---|---|
| pytest -q | PASS, 100%, exit 0 |
| ruff format --check . | PASS, 264 files formatted |
| lint-imports | PASS, 5 contracts kept |
| interrogate src | PASS, 71.5% при minimum 70% |
| vulture | PASS |
| deptry src | PASS |
| ruff check . | FAIL, 7 × E501 |
| mypy src | FAIL, 3 errors |

Ruff errors находятся в:

- providers/codex.py — четыре длинные строки комментариев;
- tests/providers/test_codex_run.py — три длинные строки комментариев.

Mypy errors:

- process.py: os.killpg отсутствует в Windows stubs;
- process.py: signal.SIGKILL отсутствует в Windows stubs, два использования.

Код защищает POSIX path runtime-проверкой, но mypy запущен из Windows venv и проверяет Windows
platform stubs. Требуется platform-safe typing/narrowing, а не отключение gate.

## 6. Матрица моделей и reasoning

### 6.1. Модели

| Модель | Публичный статус на дату ревью | Состояние orchestrator |
|---|---|---|
| gpt-5.6-sol | актуальная публичная | argv passthrough работает |
| gpt-5.6-terra | актуальная публичная | argv passthrough работает |
| gpt-5.6-luna | актуальная публичная | argv passthrough работает |
| gpt-5.4 | устаревший packaged default | всё ещё установлен по умолчанию |
| GPT-6 Sol/Terra/Luna | публичный контракт не найден | поддержка не может быть заявлена |

Если GPT-6 доступен как private preview/account entitlement, свободный --model может позволить
запуск, но это не подтверждает:

- совместимый reasoning contract;
- output-schema behavior;
- event schema;
- tool/capability surface;
- entitlement preflight.

Поэтому официально заявлять GPT-6 support без документированного contract/smoke matrix нельзя.

### 6.2. Reasoning

Новая схема должна разделить:

1. scalar reasoning effort: low/medium/high/xhigh и документированные model-specific значения;
2. пользовательские aliases: light, extra-high;
3. compute mode: Max;
4. orchestration mode: Ultra/multi-agent.

Нельзя молча понижать неизвестный или недоступный уровень. Допустимые исходы:

- exact support;
- explicit documented normalization;
- configuration error до запуска;
- capability unavailable с понятным сообщением.

## 7. Предлагаемая целевая архитектура

### 7.1. CodexExecutionCapabilities

Ввести typed provider-level описание:

- model;
- reasoning_effort;
- compute_mode;
- agent_mode;
- image_inputs;
- web_search;
- sandbox_network;
- apps;
- mcp;
- browser;
- computer_use;
- plugins/hooks;
- writable_roots;
- readable-path deny policy.

Flow/node запрашивает capability, global security config задаёт ceiling, CodexProvider только
проецирует итоговый effective request в CLI/config/rules.

### 7.2. Controlled invocation bundle

Для каждого attempt формировать:

- argv;
- stdin;
- controlled config;
- generated rules;
- private scratch paths;
- effective capability manifest.

В request artifact сохранять redacted manifest фактических возможностей. Это позволит доказать,
что offline node действительно был offline, а read-only node не получил дополнительные writable
directories.

### 7.3. Capability negotiation

Preflight должен отдельно сообщать:

- installed;
- version;
- authenticated;
- CLI contract compatible;
- supported models/unknown entitlement;
- supported reasoning modes;
- output schema;
- resume;
- image input;
- controlled config/rules support;
- external tool isolation.

Router должен считать отсутствующую требуемую capability configuration/unsupported error, а не
случайной infrastructure failure.

## 8. Рекомендуемый порядок исправлений

### Этап 1 — закрыть security blockers

1. CXP-01: Codex deny rules для commands/read paths.
2. CXP-02: typed extra args/strict allowlist.
3. CXP-03: controlled config и external capability isolation.
4. CXP-04/CXP-05: end-to-end redaction без raw durable files.

До завершения этапа нельзя считать security ceiling выполненным.

### Этап 2 — актуализировать модели и reasoning

1. Убрать silent max → xhigh.
2. Добавить light → low и aliases для extra high.
3. Ввести model-aware validation.
4. Спроектировать Ultra как отдельный multi-agent execution mode.
5. Синхронизировать packaged config, installer, flows, docs и tests.

### Этап 3 — capability platform

1. Typed image attachments с path validation.
2. Typed grants для apps/MCP/browser/computer/plugins/hooks.
3. Effective capability manifest.
4. Auth и feature preflight.

### Этап 4 — robustness

1. Обновить JSONL parser.
2. Сузить Windows failure signatures.
3. Добавить real-CLI contract smoke tests.
4. Закрыть ruff/mypy gates.

## 9. Минимальная тестовая программа для исправления

Обязательные новые тесты:

- Codex не может выполнить каждый default denied_command.
- Codex не может прочитать каждый default denied_read_path.
- Task extra_args не могут добавить writable root.
- Task extra_args не могут включить network/web search/apps/MCP.
- Offline node запускается без внешних tools даже при небезопасном user config.
- Secret внутри nested structured_output отсутствует во всех artifacts/state sinks.
- Hard kill не оставляет raw stdout/last-message.
- light нормализуется в low.
- max никогда не понижается молча.
- ultra не преобразуется в model_reasoning_effort.
- capability unavailable определяется до model run.
- gpt-5.6-sol/terra/luna проходят argv contract tests.
- image paths ограничены разрешёнными roots.
- unauthenticated CLI даёт authenticated=false.
- current item.completed и turn.failed fixtures разбираются корректно.
- benign Windows helper diagnostics не вызывают fallback.

Рекомендуемые real CLI smoke tests должны быть opt-in и не входить в обычный hermetic unit suite.

## 10. Официальные источники

- [OpenAI API models](https://developers.openai.com/api/docs/models)
- [Codex models и reasoning modes](https://learn.chatgpt.com/docs/models)
- [Codex CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Codex approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security)
- [Codex execpolicy rules](https://learn.chatgpt.com/docs/agent-configuration/rules)
- [Codex sandboxing](https://learn.chatgpt.com/docs/sandboxing)

## 11. Итоговый вывод

CodexProvider уже является рабочим и хорошо протестированным CLI adapter-ом, включая сложные
Windows и resume scenarios. Но он пока не является доказуемо безопасным provider boundary:
policy intent из SecurityConfig не полностью переносится в Codex runtime, а свободный extra_args
позволяет расширять полномочия.

Поддержка текущих Sol/Terra/Luna обеспечена только на уровне model ID passthrough. Полная
современная поддержка требует model-aware reasoning, отдельного Ultra/multi-agent mode,
типизированных capabilities и контролируемого config/rules layer.

До закрытия CXP-01—CXP-05 и CXP-06 заявлять полное соответствие hard invariants и поддержку
Light–Ultra нельзя.

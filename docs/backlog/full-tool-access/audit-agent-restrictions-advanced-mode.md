# Что агенту всё равно запрещено в advanced mode — аудит явных ограничений

Status: **аудит по коду ветки `feat/full-tool-access`** Date: 2026-08-24 Owner: Vladimir Makarevich

## О чём этот документ

Оператор поставил в `config.yaml` три ключа:

```yaml
security:
  strict_isolation: false
  disable_read_isolation: true
  allow_git_evidence: true
```

Это ровно то, что пишет `worc install` сегодня. Документ отвечает на один вопрос: **что оркестратор при таких настройках всё равно явно указывает агенту при запуске** — какие флаги, какие деналь-пути, какое окружение, какие проверки до и после запуска. Не «что режим разрешает» (это [requirements-advanced-mode.md](requirements-advanced-mode.md) и [security.md](../../../src/wastech_orchestrator/packaged/guide/config/security.md)), а обратная сторона: остаточные стены, которые никакой ключ не убирает.

Простыми словами: даже в «режиме максимальной свободы» командная строка агента остаётся собранной оркестратором, и в ней остаётся около двадцати явных ограничений. Ниже они перечислены по одному, с примером на каждое.

### Что означает эта тройка ключей

| Ключ | Значение | Что реально включилось |
| --- | --- | --- |
| `strict_isolation: false` | advanced mode | Родительское окружение уходит агенту целиком; гейта существования инструментов нет; запись разрешена за пределы клона (на весь том); сеть включена у каждого узла; у каждого узла есть шелл, включая `read-only`. |
| `disable_read_isolation: true` | read-isolation OFF | Провайдеры читают свои родные конфиги проекта: Claude — `--setting-sources project` (то есть `CLAUDE.md`, `.claude/settings.json`, хуки, MCP, скиллы репозитория), Codex — пользовательский `config.toml` и `.codex/` проекта как **доверенный**. В режиме этот ключ избыточен: эффективное значение считается как `disable_read_isolation OR NOT strict_isolation` ([`config/schema.py:346`](../../../src/wastech_orchestrator/config/schema.py)). |
| `allow_git_evidence: true` | инертен | Грант «read-only git-глаголы» в режиме не применяется вообще: у каждого узла и так неограниченный шелл, добавлять нечего. `preflight` и лог прогона пишут это прямым текстом: `git-evidence: ON … — inert under strict_isolation=false`. |

### Три уровня, на которых живут ограничения

1. **Выбор, который нельзя сделать никаким ключом** — абсолютные запреты флагов. Проверяются трижды: валидатором конфига, валидатором флоу и билдером argv.
2. **То, что жёстко подставляется в каждый запуск** — argv, профиль песочницы, файл настроек, окружение. Оператор это не пишет и не может переопределить.
3. **То, что проверяется вокруг запуска** — канарейка до запуска, отпечатки, гейты стейджинга и коммита, редакция, пломбирование обмена.

---

## 1. Что нельзя выбрать вообще, ни при каком значении ключей (уровень 1)

### 1.1 Абсолютно запрещённые флаги (оба провайдера)

Источник истины — [`security/forbidden_args.py:53`](../../../src/wastech_orchestrator/security/forbidden_args.py). Ловятся оба написания (`--flag value` и `--flag=value`):

- любой флаг, начинающийся на `--dangerously` (в том числе Claude `--dangerously-skip-permissions`, Codex `--dangerously-bypass-approvals-and-sandbox`);
- `--allow-dangerously-skip-permissions`, `--yolo`, `--ignore-rules`;
- `--sandbox danger-full-access` и `-s danger-full-access` (Codex full access);
- `--permission-mode bypassPermissions` (Claude full access);
- `--sandbox` без значения (сломанный флаг съел бы следующий — отказ, а не «сойдёт»).

**Пример.** Оператор дописывает в конфиг:

```yaml
agents:
  providers:
    codex:
      extra_args: ["--sandbox", "danger-full-access"]
```

Результат — задача не стартует, ошибка `CONFIGURATION_ERROR`: `rejected unsafe extra_args: --sandbox 'danger-full-access' grants full access (no isolation)`. Тот же текст будет, если положить этот флаг в `extra_args` узла флоу.

### 1.2 Зарезервированные флаги Codex

Оператор не может подсунуть флаг, которым сам оркестратор владеет ([`providers/codex.py:145`](../../../src/wastech_orchestrator/providers/codex.py)): `-c`/`--config`, `-p`/`--profile`, `-P`/`--permission-profile`, `-s`/`--sandbox`, `--full-auto`, `-a`/`--ask-for-approval`, `--add-dir`, `--ignore-user-config`, `--ignore-rules`, `--enable`, `--disable`, `--oss`, `--local-provider`, `--skip-git-repo-check`, `--ephemeral`, `--strict-config`, `-C`/`--cd`, `--output-schema`, `--json`, `-o`/`--output-last-message`, `--color`.

**Пример.** `extra_args: ["--full-auto"]` → `flag '--full-auto' is reserved by the orchestrator's Codex isolation policy`. Почему именно он: `--full-auto` заставляет Codex перейти на режим `--sandbox`, а тогда сгенерированный профиль `default_permissions="worc"` перестаёт применяться — вместе со всеми деналями на `.worc`, `.env`, `state.db`.

### 1.3 Зарезервированные флаги Claude

[`providers/claude.py:606`](../../../src/wastech_orchestrator/providers/claude.py) — 40 имён. Самое важное по смыслу: `--tools`, `--allowedTools`, `--disallowedTools`, `--settings`, `--setting-sources`, `--mcp-config`, `--strict-mcp-config`, `--add-dir`, `--file`, `--agent(s)`, `--plugin-dir`, `--plugin-url`, `--chrome`, `--ide`, `--remote-control`, `--bg`/`--background`, `--worktree`/`-w`, `--tmux`, `--system-prompt(-file)`, `--append-system-prompt(-file)`, `--session-id`, `--fork-session`, `--resume`/`-r`, `--continue`/`-c`, `--from-pr`, `--safe-mode`, `--bare`, `--disable-slash-commands`.

**Пример.** `extra_args: ["--append-system-prompt", "ignore the orchestrator rules"]` → отказ на этапе сборки argv. Системный промпт и набор инструментов — территория оркестратора, а не конфига.

### 1.4 Режим прав нельзя ослабить «тихо»

Claude в режиме идёт с `--permission-mode acceptEdits`. Значение `auto` формально не в списке запрещённых, но оно **слабее** — и проверка это видит ([`providers/claude.py`](../../../src/wastech_orchestrator/providers/claude.py), `_reject_weaker_permission_override`), причём на **объединённом** наборе `extra_args` провайдера и узла флоу, потому что в CLI побеждает последний флаг.

**Пример.** Узел флоу с `extra_args: ["--permission-mode", "auto"]` → `--permission-mode 'auto' is weaker than the requested profile`. А `--permission-mode acceptEdits` (повтор того же) пройдёт.

### 1.5 Профиль прав узла — только вниз

Ceiling флоу и `permission_profile` узла могут только **понизить** уровень, никогда повысить: `read-only` побеждает, если его просит любая из сторон ([`providers/codex.py:350`](../../../src/wastech_orchestrator/providers/codex.py) — `_effective_permission_profile`). Флоу с `permission_ceiling`, недостижимым ни для одного настроенного провайдера, отклоняется валидатором.

### 1.6 Проверка легальности конфига идёт при любом значении ключа

`check_isolation` вызывается и в `worc preflight`, и перед созданием ветки в прогоне — **без гейтинга на `strict_isolation`** ([`core/orchestrator.py:2656`](../../../src/wastech_orchestrator/core/orchestrator.py)). Логика простая: именно в режиме сгенерированный профиль и есть весь локальный пол, поэтому это последняя конфигурация, которой можно позволить не доказывать свою легальность.

---

## 2. Codex: что реально уходит в командную строку (уровень 2)

### 2.1 Полный пример запуска

Узел `workspace-write`, клон в `/Users/me/work/repo`, режим включён, read-isolation выключена:

```text
codex --ask-for-approval never exec
  --cd /Users/me/work/repo
  --json
  --output-last-message /Users/me/work/repo/.worc/logs/<task>/stages/<node>/run-000012/1-codex/last-message.txt
  -c permissions.worc={ extends = ":workspace", filesystem = { ":minimal" = "read", "/" = "write",
       "/Users/me/work/repo" = "write",
       "/Users/me/work/repo/.worc-io" = "read",
       "/Users/me/work/repo/.git" = "read",
       "/Users/me/work/repo/.git/hooks" = "read",
       "/Users/me/work/repo/tasks" = "read",
       "/Users/me/work/repo/.worc" = "deny",
       "/Users/me/work/repo/.worc/.env" = "deny",
       "/Users/me/work/repo/.worc/runs" = "deny",
       "/Users/me/work/repo/.env" = "deny",
       "/Users/me/work/repo/secrets" = "deny" }, network = { enabled = true } }
  -c default_permissions="worc"
  -c projects."/Users/me/work/repo".trust_level="trusted"
  --output-schema <attempt>/output-schema.json
  --model gpt-5.1-codex-max
  -c model_reasoning_effort="high"
  -
```

Промпт уходит на **stdin** (последний `-`). Содержимое задачи никогда не попадает в argv — только пути к файлам.

### 2.2 Ограничения в этом argv, по одному

**(1) `--ask-for-approval never` — жёстко.** Запрос одобрения у человека в headless-прогоне никто не ответит, поэтому политика зафиксирована, а `-a`/`--ask-for-approval` в `extra_args` запрещены (см. 1.2).

**(2) Профиль прав эмитится всегда, отказа от него нет.** Если в argv профиля нет, попытка отклоняется: `no generated permission profile in the launch argv, so the pre-launch canary cannot prove the sandbox — refusing the attempt rather than running unproven` ([`providers/codex.py:741`](../../../src/wastech_orchestrator/providers/codex.py)).

**(3) `deny` на приватное множество оркестратора — при любом значении read-isolation.** Это `InternalDenyPolicy` ([`runtime_layout.py:126`](../../../src/wastech_orchestrator/runtime_layout.py)): control home `.worc`, private home `.worc`, разрешённый env-файл `.worc/.env`, корень замороженных бандлов `.worc/runs`.

**Пример.** `cat .worc/.env` из шелла узла → `sandbox denied` / `permission denied`, попытка получает класс ошибки `PERMISSION_DENIED`. То же для `sqlite3 .worc/state.db`, `ls .worc/runs/instruction-bundles/<task>`.

**(4) `deny` на публичный чёрный список `denied_read_paths`.** Из установки: `.env`, `secrets/**`. Запись `secrets/**` схлопывается в деналь каталога `secrets` — подкаталоги закрыты без обхода дерева.

**Пример.** `cp secrets/id_rsa /tmp/` → отказ песочницы. Отдельная деталь режима: неограниченный `**` в середине пути (например `a/**/b`) в режиме **разрешён** — при `strict_isolation: true` он был бы отклонён как непроверяемый кроссплатформенно.

**(5) `read` (то есть «читать можно, писать нельзя») на контрольные корни клона.** Это `ProviderWriteGuardPolicy` ([`runtime_layout.py:166`](../../../src/wastech_orchestrator/runtime_layout.py)): `.worc-io` (обмен), `git_dir`, `git_common_dir`, каталог хуков, дерево `tasks/`. Пересчитывается **под каждую попытку** после подготовки ветки и выдаётся любой попытке, у которой есть чем менять клон — инструменты записи **или** шелл. То есть `read-only` узел Codex тоже его получает.

**Пример.** `git config --local core.hooksPath /tmp/mine` → отказ (запись в `.git`). `echo x >> tasks/pending/t-42.md` → отказ. А `cat .git/config` — разрешено, это `read`.

**(6) Рабочий каталог зафиксирован `--cd`.** `-C`/`--cd` в `extra_args` запрещены, так что узел не может «переехать» в другой репозиторий, оставшись под тем же профилем.

**(7) Канал вывода принадлежит оркестратору.** `--json`, `--output-last-message`, `--output-schema` подставляются нами; `--json`, `-o`, `--output-schema`, `--color` зарезервированы. Типизированный ответ узла — это схема, которую собрал core, а не то, что придумал агент.

**(8) Канарейка перед **каждым** запуском.** Тот же профиль прогоняется под `codex sandbox -P` без модели и без сети ([`providers/codex.py:741`](../../../src/wastech_orchestrator/providers/codex.py)). Проверяется: приватный дом закрыт (напрямую и через шелл), обмен только на чтение, сам бинарь CLI исполняется под профилем, и **каждый** write-guard корень действительно отказывает записи. Результат пишется в `<attempt>/canary.json`.

**Пример.** Доказанная утечка (`CONFIGURATION_ERROR`) — фатальна и в режиме тоже. А вот «песочницу не удалось продемонстрировать на этом хосте» (`CAPABILITY_UNAVAILABLE`) в режиме становится предупреждением, и попытка продолжается: `the sandbox could not be demonstrated on this host and the run continues (strict_isolation is off) … treat .git and .worc as writable here`.

**(9) `read-only` узел: клон остаётся только на чтение.** В режиме профиль `read-only` получает `"/" = "write"` (том) и `"<клон>" = "read"`. Более специфичное правило побеждает, поэтому такой узел **не может** изменить репозиторий, но **может** писать вне клона.

**Пример.** Ревьюер (`read-only`) не запишет `src/foo.py`, но запишет `~/.npm/_cacache` — и это ровно то, чего режим и хотел.

### 2.3 Что режим у Codex отдал обратно (для контраста)

- Все `--disable <feature>` больше не эмитятся: `hooks`, `multi_agent`, `computer_use`, `browser_use`, `browser_use_external`, `browser_use_full_cdp_access`, `in_app_browser`, `memories`, `apps`, `plugins` — включены ([`providers/codex.py:126`](../../../src/wastech_orchestrator/providers/codex.py)).
- `network = { enabled = true }` у каждого узла, и тем же флагом включён backend-side `web_search` — один флаг ведёт обе поверхности, чтобы «офлайн» узел не оказался онлайн через бэкенд.
- `--ignore-user-config` не эмитится, проект помечен `trust_level="trusted"` — это следствие `disable_read_isolation: true`.

---

## 3. Claude: что реально уходит в командную строку (уровень 2)

### 3.1 Полный пример запуска

Узел `workspace-write` на macOS, режим включён, read-isolation выключена:

```text
claude -p --output-format stream-json --verbose
  --setting-sources project
  --permission-mode acceptEdits
  --allowedTools Read,Glob,Grep,Edit,Write,Bash,PowerShell,TodoWrite,BashOutput,KillShell,WebFetch,WebSearch
  --disallowedTools <см. 3.3>
  --settings <attempt>/claude-sandbox-settings.json
  --model claude-opus-4-6
  --effort high
  --json-schema {"type":"object",...}
  --max-turns 400
```

Промпт — на stdin. Контекст (задача, план, дифф, отчёты проверок) — только путями.

### 3.2 Что зафиксировано в самом argv

**(1) `-p --output-format stream-json --verbose`** — headless-режим и канал вывода принадлежат оркестратору; поток событий редактируется построчно перед записью в артефакты.

**(2) `--permission-mode acceptEdits`** — не `auto` и не `bypassPermissions`. Более слабое значение из `extra_args` отклоняется (1.4).

**(3) `--allowedTools` эмитится всегда.** В режиме `--tools` (жёсткий гейт существования) не эмитится — это и есть заявленное ослабление, — но список авто-одобрения остаётся: headless-прогону некому отвечать на запрос прав.

**(4) `--max-turns` из конфига** (по умолчанию 400). Исчерпание лимита — «чистый» отказ задачи (`error_max_turns`), а не инфра-сбой; при `max_turns_gate: true` вместо отказа приходит Telegram-гейт «продолжить/остановить».

**(5) `--json-schema`** — типизированный ответ узла обязателен.

### 3.3 `--disallowedTools` — в режиме это единственный носитель инструментального пола

Собирается в [`providers/claude.py:851`](../../../src/wastech_orchestrator/providers/claude.py). Пять категорий, и их **нельзя путать**: первые две — реальный пол, остальные — трение и телеметрия.

**(а) Приватное множество оркестратора — `Read` + все четыре редактора.** Для каждого пути из `InternalDenyPolicy` эмитится и точный узел, и его поддерево:

```text
Read(//Users/me/work/repo/.worc), Read(//Users/me/work/repo/.worc/**),
Write(//Users/me/work/repo/.worc), Edit(//Users/me/work/repo/.worc),
MultiEdit(//Users/me/work/repo/.worc), NotebookEdit(//Users/me/work/repo/.worc), …
Read(//Users/me/work/repo/.worc/.env), Read(//Users/me/work/repo/.worc/.env/**), …
Read(//Users/me/work/repo/.worc/runs), …
```

**Почему это важно именно здесь.** Инструменты редактирования **не проходят** через OS-песочницу — их держит только этот список. Поэтому в режиме он называет весь набор из четырёх (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`), а не историческую пару: без гейта существования `MultiEdit` так же доступен, как `Write` ([`providers/claude.py:242`](../../../src/wastech_orchestrator/providers/claude.py)).

**Пример.** Инструмент `Read` на `.worc/.env` → отказ. Но `cat .worc/.env` из `Bash` при `disable_read_isolation: true` песочница **не** заблокирует (см. 3.4) — это разные слои, и разница признана.

**(б) Write-guard корни — все четыре редактора.** `.worc-io`, `git_dir`, `git_common_dir`, каталог хуков, `tasks/` — каждый как `Write(//abs)`, `Write(//abs/**)`, и так для `Edit`/`MultiEdit`/`NotebookEdit`.

**Пример.** Попытка `Edit` файла `.worc-io/<task>/plan.md` (переписать себе же входной план) → отказ инструмента; а сверх этого мутация обмена ловится отпечатком после попытки (см. 5.6).

**(в) `read-only` узел: четыре редактора запрещены целиком, по имени.** Без пути — просто `Write,Edit,MultiEdit,NotebookEdit`. Плюс в файле песочницы весь клон уходит в `denyWrite`.

**Пример.** Узел ревью не изменит ни один файл репозитория ни инструментом, ни (на хосте с песочницей) командой.

**(г) `denied_commands` → шаблоны для обоих шеллов.** Из установки `["git commit","git push","gh pr create","gh pr merge"]` получается:

```text
Bash(git commit:*),PowerShell(git commit:*),Bash(git push:*),PowerShell(git push:*),
Bash(gh pr create:*),PowerShell(gh pr create:*),Bash(gh pr merge:*),PowerShell(gh pr merge:*)
```

**Честная оценка веса.** Это **не пол**, а трение и след в логе. Префиксное сравнение обходится через `bash -c`, абсолютный путь, `git -C`, `git --git-dir=`, цель Makefile, дочерний процесс, `gh api` или `curl`. Ценность — ровно одна строка в логе, говорящая «агент потянулся к публикации». Именно поэтому шаблоны эмитятся для **обоих** шеллов: на Windows шелл называется `PowerShell`, и без второй половины след терялся бы там, где он нужен.

**(д) `denied_read_paths` → `Read(.env)`, `Read(secrets/**)`.** Инструментальный слой поверх песочницы и поверх редакции.

**(е) Набор трения режима: `AskUserQuestion`, `CronCreate`, `RemoteTrigger`, `EnterWorktree`.** Честно только про первый: у headless-прогона нет кому отвечать, узел просто сожжёт ходы. Остальные три оставлены потому, что ничего не стоят, но их обоснование больше не держится — право писать вне клона (`~/Library/LaunchAgents`, `~/.config/systemd/user`, rc-файл шелла) даёт ту же персистентность вообще без инструментов. **Персистентность этим режимом не удерживается**, и это записано прямо в коде ([`providers/claude.py:198`](../../../src/wastech_orchestrator/providers/claude.py)).

### 3.4 Файл `--settings`: OS-песочница шелла

Пишется под приватный дом попытки, читает его родительский процесс CLI вне песочницы ([`providers/claude.py:722`](../../../src/wastech_orchestrator/providers/claude.py)). Для `workspace-write` узла в режиме:

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "allowUnsandboxedCommands": false,
    "excludedCommands": [],
    "autoAllowBashIfSandboxed": true,
    "filesystem": {
      "allowWrite": ["/"],
      "denyRead": [],
      "denyWrite": [
        "/Users/me/work/repo/.worc",
        "/Users/me/work/repo/.worc/.env",
        "/Users/me/work/repo/.worc/runs",
        "/Users/me/work/repo/.worc-io",
        "/Users/me/work/repo/.git",
        "/Users/me/work/repo/.git/hooks",
        "/Users/me/work/repo/tasks"
      ]
    },
    "network": { "allowedDomains": ["*"] },
    "credentials": {
      "files": [{ "path": "/Users/me/work/repo/.worc/.env", "mode": "deny" }]
    }
  }
}
```

Что здесь ограничение, а что нет:

- **`failIfUnavailable: true`** — если песочница недоступна, запуск падает, а не «продолжает без неё».
- **`allowUnsandboxedCommands: false` и `excludedCommands: []`** — ни одна команда не выводится из-под песочницы. Ключи `enableWeakerNestedSandbox`, `mask`, `tlsTerminate` не пишутся никогда.
- **`autoAllowBashIfSandboxed: true`** — записан явно, чтобы вендорский дефолт не превратил каждую команду в запрос прав, на который в headless некому ответить.
- **`denyWrite`** — контрольная плоскость и Git остаются неизменяемыми **внутри** разрешения на весь том. Вложение деналя в разрешение — конструкция самого вендора (`denyWithinAllow`), но она **прочитана из бинаря, а не доказана на хосте**; проверить у себя — `worc preflight --paid-isolation-probe`.
- **`denyRead: []`** — вот это важный нюанс именно вашей конфигурации. При `disable_read_isolation: true` приватное множество перестаёт быть **read**-запрещённым на уровне песочницы (шелл может его читать), но остаётся **write**-запрещённым, и остаётся `Read`-запрещённым на уровне инструментов, а env-файл дополнительно закрыт через `credentials.files`.
- **`network.allowedDomains: ["*"]`** — сеть бинарная, фильтрации домена нет и не планируется.
- Для `read-only` узла добавляется `denyWrite` на **весь корень клона**.

### 3.5 Что режим у Claude отдал обратно

- Нет `--tools`, то есть существуют все встроенные инструменты, включая те, что появятся в будущем релизе (принятая цена: неизвестный инструмент авто-одобряется, а не авто-отклоняется).
- `Bash`, `PowerShell`, `TodoWrite`, `BashOutput`, `KillShell` — у каждого узла, включая `read-only`.
- `WebFetch` и `WebSearch` — у каждого узла, что бы ни объявил флоу.
- `--setting-sources project` — из-за `disable_read_isolation: true` грузятся `CLAUDE.md`, `.claude/settings.json`, хуки, MCP и скиллы репозитория; `--strict-mcp-config` не эмитится.
- Конструкция `git_evidence` (шелл, суженный до 12 read-only глаголов) **не применяется** — в режиме её нечем дополнить.

---

## 4. Окружение: что не отдаётся даже в режиме (уровень 3)

### 4.1 Агенту уходит родительское окружение целиком — но с двумя изъятиями

[`security/env.py:386`](../../../src/wastech_orchestrator/security/env.py), `build_child_env`. Изъятия:

1. **Имена, определённые в `.worc/.env`.** Агенту запрещено читать этот файл, поэтому проброс его содержимого обошёл бы деналь. Нужен один из них — оператор называет его в `extra_environment`, то есть принимает решение осознанно.
2. **`extra_environment` присваивается сверху** и побеждает пробросанное.

**Пример.** В `.worc/.env` лежит `TELEGRAM_BOT_TOKEN=…`. В шелле оператора эта переменная тоже есть. Дочерний процесс агента её **не** получит. А `NUGET_PACKAGES` из шелла — получит.

Порядок ключей детерминирован (сортировка, потом присвоения) — окружение прогона воспроизводимо, а не зависит от порядка обхода `os.environ`.

### 4.2 Процессы `git`/`gh` самого оркестратора остаются на allowlist

[`security/env.py:454`](../../../src/wastech_orchestrator/security/env.py), `build_orchestrator_env`. Режим расширяет то, что может **агент**, а это — не агент. Отдельная функция, а не флаг, чтобы новый call-site не мог случайно опереться на исключение.

**Пример.** `GH_REPO` из шелла оператора перенацелил бы pull request на чужой репозиторий, а `GIT_DIR` увёл бы и команды, и пути, которые защищает write-guard.

### 4.3 Скраб перенацеливающих имён — поверх всего

[`git_manager.py:220`](../../../src/wastech_orchestrator/git_manager.py). Пространство `GIT_*`/`GH_*` закрыто **whitelist**-ом: имя доходит до git/gh только если оно перечислено, а новое имя из будущего релиза закрыто по умолчанию. Удаляются в том числе `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_CONFIG*`, `GIT_SSH`/`GIT_SSH_COMMAND`, `GIT_ASKPASS`/`SSH_ASKPASS`, `GIT_AUTHOR_*`/`GIT_COMMITTER_*`, `GH_REPO`, `GH_HOST`, `GIT_CONFIG_PARAMETERS`, `GH_CONFIG_DIR`.

Сверху накладывается hardening ([`git_manager.py:159`](../../../src/wastech_orchestrator/git_manager.py)): `GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0`, `GIT_EDITOR=false`, `GIT_SEQUENCE_EDITOR=false`, `GCM_INTERACTIVE=Never`, `LC_ALL=C` — и это уже никаким значением конфига не переопределяется.

**Пример.** Оператор кладёт `LANG=ru_RU.UTF-8` в `extra_environment` — оркестратор всё равно исполнит git с `LC_ALL=C`, иначе сетевой сбой перестал бы распознаваться как временный.

### 4.4 Редакция расширяется, чтобы компенсировать снятый гейт

Когда гейта имён нет, значения переменных с «секретным» именем вырезаются из логов и артефактов **по имени**, без исключений для allowlist ([`providers/redaction.py:148`](../../../src/wastech_orchestrator/providers/redaction.py)). Принятая цена — безобидное значение с секретным именем напечатается как `[REDACTED]`. Исключены по имени `PWD` и `OLDPWD`: они матчатся по сегменту `pwd`, а держат путь, и их вырезание превращало каждую ссылку `file:line` в `[REDACTED]/src/foo.py:42`.

### 4.5 Локальный git-конфиг клона: гейт драйверов

Перед стейджингом и checkout репо-локальный конфиг инвентаризируется, и ключ, чьё значение — исполняемая программа (`filter.*.clean|smudge|process`, `diff.*.command|textconv`), приводит к отказу с `ManualActionRequired` ([`git_manager.py:2036`](../../../src/wastech_orchestrator/git_manager.py)). Глобальный конфиг оператора сознательно не инвентаризируется — там живут учётные данные, а этот случай закрывает детект.

---

## 5. Что ограничено помимо флагов (уровень 3)

### 5.1 Промпт: контракт безопасности перед каждым запуском

Core-owned текст, добавляемый **к каждому** промпту — агент, ревьюер, каждый ход супервизора ([`core/flow/security_preamble.py:30`](../../../src/wastech_orchestrator/core/flow/security_preamble.py)). Это **не** enforcement, и так и написано. В вашей конфигурации рендерятся базовый блок плюс два абзаца (read-restraint и advanced-mode); третий (нет OS-песочницы) — только на таком хосте.

В режиме абзац говорит буквально: делай что нужно, у тебя есть сеть и запись вне клона, но два правила не обсуждаются — **ничего не публиковать** (никакой commit/push/merge/tag/PR, ни в этот remote, ни на любой другой адрес, ни через второй клон) и **не читать и не писать** `.worc/`, `.worc-io/`, `.git/`, `tasks/`. И добавляет, что оба пункта проверяются после завершения, а находки уходят человеку.

### 5.2 Запуск процесса: единственный чокпоинт

[`providers/process.py`](../../../src/wastech_orchestrator/providers/process.py) — все внешние CLI идут через `run_process`, и там инварианты: argv-список (никогда строка, никогда `shell=True`), **обязательный** таймаут, ребёнок получает ровно переданный `env`, промпт на stdin (без `stdin_text` детский stdin — `DEVNULL`, родительский не наследуется), stdout стримится в файл.

### 5.3 Пиннинг исполняемых файлов

`git`, `gh`, CLI агентов, `ps`, лончер демона резолвятся один раз на старте процесса и дальше запускаются по этим путям (`_pinned_argv`). Программа, подложенная на `PATH` **во время** работы агента, не меняет то, что запускает оркестратор. Две вещи это не покрывает и не может: подмену **между** прогонами и правку кода самого установленного пакета.

### 5.4 Таймауты и лимиты циклов

Таймаут узла (`timeout_seconds` узла или дефолт флоу), таймаут проверок (`checks.timeout_seconds`, установка пишет 7200), дефолтный таймаут tool-узла (`tools.default_timeout_seconds`), `agents.max_stage_attempts: 3`, `max_fix_cycles: 15`, `max_total_fix_iterations: 30`, политика ретраев только для транзиентных классов. Ни один из них не ослабляется режимом.

### 5.5 Барьер тишины процессов

После попытки дерево процессов терминируется и **доказывается пустым** в ограниченном бюджете. Если доказать не удалось — результат провайдера нельзя доверять, потому что неизвестный процесс мог продолжать писать в репозиторий или обмен.

### 5.6 Обмен `.worc-io`: отпечаток, пломба, карантин

- Манифест обмена снимается **до** попытки и сверяется **после** — мутация курируемой поверхности ловится из состояния, которое держит родитель, до того как её потребит следующий узел.
- Манифест отказывает симлинку/reparse point, hard link, специальному файлу и NTFS alternate data stream.
- На терминальном статусе обмен пломбируется в приватный снапшот с контрольными суммами и удаляется из репозитория, чтобы следующая задача не увидела чужой план/дифф.
- Помеченное мутацией дерево уезжает в карантин как испорченная улика — не пломбируется и не подлежит восстановлению.

### 5.7 Гейты стейджинга и коммита

Публикует только оркестратор, и это проверяется механически перед каждым коммитом ([`git_manager.py:2262`](../../../src/wastech_orchestrator/git_manager.py) и далее):

- `.worc-io` не должен быть **трекнут** вообще, и ни одно застейдженное изменение под `.worc`/`.worc-io` не может поехать в коммит. Правило `.gitignore` границей коммита не считается — `git add -f` его обходит, поэтому проверяется индекс.
- Весь застейдженный набор доказывается против положительного allowlist операции: любой чужой путь — насильно добавленный артефакт, посторонний файл в `tasks/`, несвязанный код — это нарушение, даже если его `git add` прошёл.
- Источник переименования освобождён от allowlist, **кроме** случая, когда это артефактный путь — переименование из `.worc-io`/`.worc`/`tasks` вынесло бы их содержимое наружу.

**Пример.** Агент сделал `git add -f .worc/state.db` и оставил в индексе. Публикация не состоится: `refusing to commit: runtime artifact path(s) would be committed (…)`, задача уходит на ручное разбирательство.

### 5.8 Гейт опасного диффа

Считается **не** от `HEAD`, а от последнего коммита, который сделал сам оркестратор для этой задачи (до первого — от базы задачи). Поэтому самокоммит внутри прогона не «съедает» изменения — их всё равно предъявят человеку. Спрашивают в трёх местах: у пишущего узла, на его `hitl`-развороте и **ещё раз прямо перед публикующим коммитом** — последнее и делает обещание выполнимым для флоу, где шелл есть у каждого узла. Отказ — это стоп, а не ретрай: агента к тому моменту уже нет.

При `trust_level: auto` (что пишет установка) форма диффа сама по себе не гейтится — спрашивает только совпадение с `protected_paths`. Список зависимостей (`package.json`, `pyproject.toml`, `Cargo.lock`, `*.csproj`, … — около 50 шаблонов) относится к уровню `strict`.

### 5.9 Отпечаток контрольной плоскости вокруг каждой попытки

Снимается и сверяется: адрес, куда реально пойдёт push (`remote get-url --push`, хранится дайджестом — там может быть токен), пользовательский git-конфиг, конфиг агентского CLI в клоне. Всё — дайджестами, никогда значениями. Каждый `gh` вызов несёт `--repo`, приколоченный из `repo.url`. Push дополнительно перечитывает адрес прямо перед отправкой и отказывается, если он изменился.

Важно читать буквально: с 2026-08-24 дрейф **сообщается, но не паркует** задачу. Подложенный хук, переписанный `pushurl` и подменённый бинарь на `PATH` — это громкое предупреждение и ⚠️ в трейсе, после чего прогон продолжается. Что это требует от оператора: предупреждение о дрейфе — сигнал остановить прогон и выбросить клон, если это делали не вы.

### 5.10 Входные данные задачи

Гейт валидации до создания ветки: `max_task_bytes: 262144`, `max_task_lines: 5000`, `max_line_bytes: 8192`, `max_control_ratio: 0.01`, плюс сканер инъекций по **значениям front-matter** (значение, начинающееся с `-`, или содержащее `;`, `` ` ``, `|`, `$(`, перевод строки — отклоняется, не «санируется»). Плюс структурная гарантия: содержимое задачи доходит до провайдера только путями.

### 5.11 `tool`-узлы и скиллы

- `tool`-узел запускается через тот же `run_process` (argv-список, обязательный таймаут), имя резолвится fail-closed в реестре `<repo>/.worc/tools/`: путь должен лежать внутри каталога (симлинк наружу отклоняется), быть обычным файлом и быть исполняемым. Программа получает на stdin **только** allowlist путей и `args` флоу — никогда сырой session id. Core **записывает** её `findings`/`data`, но никогда не применяет: нет пути, где возвращённое значение приводит к git- или state-записи.
- Скиллы поверхностятся **только как read-only пути**, никогда не исполняются; выбор provenance-closed — токен принимается лишь если он резолвится ровно в один **обнаруженный** скилл, так что имя не может внести файл, которого скан не нашёл сам. Чтение `SKILL.md` ограничено 262144 байтами.

### 5.12 Куда можно писать по политике вывода

Флоу с `output_policy` (например research/security-audit) резолвится в **один** репо-относительный каталог, куда пишущим узлам разрешено писать, плюс список обязательных файлов и признак «приватный» (такой результат не должен попасть в git — fail-closed на стейджинге).

### 5.13 Артефакты и редакция

Каждая запись под приватным и обменным корнями проходит через `assert_contained_path` — построенный путь с `..`, абсолютным, drive-relative или UNC-сегментом отклоняется до создания каталога. stdout/stderr/события редактируются **до** записи; `stdout`/`events` — построчно по декодированному JSON, чтобы редакция не ломала парсинг. Сырой session id живёт только в `state.db`, во всех артефактах — нормализованный токен `session:<12 hex>`.

---

## Что в этой конфигурации НЕ ограничено (честный список)

Аудит был бы неполным без обратной стороны. Ниже — то, что режим действительно отдаёт, и это принятая цена, а не недоделка.

1. **Config home провайдеров не защищён ничем.** `~/.claude` / `$CLAUDE_CONFIG_DIR`, `$CODEX_HOME` — ни write-деналя, ни read-деналя, ни при каком значении любого ключа, и настройки, которая вернула бы деналь, нет (решение владельца 2026-08-24). Там лежат учётные данные (`$CODEX_HOME/auth.json` — файл на диске) и конфигурация, которую CLI загрузит сам при следующем старте (хуки в `~/.claude/settings.json`, MCP-команды в `$CODEX_HOME/config.toml`). Причина решения: деналь ломал собственную механику провайдера — standalone-пакет Codex держит бинарь **внутри** `$CODEX_HOME`, и `apply_patch` не мог исполнить свой sandbox-helper, из-за чего не проходил ни один патч.
2. **Запись в каталог на `PATH`** — то есть право подменить исполняемый файл, который позже запустится **вне** песочницы. Пиннинг (5.3) закрывает только подмену **во время** прогона.
3. **Персистентность не удерживается**: `~/Library/LaunchAgents`, `~/.config/systemd/user`, rc-файл шелла доступны на запись; деналь `CronCreate`/`RemoteTrigger` этого не меняет.
4. **Публикация на любой адрес, кроме нашего `origin`, не удерживается и не детектируется.** Учётные данные подхватываются сами: `gh` читает свой `hosts.yml`, `git` по HTTPS — ваш `credential.helper`, по SSH — ваш ключ. Собранный вне клона репозиторий и запушенный куда угодно — ни предотвращается, ни замечается. Что вместо этого — мандат в промпте (5.1).
5. **Обычное состояние `origin` — рабочее состояние.** Ветка, появившаяся или сдвинувшаяся на `origin`, и PR на голове задачи не паркуют прогон и не сверяются с записями оркестратора. Вместо этого — восстановление: разошедшийся remote вливается локально, проверки прогоняются по объединению, и только потом что-то уходит. Практическое следствие: PR, который **вы** открыли на ветке задачи, будет переименован и дополнен, как любой другой.
6. **Сеть без фильтрации домена**, и это три поверхности, а не одна: песочница шелла, встроенные `WebFetch`/`WebSearch` (они через песочницу **не** проходят) и Codex-овский `web_search` (исполняется на бэкенде, вне профиля).
7. **Шелл может читать `.worc` (кроме env-файла).** Это цена `disable_read_isolation: true` у Claude: `denyRead` в файле песочницы пуст. Инструмент `Read` по-прежнему запрещён, env-файл закрыт через `credentials.files`, но `cat .worc/logs/...` из `Bash` ничем не блокируется. У Codex приватное множество остаётся `deny` при любом значении.
8. **Хуки, MCP и настройки проекта загружаются** (`--setting-sources project`, `trust_level="trusted"`) — то есть код из репозитория исполняется в процессе агента. Это прямое следствие `disable_read_isolation: true`.
9. **Все feature-поверхности Codex включены** (2.3), включая `memories` — персистентное хранилище вне нашей редакции и аудита.
10. **Неизвестный будущий инструмент Claude авто-одобряется**, потому что гейта существования нет и перечислить набор нельзя.
11. **На хосте без OS-песочницы пола нет вообще.** Нативная Windows; Linux/WSL2 без `bubblewrap`+`socat`. У Claude там `needs_sandbox` = false, файл `--settings` **не пишется** — остаются только инструментальные деняли, а любая команда шелла достаёт и `.git`, и `.worc`. `preflight` и лог прогона печатают это громкой строкой, и прогон продолжается.
12. **Дрейф не паркует** (5.9) — сообщается и продолжается.

---

## Сводная таблица

| Ограничение | Codex | Claude | Механизм | Что будет при попытке |
| --- | --- | --- | --- | --- |
| Full-access селекторы | нет | нет | `find_forbidden_args` ×3 слоя | `CONFIGURATION_ERROR` до запуска |
| Зарезервированные флаги | 28 имён | 40 имён | билдер argv | `CONFIGURATION_ERROR` до запуска |
| Ослабление режима прав | — | `auto` отклоняется | `_reject_weaker_permission_override` | `CONFIGURATION_ERROR` |
| `.worc`, `.worc/.env`, `.worc/runs` | `deny` (чтение+запись) | `Read`+4 редактора; `denyWrite`; `credentials` на env-файл | профиль / `--disallowedTools` / `--settings` | отказ песочницы или инструмента |
| `.worc-io`, `.git`, hooks, `tasks/` | `read` | 4 редактора + `denyWrite` | write-guard под каждую попытку | отказ записи, чтение разрешено |
| `.env`, `secrets/**` | `deny` | `Read(...)` | `denied_read_paths` | отказ |
| `read-only` узел не меняет клон | клон `read` | `denyWrite` на клон + 4 редактора | профиль / settings | отказ записи |
| Профиль эмитится всегда | да | — | `default_permissions="worc"` | без профиля попытка отклоняется |
| Доказательство песочницы до запуска | канарейка каждый раз | платная проба по опт-ину | `codex sandbox -P` | утечка → фатально; недоказуемо → предупреждение |
| Имена из `.worc/.env` | не пробрасываются | не пробрасываются | `build_child_env` | переменной просто нет |
| `git`/`gh` оркестратора | allowlist + скраб | allowlist + скраб | `build_orchestrator_env` | имя удаляется |
| Публикация оркестратором | мандат | мандат | промпт + гейты стейджинга + отпечаток | коммит с чужим путём отклоняется |
| Промпт в argv | никогда | никогда | stdin | содержимое задачи только путями |
| Таймаут | обязателен | обязателен | `run_process` | попытка убивается, дерево доказывается пустым |
| Типизированный вывод | `--output-schema` | `--json-schema` | схема из core | невалидный ответ не проходит контракт |

---

## Как проверить это у себя

1. `worc preflight` — в вашей конфигурации отчёт покажет примерно такие строки:

```text
isolation: OK (strict_isolation=false)
read-isolation: OFF (strict_isolation=false)
advanced-mode: ON (security.strict_isolation=false) — full freedom for the agent under the operator's responsibility, except the floor; guide/config/security.md says what that floor holds and what it does not
git-evidence: ON (security.allow_git_evidence=true) — inert under strict_isolation=false
```

Плюс `isolation-floor: NONE — …`, если на этом хосте OS-песочницы нет, и `write-grant: WARN — CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is set …`, если эта переменная выставлена (тогда грант записи на том не применяется так, как обещано).

2. Реальный argv любой попытки — в `.worc/logs/<task-id>/stages/<node-id>/run-<NNNNNN>/<attempt>-<provider>/request.json`. Это первое место, куда стоит смотреть, если спор идёт о том, что именно ушло провайдеру.
3. Файл песочницы Claude — `claude-sandbox-settings.json` в том же каталоге попытки. Результат канарейки Codex — `canary.json` рядом.
4. `worc preflight --paid-isolation-probe` — единственный инструмент, который отвечает на вопрос «держится ли `denyWrite`, вложенный в `allowWrite`, на **этой** машине». Стоит один вызов модели, поэтому не запускается неявно. Постраничные результаты — в `<private home>/preflight/claude-paid-isolation-probe.json`.

## Связанные документы

- [requirements-advanced-mode.md](requirements-advanced-mode.md) — контракт приёмки режима, требования ТA.1–ТA.9, отрицательные требования.
- [audit-follow-ups-am.md](audit-follow-ups-am.md) — открытые находки по блоку режима.
- [phase-am-5-ordinary-state-and-codex-home.md](phase-am-5-ordinary-state-and-codex-home.md) — решение про config home провайдеров и снятие парковок на обычном состоянии.
- [security.md](../../../src/wastech_orchestrator/packaged/guide/config/security.md) — операторская версия того же материала: четыре уровня пола и таблица ключей.
- [.agents/rules/security.md](../../../.agents/rules/security.md) — политика, которую этот аудит проверяет.

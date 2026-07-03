# Post-p0 robustness hardening: revive-finalize durability + doc-node capability policy

Status: **implemented** (2026-07-03) Date: 2026-07-03 Owner: Vladimir Makarevich

Реализовано 2026-07-03. **Решение A:** оба слоя отгружены — (a) громкая деградация (WARNING + пометка «Fallback summary» в теле) в `_engine_finalize`→`write_minimal_summary`, гейтится на DONE-путь (провальный терминал не помечается); (b) устойчивый re-синтез — `Supervisor._session_live` + `_finalize_digest` собирает дайджест из `supervisor_step`-строк `evaluations`, свежий finalize-турн без резюма мёртвой сессии (тот же один турн). **Решение B (V1):** documentation-узел в packaged `implementation.yaml` явно `network_access: false`; асимметрия enforcement (Codex hard vs Claude tools-only) + рекомендация `provider: codex` задокументированы в [configuration.md](../configuration.md). Ядро не тронуто, ветвления по имени узла нет.

Два отложенных hardening-решения, всплывшие в ходе p0-кампании на `wastech-mdlint` ([TEST-FINDINGS.md](../../TEST-FINDINGS.md)) при закрытии [test-findings-remediation-plan.md](test-findings-remediation-plan.md). Обе находки — про **мягкую гарантию, которая может молча деградировать**: (1) синтез итогового summary — best-effort и на revived-задаче падает в минимальный fallback; (2) скоуп documentation-узла держится на prompt-adherence, а не на capability-политике. Решения **независимы** и планируются по отдельности — сгруппированы в один ADR по общему источнику (p0-фидбэк) и общей теме («убрать тихую деградацию мягкой гарантии»). Ни одно не входило в объём v1.

## Общие инварианты (для обоих решений)

- **Супервизор `advisory by construction`** ([[supervisor-constant-layer]], [core/supervisor.py](../../src/wastech_orchestrator/core/supervisor.py)) — форсирован `read-only`, никогда не reworks/reroutes/blocks. Ни одно решение не даёт ему мутаций.
- **Best-effort контракт finalize/observe** — сбой надзорного слоя никогда не роняет задачу; summary производится ВСЕГДА (синтез или детерминированный минимум).
- **Ядро не знает синтаксис CLI** — любая capability-политика резолвится в провайдер-флаги только в `providers/`; граф/ядро оперируют абстракцией.
- **Hard security-ceiling неотключаем** (env-allowlist, запрет `bypassPermissions`/`--dangerously-*`, `cwd`=корень клона) — эти решения его не трогают и не ослабляют.
- **Greenfield, миграции нет** ([[greenfield-mvp-no-migration]]) — knobs/поля можно вводить без back-compat-машинерии; при необходимости — bump `CONFIG_SCHEMA_VERSION`.

---

## Решение A — устойчивый finalize на revived-задаче

### Проблема

Супервизор ведёт собственную durable-сессию (`resume_own_lineage`, персистится в `node_lineage` под `_SUPERVISOR_LINEAGE_NODE_ID`). На штатном прогоне пошаговые observe-турны накапливают контекст, и `finalize()` синтезирует богатый summary (тело PR) + follow-ups + memory-delta из тёплой сессии. На `rerun --continue` (revive) задача повторно входит с чекпоинта — но прерывающий стоп оставляет durable-сессию непригодной к резюму (провайдер не может её продолжить / она не была сохранена в нужной точке), поэтому финальный турн падает сразу, `_finalize_turn` возвращает пусто, и срабатывает **детерминированный минимальный fallback** оркестратора: тело PR деградирует в заглушку — **молча**.

Смежно с **F14** (закрыт): `rerun --continue` теперь доводит publish до PR после commit-фейла, но тело PR, которое он отгружает на revived-задаче, — деградированный fallback, а не реальный синтез. Recovery механически работает, качество артефакта тихо падает.

### Constraints

- Один LLM-турн на observe-шаг и один на finalize — не плодить турны на восстановлении (бюджетная дисциплина супервизора). (b) её соблюдает: тот же единственный finalize-турн, просто с другим входом.
- `finalize` уже best-effort и симметрично не трогает существующий непустой `summary.{md,json}` при провале (F16) — восстановление не должно это ломать. Следствие: (b) актуален только когда хорошего `summary.md` ещё нет (иначе он переиспользуется, и синтез не нужен).
- **Источник (b) безусловен.** Пошаговые наблюдения персистятся как immutable-строки `supervisor_step` в `evaluations` (`.worc/state.db`) через `record_evaluation` → `INSERT` (append-only, «audit + recovery») — на всегда-включённом бэкбоне state-машины, который в принципе нельзя отключить (от него зависят чекпоинты/resume). Он **не** гейтится `config.logging.level` / `--log-level` / `logging.artifacts` — те правят только человеческий operator-trace (stdlib `logging` → stderr/файл), а не таблицы БД ([observability/logging.py](../../src/wastech_orchestrator/observability/logging.py): «the machine-readable audit lives elsewhere (SQLite…)»). То есть источник гарантированно есть на revive, без живой сессии и независимо от настроек логирования. Строка пишется даже когда сам observe-турн упал (с `observation_failed: true`) — присутствие строк безусловно; открыт лишь вопрос их содержательности (см. Open questions).

### Alternatives considered

| Вариант | Оценка |
| --- | --- |
| (a) Принять fallback, но сделать деградацию **громкой** | Дёшево и честно: WARNING в лог «summary деградировал на revive», и пометка в самом summary.md, что это fallback, а не синтез. Ставит **пол** — оператор перестаёт получать заглушку, думая, что это полный разбор. |
| (b) Пересобрать вход finalize из записанных наблюдений | `finalize` запускает свежий турн, **seeded** дайджестом `supervisor_step`-строк из `evaluations` (не резюмируя мёртвую сессию). Делает синтез устойчивым к потере сессии. Дороже, но чинит качество, а не только видимость. |
| (c) Персистить дайджест-вход finalize на каждом шаге | Finalize становится stateless относительно сессии — читает накопленный дайджест. Больше кода/схемы; (b) даёт то же из уже имеющихся `evaluations`. |
| (d) Снять слепок finalize при падении в файл (`.worc`) и подложить на resume | Отвергнуто в пользу (b). Стреляет только при кооперативных стопах (при жёстком kill/OOM процесса нечего снять); пред-дайджест на падении теряет фиделити и описывает **незавершённую** задачу; дублирует то, что уже безусловно лежит в `evaluations`/`state.db`. Место хранения было выбрано верно (`.worc`, гитигнор, рядом с логами/памятью) — но (b) читает тот же `.worc/state.db`, так что отдельный артефакт избыточен. |
| Ничего не делать | Отвергнуто: тихая деградация тела PR на любом recovery — потеря value без сигнала. |

### Decision

**(a) как обязательный пол сейчас; (b) как quality-фикс сверху, если понадобится.** Это два слоя, а не «или/или»:

- **(a) срабатывает всегда** — детерминированный код без LLM: как только на revive настоящий summary не получен, включается видимая деградация (WARNING + явная пометка «fallback summary» в теле). Безусловная гарантия: оператор больше не принимает заглушку за полный разбор.
- **(b) — best-effort поверх (a).** На revive **не воскрешаем мёртвую сессию**, а собираем вход для finalize из строк `supervisor_step` в `.worc/state.db` (не крэш-снапшот — вариант (d) отвергнут) и гоним свежий finalize-турн. (b) срабатывает, только когда: (1) хорошего `summary.md` ещё нет; (2) LLM-турн физически можно поднять на resume; (3) он вернул осмысленный результат. Не выполнено любое — работает (a).

Ключевое: (b) **не зависит от живой сессии** (читает `state.db`), поэтому закрывает самый частый кейс деградации — сессия мертва, но провайдер/сеть живы (F14, mid-task стоп). Не срабатывает лишь там, где на resume не поднять вообще ничего (провайдер/сеть всё ещё лежат) — а там и падаем в (a). Устойчивый re-синтез (b) оправдан как отдельный шаг, если fallback-тело на recovery окажется частым и мешающим ревью. Супервизор остаётся advisory; восстановление ничего не мутирует помимо summary-артефакта.

### Open questions

- Точная причина непригодности сессии на revive: сессия не сохранена до стопа, или провайдер отклоняет резюм после прерывания? Менее критично для (b), чем считалось: (b) сессию не резюмит, поэтому от этой причины не зависит — но ответ уточняет, как часто fallback вообще случается (и, значит, приоритет (b)).
- **Содержательность, а не наличие.** Присутствие `supervisor_step`-строк гарантировано (см. Constraints); открытый вопрос — достаточно ли заметок observe для осмысленного re-синтеза, или они слишком куцые. Заметки, записанные ДО прерывания (тёплая сессия), богаты; после resume новые observe-турны на сломанной сессии могут падать → пустые строки. Рычаг, если куцых окажется много, — обогатить то, что observe кладёт в `supervisor_step`, а не крэш-снапшот.

### Implementation notes

- Шов: [core/supervisor.py](../../src/wastech_orchestrator/core/supervisor.py) `_resume_session` / `finalize` / `_finalize_turn`; оркестраторский хук `_engine_finalize` и путь минимального fallback в [core/orchestrator.py](../../src/wastech_orchestrator/core/orchestrator.py).
- (a): при пустом результате finalize на revive — `WARNING` (task_id, «summary degraded to fallback on revive») и детерминированный префикс/пометка в теле fallback-summary.
- (b): собрать дайджест из `store.get_evaluations(task_id)` (строки `supervisor_step`) и передать его в свежий finalize-турн как контекст вместо `session_id`. **Новый артефакт/файл не вводится** — источник уже лежит в `.worc/state.db`. Если re-синтез окажется куцым — сначала обогащать заметки observe (что кладётся в `supervisor_step`), а не менять механику восстановления.

---

## Решение B — capability-политика documentation-узла (F17b)

### Проблема

worc скоупит поведение узла **промптом** (role-файл «не ставь зависимости, не собирай» — это F17a, починен per-repo в mdlint). Но физически doc-агента ничто не держит: у него есть Bash и (в зависимости от провайдера) сеть. На **p0-04** documentation-узел прогнал `workspace:*` + `npm install` + откат — scope-drift, integrity-риск, живая сеть; вреда не было только потому, что модель сама аккуратно откатилась. Опора на adherence — не гарантия.

Ключевой нюанс существующего шва: per-node `network_access` УЖЕ есть ([core/flow/schema.py](../../src/wastech_orchestrator/core/flow/schema.py), резолв [contracts.py](../../src/wastech_orchestrator/core/flow/contracts.py) `resolve_network_access`), и в `implementation`-флоу (без `network_policy`) он и так резолвится в `False` для всех узлов. Но **enforcement асимметричен по провайдерам** ([providers/base.py](../../src/wastech_orchestrator/providers/base.py#L114)):

- **Codex:** `network_access=False` = OS-sandbox `workspace-write` без сети → `npm install` (нужна сеть) **физически заблокирован**. Твёрдая гарантия.
- **Claude:** `network_access` переключает лишь инструменты `WebFetch`/`WebSearch`, но **не песочит Bash** — `npm install` уходит в сеть через ОС, а не через WebFetch, и остаётся возможным.

То есть на Claude-primary (как у mdlint) `network: none` НЕ даёт твёрдого запрета на install — вот почему p0-04 и случился. Гарантия «doc-узел не поставит зависимости» достижима сегодня только через OS-sandbox Codex.

### Constraints

- Ядро не знает CLI — network/write-политика → провайдер-флаги в `providers/` (Codex `--sandbox`; у Claude нет сетевой песочницы — см. смежный [trust-levels-danger-approval.md](trust-levels-danger-approval.md), тот же residual-risk по out-of-repo Bash).
- `security.denied_commands` — НЕ твёрдый OS-блок: агент запускает команды внутри собственного Bash, worc не перехватывает его внутренний shell (denied_commands работает потому, что git/PR делает сам оркестратор, а не по перехвату).
- Doc-узлу всё ещё нужно запускать docs-форматтер (`npm run format`) — но он локальный (без сети), поэтому с `network: none` не конфликтует; отсутствие `node_modules` — забота оператора, не этого решения.

### Alternatives considered

| Вариант | Оценка |
| --- | --- |
| (a) Роутить documentation-узел на **Codex** с `network_access: false` | OS-sandbox физически блокирует install/сеть — твёрдая гарантия. Цена: навязывает провайдера узлу; на Claude-only сетапе неприменимо. |
| (b) Оставить prompt-only (F17a) | Отвергнуто как единственная мера: p0-04 показал, что adherence не гарантирована. Годится как слой, не как гарантия. |
| (c) Обобщённый per-node capability-блок (network + write-path scope) в схеме флоу | Чище и переиспользуемо (doc-узел — первый потребитель); но это фича, а не мелкий фикс, и упирается в тот же предел: hard-enforce network только там, где провайдер даёт песочницу. |
| (d) Per-node write-path allowlist (узел пишет только под `docs/`, `*.md`) | Ортогонально сети: ловит именно scope-drift «полез в код». Но enforce write-scope на уровне провайдера — та же провайдер-зависимость; на Claude только advisory. |

### Decision

**V1: `network_access: false` на documentation-узле (уже дефолт) + явно документированный residual-risk и рекомендация роутить doc-узел на Codex, когда нужна твёрдая гарантия** (вариант a как опция, не принуждение). На Claude узел остаётся prompt-скоупнутым (F17a) — это defense-in-depth, не гарантия; риск фиксируем письменно, как это сделано для out-of-repo Bash в trust-levels ADR. Обобщение до полноценного per-node capability-блока (c/d) — отдельная оценка, если появится второй потребитель.

**Инвариант: никакого хардкода к имени узла.** Всё это — generic per-node механизм, не логика ядра, привязанная к узлу с именем `documentation`. Конкретно: (1) `network_access` — это дефолтное значение generic-поля **в файле флоу** (данные оператора, которые он полностью контролирует и может переопределить/убрать), а не ветвление в ядре: в `core/`/`providers/`/`routing/` нет ни одного `if node.id == "documentation"` — резолв [contracts.py](../../src/wastech_orchestrator/core/flow/contracts.py#L96) `resolve_network_access` и применение [nodes/agent.py](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L545) одинаковы для всех agent-узлов. (2) В packaged `implementation`-флоу (без `network_policy`) этот узел **и так уже offline по общему дефолту** — `resolve_network_access(None, None)` резолвится в `False` для всех узлов; явная простановка лишь делает существующий дефолт видимым, ничего не ужесточая именно для doc-узла. (3) Сама «твёрдость» гарантии — свойство провайдера (Codex OS-sandbox), применимое к любому узлу с `network_access: false`, а не привилегия/ограничение имени `documentation`. Doc-узел здесь — просто первый _потребитель_ generic-механизма и пример в поставляемом шаблоне; кастомный флоу называет узлы как угодно и получает ровно то же поведение.

### Open questions

- **Enforceability на Claude.** Есть ли у Claude Code механизм запретить сетевой Bash (флаг/hook), или `network: none` там принципиально best-effort? Если нет — честно закрепить, что твёрдая гарантия = только Codex-роут.
- **Обобщать ли** до per-node `capabilities` (network + write-scope) вместо хардкода doc-узла — ждём второго кейса (YAGNI).
- **Write-scope (d)** нужнее сети? scope-drift в код опаснее живого install; возможно, приоритет — write-path allowlist, а не network.

### Implementation notes

- Шов network уже есть: per-node `network_access` ([schema.py](../../src/wastech_orchestrator/core/flow/schema.py)), резолв [contracts.py](../../src/wastech_orchestrator/core/flow/contracts.py#L96), маппинг на провайдера [providers/base.py](../../src/wastech_orchestrator/providers/base.py#L114), применение в [core/flow/nodes/agent.py](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L545).
- (a): в packaged/operator `implementation`-флоу закрепить documentation-узел `network_access: false` явно (сегодня — неявный дефолт) + при потребности в гарантии пиновать `provider: codex` на узле (per-node override уже поддержан, [[task-node-model-override-design]]). Обе правки — в **YAML-файле флоу** (данные, которые оператор владеет и правит), а не в ядре: ни один шов не ветвится по имени узла, поэтому любой узел любого кастомного флоу получает тот же generic-механизм.
- Документировать асимметрию enforcement (Codex hard vs Claude tools-only) в `docs/configuration.md` рядом с `network_policy`, чтобы оператор понимал предел гарантии.
- Смежный per-repo слой F17a (role-промпт «read-only к коду/build-state») уже отгружен в mdlint и в packaged `flows/implementation/documentation.md` — B его дополняет capability-стороной, не заменяет.

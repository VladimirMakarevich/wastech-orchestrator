# Post-p0 robustness hardening: revive-finalize durability + doc-node capability policy

Status: **proposed** (2026-07-03) Date: 2026-07-03 Owner: Vladimir Makarevich

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

- Один LLM-турн на observe-шаг и один на finalize — не плодить турны на восстановлении (бюджетная дисциплина супервизора).
- `finalize` уже best-effort и симметрично не трогает существующий непустой `summary.{md,json}` при провале (F16) — восстановление не должно это ломать.
- Пошаговые наблюдения уже персистятся как immutable-строки `supervisor_step` в `evaluations` — это доступный на revive источник, не требующий живой сессии.

### Alternatives considered

| Вариант | Оценка |
| --- | --- |
| (a) Принять fallback, но сделать деградацию **громкой** | Дёшево и честно: WARNING в лог «summary деградировал на revive», и пометка в самом summary.md, что это fallback, а не синтез. Ставит **пол** — оператор перестаёт получать заглушку, думая, что это полный разбор. |
| (b) Пересобрать вход finalize из записанных наблюдений | `finalize` запускает свежий турн, **seeded** дайджестом `supervisor_step`-строк из `evaluations` (не резюмируя мёртвую сессию). Делает синтез устойчивым к потере сессии. Дороже, но чинит качество, а не только видимость. |
| (c) Персистить дайджест-вход finalize на каждом шаге | Finalize становится stateless относительно сессии — читает накопленный дайджест. Больше кода/схемы; (b) даёт то же из уже имеющихся `evaluations`. |
| Ничего не делать | Отвергнуто: тихая деградация тела PR на любом recovery — потеря value без сигнала. |

### Decision

**(a) как обязательный пол сейчас; (b) как quality-фикс, если понадобится.** Сначала сделать деградацию видимой (WARNING + явная пометка «fallback summary» в теле), поскольку это дёшево и снимает главную боль — молчание. Устойчивый re-синтез из `evaluations` (b) — отдельный шаг, оправдан, только если fallback-тело на recovery окажется частым и мешающим ревью. Супервизор остаётся advisory; восстановление ничего не мутирует помимо summary-артефакта.

### Open questions

- Точная причина непригодности сессии на revive: сессия не сохранена до стопа, или провайдер отклоняет резюм после прерывания? Определяет, достаточно ли (a) или нужен (b).
- Достаточно ли `supervisor_step`-строк в `evaluations` как входа для осмысленного re-синтеза (b), или наблюдения слишком куцые.

### Implementation notes

- Шов: [core/supervisor.py](../../src/wastech_orchestrator/core/supervisor.py) `_resume_session` / `finalize` / `_finalize_turn`; оркестраторский хук `_engine_finalize` и путь минимального fallback в [core/orchestrator.py](../../src/wastech_orchestrator/core/orchestrator.py).
- (a): при пустом результате finalize на revive — `WARNING` (task_id, «summary degraded to fallback on revive») и детерминированный префикс/пометка в теле fallback-summary.
- (b): собрать дайджест из `store.get_evaluations(task_id)` (строки `supervisor_step`) и передать его в свежий finalize-турн как контекст вместо `session_id`.

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

### Open questions

- **Enforceability на Claude.** Есть ли у Claude Code механизм запретить сетевой Bash (флаг/hook), или `network: none` там принципиально best-effort? Если нет — честно закрепить, что твёрдая гарантия = только Codex-роут.
- **Обобщать ли** до per-node `capabilities` (network + write-scope) вместо хардкода doc-узла — ждём второго кейса (YAGNI).
- **Write-scope (d)** нужнее сети? scope-drift в код опаснее живого install; возможно, приоритет — write-path allowlist, а не network.

### Implementation notes

- Шов network уже есть: per-node `network_access` ([schema.py](../../src/wastech_orchestrator/core/flow/schema.py)), резолв [contracts.py](../../src/wastech_orchestrator/core/flow/contracts.py#L96), маппинг на провайдера [providers/base.py](../../src/wastech_orchestrator/providers/base.py#L114), применение в [core/flow/nodes/agent.py](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L545).
- (a): в packaged/operator `implementation`-флоу закрепить documentation-узел `network_access: false` явно (сегодня — неявный дефолт) + при потребности в гарантии пиновать `provider: codex` на узле (per-node override уже поддержан, [[task-node-model-override-design]]).
- Документировать асимметрию enforcement (Codex hard vs Claude tools-only) в `docs/configuration.md` рядом с `network_policy`, чтобы оператор понимал предел гарантии.
- Смежный per-repo слой F17a (role-промпт «read-only к коду/build-state») уже отгружен в mdlint и в packaged `flows/implementation/documentation.md` — B его дополняет capability-стороной, не заменяет.

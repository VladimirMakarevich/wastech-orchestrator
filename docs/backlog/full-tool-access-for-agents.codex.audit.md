# Актуализированный аудит: OpenAI Codex и стык с Claude-адаптером

Date: 2026-08-05

Аудит актуализирован после правок в [`full-tool-access-for-agents.md`](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md). Исходные несущие ошибки Шагов 2–3 исправлены: задача теперь правильно описывает filesystem precedence Codex, внешний `write`, `:minimal`/`extends`, доменную сеть и `network_proxy`. Операторские опт-ины не блокируются новыми prerequisite: их неэквивалентные гарантии указаны как принятая цена.

Итог: задача готова служить планом реализации со стороны Codex/Claude-стыка. Остались три точности текста и набор live-проб; они не возвращают запреты, которые противоречат решению владельца.

## 1. Шаг 2: внешний путь можно выдать в `write`

**Утверждение задачи.** Прямой абсолютный `write` вне workspace поддерживается и может расширить `extends = ":read-only"`: [задача:406](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:406).

**Вердикт: верно; исправлено.** Permission grammar принимает абсолютные и `~`-относительные пути. Ранее выполненная no-model проба на `codex-cli 0.144.4` дала `exit=0` и создала файл вне workspace. Текущий генератор уже строит прямые пути: [`codex_profile.py:133`](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/providers/codex_profile.py:133).

**Что поправить.** В тексте ничего. В коде добавить проекцию `extra_writable_paths` и покрыть её live-пробой.

## 2. Шаг 2: приоритет filesystem-правил

**Утверждение задачи.** Сначала побеждает более специфичный путь; лишь при равной специфичности действует `deny > write > read`: [задача:414](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:414).

**Вердикт: верно; исправлено.** Пример `deny ~/Documents` + `write ~/Documents/codex` открывает дочерний каталог. Живая проба подтвердила: запись в дочерний путь успешна, в sibling — запрещена. Задача теперь требует двунаправленную canonical-overlap валидацию с учётом symlink/junction/reparse, регистра, UNC и drive aliases: [задача:413](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:413).

**Что поправить.** В задаче ничего. В реализации исправить докстринг и комментарий «deny last», которые пока всё ещё обещают безусловный приорит deny: [`codex_profile.py:124`](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/providers/codex_profile.py:124), [`codex_profile.py:142`](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/providers/codex_profile.py:142).

## 3. `:minimal` и `extends`

**Утверждение задачи.** `extends` наследует всю встроенную базу, а `:minimal` — платформо- и runtime-зависимый набор, не стабильный allow-list: [задача:406](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:406).

**Вердикт: верно; исправлено.** Официальный контракт не фиксирует точный список `:minimal`. `:danger-full-access` наследовать нельзя.

**Что поправить.** Ничего. Не делать acceptance assertions на точном составе `:minimal`.

## 4. Шаг 3: у Codex есть доменная гранулярность

**Утверждение задачи.** Permission profiles поддерживают `network.enabled` и `network.domains`, а адаптер должен включить `network_proxy`: [задача:450](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:450), [задача:454](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:454).

**Вердикт: верно; исправлено.** Поддерживаются exact host, `*.example.com`, `**.example.com` и allow-only `*`; в сетевых правилах deny побеждает allow. На текущем хосте `codex features list` по-прежнему показывает `network_proxy experimental false`. Ранее выполненная no-model проба показала: без feature профиль пропустил и allowed, и unlisted host; с `features.network_proxy.enabled=true` unlisted host получил CONNECT 403.

**Что поправить.** В тексте ничего. В коде сделать четыре пункта из [задача:456](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:456): проекция domains, provider-owned feature enable, capability probe и paired canary.

## 5. `allowed_domains` не ограничивает весь Codex-узел

**Утверждение задачи.** Domain policy ограничивает локальные sandboxed commands, но не backend-side `web_search`: [задача:452](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:452).

**Вердикт: верно; исправлено.** Текущий адаптер добавляет `web_search="disabled"` только при `network_access=false`: [`codex.py:466`](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/providers/codex.py:466).

**Что поправить.** При доменно-ограниченном гранте либо отключать native web tools, либо сохранить уже добавленное явное предупреждение.

## 6. Codex `read-only` уже запускает команды

**Утверждение задачи.** У адаптера Codex нет hard allow-list имён локальных команд; `read-only` уже может запускать `jq` и `git log`, а эффекты ограничивает permission profile: [задача:47](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:47).

**Вердикт: верно; исправлено.** Живые пробы дали `readonly_git exit=0`, `readonly_jq exit=0`, `readonly_workspace_write exit=1`. Запрет мутации задаёт наследуемый `:read-only`, а не только точечное workspace-правило.

**Что поправить.** Ничего.

## 7. `denied_commands` и гарантия публикации

**Утверждение задачи.** В разделе инвариантов всё ещё написано: «ни один шаг не даёт агенту публиковать; `denied_commands` остаётся полом»: [задача:90](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:90). Шаг 3 и реестр одновременно честно фиксируют, что на Codex `denied_commands` не проецируется и `push`/вызов API на allowed host не удерживается эквивалентно Claude: [задача:461](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:461), [задача:529](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:529).

**Вердикт: формально противоречиво, но расхождение теперь не скрыто и не блокирует цель.** В Claude `denied_commands` рендерится как `Bash(...)`: [`claude.py:350`](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/providers/claude.py:350). Codex его не получает; сегодня `commit` удерживает запрет записи в `.git`, а `push` — выключенная command network. После сетевого опт-ина гарантии провайдеров не эквивалентны. Паттерны Claude тоже обходимы через wrapper/второй shell.

**Что поправить.** Не добавлять новый stop-gate. Вне неизменяемой секции «Цель» не называть провайдерские гарантии эквивалентными; сохранить preflight-предупреждение, реестр и characterization-тест на безопасном test remote.

## 8. Канарейка для расширенного `write`

**Утверждение задачи.** Для каждого внешнего пути обязательна positive write/delete probe, а реальный список передаётся и в per-attempt canary, и в capability smoke: [задача:418](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:418).

**Вердикт: верно; исправлено.** Текущая canary проверяет private/exchange границы, но не знает о внешних writable paths: [`codex_canary.py:189`](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/providers/codex_canary.py:189). На read-isolation ON уже есть четыре `codex sandbox` subprocess на попытку; каждый внешний путь добавит ещё как минимум один.

**Что поправить.** В тексте ничего. В коде реализовать per-path probes и observability стоимости.

## 9. Native Windows и реальный `CODEX_HOME`

**Утверждение задачи.** Реальный `CODEX_HOME` на Windows сохраняется, но это не доказывает, что любая policy исполнима на unelevated backend: [задача:49](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:49).

**Вердикт: верно; исправлено.** Real-host smoke классифицирует restricted reads на unelevated Windows как `CAPABILITY_UNAVAILABLE`: [`test_codex_canary_smoke.py:110`](/Users/a1234/Documents/GitHub/wastech-orchestrator/tests/providers/test_codex_canary_smoke.py:110). Формулировка про конкретный capability SID не вошла в задачу, потому что публичный контракт её не подтверждает.

**Что поправить.** Ничего в тексте. Перед реализацией закрыть elevated/unelevated live-матрицу из [задача:554](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:554).

## 10. Шаг 1, router/fallback и сравнимость

**Утверждение задачи.** При правильном порядке `allow_unsandboxed_shell=false/true` не создаёт ложный `CAPABILITY_UNAVAILABLE`; fallback сравнивает effective request profile, а не provider defaults: [задача:314](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:314), [задача:326](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:326).

**Вердикт: частично верно; маршрутизация исправлена, одна фраза осталась неверной.** Router сохраняет prompt, paths, schema, permission и network flag, но сбрасывает provider-specific model, часть reasoning, `extra_args` и session: [`router.py:584`](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/routing/router.py:584). Текущий gate всё ещё берёт profile из provider config: [`router.py:621`](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/routing/router.py:621). Задача верно говорит, что результаты несравнимы, но одновременно называет fallback «security-equivalent заменой»: [задача:328](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:328). После явно принятого расхождения `denied_commands`, native web tools и network guarantees это недоказуемо.

**Что поправить.** Заменить `security-equivalent` на «сохраняет portable request profile, но не гарантирует эквивалентную security/tool/model semantics». Это точность текста, а не причина запрещать fallback.

## 11. Шаг 3 и Codex как fallback-цель

**Утверждение задачи.** Validator должен проверять `codex_allow_network_writes` для любого маршрута, где Codex может быть primary или fallback: [задача:463](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:463).

**Вердикт: верно; исправлено.** Текущий validator видит только directly resolved Codex, хотя Router сохраняет `permission_profile` и `network_access` при cross-provider fallback: [`validator.py:533`](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/core/flow/validator.py:533), [`router.py:584`](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/routing/router.py:584).

**Что поправить.** Ничего в тексте; реализовать route-aware validation и тест обеих сторон пары.

## 12. `tool_policy` на Codex не влияет

**Утверждение задачи.** Ключ остаётся `security.tool_policy`, но прямо описан как provider-specific rollback Claude и no-op на Codex: [задача:324](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:324).

**Вердикт: верно; расхождение больше не замалчивается.** У Codex нет такого hard tool-list control. Descoped Codex-проекция `denied_commands` остаётся отдельной асимметрией, которая явно внесена в Шаг 3 и реестр.

**Что поправить.** Не переименовывать ключ. Реализовать точную формулировку в схеме, гайде и preflight; тест должен показывать, что переключение меняет Claude argv и не меняет Codex argv.

## 13. `danger-full-access`

**Утверждение задачи.** Один ранний `return` выбрасывает permission profile, canary и соседнюю config/tool-surface isolation, но не все защитные механизмы провайдера: [задача:546](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:546).

**Вердикт: верно; исправлено.** Ранний return: [`codex.py:371`](/Users/a1234/Documents/GitHub/wastech-orchestrator/src/wastech_orchestrator/providers/codex.py:371). Теряются filesystem/network profile, `default_permissions`, `--ignore-user-config`, untrusted project, отключения hooks/multi-agent/computer/browser/apps/plugins и per-attempt canary. Сохраняются `--ask-for-approval never`, allowlisted env, prompt/stdin isolation, redaction, process quiescence и отдельное `web_search="disabled"` для offline request. Escape может сделать работоспособными операции, которым мешала песочница; задача правильно не использует его из-за несоразмерной ширины, а не из-за мнимого отсутствия функциональной пользы.

**Что поправить.** Ничего.

## Что осталось непроверяемым офлайн

- **Native Windows elevated/unelevated.** На обоих backend с реальным `CODEX_HOME` прогнать private/exchange probes, direct external write, parent-deny/child-write, domain allow/unlisted и publication characterization. Ожидаемый текущий результат restricted reads на unelevated — `CAPABILITY_UNAVAILABLE`, но его надо записать с версией CLI и backend.
- **Claude PowerShell.** Безвредной live-пробой проверить scoped `PowerShell(...)`, проекцию `denied_commands` и то, проходит ли сам инструмент через POSIX sandbox. Пока это не доказано, фраза «на POSIX его удерживает песочница» в [задача:285](/Users/a1234/Documents/GitHub/wastech-orchestrator/docs/backlog/full-tool-access-for-agents.md:285) имеет вердикт **недоказуемо**.
- **Claude filesystem precedence.** Живой пробой установить приоритет `allowWrite` и `denyWrite`; конфигурационная overlap-валидация не должна зависеть от результата.
- **Сеть и canary после реализации.** Fake-CLI докажет wiring, но не enforcement. Нужны paired allow/unlisted probes на фактическом provider/backend.
- **End-to-end fallback.** После реализации ветки `allow_unsandboxed_shell` прогнать матрицу missing-deps/native-Windows × true/false, проверить effective-profile comparison, число запусков Codex canary и несравнимость результатов.

Актуально на этом хосте: `codex-cli 0.144.4`, `claude 2.1.222`; `network_proxy` — `experimental false`. No-model filesystem/network probes и real-host Codex canary были выполнены в предыдущем проходе. В этом проходе повторно проверены версии CLI и feature state; повторный model-free sandbox probe не запускался.

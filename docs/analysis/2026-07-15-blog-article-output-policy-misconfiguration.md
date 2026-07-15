# Post-mortem: blog_article — output_policy запрещает запись именно туда, куда велит задача

**Задача:** `blog-happy-in-my-misfortunes-3` (ветка `docs/blog-happy-in-my-misfortunes-3`)
**Целевой репозиторий:** `C:\Users\Vladimir Makarevich\Obsidian\WastimeApp`
**Прогон:** run-000019 (stage `draft`, нода `1-codex` → fallback `2-claude`)
**Финальный статус:** `manual_action_required`
**Дата прогона:** 2026-07-14 22:50–23:03 UTC
**Анализ:** только чтение артефактов + исходников оркестратора. Правки не вносились.

---

## Вердикт (кратко)

Это третья попытка одной и той же задачи (`blog-happy-in-my-misfortunes`, `-2`, `-3`). В третьей
`draft` впервые реально дописал статью — и сразу после этого прогон встал, потому что флоу
`blog_article.yaml` объявляет `output_policy: repository_document`, а этот идентификатор в движке
жёстко резолвится в «писать можно только в `docs/research/<task_id>/`, и там обязаны появиться
`report.md` + `sources.json»`. Задача же (правильно) требует создать файл в `blog/...` — путь,
заведомо лежащий вне этой директории. Любой успешный `draft` в этом флоу обязан упереться в guard:
это не сбой агента и не сбой конкретного прогона, а несовместимость декларации флоу с тем, что флоу
должен производить.

Черновик статьи никуда не делся — он на диске, untracked, полностью готов к публикации после
исправления конфигурации.

---

## Что произошло (таймлайн)

| Нода | Провайдер | Итог | Что произошло на самом деле |
|---|---|---|---|
| context | claude | ✅ | Скаут-бриф собран, норма |
| research | claude | ✅ | Внешний ресёрч собран, норма |
| **draft** | **codex gpt-5.4 xhigh → fallback claude** | **codex ❌ `permission_denied`, claude ✅** | Codex упал на Windows-песочнице (`CreateProcessWithLogonW failed: 2`), корректно распознан как инфра-ошибка, роутер переключился на Claude, который дописал и **реально создал**  `blog/20260714-happy-in-my-misfortunes-(EN).md` |
| — | — | → `manual_action_required` | После записи сработал after-stage output-containment guard: путь вне `docs/research/blog-happy-in-my-misfortunes-3/` → `NodeManualRequired` |

Флоу так и не дошёл до `length` / `tone_style` / `polish` / `publish` — guard стоит сразу после
агент-ноды, до перехода на следующий узел графа.

Ключевые доказательства:

- Точный текст ошибки (совпадает с тем, что вернул оркестратор пользователю):
  [`agent.py:413-417`](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L413)
  ```
  agent node 'draft': output_policy 'repository_document' confines writes to
  'docs/research/blog-happy-in-my-misfortunes-3'; refusing changes outside it:
  ['blog/20260714-happy-in-my-misfortunes-(EN).md']
  ```
- Резолюция `repository_document` жёстко прибита к `docs/research`:
  [`output_policy.py:25,60-66`](../../src/wastech_orchestrator/core/flow/output_policy.py#L60)
  ```python
  _RESEARCH_DIR = "docs/research"
  ...
  if policy is OutputPolicy.REPOSITORY_DOCUMENT:
      return ResolvedOutputPolicy(
          policy=policy,
          report_subdir=f"{_RESEARCH_DIR}/{task_id}",
          required_files=("report.md", "sources.json"),
          private=False,
      )
  ```
- Файл реально создан и лежит в рабочем дереве (не потерян):
  `C:\Users\Vladimir Makarevich\Obsidian\WastimeApp\blog\20260714-happy-in-my-misfortunes-(EN).md`
  (`git status --short` → `?? blog/20260714-happy-in-my-misfortunes-(EN).md`), содержание —
  полноценная, публикуемая статья, отвечающая брифу.

---

## Находки

### F1 — `output_policy: repository_document` несовместим с целью кастомного флоу `blog_article` (root cause)

- **Категория:** flow config / целевой репозиторий · **Severity:** высокая · **Confidence:** высокая
  (подтверждено кодом резолвера + фактическим guard-срабатыванием)
- **Симптом:** любой успешный `draft` в этом флоу гарантированно ловит
  `NodeManualRequired`, как только пишет туда, куда указывает задача (`blog/...`).
- **Доказательства:**
  - Флоу [`blog_article.yaml:12,28`](../../../../Obsidian/WastimeApp/.worc/flows/blog_article.yaml#L28)
    сам поясняет комментарием намерение автора: «output_policy: repository_document — the deliverable
    is a brand-new document, not a code change» — то есть значение выбрано по смыслу английской фразы
    («это документ, а не код»), а не по тому, что оно реально резолвит в движке.
  - `blog_article` — **не пакетный** флоу оркестратора: `grep -r blog_article
    src/wastech_orchestrator/packaged/flows` не находит ничего. Это кастомный флоу, написанный для
    репозитория WastimeApp, и он не проходил через тесты/ревью core-флоу.
  - Единственный флоу, который реально предназначен под `repository_document`, — `deep_research` /
    `security_audit` (см. [`docs/flow-authoring.md:81`](../../docs/flow-authoring.md#L81):
    `output_policy` = `code_change | repository_document | private_control_workspace_report`, и
    `repository_document` требует `report.md` + `sources.json`, чего блог-флоу никогда не производит).
- **Корневая причина:** имя enum-значения (`repository_document`) читается как общее описание
  («документ в репозитории»), но механически — это узкий контракт «отчёт с двумя конкретными
  файлами в одной конкретной директории». Автор кастомного флоу опирался на название, а не на
  резолвер.
- **Рычаг (точечно, целевой репозиторий, не оркестратор):**
  `.worc/flows/blog_article.yaml` → заменить
  ```yaml
  output_policy: repository_document
  ```
  на
  ```yaml
  output_policy: code_change
  ```
  Проверено, что это безопасно:
  - `output_policy` и `publishing` — независимые скалярные поля
    ([`schema.py:233-235`](../../src/wastech_orchestrator/core/flow/schema.py#L233)), схема не требует
    их согласованности; `code_change` + `publishing: documentation_pull_request` — легальная пара.
  - При `code_change` вместо директорийного guard действует dangerous-diff guard
    ([`dangerous_diff.py`](../../src/wastech_orchestrator/core/dangerous_diff.py)): новый `.md` под
    `blog/` не удаление, не lock-файл зависимости и (по умолчанию) не `protected_paths` — пройдёт без
    дополнительных одобрений, ровно как задумано в собственном ограничении флоу («Create only the new
    article file under `blog/`; do not touch existing posts»).
- **Scope:** конфигурация кастомного флоу в целевом репозитории (`WastimeApp/.worc/flows/`), не код
  оркестратора.
- **Ожидаемый эффект:** `draft` дописывает статью в `blog/...`, guard больше не блокирует, флоу
  доходит до `length` → `tone_style` → `polish` → `publish` как задумано.

> Возможное усиление на стороне оркестратора (не обязательное для фикса, но снижает риск повтора
> для будущих кастомных флоу): значение `repository_document` могло бы называться менее обманчиво
> (например, `research_report_bundle`) или схема могла бы предупреждать/фейлить валидацию флоу, если
> `required_files` заведомо не будут покрыты ни одним нодом флоу. Пока это не заводил отдельным
> backlog-айтемом — если пригодится, могу оформить.

### F2 (для контекста, не новая) — Windows sandbox codex `CreateProcessWithLogonW failed` — уже исправлено, подтверждено работающим

- В прогонах `-1` и `-2` та же статья вообще не записывалась: codex падал на этой же Windows-песочнице,
  но ложно засчитывался как `succeeded`, fallback на Claude не срабатывал, и `length_fix`-цикл
  крутился вхолостую (`no_file_change`). Разобрано в
  [`2026-07-14-codex-windows-sandbox-false-success.md`](2026-07-14-codex-windows-sandbox-false-success.md)
  (F1) и backlog
  [`p2-codx-014-tighten-windows-failure-detection.md`](../backlog/codex-provider-improvements/p2-codx-014-tighten-windows-failure-detection.md).
- В прогоне `-3` это уже не воспроизводится как «ложный успех»: `_HELPER_LAUNCH_FAILED_PATTERN`
  ([`codex.py:88-95`](../../src/wastech_orchestrator/providers/codex.py#L88)) теперь содержит
  `CreateProcessWithLogonW failed`, поэтому та же ошибка была честно классифицирована как
  `permission_denied`, и роутер корректно переключился на Claude. **Фикс CODX-014 подтверждён
  рабочим на реальном инциденте.**
- Отдельно подтверждено, что и F4 из того же пост-мортема (роль `draft.md` подсовывала неверный
  `{task_path}` вместо реального пути статьи) тоже уже почищена: текущий
  [`.worc/flows/blog_article/draft.md:1`](../../../../Obsidian/WastimeApp/.worc/flows/blog_article/draft.md#L1)
  велит «Create the file... at the path the task specifies», без инъекции `{task_path}`.
- **Почему это здесь важно:** именно потому что F1/F4 из прошлого пост-мортема почищены, конвейер
  впервые дошёл до реальной записи файла — и тем самым впервые проявил F1 из **этого** отчёта
  (`output_policy`), который в `-1`/`-2` был замаскирован пустым диффом. Основной хостовый дефект
  Windows-песочницы (`seclogon`/`CreateProcessWithLogonW`, F2 из прошлого отчёта) на этой машине,
  судя по всему, никуда не делся — codex по-прежнему не может писать напрямую, — но теперь это не
  фатально: фолловер на Claude отрабатывает исправно.

---

## Что уже сделано хорошо

- Роутер/фолловер codex → claude для инфра-ошибок отработал ровно так, как спроектирован.
- `context` и `research` — чистые, качественные артефакты.
- Сам черновик статьи, дописанный Claude, — полноценный, публикуемый текст: держит выбранную мысль,
  честно проговаривает контраргумент, не превращается в мотивационный список, упоминание Wastime —
  одно и к месту.
- Ничего не потеряно: guard сработал ровно как задуман (fail-closed), файл остался на диске, задача
  корректно припаркована в `manual_action_required`, а не потеряна молча.

---

## Рекомендованный порядок действий

1. **F1** (целевой репозиторий, один флоу): в `WastimeApp/.worc/flows/blog_article.yaml` заменить
   `output_policy: repository_document` → `output_policy: code_change`.
2. Возобновить `blog-happy-in-my-misfortunes-3` (`rerun --continue` или эквивалент) — черновик уже на
   диске, флоу должен подхватить его и пройти `length` → `tone_style` → `polish` → `publish` без
   повторной генерации.
3. (Опционально, отдельным тикетом) обсудить с владельцем оркестратора, стоит ли переименовать/лучше
   задокументировать `repository_document`, чтобы будущие кастомные флоу не выбирали его по смыслу
   имени вместо факта резолюции.

<!-- Everything this task deliberately does NOT cover. A scope fence: it prevents creep and records
     decisions so nobody re-opens them mid-build. Be explicit, and a little generous, here. -->

# Out of scope — {{TASK_TITLE}}

## Not in this task

<!-- Concrete things a reader might assume are included but aren't. One line each, with the reason. -->

- **`<Thing>`** — `<why it's excluded / where it lives instead>`.

## Deferred (maybe later, not now)

<!-- Real future work, parked intentionally. If it deserves to outlive this folder, it belongs in the
     backlog index as its own item once this one lands. -->

- **`<Thing>`** — deferred because `<reason>`; revisit when `<trigger>`.

## Explicit non-goals

<!-- Directions we are choosing NOT to go. This repository is a greenfield MVP with no deployed
     installs, so migration and back-compat machinery is a non-goal unless the task names it. -->

- We will **not** `<generalize / abstract / add a config knob for>` `<X>`, because only one case exists today.
- We will **not** add migration or back-compat scaffolding for `<Y>` — greenfield; the schema bump is the whole story.

# 05. Recommended memory tiers

[Previous](./04-recommended-architecture-specifically-for-wastech-orchestrator.md) | [Next](./06-recommended-read-write-lifecycle.md)

### 5.1 Tier overview

| Tier | Что хранить | Чего не хранить | TTL / aging | Retrieval default |
| --- | --- | --- | --- | --- |
| Short-term episodic | task outcomes, recent findings, temporary hypotheses, failed approaches, changed entities | raw transcripts, full diffs, secrets, external web facts as durable truth | 14-45 days by default | planning, implementation, fixing |
| Long-term semantic/procedural | stable conventions, reviewer lessons, recurring failure patterns, architectural gotchas with evidence | one-off task trivia, unstable branch facts, duplicate docs prose | no TTL, but periodic validation | planning, review, fixing |
| Entity memory | facts tied to files/modules/tests/dependencies/owners, plus links and hotspots | free-form giant summaries, unverifiable entity claims | validate on touch + periodic sweep | implementation, review, fixing |

### 5.2 What to store

#### Short-term episodic

Хранить:

- concise task summary;
- files/modules touched;
- review findings and whether they were fixed;
- failing checks and eventual fix pattern;
- operator/HITL decisions that matter for future similar tasks;
- rejected approaches, если они likely to recur soon.

Не хранить:

- full `stdout`, full review transcript, raw chat, full patch;
- details without future reuse value.

#### Long-term lessons

Хранить:

- stable repo conventions not already captured in `AGENTS.md`/docs;
- fragile areas ("changing X usually requires Y and Z");
- recurring review rules;
- dependency-specific gotchas;
- stable failure signatures and their canonical remedy;
- "how to navigate this repo" facts only if they are both durable and not already better documented elsewhere.

#### Entity memory

Хранить:

- entity id (`path:...`, `module:...`, `test:...`, `dep:...`);
- type and aliases;
- bounded-context label;
- hotspots / fragility flags;
- key relationships (`calls`, `depends_on`, `owned_by`, `validated_by_tests`);
- linked long-term facts;
- provenance pointers;
- last validated commit/time.

### 5.3 What not to store

Нельзя хранить:

- secrets or tokens;
- raw provider sessions or chain-of-thought-like reasoning traces;
- repo-wide prose restatement of docs that already exist;
- low-confidence facts without evidence;
- facts learned only from external web search unless separately code-validated;
- anything that can weaken security policy or routing decisions;
- agent-generated procedural instructions that become executable automatically.

Последний пункт особенно важен для WORC: procedural memory должна оставаться advisory, пока не была явно promoted человеком в `AGENTS.md`, role prompt или repo skill. Иначе memory становится stealth control plane.

### 5.4 Promotion rules

Рекомендованные rules for `promote to long-term`:

1. entry has at least one local evidence pointer to repo/docs/artifacts;
2. entry is not contradicted by current code/docs validation;
3. one of:
   - observed in `>=2` tasks within `60` days;
   - reviewer/HITL explicitly marked it as stable/reusable;
   - it prevented or explained a failed test/review in a way likely to recur;
   - it annotates a stable entity hotspot seen across commits/tasks;
4. entry summary can be stated in one small, repo-specific sentence;
5. source trust is `internal_validated`, not `external_only`.

### 5.5 Eviction / pruning rules

Рекомендованные rules for `drop as stale`:

- episodic record older than retention window and never promoted;
- entity fact references file/symbol that no longer exists and could not be remapped;
- lesson contradicted by two consecutive validations against current code/docs;
- failure pattern has had no hits for `N` releases/tasks and validation says obsolete;
- duplicate superseded by newer canonical entry with same subject/fact.

### 5.6 Conflict handling

Не удалять конфликтующие факты silently. Делать так:

1. new conflicting fact goes to `quarantine/pending/`;
2. validator checks code/docs evidence;
3. if old fact false -> old fact gets `superseded_by`, new fact promoted;
4. if unresolved -> both remain, but old one marked `disputed`, new one stays quarantined;
5. all steps logged in `audit/mutations.jsonl`.

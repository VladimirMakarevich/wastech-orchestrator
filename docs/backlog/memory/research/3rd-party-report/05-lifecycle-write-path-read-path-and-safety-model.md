# 05. Lifecycle, write path, read path, and safety model

[Previous](./04-recommended-architecture-for-wastech-orchestrator.md) | [Next](./06-evaluation-plan-evolution-path-and-final-recommendation.md)

### Recommended read and write lifecycle

**Planning stage.**  
На planning supervisor должен собирать `planning packet` из трёх источников, в таком порядке приоритета: current task artifacts and issue text; derived repo map/symbol hits for likely-in-scope paths; then a handful of relevant memory records. Planning packet должен содержать **краткий repo profile**, top entity cards, 3–7 high-value long-term records, и ссылки на deeper files. Это соответствует progressive disclosure patterns in Codex skills and Claude memory topic files, а также помогает не повторять ContextBench-style over-retrieval. citeturn18view2turn19view1turn7search0

**Implementation stage.**  
Implementation packet должен быть уже уже, чем planning packet. Агенту обычно нужны touched-path entity cards, precise commands, known fragile areas, and failure memories relevant to touched paths. На этом этапе наиболее важно взаимодействие memory с code search and symbol index: memory should bias where to look, but source of truth remains the live codebase. Sourcegraph’s context-engineering material and Cody retrieval work strongly support this emphasis on repo-aware retrieval rather than raw memory dump. citeturn23view1turn23view2

**Review stage.**  
Review packet должен извлекать **reviewer memory** and **failure memory** по touched paths and categories. Здесь memory often yields the largest marginal gain, потому что reviewer patterns and recurring misses are exactly the type of knowledge that rediscovery wastes time on. В production tools GitHub and Codex increasingly use specialized review/explore/task agents rather than one generic agent for everything; WORC should mimic that by stage-specific retrieval, even if provider is the same. citeturn26view2turn18view5

**Fixing stage.**  
Fixing packet должен быть самым причинно-ориентированным: конкретные review findings, nearest historical precedents, matching failure signatures, and exact verification commands. Здесь важна не общая repo memory, а **memory filtered by finding category, path, and stage**.

**Write at task finalization.**  
Основной write path должен запускаться **после** review/fixing and final summary. Supervisor extracts candidate memories from merged evidence: plan, implementation summary, checks, review findings, supervisor summary, and touched paths. Затем отдельный promotion pipeline делает type assignment, dedup, conflict detection, redaction, trust labeling and promotion into long-term/entity layers. LangChain/LangMem docs прямо подчеркивают trade-off hot-path versus background writes; для WORC background style is the right default. citeturn28view0turn28view1

**Cleanup and bounded autodream between tasks.**  
“Autodream” для WORC я бы не реализовывал как свободное autonomous memory writing. Лучший вариант — **bounded reconciliation job** между задачами, с жёстким budget и no network, который умеет:
- deduplicate near-duplicates;
- compact indices;
- revalidate commands or path existence read-only;
- mark stale entries;
- move suspicious entries to quarantine;
- regenerate small stage-specific indices.

Такой bounded background memory processing совпадает с “subconscious formation” style memory systems, но для WORC должен быть ещё более constrained из-за repo security и poisoning risk. citeturn28view2turn24view3turn24view4

### Promotion, pruning, stale detection, and conflict handling

Ниже — рекомендуемые decision rules. Это уже design proposal, но он основан на cited patterns above.

| Decision | Rule |
|---|---|
| **Promote to long-term** | если знание repo-stable, backed by artifact evidence, и либо повторилось в нескольких задачах, либо критично для planning/review every time |
| **Promote to entity memory** | если знание naturally attaches to a file/module/context/dependency/owner and improves future path-scoped retrieval |
| **Keep only in short-term** | если факт task-specific, recent, still possibly superseded, or useful mainly for resume/debug |
| **Drop as stale** | если path vanished, symbol disappeared, command repeatedly fails against current default branch, or newer verified entry supersedes it |
| **Merge duplicates** | если normalized subject plus predicate match and evidence overlaps; preserve union of provenance and keep newest verification timestamp |
| **Quarantine conflict** | если new evidence contradicts active memory but is only agent-inferred or weakly grounded |

Практически это означает такие heuristics:

- **Promote to long-term**: build/test command repeatedly used successfully; reviewer repeatedly asks for same test style; module repeatedly shows same fragile integration point; architecture fact confirmed by code and docs.
- **Do not promote**: “task X failed because provider Y was flaky”; “this one PR preferred a strange workaround”; “the agent speculated that file A is legacy”.
- **Drop as stale**: path deleted, command no longer works, entity renamed and relation can be deterministically migrated, or entry has gone unused and unverified through many relevant changes.
- **Merge duplicates**: same convention expressed with slightly different wording.

AGENTbench’s evidence against bloated context files and ContextBench’s evidence of recall-heavy retrieval are the two strongest empirical reasons to keep these promotion rules **strict rather than permissive**. citeturn10view0turn7search0

### Safety model

**Redaction and secret handling.**  
Memory writes must pass through mandatory redaction and secret scanning. GitHub secret scanning docs emphasize that secret scanning uses pattern matching and validation and that push protection blocks pushes containing secrets before they land in the repo. TruffleHog documents verification-first secret detection, and GitGuardian documents large detector coverage. For WORC the minimum viable rule is: **nothing writes into `.worc/memory/` before a secret scan passes**, and any suspicious candidate is either redacted or rejected. citeturn9search0turn9search5turn9search16turn9search2turn9search3

**Deny-by-default storage policy.**  
Persist only allowlisted fields and types. This is my recommendation, but it is strongly motivated by current memory-poisoning evidence: memory poisoning is persistent, existing prompt-injection defenses are incomplete, and write/retrieve aggressiveness increases attack success. In other words, the more your system writes and blindly reuses, the larger the attack surface becomes. citeturn24view3turn24view4

**Trust levels and provenance.**  
Каждая memory entry должна иметь `trust_level` and `provenance`:
- `repo-observed` — directly verifiable from code/config;
- `artifact-backed` — derived from task artifacts, checks, review comments;
- `review-verified` — confirmed in review/fixing outcome;
- `agent-inferred` — LLM-derived synthesis not yet independently confirmed;
- `external-untrusted` — came from web/doc/user/API content and cannot auto-promote.

Recent poisoning work shows that both content-based trust scoring and lineage alone are malleable; origin matters, but it also needs to be enforced in the write-retrieve-act pipeline. WORC should therefore never let low-trust memory silently behave like high-trust repo facts. citeturn24view3

**Audit trail, rollback, and quarantine.**  
Append-only `audit/log.jsonl`, content hashes, and periodic snapshots are worth adding from day one. Anthropic’s plain-file auto memory shows the value of auditable editable artifacts; recent deterministic-control-plane research argues for hash-chained audit logs and traceability around agent-governing artifacts; and security work makes clear that bad memory writes must be reversible. citeturn19view0turn25view1

**Bounded autonomy.**  
Hooks and enforcement should remain deterministic. Anthropic explicitly notes that memory and instruction files are only context, not enforced configuration, and recommends hooks such as `PreToolUse` when you need hard blocking behavior. WORC should follow the same principle: memory may guide planning and review, but enforcement lives in orchestrator policies, not in memory text. citeturn19view0turn18view3

**Containment over supervision-only.**  
Anthropic’s engineering writeup on containing Claude stresses that human approvals alone degrade because users approve most prompts, and that strict access boundaries and sandboxes are central to limiting blast radius. For WORC this means memory cleanup, extraction and retrieval should run under the same containment assumptions as agents themselves: no secret-bearing environment, no arbitrary network, no hidden write channels into core state. citeturn24view0


# 07. Safety model

[Previous](./06-recommended-read-write-lifecycle.md) | [Next](./08-concrete-proposals-for-this-repo.md)

### 7.1 Redaction

Memory должна считаться artifact-class storage, не "безопасным локальным кэшем". Значит:

- redaction before disk write;
- no raw secrets in memory, SQLite, logs, artifacts;
- no full environment capture;
- no raw session ids.

Это полностью соответствует текущим repo invariants. [AGENTS.md](../../AGENTS.md) [security rules](../../.agents/rules/security.md)

### 7.2 Deny-by-default storage policy

Писать в durable memory можно только из allowlisted source classes:

- local task artifacts;
- review/check outputs;
- repo files/docs;
- operator/HITL inputs;
- deterministic repo analysis.

Нельзя durable-promote:

- arbitrary web search facts;
- MCP connector output without local validation;
- raw agent self-claims without evidence.

### 7.3 Poisoning resistance

Рекомендованный trust model:

- `internal_validated`: derived from local code/docs + validator passed;
- `internal_unvalidated`: local artifact exists, but fact not reconciled;
- `external_mixed`: includes external input, needs quarantine;
- `human_curated`: manually edited/approved.

Rules:

- only `internal_validated` and `human_curated` can become durable long-term;
- `external_mixed` can live in episodic store but not durable long-term by default;
- procedural memory requires `human_curated` or explicit operator approval.

MPBench and related work show that memory poisoning harms agents more when malicious info can enter high-impact memory channels and be retrieved later as trusted context. [MPBench](https://arxiv.org/html/2606.04329v1)

### 7.4 Audit trail

Every memory mutation should log:

- mutation id;
- timestamp;
- actor (`finalizer`, `cleanup`, `operator`);
- source artifact ids;
- affected memory ids;
- action (`append`, `promote`, `merge`, `quarantine`, `prune`, `rollback`);
- pre/post hashes;
- rationale.

### 7.5 Rollback / quarantine

Нужны:

- pre-cleanup snapshots;
- mutation log;
- quarantine folders;
- simple restore command.

Это важнее, чем сложный ML scoring. Bad memory update should be cheap to undo.

### 7.6 Bounded autonomy

Autonomous cleanup must have hard limits:

- max entries scanned per pass;
- max promotions per pass;
- max wall-clock budget;
- fail-closed if validator uncertain;
- no writes during active task.


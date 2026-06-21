# B32 — Flow Checkers (Citation, Dependency Scan)

> Reconstructed from code (`src/wastech_orchestrator/core/flow/checkers/citation.py`, `dependency_scan.py`) and tests (`tests/core/flow/`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `core/flow/checkers/citation.py`, `core/flow/checkers/dependency_scan.py`

## Responsibility

A `checks` node ([B30](B30-flow-node-runners.md)) names a `checker`; beyond the `command_profile` quality gate ([B24](B24-check-execution.md)), two **core-owned** checkers exist as alternative gates for the research and audit flows. The flow can never supply commands or scanners (the security-ceiling rule): the checker set is fixed in this module. Both reduce to the same `pass`/`fail` outcome so the engine needs no per-checker special case.

## Public surface

- `validate_citations(repo_dir, manifest_path)` ([citation.py:61](../../../src/wastech_orchestrator/core/flow/checkers/citation.py#L61)) → `CitationReport` ([citation.py:48](../../../src/wastech_orchestrator/core/flow/checkers/citation.py#L48)); `CitationEntry` ([citation.py:39](../../../src/wastech_orchestrator/core/flow/checkers/citation.py#L39)); `CitationStatus` ([citation.py:31](../../../src/wastech_orchestrator/core/flow/checkers/citation.py#L31)).
- `run_dependency_scan(...)` ([dependency_scan.py:53](../../../src/wastech_orchestrator/core/flow/checkers/dependency_scan.py#L53)) → `DependencyScanReport` ([dependency_scan.py:45](../../../src/wastech_orchestrator/core/flow/checkers/dependency_scan.py#L45)); `ScannerRun` ([dependency_scan.py:33](../../../src/wastech_orchestrator/core/flow/checkers/dependency_scan.py#L33)); `DEFAULT_DEPENDENCY_SCANNERS` ([dependency_scan.py:27](../../../src/wastech_orchestrator/core/flow/checkers/dependency_scan.py#L27)).

## Behavior

### Citation checker (gating, deterministic, no LLM, no network)

The research synthesis node writes a `sources.json` manifest beside its report; `validate_citations` ([citation.py:61](../../../src/wastech_orchestrator/core/flow/checkers/citation.py#L61)) validates every entry against the repository and classifies it ([citation.py:31-37](../../../src/wastech_orchestrator/core/flow/checkers/citation.py#L31)):

- **`verified`** — the cited `path` exists in the repo and the `line`/`snippet` (when given) is present;
- **`broken`** — the citation points at something that does not exist (a hallucinated path/line, a snippet absent from the file, or a path escaping the repo) — the gating signal;
- **`uncheckable`** — cannot be validated deterministically (an external `url`, or a malformed entry) — recorded, never gating.

The aggregate `passed` is `False` **iff any entry is `broken`** ([citation.py:100](../../../src/wastech_orchestrator/core/flow/checkers/citation.py#L100)): a hallucinated citation fails the check, so the flow's `citation → synthesis (fail)` edge sends synthesis back. A **missing or malformed manifest** is `uncheckable` and returns `passed=True` — it never crashes or fails, because a hallucination cannot be proven from an unreadable manifest; the after-stage output guard ([B30](B30-flow-node-runners.md)) is what enforces that `sources.json` exists ([citation.py:68-98](../../../src/wastech_orchestrator/core/flow/checkers/citation.py#L68)). Path containment uses `is_within` ([B30](B30-flow-node-runners.md), `output_policy.py`), so a `../`-escaping path is `broken` ([citation.py:119-120](../../../src/wastech_orchestrator/core/flow/checkers/citation.py#L119)).

### Dependency-scan checker (evidence, never gates)

`run_dependency_scan` ([dependency_scan.py:53](../../../src/wastech_orchestrator/core/flow/checkers/dependency_scan.py#L53)) runs the core-owned scanner set — `pip-audit` and `osv-scanner`, each an **argv list, never a shell string** ([dependency_scan.py:27-30](../../../src/wastech_orchestrator/core/flow/checkers/dependency_scan.py#L27)) — through the safe process runner ([B19](B19-subprocess-runner.md)) with a mandatory timeout and the allowlisted child environment. Each scanner's stdout streams to `<logs_dir>/<name>.json` as machine-readable evidence. The scan **always reports `passed=True`** (the scan ran) so the `checks` node stays uniformly `pass`/`fail` and the engine needs no "this checker doesn't gate" case ([dependency_scan.py:6-8](../../../src/wastech_orchestrator/core/flow/checkers/dependency_scan.py#L6)); whether the findings gate is the flow's decision, expressed by its edges. A scanner that is not installed _launch-fails_ and contributes no findings (`launched=False`) — a missing tool is not a quality failure for an evidence scan ([dependency_scan.py:80-89](../../../src/wastech_orchestrator/core/flow/checkers/dependency_scan.py#L80)).

## Invariants & guarantees

- **Core-owned set** — a flow may not invent a checker kind or supply scanners (enforced at load, [B29](B29-flow-definition-and-validation.md); `_CHECKER_KINDS`).
- **No shell, no network for the scan** — scanners run as argv lists with the allowlisted env; the citation checker reads the filesystem only (an external `url` is `uncheckable`).
- **Never crash** — a missing/malformed citation manifest is `uncheckable`, not a crash or a fail.
- **Read-only** — both checkers are read-only, so the `checks` node's mutation guard ([B30](B30-flow-node-runners.md)) does not apply to them.

## Dependencies

- **Uses:** [B30](B30-flow-node-runners.md) (`output_policy.is_within`/`resolve_output_policy`), [B19](B19-subprocess-runner.md) (`run_process`).
- **Used by:** [B30](B30-flow-node-runners.md) (`ChecksNodeRunner` dispatch); the `deep_research` (citation) and `security_audit` (dependency_scan) flows ([B29](B29-flow-definition-and-validation.md)).

## Tests

- `tests/core/flow/` — citation classification (verified/broken/uncheckable, missing/malformed manifest), dependency-scan evidence (launch failure → no findings, always-pass).

# Backlog: Token optimization

Status: **backlog / not scheduled** Date: 2026-06-11 Owner: Vladimir Makarevich

This document captures the idea of reducing token consumption in the orchestrator and the analysis behind it. It is a backlog item, not part of the v1 scope (see [worc_architecture.md](../worc_architecture.md) §2). Nothing here overrides the canonical reference (the [Functional Map](../functional/index.md)) or the hard invariants in [CLAUDE.md](../../CLAUDE.md) and [.agents/rules/](../../.agents/rules/).

## 1. Goal

Reduce the number of tokens flowing through the agent CLIs (Codex / Claude Code) so that runs stay within context windows, avoid auto-compaction, leave headroom against rate limits, and cost less when billing is API-based. This must happen **without** weakening any invariant: the Core stays CLI-agnostic, no secrets leak into logs/SQLite/artifacts, only allowlisted env is passed, and the security policy cannot be relaxed.

## 2. Where tokens are actually spent (two distinct sinks)

The orchestrator has two different token consumers, and any solution must be mapped to the right one. They are **not** interchangeable.

### Sink A — the agent's own tool loop (usually the largest)

Codex / Claude Code run shell commands (`git`, `pytest`, `npm`, build, …) inside the sandbox, and the verbose output of those commands lands in _their own_ context. The orchestrator Core does not see or directly control this — only the agent's own hooks/wrappers can compress it.

### Sink B — context the orchestrator injects

For each stage run the Core assembles a prompt plus artifacts (see the [Functional Map](../functional/index.md)): `current.diff`, check logs (`checks/<run-id>.log`), `review/findings.json`, `plan.md`, `task.normalized.json`. Large diffs and verbose test logs inflate easily. The Core fully controls this — it builds the `AgentRunRequest` ([src/wastech_orchestrator/providers/base.py](../../src/wastech_orchestrator/providers/base.py)).

> Key consequence: a single tool will not cover both sinks. RTK ≈ sink A, Headroom/LLMLingua ≈ sink B, and sink A is typically the bigger one.

## 3. Proposed phased plan

Mapped to the existing architecture and invariants. Each phase is independently shippable and gated behind config flags.

### Phase 0 — measure first

`AgentRunResult.usage` already exists ([base.py](../../src/wastech_orchestrator/providers/base.py)) and `provider_attempts` is already a State Store entity (see the [Functional Map](../functional/index.md)).

- Persist tokens/cost per attempt in SQLite.
- Collect a baseline of "tokens per stage" on a few real tasks.
- Without a baseline there is no before/after number to justify the rest.

### Phase 1 — deterministic artifact reduction (sink B, **no third-party deps**)

A new optional context-preparation step in the Core/artifact layer (e.g. `context/reducer.py`) that bounds what enters the prompt while keeping the full artifact on disk:

- strip ANSI codes from logs;
- keep only failing tests + a tail, drop "green" noise;
- dedupe repeated log lines;
- cap diff size (head/tail + `... N lines elided ...` marker);
- write a summary file next to the full log.

Rationale: safest, fully auditable, reversible (the original under `logs/<task-id>/...` is untouched; only the prompt-injected copy is reduced), and aligned with the "deterministic Core" philosophy ([worc_architecture.md](../worc_architecture.md) §2). Likely captures most of the sink-B win **without** any library.

### Phase 2 — RTK for sink A (agent tool output), flagged, **in the provider adapters**

- New `token_optimizer` config section (e.g. rtk: enabled/command/profile). Invariant "Core does not know CLI syntax" → all RTK wiring lives in `CodexProvider` / `ClaudeCodeProvider`, never in Core.
- Adapters optionally install the RTK hook into the per-run agent config (`rtk init` → PreToolUse hook for Claude Code; `AGENTS.md + RTK.md` for Codex).
- Add RTK to `preflight()` (binary presence/version) with a graceful no-op when absent — a missing optimizer must not fail a run.
- **Security:** RTK writes full unfiltered output to its tee cache (`~/.local/share/rtk/tee/`) on command failure — a new place secrets could leak. Redirect tee into the task artifact directory and run it through the same redaction / `denied_read_paths` rules, or disable tee. Put the RTK binary and its config dir explicitly on the env allowlist.
- RTK rewrites commands but does **not** bypass sandbox/approvals → policy not weakened.
- ⚠️ Windows: on native Windows the RTK auto-rewrite hook does not work (falls back to CLAUDE.md-injection mode only); full support needs WSL.

### Phase 3 — ML compression for the heaviest sink-B artifacts (optional, later)

Only if Phase 1 falls short. Headroom (as a **library**, not proxy) or LLMLingua, strictly:

- logs only, **never the diff that feeds review** (lossy compression can hide a bug → breaks the meaning of review/fixing);
- originals preserved; any reversible-compression cache redirected into the task artifact dir + redacted;
- A/B against the deterministic baseline.

Out of the critical deterministic path; treat as an opt-in experiment.

## 4. Library analysis

| Criterion | **RTK** (rtk-ai/rtk) | **Headroom** (chopratejas/headroom) | **LLMLingua** (microsoft) |
| --- | --- | --- | --- |
| Compresses | Agent shell-command output (**sink A**) | Messages/JSON/code/files (**sink B**) | Prompt/context, RAG (**sink B**) |
| Method | Rules/filters (**deterministic**) | ML model + AST (**lossy**) | Small LM by perplexity (**lossy**) |
| Lang/runtime | Rust, single binary, 0 deps | Python 3.10+ (+Rust/HF model) | Python + small LM (GPU helps) |
| Integration | PreToolUse hook (Claude), AGENTS.md/RTK.md (Codex) | library `compress()` / proxy / MCP | PyPI, `PromptCompressor` in Python |
| Codex + Claude Code | **Both natively** | via proxy/MCP/SDK, not CLI-native | no CLI binding |
| Windows | Limited (WSL needed for the hook) | PowerShell claimed | ordinary py package |
| Invariant risk | tee cache = secret-leak spot (manageable) | proxy intercepts traffic ↔ conflicts with subscription auth and "no secrets"; CCR cache | lossy + model weight |
| Maturity / license | Active, Apache-2.0 | Active, Apache-2.0 | Mature, research (Microsoft) |

Note on integration modes: the orchestrator launches the CLIs on **subscription (OAuth) auth**, not API keys. Therefore Headroom's **proxy mode** (repointing `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` at a local proxy) is **not recommended**: the proxy would see all traffic (secret-leak risk, against "no secrets") and does not play well with OAuth subscription auth. Library mode only.

(Star counts from web pages were unreliable and are intentionally omitted — verify maturity and license before adopting.)

## 5. Recommendation

1. **Do Phase 0 + Phase 1 first** — measurement and deterministic artifact reduction. No dependencies, safe, auditable, on-philosophy. Often the main sink-B win.
2. **Primary library candidate: RTK.** It covers the largest sink (A), which the Core cannot see at all; it is rule-based/deterministic (in the spirit of §18.1), single-binary with no deps, and natively supports both providers via their own mechanisms. Ship behind a flag, in the adapters, with preflight + no-op fallback. Mind the tee-cache redaction and the native-Windows limitation (WSL for full support).
3. **Headroom: not** as the primary tool — it targets sink B (cheaper to cover deterministically), is lossy/heavier, and its most convenient mode (proxy) conflicts with the security model and subscription auth. Keep as an optional library-mode log compressor for Phase 3.
4. **LLMLingua:** the alternative to Headroom for serious sink-B prompt compression if ever needed — more mature and better researched, but it is an inference model (weight, lossy) aimed at RAG/long context, overkill for this orchestrator's agent loop.

## 6. Open questions

- What is the realistic per-stage token baseline (needs Phase 0 data)?
- Is the dev/runtime target Windows-native or WSL? Decides whether RTK's hook mode is even available.
- Acceptable lossiness for review/fixing context — almost certainly "diff must stay verbatim", to confirm.
- Should the reducer's reductions be recorded in the run audit (e.g. "diff capped, N lines elided") for traceability?

## 7. References

- RTK — <https://github.com/rtk-ai/rtk>
- Headroom — <https://github.com/chopratejas/headroom>
- LLMLingua — <https://github.com/microsoft/LLMLingua>, <https://www.microsoft.com/en-us/research/blog/llmlingua-innovating-llm-efficiency-with-prompt-compression/>

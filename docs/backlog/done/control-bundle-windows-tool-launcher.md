# Frozen control bundles drop a Windows tool's payload, and a crashed tool is mistaken for a failed check

Status: **implemented** Date: 2026-07-26 Owner: Vladimir Makarevich

Two defects that compound. The WRI-010 control-bundle freezer copies **one file per `tool` node** — whatever `ToolRegistry.resolve()` returns. On Windows that is the `.cmd` launcher, and the extensionless script the launcher delegates to is left behind, so every `tool` node in every frozen task fails with a Python "file not found". The `tool` runner then classifies that crash as a _quality_ failure (linter-style non-zero exit), routes to the fix edge, and the flow spends its fix budget editing real content against a gate that never ran.

Observed end to end on `restructure-ch16-questions2` (`content_structure` flow, WastimeApp, 2026-07-26): five `constraints` runs (7, 9, 11, 13, 15) alternating with five `fixing` runs (8, 10, 12, 14, 16), `fix_iterations=5`, task parked at `fixing`, and `structure_critic` never reached. The chapter was edited five times with no finding behind a single edit.

## Part 1 — the launcher is frozen without its payload

### Evidence

```
.worc/control-bundles/restructure-ch16-questions2/tools/   check_journey.cmd
.worc/tools/                                               check_journey, check_journey.cmd,
                                                           check_length, check_length.cmd
```

Every `constraints` run wrote an empty `stdout.txt` and this `stderr.txt`:

```
python.exe: can't open file 'C:\...\.worc\control-bundles\restructure-ch16-questions2\tools\check_journey':
[Errno 2] No such file or directory
```

The shim is a two-file arrangement by design — `.worc/tools/check_journey.cmd` is:

```bat
python "%~dp0check_journey" %*
```

`%~dp0` is the launcher's own directory, so inside the bundle it points at `…/control-bundles/<task-id>/tools/`, where the script is absent.

### Root cause

[`_referenced_inputs`](../../../src/wastech_orchestrator/core/flow/control_bundle.py#L126) collects the tool names used by the snapshot, resolves each through the live registry, and appends **exactly the resolved path**:

```python
resolved = tools.resolve(name)          # control_bundle.py:129
refs.append(_Ref(f"{_TOOLS_SUBDIR}/{resolved.name}", resolved))   # :132
```

[`ToolRegistry.resolve`](../../../src/wastech_orchestrator/core/flow/tools_registry.py#L51) is explicitly suffix-aware on Windows — it walks [`_candidate_names`](../../../src/wastech_orchestrator/core/flow/tools_registry.py#L92) (`check_journey` → `check_journey.cmd`) and returns the first _launchable_ candidate. Freezing only that one file is correct on POSIX, where a tool is a single `+x` script, and wrong on Windows, where the launchable file is a shim over a sibling.

### Blast radius

Every `tool` node of every task on a Windows host, since frozen bundles landed with WRI-010 in 61ef90f5 (#39, 2026-07-25). That is the deterministic gate of all five content flows (`check_journey` in `content_chapter` / `content_structure` / `content_translate` / `content_book`, `check_length` in `blog_article` / `blog_article_revise`). Chapters restructured before that commit (ch14, ch12, ch10) passed their gate; everything after cannot.

The failure is silent in the sense that matters: the gate reports `fail`, which reads exactly like "the content violates the rules".

## Part 2 — a crashed tool spends the fix budget

The runner's own contract ([`tool.py` docstring](../../../src/wastech_orchestrator/core/flow/nodes/tool.py#L10)) draws the infra/quality line at launch:

1. launch-error / timeout → `NodeManualRequired` — "infra, not a quality fail — it never spends a fix iteration";
2. a JSON `outcome` on stdout is authoritative;
3. otherwise the exit code gates: non-zero → `fail` (linter style).

A launcher shim moves that line. `cmd`/`python` **launched** fine, so `result.launch_error is None` ([tool.py:143](../../../src/wastech_orchestrator/core/flow/nodes/tool.py#L143)); the interpreter then failed to open its script and exited non-zero with empty stdout, so [`parse_tool_output`](../../../src/wastech_orchestrator/core/flow/nodes/tool.py#L243) fell through to rule 3 and returned `fail`. The flow did what `fail` means: route to `fixing`, spend an iteration, re-run the gate, repeat — five times, on real prose, with an empty `{constraints_path}` as the only input.

A tool that produced **no stdout at all**, a non-zero exit, and a non-empty stderr has not judged anything. That shape is distinguishable from a linter that fails loudly on stdout, and it should never consume a fix iteration.

## Implemented direction

1. **Freeze the whole launchable set, not the winning candidate.** For each referenced tool, freeze every `_candidate_names(name)` entry that exists in `.worc/tools/` (the bare name _and_ its launcher suffixes), keeping the existing per-file identity/digest gate and manifest entries. Resolution inside the bundle then finds the same file it finds live, with its payload beside it. Fail-closed is unchanged: at least one candidate must still resolve, and the current `ControlBundleError` on a resolution failure stays. On POSIX only the bare name exists, so nothing changes there — worth an explicit test so the cross-platform contract is pinned.
   - Consider whether a tool may legitimately need more than its own name (a helper module, a data file). If so, the honest fix is a declared payload (a per-tool manifest) rather than name-guessing; if not, say so in the docs so the two-file shim stays the only supported shape.
2. **Classify a silent crash as infrastructure.** When a tool exits non-zero with **empty stdout** and non-empty stderr, treat it as a malfunction — `NodeManualRequired`, no fix iteration — instead of a content verdict. Keep the linter-style path for a tool that actually said something. The message should name the tool and quote the redacted stderr head, so the operator sees "the checker crashed", not "the chapter is wrong".
3. **Guard the loop.** Even with (2), a gate that fails identically N times in a row with no findings is a signal in itself: parking after two identical no-finding failures would have cost one round instead of five.

## Acceptance criteria

- On Windows, a task whose flow has a `tool` node runs its gate from the frozen bundle successfully; the bundle's `tools/` holds both the launcher and its script, and both appear in the bundle manifest with their digests.
- On POSIX the frozen `tools/` content is byte-identical to today (one file per tool).
- A tool that exits non-zero with empty stdout and a non-empty stderr parks the task with a message naming the tool and its stderr — and does **not** advance `fix_iterations`.
- A tool that exits non-zero _with_ output (linter style) still routes to the fix edge exactly as now; the JSON `outcome` path is untouched.
- Deterministic tests: a fake tools dir containing `t` + `t.cmd` freezes both under `system="Windows"`; a crashed-tool run asserts `NodeManualRequired` and an unchanged fix counter; the existing tool-contract tests stay green.
- `/run-checks` green, and the `tool.py` outcome-contract docstring updated to describe the new infra case.

## Out of scope

- The `check_journey` → `check_chapter` rename upstream and the operator's local copy of the tool — a separate migration noted in the flows-sync work; this item must work for whatever tool name a flow references.
- Retention/cleanup of `control-bundles/<task-id>/`, covered by [runtime-artifact-retention](../runtime-artifact-retention.md).
- The fix-budget policy itself (`constraint_fix`, `max_total_fix_iterations`); item 3 above is a stop condition for a degenerate loop, not a change to the budgets.

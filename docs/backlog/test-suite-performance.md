# Test-suite performance review

Status: **living — P0 + P1 + P2 shipped (44 min → a few minutes; `-m "not slow"` ~12 s); P3 open** Date: 2026-07-23 Owner: Vladimir Makarevich

Analysis of why `pytest` is slow and where the time goes, with measured evidence and prioritized, KISS-respecting optimization levers. This is a **living document**: the analysis is recorded below, and the [Implementation log](#implementation-log) tracks each lever as it ships. The security envelope and the WRI-012 quiescence guarantees must not be weakened by any test-speed change (see the invariants in [../../AGENTS.md](../../AGENTS.md)).

## Implementation log

- **2026-07-23 — analysis complete.** Profiled the suite (44 min serial), isolated the root cause (the per-git-call `ps`-scan tax) with a micro-benchmark, and measured the `pytest-xdist` speedup. Levers P0–P3 defined below.
- **2026-07-23 — P1 shipped (`pytest-xdist`).** Declared `pytest-xdist>=3.6` in the `dev` extra and set `addopts = "-q -n auto"` in `[tool.pytest.ini_options]` (`pyproject.toml`). This parallelizes **both** local runs and CI — CI's `pytest -q` step inherits `addopts`, so `.github/workflows/ci.yml` needed no change (it already installs `.[dev]`). Escape hatch for debugging: `pytest -n0`. Verified: a full `-n auto` run is green (2857 passed / 3 skipped) and `deptry src` stays clean.
- **2026-07-23 — P0 shipped (trusted git containment).** Added a `trusted` parameter to `run_process` (`providers/process.py`) that selects a lightweight POSIX containment (`_trusted_make_containment`, snapshot = `_no_descendants`): same process-group isolation, SIGKILL, and bounded emptiness proof, but **no per-call `ps` descendant sweep**. `GitManager._run` passes `trusted=True` for `git` argv only; `gh` and every agent launch keep the full WRI-012 barrier untouched. Measured effect: the profiled `git_manager._run` cost fell from 22.9 s → 3.4 s on one pipeline test (per-git-call 150 ms → ~23 ms, i.e. raw git speed; the `ps` sweep is gone entirely), and that test fell 20.9 s → 4.2 s. Tests added: trusted-containment wiring + in-group reaping (`test_process_quiescence_posix.py`), `trusted` param + injection-seam precedence (`test_process.py`), git-vs-gh scoping (`test_git_manager.py`). **Security note:** this narrows the WRI-012 barrier for a _trusted, hooks/pager/ext-diff/textconv/signing-hardened_ git process (which cannot spawn a `setsid`-escaped writer) — it does **not** touch the untrusted-agent boundary. It still warrants a `/security-review` pass before merge (see the **P0** lever below for the full rationale and guardrails).
- **2026-07-23 — flake fixed (exposed by P1).** Enabling `-n auto` surfaced a latent flaky test: `tests/notify/test_ask_human.py::test_timeout_is_capped_by_config` asserted a wall-clock-derived poll deadline with `pytest.approx([105.0])` (tolerance ~1e-4). `TelegramNotifier.wait_for_answer` computes `deadline = monotonic() + (expires_at - wall_clock())`; the test pinned `monotonic` but left `wall_clock` real, so under xdist CPU contention the elapsed-time drift (~0.85 ms) exceeded the tolerance. Fix (test-only): the `_notifier` helper now also injects a constant `wall_clock`, making the deadline exactly 105.0. No production change.
- **2026-07-23 — combined result (measured).** Full suite, default `-n auto` (P0 + P1): **2648 s → ~3–5 min, all green** — an order-of-magnitude speedup. (Wall-clock varies run-to-run on this laptop from thermal/background load; the reliable, load-independent evidence — ps-sweep gone, per-test 20.9 s → 4.2 s, xdist 5.5× — is in the closing "Measured end state".)
- **2026-07-23 — P2 shipped (`slow` marker + fast inner loop).** Registered a `slow` marker and `--strict-markers` in `pyproject.toml`, and module-tagged the integration tier (`pytestmark = pytest.mark.slow`) — chosen **data-driven** from a full `--durations=0` run, not by fixture heuristic (that missed the top-level `tests/*.py` files, e.g. the 140 s `test_cli_install.py`). Result: `pytest -m "not slow"` runs **2365 tests in ~12 s** under `-n auto` (497 tests are `slow`). Two clocks pinned in `test_ask_human` had already de-flaked the timing test. Tagged files: the `tests/core` pipeline set (orchestrator, cli_pipeline, cli_rerun, recovery, merge_task, cli_finalize), `tests/git/*`, `test_provider_integration`, `test_router_integration`, `tests/providers` real-process files (process, quiescence, subtree-kill, canary-smoke), `test_detect`, `test_cli_install`, `test_cli_watch`.
- **2026-07-23 — clock-determinism sweep (harden vs `-n auto` flakes).** Swept every test that reads a real clock or asserts a timing value. Found exactly **one** `pytest.approx` in the whole suite — the `test_ask_human` deadline, already fixed. All other real-clock reads (`test_http_client` poll deadlines, `test_progress` heartbeat wait, `test_subtree_kill` reap polls, a `test_recovery` setup expiry) are generous bounds or condition-waits whose assertions are timing-independent — robust under load, deliberately left unchanged (no churn). Net: the suite has no remaining tight-tolerance timing assertion.
- **2026-07-23 — second `-n auto` flake fixed (preflight probe timeout).** A full-suite parallel run (max 12-worker saturation) intermittently failed `test_preflight_detects_fake_version[codex|claude]` with `version=None`: the version/capability probe launches a real subprocess under the fixed 10 s `_PREFLIGHT_TIMEOUT_SECONDS`, which starvation under saturation could exceed. Fix (matches the existing injected-seam pattern): `BaseCliProvider.__init__` gained a `preflight_timeout_seconds` param (default unchanged at 10 s — **production semantics identical**); the integration test injects a generous 120 s budget so the probe can never spuriously time out under test-machine load. Both providers inherit it via `**kwargs` (no subclass change). `_adapter_base.py` picked up a `PLR0913` baseline entry (11th injected-seam arg).

## TL;DR

- Full suite: **2857 passed, 3 skipped in 2648 s (~44 min)**, serial, on a 12-core macOS host (`.venv`, pytest 9.0.3), no plugins beyond `pytest-cov`.
- **`sys` time (1979 s) dwarfs `user` time (787 s)** — the suite is process-spawn / I/O bound, not CPU bound. Almost everything is one core waiting on subprocesses.
- **58 % of the wall-clock (≈ 26 min) is the 60 slowest tests, all end-to-end pipeline tests under `tests/core/`** (`test_orchestrator.py`, `test_cli_pipeline.py`, `test_recovery.py`, `test_cli_rerun.py`). Each takes **20–46 s**.
- Root cause of the per-test cost: each pipeline test makes **~150 real `git` subprocess calls** through `GitManager`, and **every git call pays a ~126 ms `ps`-table-scan tax** from the WRI-012 process-quiescence containment — a **6.9×** blow-up over the raw ~20 ms git cost. That is ~19 s of pure `ps`-scanning per test.
- Two independent, compounding levers, **both shipped 2026-07-23** (see the Implementation log): **(1) don't run the agent-grade descendant scan for the orchestrator's own trusted git calls** (also a production win), and **(2) run pytest in parallel with `pytest-xdist`** (12 idle cores; near-linear on an I/O-bound suite). Result: **44 min → a few minutes (~10–16×), all green**.

## How this was measured

- `time pytest --durations=60 -q` (full run) → total wall-clock, `user`/`sys` split, and the 60 slowest tests.
- `cProfile` on one representative test (`test_happy_path_complete_task`, 20.9 s) → where the wall-clock goes inside `run_task`.
- A focused micro-benchmark (`scratchpad/bench_git.py`) → per-git-call cost: raw `subprocess.run` vs the hardened `run_process` (with and without the `ps` descendant scan).
- `pytest-xdist -n auto` serial-vs-parallel comparison (now a declared `dev` dependency, enabled by default via `addopts`; see Notes).

## Where the time goes

### 1. The 60 slowest tests are 58 % of the run, all in `tests/core`

| File | Σ of its tests in the slowest-60 | note |
| --- | --- | --- |
| `tests/core/test_orchestrator.py` | 1321 s (45 tests) | 114 tests total → the file alone is ≈ 30+ min |
| `tests/core/test_cli_pipeline.py` | 92 s | end-to-end `watch`/`run` |
| `tests/core/test_recovery.py` | 91 s | resume/checkpoint |
| `tests/core/test_cli_rerun.py` | 46 s | rerun paths |
| **total (slowest 60)** | **1550.6 s (avg 25.8 s/test)** | **= 58 % of 2648 s** |

No `setup`/`teardown` entries appear in the slowest-60 — the cost is entirely in the `call` phase (running the pipeline), **not** in fixtures. The `git_repo` fixture itself is cheap: it uses raw `subprocess.run` (`run_git`), ~10 calls ≈ 0.2 s.

### 2. cProfile of one 20.9 s test

`test_happy_path_complete_task` → `Orchestrator.run_task` = 23.2 s (profiled), of which **22.9 s is `git_manager._git` → `_run`, called 150 times** (~0.15 s each). 301 short-lived threads are created (one heartbeat + one containment-tracker thread per git call). The heartbeat helper itself is not the cost (it runs the op in the calling thread and the join returns immediately once the op finishes).

### 3. The per-git-call tax — measured (`bench_git.py`, 100 calls of `git status`)

```
raw subprocess.run          :  22.0 ms/call
run_process (real ps scan)  : 149.9 ms/call   ← what GitManager uses today
run_process (no ps scan)    :  24.2 ms/call   ← containment minus the ps scans
--------------------------------------------------
ps-scan overhead            : 125.7 ms/call   (the entire blow-up)
containment (minus ps)      :   2.2 ms/call   (process-group + threads = negligible)
```

**The entire 6.9× overhead is the `ps` process-table scan.** `providers/process.py::_posix_descendants` runs `ps -axo pid=,ppid=` (a full process-table snapshot) and it is invoked **2–3× per git call**: once when the containment tracker thread starts, once in `terminate()`, and once per iteration of the `terminate_and_prove()` quiescence loop (`PosixProcessContainment._alive_tracked`). On macOS each `ps` scan is ~40 ms.

**Why it exists:** the WRI-012 quiescence barrier proves that a launched process left no detached/background descendant still able to write after `run_process` returns. That guarantee matters for the **untrusted agent** process tree (Claude/Codex may spawn grandchildren). It is applied uniformly to the orchestrator's **own** `git` calls too — which are argv-controlled, hook/pager/textconv-hardened (`_harden_git_argv`), and never spawn lingering descendants. So the hundreds of tiny git calls each pay a barrier tax designed for the agent.

In the real product the git tax is negligible (an agent run is minutes; git calls are noise). In tests the "agent" is an instant in-memory `FakeProvider`, so the git-containment tax becomes the whole cost.

### 4. Everything runs serially on 1 of 12 cores

`sys` 1979 s ≫ `user` 787 s and `real` ≈ `user` + `sys` ⇒ single-threaded, syscall/spawn-bound. CI (`.github/workflows/ci.yml`) runs bare `pytest -q` on a 4-way OS/Python matrix — so this ~44 min is paid 4× per CI run, serially each.

### 5. Minor fixed costs

- `tests/providers/test_process_quiescence_posix.py` sleeps `_WAIT_PAST = 2.5 s` ×3 (~7.5 s), plus child sleeps — genuine timing tests (prove a background writer is reaped). POSIX-only.
- Real-subprocess provider/integration tests (`fake_cli` spawns a real Python per call; `test_provider_integration.py::test_timeout` waits `timeout_seconds=1`). Individually small; they add up but are not the bottleneck.

## Optimization levers (prioritized)

### P0 — Don't run the agent-grade descendant scan for the orchestrator's own git calls ✅ shipped 2026-07-23

**Impact (measured):** removed ~126 ms from each of ~150 git calls per pipeline test. On one representative test the profiled `git_manager._run` total fell 22.9 s → 3.4 s (per-call 150 ms → ~23 ms, raw git speed) and the test fell 20.9 s → 4.2 s. Combined with P1 the whole suite went from 44 min to a few minutes. **Also a production improvement** — every real `worc` run makes many git calls, each previously paying 2–3 `ps` full-table scans.

**Shape (shipped):** `run_process` gained a `trusted: bool` parameter. When set (and no `make_containment` is injected) it builds `_trusted_make_containment` — on POSIX a `PosixProcessContainment(snapshot_fn=_no_descendants)`: same new-session/process-group isolation, SIGKILL-on-timeout, and the O(1) `killpg(pgid, 0)` group-empty proof, but **no `ps` descendant enumeration/tracking**. Windows is unchanged (its kill-on-close Job Object never scanned). `GitManager._run` passes `trusted=True` for `git` argv only; `gh` and every agent launch keep the full WRI-012 barrier. The injected-`make_containment` seam still wins over `trusted`, so tests are unaffected.

**Guardrails / security:** this narrows the WRI-012 barrier for a hardened git process only. git argv is fixed and hooks/pager/ext-diff/textconv/signing are disabled via `_harden_git_argv`, so it spawns no `setsid`-escaped writer — the process group is a complete quiescence proof, and only the escaped-descendant detection (which git can't trigger) is dropped. It is **not** a "disable the sandbox" change (forbidden by invariant); the untrusted-agent boundary is untouched. Still, because it edits a security boundary, run `/security-review` on this diff before merge. Regression coverage: `test_trusted_containment_still_reaps_an_in_group_writer` (group reap intact), `test_trusted_containment_skips_the_descendant_scan` (wiring), `test_git_calls_use_trusted_containment_but_gh_does_not` (scoping).

### P1 — Run pytest in parallel with `pytest-xdist` ✅ shipped 2026-07-23

**Impact:** the suite is I/O/spawn-bound with 12 idle cores → large speedup. Measured on this host:

- `tests/git/test_git_manager.py`: serial 286.5 s → `-n auto` 52.5 s (**5.5×**).
- Full suite: serial 2648 s → `-n auto` **670 s (11 min 10 s)** (**3.95×**), 2857 passed / 3 skipped. (Enabling `-n auto` did surface one latent flaky timing test — now fixed deterministically; see the Implementation log.)

Note the full-suite speedup is ~4×, not ~12× despite 12 cores: under `-n auto` the total `sys` time _rises_ (1979 s → 2265 s) because ~10 workers hammer the process table (`ps`) and spawn git concurrently, so kernel/spawn contention caps parallel efficiency. This is exactly why **P0 and P1 compound** — removing the `ps` tax (P0) both speeds each test and removes the contention that limits P1's scaling.

**Adopted (2026-07-23):** `pytest-xdist>=3.6` added to the `dev` extra and `addopts = "-q -n auto"` set in `pyproject.toml` — one source of truth that parallelizes local runs and CI alike (CI's `pytest -q` inherits `addopts`; it already installs `.[dev]`, so `ci.yml` was unchanged). Verified xdist-safe (`tmp_path` everywhere, per-test `git_repo`, no shared ports/CWD assumptions; the full parallel run passed clean, 2857/3). Debugging escape hatch: `pytest -n0` (serial — for `--pdb`, `-s` streaming, deterministic ordering). If ordering-coupled tests ever surface, switch to `--dist loadscope`. Stacks with P0.

### P2 — Register test markers for a fast inner-loop subset ✅ shipped 2026-07-23

**Shipped:** a `slow` marker (+ `--strict-markers`) is registered in `pyproject.toml`, and the integration tier is module-tagged `pytestmark = pytest.mark.slow`. The tier was picked **data-driven** from a full `--durations=0` run rather than by fixture heuristic — the heuristic missed the top-level `tests/*.py` files (a git-glob `tests/**/*.py` skips them), which include the single heaviest file, `test_cli_install.py` (~140 s). `pytest -m "not slow"` now runs **2365 tests in ~12 s** under `-n auto`; 497 tests carry `slow`. The handful of ~1–3 s survivors in the fast tier (a redaction test, `test_cli_clear`, `test_env_file`) were left unmarked — under `-n auto` they don't gate wall-clock, and marking single-second tests is diminishing returns. Pairs with the `/test` skill's targeted mode.

### P3 — Trim the fixed sleeps in the POSIX quiescence tests (open)

`test_process_quiescence_posix.py`'s `_CHILD_SLEEP`/`_WAIT_PAST` (1.5 s / 2.5 s) are conservative. Now that the file is `slow`-tagged it is out of the fast inner loop, so this is even lower priority — shortening the waits is timing-sensitive (flakiness risk) for a ~5–10 s gain on the full run only. Do it only with margin preserved.

## Recommended order

1. ~~**P1 (`pytest-xdist`)** — zero-risk, broad, immediate, helps CI now.~~ **✅ done 2026-07-23.**
2. ~~**P0 (git containment)** — biggest per-test win and a production improvement.~~ **✅ done 2026-07-23** (run `/security-review` on the diff before merge — it edits the WRI-012 boundary).
3. ~~**P2 (`slow` marker)** — fast inner loop via `pytest -m "not slow"`.~~ **✅ done 2026-07-23.** P3 (trim POSIX quiescence sleeps) remains open — low priority.

Measured end state (this host): baseline **2648 s (44 min)** serial (cold, clean) → **P0 + P1**, default `-n auto`, **all green in ~3–5 min** (three runs landed at 167 s / 243 s / 324 s). The wide spread is laptop measurement noise — thermal throttling and background load after ~1.5 h of back-to-back full-suite runs — **not** a property of the change; the reliable, load-independent evidence is elsewhere:

- **ps sweep eliminated:** cProfile of a pipeline test shows `_posix_descendants` called **0** times (was 2–3× per git call); `git_manager._run` per-call cost 150 ms → ~23 ms (raw git speed).
- **Per-test:** the representative `test_happy_path_complete_task` fell **20.9 s → 4.2 s**.
- **Parallelism:** `tests/git/test_git_manager.py` serial 286.5 s → `-n auto` 52.5 s (**5.5×**).

Net: a ~**10–16×** end-to-end speedup (44 min → a few minutes), dominated now by real subprocess/git spawns and pytest import overhead rather than the containment tax. CI drops proportionally on every matrix leg (it runs `pytest -q`, which inherits `-n auto` and the trusted-git speedup).

## Notes / reproduction

- Full timing + slowest tests: `pytest --durations=60 -q` (numbers above are from a single macOS run; absolute values vary by host, the ratios do not).
- Per-git-call tax: a throwaway benchmark timed 100 `git status` calls three ways — raw `subprocess.run`, `run_process` as-is, and `run_process` with an injected no-op `snapshot_fn` — isolating the `ps` scans as the entire delta.
- Parallel numbers: `pytest -n auto -q`.
- `pytest-xdist` is now declared in the `dev` extra and enabled by default via `addopts` (P1, shipped) — a fresh `pip install -e ".[dev]"` gets it. Reproduce the serial baseline with `pytest -n0 --durations=60`.

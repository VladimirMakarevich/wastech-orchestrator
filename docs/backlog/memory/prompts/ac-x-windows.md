AC-X (cross-platform) — closed on real Windows
Precondition (F1–F6): verified present before doing anything. service.py index: DerivedIndex | None (L114), lifecycle.assign_entity_trust(..., path_exists=...) (L58), cleanup._reconcile_lessons (L88/L193), redaction.secret_env_values (L109). The branch was in sync — I proceeded.

Environment: the existing .venv was a POSIX leftover from a Linux run (had bin/, lib64 symlink, no Scripts\python.exe) — concrete confirmation the memory suite had never run on Windows. Recreated it as a fresh Windows venv: Python 3.14.5, pip install -e ".[dev]" (pytest 9.1.1, ruff 0.15.20, mypy 2.1.0), git 2.45.1 working.

What I ran (Windows 10 / Python 3.14.5)
Gate	Result
pytest tests/memory (baseline, first-ever on Windows)	122 passed
4 safety drills (redaction/poisoning/staleness/rollback)	16 passed
New tests/memory/test_memory_cross_platform.py	5 passed (incl. the win32-only test, which ran, not skipped)
pytest tests/memory (with new file)	127 passed
ruff check .	clean
mypy src	clean (104 files)
grep os.kill/signal/killpg in memory/	no matches (AC-X2)
Real Windows problems found in the memory subsystem: none
Everything was green without any production-code change. The as_posix() and newline="\n" invariants hold as written on real Windows — no backslash paths stored, no CRLF, git ls-files returns forward slashes, path_exists resolves against the native FS. This is the "green without prod fixes" outcome the prompt anticipated.

New tests added — tests/memory/test_memory_cross_platform.py (cross-platform, live in the shared suite)
(a) POSIX-stored path resolves against the native Windows FS: real src/a.py, empty tracked set → path_exists("src/a.py") True via FS stat, "src/gone.py" False; find_by_basename returns forward slashes.
(b) end-to-end F1 with MemoryService(index=DerivedIndex(repo)): verified path → stored as repo-observed with paths == ["src/a.py"]; missing path → quarantine; the persisted .jsonl contains "src/a.py" and no backslash anywhere.
(c) tier bytes are LF, never CRLF, JSON reads back identically, keys sorted on disk, and a repeated write is byte-identical (determinism).
(d) git_tracked_paths over a real Windows git repo (git init/add/commit a nested file) returns src/pkg/mod.py with forward slashes.
(e) @skipif(sys.platform != "win32"): a native backslash entry normalizes via as_posix() and a backslash query path still resolves.
One genuine bug surfaced — pre-existing, outside the memory subsystem (not fixed here)
The full-gate run failed 2 tests, both in tests/test_cli_shell.py (enqueue). Root cause: cli_shell.dispatch uses shlex.split(line) in POSIX mode, which strips backslashes from Windows paths — enqueue C:\Users\x y\task.md → ['enqueue', 'C:Usersx', 'ytask.md']. Working tree was clean, so it's committed code, and it's unrelated to memory (the memory suite + drills are green). It's a console-portability gap the 2026-06-26 Windows pass didn't cover (the console shipped later). I left it alone (out of the memory AC-X scope) and tracked it as a new follow_ups.md row with the fix direction (shlex.split(line, posix=(os.name != "nt"))).

Docs synced (same change)
implementation-audit.md — F5 rewritten: AC-X1/AC-X2 moved from "untested / rides follow-up" to verified on real Windows (with the test names, the "no prod fix needed" note, and the AC-X2 grep re-confirmation); conformance-table AC-X1 footnote updated from ✅* to ✅; bottom-line and Actions-taken updated. Only AC-SF5 remains of F5.
follow_ups.md — addendum on the "Windows test-suite portability gap" row (memory now verified on Windows, AC-SF5 still separate) + the new cli_shell shlex row.
npx prettier@3 --write "docs/backlog/**/*.md" → both edited docs reported unchanged (already conformant).
Housekeeping note
Prettier's docs/backlog/** glob touched two unrelated files (00-raw-topics.md, archive/done/README.md) with no content change (empty numstat; core.autocrlf=true line-ending noise), and I found ac-x-windows.md had been deleted before my session (tracked in HEAD, already missing at my first read — not my doing). I restored all three so the working tree carries only the three intended changes.

Final state (nothing committed — holding per your instruction):

A tests/memory/test_memory_cross_platform.py
M docs/backlog/follow_ups.md
M docs/backlog/memory/implementation-audit.md
"""Unit tests for the shared diff parser and the bounded change overview (``diff_overview``)."""

from __future__ import annotations

import pytest

from wastech_orchestrator.diff_overview import (
    MAX_LISTED_PATHS,
    MAX_SECTION_BYTES,
    DiffEntry,
    OverviewSection,
    parse_diff,
    render_overview,
    total_counts,
)

# Real `git diff --text <base>` output (rename detection on, `core.quotePath` on): an empty file
# added, a deletion, a modification, a mode-only change, a pure rename, an intent-to-add
# untracked file, a path with a space (git appends a tab to its `---`/`+++` names) and a quoted
# non-ASCII path.
_DIFF = """\
diff --git a/empty.txt b/empty.txt
new file mode 100644
index 0000000..e69de29
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index b77b4eb..0000000
--- a/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-x
-y
diff --git a/mod.txt b/mod.txt
index de98044..a7bc997 100644
--- a/mod.txt
+++ b/mod.txt
@@ -1,3 +1,4 @@
 a
-b
+B
 c
+d
diff --git a/mode.sh b/mode.sh
old mode 100644
new mode 100755
diff --git a/old.txt b/new.txt
similarity index 100%
rename from old.txt
rename to new.txt
diff --git a/untracked.txt b/untracked.txt
new file mode 100644
index 0000000..895bfc4
--- /dev/null
+++ b/untracked.txt
@@ -0,0 +1 @@
+-- dashes
diff --git a/with space.txt b/with space.txt
index b478595..5e28b27 100644
--- a/with space.txt\t
+++ b/with space.txt\t
@@ -1 +1 @@
-s
+s2
diff --git "a/\\303\\251 sp.txt" "b/\\303\\251 sp.txt"
index bca70f3..d169a2f 100644
--- "a/\\303\\251 sp.txt"\t
+++ "b/\\303\\251 sp.txt"\t
@@ -1 +1 @@
-q
+q2
"""

_SHA = "3f9c2a7e1d4b5a6c8e0f1a2b3c4d5e6f7a8b9c0d"


def _section(entries: tuple[DiffEntry, ...], **kwargs: object) -> OverviewSection:
    return OverviewSection(
        title="Change since the task base", base_sha=_SHA, entries=entries, **kwargs
    )


def test_every_kind_of_change_is_one_entry_in_diff_order() -> None:
    assert parse_diff(_DIFF) == (
        DiffEntry("empty.txt", "A", 0, 0),
        DiffEntry("gone.txt", "D", 0, 2),
        DiffEntry("mod.txt", "M", 2, 1),
        # A mode-only change and an empty file have no `---`/`+++` lines: the `diff --git` line is
        # the only place that names them, and dropping them would hide a real change.
        DiffEntry("mode.sh", "M", 0, 0),
        DiffEntry("new.txt", "R", 0, 0, previous_path="old.txt"),
        DiffEntry("untracked.txt", "A", 1, 0),
        DiffEntry("with space.txt", "M", 1, 1),
        DiffEntry("é sp.txt", "M", 1, 1),
    )


def test_hunk_lines_that_look_like_headers_are_counted_not_read_as_paths() -> None:
    # A removed line `-- x` and an added line `++ y` render as `--- x` / `+++ y` inside a hunk.
    diff = (
        "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n--- x\n+++ y\n kept\n"
    )
    assert parse_diff(diff) == (DiffEntry("f.py", "M", 1, 1),)


def test_a_path_containing_b_slash_is_read_whole_from_the_git_line() -> None:
    diff = "diff --git a/x b/y b/x b/y\nold mode 100644\nnew mode 100755\n"
    assert parse_diff(diff) == (DiffEntry("x b/y", "M", 0, 0),)


def test_a_rename_with_edits_keeps_both_paths_and_its_counts() -> None:
    diff = (
        "diff --git a/src/old.py b/src/new.py\nsimilarity index 80%\n"
        "rename from src/old.py\nrename to src/new.py\nindex 1..2 100644\n"
        "--- a/src/old.py\n+++ b/src/new.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert parse_diff(diff) == (DiffEntry("src/new.py", "R", 1, 1, previous_path="src/old.py"),)


def test_an_empty_diff_has_no_entries() -> None:
    assert parse_diff("") == ()


def test_crlf_text_parses_to_the_same_entries() -> None:
    # A diff written in text mode on Windows reads back with CRLF line ends; the overview is
    # rendered from the in-memory text and the report from the artifact, so both must agree.
    assert parse_diff(_DIFF.replace("\n", "\r\n")) == parse_diff(_DIFF)


def test_only_real_line_ends_split_a_diff() -> None:
    # Form feeds and Unicode line separators are file content to git; read as line ends they
    # would turn content into fake diff lines — here a fabricated file and a phantom deletion.
    diff = (
        "diff --git a/f.js b/f.js\n--- a/f.js\n+++ b/f.js\n@@ -1 +1 @@\n"
        "-old\n+s = 'a\u2028diff --git a/evil b/evil'\n+p\x0c-x\x85-y\n"
    )
    assert parse_diff(diff) == (DiffEntry("f.js", "M", 2, 1),)


def test_a_type_change_counts_as_one_file() -> None:
    # git shows a file replaced by a symlink as a deletion and an addition of the same path.
    diff = (
        "diff --git a/cfg b/cfg\ndeleted file mode 100644\n--- a/cfg\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-x\n"
        "diff --git a/cfg b/cfg\nnew file mode 120000\n--- /dev/null\n+++ b/cfg\n"
        "@@ -0,0 +1 @@\n+target\n"
    )
    entries = parse_diff(diff)
    assert [entry.kind for entry in entries] == ["D", "A"]
    header = render_overview([_section(entries)], diff_size=0).splitlines()[0]
    assert "— 1 file, +1 −1," in header


def test_total_counts_sums_the_entries() -> None:
    assert total_counts(parse_diff(_DIFF)) == (5, 5)
    assert total_counts(()) == (0, 0)


def test_the_overview_lists_each_path_with_its_kind_and_counts() -> None:
    entries = parse_diff(_DIFF)
    text = render_overview([_section(entries, denied=frozenset({"gone.txt"}))], diff_size=919)
    assert text == (
        f"Change since the task base {_SHA} — 8 files, +5 −5, full diff 919 B\n"
        "\n"
        "A  empty.txt  +0  −0\n"
        "D  gone.txt  +0  −2  [denied]\n"
        "M  mod.txt  +2  −1\n"
        "M  mode.sh  +0  −0\n"
        "R  old.txt → new.txt  +0  −0\n"
        "A  untracked.txt  +1  −0\n"
        "M  with space.txt  +1  −1\n"
        "M  é sp.txt  +1  −1\n"
    )


def test_a_rename_out_of_a_denied_path_is_marked() -> None:
    entry = DiffEntry("public.txt", "R", 0, 0, previous_path="secret/keys.txt")
    text = render_overview([_section((entry,), denied=frozenset({"secret/keys.txt"}))], diff_size=0)
    assert "R  secret/keys.txt → public.txt  +0  −0  [denied]" in text


def test_an_empty_change_states_zero_files() -> None:
    assert render_overview([_section(())], diff_size=0) == (
        f"Change since the task base {_SHA} — 0 files, +0 −0, full diff 0 B\n\n"
    )


def test_only_the_first_section_names_the_full_diff_size() -> None:
    whole = _section((DiffEntry("a.py", "M", 3, 1), DiffEntry("b.py", "A", 2, 0)))
    since = OverviewSection(
        title="Change since the orchestrator's last commit",
        base_sha="b" * 40,
        entries=(DiffEntry("b.py", "A", 2, 0),),
    )
    text = render_overview([whole, since], diff_size=61 * 1024)
    first, second = text.split("\n\n", 1)[0], text.split("\n\n")[2]
    assert first.endswith("— 2 files, +5 −1, full diff 61 KB")
    assert second == f"Change since the orchestrator's last commit {'b' * 40} — 1 file, +2 −0"


@pytest.mark.parametrize(
    ("size", "shown"),
    [(0, "0 B"), (1023, "1023 B"), (1024, "1 KB"), (61 * 1024, "61 KB"), (1_200_000, "1.1 MB")],
)
def test_the_diff_size_is_human_readable(size: int, shown: str) -> None:
    assert render_overview([_section(())], diff_size=size).splitlines()[0].endswith(shown)


def test_a_large_change_lists_the_bound_and_counts_the_rest_exactly() -> None:
    entries = tuple(DiffEntry(f"src/m{i:03}.py", "M", i, 1) for i in range(250))
    lines = render_overview([_section(entries)], diff_size=1).splitlines()
    listed = [line for line in lines[2:] if line.startswith("M  ")]
    assert len(listed) == MAX_LISTED_PATHS
    rest = entries[MAX_LISTED_PATHS:]
    assert lines[-1] == (f"… and 50 more paths (+{sum(e.added for e in rest)} −50) not listed")
    # The header still states the whole change.
    assert f"— 250 files, +{sum(range(250))} −250," in lines[0]


def test_long_paths_stop_the_listing_at_the_byte_bound() -> None:
    entries = tuple(DiffEntry(f"{'d' * 1000}/{i}.py", "A", 1, 0) for i in range(50))
    text = render_overview([_section(entries)], diff_size=1)
    listed = text.splitlines()[2:-1]
    assert sum(len(line.encode()) + 1 for line in listed) <= MAX_SECTION_BYTES
    assert (
        text.splitlines()[-1]
        == f"… and {50 - len(listed)} more paths (+{50 - len(listed)} −0) not listed"
    )


def test_a_path_with_a_control_character_is_shown_quoted() -> None:
    entry = DiffEntry('bad\nname"\\.txt', "A", 1, 0)
    assert '"bad\\nname\\"\\\\.txt"' in render_overview([_section((entry,))], diff_size=0)


def test_rendering_is_deterministic_and_newline_stable() -> None:
    sections = [_section(parse_diff(_DIFF), denied=frozenset({"mod.txt"}))]
    first = render_overview(sections, diff_size=4096)
    assert first == render_overview(sections, diff_size=4096)
    assert first == render_overview(
        [_section(parse_diff(_DIFF.replace("\n", "\r\n")), denied=frozenset({"mod.txt"}))],
        diff_size=4096,
    )
    assert "\r" not in first and first.endswith("\n")

"""What a unified diff touched, per file, and the bounded overview an agent reads before the diff.

One parser for the question "which files did this change touch, how, and by how much", shared by
everything that asks it: the deterministic report and the supervisor packet fold its entries into a
diff stat, and the Git Manager renders them as the change overview handed to every node after an
edit. Two parsers would be free to disagree about the same ``current.diff`` — the overview would
then list a different change than the pull request describes.

Pure and stdlib-only — no I/O, no clock, no platform branch — and it lives at the package root so
both ``git_manager`` and ``core`` can import it without crossing an import boundary. The input is
always text the caller has already redacted; nothing here sees a secret it could leak, and the
overview carries paths and counts only, never hunk content.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

#: How a file changed: added, modified (content or mode only), deleted, or renamed.
EntryKind = Literal["A", "M", "D", "R"]

#: The most paths one overview section lists. The overview exists to stay small whatever the size
#: of the change, so the bound is a constant rather than a knob: a section an operator could widen
#: stops being the page an agent reads before deciding what to open.
MAX_LISTED_PATHS = 200

#: The most bytes the listed lines of one section may take. The path count alone does not bound
#: the file — one path may be a few thousand characters — so the listing also stops here, and the
#: closing "not listed" line keeps the totals exact either way.
MAX_SECTION_BYTES = 16 * 1024

_GIT_HEADER = "diff --git "
_DEV_NULL = "/dev/null"

# git's C-style quoting, used for a path with a control character, a quote, a backslash or (under
# the default ``core.quotePath``) any non-ASCII byte: each escape letter and the character it
# stands for. Octal escapes carry raw bytes, decoded as UTF-8 once the whole path is collected.
_C_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}
_C_QUOTED = {char: "\\" + letter for letter, char in _C_ESCAPES.items()}


@dataclass(frozen=True)
class DiffEntry:
    """One file a diff touched: its path, how it changed, and its hunk line counts.

    ``path`` is the file as it stands after the change, except for a deletion, where it is the file
    that went away; ``previous_path`` is set only for a rename. Paths are repository-relative POSIX
    strings exactly as git names them, with git's quoting undone.
    """

    path: str
    kind: EntryKind
    added: int
    removed: int
    previous_path: str | None = None


@dataclass(frozen=True)
class OverviewSection:
    """One scope of a change, rendered as a header line plus one line per listed path.

    ``title`` opens the header and ``base_sha`` follows it, so every section states the commit it
    is measured from. ``denied`` names paths the reader must not be pointed at: such a path is
    still listed, by name and counts only, with a ``[denied]`` mark.
    """

    title: str
    base_sha: str
    entries: tuple[DiffEntry, ...]
    denied: frozenset[str] = field(default_factory=frozenset)


def parse_diff(diff_text: str) -> tuple[DiffEntry, ...]:
    """Parse a unified ``git diff`` into one entry per file, in the order the diff lists them.

    Header lines (``---``/``+++``, mode and rename lines) are read only before a block's first
    ``@@``; after it every ``+``/``-`` line is a hunk line, so a removed line whose own text begins
    with ``--`` is counted rather than mistaken for a header. ``splitlines`` rather than a split on
    ``\\n`` on purpose: a diff written in text mode on Windows and read back then yields the same
    lines it did in memory, so a count taken before the write equals one taken from the artifact.
    """
    entries: list[DiffEntry] = []
    block: _Block | None = None
    in_header = False
    for line in diff_text.splitlines():
        if line.startswith(_GIT_HEADER):
            _close(block, entries)
            block, in_header = _Block(git_line=line[len(_GIT_HEADER) :]), True
        elif block is None:
            continue
        elif in_header:
            in_header = block.read_header_line(line)
        elif line.startswith("+"):
            block.added += 1
        elif line.startswith("-"):
            block.removed += 1
    _close(block, entries)
    return tuple(entries)


def total_counts(entries: Iterable[DiffEntry]) -> tuple[int, int]:
    """The summed ``(added, removed)`` hunk line counts of *entries*."""
    added = removed = 0
    for entry in entries:
        added += entry.added
        removed += entry.removed
    return added, removed


def render_overview(sections: Sequence[OverviewSection], *, diff_size: int) -> str:
    """Render *sections* as the change overview text: ``\\n`` newlines, a trailing newline.

    The first section is the one the full diff holds, so its header also names the diff's size —
    *diff_size* bytes, the figure a reader weighs before deciding to open it. Each section lists at
    most :data:`MAX_LISTED_PATHS` paths within :data:`MAX_SECTION_BYTES`, then one line counting
    the rest, so the header totals stay exact however much is left out. The same input renders the
    same text on every platform.
    """
    return "\n".join(
        _render_section(section, diff_size=diff_size if index == 0 else None)
        for index, section in enumerate(sections)
    )


@dataclass
class _Block:
    """The facts one ``diff --git`` block has declared so far."""

    git_line: str
    kind: EntryKind = "M"
    old_path: str | None = None
    new_path: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None
    added: int = 0
    removed: int = 0

    def read_header_line(self, line: str) -> bool:
        """Record one header *line*; return whether the block is still in its header."""
        if line.startswith("@@"):
            return False
        if line.startswith("new file mode"):
            self.kind = "A"
        elif line.startswith("deleted file mode"):
            self.kind = "D"
        elif line.startswith("rename from "):
            self.rename_from = _unquote(line[len("rename from ") :])
        elif line.startswith("rename to "):
            self.rename_to = _unquote(line[len("rename to ") :])
        elif line.startswith("--- "):
            self.old_path = _side_path(line[4:], "a/")
        elif line.startswith("+++ "):
            self.new_path = _side_path(line[4:], "b/")
        return True

    def entry(self) -> DiffEntry | None:
        if self.rename_from is not None and self.rename_to is not None:
            return DiffEntry(
                path=self.rename_to,
                kind="R",
                added=self.added,
                removed=self.removed,
                previous_path=self.rename_from,
            )
        # A deletion's ``+++`` side is ``/dev/null``: the file it names is on the ``---`` side.
        # An empty file added or deleted and a mode-only change have no ``---``/``+++`` lines at
        # all, and the ``diff --git`` line is then the only place that names the file.
        path = (
            (self.old_path if self.kind == "D" else self.new_path)
            or self.new_path
            or self.old_path
            or _path_from_git_line(self.git_line)
        )
        if path is None:
            return None
        return DiffEntry(path=path, kind=self.kind, added=self.added, removed=self.removed)


def _close(block: _Block | None, entries: list[DiffEntry]) -> None:
    if block is not None and (entry := block.entry()) is not None:
        entries.append(entry)


def _render_section(section: OverviewSection, *, diff_size: int | None) -> str:
    entries = section.entries
    added, removed = total_counts(entries)
    header = (
        f"{section.title} {section.base_sha} — {_count_files(len(entries))}, +{added} −{removed}"
    )
    if diff_size is not None:
        header += f", full diff {_format_size(diff_size)}"
    lines = [header, ""]
    listed = listed_bytes = 0
    for entry in entries:
        line = _entry_line(entry, section.denied)
        size = len(line.encode("utf-8")) + 1
        if listed == MAX_LISTED_PATHS or listed_bytes + size > MAX_SECTION_BYTES:
            break
        lines.append(line)
        listed += 1
        listed_bytes += size
    rest = entries[listed:]
    if rest:
        added, removed = total_counts(rest)
        lines.append(f"… and {len(rest)} more paths (+{added} −{removed}) not listed")
    return "\n".join(lines) + "\n"


def _entry_line(entry: DiffEntry, denied: frozenset[str]) -> str:
    name = _display(entry.path)
    if entry.previous_path is not None:
        name = f"{_display(entry.previous_path)} → {name}"
    line = f"{entry.kind}  {name}  +{entry.added}  −{entry.removed}"
    if entry.path in denied or entry.previous_path in denied:
        line += "  [denied]"
    return line


def _count_files(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _display(path: str) -> str:
    """*path* as one overview line can carry it: verbatim unless it holds a control character.

    A newline or tab inside a path would split or misalign its line, so such a path is shown in
    git's own C-style quoting — the form ``git diff`` prints it in — rather than raw.
    """
    if not any(_is_control(ch) for ch in path):
        return path
    quoted = (_C_QUOTED.get(ch) or (f"\\{ord(ch):03o}" if _is_control(ch) else ch) for ch in path)
    return '"' + "".join(quoted) + '"'


def _is_control(ch: str) -> bool:
    return ch < " " or ch == "\x7f"


def _side_path(text: str, prefix: str) -> str | None:
    """The path a ``---``/``+++`` line names, or ``None`` for ``/dev/null``.

    git appends a tab to the name on these lines when it contains a space, so the tab is dropped
    before the name is read.
    """
    text = text.removesuffix("\t")
    if text == _DEV_NULL:
        return None
    return _unquote(text).removeprefix(prefix)


def _path_from_git_line(rest: str) -> str | None:
    """The path a ``diff --git a/<p> b/<p>`` line names, for a block that names it nowhere else.

    Only blocks without ``---``/``+++`` and rename lines get here, and in each of them both sides
    name the same file — so an unquoted line splits exactly at its middle, which stays correct for
    a path that itself contains `` b/``.
    """
    if rest.startswith('"'):
        first, tail = _take_quoted(rest)
        if first is None:
            return None
        second = _unquote(tail.removeprefix(" "))
        return second.removeprefix("b/") or first.removeprefix("a/")
    half = (len(rest) - 1) // 2
    left, separator, right = rest[:half], rest[half : half + 1], rest[half + 1 :]
    if separator == " " and left.removeprefix("a/") == right.removeprefix("b/"):
        return right.removeprefix("b/")
    return None


def _take_quoted(text: str) -> tuple[str | None, str]:
    """Split a leading git-quoted string off *text*: ``(unquoted, rest)``, or ``(None, text)``."""
    index = 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == '"':
            return _unquote(text[: index + 1]), text[index + 1 :]
        else:
            index += 1
    return None, text


def _unquote(text: str) -> str:
    """Undo git's C-style quoting of a path; an unquoted *text* is returned as it is."""
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    raw = bytearray()
    body = text[1:-1]
    index = 0
    while index < len(body):
        ch = body[index]
        octal = body[index + 1 : index + 4]
        if ch != "\\" or index + 1 == len(body):
            raw += ch.encode("utf-8")
            index += 1
        elif len(octal) == 3 and all(c in "01234567" for c in octal):
            raw.append(int(octal, 8) & 0xFF)
            index += 4
        else:
            raw += _C_ESCAPES.get(body[index + 1], body[index + 1]).encode("utf-8")
            index += 2
    return raw.decode("utf-8", errors="replace")

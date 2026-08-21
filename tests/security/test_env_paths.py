"""Where an assigned environment variable may point: the two-level path policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.security import env_paths
from wastech_orchestrator.security.env_paths import (
    ProtectedPath,
    assigned_path_elements,
    canonical_collision,
    denied_read_path_collision,
    is_inside,
    lexical_collision,
)

_WORC = (ProtectedPath("the private runtime home", Path("/repo/.worc")),)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/repo/.toolcache/nuget", ("/repo/.toolcache/nuget",)),
        ("~/caches/nuget", ("~/caches/nuget",)),
        ("1", ()),
        ("C.UTF-8", ()),
        ("", ()),
        ("relative/path", ()),
    ],
)
def test_only_path_shaped_values_are_examined(value: str, expected: tuple[str, ...]) -> None:
    # A value the operator did not mean as a path must not be guessed at: `1` is `DOTNET_NOLOGO`,
    # and treating it as a path would turn an ordinary config into a spurious error.
    assert assigned_path_elements(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "/a:/repo/.worc:/b",
        r"C:\a;C:\repo\.worc;C:\b",
    ],
)
def test_a_list_value_is_split_by_both_platform_separators(value: str) -> None:
    # The failure this exists for: unsplit, the string matches no protected root and sails through,
    # while the element in the middle of it is the one that lands on the runtime home.
    elements = assigned_path_elements(value)
    assert any(".worc" in element for element in elements)


def test_ignore_candidates_never_include_a_whole_path_list() -> None:
    value = "/repo/cache/a:/repo/cache/b"
    assert assigned_path_elements(value, include_unsplit=False) == (
        "/repo/cache/a",
        "/repo/cache/b",
    )
    assert value in assigned_path_elements(value)


def test_a_windows_value_survives_a_posix_separator_split() -> None:
    # `os.pathsep` is `:` on POSIX, so the drive letter is torn off there and the remaining fragment
    # is not the path the operator wrote. Keeping the whole value as a candidate too is what makes
    # the answer the same on either OS instead of depending on which host read the config.
    assert r"C:\repo\.toolcache" in assigned_path_elements(r"C:\repo\.toolcache")


def test_collision_is_detected_in_both_directions() -> None:
    # Naming a protected directory's PARENT redirects a cache onto it just as effectively as naming
    # something inside it, so containment is checked both ways.
    assert lexical_collision("/repo/.worc/cache", _WORC) is not None  # inside
    assert lexical_collision("/repo", _WORC) is not None  # parent
    assert lexical_collision("/repo/.worc", _WORC) is not None  # the root itself
    assert lexical_collision("/repo/.toolcache/nuget", _WORC) is None  # a sibling is fine


@pytest.mark.parametrize(
    ("clone_root", "value"),
    [
        (
            "/repo",
            r"\repo\.worc\cache",
        ),  # drive-less: Windows resolves it against the current drive
        ("C:/repo", r"C:\repo\.worc\cache"),  # the ordinary Windows spelling
    ],
)
def test_a_windows_style_value_collides_on_a_posix_host(clone_root: str, value: str) -> None:
    # Backslashes are normalized to separators, so the value is compared component-wise instead of
    # as one opaque name — otherwise a Windows config would validate clean everywhere but Windows.
    protected = (ProtectedPath("the private runtime home", Path(f"{clone_root}/.worc")),)
    assert lexical_collision(value, protected) is not None


def test_the_lexical_level_never_touches_the_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    # The reason the levels are split at all: a config file must get the same verdict on every
    # machine, which it cannot if the answer depends on what this disk happens to hold.
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the lexical level canonicalized a path")

    monkeypatch.setattr(env_paths, "_canonical", explode)
    assert lexical_collision("/repo/.toolcache/nuget", _WORC) is None
    assert lexical_collision("1", _WORC) is None


def test_canonicalization_sees_through_a_symlink(tmp_path: Path) -> None:
    # The case the lexical level cannot catch and the reason preflight owns this half: the value
    # names an innocent path, and only the filesystem knows where it leads.
    protected_dir = tmp_path / ".worc"
    protected_dir.mkdir()
    (tmp_path / "innocent").symlink_to(protected_dir)
    protected = (ProtectedPath("the private runtime home", protected_dir),)
    disguised = str(tmp_path / "innocent" / "cache")
    assert lexical_collision(disguised, protected) is None
    assert canonical_collision(disguised, protected) is not None


def test_windows_folds_case_and_posix_does_not(tmp_path: Path) -> None:
    # Two spellings address one directory on Windows, so a case-sensitive comparison would miss the
    # alias there; on POSIX they are two different directories and folding would be a false alarm.
    # The platform is injected so both branches are covered on any host.
    protected = (ProtectedPath("the private runtime home", tmp_path / ".worc"),)
    variant = str(tmp_path / ".WORC" / "cache")
    assert canonical_collision(variant, protected, system="Windows") is not None
    assert canonical_collision(variant, protected, system="Linux") is None


def test_is_inside_folds_case_only_on_windows(tmp_path: Path) -> None:
    root = tmp_path / "Clone"
    variant = str(tmp_path / "clone" / "cache")
    assert is_inside(variant, root, system="Windows")
    assert not is_inside(variant, root, system="Linux")


def test_a_value_that_cannot_be_resolved_does_not_take_the_gate_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A dead mount or a permission wall is not a reason to refuse a config: the unresolved form is
    # still compared, so the check degrades instead of failing closed on an unrelated path.
    def refuse(self: Path, *_args: object, **_kwargs: object) -> Path:
        raise OSError("no such device")

    monkeypatch.setattr(Path, "resolve", refuse)
    assert canonical_collision("/repo/.worc/cache", _WORC, system="Linux") is not None


def test_is_inside_decides_the_clone_half(tmp_path: Path) -> None:
    # The two branches the cache recipe turns on: inside the clone it must be excluded from git,
    # outside it cannot be written to at all by a sandboxed node.
    clone = tmp_path / "clone"
    clone.mkdir()
    assert is_inside(str(clone / ".toolcache" / "nuget"), clone)
    assert not is_inside(str(tmp_path / "elsewhere"), clone)


def test_denied_read_path_uses_real_glob_semantics() -> None:
    patterns = ("**/private/**", "conf/*.yaml")
    leading_wildcard = denied_read_path_collision(
        "/repo/x/private/cache", Path("/repo"), patterns, canonical=False
    )
    assert leading_wildcard is not None
    assert "**/private/**" in leading_wildcard.label

    unrelated_sibling = denied_read_path_collision(
        "/repo/conf/gocache", Path("/repo"), patterns, canonical=False
    )
    assert unrelated_sibling is None
    matching_file = denied_read_path_collision(
        "/repo/conf/private.yaml", Path("/repo"), patterns, canonical=False
    )
    assert matching_file is not None
    assert "conf/*.yaml" in matching_file.label

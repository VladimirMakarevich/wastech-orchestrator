"""Unit tests for the runtime layout leaf and internal deny policy.

Two invariants are pinned here:

* :meth:`RuntimeLayout.default` resolves ``<repo>/.worc`` for both
  the control and private homes, ``<repo>/.worc-io`` for the exchange), path-for-path, on POSIX and
  Windows path shapes.
* :class:`InternalDenyPolicy` carries the control/private homes, the resolved env-file (which may
  live outside the private home), provider auth homes, and the per-task ``runs/`` root —
  de-duplicated — without touching the public ``security.denied_read_paths`` list.
"""

from __future__ import annotations

import sys
from pathlib import Path, PureWindowsPath

import pytest

from wastech_orchestrator.runtime_layout import (
    CONTROL_HOME_DIRNAME,
    EXCHANGE_HOME_DIRNAME,
    PRIVATE_HOME_DIRNAME,
    RUNS_DIRNAME,
    InternalDenyPolicy,
    RuntimeLayout,
    runs_root,
)


def test_default_reproduces_todays_paths(tmp_path: Path) -> None:
    layout = RuntimeLayout.default(tmp_path)
    assert layout.repo_root == tmp_path
    assert layout.control_home == tmp_path / ".worc"
    assert layout.private_home == tmp_path / ".worc"
    assert layout.exchange_root == tmp_path / ".worc-io"


def test_default_structure_is_os_independent(tmp_path: Path) -> None:
    # The join structure holds on any OS: the home dirs sit directly under the repo root and carry
    # the canonical names, so the guard/wiring never depends on the host separator.
    layout = RuntimeLayout.default(tmp_path)
    assert layout.control_home.name == CONTROL_HOME_DIRNAME
    assert layout.private_home.name == PRIVATE_HOME_DIRNAME
    assert layout.exchange_root.name == EXCHANGE_HOME_DIRNAME
    assert layout.control_home.parent == tmp_path
    assert layout.exchange_root.parent == tmp_path


def test_default_posix_as_posix_form() -> None:
    layout = RuntimeLayout.default("/srv/repo")
    assert layout.control_home.as_posix() == "/srv/repo/.worc"
    assert layout.private_home.as_posix() == "/srv/repo/.worc"
    assert layout.exchange_root.as_posix() == "/srv/repo/.worc-io"


def test_default_accepts_relative_repo_path() -> None:
    layout = RuntimeLayout.default("some/rel/repo")
    assert layout.control_home.as_posix() == "some/rel/repo/.worc"
    assert layout.exchange_root.as_posix() == "some/rel/repo/.worc-io"


def test_default_windows_pure_path_join() -> None:
    # Pure-path check so Windows drive/UNC join semantics are covered on any host: joining the
    # canonical names onto a Windows drive/UNC root yields the expected POSIX display form.
    drive = PureWindowsPath(r"C:\repo")
    assert (drive / CONTROL_HOME_DIRNAME).as_posix() == "C:/repo/.worc"
    assert (drive / EXCHANGE_HOME_DIRNAME).as_posix() == "C:/repo/.worc-io"
    unc = PureWindowsPath(r"\\server\share\repo")
    assert (unc / PRIVATE_HOME_DIRNAME).as_posix() == "//server/share/repo/.worc"


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows drive paths only")
def test_default_on_native_windows_drive(tmp_path: Path) -> None:
    layout = RuntimeLayout.default(r"C:\repo")
    assert layout.control_home.as_posix() == "C:/repo/.worc"
    assert layout.exchange_root.as_posix() == "C:/repo/.worc-io"


def test_runs_home_follows_the_private_home(tmp_path: Path) -> None:
    # Derived, never a stored field: relocating the private home must carry the per-task state with
    # it, or the named deny entry would stop covering the roots it exists for.
    relocated = RuntimeLayout(
        repo_root=tmp_path,
        control_home=tmp_path / "ctrl",
        private_home=tmp_path / "elsewhere" / "priv",
        exchange_root=tmp_path / ".worc-io",
    )
    assert relocated.runs_home == tmp_path / "elsewhere" / "priv" / RUNS_DIRNAME
    assert RuntimeLayout.default(tmp_path).runs_home == tmp_path / ".worc" / RUNS_DIRNAME


def test_runs_root_join_is_os_independent() -> None:
    # Pure-path check so the Windows drive/UNC join is covered on any host.
    assert runs_root("/srv/repo/.worc").as_posix() == "/srv/repo/.worc/runs"
    assert (PureWindowsPath(r"C:\repo\.worc") / RUNS_DIRNAME).as_posix() == "C:/repo/.worc/runs"
    assert (
        PureWindowsPath(r"\\server\share\repo\.worc") / RUNS_DIRNAME
    ).as_posix() == "//server/share/repo/.worc/runs"


def test_runs_home_is_a_named_deny_entry(tmp_path: Path) -> None:
    layout = RuntimeLayout.default(tmp_path)
    policy = InternalDenyPolicy(
        control_home=layout.control_home,
        private_home=layout.private_home,
        env_file=None,
        provider_homes=(),
        runs_home=layout.runs_home,
    )
    assert layout.runs_home in policy.denied_paths


def test_layout_is_immutable(tmp_path: Path) -> None:
    layout = RuntimeLayout.default(tmp_path)
    with pytest.raises((AttributeError, TypeError)):
        layout.private_home = tmp_path  # type: ignore[misc]


def test_deny_policy_collects_all_sources(tmp_path: Path) -> None:
    layout = RuntimeLayout.default(tmp_path)
    env_file = Path("/etc/secrets/prod.env")  # deliberately outside the private home
    claude_home = Path("/home/op/.claude")
    codex_home = Path("/home/op/.codex")
    policy = InternalDenyPolicy(
        control_home=layout.control_home,
        private_home=layout.private_home,
        env_file=env_file,
        provider_homes=(claude_home, codex_home),
    )
    denied = policy.denied_paths
    assert layout.control_home in denied
    assert env_file in denied  # out-of-tree explicit env-file is a deny target
    assert claude_home in denied
    assert codex_home in denied


def test_deny_policy_dedupes_and_orders(tmp_path: Path) -> None:
    layout = RuntimeLayout.default(tmp_path)  # control_home == private_home today
    policy = InternalDenyPolicy(
        control_home=layout.control_home,
        private_home=layout.private_home,
        env_file=None,
        provider_homes=(layout.control_home,),  # duplicate on purpose
    )
    denied = policy.denied_paths
    # control_home and private_home coincide and the provider dup repeats it — collapsed to one.
    assert denied == (layout.control_home,)


def test_deny_policy_env_file_optional(tmp_path: Path) -> None:
    layout = RuntimeLayout.default(tmp_path)
    policy = InternalDenyPolicy(
        control_home=layout.control_home,
        private_home=layout.private_home,
        env_file=None,
        provider_homes=(),
    )
    assert all(isinstance(p, Path) for p in policy.denied_paths)
    assert layout.private_home in policy.denied_paths

"""`watch` reads its pending queue from the configured artifact root, not the current directory
(backlog: interactive installer) — so an installed project's `watch` works from anywhere."""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator import cli
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.install.config_writer import InstallSpec, build_and_validate
from wastech_orchestrator.providers.base import ProviderId


def test_pending_dir_is_under_the_external_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "my-repo-orchestrator"
    spec = InstallSpec(
        repo_url="git@github.com:me/my-repo.git",
        repo_local_path=tmp_path / "my-repo",
        base_branch="main",
        workspace=workspace,
        providers=(ProviderId.CODEX,),
        checks=(),
        create_pull_request=False,
        auto_mode=False,
    )
    config = loads_config(build_and_validate(spec)).config
    assert cli.pending_dir(config) == workspace / "tasks" / "pending"

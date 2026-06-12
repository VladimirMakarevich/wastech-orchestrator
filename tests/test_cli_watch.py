"""`watch` reads its pending queue from the configured artifact root (backlog: interactive
installer) — under the in-repo footprint that is the bound repo itself, not the cwd."""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator import cli
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.install.config_writer import InstallSpec, build_and_validate
from wastech_orchestrator.providers.base import ProviderId


def test_pending_dir_is_under_the_bound_repo(tmp_path: Path) -> None:
    repo = tmp_path / "my-repo"
    spec = InstallSpec(
        repo_url="git@github.com:me/my-repo.git",
        repo_local_path=repo,
        base_branch="main",
        workspace=tmp_path / "my-repo-orchestrator",
        providers=(ProviderId.CODEX,),
        checks=(),
        create_pull_request=False,
        auto_mode=False,
    )
    config = loads_config(build_and_validate(spec)).config
    # in_repo footprint: artifacts (and the pending queue) live in the bound repo, not the cwd.
    assert cli.pending_dir(config) == repo / "tasks" / "pending"

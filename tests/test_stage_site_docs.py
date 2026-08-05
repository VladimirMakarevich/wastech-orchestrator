from pathlib import Path

import pytest
import tools.stage_site_docs as site_docs
import yaml


def flatten_nav(entries: object) -> list[str]:
    """Collect every page target in a MkDocs ``nav`` tree, depth first."""
    targets: list[str] = []
    if isinstance(entries, str):
        targets.append(entries)
    elif isinstance(entries, list):
        for entry in entries:
            targets.extend(flatten_nav(entry))
    elif isinstance(entries, dict):
        for value in entries.values():
            targets.extend(flatten_nav(value))
    return targets


def nav_pages() -> set[str]:
    """The Markdown pages ``mkdocs.yml`` puts in the nav, relative to ``docs_dir``."""
    config = yaml.safe_load((site_docs.repo_root() / "mkdocs.yml").read_text(encoding="utf-8"))
    return {target for target in flatten_nav(config["nav"]) if target.endswith(".md")}


def staged_pages() -> set[str]:
    """The Markdown pages ``stage_site_docs`` copies into the staging tree."""
    root = site_docs.repo_root()
    return {
        site_docs.destination_for(root / source, root).relative_to(site_docs.STAGE_DIR).as_posix()
        for source in site_docs.PUBLIC_MARKDOWN
    }


def test_nav_and_staged_markdown_agree() -> None:
    # `strict: true` aborts the site build on a nav entry with no staged file, and a staged file
    # with no nav entry publishes an orphan page. Pure list comparison, so it guards the pair on
    # `dev` too, where the derived docs/ tree is absent but both files are present. `mkdocs.yml` and
    # `PUBLIC_MARKDOWN` are therefore a pair that must be ported between branches together —
    # editing the nav on `main` alone is what broke the site build (nav gained `flow-authoring.md`
    # in 3634659, the staging list did not).
    assert nav_pages() == staged_pages()


@pytest.mark.skipif(
    not (site_docs.repo_root() / "docs/index.md").exists(),
    reason="the derived docs/ tree lives on main/release only (git-workflow.md §A)",
)
def test_public_markdown_sources_exist() -> None:
    root = site_docs.repo_root()
    missing = [
        source.as_posix() for source in site_docs.PUBLIC_MARKDOWN if not (root / source).is_file()
    ]

    assert missing == []


def test_rewrite_links_keeps_public_docs_relative() -> None:
    root = Path("/repo").resolve()  # resolve() injects the drive on Windows; do it once up front
    source = root / "docs/reference/index.md"
    target = root / "docs/reference/pages/page-one.md"
    public = {source.resolve(), target.resolve()}

    rewritten = site_docs.rewrite_links(
        "[Page One](./pages/page-one.md)",
        source,
        public,
        root,
        "v1.2.3",
    )

    assert rewritten == "[Page One](pages/page-one.md)"


def test_rewrite_links_points_private_paths_to_github() -> None:
    root = Path("/repo").resolve()  # resolve() injects the drive on Windows; do it once up front
    source = root / "docs/reference/index.md"
    public = {source.resolve()}

    rewritten = site_docs.rewrite_links(
        "[cli.py](../../src/wastech_orchestrator/cli.py#L114)",
        source,
        public,
        root,
        "v1.2.3",
    )

    assert (
        rewritten == "[cli.py](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/"
        "v1.2.3/src/wastech_orchestrator/cli.py#L114)"
    )


def test_rewrite_links_leaves_external_and_anchor_links_unchanged() -> None:
    root = Path("/repo").resolve()  # resolve() injects the drive on Windows; do it once up front
    source = root / "docs/index.md"
    public = {source.resolve()}
    markdown = "[local](#start) [remote](https://example.com/docs)"

    assert site_docs.rewrite_links(markdown, source, public, root, "main") == markdown

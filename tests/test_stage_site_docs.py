from pathlib import Path

import tools.stage_site_docs as site_docs


def test_rewrite_links_keeps_public_docs_relative() -> None:
    root = Path("/repo").resolve()  # resolve() injects the drive on Windows; do it once up front
    source = root / "docs/functional/index.md"
    target = root / "docs/functional/blocks/B01-cli-and-operator-commands.md"
    public = {source.resolve(), target.resolve()}

    rewritten = site_docs.rewrite_links(
        "[B01](./blocks/B01-cli-and-operator-commands.md)",
        source,
        public,
        root,
        "v1.2.3",
    )

    assert rewritten == "[B01](blocks/B01-cli-and-operator-commands.md)"


def test_rewrite_links_points_private_paths_to_github() -> None:
    root = Path("/repo").resolve()  # resolve() injects the drive on Windows; do it once up front
    source = root / "docs/functional/index.md"
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

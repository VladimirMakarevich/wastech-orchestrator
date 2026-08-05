# Install and upgrade flow (git-tag friction → documented recipe → PyPI)

Status: **partially implemented** (2026-07-06) — short term (git-tag recipe) done; medium/optional deferred to backlog. Date: 2026-07-04 Owner: Vladimir Makarevich

This is a design record for how operators install and upgrade the orchestrator CLI. It captures the friction found while testing `v0.8.6a3` on the `wastech-mdlint` target repo, and lays out a phased path: document the correct git-tag recipe now, move to PyPI publication as the real fix, and optionally add a `worc self-update` helper. It is a stake-in-the-ground for the install story, not an implementation spec.

**Implementation status (2026-07-06):** only the **short-term** decision below is implemented — the git-tag install/upgrade recipe is documented in [operations.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/operations.md#upgrading-the-orchestrator) (the uninstall+reinstall commands, the `pipx upgrade`/`--force`+uv caveats, and the per-shell zsh/bash/PowerShell quoting note for the `[shell]` extra). The **medium-term** (PyPI) and **optional** (`worc self-update`) phases remain in the backlog — publishing to (Test)PyPI (and verifying the `worc` name is free) and `worc self-update` are both still open. See the Decision section for the per-phase state.

## The problem

The orchestrator is installed via `pipx` from a **pinned git tag**, e.g. `git+https://github.com/VladimirMakarevich/wastech-orchestrator.git@v0.8.0a1`. This surfaced three separate failures during a routine upgrade to `v0.8.6a3`:

1. **`pipx upgrade` never moves the version.** The install spec is pinned to a tag (`@v0.8.0a1`), so pipx considers the pinned ref "already at latest" and refuses to advance — regardless of newer tags. `--pip-args="--pre"` has no effect here, because the version is coming from a git ref, not a PyPI index that pre-release filtering applies to. The operator gets a misleading "already at latest version 0.8.0a1" message.
2. **`pipx install --force` fails on the uv backend.** With uv as the venv backend, `--force` errors with "A virtual environment already exists ... Not removing existing venv ... because it was not created in this session". The documented workaround (`--clear` / `UV_VENV_CLEAR=1`) is not what pipx passes, so `--force` is effectively broken for reinstalling into an existing venv. The working path is `pipx uninstall` followed by a fresh `pipx install`.
3. **Extras need PEP 508 form and shell quoting.** `worc shell` requires the `[shell]` extra (`prompt_toolkit`). `pip install wastech-orchestrator[shell]` fails under zsh (`no matches found` — zsh globs the brackets), and even quoted it targets PyPI, not the installed git package. The correct form is the quoted PEP 508 spec `"wastech-orchestrator[shell] @ git+https://...@v0.8.6a3"`. None of this is documented, so each upgrade is a rediscovery.

The net effect: there is no reliable, documented one-liner to upgrade the CLI, and the tooling actively misleads (`upgrade` reports success while doing nothing; `--force` looks like the fix but fails).

## Constraints

- **Greenfield MVP, no deployment.** There is no installed base to migrate; we are free to change the install story without back-compat machinery.
- **Credentials/auth stay outside the orchestrator.** A `self-update` helper must not touch provider or GitHub credentials; it only shells out to `pipx`.
- **Cross-platform is mandatory.** Any documented recipe or helper must work on Windows / Linux / macOS — shell-quoting guidance is shell-specific (zsh vs bash vs PowerShell) and must be called out per shell.
- **No shell interpolation of user strings** if a helper command is ever built: an argv list to `pipx`, not a formatted string.

## Alternatives considered

| Option | Why not (alone) |
| --- | --- |
| Do nothing | Every upgrade stays a manual rediscovery of the uninstall+reinstall+quoting dance; the `upgrade`/`--force` traps keep misleading operators. |
| Only document the git recipe | Cheap and correct, but upgrade stays fully manual and the `pipx upgrade` mental model stays broken. |
| Only publish to PyPI | The clean end state (`pipx upgrade` and `--pre` work, extras install as `pkg[shell]`), but needs a publish account, a release/CI process, and a version-tagging discipline that doesn't exist yet — too much to be the immediate fix. |
| Only add `worc self-update` | Convenient, but it's new code wrapping `pipx uninstall`+`install` for a problem that PyPI solves natively; risks encoding the git-tag workaround as a permanent feature. |

## Decision

Adopt all three, phased, because each unblocks the next and none alone is sufficient:

- **Short term — document the git-tag recipe. ✅ DONE (2026-07-06).** Added the install/upgrade recipe to [operations.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/operations.md#upgrading-the-orchestrator) with the working commands: `pipx uninstall wastech-orchestrator` then `pipx install "wastech-orchestrator[shell] @ git+https://github.com/VladimirMakarevich/wastech-orchestrator.git@vX.Y.Z"`. It explains _why_ `pipx upgrade` and `--force` don't work with a pinned git tag + uv backend, and gives the zsh/bash/PowerShell quoting note for the `[shell]` extra. This removes the daily friction immediately at near-zero cost.
- **Medium term — publish to PyPI (the real fix). ⏳ BACKLOG.** Once the package is on a real index, `pipx upgrade` and `--pre` work as designed, extras install as `pkg[shell]`, and the pinned-git-ref trap disappears. This becomes the recommended install path and demotes the git recipe to a "from source" fallback. Still open: publish to (Test)PyPI and verify `worc` is free there.
- **Optional — `worc self-update`. ⏳ BACKLOG.** A thin helper that runs the correct `pipx uninstall`+`install` (argv list, no shell interpolation) for a requested tag/version with extras. Worth it only if git-tag installs remain the norm after PyPI lands; if PyPI covers the common case, this stays optional.

The cost of not picking PyPI immediately is that the medium-term fix is deferred behind a release process; the cost of not doing the docs first is continued operator friction on every upgrade in the meantime.

## Open questions

- PyPI publication: package name availability, publish account/ownership, and whether releases are cut manually or via CI on tag push.
- Versioning discipline for pre-releases (`aN` suffixes) once on PyPI — does `--pre` become the default operator instruction, or do we cut non-pre releases?
- Is `worc self-update` worth building at all if PyPI lands, or does it only exist to paper over the git-tag era?

## Implementation notes

- Docs: add an "Install & upgrade" section (likely [docs/operations.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/operations.md) or the top-level [docs/index.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/index.md)) with the git-tag recipe and the `pipx upgrade`/`--force`/uv/quoting caveats. This is doc-only and can ship independently of any code.
- Extras: the `shell` extra is declared in [pyproject.toml](../../../pyproject.toml) `[project.optional-dependencies]` (`prompt_toolkit>=3`); the recipe must carry `[shell]` in the PEP 508 spec so `worc shell` works after reinstall.
- PyPI: build/publish config in `pyproject.toml`; a release workflow (CI on tag) is the natural home so tags and published versions stay in lockstep with the existing `vX.Y.Z` tagging.
- `worc self-update` (if built): a new `cmd_self_update` in [src/wastech_orchestrator/cli.py](../../../src/wastech_orchestrator/cli.py), delegating to `pipx` via the process seam as an argv list; must not touch credentials.

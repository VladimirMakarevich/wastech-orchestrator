"""The executables the orchestrator launches as itself resolve once, not per call (ТA.1.7.3)."""

from __future__ import annotations

from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.security.launchers import (
    PinnedLaunchers,
    pin_launchers,
    resolve_launcher,
)


def _which(table: dict[str, str]):
    """A ``shutil.which`` stand-in over ``table`` — so a host can be described, not required."""
    return lambda name: table.get(name)


def test_a_name_resolves_to_the_path_path_would_pick() -> None:
    assert resolve_launcher("git", which=_which({"git": "/usr/bin/git"})) == "/usr/bin/git"
    assert resolve_launcher("git", which=_which({})) is None


def test_git_and_gh_are_pinned_on_every_host(make_git_config, tmp_path) -> None:
    # The two that publish. Host-independent because the risk is: whichever machine runs a task, a
    # substitute for either commits and pushes wherever it likes while reporting what we expect.
    config: OrchestratorConfig = make_git_config(tmp_path / "clone")
    for system in ("Windows", "Linux", "Darwin"):
        pins = pin_launchers(config, which=_which({"git": "/g", "gh": "/h"}), system=system)
        assert pins.launch("git") == "/g"
        assert pins.launch("gh") == "/h"


def test_the_daemon_launcher_is_pinned_and_printed(make_git_config, tmp_path) -> None:
    # Ам2-7: ТA.1.7 names the daemon launcher among the classes to pin, and the shipped guide says
    # the report prints it. It was resolved in `cli_shell` for the spawn, printed nowhere, and never
    # re-checked for drift — while being the one path that hands the NEXT WHOLE RUN to whatever
    # answers to the name.
    config: OrchestratorConfig = make_git_config(tmp_path / "clone")
    for system in ("Windows", "Linux", "Darwin"):
        pins = pin_launchers(
            config,
            which=_which({"git": "/g", "gh": "/h", "worc": "/usr/local/bin/worc"}),
            system=system,
        )
        assert pins.launch("worc") == "/usr/local/bin/worc"
        assert any("worc -> /usr/local/bin/worc" in line for line in pins.describe())


def test_the_host_specific_names_follow_the_host_not_the_config(make_git_config, tmp_path) -> None:
    """``ps`` is POSIX-only and ``bwrap``/``socat`` are Linux-only, so the pinned set differs.

    Both branches are exercised by substituting the platform rather than by running on it: the point
    of the set is which names this host can even have, and a test that only ever saw the developer's
    machine would pin one third of the answer.
    """
    config: OrchestratorConfig = make_git_config(tmp_path / "clone")
    table = _which({"git": "/g", "gh": "/h", "ps": "/bin/ps", "bwrap": "/b", "socat": "/s"})
    windows = pin_launchers(config, which=table, system="Windows").paths
    linux = pin_launchers(config, which=table, system="Linux").paths
    darwin = pin_launchers(config, which=table, system="Darwin").paths
    # Windows proves quiescence with a Job Object and launches nothing to do it.
    assert "ps" not in windows and "bwrap" not in windows
    assert linux["ps"] == "/bin/ps" and linux["bwrap"] == "/b" and linux["socat"] == "/s"
    assert darwin["ps"] == "/bin/ps" and "bwrap" not in darwin  # macOS sandboxes without bubblewrap


def test_every_configured_provider_command_is_pinned(make_git_config, tmp_path) -> None:
    # Configured, not merely routed to: an operator who declared a provider wants to know which
    # binary was found for it whether or not today's flow reaches that node.
    config: OrchestratorConfig = make_git_config(tmp_path / "clone")
    pins = pin_launchers(
        config,
        which=_which({"git": "/g", "gh": "/h", "claude": "/c/claude", "codex": "/c/codex"}),
        system="Darwin",
    )
    assert pins.launch("claude") == "/c/claude"
    assert pins.launch("codex") == "/c/codex"


def test_an_unresolvable_name_falls_back_to_itself_and_is_reported() -> None:
    """A missing binary must stay the diagnostic it already is, not become an ``Optional``.

    Handing the bare name back keeps the process runner's "could not launch" error — which names the
    command and is what an operator can act on — instead of turning every call site into a branch.
    """
    pins = PinnedLaunchers({"git": "/usr/bin/git", "gh": None})
    assert pins.launch("gh") == "gh"
    assert pins.missing() == ("gh",)
    assert "gh -> <not found on PATH>" in pins.describe()[1]


def test_the_report_names_the_path_and_what_it_is_for() -> None:
    # A pin nobody can read is not a control: the operator has to be able to see WHICH `git` will do
    # the pushing, which means the path itself, not a claim that one was resolved.
    described = PinnedLaunchers({"git": "/opt/shim/git"}).describe()
    assert described == ("git -> /opt/shim/git (the orchestrator's own commits and pushes)",)


def test_a_replacement_during_the_run_is_reported_as_drift() -> None:
    """The window pinning exists for: `PATH` re-resolving mid-run to something else.

    Checked once, where the Git control-state comparison already runs — after the agent finished
    and before the orchestrator commits and pushes. By then the orchestrator has been using the
    pinned path all along, so a drift line reports an attempt to redirect it, not a success.
    """
    pins = PinnedLaunchers({"git": "/usr/bin/git", "gh": "/usr/bin/gh"})
    assert pins.drift(which=_which({"git": "/usr/bin/git", "gh": "/usr/bin/gh"})) == ()
    lines = pins.drift(which=_which({"git": "/tmp/evil/git", "gh": "/usr/bin/gh"}))
    assert len(lines) == 1
    assert "/usr/bin/git" in lines[0] and "/tmp/evil/git" in lines[0]
    assert "kept using the path it pinned" in lines[0]


def test_a_binary_that_disappears_is_drift_too() -> None:
    # Both directions: a name that stops resolving changed what a bare-name launch would reach just
    # as much as one that starts resolving elsewhere.
    pins = PinnedLaunchers({"gh": "/usr/bin/gh"})
    lines = pins.drift(which=_which({}))
    assert len(lines) == 1 and "<nothing>" in lines[0]

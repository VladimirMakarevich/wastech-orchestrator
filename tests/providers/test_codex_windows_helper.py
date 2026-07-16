"""Windows Codex sandbox-helper discovery, PATH augmentation, and the early preflight guard.

Regression coverage for the Windows-10 incident where ``codex`` in ``workspace-write`` launched
``codex-windows-sandbox-setup.exe`` by name but the helper's ``codex-resources`` directory was not
on the child ``PATH``. The pure resolver is exercised with injected ``system``/``which`` seams so
these run on any host; the provider methods are driven with ``platform.system`` / ``shutil.which``
monkeypatched to a fake Windows package laid out under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from wastech_orchestrator.config.schema import ProviderConfig, SecurityConfig
from wastech_orchestrator.providers import codex as codex_mod
from wastech_orchestrator.providers.codex import CodexProvider, resolve_codex_resources_dir
from wastech_orchestrator.providers.process import ProcessResult

FIXED_TIME = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _make_pkg(root: Path, *, with_helper: bool, with_manifest: bool = True) -> tuple[Path, Path]:
    """Lay out a fake Codex standalone package under ``root``; return ``(codex.exe, resources)``.

    Mirrors the real layout: ``<release>/bin/codex.exe`` with a sibling ``codex-resources`` dir
    holding the sandbox helper, and a ``codex-package.json`` naming ``resourcesDir``.
    """
    pkg = root / "releases" / "0.144.4-x86_64-pc-windows-msvc"
    (pkg / "bin").mkdir(parents=True)
    exe = pkg / "bin" / "codex.exe"
    exe.write_text("binary", encoding="utf-8")
    if with_manifest:
        (pkg / codex_mod._PACKAGE_MANIFEST_NAME).write_text(
            json.dumps({"entrypoint": "bin/codex.exe", "resourcesDir": "codex-resources"}),
            encoding="utf-8",
        )
    resources = pkg / "codex-resources"
    resources.mkdir()
    if with_helper:
        (resources / codex_mod._SANDBOX_HELPER_EXE).write_text("helper", encoding="utf-8")
    return exe, resources


class _VersionAndHelpFake:
    """A preflight fake answering ``codex --version`` and ``codex exec --help`` (with ``-c``)."""

    def __init__(self, *, help_has_config: bool = True) -> None:
        self._help_has_config = help_has_config

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Any,
        env: Any,
        timeout_seconds: int,
        stdout_path: Any,
        stdin_text: str | None = None,
        monotonic: Any = None,
    ) -> ProcessResult:
        if "--version" in argv:
            out = "codex-cli 0.144.4\n"
        elif "exec" in argv and "--help" in argv:
            out = "Usage: codex exec [OPTIONS]\n  --model <M>\n"
            if self._help_has_config:
                out += (
                    "  -c, --config <key=value>\n"
                    "  --disable <FEATURE>\n"
                    "  --ignore-rules\n"
                    "  --ignore-user-config\n"
                    "  --strict-config\n"
                )
        else:
            out = ""
        Path(stdout_path).write_text(out, encoding="utf-8")
        return ProcessResult(
            exit_code=0,
            timed_out=False,
            launch_error=None,
            duration_seconds=0.1,
            stdout_path=str(stdout_path),
            stderr_text="",
        )


def _patch_windows(
    monkeypatch: pytest.MonkeyPatch, exe: Path, *, helper_on_path: bool = False
) -> None:
    """Make the Codex adapter see a Windows host whose ``codex`` resolves to ``exe``.

    Also points ``%USERPROFILE%`` at a package-free directory so the resolver's well-known fallback
    cannot pick up a real Codex install on the host running the tests (hermeticity).
    """
    monkeypatch.setattr(codex_mod.platform, "system", lambda: "Windows")
    monkeypatch.setenv("USERPROFILE", str(exe.parent.parent))

    def _which(cmd: str, path: str | None = None, mode: int = os.F_OK | os.X_OK) -> str | None:
        if cmd == "codex":
            return str(exe)
        if cmd == codex_mod._SANDBOX_HELPER_EXE:
            return str(exe.parent) if helper_on_path else None
        return None

    monkeypatch.setattr(codex_mod.shutil, "which", _which)


def _provider(
    config: ProviderConfig, security: SecurityConfig, root: Path, fake: Any
) -> CodexProvider:
    return CodexProvider(
        config, security=security, artifacts_root=root, clock=lambda: FIXED_TIME, run_process=fake
    )


# --- the pure resolver (injected seams; runs on any host) -------------------------------------


def test_resolver_returns_none_off_windows(tmp_path: Path) -> None:
    exe, _ = _make_pkg(tmp_path, with_helper=True)
    assert resolve_codex_resources_dir("codex", system="Linux", which=lambda _c: str(exe)) is None


def test_resolver_finds_helper_dir_from_executable(tmp_path: Path) -> None:
    exe, resources = _make_pkg(tmp_path, with_helper=True)
    got = resolve_codex_resources_dir(
        "codex", system="Windows", which=lambda _c: str(exe), userprofile=""
    )
    assert got is not None and got.samefile(resources)


def test_resolver_missing_helper_returns_none(tmp_path: Path) -> None:
    exe, _ = _make_pkg(tmp_path, with_helper=False)
    assert (
        resolve_codex_resources_dir(
            "codex", system="Windows", which=lambda _c: str(exe), userprofile=""
        )
        is None
    )


def test_resolver_defaults_dirname_without_manifest(tmp_path: Path) -> None:
    exe, resources = _make_pkg(tmp_path, with_helper=True, with_manifest=False)
    got = resolve_codex_resources_dir(
        "codex", system="Windows", which=lambda _c: str(exe), userprofile=""
    )
    assert got is not None and got.samefile(resources)


def test_resolver_falls_back_to_userprofile_package(tmp_path: Path) -> None:
    # `which` finds no executable; the well-known %USERPROFILE% standalone package holds the helper.
    current = tmp_path / "home" / ".codex" / "packages" / "standalone" / "current"
    current.mkdir(parents=True)
    (current / codex_mod._PACKAGE_MANIFEST_NAME).write_text(
        json.dumps({"resourcesDir": "codex-resources"}), encoding="utf-8"
    )
    res = current / "codex-resources"
    res.mkdir()
    (res / codex_mod._SANDBOX_HELPER_EXE).write_text("helper", encoding="utf-8")
    got = resolve_codex_resources_dir(
        "codex", system="Windows", which=lambda _c: None, userprofile=str(tmp_path / "home")
    )
    assert got is not None and got.samefile(res)


# --- child-env augmentation -------------------------------------------------------------------


def test_augment_prepends_resources_dir_onto_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
) -> None:
    exe, resources = _make_pkg(tmp_path, with_helper=True)
    _patch_windows(monkeypatch, exe)
    provider = _provider(codex_config, security_config, tmp_path, _VersionAndHelpFake())
    env = provider._augment_child_env({"PATH": r"C:\existing"})
    assert env["PATH"].split(os.pathsep)[0] == str(resources)
    assert r"C:\existing" in env["PATH"]


def test_augment_is_noop_when_helper_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
) -> None:
    exe, _ = _make_pkg(tmp_path, with_helper=False)
    _patch_windows(monkeypatch, exe)
    provider = _provider(codex_config, security_config, tmp_path, _VersionAndHelpFake())
    assert provider._augment_child_env({"PATH": r"C:\existing"}) == {"PATH": r"C:\existing"}


# --- preflight guard --------------------------------------------------------------------------


def test_preflight_ready_when_helper_discoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
) -> None:
    exe, resources = _make_pkg(tmp_path, with_helper=True)
    _patch_windows(monkeypatch, exe)
    provider = _provider(codex_config, security_config, tmp_path, _VersionAndHelpFake())
    health = provider.preflight()
    assert health.supports_required_features is True
    assert "Windows sandbox helper" in health.message
    assert str(resources) in health.message


def test_preflight_not_ready_when_helper_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
) -> None:
    exe, _ = _make_pkg(tmp_path, with_helper=False)
    _patch_windows(monkeypatch, exe)
    provider = _provider(codex_config, security_config, tmp_path, _VersionAndHelpFake())
    health = provider.preflight()
    assert health.supports_required_features is False
    assert codex_mod._SANDBOX_HELPER_EXE in health.message
    assert "not discoverable" in health.message


def test_preflight_ready_and_augments_when_helper_off_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
) -> None:
    # Helper exists in the package but is NOT on PATH: preflight is ready (package resolution finds
    # it) and augmentation is what actually puts it on the child PATH.
    exe, resources = _make_pkg(tmp_path, with_helper=True)
    _patch_windows(monkeypatch, exe, helper_on_path=False)
    provider = _provider(codex_config, security_config, tmp_path, _VersionAndHelpFake())
    assert provider.preflight().supports_required_features is True
    env = provider._augment_child_env({"PATH": r"C:\existing"})
    assert env["PATH"].split(os.pathsep)[0] == str(resources)


def test_preflight_skips_helper_check_for_readonly_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
) -> None:
    # A read-only sandbox never launches the Windows helper, so a missing helper must not block it.
    exe, _ = _make_pkg(tmp_path, with_helper=False)
    _patch_windows(monkeypatch, exe)
    read_only = replace(codex_config, sandbox="read-only", permission_profile="read-only")
    provider = _provider(read_only, security_config, tmp_path, _VersionAndHelpFake())
    assert provider.preflight().supports_required_features is True

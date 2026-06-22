"""Argv dependency scanners (P3.1) — evidence, not a gate.

Runs the core-owned set of dependency / advisory scanners as argv child processes (never a shell
string) through the safe process runner — mandatory timeout, allowlisted environment — and
structures each scanner's run as evidence. The scan **never gates**: it always reports
``passed=True`` (the scan ran) so the ``checks`` node stays uniformly pass/fail and the engine needs
no "this checker doesn't gate" special case (co-design note #3). Whether the findings gate the flow
is the flow's decision, expressed by its edges (``dependency_scan → threat_analysis (pass)``).

The flow cannot specify scanners (security-ceiling) — the set is core-owned here. A scanner that
is not installed *launch-fails* and contributes no findings: a missing tool is not a quality failure
for an evidence scan (unlike a gating ``command_profile`` check, where a launch failure is infra).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.providers.process import ProcessResult, run_process

RunProcess = Callable[..., ProcessResult]

#: The core-owned scanner set: ``(name, argv)`` pairs. Each argv is a list (never a shell string).
#: JSON output is requested where supported so the captured report is machine-readable evidence.
DEFAULT_DEPENDENCY_SCANNERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pip-audit", ("pip-audit", "--format", "json", "--progress-spinner", "off")),
    ("osv-scanner", ("osv-scanner", "--format", "json", "-r", ".")),
)


@dataclass(frozen=True, slots=True)
class ScannerRun:
    """One scanner invocation as structured evidence."""

    name: str
    command: str
    exit_code: int | None
    timed_out: bool
    launched: bool  # False → the scanner binary is not installed (no findings, not a failure)
    report_path: str


@dataclass(frozen=True, slots=True)
class DependencyScanReport:
    """The aggregate of every scanner run. ``passed`` is always ``True`` (evidence, not a gate)."""

    runs: tuple[ScannerRun, ...]
    passed: bool = True


def run_dependency_scan(
    *,
    repo_dir: str | Path,
    logs_dir: str | Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    run_process: RunProcess = run_process,
    scanners: tuple[tuple[str, tuple[str, ...]], ...] = DEFAULT_DEPENDENCY_SCANNERS,
) -> DependencyScanReport:
    """Run each core-owned scanner argv against *repo_dir*, capturing its output as evidence.

    Each scanner's stdout is streamed to ``<logs_dir>/<name>.json``. A launch failure (binary not
    installed) is recorded as ``launched=False`` and contributes no findings — the scan still
    ``passed`` (it is evidence, not a gate).
    """
    out = Path(logs_dir)
    out.mkdir(parents=True, exist_ok=True)
    runs: list[ScannerRun] = []
    for name, argv in scanners:
        report_path = out / f"{name}.json"
        result = run_process(
            list(argv),
            cwd=repo_dir,
            env=env,
            timeout_seconds=timeout_seconds,
            stdout_path=str(report_path),
        )
        runs.append(
            ScannerRun(
                name=name,
                command=" ".join(argv),
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                launched=result.launch_error is None,
                report_path=str(report_path),
            )
        )
    return DependencyScanReport(runs=tuple(runs))

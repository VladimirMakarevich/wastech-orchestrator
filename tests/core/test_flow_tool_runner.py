"""Custom tool node runner — execution under the process ceiling + outcome contract.

Exercises the runner with a fake ``run_process`` and a fake tool registry so we can assert the
security ceiling (argv-no-shell, allowlisted env, no secrets on stdin, redacted artifacts), the
infra-vs-quality split (launch-error/timeout → manual), the exit-code / JSON outcome contract, and
the compose seam (stdout exposed as ``{<node_id>_path}`` and findings/data recorded, never applied).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from wastech_orchestrator.core.flow.contracts import (
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
)
from wastech_orchestrator.core.flow.engine import NodeContext
from wastech_orchestrator.core.flow.nodes import AgentNodeRunner, NodeInputs, NodeServices
from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired
from wastech_orchestrator.core.flow.nodes.tool import ToolNodeRunner, _launch_argv
from wastech_orchestrator.core.flow.prompt_vars import node_output_vars
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import AgentNode, FlowDoc, FlowNode, ToolNode
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.providers.artifacts import (
    TOOL_STDERR_FILENAME,
    TOOL_STDOUT_FILENAME,
    node_run_dir,
)
from wastech_orchestrator.providers.process import ProcessResult

# -- fakes --------------------------------------------------------------------


class FakeRunProcess:
    """Captures the launch and writes scripted stdout to the sink, like the real safe runner."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = 0,
        timed_out: bool = False,
        launch_error: str | None = None,
    ) -> None:
        self._stdout, self._stderr = stdout, stderr
        self._exit_code, self._timed_out, self._launch_error = exit_code, timed_out, launch_error
        self.calls: list[dict[str, Any]] = []

    def set_result(
        self,
        *,
        stdout: str,
        stderr: str = "",
        exit_code: int | None,
        timed_out: bool = False,
        launch_error: str | None = None,
    ) -> None:
        """Script the next and subsequent calls while preserving the captured-call history."""
        self._stdout, self._stderr = stdout, stderr
        self._exit_code, self._timed_out, self._launch_error = exit_code, timed_out, launch_error

    def __call__(
        self,
        argv: Any,
        *,
        cwd: Any,
        env: Any,
        timeout_seconds: int,
        stdout_path: Any,
        stdin_text: str | None = None,
        **_: Any,
    ) -> ProcessResult:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "env": dict(env),
                "timeout": timeout_seconds,
                "stdin": stdin_text,
            }
        )
        Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
        Path(stdout_path).write_text(self._stdout, encoding="utf-8")
        return ProcessResult(
            exit_code=self._exit_code,
            timed_out=self._timed_out,
            launch_error=self._launch_error,
            duration_seconds=0.1,
            stdout_path=str(stdout_path),
            stderr_text=self._stderr,
        )


class FakeToolRegistry:
    def __init__(self, path: Path) -> None:
        self._path = path

    def resolve(self, name: str) -> Path:
        return self._path


class FakeStore:
    def __init__(self) -> None:
        self.recorded: list[Any] = []
        self.completed: list[dict[str, Any]] = []
        self._next = 1

    def record_node_run(self, run: Any, conn: Any = None) -> int:
        rid = self._next
        self._next += 1
        self.recorded.append(run)
        return rid

    def complete_node_run(self, run_id: int, **kwargs: Any) -> None:
        self.completed.append({"run_id": run_id, **kwargs})


# -- builders -----------------------------------------------------------------


def _snapshot(*nodes: FlowNode) -> FlowSnapshot:
    doc = FlowDoc(
        name="t",
        task_type="t",
        permission_ceiling=PermissionProfile.WORKSPACE_WRITE,
        output_policy=OutputPolicy.CODE_CHANGE,
        publishing=PublishingPolicy.PULL_REQUEST,
        nodes=tuple(nodes),
        edges=(),
        budgets=MappingProxyType({}),
    )
    return FlowSnapshot(
        doc=doc,
        nodes_by_id=MappingProxyType({n.id: n for n in nodes}),
        adjacency=MappingProxyType({}),
        flow_fingerprint="fp",
    )


def _services(
    tmp_path: Path,
    run_process: FakeRunProcess,
    store: FakeStore,
    *,
    process_env: dict[str, str] | None = None,
    tools_default_timeout_seconds: int = 3600,
    prompt_secrets: tuple[str, ...] = (),
    register_artifact: Any = None,
    tool_path: Path | None = None,
    git: Any = None,
) -> NodeServices:
    return NodeServices(
        router=None,  # type: ignore[arg-type]  # the tool runner never touches router/checks
        check_runner=None,  # type: ignore[arg-type]
        store=store,
        git=git,
        repo_dir=str(tmp_path / "repo"),
        artifacts_root=str(tmp_path / "art"),
        clock=lambda: "ts",
        run_process=run_process,
        process_env=process_env if process_env is not None else {"PATH": "/usr/bin"},
        tool_registry=FakeToolRegistry(tool_path or (tmp_path / "tools" / "md-check")),
        tools_default_timeout_seconds=tools_default_timeout_seconds,
        prompt_secrets=prompt_secrets,
        register_artifact=register_artifact,
    )


def _inputs(tmp_path: Path) -> NodeInputs:
    return NodeInputs(
        flow_dir=tmp_path,
        task_path="/t/task.md",
        plan_path="/t/plan.md",
        diff_path=None,
        checks_path="/t/checks",
        review_path=None,
    )


def _ctx(
    snapshot: FlowSnapshot, node: FlowNode, run_state: FlowRunState | None = None
) -> NodeContext:
    return NodeContext(
        snapshot=snapshot,
        run_state=run_state or FlowRunState(flow_fingerprint="fp"),
        node=node,
        task_id="task-1",
    )


_TOOL = ToolNode(id="md-check", kind="tool", tool="md-check", args={"min_chars": 500})


def _run(tmp_path: Path, run_process: FakeRunProcess, store: FakeStore, **svc_kw: Any) -> Any:
    node = _TOOL
    snap = _snapshot(node)
    services = _services(tmp_path, run_process, store, **svc_kw)
    return ToolNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(snap, node))


# -- launch argv (both os.name branches) --------------------------------------


def test_launch_argv_posix_runs_the_tool_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    path = Path("/repo/.worc/tools/check_chapter")
    assert _launch_argv(path) == [str(path)]


def test_launch_argv_windows_batch_runs_through_comspec(monkeypatch: pytest.MonkeyPatch) -> None:
    # CreateProcess cannot start a .cmd/.bat directly under shell=False; it must go through the
    # interpreter — else every content-flow run would park to manual on Windows.
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    path = Path(r"C:\repo\.worc\tools\check_chapter.cmd")
    assert _launch_argv(path) == [r"C:\Windows\System32\cmd.exe", "/c", str(path)]


def test_launch_argv_windows_exe_runs_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    # A PE image (.exe) is launchable by CreateProcess directly — no interpreter wrapper needed.
    monkeypatch.setattr(os, "name", "nt")
    path = Path(r"C:\repo\.worc\tools\tool.exe")
    assert _launch_argv(path) == [str(path)]


# -- ceiling ------------------------------------------------------------------


def test_tool_runs_under_run_process_argv_no_shell(tmp_path: Path) -> None:
    fake, store = FakeRunProcess(stdout="ok", exit_code=0), FakeStore()
    tool_path = tmp_path / "tools" / "md-check"
    _run(tmp_path, fake, store, tool_path=tool_path)
    call = fake.calls[0]
    assert call["argv"] == [str(tool_path)]  # a list — the executable path, never a shell string
    assert call["cwd"] == str(tmp_path / "repo")


def test_tool_stdin_has_no_secrets_or_full_env(tmp_path: Path) -> None:
    fake, store = FakeRunProcess(exit_code=0), FakeStore()
    _run(tmp_path, fake, store)
    ctx = json.loads(fake.calls[0]["stdin"])
    assert ctx["task_id"] == "task-1"
    assert ctx["node_id"] == "md-check"
    assert ctx["subtask_order"] is None
    assert ctx["args"] == {"min_chars": 500}
    # Only the fixed allowlisted paths — never secrets, a full env, or a session id.
    assert set(ctx["paths"]) == {
        "repo",
        "task_path",
        "plan_path",
        "diff_path",
        "checks_path",
        "review_path",
    }
    assert "env" not in ctx and "session_id" not in ctx


def test_tool_child_env_is_allowlisted(tmp_path: Path) -> None:
    fake, store = FakeRunProcess(exit_code=0), FakeStore()
    allowlist = {"PATH": "/usr/bin", "HOME": "/home/u"}
    _run(tmp_path, fake, store, process_env=allowlist)
    # The child gets exactly the allowlisted env passed in — no secret-shaped names leak in.
    assert fake.calls[0]["env"] == allowlist
    assert not any("TOKEN" in k or "SECRET" in k for k in fake.calls[0]["env"])


# -- infra vs quality ---------------------------------------------------------


def test_tool_timeout_and_launch_error_go_manual(tmp_path: Path) -> None:
    for kwargs, status in (
        ({"timed_out": True, "exit_code": None}, "timeout"),
        ({"launch_error": "boom", "exit_code": None}, "launch_error"),
    ):
        store = FakeStore()
        with pytest.raises(NodeManualRequired):
            _run(tmp_path, FakeRunProcess(**kwargs), store)
        assert store.completed[-1]["status"] == status
        assert store.completed[-1]["outcome"] is None  # infra, not a quality pass/fail


def test_tool_nonzero_empty_stdout_with_stderr_is_crash_without_fix_charge(
    tmp_path: Path,
) -> None:
    fake = FakeRunProcess(
        stdout=" \n",
        stderr="python: can't open file 'missing-payload' (credential=private-value)",
        exit_code=2,
    )
    store = FakeStore()
    run_state = FlowRunState(
        flow_fingerprint="fp",
        loop_counters={FlowRunState.GLOBAL_FIX_KEY: 3},
    )
    services = _services(tmp_path, fake, store, prompt_secrets=("private-value",))
    runner = ToolNodeRunner(services, _inputs(tmp_path))

    with pytest.raises(NodeManualRequired) as exc:
        runner.run(_TOOL, _ctx(_snapshot(_TOOL), _TOOL, run_state))

    message = str(exc.value)
    assert "md-check" in message
    assert "missing-payload" in message
    assert "checker crashed or malfunctioned" in message
    assert "private-value" not in message
    assert "[REDACTED]" in message
    assert run_state.fix_iterations == 3
    assert store.completed[-1]["status"] == "crashed"
    assert store.completed[-1]["outcome"] is None


def test_core_ignores_tool_git_state_side_effects(tmp_path: Path) -> None:
    # git is None and the FakeStore has NO commit/evaluation methods: the run completing proves the
    # runner has no path that applies a returned value to git/state — it only records the node_run.
    stdout = json.dumps({"outcome": "fail", "findings": [], "data": {"n": 1}})
    fake, store = FakeRunProcess(stdout=stdout, exit_code=1), FakeStore()
    result = _run(tmp_path, fake, store)
    assert result.outcome.kind == "fail"
    assert result.outcome.structured_output == {"n": 1}
    assert [c["run_id"] for c in store.completed] == [1]  # exactly one node_run completed, no more
    assert not any("GITHUB_TOKEN" in k for k in fake.calls[0]["env"])  # no git creds in the child


# -- outcome contract ---------------------------------------------------------


@pytest.mark.parametrize(("exit_code", "expected"), [(0, "pass"), (1, "fail")])
def test_tool_exit_code_gates_pass_fail(tmp_path: Path, exit_code: int, expected: str) -> None:
    # Linter style: no JSON on stdout, the exit code alone decides.
    fake, store = FakeRunProcess(stdout="some plain text", exit_code=exit_code), FakeStore()
    result = _run(tmp_path, fake, store)
    assert result.outcome.kind == expected


def test_tool_json_outcome_authoritative_and_route(tmp_path: Path) -> None:
    # A JSON outcome overrides the exit code ...
    fake, store = FakeRunProcess(stdout='{"outcome": "pass"}', exit_code=1), FakeStore()
    assert _run(tmp_path, fake, store).outcome.kind == "pass"
    # ... and route:* flows through as the edge-selecting outcome.
    fake2, store2 = FakeRunProcess(stdout='{"outcome": "route:large"}', exit_code=0), FakeStore()
    assert _run(tmp_path, fake2, store2).outcome.kind == "route:large"


def test_repeated_identical_failure_without_findings_parks_before_another_charge(
    tmp_path: Path,
) -> None:
    fake = FakeRunProcess(stdout="same linter report", exit_code=1)
    store = FakeStore()
    run_state = FlowRunState(
        flow_fingerprint="fp",
        loop_counters={FlowRunState.GLOBAL_FIX_KEY: 1},
    )
    runner = ToolNodeRunner(_services(tmp_path, fake, store), _inputs(tmp_path))
    ctx = _ctx(_snapshot(_TOOL), _TOOL, run_state)

    assert runner.run(_TOOL, ctx).outcome.kind == "fail"
    with pytest.raises(NodeManualRequired, match="repeated an identical failure without findings"):
        runner.run(_TOOL, ctx)

    assert run_state.fix_iterations == 1
    assert store.completed[-1]["status"] == "stalled"
    assert store.completed[-1]["outcome"] == "fail"


def test_changed_or_actionable_tool_failure_resets_repeated_failure_guard(tmp_path: Path) -> None:
    store = FakeStore()
    fake = FakeRunProcess(stdout="first report", exit_code=1)
    services = _services(tmp_path, fake, store)
    runner = ToolNodeRunner(services, _inputs(tmp_path))
    ctx = _ctx(_snapshot(_TOOL), _TOOL)
    assert runner.run(_TOOL, ctx).outcome.kind == "fail"

    fake.set_result(
        stdout=json.dumps(
            {
                "outcome": "fail",
                "findings": [{"severity": "error", "reason": "actionable"}],
            }
        ),
        exit_code=1,
    )
    assert runner.run(_TOOL, ctx).outcome.findings
    fake.set_result(stdout="first report", exit_code=1)
    assert runner.run(_TOOL, ctx).outcome.kind == "fail"


def test_tool_malformed_json_outcome_fail_closed(tmp_path: Path) -> None:
    fake, store = FakeRunProcess(stdout='{"outcome": "weird"}', exit_code=0), FakeStore()
    with pytest.raises(NodeManualRequired):
        _run(tmp_path, fake, store)
    assert store.completed[-1]["status"] == "invalid_output"


def test_tool_findings_and_data_recorded_not_applied(tmp_path: Path) -> None:
    stdout = json.dumps(
        {
            "outcome": "fail",
            "findings": [{"severity": "error", "reason": "too short", "paths": ["a.md"]}],
            "data": {"files_scanned": 3},
        }
    )
    result = _run(tmp_path, FakeRunProcess(stdout=stdout, exit_code=1), FakeStore())
    assert result.outcome.kind == "fail"
    assert len(result.outcome.findings) == 1
    f = result.outcome.findings[0]
    assert f.severity == "high" and f.reason == "too short" and f.paths == ("a.md",)
    assert result.outcome.structured_output == {"files_scanned": 3}


# -- compose + redaction ------------------------------------------------------


def test_tool_output_exposed_as_node_path_var(tmp_path: Path) -> None:
    tool = _TOOL
    downstream = AgentNode(id="fix", kind="agent", role_file="fix.md")
    snap = _snapshot(tool, downstream)
    fake, store = FakeRunProcess(stdout="report body", exit_code=0), FakeStore()
    calls: list[tuple[str, str, str]] = []
    services = _services(
        tmp_path, fake, store, register_artifact=lambda t, k, p: calls.append((t, k, p))
    )
    ToolNodeRunner(services, _inputs(tmp_path)).run(tool, _ctx(snap, tool))

    # {md-check_path} is a valid prompt var and resolves to the redacted stdout artifact downstream.
    # The tool ran as the first node → node_run_id 1 (per-run dir under stages/<node>/run-<id>/).
    assert "md-check_path" in node_output_vars(snap)
    stdout_artifact = (
        node_run_dir(services.artifacts_root, "task-1", "md-check", 1) / TOOL_STDOUT_FILENAME
    )
    assert stdout_artifact.is_file()
    resolved = AgentNodeRunner(services, _inputs(tmp_path))._node_output_paths(
        _ctx(snap, downstream)
    )
    assert resolved["md-check_path"] == stdout_artifact.as_posix()
    assert calls == [("task-1", "tool:md-check", str(stdout_artifact))]


def test_tool_artifacts_redacted(tmp_path: Path) -> None:
    secret = "ghp_" + "A" * 36  # a GitHub-token-shaped value the redactor scrubs structurally
    fake = FakeRunProcess(stdout=f"leaked {secret} here", stderr=f"err {secret}", exit_code=0)
    store = FakeStore()
    services = _services(tmp_path, fake, store)
    ToolNodeRunner(services, _inputs(tmp_path)).run(_TOOL, _ctx(_snapshot(_TOOL), _TOOL))

    node_dir = node_run_dir(services.artifacts_root, "task-1", "md-check", 1)
    stdout_text = (node_dir / TOOL_STDOUT_FILENAME).read_text(encoding="utf-8")
    stderr_text = (node_dir / TOOL_STDERR_FILENAME).read_text(encoding="utf-8")
    assert secret not in stdout_text and "[REDACTED]" in stdout_text
    assert secret not in stderr_text and "[REDACTED]" in stderr_text


def test_tool_timeout_resolution_precedence(tmp_path: Path) -> None:
    # node override > config default > built-in fallback (3600).
    node_override = ToolNode(id="md-check", kind="tool", tool="md-check", timeout_seconds=7200)
    snap = _snapshot(node_override)

    fake = FakeRunProcess(exit_code=0)
    services = _services(tmp_path, fake, FakeStore(), tools_default_timeout_seconds=1800)
    ToolNodeRunner(services, _inputs(tmp_path)).run(node_override, _ctx(snap, node_override))
    assert fake.calls[0]["timeout"] == 7200  # node override wins

    fake2 = FakeRunProcess(exit_code=0)
    services2 = _services(tmp_path, fake2, FakeStore(), tools_default_timeout_seconds=1800)
    ToolNodeRunner(services2, _inputs(tmp_path)).run(_TOOL, _ctx(_snapshot(_TOOL), _TOOL))
    assert fake2.calls[0]["timeout"] == 1800  # falls back to the config default

    fake3 = FakeRunProcess(exit_code=0)
    services3 = _services(tmp_path, fake3, FakeStore())  # default services default = 3600
    ToolNodeRunner(services3, _inputs(tmp_path)).run(_TOOL, _ctx(_snapshot(_TOOL), _TOOL))
    assert fake3.calls[0]["timeout"] == 3600  # built-in fallback


# -- Git-control bracket (П4.4) -----------------------------------------------


class _FakeGit:
    """Enough Git Manager for the bracket: a scripted control-state drift and tree change."""

    def __init__(
        self, *, drift: Any = None, changed_seq: list[tuple[Any, ...]] | None = None
    ) -> None:
        self._drift = drift
        self._changed_seq = changed_seq or [(), ()]
        self.captures = 0

    def capture_git_control_state(self) -> object:
        self.captures += 1
        return object()

    def compare_git_control_state(self, before: object) -> Any:
        return self._drift

    def changed_code_entries(self, task_id: str = "task-1") -> tuple[Any, ...]:
        return self._changed_seq.pop(0) if len(self._changed_seq) > 1 else self._changed_seq[0]


def test_a_tool_that_poisons_git_control_state_is_reported(tmp_path: Path) -> None:
    # A tool node runs an operator program inside the clone: a shell by definition, no write access
    # by design, and — before this — no fingerprint at all. Reported, never parked: the outcome the
    # tool returned still decides routing.
    from wastech_orchestrator.git_manager import ChangedPath, GitControlDrift, GitControlDriftItem

    drift = GitControlDrift((GitControlDriftItem("hooks", "hook 'post-commit' added"),))
    git = _FakeGit(drift=drift, changed_seq=[(), (ChangedPath(status="??", path="stray.txt"),)])
    fake = FakeRunProcess(stdout="", exit_code=0)
    services = _services(tmp_path, fake, FakeStore(), git=git)
    result = ToolNodeRunner(services, _inputs(tmp_path)).run(_TOOL, _ctx(_snapshot(_TOOL), _TOOL))
    assert result.outcome.kind == "pass"  # the tool's own verdict is untouched
    assert result.outcome.git_control_drift == "hooks: hook 'post-commit' added"
    assert result.outcome.unexpected_write is True
    assert git.captures == 1  # one bracket per run, taken before the program starts


def test_a_clean_tool_run_reports_nothing(tmp_path: Path) -> None:
    # Before-vs-after, not "is the tree dirty": a diff an earlier writing node left behind is not
    # this tool's doing.
    from wastech_orchestrator.git_manager import ChangedPath

    dirty = (ChangedPath(status="M", path="already.txt"),)
    git = _FakeGit(changed_seq=[dirty, dirty])
    fake = FakeRunProcess(stdout="", exit_code=0)
    services = _services(tmp_path, fake, FakeStore(), git=git)
    result = ToolNodeRunner(services, _inputs(tmp_path)).run(_TOOL, _ctx(_snapshot(_TOOL), _TOOL))
    assert result.outcome.git_control_drift is None
    assert result.outcome.unexpected_write is False


def test_a_tool_run_without_a_git_manager_skips_the_bracket(tmp_path: Path) -> None:
    # A harness with no clone has nothing to compare; skipping is the honest answer, not a guess.
    fake = FakeRunProcess(stdout="", exit_code=0)
    services = _services(tmp_path, fake, FakeStore())
    result = ToolNodeRunner(services, _inputs(tmp_path)).run(_TOOL, _ctx(_snapshot(_TOOL), _TOOL))
    assert result.outcome.git_control_drift is None
    assert result.outcome.unexpected_write is False

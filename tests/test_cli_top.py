"""Read-only monitor read-surface (Phase 0 of the operator console): ``scan_pending_sorted``,
``tail_lines``, and ``build_top_snapshot``. All pure / file-derived — no engine, no network."""

from __future__ import annotations

import io
import json
import random
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.state_store import StateStore, TaskRow
from wastech_orchestrator.task.model import DEFAULT_QUEUE


def _write_task(
    folder: Path, stem: str, *, priority: str | None = None, queue: str | None = None
) -> None:
    lines = [f"id: {stem}", 'title: "T"']
    if priority is not None:
        lines.append(f"priority: {priority}")
    if queue is not None:
        lines.append(f"queue: {queue}")
    front = "---\n" + "\n".join(lines) + "\n---\n"
    (folder / f"{stem}.md").write_text(f"{front}\n## Description\n\nx\n", encoding="utf-8")


# --- natural_sort_key -------------------------------------------------------------------

# The phase-numbered naming this repo's task files use, deliberately shuffled. Natural order groups
# p9 before p10, casefolds P9 next to p9, and puts p9-9 before p9-10 — the order a file manager and
# the operator expect. Bytewise ``sorted`` would put p10 second (``'1' < '9'``).
_NATURAL_NAMES = ["p10-01-x.md", "P9-08-x.md", "p9-9-x.md", "p9-07-x.md", "p9-10-01-x.md"]
_NATURAL_ORDER = ["p9-07-x.md", "P9-08-x.md", "p9-9-x.md", "p9-10-01-x.md", "p10-01-x.md"]


def test_natural_sort_key_orders_numerically() -> None:
    assert sorted(_NATURAL_NAMES, key=cli.natural_sort_key) == _NATURAL_ORDER


def test_natural_sort_key_is_platform_stable() -> None:
    # The key operates on the plain filename and casefolds explicitly, so it never leans on
    # ``Path.__lt__`` (case-sensitive on POSIX, case-folded on Windows). Reading the same names as
    # POSIX *or* Windows paths must therefore yield the identical order...
    posix = sorted(
        (PurePosixPath(n) for n in _NATURAL_NAMES), key=lambda p: cli.natural_sort_key(p.name)
    )
    windows = sorted(
        (PureWindowsPath(n) for n in _NATURAL_NAMES), key=lambda p: cli.natural_sort_key(p.name)
    )
    assert [p.name for p in posix] == _NATURAL_ORDER
    assert [p.name for p in windows] == _NATURAL_ORDER
    # ...whereas the old ``sorted(Path)`` scheduling key really does disagree across the two
    # flavours (the cross-platform bug the natural key removes).
    assert [p.name for p in sorted(PurePosixPath(n) for n in _NATURAL_NAMES)] != [
        p.name for p in sorted(PureWindowsPath(n) for n in _NATURAL_NAMES)
    ]


def test_natural_sort_key_is_strict_total_order_independent_of_input_order() -> None:
    # Independent of ``iterdir()`` yield order: any shuffle collapses to the same sequence.
    for seed in range(8):
        shuffled = _NATURAL_NAMES[:]
        random.Random(seed).shuffle(shuffled)
        assert sorted(shuffled, key=cli.natural_sort_key) == _NATURAL_ORDER
    # Distinct names never compare equal — not on case, not on leading zeros.
    assert cli.natural_sort_key("Foo.md") != cli.natural_sort_key("foo.md")
    assert cli.natural_sort_key("p9-07.md") != cli.natural_sort_key("p9-7.md")


def test_natural_sort_key_leading_zeros_are_magnitude_equal_and_adjacent() -> None:
    # 07 and 7 are the same magnitude, so they land next to each other with a deterministic
    # tie-break — neither creates a distinct numeric rank.
    assert sorted(["p9-7.md", "p9-07.md"], key=cli.natural_sort_key) == ["p9-07.md", "p9-7.md"]
    # An all-zero run is magnitude 0 (same natural tokens as "0"), only the raw name distinguishes.
    assert cli.natural_sort_key("a000.md")[0] == cli.natural_sort_key("a0.md")[0]


# --- scan_pending_sorted ----------------------------------------------------------------


def test_scan_pending_sorted_orders_by_priority_then_natural_filename(tmp_path: Path) -> None:
    folder = tmp_path / "pending"
    folder.mkdir()
    # Equal-priority ties use the natural filename key: p9-07 before p10-01 (a file manager's
    # order), not the bytewise ``'1' < '9'`` that ranks p10 first. Priority still dominates.
    _write_task(folder, "p10-01", priority="high")
    _write_task(folder, "p9-07", priority="high")
    _write_task(folder, "m-default")
    _write_task(folder, "b-low", priority="low")
    scans = cli.scan_pending_sorted(folder, DEFAULT_QUEUE)
    assert [p.stem for p, _ in scans] == ["p9-07", "p10-01", "m-default", "b-low"]


def test_scan_pending_sorted_filters_by_queue(tmp_path: Path) -> None:
    folder = tmp_path / "pending"
    folder.mkdir()
    _write_task(folder, "a", queue="default")
    _write_task(folder, "b", queue="backend")
    _write_task(folder, "c")  # untagged → default
    assert [p.stem for p, _ in cli.scan_pending_sorted(folder, "default")] == ["a", "c"]
    assert [p.stem for p, _ in cli.scan_pending_sorted(folder, "backend")] == ["b"]


def test_scan_pending_sorted_missing_folder_is_empty(tmp_path: Path) -> None:
    assert cli.scan_pending_sorted(tmp_path / "absent", DEFAULT_QUEUE) == []


# --- tail_lines -------------------------------------------------------------------------


def test_tail_lines_absent_or_nonpositive(tmp_path: Path) -> None:
    assert cli.tail_lines(None, 5) == []
    assert cli.tail_lines(tmp_path / "absent.log", 5) == []
    present = tmp_path / "x.log"
    present.write_text("a\nb\n", encoding="utf-8")
    assert cli.tail_lines(present, 0) == []


def test_tail_lines_returns_last_n(tmp_path: Path) -> None:
    path = tmp_path / "x.log"
    path.write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    assert cli.tail_lines(path, 2) == ["l3", "l4"]
    assert cli.tail_lines(path, 99) == ["l1", "l2", "l3", "l4"]


def test_tail_lines_survives_rotation(tmp_path: Path) -> None:
    path = tmp_path / "x.log"
    path.write_text("old\n", encoding="utf-8")
    assert cli.tail_lines(path, 5) == ["old"]
    path.write_text("new1\nnew2\n", encoding="utf-8")  # file replaced under us
    assert cli.tail_lines(path, 5) == ["new1", "new2"]


# --- build_top_snapshot -----------------------------------------------------------------


def _seed_store(config: OrchestratorConfig) -> StateStore:
    db = cli.worc_home_for(config) / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return StateStore.open(db)


def test_build_top_snapshot_empty_without_store(
    make_git_config: Callable[..., OrchestratorConfig], tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    snap = cli.build_top_snapshot(
        config, None, selector="default", log_path=None, log_tail_lines=5, recent_limit=5
    )
    assert snap.db_present is False
    assert snap.active == ()
    assert snap.queue == ()
    assert snap.recent == ()
    assert snap.log_lines == ()


def test_build_top_snapshot_active_parked_gate_queue_recent(
    make_git_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clone = tmp_path / "clone"
    config = make_git_config(clone)
    monkeypatch.setattr(cli, "_daemon_alive", lambda _c: True)  # live daemon → B-lite "(paused)"
    store = _seed_store(config)
    # An active RUNNING task that is parked (blocked_since) at a flow checkpoint node.
    store.insert_task(TaskRow(task_id="t-active", title="Active", status=Status.RUNNING))
    store.update_task("t-active", blocked_since="2026-06-28T00:00:00+00:00", current_node="review")
    # A terminal task → the "recent" section.
    store.insert_task(TaskRow(task_id="t-done", title="Done", status=Status.DONE))
    # A durable waiting gate for the active task.
    hitl = cli.worc_home_for(config) / "logs" / "t-active" / "hitl"
    hitl.mkdir(parents=True)
    (hitl / "turn-gate-implementation.json").write_text(
        json.dumps({"status": "waiting"}), encoding="utf-8"
    )
    # Pending queue: a high + a low in the served queue, plus a foreign-queue file to be filtered.
    pending = cli.pending_dir(config)
    pending.mkdir(parents=True)
    _write_task(pending, "p-high", priority="high")
    _write_task(pending, "p-low", priority="low")
    _write_task(pending, "p-other", queue="backend")
    log = clone / "daemon.log"
    log.write_text("line-1\nline-2\n", encoding="utf-8")

    snap = cli.build_top_snapshot(
        config, store, selector="default", log_path=log, log_tail_lines=10, recent_limit=5
    )
    store.close()

    assert snap.db_present is True
    assert len(snap.active) == 1
    active = snap.active[0]
    assert active.task_id == "t-active"
    assert active.current_node == "review"
    assert active.parked_since == "2026-06-28T00:00:00+00:00"
    assert "paused" in active.status_label
    assert active.gate_pending is True
    # Foreign queue filtered out; survivors in priority order, each labelled with its priority.
    assert [(q.label, q.priority) for q in snap.queue] == [("p-high", "high"), ("p-low", "low")]
    assert [r["task_id"] for r in snap.recent] == ["t-done"]
    assert snap.log_lines == ("line-1", "line-2")


def test_build_top_snapshot_unparked_active_has_no_gate(
    make_git_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clone = tmp_path / "clone"
    config = make_git_config(clone)
    monkeypatch.setattr(cli, "_daemon_alive", lambda _c: True)  # live daemon → plain "running"
    store = _seed_store(config)
    store.insert_task(TaskRow(task_id="t1", title="Plain", status=Status.RUNNING))
    snap = cli.build_top_snapshot(
        config, store, selector="default", log_path=None, log_tail_lines=5, recent_limit=5
    )
    store.close()
    assert len(snap.active) == 1
    assert snap.active[0].parked_since is None
    assert snap.active[0].gate_pending is False
    assert snap.active[0].status_label == "running"


def test_build_top_snapshot_running_no_daemon_is_parked(
    make_git_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A RUNNING row with no live daemon reads as "parked (no daemon)" — dominating the (paused)
    # marker, since a task cannot be actively paused by a daemon that is not running.
    clone = tmp_path / "clone"
    config = make_git_config(clone)
    monkeypatch.setattr(cli, "_daemon_alive", lambda _c: False)
    store = _seed_store(config)
    store.insert_task(TaskRow(task_id="t1", title="Plain", status=Status.RUNNING))
    store.update_task("t1", blocked_since="2026-06-28T00:00:00+00:00")  # even a B-lite park...
    snap = cli.build_top_snapshot(
        config, store, selector="default", log_path=None, log_tail_lines=5, recent_limit=5
    )
    store.close()
    assert snap.active[0].status_label == "parked (no daemon)"  # ...loses to "no daemon"


# --- render_top (pure) ------------------------------------------------------------------


def test_render_top_full_frame() -> None:
    snap = cli.TopSnapshot(
        db_present=True,
        selector="default",
        active=(
            cli._ActiveView(
                task_id="t1",
                status_label="running (paused)",
                title="Do thing",
                branch="worc/t1",
                current_node="review",
                fix_iterations=2,
                subtask="1/3",
                parked_since="2026-06-28T00:00:00+00:00",
                gate_pending=True,
            ),
        ),
        queue=(
            cli._QueueView(label="p-high", priority="high", queue="default"),
            cli._QueueView(label="p-low", priority="low", queue="default"),
        ),
        recent=({"task_id": "t0", "status": "done", "title": "Old", "branch": None},),
        log_path="/tmp/daemon.log",
        log_lines=("log line a", "log line b"),
    )
    out = cli.render_top(snap)
    assert "worc top — queue 'default'" in out
    assert "running (paused)" in out
    assert "t1" in out and "Do thing" in out
    assert "node=review" in out
    assert "subtask=1/3" in out
    assert "paused — every provider unavailable since 2026-06-28T00:00:00+00:00" in out
    assert "awaiting operator (gate pending)" in out
    assert "p-high" in out and "p-low" in out
    assert "/tmp/daemon.log" in out
    assert "log line a" in out


def test_render_top_empty_and_no_db() -> None:
    snap = cli.TopSnapshot(
        db_present=False,
        selector="default",
        active=(),
        queue=(),
        recent=(),
        log_path=None,
        log_lines=(),
    )
    out = cli.render_top(snap)
    assert "(no state database yet)" in out
    assert "(empty)" in out  # queue
    assert "(none)" in out  # recent
    assert "no --log-file" in out


# --- the q-to-quit watcher --------------------------------------------------------------


def test_stdin_quit_watcher_quits_on_q() -> None:
    event = threading.Event()
    cli._stdin_quit_watcher(event, io.StringIO("q\n"))
    assert event.is_set()


def test_stdin_quit_watcher_ignores_other_lines_then_quits() -> None:
    event = threading.Event()
    cli._stdin_quit_watcher(event, io.StringIO("hello\nquit\n"))
    assert event.is_set()


def test_stdin_quit_watcher_stops_on_eof_without_quitting() -> None:
    event = threading.Event()
    cli._stdin_quit_watcher(event, io.StringIO(""))
    assert not event.is_set()


# --- the refresh loop + cmd_top wiring --------------------------------------------------


def test_run_top_loop_renders_one_frame_and_exits(
    make_git_config: Callable[..., OrchestratorConfig], tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    event = threading.Event()
    event.set()  # pre-set: render exactly one frame, then return
    buf = io.StringIO()
    rc = cli._run_top_loop(
        config,
        selector="default",
        log_path=None,
        poll_seconds=0.01,
        recent_limit=5,
        log_tail_lines=5,
        stop_event=event,
        out=buf,
        clear=False,
    )
    assert rc == 0
    assert "worc top" in buf.getvalue()
    assert "(no state database yet)" in buf.getvalue()  # db absent → empty active section


def test_top_subparser_parses_flags() -> None:
    args = cli.build_parser().parse_args(
        ["top", "--poll-seconds", "5", "--queue", "backend", "--log-file", "d.log", "--recent", "3"]
    )
    assert args.command == "top"
    assert args.poll_seconds == 5.0
    assert args.queue == "backend"
    assert args.tail_file == "d.log"  # distinct dest, never the global --log-file write sink
    assert args.recent == 3


def test_cmd_top_returns_2_without_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli, "load_config_for", lambda _args: None)
    assert cli.cmd_top(cli.build_parser().parse_args(["top"])) == 2


def test_cmd_top_wires_args_into_loop(
    monkeypatch,  # type: ignore[no-untyped-def]
    make_git_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    config = make_git_config(tmp_path / "clone")
    monkeypatch.setattr(cli, "load_config_for", lambda _args: config)
    # Stub the blocking stdin reader so the daemon thread never touches pytest-captured stdin.
    monkeypatch.setattr(cli, "_stdin_quit_watcher", lambda *_a, **_k: None)
    captured: dict[str, object] = {}

    def fake_loop(cfg: OrchestratorConfig, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_run_top_loop", fake_loop)
    rc = cli.cmd_top(cli.build_parser().parse_args(["top", "--queue", "backend", "--recent", "3"]))
    assert rc == 0
    assert captured["selector"] == "backend"
    assert captured["recent_limit"] == 3
    assert captured["poll_seconds"] == cli._TOP_DEFAULT_POLL_SECONDS
    assert captured["log_path"] is None

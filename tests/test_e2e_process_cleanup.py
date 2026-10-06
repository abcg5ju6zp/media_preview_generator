"""E2E servers must not outlive failed startup or a hard-killed pytest worker."""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from pathlib import Path

import pytest

from tests.e2e import conftest


def _wait_until(predicate: Callable[[], bool], timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def child_processes(monkeypatch: pytest.MonkeyPatch) -> Generator[list[subprocess.Popen], None, None]:
    """Reap even deliberately broken launchers so a failed regression cannot leak."""
    children = []
    real_popen = subprocess.Popen

    def tracked_popen(*args, **kwargs) -> subprocess.Popen:
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", tracked_popen)
    monkeypatch.setattr(conftest, "app_boot_payload", lambda port: "import time; time.sleep(30)")
    yield children
    for child in children:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_startup_timeout_reaps_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_processes: list) -> None:
    monkeypatch.setattr(conftest, "wait_for_port", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="App failed to start"):
        conftest._start_app(str(tmp_path), 1234)

    assert child_processes[0].returncode is not None


def test_startup_interruption_reaps_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_processes: list
) -> None:
    def interrupted(*args, **kwargs) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(conftest, "wait_for_port", interrupted)

    with pytest.raises(KeyboardInterrupt):
        conftest._start_app(str(tmp_path), 1234)

    assert child_processes[0].returncode is not None


def test_output_larger_than_pipe_buffer_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_processes: list
) -> None:
    ready = tmp_path / "ready"
    payload = (
        "import pathlib, sys, time; "
        "print('x' * 1024 * 1024, flush=True); "
        "print('y' * 1024 * 1024, file=sys.stderr, flush=True); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    )
    monkeypatch.setattr(conftest, "app_boot_payload", lambda port: payload)
    monkeypatch.setattr(conftest, "wait_for_port", lambda *a, **k: _wait_until(ready.exists))

    child = conftest._start_app(str(tmp_path), 1234)

    assert child.poll() is None
    assert (tmp_path / "app-subprocess.log").stat().st_size > 2 * 1024 * 1024
    conftest._stop_app(child)
    assert child.returncode is not None


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM can only be ignored on POSIX")
def test_shutdown_kills_and_reaps_sigterm_resistant_child(tmp_path: Path, child_processes: list) -> None:
    ready = tmp_path / "ready"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, pathlib, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)",
        ]
    )
    assert _wait_until(ready.exists)

    conftest._stop_app(child, timeout=0.2)

    assert child.returncode == -signal.SIGKILL


def test_boot_payload_skips_linux_guard_on_other_platforms() -> None:
    prefix = conftest.app_boot_payload(1234).split("from media_preview_generator")[0]
    probe = (
        "import ctypes, sys; sys.platform = 'darwin'; "
        "ctypes.CDLL = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Linux-only call')); "
        + prefix
        + "print('portable')"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "portable"


@pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux-only")
def test_boot_payload_reports_parent_death_registration_failure() -> None:
    prefix = conftest.app_boot_payload(1234).split("from media_preview_generator")[0]
    probe = (
        "import ctypes, types; "
        f"ctypes.set_errno({errno.EPERM}); "
        "ctypes.CDLL = lambda *a, **k: types.SimpleNamespace(prctl=lambda *a: -1); " + prefix
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "PermissionError" in result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux-only")
def test_boot_payload_exits_if_parent_died_before_registration(tmp_path: Path) -> None:
    payload_file = tmp_path / "payload"
    # Generate in a different parent that exits before the payload can start.
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.e2e.conftest import app_boot_payload; from pathlib import Path; "
            f"Path({str(payload_file)!r}).write_text(app_boot_payload(1234).split('from media_preview_generator')[0])",
        ],
        check=True,
        timeout=10,
        cwd=conftest._REPO_ROOT,
    )
    result = subprocess.run(
        [sys.executable, "-c", payload_file.read_text() + "print('survived')"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == -signal.SIGKILL
    assert not result.stdout


def _process_running(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.skipif(sys.platform != "linux", reason="Linux parent-death signal and /proc proof")
def test_hard_worker_exit_stops_server_and_resource_tracker(tmp_path: Path) -> None:
    ready = tmp_path / "pids.json"
    child_payload = (
        "import json, os, time; from pathlib import Path; from multiprocessing import resource_tracker; "
        "resource_tracker.ensure_running(); "
        f"Path({str(ready)!r}).write_text(json.dumps([os.getpid(), resource_tracker._resource_tracker._pid])); "
        "time.sleep(30)"
    )
    worker_payload = (
        "import subprocess, sys, time; from tests.e2e.conftest import app_boot_payload; "
        "prefix = app_boot_payload(1234).split('from media_preview_generator')[0]; "
        f"subprocess.Popen([sys.executable, '-c', prefix + {child_payload!r}]); time.sleep(30)"
    )
    worker = subprocess.Popen([sys.executable, "-c", worker_payload], cwd=conftest._REPO_ROOT)
    pids = []
    try:
        assert _wait_until(ready.exists, timeout=10)
        pids = json.loads(ready.read_text())
        worker.kill()
        worker.wait(timeout=5)

        assert _wait_until(lambda: all(not _process_running(pid) for pid in pids))
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=5)
        if ready.exists():
            pids = json.loads(ready.read_text())
        for pid in pids:
            if _process_running(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

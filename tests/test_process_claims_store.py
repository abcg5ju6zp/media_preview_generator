"""Claim/TODO persistence and the ClaimHandle termination state machine.

No real signals and no real waits: group signalling, the clock and sleeps
are patched. These tests pin the safety properties recovery depends on —
atomic records, one-shot signals, exit-before-release, and durable
deduplicated TODOs.
"""

from __future__ import annotations

import json
import os
import signal
import stat as stat_mod
import subprocess

import pytest

from media_preview_generator.processing import process_claims as pc
from media_preview_generator.processing.process_claims import (
    TODO_CLEANUP_FAILED,
    TODO_GRACEFUL_TIMEOUT,
    TODO_IDENTITY_UNCONFIRMED,
    TODO_KILL_FAILED,
    TODO_OPEN,
    TODO_RESOLVED,
    ClaimPersistError,
    ClaimsStore,
)


@pytest.fixture
def store(tmp_path):
    return ClaimsStore(str(tmp_path / "config"))


class FakeProc:
    """Popen stand-in: exits once a signal in ``exits_on`` has been delivered."""

    def __init__(self, exits_on=frozenset()):
        self.returncode = None
        self.exits_on = set(exits_on)
        self.poll_calls = 0

    def poll(self):
        self.poll_calls += 1
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
        return self.returncode


class FakeClock:
    """Monotonic clock that advances 0.2s per call; sleeps are no-ops."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        self.t += 0.2
        return self.t


@pytest.fixture
def patched_time(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(pc, "_monotonic", clock.now)
    monkeypatch.setattr(pc, "_sleep", lambda _delay: None)
    return clock


def _make_kill(monkeypatch, proc_by_pgid):
    """Install a fake group-kill that marks FakeProc death on configured signals."""
    sent = []

    def fake_kill(pgid, sig):
        sent.append((pgid, sig))
        proc = proc_by_pgid.get(pgid)
        if proc is not None and sig in proc.exits_on:
            proc.returncode = -sig
        return True  # delivered

    monkeypatch.setattr(pc, "_kill_process_group", fake_kill)
    return sent


def _publish(store: ClaimsStore, *, pid=4242, pgid=4242, token="tok-abc", kind="preview", job_id="job-1"):
    return store.publish_claim(
        kind=kind,
        job_id=job_id,
        worker={"id": 2, "type": "GPU", "name": "GPU 2", "gpu": "NVIDIA", "gpu_device": "/dev/dri/renderD129"},
        ffmpeg_path="/usr/bin/ffmpeg",
        args=["/usr/bin/ffmpeg", "-i", "movie.mkv", "out/img-%06d.jpg"],
        pid=pid,
        pgid=pgid,
        output_folder="/tmp/frames-abc",
        stderr_log="/tmp/ffmpeg_output.log",
        ephemeral_output=True,
        token=token,
    )


class TestClaimPersistence:
    def test_publish_writes_complete_record(self, store):
        h = _publish(store)
        with open(store._active_path(h.claim_id)) as fh:
            data = json.loads(fh.read())
        assert data["version"] == 1
        assert data["schema"] == "ffmpeg-frame-extraction"
        assert data["state"] == "active"
        assert data["kind"] == "preview"
        assert data["job_id"] == "job-1"
        assert data["worker"]["name"] == "GPU 2"
        assert data["command"]["ffmpeg"] == "/usr/bin/ffmpeg"
        assert len(data["command"]["argv_sha256"]) == 64
        assert "movie.mkv" not in json.dumps(data)  # raw argv never persisted
        proc = data["process"]
        assert proc["pid"] == 4242
        assert proc["pgid"] == 4242
        assert proc["token"] == "tok-abc"
        assert data["owner"]["instance_id"] == store.instance_id
        assert len(data["owner"]["run_id"]) == 32
        assert data["paths"]["ephemeral_output"] is True
        assert data["paths"]["stderr_log"] == "/tmp/ffmpeg_output.log"
        mode = stat_mod.S_IMODE(os.stat(store._active_path(h.claim_id)).st_mode)
        assert mode == 0o600
        assert h in store.active_handles()

    def test_release_requires_exit_and_is_idempotent(self, store):
        h = _publish(store)
        proc = FakeProc()
        h.bind_process(proc)
        assert h.release_after_exit() is False  # still running → refuse
        assert os.path.exists(store._active_path(h.claim_id))

        proc.returncode = 0
        assert h.release_after_exit() is True
        assert not os.path.exists(store._active_path(h.claim_id))
        assert h.released is True
        assert h.release_after_exit() is True  # idempotent
        assert h.claim_id not in {x.claim_id for x in store.active_handles()}

    def test_persist_failure_raises_claim_persist_error(self, store, tmp_path):
        os.chmod(store.active_dir, 0o500)
        try:
            with pytest.raises(ClaimPersistError):
                _publish(store, pid=7000)
        finally:
            os.chmod(store.active_dir, 0o700)

    def test_scan_flags_corrupt_records(self, store):
        good = _publish(store, pid=111)
        with open(os.path.join(store.active_dir, "broken.json"), "w") as fh:
            fh.write("{not json")
        scanned = store.scan_active_claims()
        by_name = {os.path.basename(c.path): c for c in scanned}
        assert by_name[f"{good.claim_id}.json"].record is not None
        assert by_name["broken.json"].corrupt is True
        assert "JSONDecodeError" in by_name["broken.json"].error


class TestTerminateGroup:
    def test_exits_on_term_skips_kill(self, store, monkeypatch, patched_time):
        h = _publish(store)
        proc = FakeProc(exits_on={signal.SIGTERM})
        proc_by_pgid = {h.pgid: proc}
        sent = _make_kill(monkeypatch, proc_by_pgid)
        h.bind_process(proc)

        result = h.terminate_group(grace_s=5, kill_s=5)

        assert result.exited is True
        assert result.kill_sent is False
        assert (h.pgid, signal.SIGCONT) in sent
        assert (h.pgid, signal.SIGTERM) in sent
        assert not any(sig == signal.SIGKILL for _pgid, sig in sent)

    def test_term_timeout_then_kill(self, store, monkeypatch, patched_time):
        h = _publish(store)
        proc = FakeProc(exits_on={signal.SIGKILL})
        sent = _make_kill(monkeypatch, {h.pgid: proc})
        h.bind_process(proc)

        result = h.terminate_group(grace_s=5, kill_s=5)

        assert result.exited is True
        assert result.kill_sent is True
        assert [sig for _p, sig in sent] == [signal.SIGCONT, signal.SIGTERM, signal.SIGKILL]

    def test_never_exits_reports_unconfirmed(self, store, monkeypatch, patched_time):
        h = _publish(store)
        proc = FakeProc(exits_on=set())
        _make_kill(monkeypatch, {h.pgid: proc})
        h.bind_process(proc)

        result = h.terminate_group(grace_s=1, kill_s=1)

        assert result.exited is False
        assert result.kill_sent is True
        assert h.released is False  # caller must retain + TODO

    def test_esrch_counts_as_gone(self, store, monkeypatch, patched_time):
        h = _publish(store)
        proc = FakeProc()
        h.bind_process(proc)
        monkeypatch.setattr(pc, "_kill_process_group", lambda pgid, sig: False)  # ProcessLookupError

        result = h.terminate_group(grace_s=1, kill_s=1)

        assert result.exited is True
        assert result.kill_sent is False

    def test_terminate_is_one_shot_under_repeat_calls(self, store, monkeypatch, patched_time):
        h = _publish(store)
        proc = FakeProc(exits_on={signal.SIGKILL})
        sent = _make_kill(monkeypatch, {h.pgid: proc})
        h.bind_process(proc)

        first = h.terminate_group(grace_s=1, kill_s=1)
        second = h.terminate_group(grace_s=1, kill_s=1)

        assert first is second
        # CONT/TERM/KILL each appear exactly once despite two callers.
        sigs = [sig for _p, sig in sent]
        assert sorted(sigs) == sorted({signal.SIGCONT, signal.SIGTERM, signal.SIGKILL})


class TestTodos:
    def test_add_dedup_update_and_fields(self, store):
        rec1 = store.add_todo(
            TODO_KILL_FAILED,
            job_id="job-9",
            worker={"name": "GPU 1"},
            pid=4242,
            claim_id="claim-1",
            paths={"output_folder": "/tmp/frames-abc"},
            detail="still alive",
        )
        rec2 = store.add_todo(
            TODO_KILL_FAILED,
            job_id="job-9",
            pid=4242,
            claim_id="claim-1",
            detail="still alive again",
        )
        assert rec1["id"] == rec2["id"]
        assert rec2["occurrences"] == 2
        assert rec2["detail"] == "still alive again"
        assert rec2["status"] == TODO_OPEN
        todos = store.list_todos()
        assert len(todos) == 1
        assert todos[0]["pid"] == 4242
        assert todos[0]["paths"]["output_folder"] == "/tmp/frames-abc"
        mode = stat_mod.S_IMODE(os.stat(store._todo_path(rec1["id"])).st_mode)
        assert mode == 0o600

    def test_different_path_is_distinct_todo(self, store):
        store.add_todo(TODO_CLEANUP_FAILED, pid=None, paths={"path": "/tmp/a"}, detail="x")
        store.add_todo(TODO_CLEANUP_FAILED, pid=None, paths={"path": "/tmp/b"}, detail="y")
        assert len(store.list_todos()) == 2

    def test_resolve_and_matching(self, store):
        rec = store.add_todo(TODO_GRACEFUL_TIMEOUT, pid=10, claim_id="c-10", detail="budget")
        other = store.add_todo(TODO_IDENTITY_UNCONFIRMED, pid=20, claim_id="c-20", detail="reused")

        resolved = store.resolve_matching(lambda r: r["pid"] == 10, note="process gone")
        assert resolved == [rec["id"]]

        assert store.resolve_todo("does-not-exist") is None
        again = store.resolve_todo(rec["id"])  # idempotent
        assert again["status"] == TODO_RESOLVED
        assert again["resolution"] == "process gone"
        open_ids = {t["id"] for t in store.list_todos(TODO_OPEN)}
        assert open_ids == {other["id"]}
        all_ids = {t["id"] for t in store.list_todos(None)}
        assert all_ids == {rec["id"], other["id"]}

    def test_reopened_todo_after_resolve(self, store):
        rec = store.add_todo(TODO_KILL_FAILED, pid=30, claim_id="c-30", detail="first")
        store.resolve_todo(rec["id"], "gone")
        rec2 = store.add_todo(TODO_KILL_FAILED, pid=30, claim_id="c-30", detail="came back")
        assert rec2["id"] == rec["id"]
        assert rec2["status"] == TODO_OPEN
        assert rec2["detail"] == "came back"
        assert rec2["occurrences"] == 1  # fresh occurrence count on reopen

    def test_unknown_kind_rejected(self, store):
        with pytest.raises(ValueError):
            store.add_todo("not_a_kind")

    def test_resolve_rejects_malformed_ids(self, store):
        """Path-traversal / non-uuid ids never touch the todos directory."""
        assert store.resolve_todo("../../etc/passwd") is None
        assert store.resolve_todo("not-a-uuid") is None
        assert store.resolve_todo("") is None
        todo = store.add_todo(TODO_KILL_FAILED, pid=1, claim_id="c1")
        assert store.resolve_todo(todo["id"]) is not None  # well-formed works


class TestWaitPidExit:
    def test_waits_until_procfs_reports_gone(self, monkeypatch, patched_time):
        states = iter([True, True, True, False])
        monkeypatch.setattr(pc.procfs_info, "pid_alive", lambda pid: next(states))
        assert pc.wait_pid_exit(999, timeout_s=5) is True

    def test_times_out_when_pid_stays(self, monkeypatch, patched_time):
        monkeypatch.setattr(pc.procfs_info, "pid_alive", lambda pid: True)
        assert pc.wait_pid_exit(999, timeout_s=0.5, poll_interval_s=0.2) is False


class TestSingleton:
    def test_configure_and_get(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "env-config"))
        pc.reset_claims_for_tests()
        lazy = pc.get_claims_store()
        assert lazy.config_dir == str(tmp_path / "env-config")
        configured = pc.configure_claims(str(tmp_path / "explicit"))
        assert pc.get_claims_store() is configured
        pc.reset_claims_for_tests()

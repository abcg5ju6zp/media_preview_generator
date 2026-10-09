"""Ownership-claim lifecycle inside the FFmpeg runner and shutdown convergence.

Complements test_process_claims_store.py (state-machine unit tests) and
test_ffmpeg_active_deadline.py (diagnostic semantics): here the runner is
driven end to end with faked Popen/procfs/group-signal primitives to pin
publish-before-work, release-after-convergence, cancel idempotency and the
shutdown coordinator's four outcomes.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from media_preview_generator.processing import ffmpeg_runner, generator
from media_preview_generator.processing import process_claims as pc
from media_preview_generator.processing.process_claims import ClaimContext, ClaimPersistError


class FakeProc:
    """Popen stand-in controllable per test: poll count exit and which signals kill it."""

    def __init__(self, *, pid=4242, exit_after_polls=None, obey_terminate=True, obey_kill=True):
        self.pid = pid
        self.returncode = None
        self.polls = 0
        self._exit_after_polls = exit_after_polls
        self.obey_terminate = obey_terminate
        self.obey_kill = obey_kill
        self.direct_signals = []
        self._gone = threading.Event()

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        self.polls += 1
        if self._exit_after_polls is not None and self.polls >= self._exit_after_polls:
            self.returncode = 0
            self._gone.set()
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            if timeout is None:
                # Real Popen.wait() blocks until exit; the detached reaper relies on that.
                self._gone.wait()
                return self.returncode
            if not self._gone.wait(timeout):
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.returncode

    def send_signal(self, sig):
        self.direct_signals.append(sig)

    def kill(self):
        if self.obey_kill:
            self.returncode = -9
            self._gone.set()

    def terminate(self):
        if self.obey_terminate:
            self.returncode = -15
            self._gone.set()


@pytest.fixture
def claim_env(tmp_path, monkeypatch):
    """Isolated claim store + runner primitives (clock, group signalling, Popen)."""
    config_dir = str(tmp_path / "config")
    store = pc.configure_claims(config_dir)
    clock = SimpleNamespace(now=1000.0)
    ctx = SimpleNamespace(proc=None, popen_kwargs=None, signals=[], stderr_fh=None)

    def fake_group_kill(pgid, sig):
        ctx.signals.append(sig)
        proc = ctx.proc
        if proc is None:
            return False
        if sig == signal.SIGTERM and proc.obey_terminate:
            proc.returncode = -15
        elif sig == signal.SIGKILL and proc.obey_kill:
            proc.returncode = -9
        # SIGSTOP/SIGCONT/CONT need no state in these tests.
        return True

    monkeypatch.setattr(ffmpeg_runner, "get_process_group", lambda pid: pid)
    monkeypatch.setattr(pc, "_kill_process_group", fake_group_kill)

    config = SimpleNamespace(
        ffmpeg_path="/usr/bin/ffmpeg",
        ffmpeg_threads=1,
        thumbnail_quality=4,
        log_level="INFO",
        plex_bif_frame_interval=10,
    )

    def make_kwargs(**overrides):
        kwargs = dict(
            video_file="/media/Movie.mkv",
            output_folder=str(tmp_path / "frames"),
            gpu=None,
            gpu_device_path=None,
            config=config,
            progress_callback=None,
            ffmpeg_threads_override=1,
            cancel_check=None,
            pause_check=None,
            path_kind="sdr",
            libplacebo_vf=None,
            use_libplacebo=False,
            dv5_software_fallback=False,
            base_scale="scale=w=320:h=240:force_original_aspect_ratio=decrease",
            fps_filter="fps=fps=0.1:round=up",
            hdr10_zscale_chain="",
        )
        kwargs.update(overrides)
        return kwargs

    def popen_with_policy(args, **kwargs):
        proc = FakeProc(pid=4242, exit_after_polls=getattr(ctx, "exit_after_polls", 1))
        proc.obey_terminate = getattr(ctx, "obey_terminate", True)
        proc.obey_kill = getattr(ctx, "obey_kill", True)
        ctx.proc = proc
        ctx.popen_kwargs = kwargs
        ctx.stderr_fh = kwargs["stderr"]
        return proc

    monkeypatch.setattr(ffmpeg_runner.subprocess, "Popen", popen_with_policy)
    # Fast wall clock so the stall watchdog trips without real waits; claim waits use an independent patched clock.
    claim_clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(pc, "_monotonic", lambda: claim_clock.now)
    monkeypatch.setattr(
        pc,
        "_sleep",
        lambda seconds: setattr(claim_clock, "now", claim_clock.now + max(0.0, seconds)),
    )

    def run_once(*, runner_kwargs=None, call_kwargs=None, wall_step=100.0):
        # Runner-level time: jump a stall timeout per loop sleep when no progress arrives.
        runner_time = SimpleNamespace(
            time=lambda: clock.now,
            monotonic=lambda: clock.now,
            time_ns=lambda: 1,
            sleep=lambda s: setattr(clock, "now", clock.now + wall_step),
        )
        monkeypatch.setattr(ffmpeg_runner, "time", runner_time)
        runner = ffmpeg_runner.create_ffmpeg_runner(**make_kwargs(**(runner_kwargs or {})))
        return runner(use_skip=True, **(call_kwargs or {}))

    return SimpleNamespace(store=store, ctx=ctx, config=config, run_once=run_once, tmp=tmp_path)


def _active_records(store):
    return [c.record for c in store.scan_active_claims() if c.record is not None]


def test_publish_before_work_and_release_on_success(claim_env):
    env = claim_env
    env.ctx.exit_after_polls = 1
    rc, _sec, _speed, _lines = env.run_once()

    assert rc == 0
    kwargs = env.ctx.popen_kwargs
    assert kwargs["start_new_session"] is True
    token = kwargs["env"][pc.CLAIM_ENV_VAR]
    assert token and len(token) == 32
    assert _active_records(env.store) == []  # revoked after convergence
    assert env.store.list_todos() == []


def test_published_record_carries_full_identity(claim_env):
    env = claim_env
    published = {}
    real_publish = pc.ClaimsStore.publish_claim

    def spy(self, **kwargs):
        handle = real_publish(self, **kwargs)
        published["record"] = handle.record
        return handle

    env.ctx.exit_after_polls = 1
    with mock.patch.object(pc.ClaimsStore, "publish_claim", spy):
        env.run_once(
            runner_kwargs=dict(
                claim_context=ClaimContext(
                    job_id="job-77",
                    worker_id=3,
                    worker_type="GPU",
                    worker_name="GPU 3",
                    gpu="NVIDIA",
                    gpu_device="/dev/dri/renderD128",
                    group_id="g1",
                )
            )
        )

    rec = published["record"]
    assert rec["job_id"] == "job-77"
    assert rec["kind"] == "preview"
    assert rec["worker"]["name"] == "GPU 3"
    assert rec["process"]["pid"] == 4242 and rec["process"]["pgid"] == 4242
    assert rec["process"]["token"] == env.ctx.popen_kwargs["env"][pc.CLAIM_ENV_VAR]
    assert len(rec["command"]["argv_sha256"]) == 64
    assert rec["owner"]["instance_id"] == env.store.instance_id
    assert len(rec["owner"]["run_id"]) == 32
    assert rec["paths"]["stderr_log"].endswith(".log")
    # Raw argv must never be persisted (the input path may carry credentials).
    assert "Movie.mkv" not in str(rec)


def test_cancel_terminates_group_and_revokes(claim_env):
    env = claim_env
    env.ctx.exit_after_polls = 10_000_000
    with pytest.raises(generator.CancellationError):
        env.run_once(runner_kwargs=dict(cancel_check=lambda: env.ctx.proc.polls >= 1))

    assert env.ctx.signals == [signal.SIGCONT, signal.SIGTERM]
    assert _active_records(env.store) == []
    assert env.store.list_todos() == []


def test_cancel_unconfirmed_keeps_claim_todo_then_reaper_releases(claim_env):
    env = claim_env
    env.ctx.exit_after_polls = 10_000_000
    env.ctx.obey_terminate = False
    env.ctx.obey_kill = False
    with pytest.raises(generator.CancellationError):
        env.run_once(runner_kwargs=dict(cancel_check=lambda: env.ctx.proc.polls >= 1))

    assert env.ctx.signals == [signal.SIGCONT, signal.SIGTERM, signal.SIGKILL]
    records = _active_records(env.store)
    assert len(records) == 1  # not revoked: unconfirmed after KILL
    todos = env.store.list_todos()
    assert any(t["kind"] == pc.TODO_KILL_FAILED and t["pid"] == 4242 for t in todos)

    # When the D-state wait eventually completes, the detached reaper revokes the claim.
    env.ctx.proc.returncode = -9
    env.ctx.proc._gone.set()
    deadline = time.time() + 2
    while _active_records(env.store) and time.time() < deadline:
        time.sleep(0.02)
    assert _active_records(env.store) == []


def test_stall_watchdog_kills_group_and_revokes(claim_env):
    env = claim_env
    env.ctx.exit_after_polls = 10_000  # never exits by itself
    rc, _sec, _speed, lines = env.run_once(wall_step=10_000.0)
    assert rc == -signal.SIGKILL
    assert ffmpeg_runner.STALL_WATCHDOG_LINE in lines
    assert signal.SIGKILL in env.ctx.signals
    assert _active_records(env.store) == []


def test_gpu_cant_decode_verdict_terminates_politely_and_revokes(claim_env):
    env = claim_env
    env.ctx.exit_after_polls = 10_000

    def popen_writes_verdict(args, **kwargs):
        proc = FakeProc(exit_after_polls=10_000)
        env.ctx.proc = proc
        env.ctx.popen_kwargs = kwargs
        kwargs["stderr"].write("frame=  0\n" + ffmpeg_runner.GPU_CANT_DECODE_VERDICT + "\n")
        kwargs["stderr"].flush()
        return proc

    with mock.patch.object(ffmpeg_runner.subprocess, "Popen", popen_writes_verdict):
        rc, _sec, _speed, lines = env.run_once(
            runner_kwargs=dict(gpu="NVIDIA", gpu_device_path="cuda:0"), wall_step=0.01
        )

    assert rc == -15
    assert ffmpeg_runner.GPU_CANT_DECODE_LINE in lines
    assert signal.SIGTERM in env.ctx.signals and signal.SIGKILL not in env.ctx.signals
    assert _active_records(env.store) == []


def test_terminate_is_one_shot_when_cancel_races_exit(claim_env, monkeypatch):
    env = claim_env
    proc = FakeProc(exit_after_polls=None)

    def race_kill(pgid, sig):
        env.ctx.signals.append(sig)
        if sig == signal.SIGTERM:
            proc.returncode = -15
        return True

    monkeypatch.setattr(pc, "_kill_process_group", race_kill)
    handle = env.store.publish_claim(
        kind="preview",
        job_id="job-race",
        worker={"name": "CPU 1"},
        ffmpeg_path="/usr/bin/ffmpeg",
        args=["/usr/bin/ffmpeg", "-i", "x.mkv"],
        pid=proc.pid,
        pgid=proc.pid,
        output_folder="/tmp/x",
        stderr_log="/tmp/x.log",
        ephemeral_output=False,
        token="race-token",
    )
    handle.bind_process(proc)

    results = []
    releases = []

    def cancel():
        results.append(handle.terminate_group(grace_s=1, kill_s=1))
        releases.append(handle.release_after_exit())

    t1 = threading.Thread(target=cancel)
    t2 = threading.Thread(target=cancel)
    t1.start()
    t2.start()
    t1.join(5)
    t2.join(5)

    # Exactly one CONT/TERM round, both callers share the cached result, one revocation.
    assert results[0] is results[1]
    assert env.ctx.signals == [signal.SIGCONT, signal.SIGTERM]
    assert releases == [True, True]  # first revokes, second confirms the idempotent no-op
    assert handle.released is True
    assert _active_records(env.store) == []


def test_publish_failure_aborts_the_attempt_and_kills_child(claim_env, monkeypatch):
    env = claim_env
    os.chmod(env.store.active_dir, 0o500)
    env.ctx.exit_after_polls = 1
    try:
        with pytest.raises(ClaimPersistError):
            env.run_once()
    finally:
        os.chmod(env.store.active_dir, 0o700)
    # The just-spawned child was SIGKILLed and no active record exists.
    assert _active_records(env.store) == []


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses directory permissions")
def test_publish_failure_is_real_on_readonly_dir(claim_env):
    # Defence in depth: the chmod in the previous test actually denies writes here.
    env = claim_env
    testfile = os.path.join(env.store.active_dir, ".write-probe")
    os.chmod(env.store.active_dir, 0o500)
    try:
        with pytest.raises(OSError):
            with open(testfile, "w"):
                pass
    finally:
        os.chmod(env.store.active_dir, 0o700)


def test_shutdown_coordinator_four_outcomes(claim_env, monkeypatch):
    env = claim_env
    # Independent claim clock for this test (deterministic budget burn).
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(pc, "_monotonic", lambda: clock.now)
    monkeypatch.setattr(pc, "_sleep", lambda s: setattr(clock, "now", clock.now + max(0.0, s)))

    procs = {}

    def kill(pgid, sig):
        proc = procs[pgid]
        env.ctx.signals.append((pgid, sig))
        if sig == signal.SIGTERM and proc.obey_terminate:
            proc.returncode = -15
        elif sig == signal.SIGKILL and proc.obey_kill:
            proc.returncode = -9
        return True

    monkeypatch.setattr(pc, "_kill_process_group", kill)

    def make(pid, **policy):
        proc = FakeProc(pid=pid, **policy)
        procs[pid] = proc
        handle = env.store.publish_claim(
            kind="preview",
            job_id=f"job-{pid}",
            worker={},
            ffmpeg_path="ffmpeg",
            args=["ffmpeg", "-i", f"{pid}.mkv"],
            pid=pid,
            pgid=pid,
            output_folder=f"/t/{pid}",
            stderr_log=f"/t/{pid}.log",
            ephemeral_output=False,
            token=f"tok-{pid}",
        )
        handle.bind_process(proc)
        return handle, proc

    # 1 already gone, 2 exits on TERM, 3 needs KILL, 4 ignores everything.
    h1, p1 = make(101, obey_terminate=True)
    p1.returncode = 0
    make(102, obey_terminate=True, obey_kill=True)
    make(103, obey_terminate=False, obey_kill=True)
    make(104, obey_terminate=False, obey_kill=False)

    summary = pc.shutdown_all_claims(total_budget_s=1.0)

    assert summary["handles"] == 4
    assert summary["released"] == 3
    assert summary["kill_failed"] == 1
    assert summary["graceful_timeout"] == 0
    open_todos = env.store.list_todos()
    assert len(open_todos) == 1 and open_todos[0]["kind"] == pc.TODO_KILL_FAILED
    # The two lingerers definitely receive KILL; the already-gone one never does.
    # (A TERM-confirmed exit whose turn came late in the shared-budget race may or
    # may not also take a redundant KILL — its outcome is identical either way.)
    kpgids = {pgid for pgid, sig in env.ctx.signals if sig == signal.SIGKILL}
    assert {103, 104} <= kpgids
    assert 101 not in kpgids
    assert h1.released is True
    # Total budget was shared: wall-clock burn stayed within the budget.
    assert clock.now <= 1.25


def test_shutdown_with_no_store_is_safe(monkeypatch):
    monkeypatch.setattr(pc, "_store", None)
    assert pc.shutdown_all_claims()["handles"] == 0


def test_store_construction_failure_kills_child_and_aborts(claim_env, monkeypatch):
    """M1: a store whose directory cannot even be constructed must not leave a live unowned ffmpeg."""
    env = claim_env
    env.ctx.exit_after_polls = 1

    def boom():
        raise PermissionError("config dir not writable")

    monkeypatch.setattr(ffmpeg_runner, "get_claims_store", boom)
    with pytest.raises(ClaimPersistError):
        env.run_once()
    # The just-spawned child was killed before the runner gave up.
    assert env.ctx.proc.returncode == -9
    assert _active_records(env.store) == []


def test_two_sequential_runs_each_publish_distinct_claims_and_release(claim_env):
    """Retry cascade: every FFmpeg attempt gets its own claim; the first is gone before the second completes."""
    env = claim_env
    env.ctx.exit_after_polls = 1
    published: list[str] = []
    real_publish = pc.ClaimsStore.publish_claim

    def spy(self, **kwargs):
        h = real_publish(self, **kwargs)
        published.append(h.record["claim_id"])
        return h

    with mock.patch.object(pc.ClaimsStore, "publish_claim", spy):
        env.run_once()
        env.run_once()

    assert len(published) == 2 and published[0] != published[1]
    assert _active_records(env.store) == []
    assert env.store.list_todos() == []


def test_shutdown_converge_graceful_timeout_when_no_kill_window_left(claim_env, monkeypatch):
    """The shared budget ended after the grace wait: graceful_terminate_timeout, claim retained."""
    env = claim_env
    proc = FakeProc(exit_after_polls=None, obey_terminate=False)
    handle = env.store.publish_claim(
        kind="preview",
        job_id="late",
        worker={},
        ffmpeg_path="ffmpeg",
        args=["ffmpeg", "-i", "late.mkv"],
        pid=proc.pid,
        pgid=proc.pid,
        output_folder="/t/late",
        stderr_log="/t/late.log",
        ephemeral_output=False,
        token="late-token",
    )
    handle.bind_process(proc)
    monkeypatch.setattr(pc, "_monotonic", lambda: 10.0)
    outcome = handle.shutdown_converge(kill_at=10.0, deadline=10.0)
    assert outcome == "graceful_terminate_timeout"
    assert os.path.exists(os.path.join(env.store.active_dir, os.listdir(env.store.active_dir)[0]))
    assert any(t["kind"] == pc.TODO_GRACEFUL_TIMEOUT for t in env.store.list_todos())


def test_reconcile_resolves_process_todo_when_pid_and_claim_gone(claim_env):
    env = claim_env
    # No active claim file for it, and this host has no procfs (process unprovable) → auto-resolve.
    env.store.add_todo(pc.TODO_KILL_FAILED, pid=999_999, claim_id="claim-ghost", detail="old")
    result = pc.reconcile_cleanup_todos(env.store.config_dir)
    assert result["after"] == 0 and result["resolved"]
    assert env.store.list_todos() == []

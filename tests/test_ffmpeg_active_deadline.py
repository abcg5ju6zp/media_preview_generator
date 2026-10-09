"""Chapter attempts stay bounded even while a damaged demuxer prints errors.

Signalling goes through the per-claim process group (SIGCONT/TERM/KILL), so
the fakes model a session leader whose group receives the signals. The
diagnostic lines, timing budgets and exit codes are the contract under test.
"""

from __future__ import annotations

import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.processing import ffmpeg_runner, generator
from media_preview_generator.processing import process_claims as pc


@pytest.fixture
def managed_process(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=0.0)
    state = SimpleNamespace(
        polls=0,
        paused=False,
        signals=[],
        killed_at=None,
        finished_after=80,
        ignore_signals=False,
        stderr=None,
        output=lambda: "[matroska,webm] Invalid data found when processing input\n",
        save_log=generator._save_ffmpeg_failure_log,
    )
    process = MagicMock(pid=4242, returncode=None)

    def poll():
        if process.returncode is not None:
            return process.returncode
        state.polls += 1
        clock.now += 0.25
        state.stderr.write(state.output())
        state.stderr.flush()
        if state.polls >= state.finished_after:
            process.returncode = 0
        return process.returncode

    def wait(timeout=None):
        if process.returncode is None:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return process.returncode

    def send_signal(value):
        # Kept for the pgid-less fallback path (not taken in these tests).
        state.signals.append(value)
        state.paused = value == signal.SIGSTOP

    def fake_group_kill(pgid, sig):
        state.signals.append(sig)
        if state.ignore_signals:
            return True  # delivered, but an uninterruptible (D-state) process stays
        if sig == signal.SIGSTOP:
            state.paused = True
        elif sig == signal.SIGCONT:
            state.paused = False
        elif sig == signal.SIGTERM:
            process.returncode = -15
        elif sig == signal.SIGKILL:
            state.killed_at = clock.now
            process.returncode = -9
        return True

    def popen(_args, **kwargs):
        state.stderr = kwargs["stderr"]
        return process

    def sleep(seconds):
        clock.now += 10.0 if state.paused else seconds

    process.poll.side_effect = poll
    process.wait.side_effect = wait
    process.send_signal.side_effect = send_signal
    monkeypatch.setattr(ffmpeg_runner.subprocess, "Popen", popen)
    monkeypatch.setattr(ffmpeg_runner, "get_process_group", lambda pid: pid)
    monkeypatch.setattr(pc, "_kill_process_group", fake_group_kill)
    # Claim handle waits consume the same fake clock without real sleeps.
    monkeypatch.setattr(pc, "_monotonic", lambda: clock.now)
    monkeypatch.setattr(pc, "_sleep", lambda seconds: setattr(clock, "now", clock.now + max(0.0, seconds)))
    monkeypatch.setattr(
        ffmpeg_runner,
        "time",
        SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now, time_ns=lambda: 1, sleep=sleep),
    )
    monkeypatch.setattr(generator, "_save_ffmpeg_failure_log", lambda *_args, **_kwargs: None)
    config = SimpleNamespace(
        ffmpeg_path="ffmpeg", ffmpeg_threads=1, thumbnail_quality=4, log_level="INFO", plex_bif_frame_interval=10
    )
    kwargs = dict(
        video_file="/synthetic/damaged.mkv",
        output_folder=str(tmp_path),
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
        base_scale="scale=w=1280:h=-2",
        fps_filter="null",
        hdr10_zscale_chain="",
        chapter_start_ms=4000,
        chapter_output=str(tmp_path / "frame.jpg"),
    )
    return clock, state, process, kwargs


def test_noisy_demuxer_is_killed_and_reaped_at_total_active_deadline(managed_process):
    clock, state, process, kwargs = managed_process
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == -9
    assert ffmpeg_runner.ACTIVE_TIMEOUT_LINE in stderr
    assert ffmpeg_runner.STALL_WATCHDOG_LINE not in stderr
    assert any("Invalid data" in line for line in stderr)
    assert 2 <= state.killed_at < 3
    assert clock.now < 3
    assert state.signals.count(signal.SIGKILL) == 1


def test_long_pause_does_not_consume_active_attempt_budget(managed_process):
    clock, state, process, kwargs = managed_process
    kwargs["pause_check"] = lambda: 2 <= state.polls <= 4
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == -9 and ffmpeg_runner.ACTIVE_TIMEOUT_LINE in stderr
    assert state.signals[:2] == [signal.SIGSTOP, signal.SIGCONT]
    assert 32 <= clock.now < 34
    assert not state.paused
    assert state.signals.count(signal.SIGKILL) == 1


def test_cancellation_preempts_active_deadline_and_reaps_process(managed_process):
    _clock, state, process, kwargs = managed_process
    kwargs["cancel_check"] = lambda: state.polls >= 2
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    with pytest.raises(generator.CancellationError):
        run(use_skip=False)

    # Resume-first, one polite TERM, no KILL: the group exits on TERM.
    assert state.signals == [signal.SIGCONT, signal.SIGTERM]
    assert signal.SIGKILL not in state.signals


def test_success_before_deadline_keeps_normal_result(managed_process):
    _clock, state, process, kwargs = managed_process
    state.finished_after = 3
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == 0
    assert ffmpeg_runner.ACTIVE_TIMEOUT_LINE not in stderr
    assert signal.SIGKILL not in state.signals
    assert signal.SIGTERM not in state.signals


def test_existing_callers_without_budget_keep_their_stall_policy(managed_process):
    clock, state, process, kwargs = managed_process
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == 0 and clock.now > 20
    assert ffmpeg_runner.ACTIVE_TIMEOUT_LINE not in stderr
    assert signal.SIGKILL not in state.signals


@pytest.mark.parametrize("cancel", [False, True])
def test_uninterruptible_process_returns_explicit_failure_and_defers_reaping(managed_process, monkeypatch, cancel):
    _clock, state, process, kwargs = managed_process
    # SIGKILL is delivered but the process stays (uninterruptible mount read).
    state.ignore_signals = True
    thread = MagicMock()
    factory = MagicMock(return_value=thread)
    monkeypatch.setattr(ffmpeg_runner, "threading", SimpleNamespace(get_ident=lambda: 1, Thread=factory))
    if cancel:
        kwargs["cancel_check"] = lambda: state.polls >= 2
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    if cancel:
        with pytest.raises(generator.CancellationError):
            run(use_skip=False)
    else:
        code, _elapsed, _speed, stderr = run(use_skip=False)
        assert code == -signal.SIGKILL
        assert ffmpeg_runner.ACTIVE_TIMEOUT_LINE in stderr
        assert ffmpeg_runner.PROCESS_EXIT_PENDING_LINE in stderr

    assert process.returncode is None
    # Cancellation resumes + politely terminates first; the watchdog paths fire KILL directly.
    expected = [signal.SIGCONT, signal.SIGTERM, signal.SIGKILL] if cancel else [signal.SIGKILL]
    assert state.signals == expected
    # The claim stays active with a kill_failed TODO.
    open_todos = pc.get_claims_store().list_todos()
    assert any(t["kind"] == pc.TODO_KILL_FAILED and t["pid"] == 4242 for t in open_todos)
    factory.assert_called_once()
    assert factory.call_args.kwargs["name"] == "ffmpeg-reap-4242"
    assert factory.call_args.kwargs["daemon"] is True
    thread.start.assert_called_once_with()


def test_chapter_diagnostic_flood_terminates_with_bounded_reads_and_reaping(managed_process, monkeypatch):
    _clock, state, process, kwargs = managed_process
    state.output = lambda: "x" * (4 * 1024 * 1024 + 1)
    sizes = []
    real_open = open

    def tracked_open(path, mode="r", **options):
        handle = real_open(path, mode, **options)
        if mode != "r":
            return handle

        def read(size=-1):
            sizes.append(size)
            assert 0 < size <= 65536
            return handle.read(size)

        return SimpleNamespace(read=read, tell=handle.tell, seek=handle.seek, close=handle.close)

    monkeypatch.setattr(ffmpeg_runner, "open", tracked_open, raising=False)
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=30.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == -9 and ffmpeg_runner.DIAGNOSTIC_LIMIT_LINE in stderr
    assert sizes and len(stderr) <= 268 and max(map(len, stderr)) <= 8192
    assert state.signals.count(signal.SIGKILL) == 1


def test_chapter_diagnostic_tail_preserves_earlier_fatal_corruption(managed_process):
    _clock, state, _process, kwargs = managed_process
    fatal = "[matroska,webm] 0x00 at pos 100 invalid as first byte of an EBML number"
    state.output = lambda: (fatal + "\n" if state.polls == 1 else "") + "noise\n" * 600
    state.finished_after = 3
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=30.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == 0 and fatal in stderr
    assert len(stderr) <= 268 and max(map(len, stderr)) <= 8192
    from media_preview_generator.processing.chapters import ChapterSourceCorruptionError, _check_fatal_extraction

    with pytest.raises(ChapterSourceCorruptionError):
        _check_fatal_extraction(code, stderr, 4000)


def test_controller_stop_saves_bounded_hevc_diagnostics_before_temporary_log_is_removed(managed_process, monkeypatch):
    _clock, state, _process, kwargs = managed_process
    signature = [
        "[hevc @ 0x123] VPS 0 does not exist",
        "[hevc @ 0x123] SPS 0 does not exist.",
        "[hevc @ 0x456] PPS id out of range: 0",
        "[hevc @ 0x456] Skipping invalid undecodable NALU: 20",
    ]
    state.output = lambda: ("\n".join(signature) + "\n" if state.polls == 1 else "") + "noise\n" * 600
    save = MagicMock()
    monkeypatch.setattr(generator, "_save_ffmpeg_failure_log", save)
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == -9
    assert all(line in stderr for line in signature)
    assert ffmpeg_runner.ACTIVE_TIMEOUT_LINE in stderr
    assert len(stderr) <= 268 and max(map(len, stderr)) <= 8192
    save.assert_called_once_with(kwargs["video_file"], code, stderr)


def test_pending_process_exit_still_saves_honest_bounded_failure_log(managed_process, monkeypatch, tmp_path):
    _clock, state, process, kwargs = managed_process
    state.ignore_signals = True
    monkeypatch.setattr(ffmpeg_runner, "threading", SimpleNamespace(get_ident=lambda: 1, Thread=MagicMock()))
    monkeypatch.setattr(generator, "_save_ffmpeg_failure_log", state.save_log)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    pc.reset_claims_for_tests()
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == -signal.SIGKILL and process.returncode is None
    (saved,) = (tmp_path / "logs" / "ffmpeg_failures").glob("*.log")
    text = saved.read_text()
    assert "exit_code: None" in text and "exit_diagnosis: process_exit_pending" in text
    assert ffmpeg_runner.PROCESS_EXIT_PENDING_LINE in text
    assert ffmpeg_runner.ACTIVE_TIMEOUT_LINE in text
    assert saved.stat().st_size < 65536


def test_success_with_large_unread_diagnostics_is_not_accepted(managed_process):
    _clock, state, process, kwargs = managed_process
    state.output = lambda: "unexamined output\n" * 100000
    state.finished_after = 1
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=30.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == 0 and ffmpeg_runner.DIAGNOSTIC_LIMIT_LINE in stderr
    assert len(stderr) <= 268 and max(map(len, stderr)) <= 8192
    assert signal.SIGKILL not in state.signals
    from media_preview_generator.processing.chapters import ChapterExtractionStalledError, _check_fatal_extraction

    with pytest.raises(ChapterExtractionStalledError):
        _check_fatal_extraction(code, stderr, 4000)

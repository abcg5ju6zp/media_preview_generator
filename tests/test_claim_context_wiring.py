"""Job/worker ClaimContext wiring, ephemeral-flag forwarding, and cleanup TODOs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.jobs.worker import Worker
from media_preview_generator.processing import generator
from media_preview_generator.processing import process_claims as pc
from media_preview_generator.processing.generator import _cleanup_temp_directory
from media_preview_generator.processing.process_claims import (
    TODO_CLEANUP_FAILED,
    ClaimContext,
    current_claim_context,
)
from tests.conftest import _pi


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = pc.configure_claims(str(tmp_path / "config"))
    yield s
    pc.reset_claims_for_tests()


class TestWorkerClaimContext:
    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_context_carries_job_and_worker_and_resets_per_task(self, process, store, monkeypatch):
        resets = []
        monkeypatch.setattr(
            "media_preview_generator.jobs.worker.reset_claim_context", lambda token: resets.append(token)
        )

        seen: list[ClaimContext | None] = []

        def run(**kwargs):
            seen.append(current_claim_context())
            return MagicMock(
                status=MagicMock(value="published"),
                publishers=[],
                frame_source=MagicMock(value="extracted"),
            )

        process.side_effect = run

        worker = Worker(2, "GPU", "NVIDIA", "/dev/dri/renderD128", 2, "NVIDIA")
        worker.assign_task(_pi("mkv"), MagicMock(), MagicMock(), job_id="job-aaa")
        worker.current_thread.join(timeout=2)
        worker.check_completion()

        worker.assign_task(_pi("mkv"), MagicMock(), MagicMock(), job_id="job-bbb")
        worker.current_thread.join(timeout=2)
        worker.check_completion()

        assert [ctx.job_id for ctx in seen] == ["job-aaa", "job-bbb"]
        assert seen[0].worker_id == 2
        assert seen[0].worker_type == "GPU"
        assert seen[0].gpu_device == "/dev/dri/renderD128"
        assert seen[0].worker_name == "GPU 2"
        assert len(resets) == 2  # every task thread resets before it ends

    def test_claim_context_record_shape(self):
        ctx = Worker(7, "CPU", None, None, 0, None).claim_context()
        assert ctx.job_id is None  # set at assign time
        assert ctx.worker_id == 7 and ctx.worker_type == "CPU"
        assert ctx.worker_record()["type"] == "CPU"


class TestEphemeralForwarding:
    def test_generate_images_forwards_context_and_ephemeral_flag(self, store, tmp_path, monkeypatch):
        captured = {}

        def fake_runner(**kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop-after-capture")

        # No video tracks → the HDR-classification block is skipped entirely.
        fake_media_info = SimpleNamespace(video_tracks=[], general_tracks=[])
        monkeypatch.setattr(generator.MediaInfo, "parse", staticmethod(lambda path: fake_media_info))
        monkeypatch.setattr("media_preview_generator.processing.ffmpeg_runner.create_ffmpeg_runner", fake_runner)
        # The pre-flight keyframe probe shells out too; answer "nothing to probe".
        probe_proc = MagicMock()
        probe_proc.communicate.return_value = (b"", b"")
        probe_proc.returncode = 1
        monkeypatch.setattr(generator.subprocess, "Popen", lambda *a, **k: probe_proc)

        config = SimpleNamespace(
            ffmpeg_path="ffmpeg",
            ffmpeg_threads=1,
            thumbnail_quality=4,
            log_level="INFO",
            plex_bif_frame_interval=10,
            tonemap_algorithm="hable",
        )
        explicit = ClaimContext(job_id="job-ctx")
        with pytest.raises(RuntimeError, match="stop-after-capture"):
            generator.generate_images(
                str(tmp_path / "movie.mkv"),
                str(tmp_path / "frames"),
                None,
                None,
                config,
                None,
                ephemeral_output=True,
                claim_context=explicit,
            )

        assert captured["ephemeral_output"] is True
        assert captured["claim_context"] is explicit
        assert captured["claim_context"].job_id == "job-ctx"


class TestCleanupTodo:
    def test_cleanup_failure_records_open_todo(self, store, tmp_path, monkeypatch):
        target = tmp_path / "frames-xyz"
        target.mkdir()
        monkeypatch.setattr(generator.shutil, "rmtree", lambda path: (_ for _ in ()).throw(OSError("boom")))

        _cleanup_temp_directory(str(target), job_id="job-cleanup")

        todos = store.list_todos()
        assert len(todos) == 1
        todo = todos[0]
        assert todo["kind"] == TODO_CLEANUP_FAILED
        assert todo["job_id"] == "job-cleanup"
        assert todo["paths"]["path"] == str(target)
        assert "boom" in todo["detail"]

    def test_successful_cleanup_records_no_todo(self, store, tmp_path):
        target = tmp_path / "frames-abc"
        target.mkdir()
        _cleanup_temp_directory(str(target))
        assert store.list_todos() == []
        assert not target.exists()


class TestChapterKindAndSequentialRuns:
    def test_chapter_run_publishes_chapter_claim_with_job_context(self, store, tmp_path, monkeypatch):
        """Chapter grabs infer kind=chapter and carry the markers job id from the thread context."""
        from tests.test_ffmpeg_runner_claims import FakeProc  # reuse the controllable Popen

        def popen(args, **kwargs):
            return FakeProc(exit_after_polls=1)

        from media_preview_generator.processing import ffmpeg_runner

        monkeypatch.setattr(ffmpeg_runner.subprocess, "Popen", popen)
        monkeypatch.setattr(ffmpeg_runner, "get_process_group", lambda pid: pid)
        monkeypatch.setattr(pc, "_kill_process_group", lambda pgid, sig: True)
        monkeypatch.setattr(
            ffmpeg_runner,
            "time",
            SimpleNamespace(time=lambda: 1.0, monotonic=lambda: 1.0, time_ns=lambda: 1, sleep=lambda s: None),
        )

        published = {}
        real_publish = pc.ClaimsStore.publish_claim

        def spy(self, **kwargs):
            handle = real_publish(self, **kwargs)
            published["record"] = handle.record
            return handle

        monkeypatch.setattr(pc.ClaimsStore, "publish_claim", spy)

        config = SimpleNamespace(
            ffmpeg_path="ffmpeg",
            ffmpeg_threads=1,
            thumbnail_quality=4,
            log_level="INFO",
            plex_bif_frame_interval=10,
        )
        token = pc.set_claim_context(ClaimContext(job_id="markers-job-9"))
        try:
            runner = ffmpeg_runner.create_ffmpeg_runner(
                video_file=str(tmp_path / "movie.mkv"),
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
                base_scale="scale=w=320:h=240:force_original_aspect_ratio=decrease",
                fps_filter="null",
                hdr10_zscale_chain="",
                chapter_start_ms=4000,
                chapter_output=str(tmp_path / "frame.jpg"),
                active_timeout_s=300,
            )
            rc, _sec, _speed, _lines = runner(use_skip=False)
        finally:
            pc.reset_claim_context(token)

        assert rc == 0
        assert published["record"]["kind"] == "chapter"
        assert published["record"]["job_id"] == "markers-job-9"
        assert store.scan_active_claims() == []  # revoked after the confirmed exit

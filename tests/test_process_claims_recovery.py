"""Boot-time orphan-process recovery: default-deny identity verification and ordering.

A faked procfs tree stands in for ``/proc``; signal delivery is a fake that
"exits" target processes by removing their proc directory. These tests pin
the seven claim worlds from AC-3/AC-4 without sending a single real signal.
"""

from __future__ import annotations

import json
import os
import signal
from types import SimpleNamespace

import pytest

from media_preview_generator.processing import process_claims as pc
from media_preview_generator.processing import procfs_info
from media_preview_generator.processing.process_claims import (
    CLAIM_SCHEMA,
    TODO_IDENTITY_UNCONFIRMED,
    TODO_KILL_FAILED,
)


def _stat_for(pid: int, ticks: int, pgrp: int) -> str:
    """Build a valid 22+-field stat with the given pgrp (field 5) and starttime (field 22)."""
    fields = [
        str(pid),
        "(ffmpeg)",
        "R",  # state
        "1",  # ppid
        str(pgrp),  # pgrp
        "1",  # session
        "0",  # tty
        "-1",  # tpgid
        "0",  # flags
        "0",
        "0",
        "0",
        "0",  # min/cmin/maj/cmaj flt
        "0",
        "0",
        "0",
        "0",  # utime/stime/cutime/cstime
        "20",  # priority
        "0",  # nice
        "1",  # num_threads
        "0",  # itrealvalue
        str(ticks),  # starttime (field 22)
    ]
    head, comm_rest = fields[0], fields[1:]
    return f"{head} {' '.join(comm_rest)}"


@pytest.fixture
def world(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    store = pc.configure_claims(str(config_dir))
    monkeypatch.setattr(procfs_info, "_proc_root", str(proc_root))
    monkeypatch.setattr(procfs_info.sys, "platform", "linux")
    # procfs_available needs our own pid to exist in the fake tree.
    os.makedirs(proc_root / str(os.getpid()))
    (proc_root / str(os.getpid()) / "stat").write_text(_stat_for(os.getpid(), 1, os.getpid()))
    monkeypatch.setattr(pc, "_sleep", lambda _s: None)

    signals: list[tuple[int, int]] = []

    def make_target(*, obey_terminate=True, obey_kill=True, pid=5001):
        """Create procfs files for a target + its signal behaviour; return its (claim_args)."""

    def kill_group(pgid, sig):
        signals.append((pgid, sig))
        target = targets.get(pgid)
        if target is None:
            return False
        if sig in (signal.SIGTERM, signal.SIGCONT):
            if sig == signal.SIGTERM and target["obey_terminate"]:
                _remove(pgid)
        elif sig == signal.SIGKILL and target["obey_kill"]:
            _remove(pgid)
        return True

    def kill_pid(pid_, sig):
        signals.append((pid_, sig))
        target = targets.get(pid_)
        if target is None:
            return False
        if sig in (signal.SIGTERM, signal.SIGCONT):
            if sig == signal.SIGTERM and target["obey_terminate"]:
                _remove(pid_)
        elif sig == signal.SIGKILL and target["obey_kill"]:
            _remove(pid_)
        return True

    targets: dict[int, dict] = {}

    def _remove(id_):
        import shutil

        shutil.rmtree(proc_root / str(id_), ignore_errors=True)

    monkeypatch.setattr(pc, "_kill_process_group", kill_group)
    monkeypatch.setattr(os, "kill", kill_pid)

    def add_target(
        pid,
        *,
        ticks=500,
        token="secret-token",
        argv=("ffmpeg", "-i", "a.mkv"),
        alive=True,
        record_ticks=None,
        record_token=None,
        record_argv=None,
        environ_token=None,
        cmdline_argv=None,
        obey_terminate=True,
        obey_kill=True,
        environ_readable=True,
    ):
        targets[pid] = {"obey_terminate": obey_terminate, "obey_kill": obey_kill}
        if alive:
            pdir = proc_root / str(pid)
            pdir.mkdir()
            (pdir / "stat").write_text(_stat_for(pid, ticks, pid))
            env_argv = cmdline_argv if cmdline_argv is not None else argv
            (pdir / "cmdline").write_bytes(b"\x00".join(os.fsencode(a) for a in env_argv) + b"\x00")
            if environ_readable:
                env_token = environ_token if environ_token is not None else token
                (pdir / "environ").write_bytes(f"{pc.CLAIM_ENV_VAR}={env_token}\x00OTHER=x\x00".encode())
        args = record_argv if record_argv is not None else argv
        return {
            "pid": pid,
            "pgid": pid,
            "start_ticks": ticks if record_ticks is None else record_ticks,
            "token": token if record_token is None else record_token,
            "argv": args,
        }

    def write_claim(
        target,
        *,
        instance_id=None,
        owner_pid=9000,
        owner_ticks=100,
        owner_alive=False,
        ephemeral=True,
        job_id="job-1",
        kind="preview",
        cid=None,
        claim_state="active",
        extra_paths=None,
    ):
        cid = cid or f"claim-{target['pid']}"
        if owner_alive:
            odir = proc_root / str(owner_pid)
            odir.mkdir(exist_ok=True)
            (odir / "stat").write_text(_stat_for(owner_pid, owner_ticks, owner_pid))
        ephemeral_dir = tmp_path / f"frames-{target['pid']}"
        stderr_log = tmp_path / f"ffmpeg_{target['pid']}.log"
        ephemeral_dir.mkdir(exist_ok=True)
        (ephemeral_dir / "img-001.jpg").write_bytes(b"x")
        stderr_log.write_text("stderr")
        record = {
            "version": 1,
            "schema": CLAIM_SCHEMA,
            "state": claim_state,
            "claim_id": cid,
            "created_at": "2026-01-01T00:00:00+00:00",
            "kind": kind,
            "job_id": job_id,
            "worker": {"id": 1, "type": "GPU", "name": "GPU 1"},
            "command": {"ffmpeg": "ffmpeg", "argv_sha256": procfs_info.argv_hash(target["argv"])},
            "process": {
                "pid": target["pid"],
                "pgid": target["pgid"],
                "start_ticks": target["start_ticks"],
                "token": target["token"],
            },
            "owner": {
                "instance_id": instance_id or store.instance_id,
                "run_id": "previous-boot-run",
                "pid": owner_pid,
                "start_ticks": owner_ticks,
            },
            "paths": {
                "output_folder": str(ephemeral_dir),
                "stderr_log": str(stderr_log),
                "ephemeral_output": ephemeral,
            },
        }
        if extra_paths:
            record["paths"].update(extra_paths)
        with open(os.path.join(store.active_dir, f"{cid}.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        return record, str(ephemeral_dir), str(stderr_log)

    def recover(**kw):
        return pc.recover_orphaned_processes(str(config_dir), grace_s=0.2, kill_s=0.2, **kw)

    yield SimpleNamespace(
        store=store,
        add_target=add_target,
        write_claim=write_claim,
        recover=recover,
        signals=signals,
        config_dir=str(config_dir),
        tmp=tmp_path,
    )
    pc.reset_claims_for_tests()


class TestRecoveryWorlds:
    def test_verified_orphan_is_signalled_and_cleaned_and_revoked(self, world):
        target = world.add_target(5001, obey_terminate=True)
        _record, frame_dir, stderr_log = world.write_claim(target)
        # An unresolved kill_failed TODO from an earlier boot should auto-resolve.
        world.store.add_todo(TODO_KILL_FAILED, pid=5001, claim_id="claim-5001", detail="old")

        summary = world.recover()

        assert [sig for _pgid, sig in world.signals] == [signal.SIGCONT, signal.SIGTERM]
        assert summary["killed"] == 1
        assert not os.path.exists(os.path.join(world.store.active_dir, "claim-5001.json"))
        assert not os.path.exists(frame_dir) and not os.path.exists(stderr_log)
        assert not world.store.list_todos()

    def test_orphan_ignoring_term_gets_kill_then_reclaimed(self, world):
        target = world.add_target(5002, obey_terminate=False, obey_kill=True)
        world.write_claim(target, cid="claim-5002")
        summary = world.recover()
        assert [sig for _pgid, sig in world.signals] == [signal.SIGCONT, signal.SIGTERM, signal.SIGKILL]
        assert summary["killed"] == 1

    def test_target_already_exited_is_cleaned_without_signals(self, world):
        target = world.add_target(5003, alive=False)
        _record, frame_dir, _stderr = world.write_claim(target, cid="claim-5003")
        summary = world.recover()
        assert world.signals == []
        assert summary["exited_already"] == 1
        assert not os.path.exists(frame_dir)
        assert not os.path.exists(os.path.join(world.store.active_dir, "claim-5003.json"))

    def test_pid_reused_is_never_signalled_and_stale_claim_revoked(self, world):
        # Record says ticks=500 but the live pid at 5004 started at ticks=9999.
        target = world.add_target(5004, ticks=9999, record_ticks=500, token="x", record_token="x")
        world.write_claim(target, cid="claim-5004")
        summary = world.recover()
        assert world.signals == []
        assert summary["unconfirmed"] == 1
        assert not os.path.exists(os.path.join(world.store.active_dir, "claim-5004.json"))
        todos = world.store.list_todos()
        assert todos and todos[0]["kind"] == TODO_IDENTITY_UNCONFIRMED and todos[0]["pid"] == 5004

    def test_token_mismatch_is_never_signalled_and_claim_kept(self, world):
        target = world.add_target(5005, token="live-token", record_token="record-token")
        world.write_claim(target, cid="claim-5005")
        summary = world.recover()
        assert world.signals == []
        assert summary["unconfirmed"] == 1
        assert os.path.exists(os.path.join(world.store.active_dir, "claim-5005.json"))  # kept
        assert world.store.list_todos()[0]["kind"] == TODO_IDENTITY_UNCONFIRMED

    def test_cmdline_mismatch_is_never_signalled_and_claim_kept(self, world):
        target = world.add_target(5006, argv=("ffmpeg", "-i", "a.mkv"), record_argv=("ffmpeg", "-i", "OTHER.mkv"))
        world.write_claim(target, cid="claim-5006")
        summary = world.recover()
        assert world.signals == []
        assert summary["unconfirmed"] == 1
        assert os.path.exists(os.path.join(world.store.active_dir, "claim-5006.json"))

    def test_live_owner_is_never_touched(self, world):
        target = world.add_target(5007)
        world.write_claim(target, cid="claim-5007", owner_pid=9007, owner_ticks=333, owner_alive=True)
        summary = world.recover()
        assert world.signals == []
        assert summary["skipped_owner_alive"] == 1
        assert os.path.exists(os.path.join(world.store.active_dir, "claim-5007.json"))
        assert world.store.list_todos() == []

    def test_foreign_instance_claim_is_never_touched(self, world):
        target = world.add_target(5008)
        world.write_claim(target, cid="claim-5008", instance_id="some-other-installation-id")
        summary = world.recover()
        assert world.signals == []
        assert summary["skipped_foreign"] == 1
        assert os.path.exists(os.path.join(world.store.active_dir, "claim-5008.json"))
        assert world.store.list_todos() == []

    def test_corrupt_claim_records_identity_todo_and_continues(self, world):
        with open(os.path.join(world.store.active_dir, "broken.json"), "w") as fh:
            fh.write("{not json")
        target = world.add_target(5009)
        world.write_claim(target, cid="claim-5009")
        summary = world.recover()
        assert summary["corrupt"] == 1 and summary["killed"] == 1
        todos = world.store.list_todos()
        kinds = {t["kind"] for t in todos}
        assert TODO_IDENTITY_UNCONFIRMED in kinds

    def test_surviving_sigkill_keeps_claim_and_kill_failed_todo(self, world):
        target = world.add_target(5010, obey_terminate=False, obey_kill=False)
        world.write_claim(target, cid="claim-5010")
        summary = world.recover()
        assert [sig for _pgid, sig in world.signals][-1] == signal.SIGKILL
        assert summary["unconfirmed"] == 1
        assert os.path.exists(os.path.join(world.store.active_dir, "claim-5010.json"))
        assert any(t["kind"] == TODO_KILL_FAILED for t in world.store.list_todos())

    def test_non_ephemeral_dir_is_not_deleted(self, world, tmp_path):
        target = world.add_target(5011, alive=False)
        _record, frame_dir, _stderr = world.write_claim(target, cid="claim-5011", ephemeral=False)
        summary = world.recover()
        assert summary["exited_already"] == 1
        assert os.path.isdir(frame_dir)  # cache-style slot preserved

    def test_cleanup_failure_still_revokes_and_records_todo(self, world, monkeypatch):
        import media_preview_generator.processing.process_claims as pcmod

        target = world.add_target(5012, alive=False)
        _record, frame_dir, _stderr = world.write_claim(target, cid="claim-5012")

        def boom(_path, *a, **k):
            raise OSError("cannot delete")

        monkeypatch.setattr(pcmod.shutil, "rmtree", boom)
        summary = world.recover()

        assert summary["exited_already"] == 1 and summary["cleanup_failed"] == 1
        assert not os.path.exists(os.path.join(world.store.active_dir, "claim-5012.json"))
        todos = world.store.list_todos()
        assert len(todos) == 1 and todos[0]["kind"] == "cleanup_failed"
        assert todos[0]["paths"]["path"] == frame_dir


class TestRecoveryPlatform:
    def test_procfs_unavailable_is_read_only(self, world, monkeypatch):
        target = world.add_target(5012)
        world.write_claim(target, cid="claim-5012")
        monkeypatch.setattr(procfs_info.sys, "platform", "darwin")
        summary = world.recover()
        assert summary["procfs_available"] is False
        assert world.signals == []
        assert summary["killed"] == 0 and summary["unconfirmed"] == 0
        assert os.path.exists(os.path.join(world.store.active_dir, "claim-5012.json"))

    def test_unreadable_environment_is_never_signalled_and_claim_kept(self, world):
        target = world.add_target(5013, environ_readable=False)
        world.write_claim(target, cid="claim-5013")
        summary = world.recover()
        assert world.signals == []
        assert summary["unconfirmed"] == 1
        assert os.path.exists(os.path.join(world.store.active_dir, "claim-5013.json"))

    def test_signalling_denied_is_not_treated_as_gone(self, world, monkeypatch):
        """EPERM on the group must not be read as ESRCH: process stays, claim stays, TODO opens."""
        target = world.add_target(5014)
        world.write_claim(target, cid="claim-5014")

        def deny(pgid, sig):
            world.signals.append((pgid, sig))
            return None  # EPERM third state

        monkeypatch.setattr(pc, "_kill_process_group", deny)
        summary = world.recover()
        assert [sig for _p, sig in world.signals] == [signal.SIGCONT, signal.SIGTERM]
        assert summary["killed"] == 0 and summary["unconfirmed"] == 1
        assert os.path.exists(os.path.join(world.store.active_dir, "claim-5014.json"))
        assert any(t["pid"] == 5014 for t in world.store.list_todos())


class TestRecoveryBootOrder:
    def test_recovery_runs_before_job_requeue(self, tmp_path, monkeypatch):
        # Stop the scheduler created by create_app so it doesn't outlive the test.
        import media_preview_generator.web.scheduler as sched_mod

        calls: list[str] = []
        monkeypatch.setattr(
            "media_preview_generator.processing.process_claims.recover_orphaned_processes",
            lambda *a, **k: calls.append("recover"),
        )

        import media_preview_generator.web.app as app_mod

        monkeypatch.setattr(app_mod, "_requeue_interrupted_on_startup", lambda config_dir: calls.append("requeue"))
        monkeypatch.setattr(app_mod, "_resume_interrupted_retry_chains_on_startup", lambda config_dir: None)
        monkeypatch.setattr(app_mod, "log_token_on_startup", lambda: None)
        monkeypatch.setattr(app_mod, "_prewarm_caches", lambda: None)
        monkeypatch.setattr(app_mod, "_warn_unhealthy_media_mounts", lambda servers: [])

        from media_preview_generator.web.app import create_app

        try:
            create_app(config_dir=str(tmp_path / "config"))
        finally:
            with sched_mod._schedule_lock:
                if sched_mod._schedule_manager is not None:
                    sched_mod._schedule_manager.stop()
                    sched_mod._schedule_manager = None

        assert calls[:2] == ["recover", "requeue"]

    def test_job_requeue_still_runs_when_recovery_raises(self, tmp_path, monkeypatch):
        import media_preview_generator.web.scheduler as sched_mod

        calls: list[str] = []

        def _boom(*a, **k):
            calls.append("recover")
            raise RuntimeError("recovery boom")

        monkeypatch.setattr(
            "media_preview_generator.processing.process_claims.recover_orphaned_processes",
            _boom,
        )

        import media_preview_generator.web.app as app_mod

        monkeypatch.setattr(app_mod, "_requeue_interrupted_on_startup", lambda config_dir: calls.append("requeue"))
        monkeypatch.setattr(app_mod, "_resume_interrupted_retry_chains_on_startup", lambda config_dir: None)
        monkeypatch.setattr(app_mod, "log_token_on_startup", lambda: None)
        monkeypatch.setattr(app_mod, "_prewarm_caches", lambda: None)
        monkeypatch.setattr(app_mod, "_warn_unhealthy_media_mounts", lambda servers: [])

        from media_preview_generator.web.app import create_app

        try:
            create_app(config_dir=str(tmp_path / "config"))
        finally:
            with sched_mod._schedule_lock:
                if sched_mod._schedule_manager is not None:
                    sched_mod._schedule_manager.stop()
                    sched_mod._schedule_manager = None

        assert calls[:2] == ["recover", "requeue"]

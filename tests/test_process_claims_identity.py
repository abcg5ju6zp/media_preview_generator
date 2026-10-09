"""procfs identity reads and installation/run identity for process claims.

The /proc reader is the safety boundary for orphan reaping: a wrong parse
or an over-eager exception would either block recovery or, worse, make a
reused PID look verifiable. These tests pin the parsing (comm with spaces
and parens), the fail-safe returns, the cmdline hash equivalence, and the
stable instance-id bootstrap.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mod

import pytest

from media_preview_generator.processing import procfs_info
from media_preview_generator.processing.process_claims import get_run_id, load_or_create_instance_id


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    """Point the procfs reader at a faked /proc tree and pretend to be Linux."""
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(procfs_info, "_proc_root", str(root))
    monkeypatch.setattr(procfs_info.sys, "platform", "linux")
    return root


def _write_proc(root, pid, *, stat_text=None, cmdline=None, environ=None):
    pdir = root / str(pid)
    pdir.mkdir(parents=True, exist_ok=True)
    if stat_text is not None:
        (pdir / "stat").write_text(stat_text, encoding="utf-8")
    if cmdline is not None:
        (pdir / "cmdline").write_bytes(cmdline)
    if environ is not None:
        (pdir / "environ").write_bytes(environ)
    return pdir


# A realistic but adversarial stat: comm contains spaces and parentheses.
# starttime (field 22) = 123456 ticks, pgrp (field 5) = 777.
_STAT_BAD_COMM = (
    "4242 (weird (name) x) R 100 777 100 0 -1 4194304 0 0 0 0 0 0 0 0 20 0 1 0 123456 "
    "0 0 18446744073709551615 9470 9485 0 0 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0"
)


class TestProcfsParsing:
    def test_starttime_pgrp_with_hostile_comm(self, fake_proc):
        _write_proc(fake_proc, 4242, stat_text=_STAT_BAD_COMM)
        assert procfs_info.read_start_ticks(4242) == 123456
        assert procfs_info.read_pgrp(4242) == 777
        assert procfs_info.parse_stat_starttime(_STAT_BAD_COMM) == 123456
        assert procfs_info.parse_stat_pgrp(_STAT_BAD_COMM) == 777

    def test_cmdline_and_argv_hash_equivalence(self, fake_proc):
        args = ["/usr/lib/jellyfin-ffmpeg/ffmpeg", "-i", "movie (1).mkv", "out/img-%06d.jpg"]
        # Kernel stores argv NUL-separated with a trailing NUL.
        cmdline = b"\x00".join(os.fsencode(a) for a in args) + b"\x00"
        _write_proc(fake_proc, 4242, stat_text=_STAT_BAD_COMM, cmdline=cmdline)

        live = procfs_info.read_cmdline_bytes(4242)
        assert live == b"\x00".join(os.fsencode(a) for a in args)
        assert hashlib.sha256(live).hexdigest() == procfs_info.argv_hash(args)
        assert procfs_info.cmdline_matches_hash(4242, procfs_info.argv_hash(args)) is True
        assert procfs_info.cmdline_matches_hash(4242, "deadbeef") is False

    def test_environ_parsing(self, fake_proc):
        raw = b"MPG_PROCESS_CLAIM=tok-123\x00FOO=bar=baz\x00\x00NOEQUALSSKIP"
        _write_proc(fake_proc, 4242, stat_text=_STAT_BAD_COMM, environ=raw)
        env = procfs_info.read_environ(4242)
        # The empty chunk and the NOEQUALSSKIP entry (no "=") must be dropped.
        assert env == {"MPG_PROCESS_CLAIM": "tok-123", "FOO": "bar=baz"}

    def test_self_identity_uses_own_pid(self, fake_proc):
        pid = os.getpid()
        # Fields 6..14 (flags, min/cmin/maj/cmaj-flt, utime/stime/cutime/cstime):
        # nine tokens between tpgid(-1) and priority(20).
        stat_text = f"{pid} (python3) R 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 9999 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        _write_proc(fake_proc, pid, stat_text=stat_text)
        own_pid, ticks = procfs_info.self_identity()
        assert own_pid == pid
        assert ticks == 9999


class TestProcfsFailSafe:
    def test_missing_process_returns_none_or_false(self, fake_proc):
        assert procfs_info.read_start_ticks(999999) is None
        assert procfs_info.read_pgrp(999999) is None
        assert procfs_info.read_cmdline_bytes(999999) is None
        assert procfs_info.read_environ(999999) is None
        assert procfs_info.pid_alive(999999) is False

    @pytest.mark.parametrize(
        "stat_text",
        [
            "",
            "4242 (f) R",  # too few fields
            "4242 (f) R 1 1 1 0 -1 0 0 0 0 0 0 0 0 20 0 1 0 notanumber",
            "garbage",
        ],
    )
    def test_malformed_stat_returns_none(self, fake_proc, stat_text):
        _write_proc(fake_proc, 4242, stat_text=stat_text)
        assert procfs_info.read_start_ticks(4242) is None

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses file permissions")
    def test_unreadable_stat_returns_none(self, fake_proc):
        pdir = _write_proc(fake_proc, 4242, stat_text=_STAT_BAD_COMM)
        os.chmod(pdir / "stat", 0)
        try:
            assert procfs_info.read_start_ticks(4242) is None
        finally:
            os.chmod(pdir / "stat", stat_mod.S_IRUSR | stat_mod.S_IWUSR)

    def test_procfs_available_true_with_fake_tree(self, fake_proc):
        _write_proc(fake_proc, os.getpid(), stat_text=_STAT_BAD_COMM)
        assert procfs_info.procfs_available() is True

    def test_procfs_available_false_when_root_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(procfs_info, "_proc_root", str(tmp_path / "nope"))
        monkeypatch.setattr(procfs_info.sys, "platform", "linux")
        assert procfs_info.procfs_available() is False

    def test_procfs_available_false_off_linux(self, fake_proc, monkeypatch):
        # Real /proc on the dev machine may be absent; even with a fake root
        # present, non-Linux must report unavailable.
        _write_proc(fake_proc, os.getpid(), stat_text=_STAT_BAD_COMM)
        monkeypatch.setattr(procfs_info.sys, "platform", "darwin")
        assert procfs_info.procfs_available() is False
        monkeypatch.setattr(procfs_info.sys, "platform", "win32")
        assert procfs_info.procfs_available() is False

    def test_off_linux_reads_still_fail_safe_without_raising(self, tmp_path, monkeypatch):
        # With no per-process tree at all every read returns None, no exception.
        monkeypatch.setattr(procfs_info, "_proc_root", str(tmp_path / "proc-missing"))
        for fn in (procfs_info.read_start_ticks, procfs_info.read_pgrp, procfs_info.read_cmdline_bytes):
            assert fn(1234) is None
        assert procfs_info.read_environ(1234) is None
        assert procfs_info.pid_alive(1234) is False


class TestInstanceIdentity:
    def test_instance_id_created_stable_and_private(self, tmp_path, monkeypatch):
        monkeypatch.setattr("media_preview_generator.processing.process_claims._instance_id_cache", None)
        config_dir = tmp_path / "config"
        first = load_or_create_instance_id(str(config_dir))
        path = config_dir / "instance_id"
        assert path.exists()
        assert len(first) == 32
        int(first, 16)  # hex
        mode = stat_mod.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

        # A fresh cache (simulating another boot against the same volume) reuses it.
        monkeypatch.setattr("media_preview_generator.processing.process_claims._instance_id_cache", None)
        second = load_or_create_instance_id(str(config_dir))
        assert second == first

    def test_distinct_dirs_get_distinct_ids(self, tmp_path, monkeypatch):
        monkeypatch.setattr("media_preview_generator.processing.process_claims._instance_id_cache", None)
        a = load_or_create_instance_id(str(tmp_path / "a"))
        monkeypatch.setattr("media_preview_generator.processing.process_claims._instance_id_cache", None)
        b = load_or_create_instance_id(str(tmp_path / "b"))
        assert a != b

    def test_run_id_is_stable_hex_within_process(self):
        rid = get_run_id()
        assert len(rid) == 32
        int(rid, 16)
        assert get_run_id() == rid

"""Linux procfs identity reads for external-process ownership.

Every fact the orphan-recovery logic needs about a *live* process comes
from here and nowhere else:

* ``starttime`` — field 22 of ``/proc/<pid>/stat`` in clock ticks. It is
  the OS process-start identity: a reused PID exposes a different value,
  so comparing it is what makes signalling across a restart safe.
* ``cmdline`` — raw NUL-separated bytes, hashed with the same rule used
  at spawn time to verify command identity.
* ``environ`` — parsed ``/proc/<pid>/environ``, used to verify the
  per-process claim token injected at Popen.

The module is the ONLY place that touches ``/proc``. All functions fail
safe: when procfs is absent (macOS/Windows) or unreadable, they return
``None``/``False`` and never raise, so callers can treat "I don't know"
as "do not signal". ``set_proc_root`` lets tests point the reader at a
faked procfs tree without monkey-patching built-ins.
"""

from __future__ import annotations

import hashlib
import os
import sys

# Field 22 (1-indexed) of /proc/<pid>/stat is starttime, measured in
# clock ticks since boot. After stripping "pid (comm)" — whose comm may
# itself contain spaces and parentheses — state (field 3) is the first
# remainder token, so starttime sits at remainder index 22 - 3 = 19.
_STARTTIME_INDEX = 19
# pgrp is field 5 → remainder index 2 (kept for tests/verification; the
# claim records pgid via os.getpgid at spawn).
_PGRP_INDEX = 2

_proc_root = "/proc"


def set_proc_root(root: str) -> None:
    """Redirect procfs reads (tests only; production keeps ``/proc``)."""
    global _proc_root
    _proc_root = root or "/proc"


def proc_root() -> str:
    """The directory procfs reads are rooted at."""
    return _proc_root


def procfs_available() -> bool:
    """Whether a readable procfs with per-process entries exists here.

    False on non-Linux platforms and when the configured root is not a
    directory — callers must then skip every signal-based recovery step.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        return os.path.isdir(os.path.join(_proc_root, str(os.getpid())))
    except OSError:
        return False


def _stat_path(pid: int) -> str:
    return os.path.join(_proc_root, str(int(pid)), "stat")


def parse_stat_starttime(stat_text: str) -> int | None:
    """Extract the starttime field from ``/proc/<pid>/stat`` contents.

    The comm field (field 2, wrapped in parentheses) may contain spaces
    and even ``)`` characters, so split at the LAST ``)`` rather than
    tokenising the whole string. Returns None for malformed input.
    """
    try:
        _pid_part, _, remainder = stat_text.rpartition(")")
        fields = remainder.split()
        value = fields[_STARTTIME_INDEX]
        starttime = int(value)
    except (ValueError, IndexError, AttributeError):
        return None
    return starttime if starttime >= 0 else None


def parse_stat_pgrp(stat_text: str) -> int | None:
    """Extract the process-group field (field 5) from stat contents."""
    try:
        _pid_part, _, remainder = stat_text.rpartition(")")
        fields = remainder.split()
        return int(fields[_PGRP_INDEX])
    except (ValueError, IndexError, AttributeError):
        return None


def read_stat_text(pid: int) -> str | None:
    """Read ``/proc/<pid>/stat`` text, or None when the process is gone/unreadable."""
    try:
        with open(_stat_path(pid), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None


def read_start_ticks(pid: int) -> int | None:
    """The process starttime in clock ticks; None when unavailable or malformed."""
    text = read_stat_text(pid)
    if text is None:
        return None
    return parse_stat_starttime(text)


def read_pgrp(pid: int) -> int | None:
    """The process group id recorded in stat."""
    text = read_stat_text(pid)
    if text is None:
        return None
    return parse_stat_pgrp(text)


def read_cmdline_bytes(pid: int) -> bytes | None:
    """Raw argv bytes as the kernel stores them (NUL-joined), trailing NULs stripped.

    None means the process is gone or its cmdline is unreadable — an
    unverifiable identity, never a match.
    """
    path = os.path.join(_proc_root, str(int(pid)), "cmdline")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    return data.rstrip(b"\x00")


def read_environ(pid: int) -> dict[str, str] | None:
    """Parse ``/proc/<pid>/environ`` into a dict.

    Returns None when the process is gone or environ is unreadable
    (permissions). An empty dict can legitimately come back for a
    process that was started with a scrubbed environment.
    """
    path = os.path.join(_proc_root, str(int(pid)), "environ")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    env: dict[str, str] = {}
    for chunk in data.split(b"\x00"):
        if not chunk or b"=" not in chunk:
            continue
        key, _, value = chunk.partition(b"=")
        env[key.decode("utf-8", "surrogateescape")] = value.decode("utf-8", "surrogateescape")
    return env


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` currently exists (a signal of 0 would also work, but stat keeps reads in one place)."""
    return os.path.exists(_stat_path(pid))


def argv_hash(args: list[str] | tuple[str, ...]) -> str:
    """SHA-256 of argv joined with NUL — the exact byte layout of ``/proc/<pid>/cmdline``.

    Hashing bytes (via ``os.fsencode`` with surrogateescape) keeps the
    value comparable with :func:`read_cmdline_bytes` on odd file names.
    Only the hash is ever persisted — never the raw argv, which may
    contain credential-bearing input paths.
    """
    joined = b"\x00".join(os.fsencode(str(arg)) for arg in args)
    return hashlib.sha256(joined).hexdigest()


def cmdline_matches_hash(pid: int, expected_hash: str) -> bool:
    """Whether the live process cmdline hashes to ``expected_hash``.

    A missing/unreadable cmdline is a mismatch, never a match.
    """
    cmdline = read_cmdline_bytes(pid)
    if cmdline is None:
        return False
    return hashlib.sha256(cmdline).hexdigest() == (expected_hash or "")


def self_identity() -> tuple[int, int | None]:
    """Return ``(own pid, own start ticks)`` for ownership records."""
    pid = os.getpid()
    return pid, read_start_ticks(pid)

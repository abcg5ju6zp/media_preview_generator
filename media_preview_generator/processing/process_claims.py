"""Persistent ownership of external FFmpeg frame-extraction processes.

A frame-extraction FFmpeg is an *external* process: when the web process
is restarted by its supervisor (s6 re-running the gunicorn service) or
exits after the worker-join timeout, the child is reparented to PID 1 and
keeps consuming GPU/CPU and the per-file temp directory. The new process
used to know nothing about it — jobs were merely marked interrupted and
the UI could only tell an admin to clean up by hand.

This module publishes a verifiable ownership record (a "claim") for every
extraction FFmpeg, revokes it once the process has truly exited and the
output has converged, records observable cleanup TODOs when termination
or cleanup cannot be confirmed, and reconciles orphaned processes at the
next boot — signalling a PID only when four independent facts prove the
live process is the exact child this instance owned.

Identity hierarchy:

* ``instance_id`` — a stable random UUID for this installation, stored
  in ``config_dir/instance_id``. Records carrying a different id belong
  to another instance sharing the volume and are never touched.
* ``run_id`` — a random UUID minted per web-process boot, kept in
  memory; together with the owner pid and its procfs starttime it names
  the exact dead owner an orphan must have.
* claim ``token`` — a per-child random secret injected into the child
  environment (``MPG_PROCESS_CLAIM``); at recovery the token is read back
  from ``/proc/<pid>/environ``, so a PID reused by an unrelated process
  can never be signalled.

Storage layout under ``config_dir/process_claims/``::

    instance_id                  (sibling of the folder)
    active/<claim_id>.json       one per live, owned extraction FFmpeg
    todos/<todo_id>.json         durable, observable cleanup TODOs

Signal sends, sleeps and clocks are module-level indirections so the
state machine and recovery are deterministically testable without real
processes.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import secrets
import shutil
import signal
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from . import procfs_info

INSTANCE_ID_FILENAME = "instance_id"
# Environment variable carrying the per-child claim token.
CLAIM_ENV_VAR = "MPG_PROCESS_CLAIM"

CLAIM_SCHEMA = "ffmpeg-frame-extraction"
CLAIM_VERSION = 1
CLAIM_STATE_ACTIVE = "active"

# Minted once per interpreter boot; identifies the owning web incarnation
# inside every claim this process publishes.
_RUN_ID = uuid.uuid4().hex

# (config_dir, instance_id) cache so a long-running process reads the file once.
_instance_id_cache: tuple[str, str] | None = None

# ---------------------------------------------------------------------------
# TODO kinds — the fixed vocabulary of observable, durable cleanup work.
# ---------------------------------------------------------------------------

TODO_GRACEFUL_TIMEOUT = "graceful_terminate_timeout"
TODO_KILL_FAILED = "kill_failed"
TODO_CLEANUP_FAILED = "cleanup_failed"
TODO_IDENTITY_UNCONFIRMED = "identity_unconfirmed"
TODO_KINDS = frozenset({TODO_GRACEFUL_TIMEOUT, TODO_KILL_FAILED, TODO_CLEANUP_FAILED, TODO_IDENTITY_UNCONFIRMED})
# Deterministic TODO ids are uuid4 hex; this keeps manual resolve within the todos dir.
_TODO_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TODO_OPEN = "open"
TODO_RESOLVED = "resolved"


class ClaimPersistError(RuntimeError):
    """Raised when a claim cannot be persisted — the caller must not leave the child unowned."""


@dataclass(frozen=True)
class ClaimContext:
    """Job/worker identity attached to extraction FFmpeg spawned by the current thread.

    Set at worker-thread boundaries (preview dispatcher workers and the
    Intro & Credits job thread); consumed by the runner when publishing a
    claim. Every field is optional — a claim without a known job is still
    fully recoverable by process identity — but ``job_id`` is expected on
    every real job path.
    """

    job_id: str | None = None
    worker_id: int | None = None
    worker_type: str | None = None  # "GPU" / "CPU"
    worker_name: str | None = None  # display name, e.g. "GPU 3"
    gpu: str | None = None
    gpu_device: str | None = None
    group_id: str | None = None
    kind: str | None = None  # override the runner's preview/chapter inference

    def worker_record(self) -> dict[str, Any]:
        return {
            "id": self.worker_id,
            "type": self.worker_type,
            "name": self.worker_name,
            "gpu": self.gpu,
            "gpu_device": self.gpu_device,
            "group_id": self.group_id,
        }


_claim_context: contextvars.ContextVar[ClaimContext | None] = contextvars.ContextVar(
    "mpg_process_claim_context", default=None
)


def current_claim_context() -> ClaimContext | None:
    """The ClaimContext set for this thread/task, if any."""
    return _claim_context.get()


def set_claim_context(ctx: ClaimContext | None) -> contextvars.Token:
    """Set the current ClaimContext; pair with :func:`reset_claim_context`."""
    return _claim_context.set(ctx)


def reset_claim_context(token: contextvars.Token) -> None:
    """Restore the ClaimContext to before :func:`set_claim_context`."""
    _claim_context.reset(token)


def get_run_id() -> str:
    """This web-process incarnation's random id (stable for the process lifetime)."""
    return _RUN_ID


def _mkdir_p(path: str) -> None:
    """``makedirs`` that stays correct when callers have ``os.path.exists`` mocked truthy.

    Plain ``os.makedirs(exist_ok=True)`` decides which ancestors to create
    via ``os.path.exists``; some (test) environments patch that to a truthy
    value for every path, which makes it skip ancestor creation and raise
    ``FileNotFoundError``. On that exact failure, walk parents down with
    plain ``mkdir`` (whose EEXIST tells the truth) and retry.
    """
    try:
        os.makedirs(path, exist_ok=True)
        return
    except FileNotFoundError:
        parent = os.path.dirname(path)
        if parent and parent != path:
            _mkdir_p(parent)
        try:
            os.mkdir(path)
        except FileExistsError:
            pass


def _atomic_write(path: str, text: str, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` via a same-directory temp file + rename with fixed permissions."""
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_or_create_instance_id(config_dir: str) -> str:
    """Return the stable installation id, creating it on first use.

    Stored as ``config_dir/instance_id`` (0600). An existing file is
    reused verbatim (whitespace stripped) so every boot and every worker
    thread sees the same identity. Creation is atomic; an unreadable
    present file yields an ephemeral id for the boot rather than being
    overwritten.
    """
    global _instance_id_cache
    if _instance_id_cache is not None and _instance_id_cache[0] == config_dir:
        return _instance_id_cache[1]

    path = os.path.join(config_dir, INSTANCE_ID_FILENAME)
    instance_id = ""
    try:
        with open(path, encoding="utf-8") as fh:
            instance_id = fh.read().strip()
    except FileNotFoundError:
        instance_id = ""
    except OSError as exc:
        logger.warning(
            "The instance identity file {} is unreadable ({}: {}); using an ephemeral identity for this boot. "
            "Orphan-process reconciliation may skip claims until the file is readable again.",
            path,
            type(exc).__name__,
            exc,
        )
        instance_id = uuid.uuid4().hex
        _instance_id_cache = (config_dir, instance_id)
        return instance_id

    if not instance_id:
        instance_id = uuid.uuid4().hex
        try:
            os.makedirs(config_dir, exist_ok=True)
            _atomic_write(path, f"{instance_id}\n")
            logger.info("Created a new instance identity at {}", path)
        except OSError as exc:
            logger.warning(
                "Could not persist the instance identity to {} ({}: {}); using an ephemeral identity for this boot. "
                "Claims published this boot may be treated as foreign by a future instance that later creates the "
                "file — orphan reconciliation prefers leaving an unrecognised process alone over killing it.",
                path,
                type(exc).__name__,
                exc,
            )

    _instance_id_cache = (config_dir, instance_id)
    return instance_id


def new_claim_token() -> str:
    """Fresh per-child random secret (also injected into the child environment)."""
    return secrets.token_hex(16)


# ---------------------------------------------------------------------------
# Indirections for time/signals so the state machine is testable without
# waiting real seconds or signalling real processes.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sleep(delay: float) -> None:  # pragma: no cover - replaced in tests
    time.sleep(max(0.0, delay))


def _monotonic() -> float:  # pragma: no cover - replaced in tests
    return time.monotonic()


def _kill_process_group(pgid: int | None, sig: int) -> bool | None:
    """Signal a process group.

    Returns ``True`` when delivered, ``False`` when the group is gone
    (ESRCH) / pgid is unknown, and ``None`` when signalling is denied
    (EPERM) — callers must treat denial as "process may still be alive",
    never as "gone".
    """
    if pgid is None:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        logger.warning("No permission to signal process group {} ({}: {})", pgid, type(exc).__name__, exc)
        return None


def get_process_group(pid: int) -> int | None:
    """The live process-group id of ``pid`` (None once the process is gone)."""
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None


@dataclass
class TerminationResult:
    """Outcome of one terminate-group attempt."""

    exited: bool
    kill_sent: bool = False
    cont_sent: bool = False
    signals: tuple[int, ...] = ()
    detail: str = ""


class ClaimHandle:
    """In-process state machine for one published claim.

    Guards the two things that must happen at most once even when cancel
    races the process's natural exit: a signal round and the claim
    revocation. The bound :class:`subprocess.Popen` lets the handle use
    the exact ``wait``/``poll`` for exit confirmation; boot-time
    recovery (no live Popen object) uses the standalone
    :func:`wait_pid_exit` + :func:`terminate_recovery_target` helpers.
    """

    def __init__(self, store: ClaimsStore, record: dict[str, Any]):
        self._store = store
        self.record = record
        self._proc: Any = None
        self._lock = threading.Lock()
        self._released = False
        self._signals: list[int] = []
        self._last_result: TerminationResult | None = None

    # -- identity accessors -------------------------------------------------

    @property
    def claim_id(self) -> str:
        return str(self.record["claim_id"])

    @property
    def pid(self) -> int:
        return int(self.record["process"]["pid"])

    @property
    def pgid(self) -> int | None:
        value = self.record["process"].get("pgid")
        return int(value) if value else None

    @property
    def token(self) -> str:
        return str(self.record["process"]["token"])

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def bind_process(self, proc: Any) -> None:
        """Attach the live Popen so exit confirmation can use ``poll``/``wait``."""
        self._proc = proc

    # -- signalling ---------------------------------------------------------

    def _deliver(self, sig: int) -> bool:
        """Deliver one signal: process group when known, else the bound Popen.

        Returns True only when the target is confirmed gone (ESRCH). The
        Popen fallback matters when the group id could not be read right
        after spawn (e.g. the child exited instantly) and on platforms
        without process groups in tests.
        """
        pgid = self.pgid
        if pgid is not None:
            # Only a definite ESRCH means "already gone"; delivered or denied stays alive.
            return _kill_process_group(pgid, sig) is False
        if self._proc is not None:
            try:
                if sig == signal.SIGTERM:
                    self._proc.terminate()
                elif sig == signal.SIGKILL:
                    self._proc.kill()
                else:
                    self._proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                return True
        return False

    def _signal_once(self, sig: int) -> bool:
        """Send ``sig`` to the group at most once; ESRCH counts as 'gone'.

        Returns True when the process was already gone (so callers need
        not wait), None-y False when delivered or undeliverable-but-alive.
        """
        if sig in self._signals:
            return False
        self._signals.append(sig)
        return self._deliver(sig)

    def _wait_bound_exit(self, timeout_s: float) -> bool:
        """Confirm the bound Popen exited within ``timeout_s`` (testable clock/sleep)."""
        if self._proc is None:
            return wait_pid_exit(self.pid, timeout_s)
        deadline = _monotonic() + max(0.0, timeout_s)
        while True:
            if self._proc.poll() is not None:
                return True
            if _monotonic() >= deadline:
                return False
            try:
                self._proc.wait(timeout=min(0.1, max(0.0, deadline - _monotonic())))
            except Exception as exc:
                # subprocess.TimeoutExpired: keep looping. Anything else: only
                # return if the process is genuinely gone now. ALWAYS sleep
                # afterwards — skipping it here was a busy-loop hammering wait().
                if type(exc).__name__ != "TimeoutExpired" and self._proc.poll() is not None:
                    return True
            _sleep(min(0.1, max(0.0, deadline - _monotonic())))

    def terminate_group(self, *, grace_s: float = 5.0, kill_s: float = 5.0, resume: bool = True) -> TerminationResult:
        """SIGCONT (resume a paused group) → SIGTERM with a bounded wait → SIGKILL with a bounded wait.

        Idempotent and concurrency-safe: a second concurrent caller gets
        the same result without re-signalling. Never raises — an
        unconfirmed exit is reported via the returned result so the
        caller records the right TODO instead of crashing a worker
        thread.
        """
        with self._lock:
            if self._last_result is not None:
                return self._last_result
            cont_sent = False
            if resume:
                if self._signal_once(signal.SIGCONT):
                    # Process was already gone before CONT.
                    self._last_result = TerminationResult(True, signals=tuple(self._signals), detail="already gone")
                    return self._last_result
                cont_sent = signal.SIGCONT in self._signals
            if self._signal_once(signal.SIGTERM):
                self._last_result = TerminationResult(
                    True, cont_sent=cont_sent, signals=tuple(self._signals), detail="already gone at term"
                )
                return self._last_result
            exited = self._wait_bound_exit(grace_s)
            kill_sent = False
            detail = ""
            if not exited:
                self._signal_once(signal.SIGKILL)
                kill_sent = True
                exited = self._wait_bound_exit(kill_s)
                if not exited:
                    detail = "process did not exit after SIGKILL within the wait"
            result = TerminationResult(
                exited,
                kill_sent=kill_sent,
                cont_sent=cont_sent,
                signals=tuple(self._signals),
                detail=detail,
            )
            self._last_result = result
            return result

    # -- revocation ---------------------------------------------------------

    def release_after_exit(self) -> bool:
        """Delete the active claim once the bound process is confirmed gone.

        Refuses (returns False, no deletion) while the bound Popen is
        still running — callers must have a real exit confirmation.
        Idempotent afterwards. Unbound handles (recovery-built) are
        released through :meth:`ClaimsStore.delete_active_record`.
        """
        with self._lock:
            if self._released:
                return True
            if self._proc is not None and not self.is_confirmed_exit():
                return False
            self._store.delete_active_record(self.record)
            self._released = True
            return True

    def signal_group(self, sig: int) -> bool:
        """Signal the group without one-shot dedup (pause episodes repeat).

        Returns False when the target is gone or the signal could not be
        delivered; the poll loop observes an exit on its next iteration.
        Falls back to the bound Popen when the group id is unknown.
        """
        gone = self._deliver(sig)
        return not gone

    def is_confirmed_exit(self) -> bool:
        """Whether the process is truly gone (bound Popen poll, or a procfs lookup for recovery-built handles)."""
        if self._proc is None:
            return not procfs_info.pid_alive(self.pid)
        # Popen caches the exit code once poll/wait returned it; trust it
        # instead of issuing another poll (finite test iterators aside, this
        # is also one fewer syscall on the hot convergence path).
        if getattr(self._proc, "returncode", None) is not None:
            return True
        return self._proc.poll() is not None

    def shutdown_converge(self, *, kill_at: float, deadline: float) -> str:
        """One bounded shutdown pass: resume+TERM, grace window, then KILL until the deadline.

        Revokes the claim on confirmed exit. Returns ``"released"`` or the
        TODO kind recorded when the process could not be confirmed dead
        (``graceful_terminate_timeout`` when the budget ended before/around
        SIGKILL, ``kill_failed`` after SIGKILL was sent).
        """
        with self._lock:
            if self._released:
                return "released"
            already_gone = self._signal_once(signal.SIGCONT) or self._signal_once(signal.SIGTERM)
        if already_gone and self.release_after_exit():
            return "released"
        while _monotonic() < kill_at:
            if self.is_confirmed_exit() and self.release_after_exit():
                return "released"
            _sleep(0.1)
        kill_sent = _monotonic() < deadline
        if kill_sent:
            with self._lock:
                self._signal_once(signal.SIGKILL)
            while _monotonic() < deadline:
                if self.is_confirmed_exit() and self.release_after_exit():
                    return "released"
                _sleep(0.1)
        kind = TODO_KILL_FAILED if kill_sent else TODO_GRACEFUL_TIMEOUT
        self.retain_with_todo(kind, "the extraction process was still unconfirmed when the shutdown budget expired")
        return kind

    def retain_with_todo(self, kind: str, detail: str, *, paths: dict[str, Any] | None = None) -> dict[str, Any]:
        """Keep the claim active and record a durable TODO (process unconfirmed / cleanup failed)."""
        return self._store.add_todo(
            kind,
            job_id=self.record.get("job_id") or None,
            worker=self.record.get("worker") or None,
            pid=self.pid,
            claim_id=self.claim_id,
            paths=paths or self.record.get("paths") or None,
            detail=detail,
        )


def wait_pid_exit(pid: int, timeout_s: float, *, poll_interval_s: float = 0.1) -> bool:
    """Bounded wait for an unbound PID to disappear from procfs (boot recovery)."""
    deadline = _monotonic() + max(0.0, timeout_s)
    while True:
        if not procfs_info.pid_alive(pid):
            return True
        if _monotonic() >= deadline:
            return False
        _sleep(poll_interval_s)


@dataclass
class ActiveClaim:
    """A scanned active record, or a corrupt-file marker (record is None)."""

    path: str
    record: dict[str, Any] | None = None
    error: str | None = None

    @property
    def corrupt(self) -> bool:
        return self.record is None


class ClaimsStore:
    """Filesystem-backed claim + TODO store with an in-process handle registry."""

    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self.claims_dir = os.path.join(config_dir, "process_claims")
        self.active_dir = os.path.join(self.claims_dir, "active")
        self.todos_dir = os.path.join(self.claims_dir, "todos")
        self._ensure_dirs()
        self.instance_id = load_or_create_instance_id(config_dir)
        # claim_id -> live handle owned by THIS process (shutdown coordination).
        self._handles: dict[str, ClaimHandle] = {}
        self._handles_lock = threading.RLock()
        # Most recent recovery summary, surfaced by the status API.
        self.last_recovery: dict[str, Any] | None = None

    def _ensure_dirs(self) -> None:
        for target in (self.claims_dir, self.active_dir, self.todos_dir):
            _mkdir_p(target)
        try:
            os.chmod(self.claims_dir, 0o700)
            os.chmod(self.active_dir, 0o700)
            os.chmod(self.todos_dir, 0o700)
        except OSError:
            pass

    # -- claims -------------------------------------------------------------

    def publish_claim(
        self,
        *,
        kind: str,
        job_id: str | None,
        worker: dict[str, Any] | None,
        ffmpeg_path: str,
        args: list[str] | tuple[str, ...],
        pid: object,
        pgid: int | None,
        output_folder: str,
        stderr_log: str,
        ephemeral_output: bool,
        token: str,
    ) -> ClaimHandle:
        """Atomically persist an active claim for a just-spawned child and register its handle.

        Raises :class:`ClaimPersistError` on any filesystem failure so the
        caller can terminate the child it cannot make reclaimable.
        """
        owner_pid, owner_ticks = procfs_info.self_identity()
        # Real Popen pids are always ints; tolerate non-int test doubles by
        # degrading to None (the record then isn't directly addressable).
        try:
            pid_value = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid_value = None
        record = {
            "version": CLAIM_VERSION,
            "schema": CLAIM_SCHEMA,
            "state": CLAIM_STATE_ACTIVE,
            "claim_id": uuid.uuid4().hex,
            "created_at": _now_iso(),
            "kind": kind,
            "job_id": job_id or "",
            "worker": worker or {},
            "command": {"ffmpeg": ffmpeg_path, "argv_sha256": procfs_info.argv_hash(args)},
            "process": {
                "pid": pid_value,
                "pgid": int(pgid) if pgid else None,
                "start_ticks": procfs_info.read_start_ticks(pid_value) if pid_value is not None else None,
                "token": token,
            },
            "owner": {
                "instance_id": self.instance_id,
                "run_id": get_run_id(),
                "pid": int(owner_pid),
                "start_ticks": owner_ticks,
            },
            "paths": {
                "output_folder": output_folder or "",
                "stderr_log": stderr_log or "",
                "ephemeral_output": bool(ephemeral_output),
            },
        }
        target = self._active_path(record["claim_id"])
        try:
            _atomic_write(target, json.dumps(record, ensure_ascii=False, sort_keys=True))
        except OSError as exc:
            raise ClaimPersistError(f"Could not persist process claim {target}: {exc}") from exc
        handle = ClaimHandle(self, record)
        with self._handles_lock:
            self._handles[record["claim_id"]] = handle
        return handle

    def _active_path(self, claim_id: str) -> str:
        return os.path.join(self.active_dir, f"{claim_id}.json")

    def _todo_path(self, todo_id: str) -> str:
        return os.path.join(self.todos_dir, f"{todo_id}.json")

    def delete_active_record(self, record: dict[str, Any]) -> bool:
        """Remove an active record file. Missing is success; other OSErrors are logged."""
        claim_id = str(record.get("claim_id") or "")
        path = self._active_path(claim_id) if claim_id else ""
        try:
            if path:
                os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "Could not delete the process claim file {} ({}: {}); a future boot reconciles it safely",
                path,
                type(exc).__name__,
                exc,
            )
            return False
        with self._handles_lock:
            self._handles.pop(claim_id, None)
        return True

    def unregister_handle(self, handle: ClaimHandle) -> None:
        with self._handles_lock:
            self._handles.pop(handle.claim_id, None)

    def active_handles(self) -> list[ClaimHandle]:
        with self._handles_lock:
            return list(self._handles.values())

    def scan_active_claims(self) -> list[ActiveClaim]:
        """Read every active claim on disk; unparseable files come back as corrupt markers."""
        out: list[ActiveClaim] = []
        try:
            names = sorted(os.listdir(self.active_dir))
        except FileNotFoundError:
            self._ensure_dirs()
            return out
        except OSError as exc:
            logger.warning(
                "Could not list the process-claim directory {}: {}: {}", self.active_dir, type(exc).__name__, exc
            )
            return out
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            path = os.path.join(self.active_dir, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    record = json.load(fh)
                if not isinstance(record, dict) or not record.get("claim_id") or not record.get("process"):
                    out.append(ActiveClaim(path=path, error="malformed claim record"))
                else:
                    out.append(ActiveClaim(path=path, record=record))
            except (OSError, json.JSONDecodeError) as exc:
                out.append(ActiveClaim(path=path, error=f"{type(exc).__name__}: {exc}"))
        return out

    # -- TODOs --------------------------------------------------------------

    @staticmethod
    def _todo_dedup_id(kind: str, pid: int | None, key: str) -> str:
        """Deterministic id for a TODO identity, so the same event updates one file across restarts."""
        return uuid.uuid5(uuid.NAMESPACE_URL, f"mpg-process-todo|{kind}|{pid if pid is not None else '-'}|{key}").hex

    def add_todo(
        self,
        kind: str,
        *,
        job_id: str | None = None,
        worker: dict[str, Any] | None = None,
        pid: int | None = None,
        claim_id: str | None = None,
        paths: dict[str, Any] | str | None = None,
        detail: str = "",
    ) -> dict[str, Any]:
        """Create or refresh an open TODO (deduplicated by kind+pid+claim/path); never raises."""
        if kind not in TODO_KINDS:
            raise ValueError(f"Unknown process-cleanup TODO kind: {kind!r}")
        paths_value: dict[str, Any] | None
        if isinstance(paths, dict):
            paths_value = paths
        elif paths:
            paths_value = {"path": str(paths)}
        else:
            paths_value = None
        dedup_key = (
            claim_id or (paths_value.get("output_folder") or paths_value.get("path") if paths_value else "") or "global"
        )
        todo_id = self._todo_dedup_id(kind, pid, str(dedup_key))
        path = self._todo_path(todo_id)
        now = _now_iso()
        existing: dict[str, Any] | None = None
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                existing = loaded
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            existing = None

        if existing is not None and existing.get("status") == TODO_RESOLVED:
            # The same condition came back after an admin/reaper dismissed it: reopen.
            existing = None
        if existing is not None:
            record = existing
            record["status"] = TODO_OPEN
            record["updated_at"] = now
            record["detail"] = detail or record.get("detail", "")
            record["occurrences"] = int(record.get("occurrences", 1)) + 1
        else:
            record = {
                "id": todo_id,
                "version": CLAIM_VERSION,
                "kind": kind,
                "status": TODO_OPEN,
                "created_at": now,
                "updated_at": now,
                "occurrences": 1,
                "job_id": job_id or "",
                "worker": worker or {},
                "pid": int(pid) if pid is not None else None,
                "claim_id": claim_id or "",
                "paths": paths_value or {},
                "detail": detail or "",
                "resolution": "",
                "resolved_at": None,
            }
        try:
            _atomic_write(path, json.dumps(record, ensure_ascii=False, sort_keys=True))
        except OSError as exc:  # a TODO failure must never break the caller
            logger.warning("Could not persist process-cleanup TODO {} ({}: {})", path, type(exc).__name__, exc)
        return record

    def list_todos(self, status: str | None = TODO_OPEN) -> list[dict[str, Any]]:
        """Read TODO records; corrupt files are skipped with a warning."""
        out: list[dict[str, Any]] = []
        try:
            names = sorted(os.listdir(self.todos_dir))
        except FileNotFoundError:
            self._ensure_dirs()
            return out
        except OSError as exc:
            logger.warning("Could not list the process-cleanup TODO directory: {}", exc)
            return out
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            try:
                with open(os.path.join(self.todos_dir, name), encoding="utf-8") as fh:
                    record = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Skipping unreadable process-cleanup TODO {}: {}", name, exc)
                continue
            if not isinstance(record, dict):
                continue
            if status is None or record.get("status") == status:
                out.append(record)
        out.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
        return out

    def resolve_todo(self, todo_id: str, note: str = "") -> dict[str, Any] | None:
        """Mark one TODO resolved; returns the updated record or None when absent/open-state mismatch."""
        if not _TODO_ID_RE.fullmatch(todo_id or ""):
            return None
        path = self._todo_path(todo_id)
        try:
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if not isinstance(record, dict) or record.get("status") == TODO_RESOLVED:
            return record if isinstance(record, dict) else None
        record["status"] = TODO_RESOLVED
        record["resolved_at"] = _now_iso()
        record["resolution"] = note or "resolved"
        record["updated_at"] = _now_iso()
        try:
            _atomic_write(path, json.dumps(record, ensure_ascii=False, sort_keys=True))
        except OSError as exc:
            logger.warning("Could not persist resolution of TODO {}: {}", todo_id, exc)
        return record

    def resolve_matching(self, predicate: Callable[[dict[str, Any]], bool], note: str = "") -> list[str]:
        """Resolve every open TODO for which ``predicate`` returns True; returns resolved ids."""
        resolved: list[str] = []
        for record in self.list_todos(TODO_OPEN):
            try:
                matches = bool(predicate(record))
            except Exception:
                matches = False
            if matches and self.resolve_todo(str(record["id"]), note) is not None:
                resolved.append(str(record["id"]))
        return resolved

    def active_claim_summaries(self) -> list[dict[str, Any]]:
        """UI-safe summaries of this process's live handles (never includes argv/token)."""
        return [
            {
                "claim_id": h.claim_id,
                "pid": h.pid,
                "kind": h.record.get("kind", ""),
                "job_id": h.record.get("job_id", ""),
                "worker": (h.record.get("worker") or {}).get("name", ""),
                "created_at": h.record.get("created_at", ""),
            }
            for h in self.active_handles()
        ]


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_store: ClaimsStore | None
_store = None
_store_lock = threading.RLock()


def configure_claims(config_dir: str) -> ClaimsStore:
    """Configure (or replace) the process-wide store — app startup and tests use this."""
    global _store
    with _store_lock:
        _store = ClaimsStore(config_dir)
        return _store


def reset_claims_for_tests() -> None:
    """Forget the configured store (test isolation)."""
    global _store
    with _store_lock:
        _store = None


def get_claims_store() -> ClaimsStore:
    """The process-wide store, lazily rooted at ``$CONFIG_DIR`` or ``/config``."""
    global _store
    with _store_lock:
        if _store is None:
            config_dir = os.environ.get("CONFIG_DIR", "/config")
            _store = ClaimsStore(config_dir)
        return _store


def publish_claim(**kwargs: Any) -> ClaimHandle:
    """Convenience wrapper for :meth:`ClaimsStore.publish_claim` on the singleton."""
    return get_claims_store().publish_claim(**kwargs)


# ---------------------------------------------------------------------------
# Boot-time orphan recovery
# ---------------------------------------------------------------------------


def _cleanup_claim_paths(store: ClaimsStore, record: dict[str, Any]) -> bool:
    """Best-effort delete of the ephemeral output dir and the stderr log named in a dead claim.

    Only ``ephemeral_output`` directories (ad-hoc tmp dirs) are touched —
    frame-cache slots outlive jobs and must never be deleted. Failures
    become ``cleanup_failed`` TODOs. Returns True when a cleanup failure was
    recorded.
    """
    failed = False
    paths = record.get("paths") or {}
    output_dir = paths.get("output_folder") or ""
    if paths.get("ephemeral_output") and output_dir and os.path.isdir(output_dir):
        try:
            shutil.rmtree(output_dir)
        except OSError as exc:
            failed = True
            store.add_todo(
                TODO_CLEANUP_FAILED,
                job_id=record.get("job_id") or None,
                worker=record.get("worker") or None,
                pid=(record.get("process") or {}).get("pid"),
                claim_id=record.get("claim_id"),
                paths={"path": output_dir},
                detail=f"{type(exc).__name__}: {exc}",
            )
    stderr_log = paths.get("stderr_log") or ""
    if stderr_log and os.path.isfile(stderr_log):
        try:
            os.unlink(stderr_log)
        except OSError as exc:
            failed = True
            store.add_todo(
                TODO_CLEANUP_FAILED,
                job_id=record.get("job_id") or None,
                worker=record.get("worker") or None,
                pid=(record.get("process") or {}).get("pid"),
                claim_id=record.get("claim_id"),
                paths={"path": stderr_log},
                detail=f"{type(exc).__name__}: {exc}",
            )
    return failed


def _signal_recovery_target(pid: int, pgid: int | None, sig: int) -> bool | None:
    """Signal a recovery target by group (preferred) or pid.

    ``True`` delivered, ``False`` gone (ESRCH), ``None`` denied (EPERM).
    """
    if pgid is not None:
        result = _kill_process_group(pgid, sig)
        if result is not None:
            return result
        return None
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def _terminate_recovery_target(record: dict[str, Any], *, grace_s: float, kill_s: float) -> tuple[bool, str]:
    """SIGCONT → SIGTERM(grace) → SIGKILL(kill) for a verified orphan without a bound Popen."""
    proc = record["process"]
    pid = int(proc["pid"])
    pgid = proc.get("pgid")
    # Resume first: the orphan may have been SIGSTOP-paused when its owner died.
    _signal_recovery_target(pid, pgid, signal.SIGCONT)
    term = _signal_recovery_target(pid, pgid, signal.SIGTERM)
    if term is False:
        return True, "already gone at term"
    if term is None:
        return False, "signalling denied (EPERM); process left untouched"
    if wait_pid_exit(pid, grace_s):
        return True, "terminated"
    kill = _signal_recovery_target(pid, pgid, signal.SIGKILL)
    if kill is None:
        return False, "signalling denied (EPERM); process left untouched"
    if wait_pid_exit(pid, kill_s):
        return True, "killed"
    return False, "still unconfirmed after SIGKILL"


def recover_orphaned_processes(
    config_dir: str | None = None,
    *,
    grace_s: float = 10.0,
    kill_s: float = 5.0,
) -> dict[str, Any]:
    """Reconcile extraction FFmpeg claims left by previous boots. Runs BEFORE job requeue.

    Default-deny: a signal goes out only when the owner is confirmed dead
    AND the live PID matches on start ticks, the injected claim token and
    the argv hash. Anything uncertain is left alone and, where useful,
    surfaced as an ``identity_unconfirmed`` TODO.

    Returns a summary dict (also stored on the claim store for the status API).
    """
    store = configure_claims(config_dir) if config_dir is not None else get_claims_store()
    summary = {
        "procfs_available": procfs_info.procfs_available(),
        "scanned": 0,
        "killed": 0,
        "exited_already": 0,
        "skipped_owner_alive": 0,
        "skipped_foreign": 0,
        "unconfirmed": 0,
        "corrupt": 0,
        "cleanup_failed": 0,
        "todos_open": 0,
    }
    if not summary["procfs_available"]:
        logger.info(
            "procfs is not available on this platform — skipping orphan-process signalling; "
            "{} active claim(s) are left untouched",
            len(store.scan_active_claims()),
        )
        store.last_recovery = summary
        return summary

    for active in store.scan_active_claims():
        summary["scanned"] += 1
        if active.corrupt or active.record is None:
            summary["corrupt"] += 1
            logger.warning("Skipping unreadable process claim {}: {}", active.path, active.error)
            store.add_todo(
                TODO_IDENTITY_UNCONFIRMED,
                claim_id=os.path.splitext(os.path.basename(active.path))[0],
                paths={"path": active.path},
                detail=f"corrupt claim record: {active.error}",
            )
            summary["unconfirmed"] += 1
            continue

        record = active.record
        if record.get("schema") != CLAIM_SCHEMA:
            summary["skipped_foreign"] += 1
            continue
        owner = record.get("owner") or {}
        if owner.get("instance_id") != store.instance_id:
            # Another installation sharing the volume: never touch.
            summary["skipped_foreign"] += 1
            continue

        owner_pid = owner.get("pid")
        owner_ticks = owner.get("start_ticks")
        if owner_pid is not None:
            live_owner_ticks = procfs_info.read_start_ticks(int(owner_pid))
            if owner_ticks is not None and live_owner_ticks is not None and live_owner_ticks == owner_ticks:
                summary["skipped_owner_alive"] += 1
                continue

        proc_rec = record.get("process") or {}
        pid = proc_rec.get("pid")
        claim_id = record.get("claim_id")
        if pid is None:
            summary["unconfirmed"] += 1
            store.add_todo(
                TODO_IDENTITY_UNCONFIRMED,
                claim_id=claim_id,
                detail="claim record has no process pid",
            )
            continue
        pid = int(pid)

        if not procfs_info.pid_alive(pid):
            # Owner dead, child gone: clear prior-boot TODOs first, then clean and
            # revoke (a cleanup failure recorded now must stay open).
            store.resolve_matching(lambda t, cid=claim_id: t.get("claim_id") == cid, "process gone; reconciled at boot")
            summary["cleanup_failed"] += int(_cleanup_claim_paths(store, record))
            store.delete_active_record(record)
            summary["exited_already"] += 1
            continue

        # Four independent identity facts, default-deny.
        live_ticks = procfs_info.read_start_ticks(pid)
        record_ticks = proc_rec.get("start_ticks")
        if record_ticks is None or live_ticks is None or live_ticks != record_ticks:
            # PID reused by another process (or unknowable): the record no longer names anything killable.
            logger.warning(
                "Process claim {} (pid {}) start identity does not match a live process (record={}, live={}); "
                "not signalling and revoking the stale claim",
                claim_id,
                pid,
                record_ticks,
                live_ticks,
            )
            # Clear older TODOs for this claim, then record the new identity concern (it must stay open).
            store.resolve_matching(lambda t, cid=claim_id: t.get("claim_id") == cid, "pid reused; stale claim removed")
            store.add_todo(
                TODO_IDENTITY_UNCONFIRMED,
                job_id=record.get("job_id") or None,
                worker=record.get("worker") or None,
                pid=pid,
                claim_id=claim_id,
                paths=record.get("paths") or None,
                detail="start-time mismatch or unreadable: pid was reused",
            )
            store.delete_active_record(record)
            summary["unconfirmed"] += 1
            continue

        env = procfs_info.read_environ(pid)
        token_ok = bool(env) and env.get(CLAIM_ENV_VAR) == proc_rec.get("token")
        cmdline_ok = procfs_info.cmdline_matches_hash(pid, str((record.get("command") or {}).get("argv_sha256") or ""))
        if not token_ok or not cmdline_ok:
            logger.warning(
                "Process claim {} (pid {}) identity incomplete (env_token={}, cmdline={}); leaving the process "
                "untouched and keeping the claim for a later boot",
                claim_id,
                pid,
                token_ok,
                cmdline_ok,
            )
            store.add_todo(
                TODO_IDENTITY_UNCONFIRMED,
                job_id=record.get("job_id") or None,
                worker=record.get("worker") or None,
                pid=pid,
                claim_id=claim_id,
                paths=record.get("paths") or None,
                detail=f"identity could not be confirmed (token={token_ok}, cmdline={cmdline_ok})",
            )
            summary["unconfirmed"] += 1
            continue

        # Fully verified orphan: converge it.
        exited, how = _terminate_recovery_target(record, grace_s=grace_s, kill_s=kill_s)
        if not exited:
            logger.warning("Verified orphan ffmpeg pid {} ({}) survived SIGKILL; leaving claim + TODO", pid, how)
            store.add_todo(
                TODO_KILL_FAILED,
                job_id=record.get("job_id") or None,
                worker=record.get("worker") or None,
                pid=pid,
                claim_id=claim_id,
                paths=record.get("paths") or None,
                detail=how,
            )
            summary["unconfirmed"] += 1
            continue

        logger.info("Reclaimed orphaned extraction ffmpeg pid {} ({}) for job {}", pid, how, record.get("job_id"))
        store.resolve_matching(lambda t, cid=claim_id: t.get("claim_id") == cid, f"reclaimed at boot ({how})")
        summary["cleanup_failed"] += int(_cleanup_claim_paths(store, record))
        store.delete_active_record(record)
        summary["killed"] += 1

    summary["todos_open"] = len(store.list_todos())
    store.last_recovery = summary
    logger.info(
        "Orphan-process recovery: scanned={} killed={} exited_already={} owner_alive={} foreign={} unconfirmed={} "
        "corrupt={} cleanup_failed={} open_todos={}",
        summary["scanned"],
        summary["killed"],
        summary["exited_already"],
        summary["skipped_owner_alive"],
        summary["skipped_foreign"],
        summary["unconfirmed"],
        summary["corrupt"],
        summary["cleanup_failed"],
        summary["todos_open"],
    )
    return summary


def reconcile_cleanup_todos(config_dir: str | None = None) -> dict[str, Any]:
    """Administrator-triggered recheck: reconcile live claims, then auto-resolve cleared TODOs.

    * Runs one orphan-recovery pass (idempotent and default-deny).
    * A ``cleanup_failed`` TODO resolves when its path no longer exists.
    * A process TODO (kill/graceful/identity) resolves when its pid is gone
      and its active claim has been removed.

    Returns ``{before, after, resolved}``.
    """
    store = configure_claims(config_dir) if config_dir is not None else get_claims_store()
    before = len(store.list_todos(TODO_OPEN))
    try:
        # User-triggered: keep the wait budgets small so the HTTP request
        # cannot block on a long chain of unkillable orphans; the full
        # 10s/5s convergence runs once at boot.
        recover_orphaned_processes(store.config_dir, grace_s=2.0, kill_s=2.0)
    except Exception:  # never block the TODO-only reconciliation
        logger.exception("Reconciliation recovery pass failed")

    def _cleared(todo: dict[str, Any]) -> bool:
        kind = todo.get("kind")
        if kind == TODO_CLEANUP_FAILED:
            path = (todo.get("paths") or {}).get("path") or ""
            return bool(path) and not os.path.exists(path)
        pid = todo.get("pid")
        claim_id = todo.get("claim_id") or ""
        claim_gone = not os.path.exists(store._active_path(claim_id)) if claim_id else True
        process_gone = (not procfs_info.procfs_available()) or (pid is None) or (not procfs_info.pid_alive(int(pid)))
        # Only auto-clear process TODOs when BOTH conditions hold; an
        # identity_unconfirmed for a still-present pid stays for the admin.
        return claim_gone and process_gone

    resolved = store.resolve_matching(_cleared, "auto-resolved on recheck")
    after = len(store.list_todos(TODO_OPEN))
    return {"before": before, "after": after, "resolved": resolved}


# ---------------------------------------------------------------------------
# Graceful-shutdown coordination
# ---------------------------------------------------------------------------


def shutdown_all_claims(total_budget_s: float = 25.0) -> dict[str, Any]:
    """Converge every in-process claim within one shared, bounded window.

    Registered as an ``atexit`` hook: when s6 restarts the web service (or
    gunicorn exits gracefully), owned extraction FFmpeg groups get
    SIGCONT→SIGTERM and — only for those that linger — SIGKILL, all in
    parallel so the total wait stays inside the gunicorn 30 s graceful
    window. Confirmed exits revoke the claim; anything unconfirmed keeps
    its active claim plus a TODO and is reconciled by the next boot.

    Never raises: a shutdown hook must not block interpreter exit. When no
    store was ever configured this does NOT lazily create ``/config``.
    """
    summary = {"handles": 0, "released": 0, "kill_failed": 0, "graceful_timeout": 0, "errors": 0}
    store = _store
    if store is None:
        return summary
    try:
        handles = [h for h in store.active_handles() if not h.released]
        summary["handles"] = len(handles)
        if not handles:
            return summary

        total = max(0.0, float(total_budget_s))
        start = _monotonic()
        # ~20 s graceful, last ~5 s forced (scaled for tiny test budgets).
        kill_at = start + max(0.0, total - min(5.0, total * 0.25))
        deadline = start + total
        results: dict[str, str] = {}
        threads: list[threading.Thread] = []

        def _converge(handle: ClaimHandle) -> None:
            try:
                results[handle.claim_id] = handle.shutdown_converge(kill_at=kill_at, deadline=deadline)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Shutdown convergence for claim {} failed ({}: {})",
                    handle.claim_id,
                    type(exc).__name__,
                    exc,
                )
                results[handle.claim_id] = "error"

        for handle in handles:
            thread = threading.Thread(
                target=_converge, args=(handle,), name=f"claim-shutdown-{handle.pid}", daemon=True
            )
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join(timeout=max(0.001, deadline - _monotonic() + 1.0))

        for _claim_id, outcome in results.items():
            if outcome == "released":
                summary["released"] += 1
            elif outcome == TODO_KILL_FAILED:
                summary["kill_failed"] += 1
            elif outcome == TODO_GRACEFUL_TIMEOUT:
                summary["graceful_timeout"] += 1
            else:
                summary["errors"] += 1

        # Safety net: a convergence thread that never reported (e.g. killed
        # with the interpreter) must still leave an observable TODO behind.
        for handle in handles:
            if handle.claim_id not in results and not handle.released:
                handle.retain_with_todo(
                    TODO_GRACEFUL_TIMEOUT,
                    "the shutdown coordinator could not finish converging this claim",
                )
                summary["graceful_timeout"] += 1

        logger.info(
            "Process-claim shutdown convergence: {} handle(s), {} released, {} kill-failed, {} graceful-timeout",
            summary["handles"],
            summary["released"],
            summary["kill_failed"],
            summary["graceful_timeout"],
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Process-claim shutdown coordination failed ({}: {})", type(exc).__name__, exc)
    return summary

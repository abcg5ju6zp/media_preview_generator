"""Durable Plex Analyze backlog that retries independently of media jobs.

The in-memory :class:`~media_preview_generator.processing.plex_refresh.PlexRefreshQueue`
gives published previews an *immediate* Analyze attempt and bounds chapter
publication behind it. When that attempt is uncertain, or the memory queue is
full, or the process restarts, the only remaining evidence used to be the
``plex_refresh_pending`` marker in the ``.meta`` journal beside each output —
and that marker was revisited only if some *later* media job happened to
publish the same file. With no further jobs the Analyze was lost for good.

This module adds the durable half of the retry story:

* :class:`RefreshTodo` keeps the server id, source identity (canonical path
  plus source fingerprint), the output set and the publication token.
* :class:`RefreshBacklogStore` persists todos atomically to
  ``<config_dir>/plex_refresh_pending.json``; a token identifies one
  publication and only an accepted Analyze may clear it.
* :class:`PlexRefreshBacklog` runs one daemon worker that keeps retrying due
  todos with per-server pacing and per-todo exponential backoff. It pauses
  todos for disabled servers, terminates todos for deleted servers and
  vanished outputs, and never lets one server's backoff block another
  server's todos.

Load (at startup) and every dispatch re-validate each todo against the
*current* outputs and journals: only records whose exact marker still lives
on an existing output are retried. A record superseded by a newer
publication is retired without ever acknowledging the newer token.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..output.journal import (
    MARKER_CURRENT,
    MARKER_OUTPUT_MISSING,
    MARKER_SUPERSEDED,
    classify_plex_refresh_marker,
    clear_plex_refresh_pending,
)
from ..output.plex_hash import SourceFingerprint, get_source_fingerprint
from ..servers.base import ServerType
from .retry_queue import BACKOFF_SCHEDULE

if TYPE_CHECKING:
    from ..servers.registry import ServerRegistry
    from .plex_refresh import PlexRefreshQueue

#: Terminal reasons surfaced in snapshots and logs.
REASON_SUPERSEDED = "superseded"
REASON_SERVER_DELETED = "server_deleted"
REASON_SERVER_NOT_PLEX = "server_no_longer_plex"
REASON_OUTPUT_GONE = "output_gone"
REASON_SOURCE_REPLACED = "source_replaced"
REASON_MARKER_CLEARED = "marker_cleared"

_TERMINAL_REASONS = frozenset(
    {
        REASON_SUPERSEDED,
        REASON_SERVER_DELETED,
        REASON_SERVER_NOT_PLEX,
        REASON_OUTPUT_GONE,
        REASON_SOURCE_REPLACED,
        REASON_MARKER_CLEARED,
    }
)

#: How many terminal records, and for how long, are kept for observability.
TERMINAL_KEEP_COUNT = 200
TERMINAL_KEEP_SECONDS = 7 * 24 * 3600.0

_STORE_SCHEMA = 1
_STORE_FILENAME = "plex_refresh_pending.json"


@dataclass
class RefreshTodo:
    """One durable notification for one publication."""

    server_id: str
    canonical_path: str
    item_id: str | None
    output_paths: tuple[str, ...]
    token: str
    source_fingerprint: tuple[int, ...]
    created_at: float
    attempts: int = 0
    #: Monotonic timestamp; scheduling only, never persisted as wall clock.
    not_before: float = 0.0
    last_attempt_at: float | None = None
    last_error: str | None = None

    def to_json(self, *, deadline_epoch: float) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "canonical_path": self.canonical_path,
            "item_id": self.item_id,
            "output_paths": list(self.output_paths),
            "token": self.token,
            "source_fingerprint": list(self.source_fingerprint),
            "created_at": self.created_at,
            "attempts": self.attempts,
            "not_before_epoch": deadline_epoch,
            "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_json(cls, data: Any) -> RefreshTodo | None:
        if not isinstance(data, dict):
            return None
        try:
            token = data["token"]
            path = data["canonical_path"]
            server_id = data["server_id"]
            outputs = data["output_paths"]
            fingerprint = data["source_fingerprint"]
            created_at = float(data.get("created_at", 0.0))
        except (KeyError, TypeError, ValueError):
            return None
        if not (isinstance(token, str) and token and isinstance(path, str) and path and isinstance(server_id, str)):
            return None
        if not isinstance(outputs, list) or not all(isinstance(p, str) and p for p in outputs):
            return None
        if (
            not isinstance(fingerprint, list)
            or len(fingerprint) != 5
            or not all(isinstance(v, int) for v in fingerprint)
        ):
            return None
        item_id = data.get("item_id")
        return cls(
            server_id=server_id,
            canonical_path=path,
            item_id=item_id if isinstance(item_id, str) and item_id else None,
            output_paths=tuple(outputs),
            token=token,
            source_fingerprint=tuple(fingerprint),
            created_at=created_at,
            attempts=int(data.get("attempts", 0)) if isinstance(data.get("attempts", 0), int) else 0,
            last_attempt_at=_optional_float(data.get("last_attempt_at")),
            last_error=data.get("last_error") if isinstance(data.get("last_error"), str) else None,
        )


@dataclass
class TerminatedTodo:
    """A retired todo kept (bounded) so the terminal outcome stays observable."""

    server_id: str
    canonical_path: str
    token: str
    reason: str
    at: float

    def to_json(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "canonical_path": self.canonical_path,
            "token": self.token,
            "reason": self.reason,
            "at": self.at,
        }

    @classmethod
    def from_json(cls, data: Any) -> TerminatedTodo | None:
        if not isinstance(data, dict):
            return None
        reason = data.get("reason")
        if reason not in _TERMINAL_REASONS:
            return None
        try:
            return cls(
                server_id=str(data.get("server_id", "")),
                canonical_path=str(data.get("canonical_path", "")),
                token=str(data.get("token", "")),
                reason=reason,
                at=float(data.get("at", 0.0)),
            )
        except (TypeError, ValueError):
            return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class RefreshBacklogStore:
    """Atomic JSON persistence for active and terminated refresh todos."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        epoch: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._epoch = epoch
        self._lock = threading.RLock()
        self._todos: dict[str, RefreshTodo] = {}
        self._terminated: list[TerminatedTodo] = []
        self._disk_ok = True
        self._load()

    # ------------------------------------------------------------- loading
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            doc = json.loads(self._path.read_text())
        except (OSError, ValueError) as exc:
            logger.warning(
                "Plex refresh backlog at {} is unreadable ({}: {}); starting with an empty backlog. "
                "Outputs whose Analyze was still pending will be rediscovered from their journals "
                "once a new publication or recovery run writes the store again.",
                self._path,
                type(exc).__name__,
                exc,
            )
            self._disk_ok = False
            return
        if not isinstance(doc, dict) or doc.get("schema") != _STORE_SCHEMA:
            logger.warning("Ignoring Plex refresh backlog {} with an unknown schema", self._path)
            self._disk_ok = False
            return
        for raw in doc.get("todos", []) if isinstance(doc.get("todos", []), list) else []:
            todo = RefreshTodo.from_json(raw)
            if todo is not None:
                self._todos[todo.token] = todo
        for raw in doc.get("terminated", []) if isinstance(doc.get("terminated", []), list) else []:
            ended = TerminatedTodo.from_json(raw)
            if ended is not None:
                self._terminated.append(ended)
        self._prune_terminated_locked()

    # ----------------------------------------------------------- persistence
    def _write_locked(self) -> bool:
        now_epoch = self._epoch()
        doc = {
            "schema": _STORE_SCHEMA,
            "todos": [
                todo.to_json(deadline_epoch=now_epoch + max(0.0, todo.not_before - self._clock()))
                for todo in self._todos.values()
            ],
            "terminated": [ended.to_json() for ended in self._terminated],
        }
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w") as fh:
                fh.write(json.dumps(doc, separators=(",", ":")))
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, self._path)
            self._disk_ok = True
            return True
        except OSError as exc:
            if self._disk_ok:
                logger.warning(
                    "Cannot persist the Plex refresh backlog to {} ({}: {}); pending Analyze retries continue "
                    "in memory but would not survive a restart until the store becomes writable again.",
                    self._path,
                    type(exc).__name__,
                    exc,
                )
            self._disk_ok = False
            return False
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    @property
    def disk_persisted(self) -> bool:
        return self._disk_ok

    def _persist_locked(self) -> None:
        self._write_locked()

    def retry_persistence(self) -> None:
        """Best-effort flush after a previous write failure (called per pump)."""
        with self._lock:
            if not self._disk_ok and self._todos:
                self._write_locked()

    # -------------------------------------------------------------- mutation
    def upsert(
        self,
        *,
        server_id: str,
        canonical_path: str,
        item_id: str | None,
        output_paths: tuple[str, ...],
        token: str,
        source_fingerprint: tuple[int, ...],
        grace_seconds: float,
    ) -> RefreshTodo:
        """Add or refresh one publication's todo, retiring anything it replaces.

        A new token for the same ``(server, source)`` supersedes the previous
        publication. Only the store record is retired; the journal marker for
        the newer publication is never touched from here.
        """
        with self._lock:
            superseded = [
                todo
                for todo in self._todos.values()
                if todo.token != token and todo.server_id == server_id and todo.canonical_path == canonical_path
            ]
            for old in superseded:
                self._terminate_locked(old, REASON_SUPERSEDED)
            existing = self._todos.get(token)
            if existing is None:
                todo = RefreshTodo(
                    server_id=server_id,
                    canonical_path=canonical_path,
                    item_id=item_id,
                    output_paths=tuple(dict.fromkeys(output_paths)),
                    token=token,
                    source_fingerprint=tuple(source_fingerprint),
                    created_at=self._epoch(),
                    not_before=self._clock() + max(0.0, grace_seconds),
                )
                self._todos[token] = todo
            else:
                # A later job re-published/revisited the same publication.
                # Give the immediate queue another window without losing the
                # attempt history.
                existing.item_id = item_id
                existing.output_paths = tuple(dict.fromkeys(output_paths))
                existing.not_before = self._clock() + max(0.0, grace_seconds)
                todo = existing
            self._persist_locked()
            return todo

    def acknowledge(self, token: str) -> bool:
        """Remove a todo only after Plex explicitly accepted its Analyze."""
        with self._lock:
            if token not in self._todos:
                return False
            del self._todos[token]
            self._persist_locked()
            return True

    def record_failure(self, token: str, error: str, *, delay_seconds: float) -> None:
        with self._lock:
            todo = self._todos.get(token)
            if todo is None:
                return
            todo.attempts += 1
            todo.last_error = error[:500]
            todo.last_attempt_at = self._epoch()
            todo.not_before = self._clock() + max(0.0, delay_seconds)
            self._persist_locked()

    # --------------------------------------------------------------- queries
    def get(self, token: str) -> RefreshTodo | None:
        with self._lock:
            return self._todos.get(token)

    def active(self) -> list[RefreshTodo]:
        with self._lock:
            return list(self._todos.values())

    def has_active(self, server_id: str, canonical_path: str) -> bool:
        with self._lock:
            return any(
                todo.server_id == server_id and todo.canonical_path == canonical_path for todo in self._todos.values()
            )

    def tokens_for_server(self, server_id: str) -> set[str]:
        with self._lock:
            return {todo.token for todo in self._todos.values() if todo.server_id == server_id}

    def due(self, now: float) -> list[RefreshTodo]:
        with self._lock:
            return sorted(
                (todo for todo in self._todos.values() if todo.not_before <= now),
                key=lambda todo: (todo.not_before, todo.created_at),
            )

    def reset_schedule(self, tokens: set[str], not_before: float) -> None:
        """Startup takeover: every active record is schedulable almost at once."""
        with self._lock:
            changed = False
            for token in tokens:
                todo = self._todos.get(token)
                if todo is not None and todo.not_before != not_before:
                    todo.not_before = not_before
                    changed = True
            if changed:
                self._persist_locked()

    # ------------------------------------------------------------- terminal
    def _prune_terminated_locked(self) -> None:
        cutoff = self._epoch() - TERMINAL_KEEP_SECONDS
        kept = [ended for ended in self._terminated if ended.at >= cutoff]
        if len(kept) > TERMINAL_KEEP_COUNT:
            kept = kept[-TERMINAL_KEEP_COUNT:]
        self._terminated = kept

    def _terminate_locked(self, todo: RefreshTodo, reason: str, *, when: float | None = None) -> TerminatedTodo:
        self._todos.pop(todo.token, None)
        ended = TerminatedTodo(
            server_id=todo.server_id,
            canonical_path=todo.canonical_path,
            token=todo.token,
            reason=reason,
            at=when or self._epoch(),
        )
        self._terminated.append(ended)
        self._prune_terminated_locked()
        return ended

    def terminate(self, token: str, reason: str) -> TerminatedTodo | None:
        with self._lock:
            todo = self._todos.pop(token, None)
            if todo is None:
                return None
            ended = self._terminate_locked(todo, reason)
            self._persist_locked()
            return ended

    def terminate_server(self, server_id: str, reason: str) -> list[TerminatedTodo]:
        with self._lock:
            ended = [
                self._terminate_locked(todo, reason)
                for todo in list(self._todos.values())
                if todo.server_id == server_id
            ]
            if ended:
                self._persist_locked()
            return ended

    def terminated(self) -> list[TerminatedTodo]:
        with self._lock:
            return list(self._terminated)


def default_settings_server_provider(config_dir: str | Path) -> Callable[[], ServerRegistry | None]:
    """Build a live registry from ``settings.json`` on demand.

    The returned registry is cached per raw ``media_servers`` payload, so the
    polling worker does not rebuild clients on every tick while still picking
    up edits (enable/disable/delete) moments after they are saved.
    """

    config_dir = Path(config_dir)
    cache: dict[Any, Any] = {}

    def provide() -> ServerRegistry | None:
        try:
            from ..web.settings_manager import get_settings_manager

            raw = get_settings_manager(str(config_dir)).get("media_servers") or []
        except Exception as exc:  # settings unavailable during early boot/shutdown
            logger.debug("Plex refresh backlog could not read media servers: {}", exc)
            return None
        if not isinstance(raw, list):
            raw = []
        signature = json.dumps(raw, sort_keys=True, default=str)
        if "registry" not in cache or cache.get("signature") != signature:
            from ..servers.registry import ServerRegistry

            cache.clear()
            cache["signature"] = signature
            cache["registry"] = ServerRegistry.from_settings(raw)
        return cache["registry"]

    return provide


class PlexRefreshBacklog:
    """Drive durable Plex Analyze todos on a daemon worker, per-server paced."""

    def __init__(
        self,
        config_dir: str | Path | None = None,
        *,
        store: RefreshBacklogStore | None = None,
        store_path: str | Path | None = None,
        server_provider: Callable[[], ServerRegistry | None] | None = None,
        immediate: PlexRefreshQueue | None = None,
        min_server_interval: float = 3.0,
        poll_interval: float = 2.0,
        immediate_grace: float = 60.0,
        startup_grace: float = 2.0,
        backoff_schedule: tuple[float, ...] = BACKOFF_SCHEDULE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if store is None:
            base = Path(store_path) if store_path is not None else Path(config_dir or ".") / _STORE_FILENAME
            store = RefreshBacklogStore(base, clock=clock)
        self._store = store
        self._server_provider = server_provider or (
            default_settings_server_provider(config_dir) if config_dir is not None else (lambda: None)
        )
        self._immediate = immediate
        self._min_server_interval = max(0.0, min_server_interval)
        self._poll_interval = max(0.05, poll_interval)
        self._immediate_grace = max(0.0, immediate_grace)
        self._startup_grace = max(0.0, startup_grace)
        self._backoff_schedule = tuple(max(0.0, step) for step in backoff_schedule) or BACKOFF_SCHEDULE
        self._clock = clock
        self._condition = threading.Condition()
        self._gates: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._registry: ServerRegistry | None = None
        self._stopping = False
        self._reconcile_on_startup()
        self._thread = threading.Thread(target=self._run, name="plex-refresh-backlog", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- lifecycle
    def close(self, *, join_timeout: float = 5.0) -> None:
        """Stop the worker; every state change is already durable."""
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)

    @property
    def store(self) -> RefreshBacklogStore:
        return self._store

    def upsert(
        self,
        *,
        server_id: str,
        canonical_path: str,
        item_id: str | None,
        output_paths: tuple[str | Path, ...],
        token: str,
        source_fingerprint: SourceFingerprint | tuple[int, ...] | None,
    ) -> RefreshTodo:
        todo = self._store.upsert(
            server_id=server_id,
            canonical_path=canonical_path,
            item_id=item_id,
            output_paths=tuple(str(path) for path in output_paths),
            token=token,
            source_fingerprint=tuple(source_fingerprint or ()),
            grace_seconds=self._immediate_grace,
        )
        with self._condition:
            self._condition.notify_all()
        return todo

    def has_active(self, server_id: str, canonical_path: str) -> bool:
        return self._store.has_active(server_id, canonical_path)

    def snapshot(self) -> dict[str, Any]:
        """Observable state for the status API and logs."""
        now = self._clock()
        todos = self._store.active()
        registry = self._registry
        paused_ids = self._paused_server_ids(registry, todos)
        active_rows: list[dict[str, Any]] = []
        for todo in todos:
            config = registry.get_config(todo.server_id) if registry is not None else None
            active_rows.append(
                {
                    "server_id": todo.server_id,
                    "server_name": config.name if config is not None else todo.server_id,
                    "canonical_path": todo.canonical_path,
                    "item_id": todo.item_id,
                    "attempts": todo.attempts,
                    "next_retry_in": max(0, round(todo.not_before - now, 1)),
                    "last_error": todo.last_error,
                    "paused": todo.server_id in paused_ids,
                    "created_at": todo.created_at,
                }
            )
        active_rows.sort(key=lambda row: (row["paused"], row["next_retry_in"], row["canonical_path"]))
        counts: dict[str, int] = {}
        for ended in self._store.terminated():
            counts[ended.reason] = counts.get(ended.reason, 0) + 1
        return {
            "active": active_rows,
            "counts": {
                "active": len(active_rows),
                "paused": sum(1 for row in active_rows if row["paused"]),
            },
            "paused_server_ids": sorted(paused_ids),
            "terminated": counts,
            "terminated_recent": [
                {
                    "server_id": ended.server_id,
                    "canonical_path": ended.canonical_path,
                    "reason": ended.reason,
                    "at": ended.at,
                }
                for ended in self._store.terminated()[-25:]
            ],
            "disk_persisted": self._store.disk_persisted,
        }

    # ------------------------------------------------------------ startup
    def _reconcile_on_startup(self) -> None:
        """Load only records that still correspond to the current outputs."""
        todos = self._store.active()
        if not todos:
            return
        survivors: set[str] = set()
        terminated: list[TerminatedTodo] = []
        for todo in todos:
            reason, _fingerprint = self._inspect_todo(todo)
            if reason is None:
                survivors.add(todo.token)
                continue
            ended = self._store.terminate(todo.token, reason)
            if ended is not None:
                terminated.append(ended)
        # Records in flight at shutdown are taken over promptly on next boot.
        self._store.reset_schedule(survivors, self._clock() + self._startup_grace)
        loaded = len(survivors)
        if loaded:
            logger.info("Recovered {} pending Plex Analyze notification(s) from the durable refresh backlog", loaded)
        for ended in terminated:
            self._log_termination_record(ended)

    # --------------------------------------------------------------- worker
    def _run(self) -> None:
        with self._condition:
            while not self._stopping:
                wait_for = self._pump()
                self._condition.wait(timeout=min(self._poll_interval, max(0.05, wait_for)))

    def _refresh_registry(self) -> ServerRegistry | None:
        registry = self._server_provider()
        self._registry = registry
        return registry

    def _paused_server_ids(self, registry: ServerRegistry | None, todos: list[RefreshTodo]) -> set[str]:
        if registry is None:
            return set()
        return {
            todo.server_id
            for todo in todos
            if (config := registry.get_config(todo.server_id)) is not None and not config.enabled
        }

    def _pump(self) -> float:
        """One scheduling pass; returns seconds until the next interesting event."""
        self._store.retry_persistence()
        now = self._clock()
        registry = self._refresh_registry()
        todos = self._store.active()

        paused_ids = self._paused_server_ids(registry, todos)
        if registry is not None:
            present = {config.id for config in registry.configs()}
            for server_id in sorted({todo.server_id for todo in todos} - present):
                ended = self._store.terminate_server(server_id, REASON_SERVER_DELETED)
                for record in ended:
                    self._log_termination_record(record)
                todos = [todo for todo in todos if todo.server_id != server_id]

        wait_for = self._poll_interval
        chosen: RefreshTodo | None = None
        for todo in self._store.due(now):
            if todo.token in self._inflight:
                continue
            gate = self._gates.get(todo.server_id, 0.0)
            wait_for = min(wait_for, max(0.0, gate - now), max(0.0, todo.not_before - now))
            if gate > now or todo.not_before > now:
                continue
            if registry is None:
                continue
            config = registry.get_config(todo.server_id)
            if config is None:
                continue  # deletion is handled above once the registry proves absence
            if todo.server_id in paused_ids:
                continue  # disabled server: hold without backoff or failure counts
            if config.type is not ServerType.PLEX:
                ended = self._store.terminate_server(todo.server_id, REASON_SERVER_NOT_PLEX)
                for record in ended:
                    self._log_termination_record(record)
                break
            if self._immediate is not None and self._immediate.is_active(todo.server_id, todo.canonical_path):
                # The immediate queue owns the in-flight/recent attempt; its
                # outcome re-arms this record's backoff.
                continue
            chosen = todo
            break

        if chosen is not None:
            self._gates[chosen.server_id] = now + self._min_server_interval
            self._inflight.add(chosen.token)
            self._dispatch(chosen, registry)
            self._inflight.discard(chosen.token)
            return self._poll_interval

        upcoming = [todo.not_before - now for todo in todos if todo.server_id not in paused_ids]
        upcoming.extend(gate - now for gate in self._gates.values())
        upcoming = [seconds for seconds in upcoming if seconds > 0]
        if upcoming:
            wait_for = min(wait_for, min(upcoming))
        return wait_for

    def _inspect_todo(self, todo: RefreshTodo) -> tuple[str | None, tuple[int, ...] | None]:
        """Return ``(terminal_reason, current_fingerprint)``; reason None means current."""
        try:
            fingerprint = tuple(get_source_fingerprint(todo.canonical_path))
        except OSError:
            # Transiently unreadable source (unmounted share) stays uncertain.
            fingerprint = None
        if fingerprint is not None and tuple(todo.source_fingerprint) != fingerprint:
            return REASON_SOURCE_REPLACED, fingerprint
        classification = classify_plex_refresh_marker(
            [Path(path) for path in todo.output_paths],
            todo.canonical_path,
            todo.server_id,
            todo.token,
            source_fingerprint=fingerprint,
        )
        if classification == MARKER_CURRENT:
            return None, fingerprint
        if classification == MARKER_SUPERSEDED:
            return REASON_SUPERSEDED, fingerprint
        if classification == MARKER_OUTPUT_MISSING:
            return REASON_OUTPUT_GONE, fingerprint
        # MARKER_CLEARED: accepted/rewritten away before takeover.
        return REASON_MARKER_CLEARED, fingerprint

    def _dispatch(self, todo: RefreshTodo, registry: ServerRegistry) -> None:
        reason, fingerprint = self._inspect_todo(todo)
        if reason is not None:
            ended = self._store.terminate(todo.token, reason)
            if ended is not None:
                self._log_termination(todo, reason)
            with self._condition:
                self._condition.notify_all()
            return

        server = registry.get(todo.server_id)
        if server is None:
            # Configuration exists but the live client could not be built.
            self._store.record_failure(
                todo.token,
                "Plex client unavailable — check the server's connection settings",
                delay_seconds=self._backoff(todo.attempts),
            )
            return

        try:
            accepted = server.refresh_preview_metadata(todo.canonical_path, todo.item_id)
        except Exception as exc:  # uncertain result: keep the token and retry idempotently
            delay = self._retry_after(exc, self._backoff(todo.attempts))
            self._store.record_failure(todo.token, f"{type(exc).__name__}: {exc}", delay_seconds=delay)
            self._report_external(todo, False)
            logger.info(
                "Plex Analyze for {} on {} was not confirmed ({}: {}); keeping the durable notification "
                "and retrying in {:.0f}s",
                todo.canonical_path,
                getattr(server, "name", todo.server_id),
                type(exc).__name__,
                exc,
                delay,
            )
            return

        if accepted is not True:
            # Not indexed yet: unresolved, not failed — the same PUT is safe to repeat.
            self._store.record_failure(
                todo.token,
                "Plex has not indexed the file yet; a later scan/analyze will advertise the previews",
                delay_seconds=self._backoff(todo.attempts),
            )
            self._report_external(todo, False)
            return

        # Explicit acceptance: acknowledge only this exact publication
        # token. Publish the memory-barrier completion BEFORE removing the
        # durable record so a waiting chapter thread never observes "no
        # memory evidence and no durable todo" in the gap between the two.
        clear_plex_refresh_pending(
            [Path(path) for path in todo.output_paths],
            todo.canonical_path,
            todo.server_id,
            todo.token,
            # Clear against the recorded publication identity; this Analyze
            # accepted for this token, so a transiently un-stat-able source
            # must not leave its journal marker behind.
            source_fingerprint=(todo.source_fingerprint or None),
        )
        self._report_external(todo, True)
        self._store.acknowledge(todo.token)
        logger.info(
            "Plex accepted Analyze for {} on server {}; the preview timeline can now be advertised",
            todo.canonical_path,
            getattr(server, "name", todo.server_id),
        )
        with self._condition:
            self._condition.notify_all()

    def _backoff(self, attempts: int) -> float:
        schedule = self._backoff_schedule
        return schedule[min(attempts, len(schedule) - 1)]

    @staticmethod
    def _retry_after(exc: Exception, fallback: float) -> float:
        response = getattr(exc, "response", None)
        if response is None:
            return fallback
        try:
            if getattr(response, "status_code", None) != 429:
                return fallback
            raw = response.headers.get("Retry-After") if hasattr(response, "headers") else None
            seconds = float(raw)
            return max(fallback, min(seconds, 3600.0))
        except (TypeError, ValueError):
            return fallback

    def _report_external(self, todo: RefreshTodo, success: bool) -> None:
        if self._immediate is None:
            return
        try:
            self._immediate.report_external(todo.server_id, todo.canonical_path, success)
        except Exception as exc:  # barrier bookkeeping must never affect retries
            logger.debug("Could not relay Plex refresh completion to the chapter barrier: {}", exc)

    # --------------------------------------------------------------- logs
    def _log_termination(self, todo: RefreshTodo, reason: str) -> None:
        self._log_termination_record(
            TerminatedTodo(todo.server_id, todo.canonical_path, todo.token, reason, time.time())
        )

    def _log_termination_record(self, ended: TerminatedTodo) -> None:
        if ended.reason == REASON_SUPERSEDED:
            logger.info(
                "Retiring superseded Plex refresh notification for {} on server {} — a newer publication "
                "took over; the newer publication's token is not acknowledged by this record",
                ended.canonical_path,
                ended.server_id,
            )
        elif ended.reason == REASON_MARKER_CLEARED:
            logger.info(
                "Plex refresh notification for {} on server {} was already acknowledged; closing its record",
                ended.canonical_path,
                ended.server_id,
            )
        else:
            logger.warning(
                "Stopping Plex refresh retries for {} on server {}: {} (terminal result recorded in the "
                "refresh backlog status)",
                ended.canonical_path,
                ended.server_id,
                ended.reason,
            )

"""Bounded Plex notifications, with acknowledgements stored in output journals.

The in-memory queue gives newly published previews an *immediate* Analyze
attempt and bounds chapter publication behind its :meth:`PlexRefreshQueue.wait`
barrier. It is deliberately coalescing and bounded: a full queue returns
``False`` and the worker forgets in-flight work when the process exits.

Durability lives elsewhere: when an enqueue carries a journal notification
token, the same publication is also upserted into
:class:`~media_preview_generator.processing.plex_refresh_backlog.PlexRefreshBacklog`
(once configured at app startup). That backlog survives full queues, Plex
outages and restarts, retries with per-server pacing on its own daemon worker,
and retires a token only after Plex explicitly accepts the Analyze. A token of
``None`` (legacy outputs with no journal marker) stays best-effort instant
notification exactly as before.
"""

from __future__ import annotations

import atexit
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from ..output.journal import clear_plex_refresh_pending
from ..output.plex_hash import SourceFingerprint
from .retry_queue import BACKOFF_SCHEDULE

if TYPE_CHECKING:
    from ..servers.plex import PlexServer
    from .plex_refresh_backlog import PlexRefreshBacklog


@dataclass(frozen=True)
class _Refresh:
    server: PlexServer
    canonical_path: str
    item_id: str | None
    output_paths: tuple[Path, ...]
    notification_token: str | None
    source_fingerprint: SourceFingerprint | None


class PlexRefreshQueue:
    """Coalesce pending file notifications without blocking generation on Plex.

    One daemon worker drains a bounded queue and exits after an idle period.
    An in-flight key is removed before its request starts so another write
    during that request schedules a second notification.

    When a durable ``backlog`` is attached, token-bearing notifications are
    additionally persisted there; the backlog keeps retrying after capacity
    failures, uncertain responses and restarts. Outcomes it reaches on its own
    are reported back via :meth:`report_external` so ``wait`` callers observe
    them even though the memory queue never owned those attempts.
    """

    def __init__(
        self,
        *,
        capacity: int = 128,
        idle_seconds: float = 30.0,
        backlog: PlexRefreshBacklog | None = None,
    ) -> None:
        self._capacity = capacity
        self._idle_seconds = idle_seconds
        self._backlog = backlog
        self._pending: OrderedDict[tuple[str, str], _Refresh] = OrderedDict()
        self._inflight: set[tuple[str, str]] = set()
        self._completed: OrderedDict[tuple[str, str], bool] = OrderedDict()
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._closed = False

    def attach_backlog(self, backlog: PlexRefreshBacklog | None) -> None:
        """Bind the durable backlog to this singleton (configured at startup)."""
        self._backlog = backlog

    def enqueue(
        self,
        server: PlexServer,
        canonical_path: str,
        item_id: str | None,
        *,
        output_paths: tuple[Path, ...] = (),
        notification_token: str | None = None,
        source_fingerprint: SourceFingerprint | None = None,
    ) -> bool:
        """Queue a notification, returning False when capacity is exhausted.

        Token-bearing notifications are persisted to the durable backlog
        first, so a full memory queue (or a queue closed during shutdown)
        leaves a schedulable todo another process can take over.
        """
        key = (server.id, canonical_path)
        # Persist even while closed: a publication landing during shutdown is
        # exactly the state the next startup is allowed to take over.
        if notification_token is not None and self._backlog is not None:
            self._upsert_backlog(
                server.id,
                canonical_path,
                item_id,
                output_paths,
                notification_token,
                source_fingerprint,
            )
        with self._condition:
            if self._closed:
                return False
            if key not in self._pending and len(self._pending) >= self._capacity:
                logger.warning(
                    "Plex notification queue is full; previews for {} were saved, but {} must scan/analyze "
                    "the file later to advertise them to clients. The durable refresh backlog keeps retrying it.",
                    canonical_path,
                    server.name,
                )
                return False
            previous = self._pending.get(key)
            self._completed.pop(key, None)
            self._pending[key] = _Refresh(
                server,
                canonical_path,
                item_id or (previous.item_id if previous else None),
                output_paths,
                notification_token,
                source_fingerprint,
            )
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="plex-preview-notifications", daemon=True)
                self._worker.start()
            self._condition.notify_all()
            return True

    def _upsert_backlog(
        self,
        server_id: str,
        canonical_path: str,
        item_id: str | None,
        output_paths: tuple[Path, ...],
        token: str,
        source_fingerprint: SourceFingerprint | None,
    ) -> None:
        try:
            self._backlog.upsert(
                server_id=server_id,
                canonical_path=canonical_path,
                item_id=item_id,
                output_paths=output_paths,
                token=token,
                source_fingerprint=source_fingerprint,
            )
        except Exception as exc:  # durability trouble never blocks the instant notification
            logger.debug("Could not persist Plex refresh todo for {}: {}", canonical_path, exc)

    def is_active(self, server_id: str, canonical_path: str) -> bool:
        """Whether the immediate queue still owns an attempt for this source."""
        key = (server_id, canonical_path)
        with self._condition:
            return key in self._pending or key in self._inflight

    def report_external(self, server_id: str, canonical_path: str, success: bool) -> None:
        """Record an outcome the durable backlog reached without this queue.

        Wakes ``wait`` barriers only for keys this memory queue is not
        itself processing — a later enqueue still pops and replaces this
        cached completion.
        """
        key = (server_id, canonical_path)
        with self._condition:
            if key in self._pending or key in self._inflight:
                return
            self._completed[key] = success is True
            self._completed.move_to_end(key)
            while len(self._completed) > max(256, self._capacity):
                self._completed.popitem(last=False)
            self._condition.notify_all()

    def wait(
        self,
        server_id: str,
        canonical_path: str,
        *,
        required: bool = False,
        timeout: float = 30.0,
        cancel_check: Callable[[], bool] | None = None,
    ) -> bool:
        """Wait for this source's Analyze requests before registering chapters.

        Missing completion evidence fails closed when this publication requested
        Analyze. BIF-only callers never wait; failed requests retain their journal.
        """
        from .generator import CancellationError

        key = (server_id, canonical_path)
        deadline = time.monotonic() + timeout
        while True:
            if cancel_check and cancel_check():
                raise CancellationError("Chapter publication cancelled while waiting for Plex Analyze")
            with self._condition:
                if self._closed or threading.current_thread() is self._worker:
                    return False
                if key not in self._pending and key not in self._inflight:
                    completed = self._completed.get(key)
                    if completed is not None:
                        return completed
                    # No memory evidence: a durable todo for this exact
                    # publication still counts as an outstanding Analyze.
                    if self._backlog is not None and self._backlog.has_active(server_id, canonical_path):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        self._condition.wait(min(remaining, 0.2))
                        continue
                    return not required
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 0.2))

    def _backoff_for(self, token: str | None) -> float:
        if self._backlog is None or token is None:
            return float(BACKOFF_SCHEDULE[0])
        todo = self._backlog.store.get(token)
        attempt = todo.attempts if todo is not None else 0
        return float(BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)])

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._pending and not self._closed:
                    self._condition.wait(timeout=self._idle_seconds)
                if self._closed or not self._pending:
                    self._worker = None
                    return
                key, request = self._pending.popitem(last=False)
                self._inflight.add(key)
            notified = False
            try:
                notified = request.server.refresh_preview_metadata(request.canonical_path, request.item_id)
                if notified is True and request.notification_token is not None:
                    clear_plex_refresh_pending(
                        request.output_paths,
                        request.canonical_path,
                        request.server.id,
                        request.notification_token,
                        source_fingerprint=request.source_fingerprint,
                    )
            except Exception as exc:
                logger.warning(
                    "Previews saved for {}, but {} could not be notified: {}. Plex can activate them on a later analyze.",
                    request.canonical_path,
                    request.server.name,
                    exc,
                )
                self._record_backlog_failure(
                    request,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                if request.notification_token is not None and self._backlog is not None:
                    if notified is True:
                        try:
                            self._backlog.store.acknowledge(request.notification_token)
                        except Exception as exc:
                            logger.debug("Could not acknowledge durable Plex refresh todo: {}", exc)
                    else:
                        self._record_backlog_failure(
                            request,
                            "Plex has not indexed the file yet; a later scan/analyze will advertise the previews",
                        )
            finally:
                with self._condition:
                    self._inflight.discard(key)
                    self._completed[key] = notified is True
                    self._completed.move_to_end(key)
                    while len(self._completed) > max(256, self._capacity):
                        self._completed.popitem(last=False)
                    self._condition.notify_all()

    def _record_backlog_failure(self, request: _Refresh, reason: str) -> None:
        if self._backlog is None or request.notification_token is None:
            return
        try:
            self._backlog.store.record_failure(
                request.notification_token,
                reason,
                delay_seconds=self._backoff_for(request.notification_token),
            )
        except Exception as exc:
            logger.debug("Could not update the durable Plex refresh todo after a failed attempt: {}", exc)

    def close(self) -> None:
        """Discard memory work; durable todos are retried by the backlog after restart."""
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._condition.notify_all()


_queue = PlexRefreshQueue()
atexit.register(_queue.close)
_backlog_configured = False
_configure_lock = threading.Lock()


def configure_plex_refresh_backlog(config_dir: str | Path) -> PlexRefreshBacklog | None:
    """Create and start the process-wide durable refresh backlog.

    Called once from the Flask app factory after settings are available. The
    backlog loads only records that still match outputs on disk and keeps
    retrying them independently of any media job. Returns ``None`` when a
    backlog is already configured (the first configuration wins).
    """
    global _backlog_configured
    from .plex_refresh_backlog import PlexRefreshBacklog

    with _configure_lock:
        if _backlog_configured:
            return _queue._backlog  # noqa: SLF001
        backlog = PlexRefreshBacklog(config_dir, immediate=_queue)
        _queue.attach_backlog(backlog)
        atexit.register(backlog.close)
        _backlog_configured = True
        return backlog


def enqueue_plex_refresh(
    server: PlexServer,
    canonical_path: str,
    item_id: str | None,
    *,
    output_paths: tuple[Path, ...] = (),
    notification_token: str | None = None,
    source_fingerprint: SourceFingerprint | None = None,
) -> bool:
    """Notify Plex asynchronously, acknowledging only this journal token."""
    return _queue.enqueue(
        server,
        canonical_path,
        item_id,
        output_paths=output_paths,
        notification_token=notification_token,
        source_fingerprint=source_fingerprint,
    )


def wait_for_plex_refresh(
    server_id: str,
    canonical_path: str,
    *,
    required: bool = False,
    timeout: float = 30.0,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    """Bound chapter publication behind the source's pending Analyze requests."""
    return _queue.wait(server_id, canonical_path, required=required, timeout=timeout, cancel_check=cancel_check)

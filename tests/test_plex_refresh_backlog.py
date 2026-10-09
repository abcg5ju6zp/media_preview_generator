"""Durable Plex Analyze backlog semantics.

Covers persistence across restart, per-server paced retries, idempotent
resends on uncertain results, supersede/disable/delete terminal handling
and the glue with the in-memory PlexRefreshQueue (capacity overflow and
the chapter ``wait`` barrier).
"""

from __future__ import annotations

import json
import threading
import time
from threading import Event
from types import SimpleNamespace

import pytest

from media_preview_generator.output.journal import (
    MARKER_CLEARED,
    MARKER_CURRENT,
    MARKER_OUTPUT_MISSING,
    MARKER_SUPERSEDED,
    classify_plex_refresh_marker,
    clear_plex_refresh_pending,
    get_plex_refresh_pending,
    mark_plex_refresh_pending,
    write_meta,
)
from media_preview_generator.output.plex_hash import get_source_fingerprint
from media_preview_generator.processing.plex_refresh import PlexRefreshQueue
from media_preview_generator.processing.plex_refresh_backlog import (
    REASON_OUTPUT_GONE,
    REASON_SERVER_DELETED,
    REASON_SOURCE_REPLACED,
    REASON_SUPERSEDED,
    PlexRefreshBacklog,
    RefreshBacklogStore,
)
from media_preview_generator.servers.base import ServerType

FAST = dict(
    min_server_interval=0.02,
    poll_interval=0.01,
    immediate_grace=0.05,
    startup_grace=0.02,
    backoff_schedule=(0.05, 0.2, 1.0),
)


def _wait_until(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    assert predicate(), "condition was not met in time"


def _publish(tmp_path, name="video.mkv", *, server_id="plex-a", content=b"source bytes"):
    source = tmp_path / name
    source.write_bytes(content)
    output = tmp_path / f"{name}.index-sd.bif"
    output.write_bytes(b"bif-data")
    fingerprint = get_source_fingerprint(str(source))
    write_meta([output], str(source), publisher="plex_bundle", source_fingerprint=fingerprint)
    token = mark_plex_refresh_pending([output], str(source), server_id, source_fingerprint=fingerprint)
    assert token
    return source, output, fingerprint, token


class FakePlex:
    """Minimal stand-in for PlexServer.refresh_preview_metadata."""

    def __init__(self, server_id, outcomes):
        self.id = server_id
        self.name = server_id.upper()
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, str | None]] = []
        self._lock = threading.Lock()

    def refresh_preview_metadata(self, canonical_path, item_id):
        with self._lock:
            index = len(self.calls)
            self.calls.append((canonical_path, item_id))
            outcome = self._outcomes[min(index, len(self._outcomes) - 1)]
        if callable(outcome):
            return outcome(canonical_path, item_id)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ServerHolder:
    """Mutable registry view: servers can be enabled/disabled/deleted per pump."""

    def __init__(self, servers, *, enabled=None, types=None):
        self.servers = dict(servers)
        self.enabled = enabled or {server_id: True for server_id in servers}
        self.types = types or {}

    def registry(self):
        configs = [
            SimpleNamespace(
                id=server_id,
                name=server_id.upper(),
                enabled=self.enabled.get(server_id, True),
                type=self.types.get(server_id, ServerType.PLEX),
            )
            for server_id in self.servers
        ]
        return SimpleNamespace(
            get=lambda wanted: self.servers.get(wanted),
            get_config=lambda wanted: next((cfg for cfg in configs if cfg.id == wanted), None),
            configs=lambda: list(configs),
        )


def _start(tmp_path, holder, *, immediate=None, **overrides):
    store = overrides.pop("store", None) or RefreshBacklogStore(tmp_path / "plex_refresh_pending.json")
    params = dict(FAST)
    params.update(overrides)
    return PlexRefreshBacklog(
        store=store,
        server_provider=holder.registry,
        immediate=immediate,
        **params,
    )


def _add(backlog, source, output, fingerprint, token, *, server_id="plex-a", item_id="42"):
    backlog.upsert(
        server_id=server_id,
        canonical_path=str(source),
        item_id=item_id,
        output_paths=(str(output),),
        token=token,
        source_fingerprint=fingerprint,
    )


@pytest.fixture
def accepted():
    return lambda path, item_id: True


# ---------------------------------------------------------------- uncertai n


def test_uncertain_exception_is_retried_idempotently_then_acknowledged(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path)
    server = FakePlex("plex-a", [ConnectionError("plex offline"), True])
    holder = ServerHolder({"plex-a": server})
    backlog = _start(tmp_path, holder)
    try:
        _add(backlog, source, output, fingerprint, token)
        _wait_until(lambda: len(server.calls) == 2)
        assert server.calls == [(str(source), "42"), (str(source), "42")]
        assert get_plex_refresh_pending([output], str(source), "plex-a", source_fingerprint=fingerprint) is None
        assert backlog.store.active() == []
        doc = json.loads((tmp_path / "plex_refresh_pending.json").read_text())
        assert doc["todos"] == []
    finally:
        backlog.close()


def test_unindexed_result_is_uncertain_and_retried(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path)
    server = FakePlex("plex-a", [False, True])
    holder = ServerHolder({"plex-a": server})
    backlog = _start(tmp_path, holder)
    try:
        _add(backlog, source, output, fingerprint, token)
        _wait_until(lambda: len(server.calls) == 2 and backlog.store.get(token) is None, timeout=15.0)
        todo = backlog.store.get(token)
        assert todo is None  # accepted on attempt 2
        assert get_plex_refresh_pending([output], str(source), "plex-a", source_fingerprint=fingerprint) is None
    finally:
        backlog.close()


def test_remote_rejection_keeps_failure_reason_and_backs_off(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path)
    server = FakePlex("plex-a", [RuntimeError("403 refused"), True])
    holder = ServerHolder({"plex-a": server})
    backlog = _start(tmp_path, holder, immediate_grace=0.0, backoff_schedule=(0.05, 0.2, 1.0))
    try:
        _add(backlog, source, output, fingerprint, token)
        _wait_until(lambda: len(server.calls) == 1)
        time.sleep(0.02)  # inside the first 0.05s backoff
        assert len(server.calls) == 1
        todo = backlog.store.get(token)
        assert todo is not None and todo.attempts == 1
        assert "403 refused" in (todo.last_error or "")
        assert backlog.snapshot()["active"][0]["last_error"] == todo.last_error
        _wait_until(lambda: len(server.calls) == 2)
        assert get_plex_refresh_pending([output], str(source), "plex-a", source_fingerprint=fingerprint) is None
    finally:
        backlog.close()


# -------------------------------------------------------------- supersede n


def test_newer_publication_retires_old_record_without_acknowledging_it(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path)
    rejecting = FakePlex("plex-a", [RuntimeError("still uncertain")])
    holder = ServerHolder({"plex-a": rejecting})
    backlog = _start(tmp_path, holder, immediate_grace=0.0)
    try:
        _add(backlog, source, output, fingerprint, token)
        _wait_until(lambda: len(rejecting.calls) == 1)

        # A newer publication for the same output replaces the journal marker.
        newer = mark_plex_refresh_pending([output], str(source), "plex-a", source_fingerprint=fingerprint)
        assert newer and newer != token
        _add(backlog, source, output, fingerprint, newer)

        _wait_until(lambda: any(ended.token == token for ended in backlog.store.terminated()))
        ended = next(ended for ended in backlog.store.terminated() if ended.token == token)
        assert ended.reason == REASON_SUPERSEDED
        # The old record never confirms the newer publication.
        assert get_plex_refresh_pending([output], str(source), "plex-a", source_fingerprint=fingerprint) == newer
        _wait_until(lambda: len(rejecting.calls) >= 2)
        assert all(call[0] == str(source) for call in rejecting.calls)
    finally:
        backlog.close()


# -------------------------------------------------------------- terminal s


def test_vanished_output_terminates_at_startup(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path)
    holder = ServerHolder({"plex-a": FakePlex("plex-a", [True])})
    store = RefreshBacklogStore(tmp_path / "plex_refresh_pending.json")
    store.upsert(
        server_id="plex-a",
        canonical_path=str(source),
        item_id="42",
        output_paths=(str(output),),
        token=token,
        source_fingerprint=fingerprint,
        grace_seconds=0.0,
    )
    output.unlink()

    backlog = _start(tmp_path, holder, store=store)
    try:
        _wait_until(lambda: any(ended.token == token for ended in backlog.store.terminated()))
        ended = next(ended for ended in backlog.store.terminated() if ended.token == token)
        assert ended.reason == REASON_OUTPUT_GONE
        assert holder.servers["plex-a"].calls == []
    finally:
        backlog.close()


def test_replaced_source_terminates_without_calling_plex(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path)
    server = FakePlex("plex-a", [True])
    holder = ServerHolder({"plex-a": server})
    backlog = _start(tmp_path, holder)
    try:
        _add(backlog, source, output, fingerprint, token)
        source.write_bytes(b"completely different replacement bytes here")
        _wait_until(lambda: any(ended.token == token for ended in backlog.store.terminated()))
        ended = next(ended for ended in backlog.store.terminated() if ended.token == token)
        assert ended.reason == REASON_SOURCE_REPLACED
        time.sleep(0.05)
        assert server.calls == []
        # The stale journal marker is left alone; this record never clears it.
        meta = json.loads(output.with_suffix(output.suffix + ".meta").read_text())
        markers = {(entry["path"], entry["server_id"], entry["token"]) for entry in meta["plex_refresh_pending"]}
        assert (str(source), "plex-a", token) in markers
    finally:
        backlog.close()


def test_deleted_server_terminates_but_other_servers_keep_advancing(tmp_path):
    source_a, output_a, fp_a, token_a = _publish(tmp_path, "a.mkv", server_id="plex-a")
    source_b, output_b, fp_b, token_b = _publish(tmp_path, "b.mkv", server_id="plex-b")
    server_b = FakePlex("plex-b", [True])
    holder = ServerHolder({"plex-b": server_b})
    backlog = _start(tmp_path, holder, immediate_grace=0.0)
    try:
        _add(backlog, source_a, output_a, fp_a, token_a, server_id="plex-a")
        _add(backlog, source_b, output_b, fp_b, token_b, server_id="plex-b", item_id="77")
        _wait_until(lambda: not backlog.store.active())
        reasons = {ended.token: ended.reason for ended in backlog.store.terminated()}
        assert reasons[token_a] == REASON_SERVER_DELETED
        assert server_b.calls == [(str(source_b), "77")]
        assert get_plex_refresh_pending([output_b], str(source_b), "plex-b", source_fingerprint=fp_b) is None
    finally:
        backlog.close()


# ---------------------------------------------------------------- pause dd


def test_disabled_server_pauses_without_backoff_then_resumes(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path)
    server = FakePlex("plex-a", [True])
    holder = ServerHolder({"plex-a": server}, enabled={"plex-a": False})
    backlog = _start(tmp_path, holder)
    try:
        _add(backlog, source, output, fingerprint, token)
        time.sleep(0.15)
        snapshot = backlog.snapshot()
        assert server.calls == []
        assert snapshot["paused_server_ids"] == ["plex-a"]
        row = snapshot["active"][0]
        assert row["paused"] is True and row["attempts"] == 0

        holder.enabled["plex-a"] = True
        _wait_until(lambda: len(server.calls) == 1)
        assert get_plex_refresh_pending([output], str(source), "plex-a", source_fingerprint=fingerprint) is None
        assert backlog.snapshot()["active"] == []
    finally:
        backlog.close()


# --------------------------------------------------------------- restart ss


def test_restart_takes_over_a_record_inside_its_backoff_window(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path)
    path = tmp_path / "plex_refresh_pending.json"
    store = RefreshBacklogStore(path)
    store.upsert(
        server_id="plex-a",
        canonical_path=str(source),
        item_id="42",
        output_paths=(str(output),),
        token=token,
        source_fingerprint=fingerprint,
        grace_seconds=10.0,  # previous process died during a long backoff
    )
    assert store.record_failure(token, "interrupted by shutdown", delay_seconds=10.0) is None

    server = FakePlex("plex-a", [True])
    holder = ServerHolder({"plex-a": server})
    started = time.monotonic()
    backlog = _start(tmp_path, holder, store=RefreshBacklogStore(path), startup_grace=0.02)
    try:
        _wait_until(lambda: len(server.calls) == 1, timeout=5.0)
        assert time.monotonic() - started < 1.5  # scheduled promptly, not after the 10s backoff
        assert get_plex_refresh_pending([output], str(source), "plex-a", source_fingerprint=fingerprint) is None
    finally:
        backlog.close()


def test_startup_keeps_only_current_markers_and_never_confirms_newer_publication(tmp_path):
    current_src, current_out, current_fp, current_token = _publish(tmp_path, "keep.mkv", server_id="plex-a")

    old_src, old_out, old_fp, old_token = _publish(tmp_path, "old.mkv", server_id="plex-a")
    newer_token = mark_plex_refresh_pending([old_out], str(old_src), "plex-a", source_fingerprint=old_fp)
    assert newer_token != old_token

    gone_src, gone_out, gone_fp, gone_token = _publish(tmp_path, "gone.mkv", server_id="plex-a")
    gone_out.unlink()

    store = RefreshBacklogStore(tmp_path / "plex_refresh_pending.json")
    for args in (
        (current_src, current_out, current_fp, current_token),
        (old_src, old_out, old_fp, old_token),
        (gone_src, gone_out, gone_fp, gone_token),
    ):
        store.upsert(
            server_id="plex-a",
            canonical_path=str(args[0]),
            item_id="42",
            output_paths=(str(args[1]),),
            token=args[3],
            source_fingerprint=args[2],
            grace_seconds=0.0,
        )

    server = FakePlex("plex-a", [True])
    holder = ServerHolder({"plex-a": server})
    backlog = _start(tmp_path, holder, store=store)
    try:
        _wait_until(lambda: not backlog.store.active())
        reasons = {ended.token: ended.reason for ended in backlog.store.terminated()}
        assert reasons[old_token] == REASON_SUPERSEDED
        assert reasons[gone_token] == REASON_OUTPUT_GONE
        # The newer publication is still pending and was not acknowledged by startup.
        assert get_plex_refresh_pending([old_out], str(old_src), "plex-a", source_fingerprint=old_fp) == newer_token
        # The current record was accepted by the worker.
        assert (
            get_plex_refresh_pending([current_out], str(current_src), "plex-a", source_fingerprint=current_fp) is None
        )
        assert [call[0] for call in server.calls] == [str(current_src)]
    finally:
        backlog.close()


# ------------------------------------------------------- immediate queue ss


def test_full_immediate_queue_leaves_a_durable_todo_the_backlog_drains(tmp_path):
    src_a, out_a, fp_a, token_a = _publish(tmp_path, "first.mkv")
    src_b, out_b, fp_b, token_b = _publish(tmp_path, "second.mkv")
    src_c, out_c, fp_c, token_c = _publish(tmp_path, "third.mkv")
    started, release = Event(), Event()

    def first(path, item_id):
        started.set()
        assert release.wait(5)
        return True

    server = FakePlex("plex-a", [first, True, True])
    holder = ServerHolder({"plex-a": server})
    backlog = _start(tmp_path, holder, immediate_grace=0.05)
    queue = PlexRefreshQueue(capacity=1, backlog=backlog)
    try:
        assert queue.enqueue(
            server, str(src_a), "1", output_paths=(out_a,), notification_token=token_a, source_fingerprint=fp_a
        )
        assert started.wait(2)
        assert queue.enqueue(
            server, str(src_b), "2", output_paths=(out_b,), notification_token=token_b, source_fingerprint=fp_b
        )
        # The single pending slot is now taken; a third distinct notification
        # is rejected from memory but must remain durably schedulable.
        assert not queue.enqueue(
            server, str(src_c), "3", output_paths=(out_c,), notification_token=token_c, source_fingerprint=fp_c
        )
        assert backlog.store.get(token_c) is not None
        release.set()
        _wait_until(lambda: not backlog.store.active())
        assert [call[1] for call in server.calls] == ["1", "2", "3"]
        for output, source, fp in [
            (out_a, src_a, fp_a),
            (out_b, src_b, fp_b),
            (out_c, src_c, fp_c),
        ]:
            assert get_plex_refresh_pending([output], str(source), "plex-a", source_fingerprint=fp) is None
    finally:
        queue.close()
        backlog.close()


def test_wait_barrier_observes_acceptance_reached_by_the_backlog(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path)
    proceed = Event()

    def delayed(path, item_id):
        proceed.wait(5)
        return True

    server = SimpleNamespace(id="plex-a", name="PLEX-A", refresh_preview_metadata=delayed)
    holder = ServerHolder({"plex-a": server})
    queue = PlexRefreshQueue()
    backlog = _start(tmp_path, holder, immediate=queue, immediate_grace=0.0)
    queue.attach_backlog(backlog)
    try:
        _add(backlog, source, output, fingerprint, token)
        result: dict = {}

        def waiter():
            result["ok"] = queue.wait("plex-a", str(source), required=True, timeout=3.0)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)
        assert thread.is_alive()
        proceed.set()
        thread.join(3)
        assert result == {"ok": True}
    finally:
        queue.close()
        backlog.close()


def test_wait_barrier_times_out_while_backlog_remains_uncertain(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path)
    holder = ServerHolder({"plex-a": FakePlex("plex-a", [ConnectionError("offline")])})
    backlog = _start(tmp_path, holder, immediate_grace=0.0)
    queue = PlexRefreshQueue(backlog=backlog)
    try:
        _add(backlog, source, output, fingerprint, token)
        assert queue.wait("plex-a", str(source), required=True, timeout=0.1) is False
        assert backlog.store.get(token) is not None
    finally:
        queue.close()
        backlog.close()


def test_tokenless_legacy_notification_is_not_persisted(tmp_path):
    source = tmp_path / "legacy.mkv"
    source.write_bytes(b"legacy")
    server = FakePlex("plex-a", [True])
    holder = ServerHolder({"plex-a": server})
    backlog = _start(tmp_path, holder)
    queue = PlexRefreshQueue(backlog=backlog)
    try:
        assert queue.enqueue(server, str(source), "9")
        _wait_until(lambda: len(server.calls) == 1)
        assert backlog.store.active() == []
    finally:
        queue.close()
        backlog.close()


# --------------------------------------------------------- per-server pacing


def test_one_server_backoff_does_not_block_another_server(tmp_path):
    src_a1, out_a1, fp_a1, token_a1 = _publish(tmp_path, "a1.mkv", server_id="plex-a")
    src_a2, out_a2, fp_a2, token_a2 = _publish(tmp_path, "a2.mkv", server_id="plex-a")
    src_b, out_b, fp_b, token_b = _publish(tmp_path, "b1.mkv", server_id="plex-b")
    server_a = FakePlex("plex-a", [True, True])
    server_b = FakePlex("plex-b", [True])
    holder = ServerHolder({"plex-a": server_a, "plex-b": server_b})
    backlog = _start(tmp_path, holder, immediate_grace=0.0, min_server_interval=0.3)
    try:
        for source, output, fp, token, server_id, item_id in [
            (src_a1, out_a1, fp_a1, token_a1, "plex-a", "1"),
            (src_a2, out_a2, fp_a2, token_a2, "plex-a", "2"),
            (src_b, out_b, fp_b, token_b, "plex-b", "3"),
        ]:
            _add(backlog, source, output, fp, token, server_id=server_id, item_id=item_id)
        _wait_until(lambda: len(server_b.calls) == 1, timeout=5.0)
        # B is not kept waiting for A's pacing gate.
        b_at = next(idx for idx, call in enumerate(server_b.calls) if call == (str(src_b), "3"))
        assert b_at == 0
        _wait_until(lambda: len(server_a.calls) == 2, timeout=5.0)
        first_a, second_a = server_a.calls
        assert first_a == (str(src_a1), "1")
        assert second_a == (str(src_a2), "2")
        assert not backlog.store.active()
    finally:
        backlog.close()


# -------------------------------------------------------------- store/json ss


def test_store_roundtrip_and_recovers_from_unreadable_file(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path)
    path = tmp_path / "plex_refresh_pending.json"
    first = RefreshBacklogStore(path)
    first.upsert(
        server_id="plex-a",
        canonical_path=str(source),
        item_id="42",
        output_paths=(str(output),),
        token=token,
        source_fingerprint=fingerprint,
        grace_seconds=0.0,
    )
    assert first.disk_persisted

    reloaded = RefreshBacklogStore(path)
    todo = reloaded.get(token)
    assert todo is not None
    assert (todo.server_id, todo.canonical_path, todo.output_paths) == ("plex-a", str(source), (str(output),))

    path.write_text("{not json")
    damaged = RefreshBacklogStore(path)
    assert damaged.active() == []
    assert damaged.disk_persisted is False
    damaged.upsert(
        server_id="plex-a",
        canonical_path=str(source),
        item_id="42",
        output_paths=(str(output),),
        token=token,
        source_fingerprint=fingerprint,
        grace_seconds=0.0,
    )
    assert damaged.disk_persisted is True
    assert RefreshBacklogStore(path).get(token) is not None


# ------------------------------------------------------------ journal helper


def test_classify_marker_states(tmp_path):
    source, output, fingerprint, token = _publish(tmp_path, "classified.mkv")
    paths = [output]
    assert (
        classify_plex_refresh_marker(paths, str(source), "plex-a", token, source_fingerprint=fingerprint)
        == MARKER_CURRENT
    )

    newer = mark_plex_refresh_pending(paths, str(source), "plex-a", source_fingerprint=fingerprint)
    assert (
        classify_plex_refresh_marker(paths, str(source), "plex-a", token, source_fingerprint=fingerprint)
        == MARKER_SUPERSEDED
    )

    clear_plex_refresh_pending(paths, str(source), "plex-a", newer, source_fingerprint=fingerprint)
    assert (
        classify_plex_refresh_marker(paths, str(source), "plex-a", newer, source_fingerprint=fingerprint)
        == MARKER_CLEARED
    )

    output.unlink()
    assert (
        classify_plex_refresh_marker(paths, str(source), "plex-a", newer, source_fingerprint=fingerprint)
        == MARKER_OUTPUT_MISSING
    )


def test_legacy_output_without_journal_carries_no_marker(tmp_path):
    source = tmp_path / "nojournal.mkv"
    source.write_bytes(b"x")
    output = tmp_path / "index-sd.bif"
    output.write_bytes(b"y")
    assert mark_plex_refresh_pending([output], str(source), "plex-a") is None
    assert classify_plex_refresh_marker([output], str(source), "plex-a", "anything") == MARKER_CLEARED

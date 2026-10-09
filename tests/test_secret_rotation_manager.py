"""Unit tests for the persistent webhook-secret RotationManager.

Covers round lifecycle (begin/idempotency/chain/grace), secret-free
status serialization, corrupt/divergent state safety, the per-server
worker state machine (converged/rejected/uncertain/membership), and
restart recovery from the persisted state file.
"""

from __future__ import annotations

import json
import stat
import time
from datetime import UTC, datetime, timedelta

import pytest

from media_preview_generator.web import plex_webhook_registration as pwh
from media_preview_generator.web.secret_rotation import (
    ACTIVE_STATES,
    S_CONVERGED,
    S_FAILED_REJECTED,
    S_RESOLVED_DELETED,
    S_RESOLVED_DISABLED,
    S_RESOLVED_NO_WEBHOOK,
    S_UNCERTAIN,
    RotationManager,
)

BASE_A = "http://app:8080/api/webhooks/server/plex-a"
BASE_B = "http://app:8080/api/webhooks/server/plex-b"
BASE_C = "http://app:8080/api/webhooks/server/plex-c"


class FakeSettings:
    def __init__(self, servers=None, secret=""):
        self.data = {"media_servers": servers or [], "webhook_secret": secret}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def plex_entry(server_id, *, token=None, url=None, enabled=True, with_webhook=True):
    output = {"webhook_public_url": url or f"http://app:8080/api/webhooks/server/{server_id}"}
    if not with_webhook:
        output = {}
    return {
        "id": server_id,
        "type": "plex",
        "name": f"Plex {server_id}",
        "enabled": enabled,
        "url": "http://plex:32400",
        "auth": {"token": token if token is not None else f"tok-{server_id}"},
        "output": output,
    }


EMBY_ENTRY = {
    "id": "emby-1",
    "type": "emby",
    "name": "Emby",
    "enabled": True,
    "auth": {"api_key": "k"},
    "output": {},
}


class FakePwh:
    """In-memory stand-in for plex_webhook_registration module."""

    RECONCILE_CONVERGED = pwh.RECONCILE_CONVERGED
    RECONCILE_NEW_CONFIRMED = pwh.RECONCILE_NEW_CONFIRMED
    RECONCILE_REJECTED = pwh.RECONCILE_REJECTED
    RECONCILE_UNCERTAIN = pwh.RECONCILE_UNCERTAIN
    RECONCILE_NOT_CONFIGURED = pwh.RECONCILE_NOT_CONFIGURED
    ReconcileResult = pwh.ReconcileResult

    def __init__(self, behavior=None):
        # token -> "converged" | "rejected" | "uncertain" | callable(result_factory)
        self.behavior = behavior or {}
        self.calls = []  # (token, public_url, wanted_url, stale_secrets)
        self._real = pwh

    def is_deterministic_rejection(self, exc):
        return self._real.is_deterministic_rejection(exc)

    def build_authenticated_url(self, public_url, secret):
        return self._real.build_authenticated_url(public_url, secret)

    def reconcile_webhook(self, token, public_url, wanted, stale_secrets=None):
        self.calls.append((token, public_url, wanted, list(stale_secrets or [])))
        behaviour = self.behavior.get(token, "converged")
        if callable(behaviour):
            return behaviour(token, public_url, wanted, stale_secrets)
        if behaviour == "converged":
            return self.ReconcileResult(outcome=self.RECONCILE_CONVERGED, new_confirmed=True, old_removed=True)
        if behaviour == "rejected":
            return self.ReconcileResult(
                outcome=self.RECONCILE_REJECTED,
                reason="unauthorized",
                message="plex said no",
            )
        if behaviour == "new_then_converged":
            self.behavior[token] = "converged"
            return self.ReconcileResult(
                outcome=self.RECONCILE_NEW_CONFIRMED,
                new_confirmed=True,
                reason=None,
            )
        return self.ReconcileResult(
            outcome=self.RECONCILE_UNCERTAIN,
            reason="network_error",
            message="timeout",
        )


@pytest.fixture(autouse=True)
def _short_env(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_ROTATION_GRACE_HOURS", "24")
    yield


def make_manager(tmp_path, settings, fake_pwh, *, spawn_worker=False):
    manager = RotationManager(str(tmp_path), settings_manager=settings, pwh=fake_pwh)
    if not spawn_worker:
        manager._ensure_worker = lambda: None  # drive passes deterministically
    return manager


def _server_row(manager, server_id):
    return manager._state["servers"][server_id]


# ---------------------------------------------------------------------------
# Task 2: lifecycle, persistence, auth window
# ---------------------------------------------------------------------------


def test_begin_persists_generation_servers_and_restrictive_perms(tmp_path):
    settings = FakeSettings(
        servers=[
            plex_entry("plex-a"),
            plex_entry("plex-b"),
            plex_entry("plex-off", enabled=False),
            EMBY_ENTRY,
        ],
        secret="new",
    )
    manager = make_manager(tmp_path, settings, FakePwh())

    status = manager.begin_rotation("old", "new")

    assert status["active"] is True
    assert status["generation"] == 1
    assert manager._state["legacy_secrets"] == ["old"]
    # Only ENABLED Plex servers participate.
    assert sorted(status["servers"], key=lambda s: s["server_id"]) and set(
        s["server_id"] for s in status["servers"]
    ) == {"plex-a", "plex-b"}
    assert all(s["state"] == "new_pending" for s in status["servers"])

    path = tmp_path / "webhook_secret_rotation.json"
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"rotation state must be 0600, got {oct(mode)}"


def test_server_without_target_seeds_terminal(tmp_path):
    settings = FakeSettings(
        servers=[plex_entry("plex-a"), plex_entry("plex-no-url", with_webhook=False)],
        secret="new",
    )
    manager = make_manager(tmp_path, settings, FakePwh())
    manager.begin_rotation("old", "new")
    assert _server_row(manager, "plex-no-url")["state"] == S_RESOLVED_NO_WEBHOOK


def test_begin_same_or_empty_secret_is_noop(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="same")
    fake = FakePwh()
    manager = make_manager(tmp_path, settings, fake)

    assert manager.begin_rotation("same", "same") is None
    assert manager.begin_rotation("same", "  ") is None
    assert manager.begin_rotation("same", "") is None
    assert not (tmp_path / "webhook_secret_rotation.json").exists()
    assert fake.calls == []


def test_duplicate_begin_same_new_keeps_generation(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new")
    manager = make_manager(tmp_path, settings, FakePwh())
    first = manager.begin_rotation("old", "new")
    second = manager.begin_rotation("old", "new")
    assert second["generation"] == first["generation"] == 1
    assert manager._state["legacy_secrets"] == ["old"]


def test_chained_rotation_carries_legacy_secrets(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="mid")
    manager = make_manager(tmp_path, settings, FakePwh())
    manager.begin_rotation("old", "mid")
    assert manager.accepts_secret("old")
    assert manager.accepts_secret("mid")

    settings.set("webhook_secret", "new")
    manager.begin_rotation("mid", "new")
    assert manager._state["generation"] == 2
    assert manager._state["legacy_secrets"] == ["old", "mid"]
    assert manager.accepts_secret("old")
    assert manager.accepts_secret("mid")
    assert manager.accepts_secret("new")
    assert manager.accepts_secret("intruder") is False
    # Original grace window is preserved (bounded chain, not extended).
    first = manager.public_status()
    assert first["generation"] == 2


def test_grace_expiry_revokes_legacy_but_keeps_current(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new")
    manager = make_manager(tmp_path, settings, FakePwh())
    manager.begin_rotation("old", "new")

    manager._state["grace_until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert manager.accepts_secret("old") is False
    assert manager.accepts_secret("new") is True
    status = manager.public_status()
    assert status["grace_expired"] is True
    assert status["old_secret_valid"] is False


def test_public_status_never_contains_secrets(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new-secret-value")
    manager = make_manager(tmp_path, settings, FakePwh())
    manager.begin_rotation("old-secret-value", "new-secret-value")
    raw = json.dumps(manager.public_status())
    assert "old-secret-value" not in raw
    assert "new-secret-value" not in raw
    server = manager.public_status()["servers"][0]
    for field in (
        "server_id",
        "generation",
        "state",
        "new_url_confirmed",
        "old_removed",
        "failure_reason",
        "attempts",
    ):
        assert field in server


def test_corrupt_state_is_quarantined_and_does_not_widen_auth(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new")
    state_file = tmp_path / "webhook_secret_rotation.json"
    state_file.write_text("{not json")
    manager = RotationManager(str(tmp_path), settings_manager=settings, pwh=FakePwh())
    assert manager.is_active() is False
    assert manager.accepts_secret("anything-old") is False
    assert not state_file.exists()
    assert (tmp_path / "webhook_secret_rotation.json.corrupt").exists()


def test_divergent_settings_abort_round(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new")
    manager = make_manager(tmp_path, settings, FakePwh())
    manager.begin_rotation("old", "new")

    # settings.json restored to an unrelated third secret.
    settings.set("webhook_secret", "third")
    manager.run_once()
    status = manager.public_status()
    assert status["active"] is False
    assert status["aborted"] is True
    assert manager.accepts_secret("old") is False
    assert manager.accepts_secret("new") is False
    assert manager.accepts_secret("third") is True  # current always wins


# ---------------------------------------------------------------------------
# Task 3: worker state machine
# ---------------------------------------------------------------------------


def test_all_servers_converge_revokes_old_secret(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a"), plex_entry("plex-b")], secret="new")
    fake = FakePwh(behavior={"tok-plex-a": "converged", "tok-plex-b": "converged"})
    manager = make_manager(tmp_path, settings, fake)
    manager.begin_rotation("old", "new")

    assert manager.old_secret_still_valid() is True
    manager.run_once()

    assert manager.is_active() is False
    assert not (tmp_path / "webhook_secret_rotation.json").exists()
    assert manager.accepts_secret("old") is False
    assert manager.accepts_secret("new") is True
    # Both servers got a reconcile pass carrying the NEW secret target.
    wanted_tokens = {pwh.url_token(call[2]) for call in fake.calls}
    assert wanted_tokens == {"new"}
    assert {call[0] for call in fake.calls} == {"tok-plex-a", "tok-plex-b"}


def test_rejected_server_preserves_old_and_keeps_round_open(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a"), plex_entry("plex-b")], secret="new")
    fake = FakePwh(behavior={"tok-plex-a": "converged", "tok-plex-b": "rejected"})
    manager = make_manager(tmp_path, settings, fake)
    manager.begin_rotation("old", "new")
    manager.run_once()

    status = manager.public_status()
    assert status["active"] is True
    rows = {s["server_id"]: s for s in status["servers"]}
    assert rows["plex-a"]["state"] == S_CONVERGED
    assert rows["plex-b"]["state"] == S_FAILED_REJECTED
    assert rows["plex-b"]["failure_reason"] == "unauthorized"
    assert rows["plex-b"]["next_attempt_at"] is not None  # backoff persisted
    assert manager.old_secret_still_valid() is True

    # Admin fixes the token / Plex Pass — retry now re-attempts immediately.
    fake.behavior["tok-plex-b"] = "converged"
    assert manager.request_retry() == 1
    manager.run_once()
    assert manager.is_active() is False


def test_uncertain_server_retried_via_remote_truth(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-b")], secret="new")
    fake = FakePwh(behavior={"tok-plex-b": "uncertain"})
    manager = make_manager(tmp_path, settings, fake)
    manager.begin_rotation("old", "new")
    manager.run_once()
    assert _server_row(manager, "plex-b")["state"] == S_UNCERTAIN

    fake.behavior["tok-plex-b"] = "converged"
    manager.request_retry()
    manager.run_once()
    assert manager.is_active() is False


def test_backoff_holds_server_until_due(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new")
    fake = FakePwh(behavior={"tok-plex-a": "rejected"})
    manager = make_manager(tmp_path, settings, fake)
    manager.begin_rotation("old", "new")
    manager.run_once()
    attempts_after_first = len(fake.calls)

    # A scheduled (non-forced) pass must respect next_attempt_at.
    manager.run_once(force=False)
    assert len(fake.calls) == attempts_after_first


def test_membership_added_removed_disabled_servers(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new")
    # Keep A pending so the round stays open while membership changes.
    fake = FakePwh(behavior={"tok-plex-a": "uncertain", "tok-plex-b": "uncertain"})
    manager = make_manager(tmp_path, settings, fake)
    manager.begin_rotation("old", "new")

    # New enabled Plex joins mid-round.
    settings.data["media_servers"].append(plex_entry("plex-b"))
    manager.run_once()
    assert "plex-b" in manager._state["servers"]

    # Disabled out-of-band → deterministic terminal.
    settings.data["media_servers"] = [
        dict(s, enabled=False) if s["id"] == "plex-b" else s for s in settings.data["media_servers"]
    ]
    manager.run_once()
    assert _server_row(manager, "plex-b")["state"] == S_RESOLVED_DISABLED

    # Re-enable → rejoins the live round.
    settings.data["media_servers"] = [
        dict(s, enabled=True) if s["id"] == "plex-b" else s for s in settings.data["media_servers"]
    ]
    assert manager.reenable_server("plex-b") is True
    assert _server_row(manager, "plex-b")["state"] in ACTIVE_STATES

    # Deleted out-of-band → resolved_deleted and possible completion.
    settings.data["media_servers"] = [s for s in settings.data["media_servers"] if s["id"] != "plex-a"]
    manager.run_once()
    assert _server_row(manager, "plex-a")["state"] == S_RESOLVED_DELETED


def test_resolve_server_can_complete_round(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a"), plex_entry("plex-b")], secret="new")
    manager = make_manager(
        tmp_path,
        settings,
        FakePwh(behavior={"tok-plex-a": "converged", "tok-plex-b": "uncertain"}),
    )
    manager.begin_rotation("old", "new")
    manager.run_once()  # A converges, B stuck pending
    completed = manager.resolve_server("plex-b", S_RESOLVED_DELETED)
    assert completed is True
    assert manager.is_active() is False


def test_restart_resumes_same_generation_and_converges(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a"), plex_entry("plex-b")], secret="new")
    fake1 = FakePwh(behavior={"tok-plex-a": "converged", "tok-plex-b": "uncertain"})
    manager1 = make_manager(tmp_path, settings, fake1)
    manager1.begin_rotation("old", "new")
    manager1.run_once()
    assert _server_row(manager1, "plex-a")["state"] == S_CONVERGED
    generation = manager1._state["generation"]
    manager1.shutdown()

    # Simulate process restart: brand-new manager over the same config dir.
    fake2 = FakePwh(behavior={"tok-plex-a": "converged", "tok-plex-b": "converged"})
    manager2 = RotationManager(str(tmp_path), settings_manager=settings, pwh=fake2)
    manager2._ensure_worker = lambda: None
    manager2.resume_on_startup()
    assert manager2.is_active() is True
    assert manager2._state["generation"] == generation
    assert manager2.old_secret_still_valid() is True

    manager2.run_once()
    assert manager2.is_active() is False
    # The already-converged server is NOT retried after restart — only B.
    assert {call[0] for call in fake2.calls} == {"tok-plex-b"}


def test_concurrent_worker_pass_and_resolve_stays_consistent(tmp_path):
    import threading

    settings = FakeSettings(servers=[plex_entry("plex-a"), plex_entry("plex-b")], secret="new")
    fake = FakePwh(behavior={"tok-plex-a": "uncertain", "tok-plex-b": "uncertain"})
    manager = make_manager(tmp_path, settings, fake)
    manager.begin_rotation("old", "new")

    worker = threading.Thread(target=manager.run_once, kwargs={"force": True})
    worker.start()
    # Mirror the real PATCH enabled=false endpoint: disable in settings
    # FIRST, then settle the rotation pending.
    for entry in settings.data["media_servers"]:
        if entry["id"] == "plex-a":
            entry["enabled"] = False
    manager.resolve_server("plex-a", S_RESOLVED_DISABLED)
    worker.join(timeout=5)
    assert not worker.is_alive()

    # State must always remain a coherent, persistable document.
    assert manager.is_active() is True
    json.loads(json.dumps(manager._state))
    assert manager._state["servers"]["plex-a"]["state"] == S_RESOLVED_DISABLED

    completed = manager.resolve_server("plex-b", S_RESOLVED_DELETED)
    assert completed is True
    assert manager.is_active() is False


def test_unwritable_state_dir_does_not_widen_auth(tmp_path):
    blocking_file = tmp_path / "config-dir-is-a-file"
    blocking_file.write_text("x")
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new")
    manager = RotationManager(str(blocking_file), settings_manager=settings, pwh=FakePwh())
    with pytest.raises(OSError):
        manager.begin_rotation("old", "new")
    assert manager.is_active() is False
    assert manager.accepts_secret("new") is True
    assert manager.accepts_secret("old") is False


def test_begin_without_any_plex_is_a_noop(tmp_path):
    settings = FakeSettings(servers=[EMBY_ENTRY], secret="new")
    manager = make_manager(tmp_path, settings, FakePwh())
    assert manager.begin_rotation("old", "new") is None
    assert manager.is_active() is False
    assert not (tmp_path / "webhook_secret_rotation.json").exists()
    # Old secret must NOT be held for a pure-Emby/Jellyfin deployment.
    assert manager.accepts_secret("old") is False
    assert manager.accepts_secret("new") is True


def test_all_plex_without_webhook_completes_immediately(tmp_path):
    settings = FakeSettings(
        servers=[plex_entry("plex-no-url", with_webhook=False)],
        secret="new",
    )
    manager = make_manager(tmp_path, settings, FakePwh())
    status = manager.begin_rotation("old", "new")
    assert status == {"active": False, "servers": []}
    assert manager.is_active() is False
    assert not (tmp_path / "webhook_secret_rotation.json").exists()


def test_chained_rotation_passes_full_stale_chain_to_reconcile(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="mid")
    # Reject keeps gen1 open so the worker can't converge it before gen2 begins.
    fake = FakePwh(behavior={"tok-plex-a": "rejected"})
    manager = make_manager(tmp_path, settings, fake)
    manager.begin_rotation("old", "mid")
    settings.set("webhook_secret", "new")
    manager.begin_rotation("mid", "new")

    manager.run_once()
    assert fake.calls, "expected a reconcile call"
    _token, _url, wanted, stale = fake.calls[-1]
    assert stale == ["old", "mid"]
    assert pwh.url_token(wanted) == "new"


def test_post_expiry_convergence_still_deletes_state(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new")
    fake = FakePwh(behavior={"tok-plex-a": "uncertain"})
    manager = make_manager(tmp_path, settings, fake)
    manager.begin_rotation("old", "new")
    manager.run_once()
    assert _server_row(manager, "plex-a")["state"] == S_UNCERTAIN

    manager._state["grace_until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert manager.accepts_secret("old") is False
    assert manager.accepts_secret("new") is True

    # Remote repair continues past expiry and eventually completes.
    fake.behavior["tok-plex-a"] = "converged"
    manager.request_retry()
    manager.run_once()
    assert manager.is_active() is False
    assert not (tmp_path / "webhook_secret_rotation.json").exists()
    assert manager.accepts_secret("new") is True


def test_cleared_current_secret_aborts_round(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new")
    manager = make_manager(tmp_path, settings, FakePwh(behavior={"tok-plex-a": "rejected"}))
    manager.begin_rotation("old", "new")
    assert manager.is_active() is True

    settings.set("webhook_secret", "")
    manager.run_once()
    status = manager.public_status()
    assert status["active"] is False and status["aborted"] is True
    assert manager.accepts_secret("old") is False
    assert manager.accepts_secret("new") is False


def test_state_writes_never_leave_secret_backups(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new")
    manager = make_manager(tmp_path, settings, FakePwh(behavior={"tok-plex-a": "rejected"}))
    manager.begin_rotation("old", "new")
    manager.request_retry()  # another persisted update
    manager.run_once()
    leftovers = list(tmp_path.glob("webhook_secret_rotation.json*"))
    # Only the live state file; no timestamped/.bak copies of the secret.
    assert [p.name for p in leftovers] == ["webhook_secret_rotation.json"]


def test_background_worker_converges_without_manual_pass(tmp_path):
    settings = FakeSettings(servers=[plex_entry("plex-a")], secret="new")
    fake = FakePwh(behavior={"tok-plex-a": "converged"})
    manager = RotationManager(str(tmp_path), settings_manager=settings, pwh=fake)
    manager.begin_rotation("old", "new")  # spawns the real daemon worker
    deadline = time.time() + 5
    while manager.is_active() and time.time() < deadline:
        time.sleep(0.02)
    assert manager.is_active() is False

    # The worker detaches its own reference when it exits so a later
    # begin_rotation() always spawns a successor (F-9 race window).
    detach_deadline = time.time() + 2
    while manager._thread is not None and time.time() < detach_deadline:
        time.sleep(0.01)
    assert manager._thread is None

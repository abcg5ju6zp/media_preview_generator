"""Endpoint-level tests for rotation arbitration and the status API.

Covers manual register/unregister during a live round, server
delete/disable/enable lifecycle arbitration, the GET/POST rotation
status endpoints, and the plex_webhook/status augmentation. Nothing in
any response may carry the old/new secrets.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from media_preview_generator.web import plex_webhook_registration as pwh
from media_preview_generator.web.app import create_app
from media_preview_generator.web.secret_rotation import (
    S_FAILED_REJECTED,
    S_RESOLVED_DELETED,
    S_RESOLVED_DISABLED,
    get_rotation_manager,
    reset_rotation_manager,
)
from media_preview_generator.web.settings_manager import (
    get_settings_manager,
    reset_settings_manager,
)

API_TOKEN = "test-token-12345678"
OLD = "old-secret"
NEW = "new-secret"


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_ROTATION_RETRY_BASE_SECONDS", "3600")
    monkeypatch.setenv("WEBHOOK_SECRET_ROTATION_RETRY_CAP_SECONDS", "3600")
    reset_settings_manager()
    reset_rotation_manager()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import media_preview_generator.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    yield
    reset_rotation_manager()
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
            sched_mod._schedule_manager = None


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_AUTH_TOKEN", API_TOKEN)
    app = create_app(config_dir=str(tmp_path))
    yield app
    get_rotation_manager().shutdown()


def _plex(server_id, *, enabled=True, with_webhook=True):
    output = {"webhook_public_url": f"http://app:8080/api/webhooks/server/{server_id}"} if with_webhook else {}
    return {
        "id": server_id,
        "type": "plex",
        "name": f"Plex {server_id}",
        "enabled": enabled,
        "url": "http://plex:32400",
        "auth": {"token": f"tok-{server_id}"},
        "output": output,
    }


def _seed(servers, secret=OLD):
    get_settings_manager().update({"media_servers": servers, "webhook_secret": secret})


def _reconcile_patch(behavior):
    def _fake(token, public_url, wanted, stale_secrets=None):
        outcome = behavior.get(token, "rejected")
        if outcome == "converged":
            return pwh.ReconcileResult(outcome=pwh.RECONCILE_CONVERGED, new_confirmed=True, old_removed=True)
        return pwh.ReconcileResult(outcome=pwh.RECONCILE_REJECTED, reason="unauthorized")

    return patch.object(pwh, "reconcile_webhook", side_effect=_fake)


def _begin(client, behavior, servers=None):
    if servers is None:
        servers = [_plex("plex-a"), _plex("plex-b")]
    _seed(servers)
    reconcile = _reconcile_patch(behavior)
    reconcile.start()
    response = client.post(
        "/api/settings",
        json={"webhook_secret": NEW},
        headers={"X-Auth-Token": API_TOKEN},
    )
    assert response.status_code == 200
    return reconcile


def _wait_inactive(timeout=8.0):
    manager = get_rotation_manager()
    deadline = time.time() + timeout
    while manager.is_active() and time.time() < deadline:
        time.sleep(0.02)
    return not manager.is_active()


# ---------------------------------------------------------------------------
# Manual register / unregister arbitration
# ---------------------------------------------------------------------------


def test_manual_register_during_rotation_uses_new_secret_without_removing_old(app):
    client = app.test_client()
    with app.app_context():
        reconcile = _begin(client, {"tok-plex-a": "converged", "tok-plex-b": "rejected"})
        with (
            patch.object(pwh, "add_webhook", return_value=(pwh.WRITE_CONFIRMED, [])) as add_mock,
            patch.object(pwh, "register") as legacy_register,
        ):
            response = client.post(
                "/api/settings/plex_webhook/register",
                json={"server_id": "plex-a"},
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.json["rotation"]["status"] == "rotation"

            # The arbitration path adds ONLY the current-secret URL and
            # never invokes the legacy register() that deletes variants.
            assert add_mock.call_count == 1
            assert pwh.url_token(add_mock.call_args.args[1]) == NEW
            assert legacy_register.call_count == 0
        reconcile.stop()


def test_manual_unregister_during_rotation_clears_both_urls_and_settles(app):
    client = app.test_client()
    with app.app_context():
        old_url = pwh.build_authenticated_url("http://app:8080/api/webhooks/server/plex-b", OLD)
        new_url = pwh.build_authenticated_url("http://app:8080/api/webhooks/server/plex-b", NEW)
        reconcile = _begin(client, {"tok-plex-a": "converged", "tok-plex-b": "rejected"})
        with (
            patch.object(pwh, "list_webhooks", return_value=[old_url, new_url]),
            patch.object(pwh, "delete_webhook_url", return_value=(pwh.WRITE_CONFIRMED, [])) as delete_mock,
            patch.object(pwh, "unregister") as legacy_unregister,
        ):
            response = client.post(
                "/api/settings/plex_webhook/unregister",
                json={"server_id": "plex-b"},
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.json["rotation"]["status"] == "rotation"
            removed = {pwh.url_token(call.args[1]) for call in delete_mock.call_args_list}
            assert removed == {OLD, NEW}
            assert legacy_unregister.call_count == 0

        # A was converged and B is now deterministically settled → round done.
        assert get_rotation_manager().is_active() is False
        reconcile.stop()


def test_manual_register_without_rotation_keeps_legacy_path(app):
    client = app.test_client()
    with app.app_context():
        _seed([_plex("plex-a")], secret=NEW)
        with (
            patch.object(pwh, "register") as legacy_register,
            patch.object(pwh, "add_webhook") as add_mock,
        ):
            response = client.post(
                "/api/settings/plex_webhook/register",
                json={"server_id": "plex-a"},
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 200
            assert legacy_register.call_count == 1
            assert add_mock.call_count == 0


# ---------------------------------------------------------------------------
# Server lifecycle arbitration
# ---------------------------------------------------------------------------


def test_delete_server_deterministically_settles_pending(app):
    client = app.test_client()
    with app.app_context():
        reconcile = _begin(client, {"tok-plex-a": "rejected", "tok-plex-b": "rejected"})
        with patch.object(pwh, "unregister"):  # best-effort delete cleanup
            response = client.delete(
                "/api/servers/plex-b",
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 200
            manager = get_rotation_manager()
            assert manager._state["servers"]["plex-b"]["state"] == S_RESOLVED_DELETED
            assert manager.is_active() is True  # plex-a still pending

            client.delete("/api/servers/plex-a", headers={"X-Auth-Token": API_TOKEN})
            assert manager.is_active() is False
        reconcile.stop()


def test_disable_and_reenable_server_membership(app):
    client = app.test_client()
    with app.app_context():
        reconcile = _begin(client, {"tok-plex-a": "rejected", "tok-plex-b": "rejected"})
        response = client.patch(
            "/api/servers/plex-b/enabled",
            json={"enabled": False},
            headers={"X-Auth-Token": API_TOKEN},
        )
        assert response.status_code == 200
        manager = get_rotation_manager()
        assert manager._state["servers"]["plex-b"]["state"] == S_RESOLVED_DISABLED
        assert manager.is_active() is True

        # Re-enable during the round → rejoins the current generation.
        with patch.object(pwh, "register") as register_mock:
            response = client.patch(
                "/api/servers/plex-b/enabled",
                json={"enabled": True},
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 200
            # Active round → rejoin, no legacy best-effort registration.
            assert register_mock.call_count == 0
        row = manager._state["servers"]["plex-b"]
        assert row["state"] not in {S_RESOLVED_DISABLED, S_RESOLVED_DELETED}
        assert row["generation"] == manager._state["generation"]
        reconcile.stop()


# ---------------------------------------------------------------------------
# Status API
# ---------------------------------------------------------------------------


def test_rotation_status_endpoint_shape_and_no_secret_leak(app):
    client = app.test_client()
    with app.app_context():
        reconcile = _begin(client, {"tok-plex-a": "converged", "tok-plex-b": "rejected"})
        # Wait for the background worker to converge plex-a (B stays rejected).
        deadline = time.time() + 5
        while time.time() < deadline:
            polled = client.get(
                "/api/settings/webhook-rotation",
                headers={"X-Auth-Token": API_TOKEN},
            ).json
            rows = {s["server_id"]: s for s in polled["servers"]}
            if rows.get("plex-a", {}).get("state") == "converged":
                break
            time.sleep(0.02)
        response = client.get(
            "/api/settings/webhook-rotation",
            headers={"X-Auth-Token": API_TOKEN},
        )
        assert response.status_code == 200
        raw = response.get_data(as_text=True)
        assert OLD not in raw and NEW not in raw
        data = json.loads(raw)
        assert data["active"] is True
        assert data["generation"] == 1
        assert data["old_secret_valid"] is True
        rows = {s["server_id"]: s for s in data["servers"]}
        assert rows["plex-a"]["state"] == "converged"
        assert rows["plex-b"]["state"] == S_FAILED_REJECTED
        for field in ("generation", "new_url_confirmed", "old_removed", "failure_reason", "attempts"):
            assert field in rows["plex-b"]
        reconcile.stop()


def test_rotation_status_idle_and_retry_action(app):
    client = app.test_client()
    with app.app_context():
        idle = client.get("/api/settings/webhook-rotation", headers={"X-Auth-Token": API_TOKEN})
        assert idle.status_code == 200
        assert idle.json == {"active": False, "servers": []}

        reconcile = _begin(client, {"tok-plex-a": "converged", "tok-plex-b": "rejected"})
        # Flip B healthy, kick via the API action, and wait for completion.
        reconcile.stop()
        behavior = {"tok-plex-a": "converged", "tok-plex-b": "converged"}
        with _reconcile_patch(behavior):
            kicked = client.post(
                "/api/settings/webhook-rotation/retry",
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert kicked.status_code == 200
            assert kicked.json["retried"] >= 1
            assert _wait_inactive()


def test_new_plex_server_joins_active_round_via_create_endpoint(app):
    client = app.test_client()
    with app.app_context():
        reconcile = _begin(client, {"tok-plex-a": "rejected"}, servers=[_plex("plex-a")])
        try:
            response = client.post(
                "/api/servers",
                json=_plex("plex-c"),
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 201, response.get_data(as_text=True)

            deadline = time.time() + 5
            seen = False
            while time.time() < deadline:
                data = client.get(
                    "/api/settings/webhook-rotation",
                    headers={"X-Auth-Token": API_TOKEN},
                ).json
                ids = [s["server_id"] for s in data["servers"]]
                if "plex-c" in ids:
                    seen = True
                    row = next(s for s in data["servers"] if s["server_id"] == "plex-c")
                    assert row["generation"] == data["generation"]
                    break
                time.sleep(0.02)
            assert seen, "new enabled Plex must join the in-flight rotation"
        finally:
            reconcile.stop()


def test_aborted_round_is_visible_in_status_without_leaking_secrets(app):
    client = app.test_client()
    with app.app_context():
        reconcile = _begin(client, {"tok-plex-a": "rejected"}, servers=[_plex("plex-a")])
        try:
            get_settings_manager().set("webhook_secret", "third-secret")
            get_rotation_manager().run_once()

            response = client.get(
                "/api/settings/webhook-rotation",
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 200
            raw = response.get_data(as_text=True)
            assert OLD not in raw and NEW not in raw and "third-secret" not in raw
            data = response.json
            assert data["active"] is False
            assert data["aborted"] is True
            assert data["abort_reason"]
        finally:
            reconcile.stop()


def test_settings_page_contains_rotation_panel_and_script(app):
    client = app.test_client()
    with app.app_context():
        _seed([_plex("plex-a")], secret=NEW)
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    page = client.get("/automation")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'id="secretRotationPanel"' in html
    assert "/static/js/secret_rotation_panel.js" in html
    assert 'id="secretRotationRetryBtn"' in html

    script = client.get("/static/js/secret_rotation_panel.js")
    assert script.status_code == 200
    js = script.get_data(as_text=True)
    assert "/api/settings/webhook-rotation" in js
    assert "/api/settings/webhook-rotation/retry" in js


def test_plex_webhook_status_carries_rotation_snippet(app):
    client = app.test_client()
    with app.app_context():
        reconcile = _begin(client, {"tok-plex-a": "rejected"})
        with (
            patch.object(pwh, "has_plex_pass", return_value=True),
            patch.object(pwh, "is_registered", return_value=False),
        ):
            response = client.get(
                "/api/settings/plex_webhook/status?server_id=plex-a",
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 200
            raw = response.get_data(as_text=True)
            assert OLD not in raw and NEW not in raw
            rotation = response.json["rotation"]
            assert rotation["active"] is True
            assert rotation["generation"] == 1
            assert "state" in rotation
        reconcile.stop()

"""Webhook secret rotation — recoverable multi-server protocol tests.

Replaces the old "best-effort per-server re-register" contract with
the rotation-orchestration contract:

* saving a DIFFERENT secret opens a persisted rotation round where the
  old secret keeps authenticating inbound webhooks during the bounded
  grace window;
* every enabled Plex server converges independently and the old secret
  is revoked only once all of them are terminal (or grace expires);
* saving the SAME secret (or the masked ``****`` sentinel) is a no-op
  with zero plex.tv traffic;
* the very first secret gets one best-effort registration pass;
* non-Plex servers and servers without a token/public URL never block
  the round;
* deterministic rejection keeps the old registration; uncertain
  responses are resolved from the remote list (covered at the pwh layer
  in test_plex_webhook_reconcile.py and at the manager layer in
  test_secret_rotation_manager.py).
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from media_preview_generator.web import plex_webhook_registration as pwh
from media_preview_generator.web.app import create_app
from media_preview_generator.web.secret_rotation import (
    get_rotation_manager,
    reset_rotation_manager,
)
from media_preview_generator.web.settings_manager import (
    get_settings_manager,
    reset_settings_manager,
)

API_TOKEN = "test-token-12345678"


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    # Suppress background retry noise if a thread outlives a test's patch.
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


def _plex_entry(server_id, *, token=None, public_url=None, enabled=True, with_webhook=True):
    output = {"webhook_public_url": public_url or f"http://app:8080/api/webhooks/server/{server_id}"}
    if not with_webhook:
        output = {}
    return {
        "id": server_id,
        "type": "plex",
        "name": f"Plex {server_id}",
        "enabled": enabled,
        "url": "http://plex:32400",
        "auth": {"token": token or f"tok-{server_id}"},
        "output": output,
    }


def _emby_entry(server_id):
    return {
        "id": server_id,
        "type": "emby",
        "name": f"Emby {server_id}",
        "enabled": True,
        "url": "http://emby:8096",
        "auth": {"api_key": "emby-key"},
        "output": {"webhook_public_url": "http://app:8080/api/emby"},
    }


def _seed(servers, secret):
    get_settings_manager().update({"media_servers": servers, "webhook_secret": secret})


def _reconcile_stub(behavior):
    """Build a reconcile_webhook stub keyed by Plex token."""

    def _fake(token, public_url, wanted, stale_secrets=None):
        outcome = behavior.get(token, "converged")
        if outcome == "rejected":
            return pwh.ReconcileResult(
                outcome=pwh.RECONCILE_REJECTED,
                reason="unauthorized",
                message="plex said no",
            )
        if outcome == "uncertain":
            return pwh.ReconcileResult(outcome=pwh.RECONCILE_UNCERTAIN, reason="network_error")
        return pwh.ReconcileResult(
            outcome=pwh.RECONCILE_CONVERGED, new_confirmed=True, old_removed=True
        )

    return patch.object(pwh, "reconcile_webhook", side_effect=_fake)


def _wait_for_inactive(timeout=8.0):
    manager = get_rotation_manager()
    deadline = time.time() + timeout
    while manager.is_active() and time.time() < deadline:
        time.sleep(0.02)
    assert not manager.is_active(), "rotation did not converge in time"


def _post_test_webhook(client, secret, *, header=False):
    if header:
        return client.post(
            "/api/webhooks/server/plex-a",
            json={"eventType": "test"},
            headers={"X-Auth-Token": secret},
        )
    return client.post(f"/api/webhooks/server/plex-a?token={secret}", json={"eventType": "test"})


# ---------------------------------------------------------------------------
# Rotation lifecycle through the real settings endpoint
# ---------------------------------------------------------------------------


def test_post_new_secret_opens_rotation_and_both_secrets_auth(app):
    client = app.test_client()
    with app.app_context():
        _seed([_plex_entry("plex-a"), _plex_entry("plex-b")], "old-secret")
        behavior = {"tok-plex-a": "rejected", "tok-plex-b": "rejected"}
        with _reconcile_stub(behavior):
            response = client.post(
                "/api/settings",
                json={"webhook_secret": "new-secret"},
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 200

            manager = get_rotation_manager()
            assert manager.is_active() is True
            assert manager._state["legacy_secrets"] == ["old-secret"]
            assert get_settings_manager().get("webhook_secret") == "new-secret"

            # The webhook router loads the full processing config (which
            # probes FFmpeg) when matching servers; stub it for this env.
            with patch(
                "media_preview_generator.config.load_config",
                side_effect=RuntimeError("ffmpeg unavailable in unit tests"),
            ):
                # While the round is open BOTH secrets authenticate, in query
                # and header positions; a bogus token is still rejected.
                assert _post_test_webhook(client, "old-secret").status_code == 202
                assert _post_test_webhook(client, "new-secret").status_code == 202
                assert _post_test_webhook(client, "old-secret", header=True).status_code == 202
                assert _post_test_webhook(client, "intruder").status_code == 401

            # Servers converge → old secret revoked, new keeps working.
            behavior["tok-plex-a"] = "converged"
            behavior["tok-plex-b"] = "converged"
            manager.request_retry()
            _wait_for_inactive()

            with patch(
                "media_preview_generator.config.load_config",
                side_effect=RuntimeError("ffmpeg unavailable in unit tests"),
            ):
                assert _post_test_webhook(client, "old-secret").status_code == 401
                assert _post_test_webhook(client, "new-secret").status_code == 202


def test_same_secret_save_is_a_full_noop(app):
    client = app.test_client()
    with app.app_context():
        _seed([_plex_entry("plex-a")], "same-secret")
        with _reconcile_stub({}) as reconcile_patch:
            response = client.post(
                "/api/settings",
                json={"webhook_secret": "  same-secret  "},
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 200
            assert get_rotation_manager().is_active() is False
            assert reconcile_patch.call_count == 0


def test_masked_sentinel_save_is_noop(app):
    client = app.test_client()
    with app.app_context():
        _seed([_plex_entry("plex-a")], "kept-secret")
        with _reconcile_stub({}) as reconcile_patch:
            response = client.post(
                "/api/settings",
                json={"webhook_secret": "****"},
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 200
            assert get_settings_manager().get("webhook_secret") == "kept-secret"
            assert reconcile_patch.call_count == 0


def test_first_secret_runs_best_effort_registration(app):
    client = app.test_client()
    with app.app_context():
        _seed([_plex_entry("plex-a")], "")
        with patch.object(pwh, "register") as mock_register:
            response = client.post(
                "/api/settings",
                json={"webhook_secret": "brand-new"},
                headers={"X-Auth-Token": API_TOKEN},
            )
            assert response.status_code == 200
            assert get_rotation_manager().is_active() is False
            assert mock_register.call_count == 1
            assert mock_register.call_args.kwargs.get("auth_token") == "brand-new"


def test_non_plex_and_unconfigured_plex_servers_do_not_block(app):
    client = app.test_client()
    with app.app_context():
        _seed(
            [
                _emby_entry("emby-1"),
                _plex_entry("plex-a"),
                _plex_entry("plex-no-url", with_webhook=False),
                _plex_entry("plex-off", enabled=False),
            ],
            "old",
        )
        with _reconcile_stub({"tok-plex-a": "converged"}):
            client.post(
                "/api/settings",
                json={"webhook_secret": "new"},
                headers={"X-Auth-Token": API_TOKEN},
            )
            _wait_for_inactive()

        status = get_rotation_manager().public_status()
        assert status["active"] is False  # the one real Plex converged, rest never joined


def test_settings_get_still_masks_secret(app):
    client = app.test_client()
    with app.app_context():
        _seed([], "super-secret")
        response = client.get("/api/settings", headers={"X-Auth-Token": API_TOKEN})
        assert response.status_code == 200
        assert response.json["webhook_secret"] == "****"
        assert "super-secret" not in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# First-secret helper (direct unit level)
# ---------------------------------------------------------------------------


def test_first_secret_helper_is_per_server_best_effort(app):
    from media_preview_generator.web.routes.api_settings import _reregister_plex_webhooks_first_secret

    with app.app_context():
        _seed(
            [
                _plex_entry("plex-a"),
                _plex_entry("plex-b", token="tok-b", public_url="http://app:8080/api/webhooks/server/plex-b"),
                _emby_entry("emby-1"),
            ],
            "rotated",
        )
        with (
            patch.object(pwh, "register") as mock_register,
            patch(
                "media_preview_generator.web.routes.api_plex_webhook._plex_webhook_auth_token",
                return_value="rotated",
            ),
        ):
            _reregister_plex_webhooks_first_secret(get_settings_manager())

        assert mock_register.call_count == 2
        for call in mock_register.call_args_list:
            assert call.kwargs.get("auth_token") == "rotated"

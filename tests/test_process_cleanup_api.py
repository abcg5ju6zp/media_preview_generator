"""Process-cleanup API, notification card and reconciliation semantics."""

from __future__ import annotations

import json
import os

import pytest

from media_preview_generator.processing import process_claims as pc
from media_preview_generator.processing.process_claims import TODO_CLEANUP_FAILED, TODO_KILL_FAILED
from media_preview_generator.web.app import create_app
from media_preview_generator.web.notifications import (
    PROCESS_CLEANUP_ID,
    build_active_notifications,
    reset_session,
)


@pytest.fixture()
def app_with_config(tmp_path):
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "auth.json"), "w") as fh:
        json.dump({"token": "test-token-12345678"}, fh)
    with open(os.path.join(config_dir, "settings.json"), "w") as fh:
        json.dump({"setup_complete": True}, fh)

    env = {
        "CONFIG_DIR": config_dir,
        "WEB_AUTH_TOKEN": "test-token-12345678",
        "WEB_PORT": "8099",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    pc.reset_claims_for_tests()
    reset_session()
    try:
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app, config_dir
    finally:
        import media_preview_generator.web.scheduler as sched_mod

        with sched_mod._schedule_lock:
            if sched_mod._schedule_manager is not None:
                try:
                    sched_mod._schedule_manager.stop()
                except Exception:
                    pass
                sched_mod._schedule_manager = None
        pc.reset_claims_for_tests()
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture()
def client(app_with_config):
    return app_with_config[0].test_client()


@pytest.fixture()
def auth_headers():
    return {"Authorization": "Bearer test-token-12345678"}


class TestProcessCleanupAPI:
    def test_empty_state(self, client, auth_headers):
        resp = client.get("/api/system/process-cleanup", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["todos"] == [] and data["active_claims"] == []

    def test_lists_open_todos_without_tokens(self, client, auth_headers):
        store = pc.get_claims_store()
        store.add_todo(
            TODO_KILL_FAILED,
            job_id="job-1",
            worker={"name": "GPU 1"},
            pid=4242,
            claim_id="claim-4242",
            paths={"output_folder": "/tmp/frames-x"},
            detail="stuck",
        )

        resp = client.get("/api/system/process-cleanup", headers=auth_headers)
        data = resp.get_json()
        assert len(data["todos"]) == 1
        todo = data["todos"][0]
        assert todo["kind"] == TODO_KILL_FAILED and todo["pid"] == 4242
        assert "token" not in json.dumps(todo) and "argv" not in json.dumps(todo)

    def test_requires_authentication(self, client):
        assert client.get("/api/system/process-cleanup").status_code == 401
        assert client.post("/api/system/process-cleanup/reconcile").status_code == 401

    def test_resolve_todo(self, client, auth_headers):
        store = pc.get_claims_store()
        todo = store.add_todo(TODO_KILL_FAILED, pid=99, claim_id="claim-99", detail="x")
        resp = client.post(f"/api/system/process-cleanup/{todo['id']}/resolve", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "resolved"
        assert store.list_todos("open") == []
        assert (
            client.post("/api/system/process-cleanup/does-not-exist/resolve", headers=auth_headers).status_code == 404
        )

    def test_reconcile_resolves_cleanup_todo_once_path_gone(self, client, auth_headers, tmp_path):
        store = pc.get_claims_store()
        leftover = tmp_path / "frames-gone"
        leftover.mkdir()
        store.add_todo(TODO_CLEANUP_FAILED, paths={"path": str(leftover)}, detail="boom")

        # Path still present: recheck leaves the TODO open.
        before = client.post("/api/system/process-cleanup/reconcile", headers=auth_headers).get_json()
        assert before["after"] == 1

        leftover.rmdir()
        after = client.post("/api/system/process-cleanup/reconcile", headers=auth_headers).get_json()
        assert after["before"] == 1 and after["after"] == 0 and len(after["resolved"]) == 1
        assert client.get("/api/system/process-cleanup", headers=auth_headers).get_json()["todos"] == []


class TestShutdownHookRegistration:
    def test_atexit_hook_registers_once_even_repeated_calls(self, tmp_path, monkeypatch):
        import atexit
        import sys
        import types

        import media_preview_generator.web.app as app_mod
        from media_preview_generator.processing import process_claims as pc2

        monkeypatch.setattr(app_mod, "_process_claim_hook_registered", False)
        # Pretend gunicorn owns signal handling: atexit must still register exactly once.
        monkeypatch.setitem(sys.modules, "gunicorn", types.ModuleType("gunicorn"))
        registered: list[object] = []
        monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))
        configured: list[str] = []
        monkeypatch.setattr(pc2, "configure_claims", lambda d: configured.append(d))

        config_dir = str(tmp_path / "config")
        for _ in range(3):
            app_mod._register_process_claim_shutdown(config_dir)

        assert registered == [pc2.shutdown_all_claims]  # one registration, never stacked
        assert app_mod._process_claim_hook_registered is True
        assert configured == [config_dir] * 3  # store configured every call, hooks only once


class TestProcessCleanupNotification:
    def test_card_present_when_todos_open(self, app_with_config):
        reset_session()
        pc.get_claims_store().add_todo(TODO_KILL_FAILED, pid=7, claim_id="c7", detail="stuck")
        cards = build_active_notifications()
        card = next(n for n in cards if n["id"] == PROCESS_CLEANUP_ID)
        assert card["severity"] == "warning"
        assert card["permanent_dismissable"] is False
        assert "/logs#process-cleanup" in card["body_html"]

    def test_card_absent_when_clear(self, app_with_config):
        reset_session()
        assert all(n["id"] != PROCESS_CLEANUP_ID for n in build_active_notifications())

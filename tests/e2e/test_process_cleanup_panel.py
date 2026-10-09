"""E2E: residual-process cleanup TODO surfaces in the bell and on /logs, and Dismiss clears it."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from ._mocks import _fulfill_json


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _seed_cleanup_todo(app_config_dir: Path) -> str:
    """Drop a kill_failed TODO into the running app's claim store; return its id."""
    todo_id = uuid.uuid4().hex
    todos_dir = app_config_dir / "process_claims" / "todos"
    todos_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": todo_id,
        "version": 1,
        "kind": "kill_failed",
        "status": "open",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "occurrences": 1,
        "job_id": "e2e-job",
        "worker": {"name": "GPU 1"},
        "pid": 4242,
        "claim_id": "e2e-claim",
        "paths": {"output_folder": "/tmp/frames-e2e", "stderr_log": "", "ephemeral_output": True},
        "detail": "stuck for the panel test",
        "resolution": "",
        "resolved_at": None,
    }
    (todos_dir / f"{todo_id}.json").write_text(json.dumps(record))
    return todo_id


@pytest.mark.e2e
class TestProcessCleanupPanel:
    def test_bell_links_to_logs_panel_and_dismiss_clears_item(self, authed_page: Page, app_url: str, tmp_path_factory):
        config_dir = tmp_path_factory.getbasetemp() / "config_main0"
        assert config_dir.is_dir(), f"expected the session e2e app config dir at {config_dir}"
        _seed_cleanup_todo(config_dir)

        # Keep the noisy log-stream endpoints quiet without stubbing the cleanup API.
        authed_page.route(
            "**/api/logs**",
            lambda r: _fulfill_json(r, {"logs": [], "files": []}),
        )
        authed_page.goto(f"{app_url}/logs")
        authed_page.wait_for_load_state("domcontentloaded")

        card = authed_page.locator("#processCleanupCard")
        expect(card).to_be_visible()
        expect(card).to_contain_text("Process cleanup")
        expect(card).to_contain_text("pid 4242")
        expect(card).to_contain_text("e2e-job")

        # The bell notification for the same condition is reachable from the navbar.
        authed_page.locator("#notificationBellBtn").click()
        expect(authed_page.locator("#notificationList")).to_contain_text("Residual frame-extraction process")

        # Dismiss → the panel reloads empty and hides; the API reflects the resolution.
        card.get_by_role("button", name="Dismiss").click()
        expect(card).to_be_hidden(timeout=5000)

        resp = authed_page.request.get(f"{app_url}/api/system/process-cleanup")
        assert resp.ok
        assert resp.json()["todos"] == []

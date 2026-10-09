"""Tests for the rotation-safe Plex webhook reconciliation primitives.

Pins the strict ordering the secret-rotation state machine depends on:
list → add (only when missing) → list-confirm new → delete old →
list-confirm old gone, plus the rejected/uncertain failure split and
full idempotency. These primitives live alongside the legacy
register/unregister helpers in
``media_preview_generator/web/plex_webhook_registration.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from media_preview_generator.web import plex_webhook_registration as pwh

BASE = "http://app:8080/api/webhooks/server/plex-a"
OLD_SECRET = "old-secret"
NEW_URL = pwh.build_authenticated_url(BASE, "new-secret")
OLD_URL = pwh.build_authenticated_url(BASE, OLD_SECRET)
LEGACY_OLD_URL = pwh.build_authenticated_url("http://app:8080/api/webhooks/plex", "old-secret")
OTHER_HOST_OLD_URL = pwh.build_authenticated_url("http://other.example/api/webhooks/server/x", "old-secret")


class FakePlexError(Exception):
    """Stand-in for a transport-level exception plexapi would raise."""


class FakeAccount:
    """Scriptable MyPlexAccount stand-in recording every plex.tv call."""

    def __init__(self, urls, *, list_error=None, add_error=None, delete_error=None):
        self.urls = list(urls)
        self._list_error = list_error
        self._add_error = add_error
        self._delete_error = delete_error
        self.calls: list[tuple[str, str]] = []

    def webhooks(self):
        self.calls.append(("list", ""))
        if self._list_error is not None:
            raise self._list_error
        return list(self.urls)

    def addWebhook(self, url):
        self.calls.append(("add", url))
        if self._add_error is not None:
            raise self._add_error
        if url not in self.urls:
            self.urls.append(url)

    def deleteWebhook(self, url):
        self.calls.append(("delete", url))
        if self._delete_error is not None:
            raise self._delete_error
        if url in self.urls:
            self.urls.remove(url)


def _run(account):
    with patch.object(pwh, "_account", return_value=account):
        return pwh.reconcile_webhook("plex-tok", BASE, NEW_URL, stale_secrets=["old-secret"])


def _ops(account):
    return [op for op, _ in account.calls]


def test_happy_path_list_add_confirm_delete_confirm():
    account = FakeAccount([OLD_URL])
    result = _run(account)

    assert result.outcome == pwh.RECONCILE_CONVERGED
    assert result.new_confirmed is True
    assert result.old_removed is True
    # Exact ordered call sequence: list, add(new), list confirm,
    # delete(old), list confirm. No delete may precede new confirmation.
    assert account.calls == [
        ("list", ""),
        ("add", NEW_URL),
        ("list", ""),
        ("delete", OLD_URL),
        ("list", ""),
    ]


def test_new_already_present_skips_add_and_removes_old():
    account = FakeAccount([OLD_URL, NEW_URL])
    result = _run(account)

    assert result.converged
    assert "add" not in _ops(account)
    assert account.calls == [("list", ""), ("delete", OLD_URL), ("list", "")]


def test_already_converged_is_one_list_no_writes():
    account = FakeAccount([NEW_URL])
    result = _run(account)

    assert result.converged
    assert account.calls == [("list", "")]


def test_second_reconcile_after_convergence_makes_no_writes():
    account = FakeAccount([OLD_URL])
    first = _run(account)
    assert first.converged

    account.calls.clear()
    second = _run(account)
    assert second.converged
    assert account.calls == [("list", "")], "converged server must not add/delete twice"


def test_deterministic_rejection_keeps_old_registration():
    from plexapi.exceptions import Unauthorized

    account = FakeAccount([OLD_URL], add_error=Unauthorized("(401) unauthorized"))
    result = _run(account)

    assert result.outcome == pwh.RECONCILE_REJECTED
    assert result.new_confirmed is False
    assert result.old_removed is False
    assert "delete" not in _ops(account), "old URL must be preserved when add is rejected"


def test_uncertain_add_then_remote_shows_new_skips_second_add():
    # First attempt: add times out — did it land? Unknown.
    account = FakeAccount([OLD_URL], add_error=FakePlexError("connection reset"))
    first = _run(account)
    assert first.outcome == pwh.RECONCILE_UNCERTAIN
    assert "delete" not in _ops(account)

    # Next pass starts with a list; the add had actually landed remotely.
    account._add_error = None
    account.urls = [OLD_URL, NEW_URL]
    account.calls.clear()
    second = _run(account)
    assert second.converged
    # List-then-delete: no blind second add.
    assert account.calls[0] == ("list", "")
    assert "add" not in _ops(account)


def test_uncertain_add_then_remote_missing_new_readds():
    account = FakeAccount([OLD_URL], add_error=FakePlexError("read timeout"))
    first = _run(account)
    assert first.outcome == pwh.RECONCILE_UNCERTAIN

    # The timed-out add never landed remotely: list shows only old URL,
    # so the add is retried before any delete.
    account._add_error = None
    account.calls.clear()
    second = _run(account)
    assert second.converged
    assert account.calls[0] == ("list", "")
    assert ("add", NEW_URL) in account.calls
    assert account.calls.index(("add", NEW_URL)) < _ops(account).index("delete")


def test_list_failure_is_uncertain_and_mutates_nothing():
    account = FakeAccount([OLD_URL], list_error=FakePlexError("dns failure"))
    result = _run(account)

    assert result.outcome == pwh.RECONCILE_UNCERTAIN
    assert _ops(account) == ["list"]


def test_legacy_and_other_host_urls_scope():
    # Legacy /api/webhooks/plex variant carrying the old secret on the
    # SAME host is removed; an unrelated other-host entry is left alone.
    account = FakeAccount([LEGACY_OLD_URL, OTHER_HOST_OLD_URL, NEW_URL])
    result = _run(account)

    assert result.converged
    deleted = [url for op, url in account.calls if op == "delete"]
    assert deleted == [LEGACY_OLD_URL]
    assert OTHER_HOST_OLD_URL in account.urls


def test_add_webhook_idempotent_helper():
    account = FakeAccount([NEW_URL])
    with patch.object(pwh, "_account", return_value=account):
        outcome, _urls = pwh.add_webhook("plex-tok", NEW_URL)
    assert outcome == pwh.WRITE_CONFIRMED
    assert "add" not in _ops(account)


def test_delete_missing_url_is_confirmed_noop():
    account = FakeAccount([NEW_URL])
    with patch.object(pwh, "_account", return_value=account):
        outcome, _urls = pwh.delete_webhook_url("plex-tok", OLD_URL)
    assert outcome == pwh.WRITE_CONFIRMED
    assert "delete" not in _ops(account)


def test_uncertain_delete_reverified_via_relist():
    # Delete reports a transport error but actually succeeds; the next
    # reconcile lists and sees the old URL already gone — no second delete.
    account = FakeAccount([OLD_URL, NEW_URL], delete_error=FakePlexError("timeout"))
    first = _run(account)
    assert first.outcome == pwh.RECONCILE_UNCERTAIN
    assert first.new_confirmed is True

    account._delete_error = None
    account.urls = [NEW_URL]  # remote truth: the timed-out delete did land
    account.calls.clear()
    second = _run(account)
    assert second.converged
    assert "delete" not in _ops(account), "confirmed-gone old URL must not be deleted twice"


def test_not_configured_short_circuit():
    result = pwh.reconcile_webhook("", BASE, NEW_URL, stale_secrets=["old-secret"])
    assert result.outcome == pwh.RECONCILE_NOT_CONFIGURED


class _RaisingAccount:
    """Account stand-in whose webhooks()/addWebhook() always raise."""

    def __init__(self, list_exc=None, add_exc=None, urls=None):
        self._list_exc = list_exc
        self._add_exc = add_exc
        self._urls = urls if urls is not None else []
        self.delete_calls = []

    def webhooks(self):
        if self._list_exc is not None:
            raise self._list_exc
        return list(self._urls)

    def addWebhook(self, url):
        if self._add_exc is not None:
            raise self._add_exc
        self._urls.append(url)

    def deleteWebhook(self, url):
        self.delete_calls.append(url)


def test_list_unauthorized_is_rejected_not_uncertain():
    from plexapi.exceptions import Unauthorized

    account = _RaisingAccount(list_exc=Unauthorized("(401) unauthorized"))
    with patch.object(pwh, "_account", return_value=account):
        result = pwh.reconcile_webhook("tok", BASE, NEW_URL, stale_secrets=[OLD_SECRET])
    assert result.outcome == pwh.RECONCILE_REJECTED
    assert result.reason in pwh.DETERMINISTIC_REJECT_REASONS
    assert account.delete_calls == []


def test_list_plex_pass_403_is_rejected():
    account = _RaisingAccount(list_exc=RuntimeError("(403) Forbidden"))
    with patch.object(pwh, "_account", return_value=account):
        result = pwh.reconcile_webhook("tok", BASE, NEW_URL, stale_secrets=[OLD_SECRET])
    assert result.outcome == pwh.RECONCILE_REJECTED
    assert result.reason == "plex_pass_required"


def test_list_network_error_is_uncertain():
    account = _RaisingAccount(list_exc=ConnectionError("dns failure"))
    with patch.object(pwh, "_account", return_value=account):
        result = pwh.reconcile_webhook("tok", BASE, NEW_URL, stale_secrets=[OLD_SECRET])
    assert result.outcome == pwh.RECONCILE_UNCERTAIN
    assert result.reason == "network_error"


def test_add_unauthorized_propagates_machine_reason():
    from plexapi.exceptions import Unauthorized

    account = _RaisingAccount(add_exc=Unauthorized("(401) bad token"), urls=[OLD_URL])
    with patch.object(pwh, "_account", return_value=account):
        result = pwh.reconcile_webhook("tok", BASE, NEW_URL, stale_secrets=[OLD_SECRET])
    assert result.outcome == pwh.RECONCILE_REJECTED
    assert result.reason == "unauthorized"
    assert account.delete_calls == [], "rejected add must never delete the old URL"


@pytest.mark.parametrize(
    ("where", "message"),
    [
        ("list", "(503) Service Unavailable"),
        ("list", "(429) Too Many Requests"),
        ("add", "(503) Service Unavailable"),
        ("add", "(429) Too Many Requests"),
    ],
)
def test_transient_http_statuses_are_uncertain(where, message):
    account = _RaisingAccount(
        list_exc=RuntimeError(message) if where == "list" else None,
        add_exc=RuntimeError(message) if where == "add" else None,
        urls=[OLD_URL],
    )
    with patch.object(pwh, "_account", return_value=account):
        result = pwh.reconcile_webhook("tok", BASE, NEW_URL, stale_secrets=[OLD_SECRET])
    assert result.outcome == pwh.RECONCILE_UNCERTAIN
    assert result.reason == "network_error"
    assert account.delete_calls == []


def test_explicit_400_on_add_is_deterministic_rejection():
    account = _RaisingAccount(add_exc=RuntimeError("(400) Bad Request"), urls=[OLD_URL])
    with patch.object(pwh, "_account", return_value=account):
        result = pwh.reconcile_webhook("tok", BASE, NEW_URL, stale_secrets=[OLD_SECRET])
    assert result.outcome == pwh.RECONCILE_REJECTED
    assert result.reason == "bad_request"
    assert account.delete_calls == []


@pytest.mark.parametrize(
    "urls,expected",
    [
        ([NEW_URL, OLD_URL], [OLD_URL]),
        ([NEW_URL], []),
        (["http://x/?token=other"], []),
        ([], []),
    ],
)
def test_urls_carrying_secret(urls, expected):
    assert pwh.urls_carrying_secret(urls, "old-secret") == expected

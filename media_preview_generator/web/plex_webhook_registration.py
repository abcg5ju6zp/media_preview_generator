"""Programmatic registration of Plex Pass webhooks via plex.tv account API.

Wraps :class:`plexapi.myplex.MyPlexAccount` so the app can register and
unregister its own ``/api/webhooks/incoming`` endpoint with Plex without
the user having to copy-paste the URL into the Plex Web Settings page.
Old installs that registered the legacy ``/api/webhooks/plex`` URL keep
working — the inbound endpoint stays mounted — but ``register()`` will
quietly remove that variant the next time it runs so the account ends up
on the canonical unified URL.

The plex.tv webhook endpoint requires a Plex Pass subscription on the
server-owner account.  All helpers raise :class:`PlexWebhookError` with a
human-readable message when registration fails for any reason
(missing token, no Plex Pass, network error, etc.).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from loguru import logger

# plexapi surfaces HTTP statuses inside exception messages, e.g.
# BadRequest("(503) Service Unavailable ..."). Parse them so a 5xx/429
# is treated as "uncertain — re-verify remotely", not a hard rejection.
_STATUS_RE = re.compile(r"\((\d{3})\)")


def _http_status(exc: BaseException) -> int | None:
    match = _STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


# Machine-readable outcome codes carried by :class:`ReconcileResult`.
RECONCILE_CONVERGED = "converged"
RECONCILE_NEW_CONFIRMED = "new_confirmed"
RECONCILE_REJECTED = "rejected"
RECONCILE_UNCERTAIN = "uncertain"
RECONCILE_NOT_CONFIGURED = "not_configured"

# Outcome codes for a single write attempt.
WRITE_CONFIRMED = "confirmed"
WRITE_REJECTED = "rejected"
WRITE_UNCERTAIN = "uncertain"


class PlexWebhookError(Exception):
    """Raised when a Plex webhook registration call fails.

    The :attr:`reason` attribute carries a short, machine-readable code
    so the API layer can drive UI behaviour without re-parsing the
    message string:

    * ``"missing_token"`` — no Plex token configured
    * ``"missing_url"`` — caller passed an empty webhook URL
    * ``"plex_pass_required"`` — account does not have Plex Pass
    * ``"unauthorized"`` — token rejected by plex.tv
    * ``"bad_request"`` — plex.tv returned a deterministic 4xx rejection
    * ``"network_error"`` — connection / TLS / timeout / unconfirmable response
    * ``"unknown"`` — anything else
    """

    def __init__(self, message: str, reason: str = "unknown") -> None:
        super().__init__(message)
        self.reason = reason


# Reasons that mean "plex.tv decisively said no" — the caller must keep
# the previous registration intact instead of mutating anything else.
DETERMINISTIC_REJECT_REASONS = frozenset(
    {"missing_token", "missing_url", "plex_pass_required", "unauthorized", "bad_request"}
)
# Reasons that mean "we do not know what happened remotely" — the only
# safe next step is re-listing the remote webhooks before any mutation.
UNCERTAIN_REASONS = frozenset({"network_error"})


def is_deterministic_rejection(exc: BaseException) -> bool:
    """True when ``exc`` is a classified deterministic plex.tv rejection."""
    return isinstance(exc, PlexWebhookError) and exc.reason in DETERMINISTIC_REJECT_REASONS


def is_uncertain_failure(exc: BaseException) -> bool:
    """True when ``exc`` leaves the remote state unknown (timeout/network/5xx)."""
    return isinstance(exc, PlexWebhookError) and exc.reason in UNCERTAIN_REASONS


def _account(token: str):
    """Build a :class:`MyPlexAccount` from a Plex token.

    Raises:
        PlexWebhookError: When the token is missing/invalid or plex.tv
            cannot be reached.
    """
    if not token or not str(token).strip():
        raise PlexWebhookError("Plex token is not configured", reason="missing_token")

    try:
        from plexapi.exceptions import BadRequest, Unauthorized
        from plexapi.myplex import MyPlexAccount
    except ImportError as exc:
        raise PlexWebhookError(f"plexapi is not available: {exc}", reason="unknown") from exc

    try:
        return MyPlexAccount(token=str(token).strip())
    except Unauthorized as exc:
        raise PlexWebhookError(
            "Plex token was rejected by plex.tv. Re-authenticate via the Setup Wizard and try again.",
            reason="unauthorized",
        ) from exc
    except BadRequest as exc:
        raise PlexWebhookError(f"plex.tv rejected the account request: {exc}", reason="unknown") from exc
    except Exception as exc:  # network / TLS / timeout
        raise PlexWebhookError(f"Could not reach plex.tv: {exc}", reason="network_error") from exc


def _looks_like_plex_pass_required(exc: BaseException) -> bool:
    """Return True when ``exc`` plausibly indicates a missing Plex Pass.

    plex.tv returns 401/403 for both "no Plex Pass on this account" and
    "token rejected". The plexapi-typed exceptions (Unauthorized,
    BadRequest) reach us deterministically; for transport-level errors
    we fall back to a substring check against the formatted message.
    The substring set is conservative — we'd rather raise the generic
    "unknown" error than mis-classify a quota/bad-payload failure as
    Plex Pass missing.
    """
    try:
        from plexapi.exceptions import BadRequest, Unauthorized

        if isinstance(exc, Unauthorized) or isinstance(exc, BadRequest):
            return True
    except ImportError:
        pass
    message = str(exc).lower()
    # Look for status codes only when bracketed by non-digit context so we
    # don't false-positive on bodies like "Quota exceeded; HTTP 401 will be
    # returned tomorrow" — the plexapi error format is "(401) ...".
    if "(401)" in message or "(403)" in message:
        return True
    return "unauthorized" in message or "forbidden" in message


def _normalize_url(url: str) -> str:
    """Trim whitespace and a trailing slash so URL comparisons are stable."""
    if not url:
        return ""
    return str(url).strip().rstrip("/")


def _base_url(url: str) -> str:
    """Return the URL without its query string or fragment.

    Used for matching the user-facing base URL against URLs Plex has
    stored — those may have a ``?token=...`` query param appended for
    authentication.  Trailing slashes are stripped so the result is
    stable for equality comparisons.
    """
    if not url:
        return ""
    parsed = urlparse(str(url).strip())
    base = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))
    return base.rstrip("/")


def _build_authenticated_url(url: str, auth_token: str | None, *, server_id: str | None = None) -> str:
    """Append ``?token=<auth_token>`` (and optionally ``server_id``) so Plex can authenticate.

    Plex's webhook UI does not allow custom headers, so the only way to
    authenticate POSTs from Plex Media Server is to embed the token in
    the URL. When ``auth_token`` is empty the URL is returned unchanged
    (the endpoint will then 401, which is what we want).

    K6: ``server_id`` (when supplied) is added as a separate query
    parameter so the inbound endpoint can look up that server's per-server
    webhook secret. Multiple Plex servers in the same install can each
    have their own URL token this way.
    """
    base = _normalize_url(url)
    if not base or not auth_token:
        return base
    parsed = urlparse(base)
    existing_query = parsed.query
    pairs = {"token": auth_token}
    if server_id:
        pairs["server_id"] = server_id
    new_query = urlencode(pairs)
    if existing_query:
        # Preserve any pre-existing query, but overwrite an existing token / server_id.
        existing_pairs = [
            kv
            for kv in existing_query.split("&")
            if kv and not kv.startswith("token=") and not kv.startswith("server_id=")
        ]
        existing_pairs.append(new_query)
        combined = "&".join(existing_pairs)
    else:
        combined = new_query
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, combined, ""))


def has_plex_pass(token: str) -> bool:
    """Return ``True`` when the account has an active Plex Pass subscription.

    Returns ``False`` for any failure mode (missing token, network error,
    no subscription) so callers can use this as a quick UI gate without
    needing to handle exceptions.
    """
    try:
        account = _account(token)
    except PlexWebhookError:
        return False

    try:
        sub_active = bool(getattr(account, "subscriptionActive", False))
    except Exception:
        sub_active = False

    if sub_active:
        return True

    # Some plexapi versions expose the flag as ``hasPlexPass``; fall back
    # to that, then to a final probe of the webhooks endpoint, which
    # plex.tv only allows for Plex Pass accounts.
    try:
        if bool(getattr(account, "hasPlexPass", False)):
            return True
    except Exception:
        pass

    try:
        account.webhooks()
        return True
    except Exception:
        return False


def list_webhooks(token: str) -> list[str]:
    """Return all webhook URLs currently registered on the account.

    Raises:
        PlexWebhookError: On any failure (missing token, no Plex Pass,
            network error).
    """
    account = _account(token)
    try:
        urls = account.webhooks()
    except Exception as exc:
        if _looks_like_plex_pass_required(exc):
            raise PlexWebhookError(
                "Webhooks require an active Plex Pass subscription on the server-owner account.",
                reason="plex_pass_required",
            ) from exc
        raise PlexWebhookError(f"Failed to list Plex webhooks: {exc}", reason="unknown") from exc
    return [_normalize_url(u) for u in (urls or []) if u]


def is_registered(token: str, url: str) -> bool:
    """Return ``True`` when a webhook with this base URL is registered.

    Match is performed on the base URL only — query strings (the
    ``?token=...`` we attach for auth) are stripped before comparison so
    the UI status probe works regardless of whether the registered URL
    has the token embedded.

    Returns ``False`` for any failure mode so this can be used as a
    cheap UI status probe.
    """
    target_base = _base_url(url)
    if not target_base:
        return False
    try:
        return any(_base_url(u) == target_base for u in list_webhooks(token))
    except PlexWebhookError:
        return False


def register(token: str, url: str, auth_token: str | None = None, *, server_id: str | None = None) -> list[str]:
    """Register ``url`` as a Plex webhook (idempotent).

    Args:
        token: Plex token (account-level) used to talk to plex.tv.
        url: Fully-qualified webhook URL Plex will POST to.  May be the
            user-facing base URL — the auth token is appended below.
        auth_token: Webhook authentication secret to embed in the
            registered URL as a ``?token=`` query parameter.  Plex's
            webhook UI does not support custom headers, so this is the
            only way for Plex to authenticate against the receiving
            endpoint.
        server_id: K6 — when set, embedded as a ``server_id=`` query
            parameter so the inbound endpoint knows which configured Plex
            sent the event and can validate against that server's
            per-server webhook secret.

    Returns:
        The list of registered webhook URLs after the call.

    Raises:
        PlexWebhookError: On any failure.
    """
    target_base = _base_url(url)
    if not target_base:
        raise PlexWebhookError("Webhook URL is empty", reason="missing_url")

    target_with_auth = _build_authenticated_url(target_base, auth_token, server_id=server_id)

    account = _account(token)

    try:
        current = [_normalize_url(u) for u in (account.webhooks() or []) if u]
    except Exception as exc:
        if _looks_like_plex_pass_required(exc):
            raise PlexWebhookError(
                "Webhooks require an active Plex Pass subscription on the server-owner account.",
                reason="plex_pass_required",
            ) from exc
        raise PlexWebhookError(f"Failed to read existing Plex webhooks: {exc}", reason="unknown") from exc

    # Remove any stale registrations matching the same base URL — they
    # may have a different (or missing) token query param.  This makes
    # "Re-register with Plex" idempotent and safe after rotating the
    # webhook secret.
    #
    # Also remove the legacy ``/api/webhooks/plex`` variant pointing at
    # the same host/scheme so installs upgraded from the per-vendor URL
    # to the unified ``/api/webhooks/incoming`` URL don't end up with
    # both URLs registered (which would double-fire every event).
    target_parsed = urlparse(target_base)
    legacy_target = urlunparse(
        (
            target_parsed.scheme,
            target_parsed.netloc,
            "/api/webhooks/plex",
            "",
            "",
            "",
        )
    ).rstrip("/")
    stale = [
        u for u in current if u != target_with_auth and (_base_url(u) == target_base or _base_url(u) == legacy_target)
    ]
    for stale_url in stale:
        try:
            account.deleteWebhook(stale_url)
            logger.info("Removed stale Plex webhook with old auth: {}", _base_url(stale_url))
        except Exception as exc:
            logger.warning(
                "Could not remove the old Plex webhook entry for base {} on plex.tv ({}). "
                "We'll still register the new one, but Plex will keep firing the old URL too "
                "until you delete it manually at https://app.plex.tv/desktop#!/account → Webhooks.",
                _base_url(stale_url),
                exc,
            )

    if target_with_auth in current and not stale:
        logger.debug("Plex webhook already registered: {}", target_base)
        return current

    try:
        account.addWebhook(target_with_auth)
    except Exception as exc:
        if _looks_like_plex_pass_required(exc):
            raise PlexWebhookError(
                "Webhooks require an active Plex Pass subscription on the server-owner account.",
                reason="plex_pass_required",
            ) from exc
        raise PlexWebhookError(f"Failed to register Plex webhook: {exc}", reason="unknown") from exc

    logger.info("Registered Plex webhook (token embedded in URL): {}", target_base)
    try:
        return [_normalize_url(u) for u in (account.webhooks() or []) if u]
    except Exception:
        return current + [target_with_auth]


def unregister(token: str, url: str) -> list[str]:
    """Remove the webhook for ``url`` from the account.

    Matches by base URL so that any registered variants — with or
    without the ``?token=...`` query param — are all removed in a
    single call.

    Args:
        token: Plex authentication token.
        url: Webhook URL to remove (base URL is fine).

    Returns:
        The list of registered webhook URLs after the call.

    Raises:
        PlexWebhookError: On any failure other than "not registered",
            which is treated as a no-op success.
    """
    target_base = _base_url(url)
    if not target_base:
        raise PlexWebhookError("Webhook URL is empty", reason="missing_url")

    account = _account(token)

    try:
        current = [_normalize_url(u) for u in (account.webhooks() or []) if u]
    except Exception as exc:
        raise PlexWebhookError(f"Failed to read existing Plex webhooks: {exc}", reason="unknown") from exc

    matches = [u for u in current if _base_url(u) == target_base]
    if not matches:
        logger.debug("Plex webhook not registered (no-op): {}", target_base)
        return current

    for match in matches:
        try:
            account.deleteWebhook(match)
        except Exception as exc:
            raise PlexWebhookError(f"Failed to remove Plex webhook: {exc}", reason="unknown") from exc
        # Never log the token-bearing URL itself — base only.
        logger.info("Removed Plex webhook: {}", _base_url(match))

    try:
        return [_normalize_url(u) for u in (account.webhooks() or []) if u]
    except Exception:
        return [u for u in current if _base_url(u) != target_base]


# ---------------------------------------------------------------------------
# Rotation-safe primitives
#
# Unlike ``register()`` — which lists, deletes every same-base variant, then
# adds the wanted URL in one shot — these helpers never combine a removal
# with an addition and always verify the remote list after a write. The
# secret-rotation state machine relies on that ordering: an old URL carrying
# the previous secret may only be deleted AFTER the new URL has been
# positively confirmed in plex.tv's webhook list.
# ---------------------------------------------------------------------------


def build_authenticated_url(public_url: str, secret: str) -> str:
    """Return ``public_url`` with ``?token=<secret>`` appended (public wrapper).

    Thin public wrapper around :func:`_build_authenticated_url` so the
    rotation manager and manual-registration paths share one canonical
    URL shape (per-server path already encodes the server id, so no
    extra ``server_id=`` query is appended).
    """
    return _build_authenticated_url(public_url, secret)


def url_token(url: str) -> str:
    """Extract the ``token`` query value from a registered URL (``""`` if absent)."""
    if not url:
        return ""
    try:
        values = parse_qs(urlparse(url).query).get("token") or []
    except ValueError:
        return ""
    return str(values[0]) if values else ""


def urls_carrying_secret(remote_urls: list[str], secret: str) -> list[str]:
    """Return remote URLs whose ``?token=`` query exactly equals ``secret``."""
    if not secret:
        return []
    return [u for u in remote_urls if url_token(u) == secret]


def _classify_remote_exception(exc: Exception, *, action: str) -> PlexWebhookError:
    """Classify any plexapi/transport error as a deterministic rejection or an uncertain failure.

    Status code (plexapi embeds ``"(NNN)"`` in messages) wins:
    401 → unauthorized, 403 → plex_pass_required, other explicit 4xx →
    bad_request, while 429 and 5xx are *uncertain* transport failures
    (the remote state is unknown; re-list before mutating). Without a
    parseable status the exception type provides the fallback mapping.
    """
    if isinstance(exc, PlexWebhookError):
        return exc
    code = _http_status(exc)
    if code == 401:
        return PlexWebhookError(
            "Plex token was rejected by plex.tv while updating webhook registration. "
            "Re-authenticate the Plex server and retry.",
            reason="unauthorized",
        )
    if code == 403:
        return PlexWebhookError(
            "Webhooks require an active Plex Pass subscription on the server-owner account.",
            reason="plex_pass_required",
        )
    if code is not None:
        if code == 429 or code >= 500:
            return PlexWebhookError(
                f"plex.tv returned a temporary status ({code}) for the webhook {action}; "
                "the remote list will be re-checked before any further change.",
                reason="network_error",
            )
        return PlexWebhookError(f"plex.tv rejected the webhook {action} ({code}): {exc}", reason="bad_request")

    try:
        from plexapi.exceptions import BadRequest, Unauthorized
    except ImportError:
        BadRequest = Unauthorized = ()  # type: ignore[assignment]
    if isinstance(exc, Unauthorized):
        return PlexWebhookError(
            "Plex token was rejected by plex.tv while updating webhook registration. "
            "Re-authenticate the Plex server and retry.",
            reason="unauthorized",
        )
    if isinstance(exc, BadRequest):
        return PlexWebhookError(f"plex.tv rejected the webhook {action}: {exc}", reason="bad_request")
    if _looks_like_plex_pass_required(exc):
        return PlexWebhookError(
            "Webhooks require an active Plex Pass subscription on the server-owner account.",
            reason="plex_pass_required",
        )
    return PlexWebhookError(
        f"Could not confirm the webhook {action} with plex.tv: {exc}",
        reason="network_error",
    )


def _safe_list(account) -> list[str]:
    """List remote webhooks with rotation-friendly error classification."""
    try:
        return [_normalize_url(u) for u in (account.webhooks() or []) if u]
    except Exception as exc:
        raise _classify_remote_exception(exc, action="list") from exc


def _classify_write_exception(exc: Exception, *, action: str) -> PlexWebhookError:
    """Map an add/delete error to rejected vs uncertain (status-aware)."""
    return _classify_remote_exception(exc, action=action)


def _write_with_confirmation(
    account,
    *,
    target_url: str,
    present: bool,
    current: list[str],
) -> tuple[str, list[str], str | None]:
    """Perform one idempotent add or delete and positively confirm it via re-list.

    ``present`` is the desired end state: True → ensure ``target_url``
    exists (add), False → ensure it is gone (delete). ``current`` is the
    just-fetched remote list.

    Returns ``(outcome, remote_urls, reason)`` where outcome is one of
    :data:`WRITE_CONFIRMED` / :data:`WRITE_REJECTED` /
    :data:`WRITE_UNCERTAIN`; ``reason`` carries the classified machine
    code on a rejection (``unauthorized`` / ``bad_request`` /
    ``plex_pass_required`` / …). On WRITE_UNCERTAIN the returned list may
    be stale — callers must not infer truth from it.
    """
    action = "addition" if present else "removal"
    already = target_url in current
    if present and already:
        return WRITE_CONFIRMED, current, None
    if not present and not already:
        return WRITE_CONFIRMED, current, None

    method = account.addWebhook if present else account.deleteWebhook
    try:
        method(target_url)
    except Exception as exc:  # transport/classified errors only
        classified = _classify_write_exception(exc, action=action)
        if is_deterministic_rejection(classified):
            logger.warning(
                "plex.tv deterministically rejected webhook {} for base {} (reason={}); "
                "keeping the previous registration untouched",
                action,
                _base_url(target_url),
                classified.reason,
            )
            return WRITE_REJECTED, current, classified.reason
        logger.warning(
            "Webhook {} for base {} got an uncertain response from plex.tv ({}); "
            "will re-verify the remote list before retrying",
            action,
            _base_url(target_url),
            classified.reason,
        )
        return WRITE_UNCERTAIN, current, classified.reason

    try:
        after = _safe_list(account)
    except PlexWebhookError as exc:
        # The write may have landed but we cannot read it back — unknown.
        return WRITE_UNCERTAIN, current, exc.reason
    landed = target_url in after
    if present and landed:
        logger.info("Confirmed new Plex webhook registered: {}", _base_url(target_url))
        return WRITE_CONFIRMED, after, None
    if not present and not landed:
        logger.info("Confirmed old Plex webhook removed: {}", _base_url(target_url))
        return WRITE_CONFIRMED, after, None
    # 2xx on the write but the following list disagrees — do not guess.
    logger.warning(
        "Webhook {} appeared to succeed but plex.tv's list does not reflect it (base={}); "
        "treating as uncertain pending re-verification",
        action,
        _base_url(target_url),
    )
    return WRITE_UNCERTAIN, after, "write_unconfirmed"


def add_webhook(token: str, url_with_auth: str) -> tuple[str, list[str]]:
    """Ensure one exact webhook URL exists without removing anything else.

    Idempotent: lists first, adds only when the exact URL is missing.
    Returns :data:`WRITE_CONFIRMED` / :data:`WRITE_REJECTED` /
    :data:`WRITE_UNCERTAIN` plus the best-known remote list.
    """
    target = _normalize_url(url_with_auth)
    if not target:
        raise PlexWebhookError("Webhook URL is empty", reason="missing_url")
    account = _account(token)
    current = _safe_list(account)
    outcome, remote, _reason = _write_with_confirmation(account, target_url=target, present=True, current=current)
    return outcome, remote


def delete_webhook_url(token: str, url_with_auth: str) -> tuple[str, list[str]]:
    """Remove one exact webhook URL after listing; missing URL is a confirmed no-op."""
    target = _normalize_url(url_with_auth)
    if not target:
        raise PlexWebhookError("Webhook URL is empty", reason="missing_url")
    account = _account(token)
    current = _safe_list(account)
    outcome, remote, _reason = _write_with_confirmation(account, target_url=target, present=False, current=current)
    return outcome, remote


@dataclass
class ReconcileResult:
    """Structured outcome of one :func:`reconcile_webhook` pass for one server.

    ``remote_urls`` may contain ``?token=`` secrets and is for INTERNAL
    state-machine use only — never log it or return it through APIs.
    """

    outcome: str
    new_confirmed: bool = False
    old_removed: bool = False
    reason: str | None = None
    message: str | None = None
    remote_urls: list[str] = field(default_factory=list)

    @property
    def converged(self) -> bool:
        return self.outcome == RECONCILE_CONVERGED


def _stale_urls_for_target(remote_urls: list[str], stale_secrets: list[str], target_base: str) -> list[str]:
    """Pick remote URLs that still authenticate with a pre-rotation secret.

    Scoped to the wanted base and to this host's legacy/incoming paths so
    unrelated entries on the same plex.tv account (other apps, other
    hosts) are never touched.
    """
    target_parsed = urlparse(target_base)
    legacy_paths = {"/api/webhooks/plex", "/api/webhooks/incoming"}
    stale: list[str] = []
    for url in remote_urls:
        if url_token(url) not in set(stale_secrets or []):
            continue
        parsed = urlparse(_base_url(url))
        if (
            parsed.scheme == target_parsed.scheme
            and parsed.netloc == target_parsed.netloc
            and (_base_url(url) == target_base or parsed.path in legacy_paths)
        ):
            stale.append(url)
    return stale


def reconcile_webhook(
    token: str,
    public_url: str,
    wanted_url: str,
    *,
    stale_secrets: list[str] | None = None,
) -> ReconcileResult:
    """Drive one server's remote webhooks one safe step (or to convergence).

    Ordering is strict and crash-safe because every pass starts with a
    fresh remote list:

    1. List remote webhooks. A list failure is always ``uncertain`` —
       no mutation is allowed on a stale view.
    2. Ensure ``wanted_url`` (carrying the NEW secret) exists; confirm
       via re-list. Deterministic rejection → ``rejected`` and NOTHING
       is deleted. Uncertain write → ``uncertain``.
    3. Only then delete every stale URL carrying one of
       ``stale_secrets`` (same base / legacy paths, same host),
       confirming removal via re-list.

    The function is idempotent: a fully-converged server costs one list
    call and returns :data:`RECONCILE_CONVERGED` with no writes.
    """
    target_base = _base_url(public_url or wanted_url)
    wanted = _normalize_url(wanted_url)
    if not token or not target_base or not wanted:
        return ReconcileResult(
            outcome=RECONCILE_NOT_CONFIGURED,
            reason="not_configured",
            message="Plex token or public webhook URL is not configured for this server.",
        )

    stale_secrets = [s for s in (stale_secrets or []) if s]
    account = _account(token)
    try:
        current = _safe_list(account)
    except PlexWebhookError as exc:
        # A deterministic refusal on the very first list (no Plex Pass,
        # 401/403) must be classified as a rejection — the old
        # registration stays valid — not as "uncertain network".
        if is_deterministic_rejection(exc):
            return ReconcileResult(
                outcome=RECONCILE_REJECTED,
                reason=exc.reason,
                message=str(exc),
                remote_urls=[],
            )
        return ReconcileResult(
            outcome=RECONCILE_UNCERTAIN,
            reason=exc.reason,
            message=str(exc),
            remote_urls=[],
        )

    # Step 1/2: new URL must exist and be positively confirmed first.
    if wanted not in current:
        outcome, current, reason = _write_with_confirmation(account, target_url=wanted, present=True, current=current)
        if outcome == WRITE_REJECTED:
            return ReconcileResult(
                outcome=RECONCILE_REJECTED,
                reason=reason or "rejected",
                message=(
                    "Plex refused to register the new webhook URL (subscription, permissions, or token "
                    f"problem: {reason or 'rejected'}). The previous webhook registration is kept in place "
                    "until this is resolved."
                ),
                remote_urls=current,
            )
        if outcome == WRITE_UNCERTAIN or wanted not in current:
            return ReconcileResult(
                outcome=RECONCILE_UNCERTAIN,
                reason=reason or "network_error",
                message=(
                    "Could not confirm whether plex.tv accepted the new webhook URL. "
                    "The remote list will be re-checked before any further change."
                ),
                remote_urls=current,
            )

    # Step 3: new URL is confirmed — now remove URLs carrying old secrets.
    stale = _stale_urls_for_target(current, stale_secrets, target_base)
    if not stale:
        return ReconcileResult(
            outcome=RECONCILE_CONVERGED,
            new_confirmed=True,
            old_removed=True,
            remote_urls=current,
        )

    saw_uncertain = False
    for stale_url in stale:
        outcome, current, reason = _write_with_confirmation(
            account, target_url=stale_url, present=False, current=current
        )
        if outcome == WRITE_REJECTED:
            return ReconcileResult(
                outcome=RECONCILE_REJECTED,
                new_confirmed=True,
                old_removed=False,
                reason=reason or "remove_rejected",
                message=(
                    "The new webhook URL is registered, but plex.tv refused removal of the old URL "
                    f"({reason or 'rejected'}). The old registration is kept until removal succeeds."
                ),
                remote_urls=current,
            )
        if outcome == WRITE_UNCERTAIN:
            saw_uncertain = True

    # Final verification against a fresh view already taken inside the
    # delete helper; re-derive from the best-known list.
    remaining = _stale_urls_for_target(current, stale_secrets, target_base)
    if remaining:
        return ReconcileResult(
            outcome=RECONCILE_UNCERTAIN,
            new_confirmed=True,
            old_removed=False,
            reason="old_url_still_present",
            message=(
                "The new webhook URL is registered, but an old webhook URL is still listed by plex.tv. "
                "It will be retried and re-verified."
            ),
            remote_urls=current,
        )
    if saw_uncertain:
        return ReconcileResult(
            outcome=RECONCILE_UNCERTAIN,
            new_confirmed=True,
            old_removed=True,
            reason="removal_unconfirmed",
            message="Old webhook URL removal could not be fully confirmed; the remote list will be re-checked.",
            remote_urls=current,
        )
    return ReconcileResult(
        outcome=RECONCILE_CONVERGED,
        new_confirmed=True,
        old_removed=True,
        remote_urls=current,
    )

"""Recoverable multi-server webhook-secret rotation.

When the admin rotates the global ``webhook_secret``, every registered
Plex server keeps POSTing to a URL that embeds the OLD secret until its
plex.tv registration is rewritten. Cutting straight to the new secret
rejects every event from a server that happens to be offline at save
time. This module makes the cutover a *rotation round* instead:

* the new secret becomes current immediately, but every secret in the
  round's legacy chain also authenticates inbound webhooks until a
  bounded grace deadline (24 h by default, ``WEBHOOK_SECRET_ROTATION_GRACE_HOURS``);
* per enabled Plex server, a background worker drives the strict order
  "new URL confirmed remotely → old URL removed remotely", persisting
  the per-server state after every attempt;
* a deterministic plex.tv rejection preserves the old registration and
  records the reason; an uncertain network response is resolved only by
  re-listing the remote webhooks, never by guessing;
* the whole round lives in ``webhook_secret_rotation.json`` (0600) and
  resumes after a process restart;
* the old secret is revoked only once every server has converged (or
  reached a deterministic terminal state), or once the grace window
  closes — in which case remote repair keeps running without accepting
  the old secret any longer.

The module never logs or serializes secret values or token-bearing
URLs; :meth:`RotationManager.public_status` is the only shape allowed
toward the HTTP API.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

STATE_FILENAME = "webhook_secret_rotation.json"
STATE_VERSION = 1

# Per-server lifecycle states.
S_NEW_PENDING = "new_pending"  # new URL not yet confirmed remotely
S_REMOVING_OLD = "removing_old"  # new URL confirmed; old URL(s) still to remove
S_FAILED_REJECTED = "failed_rejected"  # deterministic plex.tv refusal; old kept
S_UNCERTAIN = "uncertain"  # network/ambiguous; verify via remote list next
# Terminal states — a server in one of these blocks nothing.
S_CONVERGED = "converged"
S_RESOLVED_NO_WEBHOOK = "resolved_no_webhook"
S_RESOLVED_UNREGISTERED = "resolved_unregistered"
S_RESOLVED_DISABLED = "resolved_disabled"
S_RESOLVED_DELETED = "resolved_deleted"

ACTIVE_STATES = frozenset({S_NEW_PENDING, S_REMOVING_OLD, S_FAILED_REJECTED, S_UNCERTAIN})
TERMINAL_STATES = frozenset(
    {
        S_CONVERGED,
        S_RESOLVED_NO_WEBHOOK,
        S_RESOLVED_UNREGISTERED,
        S_RESOLVED_DISABLED,
        S_RESOLVED_DELETED,
    }
)
# Deterministic terminal rows that re-join a live round when the server
# re-appears enabled with a usable webhook target.
REACTIVATABLE_TERMINAL = frozenset({S_RESOLVED_DISABLED, S_RESOLVED_NO_WEBHOOK})

DEFAULT_GRACE_HOURS = 24.0
# Persisted retry backoff for failed/uncertain servers.
DEFAULT_RETRY_BASE_SECONDS = 30.0
DEFAULT_RETRY_CAP_SECONDS = 15 * 60.0
_BACKOFF_FACTOR = 2.0


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid {}={!r}; falling back to {}",
            name,
            raw,
            default,
        )
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive {}={!r}; falling back to {}", name, raw, default)
        return default
    return value


@dataclass
class _ServerTarget:
    """Everything a worker attempt needs, snapshotted under the lock."""

    server_id: str
    server_name: str
    plex_token: str
    public_url: str
    generation: int
    state: str
    attempts: int
    new_confirmed: bool


def _new_server_row(server_name: str, generation: int, *, terminal: str | None = None) -> dict[str, Any]:
    now_iso = _iso(_now())
    row: dict[str, Any] = {
        "server_name": server_name,
        "generation": generation,
        "state": terminal or S_NEW_PENDING,
        "new_url_confirmed": False,
        "old_removed": False,
        "failure_reason": None,
        "failure_message": None,
        "attempts": 0,
        "last_attempt_at": None,
        "next_attempt_at": now_iso if terminal is None else None,
        "updated_at": now_iso,
    }
    if terminal is not None:
        row["failure_reason"] = None
    return row


class RotationManager:
    """Owns the persisted rotation round and its reconciliation worker.

    One instance per process, constructed lazily by
    :func:`get_rotation_manager`. All public methods are thread-safe;
    plex.tv I/O happens outside the state lock using immutable server
    snapshots, and results are discarded if the server's generation
    changed while the call was in flight.
    """

    def __init__(
        self,
        config_dir: str | None = None,
        *,
        settings_manager=None,
        pwh=None,
    ) -> None:
        if config_dir is None:
            config_dir = os.environ.get("CONFIG_DIR", "/config")
        self.config_dir = Path(config_dir)
        self.state_file = self.config_dir / STATE_FILENAME
        self._settings = settings_manager
        self._pwh = pwh
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] | None = None
        self.grace_hours = _env_float("WEBHOOK_SECRET_ROTATION_GRACE_HOURS", DEFAULT_GRACE_HOURS)
        self.retry_base = _env_float("WEBHOOK_SECRET_ROTATION_RETRY_BASE_SECONDS", DEFAULT_RETRY_BASE_SECONDS)
        self.retry_cap = _env_float("WEBHOOK_SECRET_ROTATION_RETRY_CAP_SECONDS", DEFAULT_RETRY_CAP_SECONDS)
        self._load()

    # ------------------------------------------------------------------
    # Collborators (lazy so tests can construct the manager standalone)
    # ------------------------------------------------------------------

    @property
    def settings(self):
        if self._settings is not None:
            return self._settings
        from .settings_manager import get_settings_manager

        self._settings = get_settings_manager(str(self.config_dir))
        return self._settings

    @property
    def pwh(self):
        if self._pwh is not None:
            return self._pwh
        from . import plex_webhook_registration as module

        self._pwh = module
        return module

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load the rotation state; a missing file = no active round.

        A corrupt/unreadable file is treated as *no round* (so it can
        never widen webhook auth) and moved aside with a ``.corrupt``
        suffix for forensics.
        """
        if not self.state_file.exists():
            self._state = None
            return
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
                raise ValueError("rotation state shape is invalid")
            self._state = data
        except Exception as exc:
            logger.warning(
                "Could not read webhook rotation state at {} ({}: {}). "
                "Treating it as inactive — the previous secret will NOT be accepted. "
                "The file was moved aside as '.corrupt' so a new rotation can start.",
                self.state_file,
                type(exc).__name__,
                exc,
            )
            self._quarantine_corrupt_file()
            self._state = None

    def _quarantine_corrupt_file(self) -> None:
        try:
            self.state_file.replace(self.state_file.with_suffix(".json.corrupt"))
        except OSError:
            try:
                self.state_file.unlink()
            except OSError:
                pass

    def _save_locked(self) -> None:
        """Persist current state; caller holds the lock.

        Uses the plain atomic writer (no timestamped backups): the state
        carries the old-secret plaintext, so leaving ten rotating backup
        copies next to settings.json would multiply the secret at rest.
        """
        if self._state is None:
            return
        from ..utils import atomic_json_save

        self.config_dir.mkdir(parents=True, exist_ok=True)
        atomic_json_save(str(self.state_file), self._state, permissions=0o600)

    def _remove_state_backups_locked(self) -> None:
        """Best-effort removal of any state/backup/corrupt copies holding secrets."""
        for pattern in (
            STATE_FILENAME + ".bak",
            STATE_FILENAME + ".*.bak",
            STATE_FILENAME + ".corrupt",
        ):
            for path in self.config_dir.glob(pattern):
                try:
                    path.unlink()
                except OSError:
                    pass

    def _clear_state_locked(self) -> None:
        self._state = None
        self._remove_state_backups_locked()
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "Could not remove finished rotation state file {} ({}: {}); "
                "it will be treated as inactive on next start.",
                self.state_file,
                type(exc).__name__,
                exc,
            )

    # ------------------------------------------------------------------
    # Membership helpers
    # ------------------------------------------------------------------

    def _enabled_plex_servers(self) -> list[dict[str, Any]]:
        raw = self.settings.get("media_servers") or []
        out: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                if (entry.get("type") or "").lower() != "plex":
                    continue
                if entry.get("enabled", True) is False:
                    continue
                out.append(entry)
        return out

    @staticmethod
    def _server_plex_token(entry: dict[str, Any]) -> str:
        token = (entry.get("auth") or {}).get("token") or entry.get("token") or ""
        return str(token).strip()

    @staticmethod
    def _server_public_url(entry: dict[str, Any]) -> str:
        return str((entry.get("output") or {}).get("webhook_public_url") or "").strip()

    def _seed_servers_locked(self, generation: int) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for entry in self._enabled_plex_servers():
            server_id = str(entry.get("id") or "")
            if not server_id:
                continue
            name = str(entry.get("name") or server_id)
            has_target = bool(self._server_plex_token(entry) and self._server_public_url(entry))
            rows[server_id] = _new_server_row(name, generation, terminal=None if has_target else S_RESOLVED_NO_WEBHOOK)
        return rows

    def _refresh_membership_locked(self) -> bool:
        """Reconcile server rows against current settings.

        Newly-added enabled Plex servers join the current generation;
        servers that vanished or got disabled deterministically reach a
        terminal state; a re-enabled server rejoins. Returns whether the
        persisted state changed.
        """
        if self._state is None or self._state.get("aborted"):
            return False
        generation = int(self._state.get("generation") or 0)
        enabled = {str(e.get("id") or ""): e for e in self._enabled_plex_servers() if e.get("id")}
        rows = self._state.setdefault("servers", {})
        changed = False

        for server_id, entry in enabled.items():
            name = str(entry.get("name") or server_id)
            existing = rows.get(server_id)
            has_target = bool(self._server_plex_token(entry) and self._server_public_url(entry))
            if existing is None:
                rows[server_id] = _new_server_row(
                    name,
                    generation,
                    terminal=None if has_target else S_RESOLVED_NO_WEBHOOK,
                )
                logger.info("Plex server {} joined secret rotation generation {}", name, generation)
                changed = True
                continue
            if existing.get("state") in ACTIVE_STATES:
                if existing.get("server_name") != name:
                    existing["server_name"] = name
                    existing["updated_at"] = _iso(_now())
                    changed = True
                continue
            # A terminal row for a currently-enabled server reactivates
            # only for the disable→enable / webhook-reconfigured cases.
            # A manual UNREGISTER must stick even while the server stays
            # configured — the explicit register action (manual_register)
            # is the only thing that re-opens that pending.
            if existing.get("state") in REACTIVATABLE_TERMINAL:
                rows[server_id] = _new_server_row(
                    name,
                    generation,
                    terminal=None if has_target else S_RESOLVED_NO_WEBHOOK,
                )
                logger.info("Plex server {} rejoined secret rotation generation {}", name, generation)
                changed = True
            elif existing.get("server_name") != name:
                existing["server_name"] = name
                existing["updated_at"] = _iso(_now())
                changed = True

        for server_id, row in list(rows.items()):
            if server_id in enabled:
                continue
            if row.get("state") in TERMINAL_STATES:
                continue
            # Disabled or deleted out-of-band: deterministically resolve.
            entry = next(
                (
                    e
                    for e in self.settings.get("media_servers") or []
                    if isinstance(e, dict) and e.get("id") == server_id
                ),
                None,
            )
            reason = S_RESOLVED_DISABLED if entry is not None else S_RESOLVED_DELETED
            row["state"] = reason
            row["new_url_confirmed"] = bool(row.get("new_url_confirmed"))
            row["old_removed"] = bool(row.get("old_removed"))
            row["next_attempt_at"] = None
            row["updated_at"] = _iso(_now())
            logger.info(
                "Plex server {} rotation pending resolved ({})",
                row.get("server_name") or server_id,
                reason,
            )
            changed = True
        return changed

    # ------------------------------------------------------------------
    # Round lifecycle
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        with self._lock:
            return self._state is not None and not self._state.get("aborted")

    def _grace_expired_locked(self, at: datetime | None = None) -> bool:
        if self._state is None:
            return True
        deadline = _parse_iso(self._state.get("grace_until"))
        return deadline is not None and (at or _now()) >= deadline

    def begin_rotation(self, old_secret: str, new_secret: str) -> dict[str, Any] | None:
        """Open (or extend) a rotation round.

        No-op when either value is empty/whitespace or both are equal.
        Chains onto an in-flight round by bumping the generation and
        carrying the previous secrets; starts a fresh chain when the
        prior round's grace has already closed. Persists state and
        wakes the worker.
        """
        old_secret = (old_secret or "").strip()
        new_secret = (new_secret or "").strip()
        if not new_secret or new_secret == old_secret:
            return None

        with self._lock:
            now = _now()
            prior = self._state if self._state is not None and not self._state.get("aborted") else None

            if prior is not None and prior.get("new_secret") == new_secret:
                # Same save replayed / duplicate caller — never open a new round.
                logger.debug("Secret rotation for the same new value already exists; ignoring duplicate begin")
                return self.public_status()

            if prior is not None and not self._grace_expired_locked(now):
                generation = int(prior.get("generation") or 0) + 1
                legacy = []
                for value in [*list(prior.get("legacy_secrets") or []), prior.get("new_secret")]:
                    if value and value not in legacy and value != new_secret:
                        legacy.append(value)
                started_at = prior.get("started_at")
                grace_until = prior.get("grace_until")
                logger.info(
                    "Extending secret rotation to generation {} (carrying {} previous secret(s))",
                    generation,
                    len(legacy),
                )
            else:
                generation = int((prior or {}).get("generation") or 0) + 1
                legacy = [old_secret] if old_secret else []
                started_at = _iso(now)
                grace_until = _iso(now + timedelta(hours=self.grace_hours))
                logger.info("Starting webhook secret rotation generation {}", generation)

            new_state = {
                "version": STATE_VERSION,
                "generation": generation,
                "started_at": started_at,
                "grace_until": grace_until,
                "new_secret": new_secret,
                "legacy_secrets": legacy,
                "aborted": False,
                "abort_reason": None,
                "servers": self._seed_servers_locked(generation),
            }
            # Preserve deterministic terminals for servers absent/disabled
            # in the new membership but known to the prior round.
            if prior is not None:
                for server_id, row in (prior.get("servers") or {}).items():
                    if server_id in new_state["servers"]:
                        continue
                    if row.get("state") in {S_RESOLVED_DISABLED, S_RESOLVED_DELETED, S_RESOLVED_UNREGISTERED}:
                        carried = dict(row)
                        carried["generation"] = generation
                        new_state["servers"][server_id] = carried
            # Pure-Emby/Jellyfin (or no enabled Plex at all): there is no
            # remote URL embedding the secret, so nothing rotates and the
            # old secret must not be held in a state file indefinitely.
            if prior is None and not new_state["servers"]:
                logger.info(
                    "No enabled Plex server to rotate; skipping the rotation round "
                    "(non-Plex senders authenticate with the live secret directly)"
                )
                return None
            # Persist BEFORE adopting in memory: a failed save must not
            # leave the process accepting a legacy secret that never made
            # it to disk (the settings save itself already succeeded).
            previous_state = self._state
            self._state = new_state
            try:
                self._save_locked()
            except Exception:
                self._state = previous_state
                raise
            # Every participating Plex was seeded terminal (e.g. none has
            # a webhook configured): complete immediately, no worker.
            if self._maybe_complete_locked():
                return self.public_status()
            self._wake.set()
        # Re-arm outside the lock so a worker detaching in the same
        # scheduling window is observed dead and replaced (see arm_worker).
        self.arm_worker()
        return self.public_status()

    def _abort_locked(self, reason: str) -> None:
        if self._state is None:
            return
        self._state["aborted"] = True
        self._state["abort_reason"] = reason
        self._state["grace_until"] = _iso(_now())
        self._save_locked()
        logger.warning("Webhook secret rotation aborted: {}", reason)

    def _check_settings_consistency_locked(self) -> None:
        """Mark the round aborted when settings diverged to an unknown secret.

        Settings restore/hand-edit can leave a state file whose new and
        legacy secrets are all different from the CURRENT configured
        secret. Honouring the file's old secrets then would widen auth
        against the admin's restored reality, so the round is aborted
        and the configured secret is the only accepted one.
        """
        if self._state is None or self._state.get("aborted"):
            return
        current = (self.settings.get("webhook_secret") or "").strip()
        if not current:
            # The secret was cleared out-of-band: keep status truthful
            # instead of showing an "active" round that accepts nothing.
            self._abort_locked("configured webhook_secret was cleared while a rotation was in progress")
            return
        known = {self._state.get("new_secret"), *list(self._state.get("legacy_secrets") or [])}
        if current not in known:
            self._abort_locked(
                "configured webhook_secret does not match any secret of the recorded rotation "
                "(settings restored or replaced)"
            )

    def accepts_secret(self, token: str) -> bool:
        """Whether ``token`` is a valid webhook secret under the current round.

        The CURRENT configured secret always wins (caller checks it
        separately, but accepting it here too is harmless and keeps the
        predicate self-contained). A legacy secret validates only while
        the round is live, within grace, and the round still targets the
        configured secret.
        """
        if not token:
            return False
        current = (self.settings.get("webhook_secret") or "").strip()
        with self._lock:
            if current and secrets.compare_digest(token, current):
                return True
            if self._state is None or self._state.get("aborted"):
                return False
            if self._state.get("new_secret") != current:
                return False
            if self._grace_expired_locked():
                return False
            for legacy in self._state.get("legacy_secrets") or []:
                if legacy and secrets.compare_digest(token, str(legacy)):
                    return True
            return False

    def old_secret_still_valid(self) -> bool:
        with self._lock:
            if self._state is None or self._state.get("aborted"):
                return False
            if self._state.get("new_secret") != (self.settings.get("webhook_secret") or "").strip():
                return False
            return not self._grace_expired_locked()

    # ------------------------------------------------------------------
    # Server-resolution hooks (manual ops / server lifecycle)
    # ------------------------------------------------------------------

    def resolve_server(self, server_id: str, terminal_state: str) -> bool:
        """Deterministically settle one server's pending to-do.

        Used by unregister / delete / disable paths. Returns True when
        the whole round completed as a result.
        """
        if terminal_state not in TERMINAL_STATES:
            raise ValueError(f"not a terminal rotation state: {terminal_state}")
        with self._lock:
            if self._state is None or self._state.get("aborted"):
                return False
            rows = self._state.setdefault("servers", {})
            row = rows.get(server_id)
            if row is None:
                # Track it even if the round seeded before the server existed.
                row = _new_server_row(server_id, int(self._state.get("generation") or 0))
                rows[server_id] = row
            row["state"] = terminal_state
            # Remote fact flags are NOT fabricated for disable/delete:
            # the state string is the authoritative terminal reason.
            if terminal_state == S_RESOLVED_UNREGISTERED:
                row["new_url_confirmed"] = True
                row["old_removed"] = True
            row["next_attempt_at"] = None
            row["updated_at"] = _iso(_now())
            logger.info("Rotation pending for server {} resolved as {}", server_id, terminal_state)
            completed = self._maybe_complete_locked()
            self._save_locked()
            self._wake.set()
            return completed

    def reenable_server(self, server_id: str) -> bool:
        """Bring a previously-disabled server back into the live round."""
        with self._lock:
            if self._state is None or self._state.get("aborted"):
                return False
            changed = self._refresh_membership_locked()
            if changed:
                self._save_locked()
            self._wake.set()
            row = (self._state.get("servers") or {}).get(server_id)
            rejoined = bool(row and row.get("state") in ACTIVE_STATES)
        if rejoined:
            self.arm_worker()
        return rejoined

    # ------------------------------------------------------------------
    # Manual registration arbitration
    # ------------------------------------------------------------------

    def _enabled_plex_entry_locked(self, server_id: str) -> dict[str, Any] | None:
        for entry in self._enabled_plex_servers():
            if str(entry.get("id") or "") == server_id:
                return entry
        return None

    def manual_register(self, server_id: str) -> dict[str, Any] | None:
        """Arbitrate a manual "Register with Plex" click during a round.

        Ensures ONLY the wanted URL carrying the CURRENT secret exists
        (never removes old URLs — removal belongs to the worker once the
        new URL is confirmed), records the confirmation on the server's
        rotation row and kicks the worker so old-URL removal proceeds.

        Returns ``None`` when no live round concerns this server (the
        caller keeps its legacy register() path). Raises
        :class:`PlexWebhookError` on rejection/uncertain outcome.
        """
        with self._lock:
            if self._state is None or self._state.get("aborted"):
                return None
            entry = self._enabled_plex_entry_locked(server_id)
            if entry is None:
                return None
            token = self._server_plex_token(entry)
            public_url = self._server_public_url(entry)
            new_secret = str(self._state.get("new_secret") or "")
            generation = int(self._state.get("generation") or 0)
        if not token or not public_url or not new_secret:
            return None

        wanted = self.pwh.build_authenticated_url(public_url, new_secret)
        outcome, _remote = self.pwh.add_webhook(token, wanted)
        if outcome == self.pwh.WRITE_REJECTED:
            raise self.pwh.PlexWebhookError(
                "plex.tv rejected the new webhook registration; the previous registration is kept.",
                reason="rejected",
            )
        if outcome == self.pwh.WRITE_UNCERTAIN:
            raise self.pwh.PlexWebhookError(
                "Could not confirm the new webhook registration with plex.tv; it will be re-checked.",
                reason="network_error",
            )

        with self._lock:
            if self._state is None or self._state.get("aborted"):
                return {"status": "rotation", "generation": generation, "state": S_REMOVING_OLD}
            self._refresh_membership_locked()
            rows = self._state.setdefault("servers", {})
            row = rows.get(server_id)
            if row is None:
                row = _new_server_row(str(entry.get("name") or server_id), generation)
                rows[server_id] = row
            # Manual confirmation re-opens a previously-unregistered
            # pending (explicit user action) and advances the state
            # machine; the old URL still has to be removed by the normal
            # worker pass when any stale one is found.
            row["generation"] = generation
            row["new_url_confirmed"] = True
            if row.get("state") != S_CONVERGED:
                row["state"] = S_REMOVING_OLD
                row["next_attempt_at"] = _iso(_now())
                row["old_removed"] = False
                row["failure_reason"] = None
                row["failure_message"] = None
            row["updated_at"] = _iso(_now())
            self._save_locked()
            self._wake.set()
            result = {"status": "rotation", "generation": generation, "state": row["state"]}
        self.arm_worker()
        return result

    def manual_unregister(self, server_id: str) -> dict[str, Any] | None:
        """Arbitrate a manual "Remove from Plex" click during a round.

        Deletes every webhook for this server carrying the current
        secret OR any legacy-chain secret (list-first, idempotent), then
        deterministically settles the server's pending to-do. Returns
        ``None`` when no live round concerns this server.
        """
        with self._lock:
            if self._state is None or self._state.get("aborted"):
                return None
            entry = self._enabled_plex_entry_locked(server_id)
            if entry is None:
                # Allow settling for servers that are still configured
                # but disabled (manual unregister from an edited modal).
                raw = self.settings.get("media_servers") or []
                entry = next(
                    (
                        e
                        for e in raw
                        if isinstance(e, dict)
                        and str(e.get("id") or "") == server_id
                        and (e.get("type") or "").lower() == "plex"
                    ),
                    None,
                )
            if entry is None:
                return None
            token = self._server_plex_token(entry)
            public_url = self._server_public_url(entry)
            secrets_to_remove = [
                str(self._state.get("new_secret") or ""),
                *[str(s) for s in (self._state.get("legacy_secrets") or [])],
            ]
        if not token or not public_url:
            # Nothing to call plex.tv with — settle locally regardless.
            self.resolve_server(server_id, S_RESOLVED_UNREGISTERED)
            return {"status": "rotation", "removed": 0}

        remote = self.pwh.list_webhooks(token)
        targets = self.pwh._stale_urls_for_target(  # noqa: SLF001 — same-package helper
            remote, [s for s in secrets_to_remove if s], self.pwh._base_url(public_url)
        )
        failures = 0
        for target in targets:
            outcome, _remote = self.pwh.delete_webhook_url(token, target)
            if outcome != self.pwh.WRITE_CONFIRMED:
                failures += 1
        if failures:
            raise self.pwh.PlexWebhookError(
                f"{failures} old webhook URL(s) could not be confirmed removed from plex.tv.",
                reason="network_error",
            )
        completed = self.resolve_server(server_id, S_RESOLVED_UNREGISTERED)
        return {"status": "rotation", "removed": len(targets), "completed": completed}

    def kick(self) -> None:
        """Wake the worker for an immediate membership/attempt pass."""
        with self._lock:
            active = self._state is not None and not self._state.get("aborted")
            if active:
                self._wake.set()
        if active:
            self.arm_worker()

    def request_retry(self) -> int:
        """Clear backoff timers on retryable servers; returns their count."""
        with self._lock:
            if self._state is None or self._state.get("aborted"):
                return 0
            now_iso = _iso(_now())
            count = 0
            for row in (self._state.get("servers") or {}).values():
                if row.get("state") in {S_FAILED_REJECTED, S_UNCERTAIN, S_NEW_PENDING, S_REMOVING_OLD}:
                    row["next_attempt_at"] = now_iso
                    count += 1
            if count:
                self._save_locked()
                self._wake.set()
        if count:
            self.arm_worker()
        return count

    def get_server_generation(self, server_id: str) -> dict[str, Any] | None:
        """Non-sensitive per-server rotation snippet for status augmentation."""
        with self._lock:
            if self._state is None:
                return None
            row = (self._state.get("servers") or {}).get(server_id)
            if row is None:
                return None
            return {
                "active": not self._state.get("aborted"),
                "generation": int(self._state.get("generation") or 0),
                "state": row.get("state"),
                "new_url_confirmed": bool(row.get("new_url_confirmed")),
                "old_removed": bool(row.get("old_removed")),
                "failure_reason": row.get("failure_reason"),
            }

    # ------------------------------------------------------------------
    # Public status (secret-free by construction)
    # ------------------------------------------------------------------

    _PUBLIC_SERVER_FIELDS = (
        "server_name",
        "generation",
        "state",
        "new_url_confirmed",
        "old_removed",
        "failure_reason",
        "failure_message",
        "attempts",
        "last_attempt_at",
        "next_attempt_at",
        "updated_at",
    )

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            if self._state is None:
                return {"active": False, "servers": []}
            grace_expired = self._grace_expired_locked()
            aborted = bool(self._state.get("aborted"))
            servers: list[dict[str, Any]] = []
            converged = 0
            for server_id, row in sorted((self._state.get("servers") or {}).items()):
                item = {"server_id": server_id}
                for field_name in self._PUBLIC_SERVER_FIELDS:
                    item[field_name] = row.get(field_name)
                item["terminal"] = row.get("state") in TERMINAL_STATES
                if item["terminal"]:
                    converged += 1
                servers.append(item)
            total = len(servers)
            return {
                "active": not aborted,
                "aborted": aborted,
                "abort_reason": self._state.get("abort_reason"),
                "generation": int(self._state.get("generation") or 0),
                "started_at": self._state.get("started_at"),
                "grace_until": self._state.get("grace_until"),
                "grace_expired": grace_expired,
                # Old secret authenticates only in a live, unexpired round
                # whose new_secret still matches the configured secret.
                "old_secret_valid": self.old_secret_still_valid(),
                "converged_count": converged,
                "server_count": total,
                "servers": servers,
            }

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _backoff_seconds(self, attempts: int) -> float:
        seconds = self.retry_base * (_BACKOFF_FACTOR ** max(0, attempts - 1))
        return min(self.retry_cap, max(1.0, seconds))

    def _due_targets_locked(self, *, force: bool) -> list[_ServerTarget]:
        if self._state is None or self._state.get("aborted"):
            return []
        now = _now()
        generation = int(self._state.get("generation") or 0)
        by_id = {str(e.get("id") or ""): e for e in self._enabled_plex_servers() if e.get("id")}
        targets: list[_ServerTarget] = []
        for server_id, row in (self._state.get("servers") or {}).items():
            if row.get("state") not in ACTIVE_STATES:
                continue
            entry = by_id.get(server_id)
            if entry is None:
                # Membership refresh will settle disabled/deleted rows.
                continue
            if not force:
                due_at = _parse_iso(row.get("next_attempt_at"))
                if due_at is not None and now < due_at:
                    continue
            targets.append(
                _ServerTarget(
                    server_id=server_id,
                    server_name=str(entry.get("name") or server_id),
                    plex_token=self._server_plex_token(entry),
                    public_url=self._server_public_url(entry),
                    generation=generation,
                    state=str(row.get("state")),
                    attempts=int(row.get("attempts") or 0),
                    new_confirmed=bool(row.get("new_url_confirmed")),
                )
            )
        return targets

    def _attempt_remote(self, target: _ServerTarget, new_secret: str, stale_secrets: list[str]):
        """Run one reconcile pass against plex.tv (no state lock held)."""
        if not target.plex_token or not target.public_url:
            return self.pwh.ReconcileResult(
                outcome=self.pwh.RECONCILE_NOT_CONFIGURED,
                reason="not_configured",
            )
        wanted = self.pwh.build_authenticated_url(target.public_url, new_secret)
        return self.pwh.reconcile_webhook(
            target.plex_token,
            target.public_url,
            wanted,
            stale_secrets=stale_secrets,
        )

    def _apply_result_locked(self, target: _ServerTarget, result, *, network_exc: Exception | None) -> None:
        rows = self._state.setdefault("servers", {})
        row = rows.get(target.server_id)
        if row is None or int(row.get("generation") or 0) != target.generation:
            # Stale response (server deleted / new generation began mid-flight) — discard.
            logger.debug("Discarding rotation attempt result for stale generation {}", target.server_id)
            return
        if row.get("state") in TERMINAL_STATES:
            # A manual unregister / disable / delete settled this row while
            # the plex.tv call was in flight — that resolution wins.
            logger.debug(
                "Discarding rotation attempt result for already-settled server {}",
                target.server_id,
            )
            return
        now = _now()
        row["attempts"] = int(row.get("attempts") or 0) + 1
        row["last_attempt_at"] = _iso(now)
        row["updated_at"] = _iso(now)

        outcome = getattr(result, "outcome", None)
        if outcome == self.pwh.RECONCILE_NOT_CONFIGURED:
            row["state"] = S_RESOLVED_NO_WEBHOOK
            row["next_attempt_at"] = None
            row["failure_reason"] = None
            row["failure_message"] = None
            return

        if network_exc is not None or outcome == self.pwh.RECONCILE_UNCERTAIN:
            row["state"] = S_UNCERTAIN
            row["new_url_confirmed"] = bool(getattr(result, "new_confirmed", False)) or row["new_url_confirmed"]
            row["old_removed"] = False
            row["failure_reason"] = getattr(result, "reason", None) or "network_error"
            row["failure_message"] = getattr(result, "message", None) or (
                f"plex.tv response uncertain: {type(network_exc).__name__}"
                if network_exc is not None
                else "plex.tv response uncertain"
            )
        elif outcome == self.pwh.RECONCILE_REJECTED:
            row["state"] = S_FAILED_REJECTED
            row["new_url_confirmed"] = bool(getattr(result, "new_confirmed", False))
            row["old_removed"] = False
            row["failure_reason"] = getattr(result, "reason", None) or "rejected"
            row["failure_message"] = getattr(result, "message", None) or "plex.tv rejected the registration"
        elif outcome == self.pwh.RECONCILE_CONVERGED:
            row["state"] = S_CONVERGED
            row["new_url_confirmed"] = True
            row["old_removed"] = True
            row["failure_reason"] = None
            row["failure_message"] = None
            row["next_attempt_at"] = None
            logger.info(
                "Plex server {} converged on secret rotation generation {}",
                target.server_name,
                target.generation,
            )
            return
        else:  # pragma: no cover - defensive: unknown outcome
            row["state"] = S_UNCERTAIN
            row["failure_reason"] = getattr(result, "reason", None) or "unknown_outcome"
            row["failure_message"] = "Unrecognised reconciliation outcome; will re-verify"

        row["next_attempt_at"] = _iso(now + timedelta(seconds=self._backoff_seconds(row["attempts"])))

    def run_once(self, *, force: bool = True) -> None:
        """Execute one deterministic reconciliation pass. Used by the worker and tests."""
        with self._lock:
            if self._state is None or self._state.get("aborted"):
                return
            self._check_settings_consistency_locked()
            if self._state.get("aborted"):
                self._save_locked()
                return
            self._refresh_membership_locked()
            targets = self._due_targets_locked(force=force)
            generation = int(self._state.get("generation") or 0)
            new_secret = str(self._state.get("new_secret") or "")
            stale_secrets = list(self._state.get("legacy_secrets") or [])

        for target in targets:
            result = None
            exc: Exception | None = None
            try:
                result = self._attempt_remote(target, new_secret, stale_secrets)
            except Exception as caught:
                exc = caught
                if self.pwh.is_deterministic_rejection(caught):
                    result = self.pwh.ReconcileResult(
                        outcome=self.pwh.RECONCILE_REJECTED,
                        reason=getattr(caught, "reason", "rejected"),
                        message="plex.tv rejected the registration; the previous registration is kept.",
                    )
                else:
                    result = self.pwh.ReconcileResult(
                        outcome=self.pwh.RECONCILE_UNCERTAIN,
                        reason="network_error",
                    )
            with self._lock:
                if self._state is None or int(self._state.get("generation") or 0) != generation:
                    break
                self._apply_result_locked(target, result, network_exc=exc)
                self._maybe_complete_locked()
                self._save_locked()

        with self._lock:
            # Membership may need settling even when no target was due;
            # an all-terminal set completes and removes the state instead.
            self._refresh_membership_locked()
            if self._state is not None and not self._maybe_complete_locked():
                self._save_locked()

    def _next_wake_delay_locked(self) -> float | None:
        if self._state is None or self._state.get("aborted"):
            return None
        soonest: datetime | None = None
        for row in (self._state.get("servers") or {}).values():
            if row.get("state") not in ACTIVE_STATES:
                continue
            due = _parse_iso(row.get("next_attempt_at"))
            if due is not None and (soonest is None or due < soonest):
                soonest = due
        if soonest is None:
            # Membership is fully terminal — completion should have fired.
            return None
        return max(0.5, min(self.retry_cap, (soonest - _now()).total_seconds()))

    def _maybe_complete_locked(self) -> bool:
        if self._state is None or self._state.get("aborted"):
            return False
        rows = self._state.get("servers") or {}
        # Empty membership (last Plex deleted/out-of-band removal) and an
        # all-terminal set both mean nothing still needs converging.
        if all(row.get("state") in TERMINAL_STATES for row in rows.values()):
            logger.info(
                "All {} Plex server(s) converged on secret rotation generation {} — old secret revoked",
                len(rows),
                self._state.get("generation"),
            )
            self._clear_state_locked()
            return True
        return False

    def _ensure_worker(self) -> None:
        """Spawn the worker when none is alive. Caller may hold the lock."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            thread = threading.Thread(
                target=self._worker_loop,
                name="webhook-secret-rotation",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def arm_worker(self) -> None:
        """(Re)arm the worker SAFE to call after releasing the manager lock.

        The worker detaches itself in a ``finally`` right before exiting;
        a ``begin_rotation`` landing in the residual scheduling window can
        still see the dying thread as alive under the lock. Re-checking
        outside the lock lets the detached/dead state become visible and
        guarantees a successor thread starts for any live round.
        """
        with self._lock:
            active = self._state is not None and not self._state.get("aborted")
            thread = self._thread
        if active and (thread is None or not thread.is_alive()):
            self._ensure_worker()

    def _worker_loop(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._state is None or self._state.get("aborted"):
                        return
                    self._check_settings_consistency_locked()
                    if self._state.get("aborted"):
                        self._save_locked()
                        return
                    delay = self._next_wake_delay_locked()
                if delay is None:
                    # Membership check once...
                    self.run_once(force=False)
                    with self._lock:
                        # ...then re-check: a begin_rotation() may have landed
                        # in the tiny window while that pass was running and
                        # found nothing to do. Close the race instead of
                        # trusting the (possibly stale) is_alive() check.
                        still_idle = (
                            self._state is None
                            or bool(self._state.get("aborted"))
                            or self._next_wake_delay_locked() is None
                        )
                    if still_idle:
                        return
                    continue
                self._wake.wait(timeout=min(delay, 5.0))
                self._wake.clear()
                try:
                    self.run_once(force=False)
                except Exception:
                    logger.exception("Secret rotation worker pass failed; will retry on the backoff schedule")
        finally:
            # Detach BEFORE the thread actually dies: a begin_rotation()
            # landing in the residual gap (recheck taken, thread not yet
            # exited) must observe "no worker" and spawn a successor rather
            # than trust is_alive() on a dying thread.
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def resume_on_startup(self) -> None:
        """Recover an in-flight round after a process restart."""
        with self._lock:
            if self._state is None:
                return
            self._check_settings_consistency_locked()
            if self._state.get("aborted"):
                logger.warning(
                    "Skipping resume of aborted webhook secret rotation generation {}: {}",
                    self._state.get("generation"),
                    self._state.get("abort_reason"),
                )
                self._save_locked()
                return
            if self._refresh_membership_locked():
                self._save_locked()
            generation = int(self._state.get("generation") or 0)
            active_count = sum(
                1 for row in (self._state.get("servers") or {}).values() if row.get("state") in ACTIVE_STATES
            )
            if active_count:
                logger.info(
                    "Resuming webhook secret rotation generation {} with {} pending Plex server(s)",
                    generation,
                    active_count,
                )
                start_worker = True
                self._wake.set()
            else:
                self._maybe_complete_locked()
                start_worker = self._state is not None and not self._state.get("aborted")
        if start_worker:
            self.arm_worker()

    def shutdown(self) -> None:
        """Signal the worker to wake; daemon thread exits on its own."""
        self._wake.set()


# ---------------------------------------------------------------------------
# Process-wide singleton (mirrors settings_manager / scheduler patterns)
# ---------------------------------------------------------------------------

_manager: RotationManager | None = None
_manager_lock = threading.Lock()


def get_rotation_manager(config_dir: str | None = None, **kwargs) -> RotationManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = RotationManager(config_dir, **kwargs)
        elif config_dir is not None and str(_manager.config_dir) != config_dir:
            _manager = RotationManager(config_dir, **kwargs)
        return _manager


def peek_rotation_manager() -> RotationManager | None:
    return _manager


def reset_rotation_manager() -> None:
    """Reset the singleton. Test-only (matches reset_settings_manager)."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()
        _manager = None

"""Webhook-secret rotation status and control API.

Read-only visibility into the in-flight rotation round (overall state +
per-Plex generation / two-phase convergence state / failure reason) and
a single "retry now" action that clears the persisted backoff timers
and wakes the reconciliation worker.

Every response is produced by ``RotationManager.public_status()`` and
contains neither the old/new secrets nor any token-bearing URLs.
"""

from flask import jsonify

from ..auth import setup_or_auth_required
from ..secret_rotation import get_rotation_manager
from . import api


@api.route("/settings/webhook-rotation", methods=["GET"])
@setup_or_auth_required
def webhook_rotation_status():
    """Report the current rotation round (``{"active": false}`` when idle).

    Active payload includes the generation, grace deadline, whether the
    old secret still authenticates, and one row per participating Plex
    server: generation, state, new-URL/old-URL confirmation booleans,
    failure reason/message and attempt timestamps.
    """
    return jsonify(get_rotation_manager().public_status())


@api.route("/settings/webhook-rotation/retry", methods=["POST"])
@setup_or_auth_required
def webhook_rotation_retry():
    """Immediately retry failed/uncertain servers without changing generation.

    Clears persisted backoff timers on retryable per-server rows and
    wakes the background reconciliation worker. Safe to call when idle
    (returns ``retried: 0``).
    """
    retried = get_rotation_manager().request_retry()
    return jsonify({"success": True, "retried": retried})

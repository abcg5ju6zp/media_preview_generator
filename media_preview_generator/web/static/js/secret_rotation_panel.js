// Webhook secret rotation status panel for Settings -> Webhooks.
//
// Polls GET /api/settings/webhook-rotation while a rotation round is
// active and renders per-Plex generation / two-phase convergence state
// / failure reason. The panel renders ONLY data from the secret-free
// public_status() payload — no secret value is ever present in the API
// response or rendered here.

(function () {
    'use strict';

    var POLL_INTERVAL_MS = 5000;

    var STATE_LABELS = {
        new_pending: { text: 'Registering new URL…', cls: 'bg-warning text-dark' },
        removing_old: { text: 'Removing old URL…', cls: 'bg-info text-dark' },
        failed_rejected: { text: 'Plex rejected registration — old webhook kept', cls: 'bg-danger' },
        uncertain: { text: 'Re-verifying with Plex (network uncertain)', cls: 'bg-warning text-dark' },
        converged: { text: 'Converged', cls: 'bg-success' },
        resolved_no_webhook: { text: 'No webhook configured', cls: 'bg-secondary' },
        resolved_unregistered: { text: 'Unregistered', cls: 'bg-secondary' },
        resolved_disabled: { text: 'Server disabled', cls: 'bg-secondary' },
        resolved_deleted: { text: 'Server deleted', cls: 'bg-secondary' }
    };

    function $(id) {
        return document.getElementById(id);
    }

    function escapeHtml(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatRemaining(iso) {
        if (!iso) return '';
        var ms = new Date(iso).getTime() - Date.now();
        if (!isFinite(ms) || ms <= 0) return 'transition window closed';
        var mins = Math.round(ms / 60000);
        if (mins < 60) return mins + ' min left';
        var hours = Math.floor(mins / 60);
        var remMins = mins % 60;
        return hours + 'h ' + remMins + 'm left';
    }

    function setBadge(el, cls, text) {
        el.className = 'badge ' + cls;
        el.textContent = text;
    }

    function render(data) {
        var panel = $('secretRotationPanel');
        if (!panel) return;

        // An aborted round stays visible (with its reason) until a new
        // rotation replaces it; only a truly idle state hides the panel.
        if (!data || (!data.active && !data.aborted)) {
            panel.classList.add('d-none');
            return;
        }

        panel.classList.remove('d-none');
        $('secretRotationGeneration').textContent = 'Generation ' + (data.generation || 1);

        var badge = $('secretRotationBadge');
        if (data.aborted) {
            setBadge(badge, 'bg-danger', 'Interrupted');
        } else if (data.grace_expired) {
            setBadge(badge, 'bg-warning text-dark', 'Old secret expired');
        } else {
            setBadge(badge, 'bg-success', 'Old + new secret accepted');
        }

        var summary = $('secretRotationSummary');
        var parts = [];
        parts.push((data.converged_count || 0) + ' / ' + (data.server_count || 0) + ' Plex servers settled');
        if (data.old_secret_valid && data.grace_until) {
            parts.push('old secret valid for ' + formatRemaining(data.grace_until));
        } else if (!data.aborted) {
            parts.push('old secret no longer accepted — background repair continues for any pending server');
        }
        if (data.aborted && data.abort_reason) {
            parts.push(escapeHtml(data.abort_reason));
        }
        summary.textContent = '';
        summary.innerHTML = '<i class="bi bi-info-circle me-1"></i>' + parts.join(' · ');

        var list = $('secretRotationServers');
        list.innerHTML = '';
        (data.servers || []).forEach(function (srv) {
            var li = document.createElement('li');
            li.className = 'd-flex align-items-center flex-wrap gap-2 mb-1';

            var name = document.createElement('span');
            name.className = 'fw-medium';
            name.textContent = srv.server_name || srv.server_id;
            li.appendChild(name);

            var gen = document.createElement('span');
            gen.className = 'badge bg-light text-muted border';
            gen.textContent = 'gen ' + srv.generation;
            li.appendChild(gen);

            var label = STATE_LABELS[srv.state] || { text: srv.state, cls: 'bg-secondary' };
            var state = document.createElement('span');
            state.className = 'badge ' + label.cls;
            state.textContent = label.text;
            li.appendChild(state);

            var phases = [];
            phases.push(srv.new_url_confirmed ? 'new URL confirmed' : 'new URL pending');
            phases.push(srv.old_removed ? 'old URL removed' : 'old URL pending');
            var phaseSpan = document.createElement('span');
            phaseSpan.className = 'text-muted';
            phaseSpan.textContent = phases.join(' · ');
            li.appendChild(phaseSpan);

            if (srv.failure_reason) {
                var reason = document.createElement('span');
                reason.className = 'text-danger';
                var reasonText = '(' + srv.failure_reason + ')';
                if (srv.failure_message) {
                    reasonText += ' ' + srv.failure_message;
                }
                reason.textContent = reasonText;
                li.appendChild(reason);
            }
            list.appendChild(li);
        });
    }

    async function refresh() {
        try {
            // apiGet is the page's standard JSON helper (session cookie +
            // CSRF-aware headers shared with the settings forms).
            var data = (typeof apiGet === 'function')
                ? await apiGet('/api/settings/webhook-rotation')
                : await fetch('/api/settings/webhook-rotation', { credentials: 'same-origin' }).then(function (r) {
                    return r.json();
                });
            render(data);
        } catch (_e) {
            // Transient fetch errors leave the last-rendered state in place.
        }
    }

    async function retryNow() {
        var btn = $('secretRotationRetryBtn');
        if (!btn) return;
        btn.disabled = true;
        try {
            if (typeof apiPost === 'function') {
                await apiPost('/api/settings/webhook-rotation/retry', {});
            } else {
                await fetch('/api/settings/webhook-rotation/retry', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' }
                });
            }
            await refresh();
        } finally {
            btn.disabled = false;
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        var panel = $('secretRotationPanel');
        if (!panel) return;
        var btn = $('secretRotationRetryBtn');
        if (btn) btn.addEventListener('click', retryNow);
        refresh();
        setInterval(refresh, POLL_INTERVAL_MS);
    });
})();

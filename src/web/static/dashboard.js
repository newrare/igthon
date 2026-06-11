// Dashboard live-update engine, toasts, modals, and trading actions.
// Extracted from the rendered inline <script> of the former shell.py.
// ── Toast notifications ─────────────────────────────────────────────────────
const _TOAST_ICONS = { success:'circle-check', error:'circle-x', warning:'alert-circle', info:'info' };
const _TOAST_SVG = {
    'circle-check':  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>',
    'circle-x':      '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/></svg>',
    'alert-circle':  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
    'info':          '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>'
};

function showToast(title, msg, type) {
    type = type || 'info';
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
    }
    const icon = _TOAST_SVG[_TOAST_ICONS[type]] || _TOAST_SVG['info'];
    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.innerHTML =
        '<span class="toast-icon">' + icon + '</span>'
        + '<div class="toast-body">'
        + (title ? '<div class="toast-title">' + title + '</div>' : '')
        + (msg   ? '<div class="toast-msg">'   + msg   + '</div>' : '')
        + '</div>';
    container.appendChild(toast);
    const timer = setTimeout(function() { _dismissToast(toast); }, 4500);
    toast.addEventListener('click', function() { clearTimeout(timer); _dismissToast(toast); });
}

function _dismissToast(toast) {
    toast.classList.add('toast-out');
    toast.addEventListener('animationend', function() { toast.remove(); }, { once: true });
}

// ── Collapse state persistence ──────────────────────────────────────────────
const STORAGE_KEY = 'ig_sections_v1';

function _loadState() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); }
    catch { return {}; }
}

function _saveState(sid, collapsed) {
    const s = _loadState();
    s[sid] = collapsed;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
}

function _toggleSection(header) {
    const body = header.nextElementSibling;
    const btn  = header.querySelector('.section-toggle');
    if (!body || !btn) return;
    const collapsed = body.classList.toggle('collapsed');
    btn.textContent = collapsed ? '+' : '−';
    if (header.dataset.sid) _saveState(header.dataset.sid, collapsed);
}

// Event delegation — handles both existing and dynamically added headers
document.addEventListener('click', function(e) {
    const header = e.target.closest('.section-header');
    if (header) _toggleSection(header);
});

// Restore collapse state from localStorage before first paint
document.addEventListener('DOMContentLoaded', function() {
    const saved = _loadState();
    document.querySelectorAll('.section-header[data-sid]').forEach(function(header) {
        const sid = header.dataset.sid;
        if (saved[sid]) {
            const body = header.nextElementSibling;
            const btn  = header.querySelector('.section-toggle');
            if (body) body.classList.add('collapsed');
            if (btn)  btn.textContent = '+';
        }
    });
});

// ── Job mode switches (Actions section) ─────────────────────────────────────
// Each job toggles between automatic (scheduled) and manual (paused, Run-only).
// State changes only on user action, so the switches are not touched by the
// 2 s live poll — that would fight with mid-toggle interaction.
function _applyJobModeUI(card, auto) {
    const runBtn    = card.querySelector('.run-btn');
    const modeLabel = card.querySelector('.action-card-mode');
    if (runBtn) runBtn.style.display = auto ? 'none' : '';
    if (modeLabel) {
        modeLabel.textContent = auto ? 'Automatic' : 'Manual';
        modeLabel.className   = 'action-card-mode ' + (auto ? 'auto' : 'manual');
    }
    card.classList.toggle('is-auto', auto);
}

async function toggleJobMode(action, cb) {
    const auto = cb.checked;
    const card = cb.closest('.action-card');
    cb.disabled = true;
    try {
        const res = await fetch('/api/jobs/' + action + '/' + (auto ? 'auto' : 'manual'), { method: 'POST' });
        if (res.ok) {
            _applyJobModeUI(card, auto);
            showToast(action.replace(/_/g, ' '), auto ? 'Now automatic' : 'Now manual', auto ? 'success' : 'info');
        } else {
            cb.checked = !auto;  // revert on failure
            showToast(action.replace(/_/g, ' '), 'Failed to change mode', 'error');
        }
    } catch (e) {
        cb.checked = !auto;
        showToast(action.replace(/_/g, ' '), 'Network error', 'error');
    } finally {
        cb.disabled = false;
    }
}

// General pause-all / resume-all — flips every job at once.
async function setAllJobs(auto, btn) {
    btn.disabled = true;
    try {
        const res = await fetch(auto ? '/api/bot/resume' : '/api/bot/pause', { method: 'POST' });
        if (res.ok) {
            document.querySelectorAll('.action-card[data-action]').forEach(function(card) {
                const cb = card.querySelector('.switch input');
                if (cb) cb.checked = auto;
                _applyJobModeUI(card, auto);
            });
            showToast(auto ? 'All jobs automatic' : 'All jobs manual', null, auto ? 'success' : 'warning');
        } else {
            showToast('Error', 'Failed to switch all jobs', 'error');
        }
    } catch (e) {
        showToast('Error', 'Network error', 'error');
    } finally {
        btn.disabled = false;
    }
}

// ── Live fragment polling (single request → in-place section updates) ────────
// One request every POLL_INTERVAL returns the HTML for every dynamic region.
// Only the fragments whose markup actually changed are swapped into the DOM, so
// there is no full-page reload, no scroll jump and no lost UI state.
const PAUSE_KEY     = 'ig_refresh_paused';
const POLL_INTERVAL = 1000; // ms
const LIVE_STAMPS   = ['refresh-kpi', 'refresh-market', 'refresh-week', 'refresh-day', 'refresh-queue', 'refresh-epics', 'refresh-positions', 'refresh-closed', 'refresh-actions', 'refresh-logs'];
const btnPause  = document.getElementById('btn-pause');
const footer    = document.getElementById('footer-refresh');

const LOGS_PAUSE_KEY = 'ig_logs_paused';

let _paused      = localStorage.getItem(PAUSE_KEY) === 'true';
let _logsPaused  = localStorage.getItem(LOGS_PAUSE_KEY) === 'true';
let _pendingLogs = null;
let _timeoutId   = null;
let _polling     = false;
const _lastFrags = {};

function _stamp(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

function _applyPauseUI() {
    if (_paused) {
        btnPause.innerHTML = '<i data-lucide="play" class="lc-icon"></i> Resume';
        btnPause.classList.add('paused');
        footer.textContent   = 'Live updates paused';
    } else {
        btnPause.innerHTML = '<i data-lucide="pause" class="lc-icon"></i> Pause';
        btnPause.classList.remove('paused');
        footer.textContent   = 'Live — updating every 2 s';
    }
    lucide.createIcons();
}

function togglePause() {
    _paused = !_paused;
    localStorage.setItem(PAUSE_KEY, _paused ? 'true' : 'false');
    if (_paused) {
        if (_timeoutId) clearTimeout(_timeoutId);
        _applyPauseUI();
    } else {
        _applyPauseUI();
        _poll();  // resume immediately
    }
}

// Per-section pause for the server logs — freezes only that section so the
// rest of the dashboard keeps refreshing while the user reads the log stream.
function _applyLogsPauseUI() {
    const btn = document.getElementById('btn-logs-pause');
    if (!btn) return;
    if (_logsPaused) {
        btn.innerHTML = '<i data-lucide="play" class="lc-icon"></i> Resume';
        btn.classList.add('paused');
    } else {
        btn.innerHTML = '<i data-lucide="pause" class="lc-icon"></i> Pause';
        btn.classList.remove('paused');
    }
    lucide.createIcons();
}

function toggleLogsPause(event) {
    if (event) event.stopPropagation();  // don't collapse the section
    _logsPaused = !_logsPaused;
    localStorage.setItem(LOGS_PAUSE_KEY, _logsPaused ? 'true' : 'false');
    _applyLogsPauseUI();
    // On resume, flush the freshest markup captured while paused (if any).
    if (!_logsPaused && _pendingLogs !== null) {
        const el = document.getElementById('frag-logs_section');
        if (el) {
            el.innerHTML = _pendingLogs;
            _lastFrags['logs_section'] = _pendingLogs;
            lucide.createIcons();
            _reapplyLogFilter();
        }
        _pendingLogs = null;
    }
}

// Swap only the fragments whose HTML actually changed since the last poll.
function _applyFragments(frags) {
    let changed = false;
    for (const id in frags) {
        // Hold the server-log section frozen while the user is reading it; keep
        // the freshest markup aside so resuming shows the latest without a wait.
        if (id === 'logs_section' && _logsPaused) {
            _pendingLogs = frags[id];
            continue;
        }
        const el = document.getElementById('frag-' + id);
        if (!el) continue;
        if (_lastFrags[id] !== frags[id]) {
            el.innerHTML   = frags[id];
            _lastFrags[id] = frags[id];
            changed = true;
        }
    }
    if (changed) lucide.createIcons();
    _reapplyLogFilter();
}

function _scheduleNextPoll() {
    if (_timeoutId) clearTimeout(_timeoutId);
    _timeoutId = setTimeout(_poll, POLL_INTERVAL);
}

async function _poll() {
    if (_paused || _polling) return;
    _polling = true;
    try {
        const ctrl = new AbortController();
        const t    = setTimeout(() => ctrl.abort(), 5000);
        const res  = await fetch('/api/dashboard-fragments', { signal: ctrl.signal });
        clearTimeout(t);
        if (res.ok) {
            const data = await res.json();
            _applyFragments(data.fragments || {});
            const st = data.server_time || '';
            LIVE_STAMPS.forEach(function(id) { _stamp(id, st); });
        }
    } catch (_) {}
    finally {
        _polling = false;
        if (!_paused) _scheduleNextPoll();
    }
}

// Static sections (config, actions, commands) never change between polls — stamp
// them once with the page-load time; the live sections are stamped on each poll.
(function _initStamps() {
    const t = new Date().toLocaleTimeString('fr-FR', { hour12: false });
    ['refresh-commands'].forEach(function(id) { _stamp(id, t); });
    LIVE_STAMPS.forEach(function(id) { _stamp(id, t); });
})();

_applyPauseUI();
_applyLogsPauseUI();
if (!_paused) {
    _scheduleNextPoll();
}

async function clearErrors() {
    try {
        await fetch('/api/ig-errors/clear', { method: 'POST' });
        document.getElementById('err-tbody').innerHTML =
            '<tr><td colspan="6" class="err-empty">No API errors recorded this session.</td></tr>';
        showToast('Error log cleared', null, 'info');
    } catch (e) {
        console.error('Clear errors failed', e);
        showToast('Error', 'Failed to clear error log', 'error');
    }
}

// ── KPI refresh buttons ─────────────────────────────────────────────────────
async function runKpiAction(action, btn) {
    btn.disabled = true;
    btn.innerHTML = '<i data-lucide="loader-circle" class="lc-icon lc-spin"></i>';
    lucide.createIcons();
    try {
        const res = await fetch('/api/actions/' + action, { method: 'POST' });
        if (res.ok) {
            btn.textContent = '✓';
            showToast(action.replace(/_/g, ' '), 'Task triggered successfully', 'success');
        } else {
            btn.textContent = '✗';
            showToast(action.replace(/_/g, ' '), 'Task failed (HTTP ' + res.status + ')', 'error');
        }
    } catch (e) {
        btn.textContent = '✗';
        showToast(action.replace(/_/g, ' '), 'Network error', 'error');
    } finally {
        setTimeout(() => {
            btn.innerHTML = '<i data-lucide="refresh-cw" class="lc-icon"></i>';
            lucide.createIcons();
            btn.disabled = false;
        }, 3000);
    }
}

// ── Manual actions ──────────────────────────────────────────────────────────
async function runAction(action, btn, needsConfirm) {
    if (needsConfirm && !confirm('Run "' + action + '"? This action may affect live positions or data.')) return;
    const card   = btn.closest('.action-card');
    const status = card.querySelector('.action-status');
    btn.disabled = true;
    status.className = 'action-status running';
    status.innerHTML = '<i data-lucide="loader-circle" class="lc-icon lc-spin"></i> running…';
    lucide.createIcons();
    try {
        const res  = await fetch('/api/actions/' + action, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            status.className = 'action-status ok';
            status.textContent = '✓ triggered';
            showToast(action.replace(/_/g, ' '), 'Task triggered successfully', 'success');
        } else {
            const errMsg = data.error || 'error';
            status.className = 'action-status err';
            status.textContent = '✗ ' + errMsg;
            showToast(action.replace(/_/g, ' '), errMsg, 'error');
        }
    } catch (e) {
        status.className = 'action-status err';
        status.textContent = '✗ network error';
        showToast(action.replace(/_/g, ' '), 'Network error', 'error');
    } finally {
        btn.disabled = false;
        setTimeout(() => {
            status.className = 'action-status';
            status.textContent = '';
        }, 6000);
    }
}

// ── Truncated text modal ────────────────────────────────────────────────────
function showModal(el) {
    const full = el.dataset.full;
    if (!full) return;
    let modal = document.getElementById('text-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'text-modal';
        modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:9000;display:flex;align-items:center;justify-content:center;';
        modal.innerHTML = '<div style="background:#1e293b;border:1px solid #334155;border-radius:6px;padding:1.2rem 1.5rem;max-width:680px;width:90%;max-height:60vh;overflow:auto;">'
            + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.8rem;">'
            + '<span style="color:#94a3b8;font-size:0.75rem;text-transform:uppercase;letter-spacing:1px;">Full value</span>'
            + '<button onclick="document.getElementById(\'text-modal\').remove()" style="background:none;border:none;color:#94a3b8;cursor:pointer;font-size:1.1rem;">✕</button>'
            + '</div>'
            + '<pre id="text-modal-body" style="white-space:pre-wrap;word-break:break-all;color:#e2e8f0;font-family:monospace;font-size:0.82rem;margin:0;"></pre>'
            + '</div>';
        modal.addEventListener('click', function(e) {
            if (e.target === modal) modal.remove();
        });
        document.body.appendChild(modal);
    }
    document.getElementById('text-modal-body').textContent = full;
    document.getElementById('text-modal').style.display = 'flex';
}

// ── Queue modal ──────────────────────────────────────────────────────────────
function openQueueModal() {
    document.getElementById('queue-modal').style.display = 'block';
}
function closeQueueModal() {
    document.getElementById('queue-modal').style.display = 'none';
}

// ── Epic List modal ───────────────────────────────────────────────────────────
function openEpicsModal() {
    document.getElementById('epics-modal').style.display = 'block';
}
function closeEpicsModal() {
    document.getElementById('epics-modal').style.display = 'none';
}

// Client-side filter over the epic table inside the Epic List modal.
function filterEpicsModal(q) {
    const tbody = document.getElementById('epic-modal-tbody');
    if (!tbody) return;
    const ql = q.toLowerCase();
    let shown = 0;
    tbody.querySelectorAll('tr').forEach(function(tr) {
        const hide = ql && !tr.textContent.toLowerCase().includes(ql);
        tr.classList.toggle('hidden', hide);
        if (!hide) shown++;
    });
    const counter = document.getElementById('epic-modal-count');
    if (counter) counter.textContent = shown + ' shown';
}

// ── Open Positions modal ──────────────────────────────────────────────────────
function openPositionsModal() {
    document.getElementById('positions-modal').style.display = 'block';
}
function closePositionsModal() {
    document.getElementById('positions-modal').style.display = 'none';
}

// ── Closed Positions modal ────────────────────────────────────────────────────
function openClosedModal() {
    document.getElementById('closed-modal').style.display = 'block';
}
function closeClosedModal() {
    document.getElementById('closed-modal').style.display = 'none';
}

// ── Win Rate / Configuration modal ────────────────────────────────────────────
function openWinRateModal() {
    document.getElementById('winrate-modal').style.display = 'block';
}
function closeWinRateModal() {
    document.getElementById('winrate-modal').style.display = 'none';
}

// ── Buy Confirmation Modal ────────────────────────────────────────────────────
let _buyConfirmResolve = null;

function openBuyConfirmModal(epic) {
    return new Promise(function(resolve) {
        _buyConfirmResolve = resolve;
        document.getElementById('buy-confirm-epic').textContent = epic;
        document.getElementById('buy-confirm-modal').style.display = 'flex';
    });
}

function closeBuyConfirmModal(confirmed) {
    document.getElementById('buy-confirm-modal').style.display = 'none';
    if (_buyConfirmResolve) {
        _buyConfirmResolve(confirmed);
        _buyConfirmResolve = null;
    }
}

// ── Close Position Confirmation Modal ────────────────────────────────────────
let _closeConfirmResolve = null;

function openCloseConfirmModal(epic) {
    return new Promise(function(resolve) {
        _closeConfirmResolve = resolve;
        document.getElementById('close-confirm-epic').textContent = epic;
        document.getElementById('close-confirm-modal').style.display = 'flex';
    });
}

function closeCloseConfirmModal(confirmed) {
    document.getElementById('close-confirm-modal').style.display = 'none';
    if (_closeConfirmResolve) {
        _closeConfirmResolve(confirmed);
        _closeConfirmResolve = null;
    }
}

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        if (document.getElementById('buy-confirm-modal').style.display !== 'none') closeBuyConfirmModal(false);
        if (document.getElementById('close-confirm-modal').style.display !== 'none') closeCloseConfirmModal(false);
        if (document.getElementById('epics-modal').style.display !== 'none') closeEpicsModal();
        if (document.getElementById('positions-modal').style.display !== 'none') closePositionsModal();
        if (document.getElementById('closed-modal').style.display !== 'none') closeClosedModal();
        if (document.getElementById('winrate-modal').style.display !== 'none') closeWinRateModal();
    }
});

// ── Open position (manual BUY from dashboard) ─────────────────────────────────
async function openPosition(epic, btn) {
    const confirmed = await openBuyConfirmModal(epic);
    if (!confirmed) return;
    const origText  = btn.textContent;
    const origBg    = btn.style.background;
    const origColor = btn.style.color;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const res  = await fetch('/api/positions/open/' + encodeURIComponent(epic), { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            btn.textContent      = '✓';
            btn.style.background = '#16803c';
            btn.style.color      = '#f0fdf4';
            btn.title = 'Opened @ ' + data.level + ' qty=' + data.quantity;
            showToast('Position opened', epic + ' @ ' + data.level + ' — qty ' + data.quantity, 'success');
        } else {
            btn.textContent      = '✗';
            btn.style.background = '#991b1b';
            btn.style.color      = '#fef2f2';
            btn.title = data.error || 'Error';
            showToast('Order failed', data.error || 'Could not open position', 'error');
        }
    } catch(e) {
        btn.textContent      = '✗';
        btn.style.background = '#991b1b';
        btn.style.color      = '#fef2f2';
        btn.title = e.message;
        showToast('Order failed', e.message, 'error');
    } finally {
        setTimeout(function() {
            btn.textContent      = origText;
            btn.style.background = origBg;
            btn.style.color      = origColor;
            btn.title            = 'Open BUY position at minimum size';
            btn.disabled         = false;
        }, 5000);
    }
}

// ── BUY funds tooltip (margin required, on hover) ────────────────────────────
// Shows the cash needed to open a minimum-size position before the user commits,
// so an underfunded BUY never reaches IG (and never returns "insufficient funds").
// Results are cached per epic for a few seconds — hovering across the table does
// not spam the API.
const _FUNDS_CACHE_TTL = 8000; // ms
let _fundsCache   = {};        // epic -> { at, data }
let _fundsTip     = null;
let _fundsReqId   = 0;

function _ensureFundsTip() {
    if (_fundsTip) return _fundsTip;
    _fundsTip = document.createElement('div');
    _fundsTip.id = 'funds-tooltip';
    _fundsTip.style.display = 'none';
    document.body.appendChild(_fundsTip);
    return _fundsTip;
}

function _positionFundsTip(target) {
    const tip = _ensureFundsTip();
    const r = target.getBoundingClientRect();
    // Anchor above the button, horizontally centered, clamped to the viewport.
    tip.style.display = 'block';
    const tw = tip.offsetWidth;
    let left = r.left + r.width / 2 - tw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
    tip.style.left = left + 'px';
    tip.style.top  = (r.top - tip.offsetHeight - 8) + 'px';
}

function _renderFundsTip(target, data) {
    const tip = _ensureFundsTip();
    if (data.error) {
        tip.innerHTML = '<span class="ft-muted">' + data.error + '</span>';
    } else if (data.margin_eur == null) {
        tip.innerHTML = '<span class="ft-muted">Funds needed: unknown</span>';
    } else {
        const need = data.margin_eur.toFixed(2);
        const avail = data.available_eur == null ? '—' : data.available_eur.toFixed(2);
        const cls = data.sufficient ? 'ft-ok' : 'ft-low';
        const mark = data.sufficient ? '✓' : '✗';
        tip.innerHTML =
              '<div class="ft-row"><span>Funds needed</span><strong>' + need + '€</strong></div>'
            + '<div class="ft-row"><span>Available</span><strong>' + avail + '€</strong></div>'
            + '<div class="ft-verdict ' + cls + '">' + mark + ' '
            + (data.sufficient ? 'Sufficient' : 'Insufficient') + '</div>';
    }
    _positionFundsTip(target);
}

async function showFundsTooltip(evt, epic) {
    const target = evt.currentTarget;
    const reqId = ++_fundsReqId;
    const cached = _fundsCache[epic];
    if (cached && (Date.now() - cached.at) < _FUNDS_CACHE_TTL) {
        _renderFundsTip(target, cached.data);
        return;
    }
    const tip = _ensureFundsTip();
    tip.innerHTML = '<span class="ft-muted">Computing…</span>';
    _positionFundsTip(target);
    try {
        const res  = await fetch('/api/positions/funds/' + encodeURIComponent(epic));
        const data = await res.json();
        _fundsCache[epic] = { at: Date.now(), data: data };
        // Ignore if the user already hovered elsewhere / left the button.
        if (reqId === _fundsReqId) _renderFundsTip(target, data);
    } catch(e) {
        if (reqId === _fundsReqId) _renderFundsTip(target, { error: e.message });
    }
}

function hideFundsTooltip() {
    _fundsReqId++;
    if (_fundsTip) _fundsTip.style.display = 'none';
}

// ── Close position (manual SELL from positions modal) ────────────────────────
async function closePosition(positionId, epic, btn) {
    const confirmed = await openCloseConfirmModal(epic);
    if (!confirmed) return;
    const origText  = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const res  = await fetch('/api/positions/close/' + positionId, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            btn.textContent      = '✓';
            btn.style.background = '#16803c';
            btn.style.color      = '#f0fdf4';
            const pnlStr = data.pnl !== undefined ? ' P&L ' + (data.pnl >= 0 ? '+' : '') + data.pnl.toFixed(2) + '€' : '';
            showToast('Position closed', epic + ' @ ' + data.level + pnlStr, 'success');
            // Disable the row to prevent double-close
            btn.closest('tr').style.opacity = '0.45';
        } else {
            btn.textContent      = '✗';
            btn.style.background = '#991b1b';
            btn.style.color      = '#fef2f2';
            showToast('Close failed', data.error || 'Could not close position', 'error');
            setTimeout(function() {
                btn.textContent      = origText;
                btn.style.background = '';
                btn.style.color      = '';
                btn.disabled         = false;
            }, 5000);
        }
    } catch(e) {
        btn.textContent      = '✗';
        btn.style.background = '#991b1b';
        btn.style.color      = '#fef2f2';
        showToast('Close failed', e.message, 'error');
        setTimeout(function() {
            btn.textContent      = origText;
            btn.style.background = '';
            btn.style.color      = '';
            btn.disabled         = false;
        }, 5000);
    }
}

// ── Chart modal ──────────────────────────────────────────────────────────────

// Convert a UTC ISO string (e.g. "2026-06-08T08:30:00+00:00") to a naive local
// datetime string in Europe/Paris (e.g. "2026-06-08T10:30:00") so that Plotly
// displays the correct French hour without applying any extra offset.
function _toParisNaive(utcISOStr) {
    try {
        const d = new Date(utcISOStr);
        const parts = new Intl.DateTimeFormat('en-CA', {
            timeZone: 'Europe/Paris',
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit',
            hour12: false,
        }).formatToParts(d);
        const p = {};
        parts.forEach(function({type, value}) { p[type] = value; });
        return p.year + '-' + p.month + '-' + p.day + 'T' + p.hour + ':' + p.minute + ':' + p.second;
    } catch (_) {
        return utcISOStr;
    }
}

// openChartModal(epic) renders the price curve. When called from a position row
// it also receives an `overlay` object with the position levels/markers:
//   { open, zero, stop, target, close, openTime, closeTime }
// — numeric price levels are drawn as horizontal reference lines and the
// entry/exit times (UTC ISO strings) as vertical markers on the time axis.
async function openChartModal(epic, overlay) {
    const modal     = document.getElementById('chart-modal');
    const titleEl   = document.getElementById('chart-modal-title');
    const container = document.getElementById('chart-container');
    titleEl.innerHTML = '<i data-lucide="trending-up" class="lc-icon"></i> ' + epic;
    lucide.createIcons();
    container.innerHTML = '<div style="color:#64748b;padding:3rem;text-align:center;">Loading…</div>';
    modal.style.display = 'block';
    try {
        const res = await fetch('/api/prices/' + encodeURIComponent(epic));
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (!data.bid_closes || !data.bid_closes.length) {
            container.innerHTML = '<div style="color:#64748b;padding:3rem;text-align:center;">No price data available yet.</div>';
            return;
        }
        const rawBids = data.bid_closes;
        const utcTimestamps = data.timestamps && data.timestamps.length ? data.timestamps : null;
        // Convert UTC → Paris naive strings so Plotly displays the correct local hour.
        const timestamps = utcTimestamps
            ? utcTimestamps.map(_toParisNaive)
            : rawBids.map(function(_, i) { return i + 1; });
        const xIsDate = utcTimestamps !== null;

        // Normalisation bounds: include the overlay price levels so their lines
        // stay inside the [0, 100]% view even when stop/target sit outside the
        // recent bid range.
        const ov = overlay || {};
        let lo = Math.min.apply(null, rawBids);
        let hi = Math.max.apply(null, rawBids);
        ['open', 'zero', 'stop', 'target', 'close'].forEach(function(k) {
            const v = ov[k];
            if (typeof v === 'number' && isFinite(v)) {
                if (v < lo) lo = v;
                if (v > hi) hi = v;
            }
        });
        const range = hi - lo;
        const toPct = function(v) { return range === 0 ? 50 : (v - lo) / range * 100; };
        const pctY = rawBids.map(toPct);

        const traces = [{
            x: timestamps,
            y: pctY,
            customdata: rawBids,
            type: 'scatter',
            mode: 'lines',
            line: { color: '#E07B39', width: 1.5 },
            name: 'Bid close',
            hovertemplate: 'Bid: %{customdata:.4f}<br>%{y:.1f}<extra></extra>'
        }];
        const shapes = [];
        const annotations = [];

        // Horizontal reference line + right-anchored price label for one level.
        function addLevelLine(value, color, dash, label) {
            if (typeof value !== 'number' || !isFinite(value)) return;
            const y = toPct(value);
            shapes.push({
                type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: y, y1: y,
                line: { color: color, width: 1.2, dash: dash }
            });
            annotations.push({
                xref: 'paper', x: 1, xanchor: 'right', yref: 'y', y: y, yanchor: 'bottom',
                text: label + ' ' + value.toFixed(4), showarrow: false,
                font: { color: color, size: 10 }, bgcolor: 'rgba(28,23,20,0.7)'
            });
        }
        addLevelLine(ov.target, '#4ade80', 'solid', 'Target');
        addLevelLine(ov.open,   '#cbd5e1', 'solid', 'Open');
        // Break-even (0€, spread recovered) — dotted; skip if it coincides with open.
        if (typeof ov.zero === 'number' && isFinite(ov.zero) &&
            !(typeof ov.open === 'number' && Math.abs(ov.zero - ov.open) < 1e-6)) {
            addLevelLine(ov.zero, '#94a3b8', 'dot', 'Break-even 0€');
        }
        addLevelLine(ov.stop, '#ef4444', 'solid', 'Stop');

        // Vertical time marker + diamond point for an entry/exit event.
        function addEventMarker(timeStr, value, color, label) {
            if (!xIsDate || !timeStr || typeof value !== 'number' || !isFinite(value)) return;
            const xParis = _toParisNaive(timeStr);
            shapes.push({
                type: 'line', xref: 'x', x0: xParis, x1: xParis, yref: 'paper', y0: 0, y1: 1,
                line: { color: color, width: 1, dash: 'dash' }
            });
            traces.push({
                x: [xParis], y: [toPct(value)], customdata: [value],
                type: 'scatter', mode: 'markers', name: label,
                marker: { color: color, size: 9, symbol: 'diamond', line: { color: '#1c1714', width: 1 } },
                hovertemplate: label + ': %{customdata:.4f}<extra></extra>'
            });
        }
        addEventMarker(ov.openTime, ov.open, '#E0B341', 'Entry');
        addEventMarker(ov.closeTime, ov.close, '#60a5fa', 'Exit');

        Plotly.newPlot(container, traces, {
            paper_bgcolor: '#1c1714',
            plot_bgcolor: '#1c1714',
            font: { color: '#94a3b8', size: 11 },
            margin: { l: 55, r: 20, t: 10, b: 50 },
            showlegend: false,
            shapes: shapes,
            annotations: annotations,
            xaxis: {
                gridcolor: '#2d2319',
                zerolinecolor: '#2d2319',
                type: xIsDate ? 'date' : 'linear',
                tickformat: xIsDate ? '%H:%M' : '',
                title: ''
            },
            yaxis: {
                gridcolor: '#2d2319',
                zerolinecolor: '#2d2319',
                ticksuffix: '%',
                range: [-3, 103],
                title: ''
            },
        }, { responsive: true, displayModeBar: false });
    } catch(e) {
        container.innerHTML = '<div style="color:#ef4444;padding:3rem;text-align:center;">Failed to load data: ' + e.message + '</div>';
    }
}

function closeChartModal() {
    document.getElementById('chart-modal').style.display = 'none';
    Plotly.purge(document.getElementById('chart-container'));
}

// ── Server log filter ─────────────────────────────────────────────────────────
let _currentLogFilter = 'all';

function filterLogs(level, _btn) {
    _currentLogFilter = level;
    _reapplyLogFilter();
}

function _reapplyLogFilter() {
    document.querySelectorAll('.log-filter-btn').forEach(function(btn) {
        btn.classList.toggle('active', btn.dataset.level === _currentLogFilter);
    });
    document.querySelectorAll('.log-row').forEach(function(row) {
        const show = _currentLogFilter === 'all' || row.dataset.level === _currentLogFilter;
        row.style.display = show ? '' : 'none';
    });
}

lucide.createIcons();

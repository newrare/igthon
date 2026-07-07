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

// The open / stop / close selection is set in .env (the single source of truth)
// and shown read-only in the title bar — there is no runtime switching here.

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

// Wallet resync — force an immediate GET /accounts so the balance reflects a
// manual demo top-up done on the IG web platform (IG has no REST reset endpoint).
async function resyncWallet(btn) {
    if (btn) { btn.classList.add('spinning'); btn.disabled = true; }
    try {
        const res  = await fetch('/api/wallet/resync', { method: 'POST' });
        const data = await res.json().catch(function() { return {}; });
        if (res.ok) {
            const val = (typeof data.available === 'number')
                ? data.available.toLocaleString('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + '€'
                : '—';
            showToast('Wallet resynced', 'Available: ' + val, 'success');
        } else {
            showToast('Wallet resync failed', data.error || 'Unknown error', 'error');
        }
    } catch (e) {
        console.error('Wallet resync failed', e);
        showToast('Wallet resync failed', 'Network error', 'error');
    } finally {
        // The KPI fragment re-renders on the next 1 s poll and replaces this
        // button, so clearing the spin state here is only for the no-poll case.
        if (btn) { btn.classList.remove('spinning'); btn.disabled = false; }
    }
}

async function clearQueueErrors() {
    try {
        await fetch('/api/queue/errors/clear', { method: 'POST' });
        document.getElementById('queue-err-tbody').innerHTML =
            '<tr><td colspan="7" class="err-empty">No queue errors recorded this session.</td></tr>';
        showToast('Queue error log cleared', null, 'info');
    } catch (e) {
        console.error('Clear queue errors failed', e);
        showToast('Error', 'Failed to clear queue error log', 'error');
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
    // Arrow keys step the chart carousel while the chart modal is open.
    const chartOpen = document.getElementById('chart-modal').style.display !== 'none';
    if (chartOpen && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
        if (e.key === 'ArrowLeft')  chartNavPrev();
        if (e.key === 'ArrowRight') chartNavNext();
        return;
    }
    if (e.key === 'Escape') {
        if (chartOpen) { closeChartModal(); return; }
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

// Epic currently shown in the chart modal — read by the modal's Buy button so
// it opens a position on the right market without re-passing the epic.
let _chartModalEpic = null;

// Carousel state: the ordered list of epics from the table the chart was opened
// from, and our position in it. The left/right arrows step through this list so
// the user can browse every row's curve without closing the modal. Rebuilt from
// the source table on each fresh open (see _buildChartEpicList).
let _chartEpicList  = [];
let _chartEpicIndex = -1;

// True while the user has manually zoomed/panned the chart. The auto-refresh
// repaints with Plotly.react and a fresh (auto-ranged) layout, which would snap
// the view back to full-day; freezing the refresh while zoomed keeps the user's
// selected window stable until they double-click to reset (autorange).
let _chartZoomed = false;

// Auto-refresh handle for the open chart. The 2 s fragment poll only swaps
// dashboard section HTML — it never touches an open chart — so without this the
// curve would freeze at the moment the modal was opened and never show new bids.
let _chartRefreshTimer = null;
// Candles stream in (and are persisted) roughly once per minute, but the latest
// candle's bid moves intra-bar; refresh a few seconds apart so new ticks land
// promptly without hammering the whole-day chart endpoint.
const CHART_REFRESH_MS = 3000;

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

// openChartModal(epic) renders the whole-day price curve for an epic and overlays
// EVERY trade taken on it today. Data comes from /api/chart/{epic}:
//   { candles: [{t, bid}], trades: [{ id, open, zero, stop, target, close,
//                                     openTime, closeTime, pnl }] }
// Each trade draws its break-even (zero)/stop/target reference lines plus a
// labelled entry and exit vertical marker (with the Paris time), so multiple
// open/close cycles on the same epic are all visible at once.
async function openChartModal(epic, evt) {
    const modal = document.getElementById('chart-modal');
    // Snapshot the source table's epics for the carousel before painting.
    _buildChartEpicList(epic, evt);
    _showChartEpic(epic);
    modal.style.display = 'block';
    if (_chartRefreshTimer) { clearInterval(_chartRefreshTimer); _chartRefreshTimer = null; }
    await _loadChart(epic, true);
    // Keep the open chart live: re-fetch and repaint in place so new bids show
    // up without the user closing and reopening the modal — but hold still while
    // the user is zoomed in, so the refresh doesn't reset their selected window.
    _chartRefreshTimer = setInterval(function() {
        if (_chartModalEpic && !_chartZoomed) _loadChart(_chartModalEpic, false);
    }, CHART_REFRESH_MS);
}

// Paint the title/loading state for ``epic`` and reset the zoom freeze. Shared
// by the initial open and by carousel navigation (which reuses the live timer).
function _showChartEpic(epic) {
    const titleEl   = document.getElementById('chart-modal-title');
    const container = document.getElementById('chart-container');
    _chartModalEpic = epic;
    _setChartZoomed(false);
    titleEl.innerHTML = '<i data-lucide="trending-up" class="lc-icon"></i> ' + epic;
    lucide.createIcons();
    container.innerHTML = '<div style="color:#64748b;padding:3rem;text-align:center;">Loading…</div>';
    _updateChartNav();
}

// ── Chart carousel ────────────────────────────────────────────────────────────
// Rebuild the ordered epic list from the table the chart was opened from, so the
// arrows browse exactly the rows the user sees (skipping filter-hidden ones).
function _buildChartEpicList(epic, evt) {
    _chartEpicList = [];
    const src   = evt && (evt.currentTarget || evt.target);
    const scope = src && src.closest ? src.closest('table') : null;
    if (scope) {
        scope.querySelectorAll('tr.clickable-row').forEach(function(tr) {
            if (tr.classList.contains('hidden')) return;  // respect active filter
            const m = /openChartModal\('([^']+)'/.exec(tr.getAttribute('onclick') || '');
            if (m) _chartEpicList.push(m[1]);
        });
    }
    _chartEpicIndex = _chartEpicList.indexOf(epic);
    // Fallback (opened outside a table, or epic not found): a single-item list.
    if (_chartEpicIndex < 0) { _chartEpicList = [epic]; _chartEpicIndex = 0; }
}

// Show/hide the arrows (only when there is more than one epic to browse).
function _updateChartNav() {
    const many = _chartEpicList.length > 1;
    ['chart-nav-prev', 'chart-nav-next'].forEach(function(id) {
        const b = document.getElementById(id);
        if (b) b.style.display = many ? 'flex' : 'none';
    });
}

// Step ``delta`` positions through the carousel, wrapping around at the ends.
function chartNavStep(delta) {
    if (_chartEpicList.length < 2) return;
    const n = _chartEpicList.length;
    _chartEpicIndex = (_chartEpicIndex + delta + n) % n;
    const epic = _chartEpicList[_chartEpicIndex];
    _showChartEpic(epic);
    _loadChart(epic, true);  // the live timer keeps refreshing the new epic
}
function chartNavPrev() { chartNavStep(-1); }
function chartNavNext() { chartNavStep(1); }

// ── Chart zoom freeze ───────────────────────────────────────────────────────
function _setChartZoomed(zoomed) {
    _chartZoomed = zoomed;
    const badge = document.getElementById('chart-paused-badge');
    if (badge) badge.style.display = zoomed ? 'block' : 'none';
}

// Plotly relayout handler: a manual zoom/pan sets an explicit x-range (freeze);
// a double-click restores autorange (resume). Pure resize/autosize events carry
// neither key and are ignored.
function _onChartRelayout(ev) {
    if (!ev) return;
    // Double-click reset restores autorange → resume. A user zoom/pan sets the
    // bracket-notation range keys → freeze. (The full ``xaxis.range`` array key
    // is deliberately not treated as a zoom: it also appears on programmatic
    // autorange/resize, which would falsely freeze the refresh.)
    if (ev['xaxis.autorange'] === true) {
        _setChartZoomed(false);
    } else if (ev['xaxis.range[0]'] !== undefined || ev['xaxis.range[1]'] !== undefined) {
        _setChartZoomed(true);
    }
}

// Attach (idempotently) the zoom listener to a freshly-plotted chart div.
function _attachChartZoomListener(container) {
    if (!container || typeof container.on !== 'function') return;
    if (typeof container.removeAllListeners === 'function') {
        container.removeAllListeners('plotly_relayout');
    }
    container.on('plotly_relayout', _onChartRelayout);
}

// Fetch the whole-day curve for ``epic`` and (re)draw it. ``initial`` paints the
// "Loading…"/error placeholders and uses Plotly.newPlot; a refresh keeps the
// current chart visible and uses Plotly.react for a flicker-free in-place update.
async function _loadChart(epic, initial) {
    const container = document.getElementById('chart-container');
    try {
        const res = await fetch('/api/chart/' + encodeURIComponent(epic));
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        // A slow response that lands after the modal was closed or switched to
        // another epic must not paint over the current view.
        if (_chartModalEpic !== epic) return;
        const candles = data.candles || [];
        if (!candles.length) {
            if (initial) {
                container.innerHTML = '<div style="color:#64748b;padding:3rem;text-align:center;">No price data available yet.</div>';
            }
            return;
        }
        const trades = data.trades || [];
        const rawBids = candles.map(function(c) { return c.bid; });
        // Offer (ask) curve. A long is filled on the offer, so level_open/zero sit
        // on this curve, not the bid one; without it the entry marker floats a
        // spread above the only visible line. Fall back to bid when absent.
        const rawOffers = candles.map(function(c) {
            return (typeof c.offer === 'number') ? c.offer : c.bid;
        });
        // Convert UTC → Paris naive strings so Plotly displays the correct local hour.
        const timestamps = candles.map(function(c) { return _toParisNaive(c.t); });

        // Normalisation bounds: include both curves and every trade's price levels
        // so their lines stay inside the [0, 100]% view even when stop/target sit
        // outside the recent bid range.
        // Noise-adjusted bid: the raw bid minus the per-candle adverse tick-noise
        // band the zone gates test against (server-computed, in price points). Sits
        // just under the bid; where IT clears break-even / margin is where the
        // updaters treat the bid as genuinely in the band / profit zone (not mere
        // bid/offer churn). Falls back to the raw bid when the band is absent.
        const rawNoiseBid = candles.map(function(c) {
            return (typeof c.noise === 'number' && isFinite(c.noise)) ? c.bid - c.noise : c.bid;
        });
        let lo = Math.min(Math.min.apply(null, rawBids), Math.min.apply(null, rawOffers),
                          Math.min.apply(null, rawNoiseBid));
        let hi = Math.max(Math.max.apply(null, rawBids), Math.max.apply(null, rawOffers));
        trades.forEach(function(t) {
            ['open', 'openBid', 'zero', 'margin', 'stopLoose', 'stopFollower', 'target', 'close'].forEach(function(k) {
                const v = t[k];
                if (typeof v === 'number' && isFinite(v)) {
                    if (v < lo) lo = v;
                    if (v > hi) hi = v;
                }
            });
            // Either stop (follower / loose) can ratchet above the initial level,
            // so fold both whole stepped trajectories into the bounds too — else a
            // high stop would be clipped at the top of the view.
            (t.stopsFollower || []).concat(t.stopsLoose || []).forEach(function(pt) {
                const v = pt.level;
                if (typeof v === 'number' && isFinite(v)) {
                    if (v < lo) lo = v;
                    if (v > hi) hi = v;
                }
            });
        });
        const range = hi - lo;
        const toPct = function(v) { return range === 0 ? 50 : (v - lo) / range * 100; };
        const pctY = rawBids.map(toPct);
        const pctYOffer = rawOffers.map(toPct);

        const traces = [{
            x: timestamps,
            y: pctY,
            customdata: rawBids,
            type: 'scatter',
            mode: 'lines',
            line: { color: '#E07B39', width: 1.5 },
            name: 'Bid close',
            hovertemplate: 'Bid: %{customdata:.4f}<br>%{y:.1f}<extra></extra>'
        }, {
            // Offer (ask): a faint dotted line one spread above the bid, so the
            // bid/offer band (and the cost the long paid into it) is visible.
            x: timestamps,
            y: pctYOffer,
            customdata: rawOffers,
            type: 'scatter',
            mode: 'lines',
            line: { color: '#475569', width: 1, dash: 'dot' },
            name: 'Offer close',
            hovertemplate: 'Offer: %{customdata:.4f}<br>%{y:.1f}<extra></extra>'
        }, {
            // Noise-adjusted bid (bid − adverse tick-noise band): a dim orange line
            // just below the bid. When this trace — not the raw bid — clears the
            // Break-even / Margin lines is when the zone updaters stop reading the
            // move as churn, so it explains why a stop does (or does not) ratchet.
            x: timestamps,
            y: rawNoiseBid.map(toPct),
            customdata: rawNoiseBid,
            type: 'scatter',
            mode: 'lines',
            line: { color: 'rgba(224,123,57,0.4)', width: 1, dash: 'dot' },
            name: 'Bid − noise',
            hovertemplate: 'Bid − noise: %{customdata:.4f}<br>%{y:.1f}<extra></extra>'
        }];
        const shapes = [];
        const annotations = [];

        // Horizontal reference line + right-anchored price label for one level.
        // De-duplicated so trades sharing a level don't stack identical lines.
        const _seenLevels = {};
        function addLevelLine(value, color, dash, label) {
            if (typeof value !== 'number' || !isFinite(value)) return;
            const key = label + ':' + value.toFixed(4);
            if (_seenLevels[key]) return;
            _seenLevels[key] = true;
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

        // Candle timestamps as epoch ms (parsed in the browser's local tz; all
        // values use the same convention so differences and round-trips are
        // consistent regardless of which tz that is).
        const _candleMs = timestamps.map(function(t) { return new Date(t).getTime(); });
        const _pad2 = function(n) { return String(n).padStart(2, '0'); };
        const _msToParisNaive = function(ms) {
            const d = new Date(ms);
            return d.getFullYear() + '-' + _pad2(d.getMonth() + 1) + '-' + _pad2(d.getDate()) +
                'T' + _pad2(d.getHours()) + ':' + _pad2(d.getMinutes()) + ':' + _pad2(d.getSeconds());
        };
        // Time at which the bid curve crosses ``target``, nearest to ``refMs``.
        // Markers carry a real price (the open bid / the close fill), but the
        // recorded execution time can sit a candle off during a fast move, leaving
        // the diamond hanging above/below the bid line. We keep the price and slide
        // the marker along X to where the bid actually equals it, dropping the
        // diamond onto the curve (the vertical line shifts with it). Capped to a
        // few minutes so a price that only recurs far away never yanks the marker
        // across the chart. Returns null when no nearby crossing exists.
        const _SNAP_CAP_MS = 6 * 60 * 1000;
        function _snapMsToBid(target, refMs) {
            if (typeof target !== 'number' || !isFinite(target) || _candleMs.length < 2) return null;
            let best = null, bestDist = Infinity;
            for (let i = 1; i < rawBids.length; i++) {
                const a = rawBids[i - 1], b = rawBids[i];
                if (target < Math.min(a, b) || target > Math.max(a, b)) continue;
                const f = (b === a) ? 0 : (target - a) / (b - a);
                const tCross = _candleMs[i - 1] + f * (_candleMs[i] - _candleMs[i - 1]);
                const dist = Math.abs(tCross - refMs);
                if (dist < bestDist) { bestDist = dist; best = tCross; }
            }
            return (best !== null && bestDist <= _SNAP_CAP_MS) ? best : null;
        }

        // Vertical time marker + diamond point + top time label for an event.
        // ``value`` is the real bid-side price (openBid = bid at open, one spread
        // below Break-even; level_close = the sell fill at exit). We snap the
        // marker's X to where the bid curve equals that price so the diamond lands
        // on the curve; the visible label keeps the broker's true execution time.
        // ``estimated`` = the level/time were derived (position closed outside the
        // bot, not a captured fill): drawn as a hollow diamond with an "(est.)"
        // tag so it is not mistaken for a real stop/limit execution.
        function addEventMarker(timeStr, value, color, label, estimated) {
            if (!timeStr || typeof value !== 'number' || !isFinite(value)) return;
            const realHhmm = _toParisNaive(timeStr).slice(11, 16);
            const refMs = new Date(_toParisNaive(timeStr)).getTime();
            const snapped = _snapMsToBid(value, refMs);
            const xParis = snapped !== null ? _msToParisNaive(snapped) : _toParisNaive(timeStr);
            const tag = estimated ? ' (est.)' : '';
            shapes.push({
                type: 'line', xref: 'x', x0: xParis, x1: xParis, yref: 'paper', y0: 0, y1: 1,
                line: { color: color, width: 1, dash: 'dash' }
            });
            annotations.push({
                xref: 'x', x: xParis, yref: 'paper', y: 1, yanchor: 'bottom',
                text: label + ' ' + realHhmm + tag, showarrow: false,
                font: { color: color, size: 10 }, bgcolor: 'rgba(28,23,20,0.7)'
            });
            traces.push({
                x: [xParis], y: [toPct(value)], customdata: [value],
                type: 'scatter', mode: 'markers', name: label,
                marker: estimated
                    ? { color: 'rgba(0,0,0,0)', size: 10, symbol: 'diamond-open',
                        line: { color: color, width: 2 } }
                    : { color: color, size: 9, symbol: 'diamond', line: { color: '#1c1714', width: 1 } },
                hovertemplate: label + ' ' + realHhmm + tag + ': %{customdata:.4f}<extra></extra>'
            });
        }

        // Stepped stop trajectory: one protective stop's real path over the trade
        // — the initial level plus every ratchet update. Each point holds until
        // the next update (Plotly 'hv' = step-after); the last level is carried to
        // the exit (or last candle for a still-open trade) so the line spans the
        // whole trade. Drawn once per stop (the bot software stop and the IG
        // broker stop), each in its own colour/label, replacing the old single
        // flat line that only showed the frozen initial level and never matched
        // the live stop at exit once it had ratcheted up. Returns true when a
        // stepped line was drawn, false to let the caller fall back to a flat one.
        function addStopStep(stops, closeTimeStr, color, label, dash) {
            if (!Array.isArray(stops) || !stops.length) return false;
            const xs = [], ys = [];
            stops.forEach(function(pt) {
                if (typeof pt.level !== 'number' || !isFinite(pt.level)) return;
                xs.push(_toParisNaive(pt.t));
                ys.push(toPct(pt.level));
            });
            if (!xs.length) return false;
            // Carry the last level to the exit so the step spans the trade.
            const endX = closeTimeStr
                ? _toParisNaive(closeTimeStr)
                : timestamps[timestamps.length - 1];
            xs.push(endX);
            ys.push(ys[ys.length - 1]);
            const last = stops[stops.length - 1].level;
            traces.push({
                x: xs, y: ys,
                type: 'scatter', mode: 'lines', name: label,
                line: { color: color, width: 1.2, shape: 'hv', dash: dash || 'solid' },
                hoverinfo: 'skip'
            });
            annotations.push({
                xref: 'paper', x: 1, xanchor: 'right', yref: 'y',
                y: toPct(last), yanchor: 'bottom',
                text: label + ' ' + last.toFixed(4), showarrow: false,
                font: { color: color, size: 10 }, bgcolor: 'rgba(28,23,20,0.7)'
            });
            return true;
        }

        // Close reasons whose exit level/time were derived (the position vanished
        // from IG), not captured from a real fill — flagged as estimated.
        const _ESTIMATED_CLOSE = { closed_externally: 1, not_found_in_ig: 1 };

        // Draw every trade's levels and entry/exit markers.
        trades.forEach(function(t) {
            addLevelLine(t.target, '#4ade80', 'solid', 'Target');
            // Break-even line = the open offer (level_zero) = the bid at open
            // plus the spread. For a long this MUST sit one spread above the
            // entry: you buy on the offer, so the bid has to climb back through
            // the spread before the trade is flat. The Entry diamond is drawn on
            // the bid (openBid = level_zero - spread), so the gap up to this line
            // is exactly the spread; the bid curve reaching it = break-even.
            addLevelLine(t.zero, '#cbd5e1', 'solid', 'Break-even');
            // Margin line = break-even + noise margin (frozen at open): the
            // boundary between the break-even band (zone 2) and real profit
            // (zone 3). Cyan, dashed, to sit visually between break-even and the
            // profit trailing without being mistaken for a stop.
            addLevelLine(t.margin, '#22d3ee', 'dash', 'Margin');
            // Two protective stops, each its own line. Prefer the real stepped
            // path; fall back to a flat line at the scalar level for positions
            // opened before the history was captured.
            //   - Follower (red, solid): the application-side trailing stop that
            //     ratchets up with the market past break-even + margin — the level
            //     a close is actually decided on between two bid polls.
            //   - Loose (violet, dashed): the protective stop resting at the
            //     broker (the gap-safety net). It can sit BELOW the follower (IG's
            //     min-distance rule widened it at open), which is why a close can
            //     fire on the follower while this line is still untouched.
            if (!addStopStep(t.stopsFollower, t.closeTime, '#ef4444', 'Follower', 'solid')) {
                addLevelLine(t.stopFollower, '#ef4444', 'solid', 'Follower');
            }
            if (!addStopStep(t.stopsLoose, t.closeTime, '#a78bfa', 'Loose', 'dot')) {
                addLevelLine(t.stopLoose, '#a78bfa', 'dot', 'Loose');
            }
            addEventMarker(t.openTime, t.openBid, '#E0B341', 'Entry', false);
            addEventMarker(t.closeTime, t.close, '#60a5fa', 'Exit',
                !!_ESTIMATED_CLOSE[t.closeReason]);
        });
        const xIsDate = true;

        const layout = {
            paper_bgcolor: '#1c1714',
            plot_bgcolor: '#1c1714',
            font: { color: '#94a3b8', size: 11 },
            margin: { l: 55, r: 20, t: 22, b: 50 },
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
        };
        const config = { responsive: true, displayModeBar: false };
        // newPlot for the first paint (clears the Loading placeholder); react for
        // refreshes so the chart updates in place without flicker or reset zoom.
        if (initial) {
            Plotly.newPlot(container, traces, layout, config);
            // Freeze the live refresh while the user zooms/pans this fresh plot.
            _attachChartZoomListener(container);
        } else {
            Plotly.react(container, traces, layout, config);
        }
    } catch(e) {
        // Only blank the chart on an initial-load failure; a transient refresh
        // error should leave the last good chart on screen.
        if (initial) {
            container.innerHTML = '<div style="color:#ef4444;padding:3rem;text-align:center;">Failed to load data: ' + e.message + '</div>';
        }
    }
}

function closeChartModal() {
    if (_chartRefreshTimer) { clearInterval(_chartRefreshTimer); _chartRefreshTimer = null; }
    _chartModalEpic = null;
    _chartEpicList  = [];
    _chartEpicIndex = -1;
    _setChartZoomed(false);
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

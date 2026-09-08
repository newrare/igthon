// Market reading panel — the read-only "what is this curve doing?" overlay.
//
// Self-contained on purpose: it builds its own overlay on first use and depends
// on nothing from dashboard.js, so the dashboard, the chart modal and the
// standalone /charts/{epic} page can all open it with one call:
//
//     openReadingModal('CS.D.EURUSD.CFD.IP')
//
// Data comes from GET /api/reading/{epic}, which returns a ready-to-render
// { epic, name, price, minutes, headline: [...], sections: [{title, note, rows}] }.
// Every label, formatted value and colour tone is decided server-side in
// src/web/routes/dashboard/reading_view.py — this file only lays them out, so a
// new measure never needs a JS change.

const READING_REFRESH_MS = 5000;

let _readingEpic = null;
let _readingTimer = null;

// Build the overlay once and keep it in the DOM (hidden) for later opens.
function _readingModal() {
    let modal = document.getElementById('reading-modal');
    if (modal) return modal;

    modal = document.createElement('div');
    modal.id = 'reading-modal';
    modal.className = 'reading-overlay';
    modal.innerHTML =
        '<div class="reading-box">'
        + '<div class="reading-header">'
        + '<h2 id="reading-title">Reading</h2>'
        + '<div class="reading-header-right">'
        + '<span id="reading-status" class="reading-status">loading…</span>'
        + '<button class="reading-close" onclick="closeReadingModal()">✕ Close</button>'
        + '</div></div>'
        + '<div id="reading-headline" class="reading-headline"></div>'
        + '<div id="reading-body" class="reading-body"></div>'
        + '</div>';
    modal.addEventListener('click', function(e) {
        if (e.target === modal) closeReadingModal();
    });
    document.body.appendChild(modal);
    return modal;
}

// Open (or switch) the panel on ``epic`` and keep it live while it is visible.
async function openReadingModal(epic, evt) {
    if (evt && evt.stopPropagation) evt.stopPropagation();
    const modal = _readingModal();
    _readingEpic = epic;
    document.getElementById('reading-title').textContent = epic;
    document.getElementById('reading-headline').innerHTML = '';
    document.getElementById('reading-body').innerHTML =
        '<div class="reading-empty">Loading…</div>';
    modal.style.display = 'flex';
    if (_readingTimer) { clearInterval(_readingTimer); _readingTimer = null; }
    await _loadReading(epic);
    _readingTimer = setInterval(function() {
        if (_readingEpic) _loadReading(_readingEpic);
    }, READING_REFRESH_MS);
}

function closeReadingModal() {
    const modal = document.getElementById('reading-modal');
    if (modal) modal.style.display = 'none';
    if (_readingTimer) { clearInterval(_readingTimer); _readingTimer = null; }
    _readingEpic = null;
}

async function _loadReading(epic) {
    const status = document.getElementById('reading-status');
    try {
        const res = await fetch('/api/reading/' + encodeURIComponent(epic));
        // A late answer for an epic the user has already navigated away from
        // must not overwrite the panel.
        if (_readingEpic !== epic) return;
        if (!res.ok) {
            const body = await res.json().catch(function() { return {}; });
            document.getElementById('reading-body').innerHTML =
                '<div class="reading-empty">' + (body.error || 'No reading available')
                + '</div>';
            if (status) status.textContent = 'unavailable';
            return;
        }
        _renderReading(await res.json());
        if (status) {
            status.textContent = 'live · ' + new Date().toLocaleTimeString('fr-FR');
        }
    } catch (err) {
        if (status) status.textContent = 'error';
    }
}

function _renderReading(data) {
    const title = document.getElementById('reading-title');
    title.textContent = data.name ? data.epic + ' — ' + data.name : data.epic;

    document.getElementById('reading-headline').innerHTML =
        (data.headline || []).map(function(chip) {
            return '<div class="reading-chip">'
                + '<span class="reading-chip-label">' + _esc(chip.label) + '</span>'
                + '<span class="reading-chip-value ' + _tone(chip.tone) + '">'
                + _esc(chip.value) + '</span></div>';
        }).join('');

    document.getElementById('reading-body').innerHTML =
        (data.sections || []).map(_renderSection).join('')
        || '<div class="reading-empty">Nothing measurable yet.</div>';
}

function _renderSection(section) {
    const rows = (section.rows || []).map(function(row) {
        return '<div class="reading-row">'
            + '<div class="reading-row-label">' + _esc(row.label) + '</div>'
            + '<div class="reading-row-value ' + _tone(row.tone) + '">'
            + _esc(row.value) + '</div>'
            + '<div class="reading-row-detail">' + _esc(row.detail || '') + '</div>'
            + '</div>';
    }).join('');
    return '<div class="reading-section">'
        + '<div class="reading-section-title">' + _esc(section.title) + '</div>'
        + (section.note
            ? '<div class="reading-section-note">' + _esc(section.note) + '</div>'
            : '')
        + rows + '</div>';
}

function _tone(tone) {
    return tone ? 'tone-' + tone : '';
}

function _esc(value) {
    const div = document.createElement('div');
    div.textContent = value === null || value === undefined ? '' : String(value);
    return div.innerHTML;
}

// Escape closes the reading panel first (it sits above the chart modal), and the
// event is stopped so the host page's own Escape handler does not also close the
// chart underneath it.
document.addEventListener('keydown', function(e) {
    if (e.key !== 'Escape') return;
    const modal = document.getElementById('reading-modal');
    if (modal && modal.style.display === 'flex') {
        e.stopPropagation();
        closeReadingModal();
    }
}, true);

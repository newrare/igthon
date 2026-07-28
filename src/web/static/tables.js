// tables.js — global click-to-sort for every <table> in the application.
//
// The whole thing is delegated on `document`, so it works uniformly on tables
// that are present at load time (charts, epic list) *and* on tables injected
// later — dashboard poll refreshes, modal data, simulator/backtest results —
// without any per-page wiring. A MutationObserver only adds the visual
// affordance (`.sortable` class + arrow placeholder) to headers as they appear;
// the sorting itself never depends on that class.
//
// Per-cell / per-header opt-ins honoured:
//   - a cell may carry `data-sort="..."` to override the compared value
//     (e.g. a raw float behind a formatted "1.23 €" display);
//   - a header may carry `data-type="num"|"text"` to force the column type,
//     otherwise the type is auto-detected from the cell contents;
//   - a header or table may carry `data-nosort` to opt out entirely.
(function () {
    'use strict';

    // Comparable raw value for a cell: explicit data-sort wins, else the text.
    function cellValue(cell) {
        if (!cell) return '';
        const raw = cell.dataset && cell.dataset.sort !== undefined
            ? cell.dataset.sort
            : cell.textContent;
        return (raw || '').trim();
    }

    // Parse a number out of a display string, stripping currency symbols,
    // percent signs, spaces and thousands separators. Returns null when the
    // value is not numeric (those always sort to the bottom, both directions).
    function asNumber(value) {
        if (value === '' || value === '—' || value === '-') return null;
        let s = value.replace(/[\s ]/g, '');
        // Drop thousands separators (comma or dot before a 3-digit group), then
        // normalise a decimal comma to a dot.
        s = s.replace(/(\d)[,](?=\d{3}(\D|$))/g, '$1').replace(',', '.');
        s = s.replace(/[^0-9eE+\-.]/g, '');
        if (s === '' || s === '-' || s === '+' || s === '.') return null;
        const n = parseFloat(s);
        return Number.isNaN(n) ? null : n;
    }

    // Sample up to a few populated cells to decide whether a column is numeric.
    function columnIsNumeric(rows, col) {
        let seen = 0;
        for (const row of rows) {
            const cell = row.children[col];
            if (!cell) continue;
            const v = cellValue(cell);
            if (v === '' || v === '—' || v === '-') continue;
            if (asNumber(v) === null) return false;
            if (++seen >= 4) break;
        }
        return seen > 0;
    }

    function sortTable(table, col, th) {
        const tbody = table.tBodies[0];
        if (!tbody) return;
        const rows = Array.from(tbody.rows);
        // Nothing to do for a single row or an empty-state placeholder row
        // (a lone cell spanning the whole table).
        if (rows.length < 2) return;
        if (rows.some((r) => r.children.length <= col)) return;

        const forced = th.dataset ? th.dataset.type : undefined;
        const numeric = forced ? forced === 'num' : columnIsNumeric(rows, col);

        const asc = table.__sortCol === col ? !table.__sortAsc : true;
        table.__sortCol = col;
        table.__sortAsc = asc;
        const dir = asc ? 1 : -1;

        rows.sort((a, b) => {
            const av = cellValue(a.children[col]);
            const bv = cellValue(b.children[col]);
            if (numeric) {
                const an = asNumber(av);
                const bn = asNumber(bv);
                if (an === null && bn === null) return 0;
                if (an === null) return 1;   // unknowns always last
                if (bn === null) return -1;
                return (an - bn) * dir;
            }
            return av.localeCompare(bv, undefined, {
                numeric: true,
                sensitivity: 'base',
            }) * dir;
        });
        const frag = document.createDocumentFragment();
        rows.forEach((r) => frag.appendChild(r));
        tbody.appendChild(frag);

        // Refresh the arrow indicators across the header row.
        const headerRow = th.parentElement;
        for (const cell of headerRow.children) {
            const arrow = cell.querySelector('.sort-arrow');
            if (arrow) arrow.textContent = '';
        }
        let arrow = th.querySelector('.sort-arrow');
        if (!arrow) {
            arrow = document.createElement('span');
            arrow.className = 'sort-arrow';
            th.appendChild(arrow);
        }
        arrow.textContent = asc ? '▲' : '▼';
    }

    document.addEventListener('click', function (event) {
        const th = event.target.closest ? event.target.closest('th') : null;
        if (!th || th.dataset.nosort !== undefined) return;
        const thead = th.closest('thead');
        if (!thead) return;
        const table = th.closest('table');
        if (!table || table.dataset.nosort !== undefined) return;
        // Column index = position of the header within its own row.
        const col = Array.prototype.indexOf.call(th.parentElement.children, th);
        sortTable(table, col, th);
    });

    // Give headers the sortable affordance (cursor + arrow placeholder). Uses
    // the header row nearest the body so grouped headers behave sensibly.
    function markHeaders(thead) {
        if (thead.closest('table') && thead.closest('table').dataset.nosort !== undefined) {
            return;
        }
        const row = thead.rows[thead.rows.length - 1];
        if (!row) return;
        for (const th of row.cells) {
            if (th.dataset.nosort !== undefined) continue;
            th.classList.add('sortable');
            if (!th.querySelector('.sort-arrow')) {
                const arrow = document.createElement('span');
                arrow.className = 'sort-arrow';
                th.appendChild(arrow);
            }
        }
    }

    function scan(root) {
        if (!root.querySelectorAll) return;
        root.querySelectorAll('table thead').forEach(markHeaders);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => scan(document));
    } else {
        scan(document);
    }

    // Tables injected after load (modals, poll refreshes) get marked too.
    const observer = new MutationObserver((mutations) => {
        for (const mutation of mutations) {
            for (const node of mutation.addedNodes) {
                if (node.nodeType !== 1) continue;
                if (node.tagName === 'THEAD') markHeaders(node);
                else if (node.tagName === 'TABLE') {
                    node.querySelectorAll('thead').forEach(markHeaders);
                } else if (node.querySelectorAll) {
                    node.querySelectorAll('table thead').forEach(markHeaders);
                }
            }
        }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
})();

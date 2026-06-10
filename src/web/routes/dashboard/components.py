"""Reusable HTML component helpers for the dashboard web layer.

Pure string builders for the UI primitives that were previously copy-pasted with
hardcoded inline styles (modal shells, data tables, cards, buttons). Centralizing
them keeps markup and chrome styling in one place.

Convention: ``label``/``body``/``title`` arguments are treated as trusted HTML —
callers escape user-supplied data themselves (``html.escape``), matching the rest
of the web layer.
"""

# Shared modal chrome — single source of truth for the dark dialog look.
_MODAL_BOX = "background:#1c1714;border:1px solid #4a3a30;border-radius:8px;"
_MODAL_CLOSE_BTN = (
    "background:none;border:1px solid #4a3a30;color:#94a3b8;cursor:pointer;"
    "font-size:0.85rem;padding:0.3rem 0.7rem;border-radius:4px;"
    "display:inline-flex;align-items:center;gap:0.35rem;"
)


def render_button(
    label: str,
    *,
    onclick: str = "",
    cls: str = "",
    style: str = "",
    title: str = "",
    disabled: bool = False,
    attrs: str = "",
) -> str:
    """Render a ``<button>`` with the given attributes (omitting empty ones)."""
    parts: list[str] = []
    if cls:
        parts.append(f'class="{cls}"')
    if style:
        parts.append(f'style="{style}"')
    if onclick:
        parts.append(f'onclick="{onclick}"')
    if title:
        parts.append(f'title="{title}"')
    if disabled:
        parts.append("disabled")
    if attrs:
        parts.append(attrs)
    return f"<button {' '.join(parts)}>{label}</button>"


def render_card(body: str, *, cls: str = "action-card", attrs: str = "") -> str:
    """Render a ``<div>`` card wrapper around pre-built inner HTML."""
    extra = f" {attrs}" if attrs else ""
    return f'<div class="{cls}"{extra}>{body}</div>'


def render_table(
    headers: list[str],
    rows_html: str,
    *,
    cls: str = "err-table",
    style: str = "",
    tbody_id: str = "",
    wrap_scroll: bool = True,
) -> str:
    """Render a ``<table>`` with a ``<thead>`` of ``headers`` and ``rows_html``.

    ``headers`` text is emitted as-is (trusted). When ``wrap_scroll`` is set the
    table is wrapped in a horizontally scrollable container.
    """
    head = "".join(f"<th>{h}</th>" for h in headers)
    tb_id = f' id="{tbody_id}"' if tbody_id else ""
    style_attr = f' style="{style}"' if style else ""
    table = (
        f'<table class="{cls}"{style_attr}>'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody{tb_id}>{rows_html}</tbody>"
        "</table>"
    )
    return f'<div style="overflow-x:auto;">{table}</div>' if wrap_scroll else table


def render_modal(
    *,
    modal_id: str,
    body: str,
    close_fn: str,
    title: str = "",
    title_id: str = "",
    max_width: str = "960px",
    z_index: int = 8500,
    refresh_id: str = "",
) -> str:
    """Render a scrollable overlay modal with a standard header + close button.

    ``title`` is the inner HTML of the header ``<h2>`` (may carry a lucide icon).
    ``refresh_id`` adds the "Live · updated" row used by the live-data modals.
    """
    overlay = (
        f"display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);"
        f"z-index:{z_index};overflow-y:auto;padding:2rem 1rem;"
    )
    box = f"{_MODAL_BOX}max-width:{max_width};width:100%;margin:0 auto;padding:1.5rem;"
    header = ""
    if title:
        tid = f' id="{title_id}"' if title_id else ""
        header = (
            '<div style="display:flex;justify-content:space-between;'
            'align-items:center;margin-bottom:1.2rem;">'
            f'<h2{tid} style="margin:0;color:#E07B39;font-size:1.1rem;'
            f'display:flex;align-items:center;gap:0.4rem;">{title}</h2>'
            f'<button onclick="{close_fn}()" style="{_MODAL_CLOSE_BTN}">'
            '<i data-lucide="x" class="lc-icon"></i> Close</button>'
            "</div>"
        )
    refresh = ""
    if refresh_id:
        refresh = (
            '<div class="modal-refresh-row">Live · updated '
            f'<span id="{refresh_id}">—</span></div>'
        )
    return (
        f'<div id="{modal_id}" onclick="if(event.target===this){close_fn}()" '
        f'style="{overlay}"><div style="{box}">{header}{refresh}{body}</div></div>'
    )


def render_confirm_modal(
    *,
    modal_id: str,
    close_fn: str,
    title: str,
    title_color: str,
    lead_html: str,
    note: str,
    confirm_label: str,
    confirm_style: str,
) -> str:
    """Render a centered confirmation dialog (Cancel + a styled Confirm button).

    ``close_fn`` is called with ``false`` (cancel/overlay) or ``true`` (confirm).
    ``lead_html`` is the trusted first paragraph (carries the target ``<strong>``).
    """
    box = f"{_MODAL_BOX}max-width:420px;width:90%;padding:1.5rem;"
    return (
        f'<div id="{modal_id}" onclick="if(event.target===this){close_fn}(false)" '
        f'style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.72);'
        f'z-index:9000;align-items:center;justify-content:center;">'
        f'<div style="{box}">'
        f'<h2 style="margin:0 0 0.8rem;color:{title_color};'
        f'font-size:1.1rem;">{title}</h2>'
        f"{lead_html}"
        f'<p style="color:#94a3b8;font-size:0.83rem;margin:0 0 1.4rem;">{note}</p>'
        '<div style="display:flex;gap:0.7rem;justify-content:flex-end;">'
        f'<button onclick="{close_fn}(false)" style="background:none;'
        "border:1px solid #4a3a30;color:#94a3b8;cursor:pointer;font-size:0.85rem;"
        'padding:0.4rem 1rem;border-radius:4px;">Cancel</button>'
        f'<button onclick="{close_fn}(true)" '
        f'style="{confirm_style}">{confirm_label}</button>'
        "</div></div></div>"
    )

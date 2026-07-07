"""Format the structured report data into HTML tables (for the email) and plain
text tables (for the LLM prompt + text fallback).

Note on units: `converted_volume` is a physical quantity whose unit differs per
commodity (pounds / bushels / MT), so it is ALWAYS shown per row with its unit and
never summed across commodities. `contracts_traded` (lots) is unitless and may be
totalled across the market.
"""


# ── number helpers ───────────────────────────────────────────────────────────────
def _int(v) -> str:
    return f"{int(v or 0):,}"


def _vol(v, unit) -> str:
    return f"{int(v or 0):,} {unit or ''}".strip()


def _pct(v) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _price(v, unit=None) -> str:
    s = f"{v:,.2f}"
    return f"{s} ({unit})" if unit else s


def _currency(price_unit) -> str:
    """Extract the leading currency token from a price_unit string, e.g.
    'Rs. per 100 kg (delivered Karachi)' -> 'Rs'."""
    if not price_unit:
        return ""
    return price_unit.split()[0].rstrip(".")


def _turnover(v, price_unit) -> str:
    cur = _currency(price_unit)
    return f"{v:,.0f} {cur}".strip()


def fmt_hour(h) -> str:
    return f"{int(h):02d}:00"


def peak_str(peak_hours: list) -> str:
    if not peak_hours:
        return "—"
    return ", ".join(f"{fmt_hour(r['hour_of_day'])} ({_int(r['contracts_traded'])} lots)"
                     for r in peak_hours)


# ── plain-text tables (LLM prompt + fallback) ────────────────────────────────────
def _text_table(headers: list, rows: list) -> str:
    if not rows:
        return "  (no data)"
    widths = [len(h) for h in headers]
    for r in rows:
        widths = [max(w, len(str(c))) for w, c in zip(widths, r)]
    line = lambda cells: "  " + " | ".join(str(c).ljust(w) for c, w in zip(cells, widths))
    out = [line(headers), "  " + "-+-".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


def commodities_text(commodities: list) -> str:
    rows = [[c["commodity_name"], _price(c["avg_price"], c.get("price_unit")),
             _int(c["contracts_traded"]), _vol(c["converted_volume"], c["size_unit"]),
             _pct(c["avg_change_pct"]), _turnover(c["turnover_value"], c.get("price_unit"))]
            for c in commodities]
    return _text_table(["Commodity", "Avg Price", "Contracts", "Volume", "Chg%", "Turnover Value"], rows)


def contracts_text(contracts: list) -> str:
    rows = [[c["contract"], _price(c["avg_price"], c.get("price_unit")),
             _int(c["contracts_traded"]), _vol(c["converted_volume"], c["size_unit"]),
             _pct(c["avg_change_pct"]), _turnover(c["turnover_value"], c.get("price_unit"))]
            for c in contracts]
    return _text_table(["Contract", "Avg Price", "Contracts", "Volume", "Chg%", "Turnover Value"], rows)


# ── HTML tables (email body) ─────────────────────────────────────────────────────
_TH = ('style="text-align:left; padding:7px 10px; font-size:11px; text-transform:uppercase;'
       ' letter-spacing:.04em; color:#fff; background:#2e86c1;"')
_TD = 'style="padding:6px 10px; font-size:13px; color:#2c3e50; border-bottom:1px solid #eaf4fb;"'
_TDR = ('style="padding:6px 10px; font-size:13px; color:#2c3e50; border-bottom:1px solid #eaf4fb;'
        ' text-align:right; white-space:nowrap;"')


def _chg_color(v) -> str:
    if v is None or v == 0:
        return "#7f8c8d"
    return "#1e8449" if v > 0 else "#c0392b"


def _rows_and_cards(rows: list, name_key: str, name_header: str) -> str:
    """Desktop table (horizontal scroll on overflow) + a mobile stacked-card
    fallback, toggled by the media query in render.py's <style> block.

    The mobile cards exist because mail apps often hijack a horizontal swipe
    over an overflow-x:auto table as an app-level gesture (next/prev email,
    archive) instead of scrolling it — so phones get no-scroll-needed cards
    instead of a table that fights the swipe.
    """
    if not rows:
        return '<p style="color:#999; font-size:13px;">No data available.</p>'

    head = "".join(f"<th {_TH}>{h}</th>" for h in
                   [name_header, "Avg Price", "Contracts", "Volume", "Chg%", "Turnover Value"])
    body = ""
    cards = ""
    for r in rows:
        chg = r["avg_change_pct"]
        color = _chg_color(chg)
        name = r[name_key]
        price_unit = r.get("price_unit")
        avg_price = _price(r["avg_price"], price_unit)
        contracts = _int(r["contracts_traded"])
        volume = _vol(r["converted_volume"], r["size_unit"])
        pct = _pct(chg)
        turnover = _turnover(r["turnover_value"], price_unit)
        body += (
            f'<tr><td {_TD}>{name}</td>'
            f'<td {_TDR}>{avg_price}</td>'
            f'<td {_TDR}>{contracts}</td>'
            f'<td {_TDR}>{volume}</td>'
            f'<td {_TDR} style="color:{color}; padding:6px 10px; font-size:13px; border-bottom:1px solid #eaf4fb; text-align:right;">{pct}</td>'
            f'<td {_TDR}>{turnover}</td></tr>'
        )
        cards += (
            '<div style="background:#fff; border:1px solid #eaf4fb; border-radius:8px; padding:10px 12px; margin:0 0 8px;">'
            '<table style="width:100%; border-collapse:collapse;"><tr>'
            f'<td style="font-size:13px; font-weight:bold; color:#154360; padding:0;">{name}</td>'
            f'<td style="font-size:13px; font-weight:bold; text-align:right; padding:0; color:{color};">{pct}</td>'
            '</tr></table>'
            '<table style="width:100%; margin-top:6px; border-collapse:collapse;">'
            f'<tr><td style="padding:2px 0; font-size:11px; color:#888;">Avg Price</td>'
            f'<td style="padding:2px 0; font-size:12px; text-align:right; color:#2c3e50;">{avg_price}</td></tr>'
            f'<tr><td style="padding:2px 0; font-size:11px; color:#888;">Contracts</td>'
            f'<td style="padding:2px 0; font-size:12px; text-align:right; color:#2c3e50;">{contracts}</td></tr>'
            f'<tr><td style="padding:2px 0; font-size:11px; color:#888;">Volume</td>'
            f'<td style="padding:2px 0; font-size:12px; text-align:right; color:#2c3e50;">{volume}</td></tr>'
            f'<tr><td style="padding:2px 0; font-size:11px; color:#888;">Turnover Value</td>'
            f'<td style="padding:2px 0; font-size:12px; text-align:right; color:#2c3e50;">{turnover}</td></tr>'
            '</table></div>'
        )

    return (
        '<div class="pmex-table-scroll" style="width:100%; overflow-x:auto;">'
        f'<table class="pmex-table" style="width:100%; min-width:420px; border-collapse:collapse; margin:8px 0 4px;">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
        f'<div class="pmex-mobile-rows" style="display:none;">{cards}</div>'
    )


def commodities_html(commodities: list) -> str:
    return _rows_and_cards(commodities, "commodity_name", "Commodity")


def contracts_html(contracts: list) -> str:
    return _rows_and_cards(contracts, "contract", "Contract")


def stat_cards_html(ov: dict, extra: str = "") -> str:
    """Stat card row: active contracts, commodities, total lots, avg change.

    Cards are inline-block divs (not a <table><tr> of <td>s) so the media
    query in render.py can reflow them into a 2-column grid on phones instead
    of only being able to stack them full-width.
    """
    def card(label, value, color="#154360"):
        return (f'<div class="pmex-card" style="display:inline-block; vertical-align:top; width:19%;'
                f' min-width:92px; margin:0 0.5% 8px; font-size:14px;">'
                f'<div style="background:#f4faff; border:1px solid #d6eaf8; border-radius:6px; padding:10px 12px;">'
                f'<div style="font-size:18px; font-weight:bold; color:{color};">{value}</div>'
                f'<div style="font-size:10px; text-transform:uppercase; letter-spacing:.05em; color:#888; margin-top:2px;">{label}</div>'
                f'</div></div>')
    chg = ov.get("avg_change_pct")
    cards = (card("Active Contracts", _int(ov.get("active_contracts")))
             + card("Commodities", _int(ov.get("commodities")))
             + card("Contracts Traded", _int(ov.get("contracts_traded")))
             + card("Turnover Value", f"{ov.get('turnover_value') or 0:,.0f} {ov.get('currency', '')}".strip())
             + card("Avg Change", _pct(chg), _chg_color(chg)))
    block = f'<div class="pmex-cards" style="width:100%; margin:6px 0; font-size:0;">{cards}</div>'
    if extra:
        block += f'<p style="margin:2px 0 0; font-size:12px; color:#555;">{extra}</p>'
    return block

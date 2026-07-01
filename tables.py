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
    rows = [[c["commodity_name"], _int(c["contracts_traded"]),
             _vol(c["converted_volume"], c["size_unit"]), _pct(c["avg_change_pct"])]
            for c in commodities]
    return _text_table(["Commodity", "Contracts", "Volume", "Chg%"], rows)


def contracts_text(contracts: list) -> str:
    rows = [[c["contract"], _int(c["contracts_traded"]),
             _vol(c["converted_volume"], c["size_unit"]), _pct(c["avg_change_pct"])]
            for c in contracts]
    return _text_table(["Contract", "Contracts", "Volume", "Chg%"], rows)


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


def _html_table(headers: list, rows: list) -> str:
    if not rows:
        return '<p style="color:#999; font-size:13px;">No data available.</p>'
    head = "".join(f"<th {_TH}>{h}</th>" for h in headers)
    body = ""
    for r in rows:
        cells = ""
        for i, cell in enumerate(r):
            if isinstance(cell, tuple):  # (value, color) for coloured change%
                text, color = cell
                cells += f'<td {_TDR} style="color:{color}; padding:6px 10px; font-size:13px; border-bottom:1px solid #eaf4fb; text-align:right;">{text}</td>'
            else:
                td = _TD if i == 0 else _TDR
                cells += f"<td {td}>{cell}</td>"
        body += f"<tr>{cells}</tr>"
    return (f'<table style="width:100%; border-collapse:collapse; margin:8px 0 4px;">'
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")


def commodities_html(commodities: list) -> str:
    rows = [[c["commodity_name"], _int(c["contracts_traded"]),
             _vol(c["converted_volume"], c["size_unit"]),
             (_pct(c["avg_change_pct"]), _chg_color(c["avg_change_pct"]))]
            for c in commodities]
    return _html_table(["Commodity", "Contracts", "Volume", "Chg%"], rows)


def contracts_html(contracts: list) -> str:
    rows = [[c["contract"], _int(c["contracts_traded"]),
             _vol(c["converted_volume"], c["size_unit"]),
             (_pct(c["avg_change_pct"]), _chg_color(c["avg_change_pct"]))]
            for c in contracts]
    return _html_table(["Contract", "Contracts", "Volume", "Chg%"], rows)


def stat_cards_html(ov: dict, extra: str = "") -> str:
    """Small stat row: active contracts, commodities, total lots, avg change."""
    def card(label, value, color="#154360"):
        return (f'<td style="padding:4px 6px; vertical-align:top;">'
                f'<div style="background:#f4faff; border:1px solid #d6eaf8; border-radius:6px; padding:10px 12px;">'
                f'<div style="font-size:18px; font-weight:bold; color:{color};">{value}</div>'
                f'<div style="font-size:10px; text-transform:uppercase; letter-spacing:.05em; color:#888; margin-top:2px;">{label}</div>'
                f'</div></td>')
    chg = ov.get("avg_change_pct")
    cards = (card("Active Contracts", _int(ov.get("active_contracts")))
             + card("Commodities", _int(ov.get("commodities")))
             + card("Contracts Traded", _int(ov.get("contracts_traded")))
             + card("Avg Change", _pct(chg), _chg_color(chg)))
    table = f'<table style="width:100%; border-collapse:collapse; margin:6px 0;"><tr>{cards}</tr></table>'
    if extra:
        table += f'<p style="margin:2px 0 0; font-size:12px; color:#555;">{extra}</p>'
    return table

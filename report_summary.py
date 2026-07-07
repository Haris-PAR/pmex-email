"""
PMEX Local Agriculture (Phy_Agri) — Range Report

Builds a detailed PDF report + a natural-language HTML/plain email, covering
every day of data currently in the database (MIN(fetch_date) -> today), and
sends it over SMTP with the PDF attached.

Metric definition (per contract, per day):
    avg_price       = AVG(close_price) across that contract's snapshots that day,
                       restricted to ACTIVE snapshots (bid > 0 OR ask > 0). Quoted
                       per a small reference unit, e.g. "Rs. per 40 kg".
    contracts_traded = end-of-day cumulative total_volume (lots) for that contract/day
                       (last snapshot of the day — total_volume is cumulative intraday).
    volume           = end-of-day cumulative commodity_volume (physical units, e.g. MT).
    turnover_value   = true Rupee notional value traded: avg_price * (volume in kg /
                       kg-per-price-unit parsed from price_unit). NOT avg_price *
                       contracts_traded — a lot is a fixed physical size (e.g. 10 MT)
                       that has nothing to do with the price's own quotation unit, so
                       multiplying price directly by lot count understates true value
                       by exactly that (lot-size / price-unit) factor.

These per (contract, day) rows are then rolled up two ways:
    1. Overall  — summed/averaged across the whole date range, per contract.
    2. Daily    — summed across contracts, for each individual day.

The LLM only writes prose narrative from these already-computed numbers (first
an overview of the whole range, then one sentence per day) — it never computes
or restates the figures itself.

Usage:
  python report_summary.py                 # compute range, build PDF, send email
  python report_summary.py --dry-run        # write report.pdf + preview.html locally, no email
"""

import argparse
import io
import re
import smtplib
import sys
from collections import defaultdict
from datetime import date, datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg2.extras
from langchain_groq import ChatGroq
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from config import (
    EMAIL_FROM,
    EMAIL_TO,
    GROQ_API_KEY,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_SERVER,
    SMTP_USERNAME,
    log,
)
from db import get_connection

CATEGORY = "Phy_Agri"
SECTOR_LABEL = "Local Agriculture (Domestic Physical)"
ACTIVE = "(COALESCE(bid,0) > 0 OR COALESCE(ask,0) > 0)"


# ── Data layer ───────────────────────────────────────────────────────────────
def resolve_range(conn) -> tuple[date, date]:
    """Whole available history for the sector: MIN(fetch_date) -> today."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MIN(fetch_date), CURRENT_DATE FROM crop_snapshots WHERE category = '{CATEGORY}';"
        )
        start, end = cur.fetchone()
    if start is None:
        raise RuntimeError(f"No '{CATEGORY}' rows in crop_snapshots.")
    return start, end


_KG_PER_UNIT_RE = re.compile(r"per\s+([\d,]+(?:\.\d+)?)\s*kg", re.IGNORECASE)


def _kg_per_price_unit(price_unit: str) -> float | None:
    """Parse the kg count out of a price_unit string, e.g. 'Rs. per 100 kg
    (delivered Karachi)' -> 100.0, 'Rs. per 40 kg' -> 40.0. None if unparseable.
    """
    if not price_unit:
        return None
    m = _KG_PER_UNIT_RE.search(price_unit)
    return float(m.group(1).replace(",", "")) if m else None


def fetch_contract_day_rows(conn, start: date, end: date) -> list[dict]:
    """One row per (contract, day): avg_price, contracts_traded, volume, turnover_value.

    turnover_value is the true Rupee notional value traded: avg_price is quoted
    per a small reference unit (e.g. "Rs. per 40 kg"), while `volume` (MT) is the
    full physical quantity — so turnover_value = avg_price * (volume in kg /
    price-unit kg), NOT avg_price * contracts_traded (lots). Lots are a fixed
    physical size per contract (e.g. 10 MT) that has nothing to do with the
    price's own quotation unit, so multiplying price directly by lot count
    understates true notional value by exactly that (lot-size / price-unit) factor.
    price_x_lots (avg_price * contracts_traded) is kept alongside purely so
    aggregation can still recover a lots-weighted average price across days.
    """
    sql = f"""
        WITH day_avg AS (
            SELECT contract, commodity_name, size_unit, price_unit, fetch_date,
                   AVG(close_price) AS avg_price
            FROM crop_snapshots
            WHERE category = '{CATEGORY}' AND fetch_date BETWEEN %(start)s AND %(end)s AND {ACTIVE}
            GROUP BY contract, commodity_name, size_unit, price_unit, fetch_date
        ),
        day_last AS (
            SELECT DISTINCT ON (contract, fetch_date)
                contract, fetch_date, total_volume, commodity_volume
            FROM crop_snapshots
            WHERE category = '{CATEGORY}' AND fetch_date BETWEEN %(start)s AND %(end)s AND {ACTIVE}
            ORDER BY contract, fetch_date, fetch_time DESC
        )
        SELECT
            da.fetch_date, da.contract, da.commodity_name, da.size_unit, da.price_unit,
            da.avg_price,
            COALESCE(dl.total_volume, 0)     AS contracts_traded,
            COALESCE(dl.commodity_volume, 0) AS volume,
            da.avg_price * COALESCE(dl.total_volume, 0) AS price_x_lots
        FROM day_avg da
        JOIN day_last dl USING (contract, fetch_date)
        ORDER BY da.fetch_date, da.contract;
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, {"start": start, "end": end})
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        kg_per_unit = _kg_per_price_unit(r["price_unit"])
        if kg_per_unit:
            # size_unit is always MT for this sector's contracts (checked against
            # the live schema); this converts to kg to match the price's own unit.
            units_traded = (r["volume"] * 1000) / kg_per_unit
            r["turnover_value"] = r["avg_price"] * units_traded
        else:
            log.warning("Could not parse kg/unit from price_unit %r; falling back to price*lots.", r["price_unit"])
            r["turnover_value"] = r["price_x_lots"]

    log.info("Fetched %d contract-day rows for %s -> %s.", len(rows), start, end)
    return rows


# ── Aggregation (plain Python, deterministic) ───────────────────────────────
def aggregate_overall(rows: list[dict]) -> list[dict]:
    """Roll up each contract across the whole date range."""
    by_contract = defaultdict(lambda: {
        "contract": None, "commodity_name": None, "size_unit": None, "price_unit": None,
        "contracts_traded": 0, "volume": 0.0, "turnover_value": 0.0, "_price_x_lots": 0.0, "days_traded": 0,
    })
    for r in rows:
        a = by_contract[r["contract"]]
        a["contract"] = r["contract"]
        a["commodity_name"] = r["commodity_name"]
        a["size_unit"] = r["size_unit"]
        a["price_unit"] = r["price_unit"]
        a["contracts_traded"] += r["contracts_traded"]
        a["volume"] += r["volume"]
        a["turnover_value"] += r["turnover_value"]
        a["_price_x_lots"] += r["price_x_lots"]
        if r["contracts_traded"] > 0:
            a["days_traded"] += 1
    out = []
    for a in by_contract.values():
        # avg_price is a lots-weighted mean of the quoted price (NOT turnover_value
        # / contracts_traded — turnover_value is true notional Rs, a different scale).
        a["avg_price"] = (a["_price_x_lots"] / a["contracts_traded"]) if a["contracts_traded"] else 0.0
        del a["_price_x_lots"]
        out.append(a)
    out.sort(key=lambda a: a["turnover_value"], reverse=True)
    return out


def aggregate_by_commodity(overall: list[dict]) -> list[dict]:
    """Roll up the per-contract totals further, one row per commodity."""
    by_commodity = defaultdict(lambda: {
        "commodity_name": None, "size_unit": None, "contracts_traded": 0,
        "volume": 0.0, "turnover_value": 0.0, "_price_units": set(), "_price_x_lots": 0.0,
    })
    for a in overall:
        c = by_commodity[a["commodity_name"]]
        c["commodity_name"] = a["commodity_name"]
        c["size_unit"] = a["size_unit"]
        c["contracts_traded"] += a["contracts_traded"]
        c["volume"] += a["volume"]
        c["turnover_value"] += a["turnover_value"]
        c["_price_x_lots"] += a["avg_price"] * a["contracts_traded"]
        if a["price_unit"]:
            c["_price_units"].add(a["price_unit"])
    out = []
    for c in by_commodity.values():
        c["avg_price"] = (c["_price_x_lots"] / c["contracts_traded"]) if c["contracts_traded"] else 0.0
        del c["_price_x_lots"]
        # Same rule as grand_totals: only show a blended avg price when every
        # contract under this commodity quotes on the same price basis.
        c["price_unit"] = c["_price_units"].pop() if len(c["_price_units"]) == 1 else None
        del c["_price_units"]
        out.append(c)
    out.sort(key=lambda c: c["turnover_value"], reverse=True)
    return out


def aggregate_daily(rows: list[dict]) -> list[dict]:
    """Group contract-day rows under each calendar day, with a daily total."""
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["fetch_date"]].append(r)
    out = []
    for d in sorted(by_day):
        day_rows = sorted(by_day[d], key=lambda r: r["contract"])
        contracts_traded = sum(r["contracts_traded"] for r in day_rows)
        volume = sum(r["volume"] for r in day_rows)
        turnover_value = sum(r["turnover_value"] for r in day_rows)
        out.append({
            "fetch_date": d,
            "rows": day_rows,
            "contracts_traded": contracts_traded,
            "volume": volume,
            "turnover_value": turnover_value,
        })
    return out


def grand_totals(overall: list[dict]) -> dict:
    contracts_traded = sum(a["contracts_traded"] for a in overall)
    turnover_value = sum(a["turnover_value"] for a in overall)
    # avg_price is a lots-weighted mean of the quoted price (NOT turnover_value /
    # contracts_traded — turnover_value is true notional Rs, a different scale
    # from "price per small reference unit" x "lot count").
    price_x_lots = sum(a["avg_price"] * a["contracts_traded"] for a in overall)
    price_units = {a["price_unit"] for a in overall if a["price_unit"]}
    # Avg price is only meaningful as a single blended figure when every
    # contract quotes on the same basis (e.g. all "Rs. per 100 kg"); otherwise
    # dividing turnover by lots mixes incompatible bases.
    uniform_price_unit = price_units.pop() if len(price_units) == 1 else None
    currencies = {_currency(a["price_unit"]) for a in overall if a["price_unit"]}
    return {
        "contracts_traded": contracts_traded,
        "volume_by_unit": _sum_volume_by_unit(overall),
        "turnover_value": turnover_value,
        "avg_price": (price_x_lots / contracts_traded) if contracts_traded else 0.0,
        "avg_price_unit": uniform_price_unit,
        "currency": currencies.pop() if len(currencies) == 1 else "",
        "commodities": len({a["commodity_name"] for a in overall}),
        "contracts": len(overall),
    }


def _sum_volume_by_unit(overall: list[dict]) -> dict:
    out = defaultdict(float)
    for a in overall:
        out[a["size_unit"] or ""] += a["volume"]
    return dict(out)


# ── formatting helpers ───────────────────────────────────────────────────────
def _n(v) -> str:
    return f"{v:,.0f}"


def _price(v, unit=None) -> str:
    s = f"{v:,.2f}"
    return f"{s} ({unit})" if unit else s


def _vol(v, unit) -> str:
    return f"{v:,.1f} {unit or ''}".strip()


def _currency(price_unit) -> str:
    """Extract the leading currency token from a price_unit string, e.g.
    'Rs. per 100 kg (delivered Karachi)' -> 'Rs'."""
    if not price_unit:
        return ""
    return price_unit.split()[0].rstrip(".")


def _turnover(v, price_unit) -> str:
    cur = _currency(price_unit)
    return f"{v:,.0f} {cur}".strip()


# ── LLM narrative ────────────────────────────────────────────────────────────
def build_prompt(start: date, end: date, overall: list[dict], daily: list[dict], grand: dict) -> str:
    overall_lines = "\n".join(
        f"  {a['contract']} ({a['commodity_name']}): avg price {_price(a['avg_price'], a['price_unit'])}, "
        f"contracts traded {_n(a['contracts_traded'])} lots, volume {_vol(a['volume'], a['size_unit'])}, "
        f"turnover value {_turnover(a['turnover_value'], a['price_unit'])}"
        for a in overall
    )
    daily_lines = "\n".join(
        f"  {d['fetch_date']}: contracts traded {_n(d['contracts_traded'])} lots, "
        f"turnover value {_turnover(d['turnover_value'], d['rows'][0]['price_unit'] if d['rows'] else None)}, "
        + ", ".join(f"{r['contract']}={_price(r['avg_price'], r['price_unit'])}" for r in d["rows"])
        for d in daily
    )
    volume_totals = ", ".join(f"{_n(v)} {unit}" for unit, v in grand["volume_by_unit"].items() if unit)
    return f"""You are a commodity market analyst for PMEX (Pakistan Mercantile Exchange), covering
the {SECTOR_LABEL} sector. Below are pre-computed, ACCURATE figures for every trading
day from {start} to {end}. "Turnover value" = the true Rupee notional value traded that
contract/day (average price times the actual physical quantity traded, not the raw lot
count) — this is the key figure to discuss.

Every figure below has a unit (lots for contracts traded, MT/kg etc. for volume, Rs. per
X kg for price, Rs. for turnover value). ALWAYS include the correct unit next to every
number you mention in the prose — never state a bare, unitless number.

Write output in EXACTLY this format, nothing else:
===OVERVIEW===
<3-4 sentence plain-prose paragraph covering the WHOLE period {start} to {end}: overall
direction of prices, which contract/commodity had the most turnover, total contracts
traded, total volume, and any notable trend across the range — with units on every
figure. No markdown, no bullet points.>
===DAILY===
{start}: <one short plain sentence on that day's price/volume story, with units>
...(one line per date, in order, one sentence each, prefixed by the exact date)...
{end}: <one short plain sentence on that day's price/volume story, with units>

Do not invent numbers beyond what is given below. If a contract traded zero contracts
on a day, you may note it as inactive rather than ignoring it.

============ OVERALL TOTALS ({start} -> {end}) ============
Total contracts traded: {_n(grand['contracts_traded'])} lots | Total volume: {volume_totals} | Total turnover value: {_turnover(grand['turnover_value'], grand.get('avg_price_unit'))} | Commodities: {grand['commodities']}
Per contract:
{overall_lines}

============ PER-DAY FIGURES ============
{daily_lines}

Write the OVERVIEW and DAILY sections now:"""


def get_summaries(prompt: str, daily: list[dict]) -> tuple[str, dict]:
    """Returns (overview_text, {date: sentence})."""
    fallback = ("Summary unavailable.", {})
    try:
        llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0.0, api_key=GROQ_API_KEY)
        content = llm.invoke(prompt).content
        log.info("LLM response received (%d chars).", len(content))
    except Exception as exc:
        log.error("LLM call failed: %s", exc)
        return fallback

    parts = re.split(r"===\s*(OVERVIEW|DAILY)\s*===", content, flags=re.IGNORECASE)
    sections = {}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip().lower()] = parts[i + 1].strip()

    overview = sections.get("overview", "").strip() or "Summary unavailable."
    daily_text = sections.get("daily", "")
    daily_map = {}
    for d in daily:
        ds = str(d["fetch_date"])
        m = re.search(rf"{re.escape(ds)}\s*:\s*(.+)", daily_text)
        if m:
            daily_map[ds] = m.group(1).strip().splitlines()[0].strip()
    return overview, daily_map


# ── PDF ──────────────────────────────────────────────────────────────────────
def build_pdf(start: date, end: date, overall: list[dict], by_commodity: list[dict], daily: list[dict],
              grand: dict, overview_text: str, daily_map: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=16 * mm, leftMargin=16 * mm, rightMargin=16 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#154360"))
    h2 = ParagraphStyle("H2X", parent=styles["Heading2"], textColor=colors.HexColor("#154360"), spaceBefore=14, spaceAfter=4)
    h3 = ParagraphStyle("H3X", parent=styles["Heading3"], textColor=colors.HexColor("#2e86c1"), spaceBefore=10, spaceAfter=2, fontSize=11)
    body = ParagraphStyle("BodyX", parent=styles["BodyText"], fontSize=10, leading=14)
    small = ParagraphStyle("SmallX", parent=styles["BodyText"], fontSize=8, textColor=colors.grey)
    cell = ParagraphStyle("CellX", parent=styles["BodyText"], fontSize=8, leading=9.5)
    cell_r = ParagraphStyle("CellXR", parent=cell, alignment=TA_RIGHT)
    head_cell = ParagraphStyle("HeadCellX", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
    head_cell_r = ParagraphStyle("HeadCellXR", parent=head_cell, alignment=TA_RIGHT)

    def _c(text, right=False):
        """Wrap table cell text in a Paragraph so long strings (e.g. price units) wrap
        inside the column instead of overflowing into the next one."""
        return Paragraph(str(text), cell_r if right else cell)

    def _h(text, right=False):
        return Paragraph(str(text), head_cell_r if right else head_cell)

    header_row = [_h("Contract"), _h("Commodity"), _h("Avg Price", right=True),
                  _h("Contracts Traded (lots)", right=True), _h("Volume", right=True),
                  _h("Turnover Value", right=True)]

    story = [
        Paragraph("PMEX Market Report", title_style),
        Paragraph(f"{SECTOR_LABEL} &bull; {start} to {end}", body),
        Spacer(1, 10),
    ]

    # Overview section
    story.append(Paragraph("Overview", h2))
    story.append(Paragraph(overview_text.replace("\n", "<br/>"), body))
    story.append(Spacer(1, 6))

    grand_table_data = [["Metric", "Value"]] + [
        ["Date range", f"{start} to {end}"],
        ["Trading days", str(len(daily))],
        ["Commodities", str(grand["commodities"])],
        ["Contracts (instruments)", str(grand["contracts"])],
        ["Total contracts traded (lots)", _n(grand["contracts_traded"])],
        ["Total turnover value", f"{_n(grand['turnover_value'])} {grand['currency']}".strip()],
    ] + [
        [f"Total volume ({unit})", _n(v)] for unit, v in grand["volume_by_unit"].items() if unit
    ]
    t = Table(grand_table_data, colWidths=[70 * mm, 90 * mm])
    t.setStyle(_table_style())
    story.append(t)
    story.append(Spacer(1, 10))

    # 24+34+32+28+26+30 = 174mm, fits the 178mm usable width on A4 with 16mm margins.
    col_widths = [24 * mm, 34 * mm, 32 * mm, 28 * mm, 26 * mm, 30 * mm]
    # 44+38+30+28+34 = 174mm, same page width, one fewer column (no per-contract name).
    commodity_col_widths = [44 * mm, 38 * mm, 30 * mm, 28 * mm, 34 * mm]
    commodity_header_row = [_h("Commodity"), _h("Avg Price", right=True),
                             _h("Contracts Traded (lots)", right=True), _h("Volume", right=True),
                             _h("Turnover Value", right=True)]

    # Commodity-wise rollup (contracts of the same commodity summed together)
    story.append(Paragraph("Commodity Totals (Whole Period)", h2))
    rows = [commodity_header_row]
    for c in by_commodity:
        rows.append([
            _c(c["commodity_name"]),
            _c(_price(c["avg_price"], c["price_unit"]), right=True),
            _c(_n(c["contracts_traded"]), right=True),
            _c(_vol(c["volume"], c["size_unit"]), right=True),
            _c(_turnover(c["turnover_value"], c["price_unit"]), right=True),
        ])
    rows.append([
        "Grand Total",
        _price(grand["avg_price"], grand["avg_price_unit"]) if grand["avg_price_unit"] else "—",
        _n(grand["contracts_traded"]),
        ", ".join(f"{_n(v)} {unit}" for unit, v in grand["volume_by_unit"].items() if unit),
        f"{_n(grand['turnover_value'])} {grand['currency']}".strip(),
    ])
    t = Table(rows, colWidths=commodity_col_widths, repeatRows=1)
    style = _table_style()
    style.add("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")
    style.add("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eaf4fb"))
    style.add("ALIGN", (1, 1), (-1, -1), "RIGHT")
    t.setStyle(style)
    story.append(t)
    story.append(Spacer(1, 10))

    # Overall per-contract rollup
    story.append(Paragraph("Per-Contract Totals (Whole Period)", h2))
    rows = [header_row]
    for a in overall:
        rows.append([
            _c(a["contract"]), _c(a["commodity_name"]),
            _c(_price(a["avg_price"], a["price_unit"]), right=True),
            _c(_n(a["contracts_traded"]), right=True),
            _c(_vol(a["volume"], a["size_unit"]), right=True),
            _c(_turnover(a["turnover_value"], a["price_unit"]), right=True),
        ])
    rows.append([
        "", "Grand Total",
        _price(grand["avg_price"], grand["avg_price_unit"]) if grand["avg_price_unit"] else "—",
        _n(grand["contracts_traded"]),
        ", ".join(f"{_n(v)} {unit}" for unit, v in grand["volume_by_unit"].items() if unit),
        f"{_n(grand['turnover_value'])} {grand['currency']}".strip(),
    ])
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    style = _table_style()
    style.add("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")
    style.add("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eaf4fb"))
    style.add("ALIGN", (2, 1), (-1, -1), "RIGHT")
    t.setStyle(style)
    story.append(t)

    # Day-by-day breakdown
    story.append(PageBreak())
    story.append(Paragraph("Day-by-Day Breakdown", h2))
    for d in daily:
        ds = str(d["fetch_date"])
        story.append(Paragraph(ds, h3))
        sentence = daily_map.get(ds)
        if sentence:
            story.append(Paragraph(sentence, body))
        day_currency = _currency(d["rows"][0]["price_unit"]) if d["rows"] else ""
        rows = [header_row]
        for r in d["rows"]:
            rows.append([
                _c(r["contract"]), _c(r["commodity_name"]),
                _c(_price(r["avg_price"], r["price_unit"]), right=True),
                _c(_n(r["contracts_traded"]), right=True),
                _c(_vol(r["volume"], r["size_unit"]), right=True),
                _c(_turnover(r["turnover_value"], r["price_unit"]), right=True),
            ])
        rows.append([
            "", "Daily Total", "", _n(d["contracts_traded"]), _vol(d["volume"], ""),
            f"{_n(d['turnover_value'])} {day_currency}".strip(),
        ])
        t = Table(rows, colWidths=col_widths, repeatRows=1)
        style = _table_style()
        style.add("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")
        style.add("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eaf4fb"))
        style.add("ALIGN", (2, 1), (-1, -1), "RIGHT")
        t.setStyle(style)
        story.append(t)
        story.append(Spacer(1, 6))

    story.append(Spacer(1, 12))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} &bull; "
        "Figures computed from end-of-session PMEX snapshots.", small,
    ))

    doc.build(story)
    return buf.getvalue()


def _table_style() -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2e86c1")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d6eaf8")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4faff")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ])


# ── Email body (HTML + plain) ───────────────────────────────────────────────
def build_html_body(start: date, end: date, overview_text: str, grand: dict, overall: list[dict],
                     by_commodity: list[dict], trading_days: int) -> str:
    def card(label, value):
        return (f'<div class="pmex-card" style="display:inline-block; vertical-align:top; width:19%;'
                f' min-width:92px; margin:0 0.5% 8px; font-size:14px;">'
                f'<div style="background:#f4faff; border:1px solid #d6eaf8;'
                f' border-radius:6px; padding:10px 12px;"><div style="font-size:17px; font-weight:bold;'
                f' color:#154360;">{value}</div><div style="font-size:10px; text-transform:uppercase;'
                f' letter-spacing:.05em; color:#888; margin-top:2px;">{label}</div></div></div>')

    volume_totals = " + ".join(f"{_n(v)} {unit}" for unit, v in grand["volume_by_unit"].items() if unit)
    total_avg_price = (
        _price(grand["avg_price"], grand["avg_price_unit"]) if grand["avg_price_unit"] else "—"
    )
    cards = (card("Contracts Traded (lots)", _n(grand["contracts_traded"]))
             + card("Volume", volume_totals or "—")
             + card("Turnover Value", f"{_n(grand['turnover_value'])} {grand['currency']}".strip())
             + card("Commodities", grand["commodities"])
             + card("Trading Days", trading_days))

    _TD_L = 'style="padding:6px 10px; font-size:13px; border-bottom:1px solid #eaf4fb;"'
    _TD_R = 'style="padding:6px 10px; font-size:13px; text-align:right; border-bottom:1px solid #eaf4fb; white-space:nowrap;"'

    # Mobile substitute for the wide tables: a stacked card per row, so phones
    # never need horizontal scroll (mail apps often hijack that swipe as an
    # app-level gesture — next/prev email — instead of scrolling the table).
    def mobile_card(title, bold, avg_price, contracts_traded, volume, turnover, shaded=False):
        bg = "#eaf4fb" if shaded else "#fff"
        weight = "bold" if bold else "normal"
        _stat = lambda label, value: (
            f'<tr><td style="padding:2px 0; font-size:11px; color:#888;">{label}</td>'
            f'<td style="padding:2px 0; font-size:12px; text-align:right; color:#2c3e50; font-weight:{weight};">{value}</td></tr>'
        )
        return (
            f'<div style="background:{bg}; border:1px solid #eaf4fb; border-radius:8px;'
            f' padding:10px 12px; margin:0 0 8px;">'
            f'<div style="font-size:13px; font-weight:bold; color:#154360;">{title}</div>'
            f'<table style="width:100%; margin-top:4px; border-collapse:collapse;">'
            + _stat("Avg Price", avg_price)
            + _stat("Contracts Traded (lots)", contracts_traded)
            + _stat("Volume", volume)
            + _stat("Turnover Value", turnover)
            + "</table></div>"
        )

    # Commodity-wise rollup (contracts of the same commodity summed together)
    commodity_rows_html = "".join(
        f'<tr><td {_TD_L}>{c["commodity_name"]}</td>'
        f'<td {_TD_R}>{_price(c["avg_price"], c["price_unit"])}</td>'
        f'<td {_TD_R}>{_n(c["contracts_traded"])}</td>'
        f'<td {_TD_R}>{_vol(c["volume"], c["size_unit"])}</td>'
        f'<td {_TD_R}>{_turnover(c["turnover_value"], c["price_unit"])}</td></tr>'
        for c in by_commodity
    )
    commodity_rows_html += (
        f'<tr style="background:#eaf4fb; font-weight:bold;">'
        f'<td {_TD_L}>Total</td>'
        f'<td {_TD_R}>{total_avg_price}</td>'
        f'<td {_TD_R}>{_n(grand["contracts_traded"])}</td>'
        f'<td {_TD_R}>{volume_totals}</td>'
        f'<td {_TD_R}>{_n(grand["turnover_value"])} {grand["currency"]}</td></tr>'
    )
    commodity_mobile_html = "".join(
        mobile_card(
            c["commodity_name"], False,
            _price(c["avg_price"], c["price_unit"]), _n(c["contracts_traded"]),
            _vol(c["volume"], c["size_unit"]), _turnover(c["turnover_value"], c["price_unit"]),
        )
        for c in by_commodity
    )
    commodity_mobile_html += mobile_card(
        "Total", True, total_avg_price, _n(grand["contracts_traded"]),
        volume_totals, f'{_n(grand["turnover_value"])} {grand["currency"]}', shaded=True,
    )

    # Per-contract rollup (whole period, one row per contract)
    rows_html = "".join(
        f'<tr><td {_TD_L}>{a["contract"]}</td>'
        f'<td {_TD_L}>{a["commodity_name"]}</td>'
        f'<td {_TD_R}>{_price(a["avg_price"], a["price_unit"])}</td>'
        f'<td {_TD_R}>{_n(a["contracts_traded"])}</td>'
        f'<td {_TD_R}>{_vol(a["volume"], a["size_unit"])}</td>'
        f'<td {_TD_R}>{_turnover(a["turnover_value"], a["price_unit"])}</td></tr>'
        for a in overall
    )
    rows_html += (
        f'<tr style="background:#eaf4fb; font-weight:bold;">'
        f'<td {_TD_L}>Total</td><td {_TD_L}></td>'
        f'<td {_TD_R}>{total_avg_price}</td>'
        f'<td {_TD_R}>{_n(grand["contracts_traded"])}</td>'
        f'<td {_TD_R}>{volume_totals}</td>'
        f'<td {_TD_R}>{_n(grand["turnover_value"])} {grand["currency"]}</td></tr>'
    )
    mobile_rows_html = "".join(
        mobile_card(
            f'{a["contract"]} <span style="font-weight:normal; color:#888; font-size:11px;">({a["commodity_name"]})</span>',
            False,
            _price(a["avg_price"], a["price_unit"]), _n(a["contracts_traded"]),
            _vol(a["volume"], a["size_unit"]), _turnover(a["turnover_value"], a["price_unit"]),
        )
        for a in overall
    )
    mobile_rows_html += mobile_card(
        "Total", True, total_avg_price, _n(grand["contracts_traded"]),
        volume_totals, f'{_n(grand["turnover_value"])} {grand["currency"]}', shaded=True,
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body, table, td, div, p {{ -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
  @media only screen and (max-width: 600px) {{
    .pmex-container {{ margin: 0 !important; border-radius: 0 !important; width: 100% !important; }}
    .pmex-pad {{ padding-left: 16px !important; padding-right: 16px !important; }}
    .pmex-header {{ padding-left: 16px !important; padding-right: 16px !important; }}
    .pmex-footer {{ padding-left: 16px !important; padding-right: 16px !important; }}
    .pmex-header h1 {{ font-size: 18px !important; }}
    .pmex-card {{ width: 48% !important; margin: 0 1% 8px !important; }}
    .pmex-table-scroll {{ display: none !important; }}
    .pmex-mobile-rows {{ display: block !important; }}
  }}
  @media only screen and (max-width: 360px) {{
    .pmex-card {{ width: 98% !important; margin: 0 1% 8px !important; }}
  }}
</style>
</head>
<body style="margin:0; padding:0; background:#f0f4f8; font-family:Arial,sans-serif;">
<div class="pmex-container" style="max-width:640px; margin:28px auto; background:#fff; border-radius:10px; box-shadow:0 3px 12px rgba(0,0,0,.12); overflow:hidden;">
  <div class="pmex-header" style="background:#154360; padding:24px 28px;">
    <h1 style="color:#fff; margin:0; font-size:21px;">PMEX Market Report</h1>
    <p style="color:#85c1e9; margin:5px 0 0; font-size:13px;">{SECTOR_LABEL} &bull; {start} to {end}</p>
  </div>
  <div class="pmex-pad" style="padding:20px 28px;">
    <div class="pmex-cards" style="width:100%; margin:6px 0; font-size:0;">{cards}</div>
    <p style="margin:14px 0; font-size:14px; line-height:1.7; color:#2c3e50;">{overview_text}</p>
    <p style="margin:16px 0 2px; font-size:12px; font-weight:bold; color:#2e86c1; text-transform:uppercase;">Commodity Wise Summary</p>
    <div class="pmex-table-scroll" style="width:100%; overflow-x:auto;">
    <table class="pmex-table" style="width:100%; min-width:420px; border-collapse:collapse; margin:8px 0 4px;">
      <thead><tr>
        <th style="text-align:left; padding:7px 10px; font-size:11px; text-transform:uppercase; color:#fff; background:#2e86c1;">Commodity</th>
        <th style="text-align:right; padding:7px 10px; font-size:11px; text-transform:uppercase; color:#fff; background:#2e86c1;">Avg Price</th>
        <th style="text-align:right; padding:7px 10px; font-size:11px; text-transform:uppercase; color:#fff; background:#2e86c1;">Contracts Traded (lots)</th>
        <th style="text-align:right; padding:7px 10px; font-size:11px; text-transform:uppercase; color:#fff; background:#2e86c1;">Volume</th>
        <th style="text-align:right; padding:7px 10px; font-size:11px; text-transform:uppercase; color:#fff; background:#2e86c1;">Turnover Value</th>
      </tr></thead>
      <tbody>{commodity_rows_html}</tbody>
    </table>
    </div>
    <div class="pmex-mobile-rows" style="display:none;">{commodity_mobile_html}</div>
    <p style="margin:16px 0 2px; font-size:12px; font-weight:bold; color:#2e86c1; text-transform:uppercase;">Per-Contract Details</p>
    <div class="pmex-table-scroll" style="width:100%; overflow-x:auto;">
    <table class="pmex-table" style="width:100%; min-width:480px; border-collapse:collapse; margin:8px 0 4px;">
      <thead><tr>
        <th style="text-align:left; padding:7px 10px; font-size:11px; text-transform:uppercase; color:#fff; background:#2e86c1;">Contract</th>
        <th style="text-align:left; padding:7px 10px; font-size:11px; text-transform:uppercase; color:#fff; background:#2e86c1;">Commodity</th>
        <th style="text-align:right; padding:7px 10px; font-size:11px; text-transform:uppercase; color:#fff; background:#2e86c1;">Avg Price</th>
        <th style="text-align:right; padding:7px 10px; font-size:11px; text-transform:uppercase; color:#fff; background:#2e86c1;">Contracts Traded (lots)</th>
        <th style="text-align:right; padding:7px 10px; font-size:11px; text-transform:uppercase; color:#fff; background:#2e86c1;">Volume</th>
        <th style="text-align:right; padding:7px 10px; font-size:11px; text-transform:uppercase; color:#fff; background:#2e86c1;">Turnover Value</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
    </div>
    <div class="pmex-mobile-rows" style="display:none;">{mobile_rows_html}</div>
    <p style="margin:16px 0 0; font-size:13px; color:#555;">Full day-by-day breakdown is in the attached PDF report.</p>
  </div>
  <div class="pmex-footer" style="background:#eaf4fb; padding:16px 28px; border-top:1px solid #d6eaf8;">
    <p style="margin:0; font-weight:bold; color:#154360; font-size:14px;">Pakistan Agriculture Research</p>
    <p style="margin:6px 0 0; font-size:11px; color:#999;">Automated report — Pakistan Mercantile Exchange live market data.</p>
  </div>
</div>
</body>
</html>"""


def build_plain_body(start: date, end: date, overview_text: str, grand: dict, overall: list[dict],
                      by_commodity: list[dict]) -> str:
    commodity_rows = "\n".join(
        f"  {c['commodity_name']}: avg price {_price(c['avg_price'], c['price_unit'])}, "
        f"contracts traded {_n(c['contracts_traded'])} lots, volume {_vol(c['volume'], c['size_unit'])}, "
        f"turnover value {_turnover(c['turnover_value'], c['price_unit'])}"
        for c in by_commodity
    )
    rows = "\n".join(
        f"  {a['contract']} ({a['commodity_name']}): avg price {_price(a['avg_price'], a['price_unit'])}, "
        f"contracts traded {_n(a['contracts_traded'])} lots, volume {_vol(a['volume'], a['size_unit'])}, "
        f"turnover value {_turnover(a['turnover_value'], a['price_unit'])}"
        for a in overall
    )
    volume_totals = ", ".join(f"{_n(v)} {unit}" for unit, v in grand["volume_by_unit"].items() if unit)
    return f"""PMEX MARKET REPORT — {SECTOR_LABEL} — {start} to {end}

Contracts traded: {_n(grand['contracts_traded'])} lots | Volume: {volume_totals} | Turnover value: {_n(grand['turnover_value'])} {grand['currency']} | Commodities: {grand['commodities']}

{overview_text}

Commodity totals:
{commodity_rows}

Per-contract totals:
{rows}

Full day-by-day breakdown is in the attached PDF report.

Pakistan Agriculture Research
Automated report — Pakistan Mercantile Exchange live market data.
"""


# ── SMTP delivery ────────────────────────────────────────────────────────────
def send_email_with_pdf(subject: str, html: str, plain: str, pdf_bytes: bytes, pdf_filename: str) -> None:
    recipients = [r.strip() for r in EMAIL_TO.split(",") if r.strip()]

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain, "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)

    attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename=pdf_filename)
    msg.attach(attachment)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(EMAIL_FROM, recipients, msg.as_string())
    log.info("Report emailed via SMTP to %d recipients: %s", len(recipients), ", ".join(recipients))


# ── Entry point ──────────────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(description="Send PMEX Phy_Agri range report (PDF + email).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Write report.pdf and preview.html locally instead of sending an email.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        conn, _ = get_connection()
    except RuntimeError as exc:
        log.critical("DB connection failed: %s", exc)
        sys.exit(1)

    with conn:
        start, end = resolve_range(conn)
        rows = fetch_contract_day_rows(conn, start, end)
    conn.close()

    if not rows:
        log.warning("No '%s' data found for %s -> %s.", CATEGORY, start, end)
        sys.exit(0)

    overall = aggregate_overall(rows)
    by_commodity = aggregate_by_commodity(overall)
    daily = aggregate_daily(rows)
    grand = grand_totals(overall)

    log.info("=== PMEX Range Report | %s | %s -> %s | %d trading days ===", SECTOR_LABEL, start, end, len(daily))

    prompt = build_prompt(start, end, overall, daily, grand)
    overview_text, daily_map = get_summaries(prompt, daily)

    pdf_bytes = build_pdf(start, end, overall, by_commodity, daily, grand, overview_text, daily_map)
    html = build_html_body(start, end, overview_text, grand, overall, by_commodity, len(daily))
    plain = build_plain_body(start, end, overview_text, grand, overall, by_commodity)

    subject = f"PMEX {SECTOR_LABEL} Report — {start} to {end}"
    pdf_filename = f"pmex_local_agri_report_{start}_to_{end}.pdf"

    if args.dry_run:
        with open("report.pdf", "wb") as f:
            f.write(pdf_bytes)
        with open("preview.html", "w") as f:
            f.write(html)
        log.info("Dry run — wrote report.pdf and preview.html (subject: %s)", subject)
        return

    try:
        send_email_with_pdf(subject, html, plain, pdf_bytes, pdf_filename)
    except Exception:
        log.error("Failed to send report email. Plain body:\n%s", plain)
        sys.exit(1)
    log.info("=== Done ===")


if __name__ == "__main__":
    main()

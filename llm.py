"""LLM narrative generation.

The numbers (tables, counts, peak hours) are computed deterministically in code and
injected into the email directly. The LLM ONLY writes short prose summaries from the
already-computed tables — it never invents or restates counts as authoritative.
"""

import re

from langchain_groq import ChatGroq

from config import GROQ_API_KEY, log
from tables import commodities_text, contracts_text, peak_str

_SECTIONS = ("DAILY", "WEEKLY", "MONTHLY")


def build_prompt(data: dict, today: str, sector_label: str) -> str:
    d, w, m = data["daily"], data["weekly"], data["monthly"]
    ov_line = lambda ov: (
        f"Active contracts: {ov.get('active_contracts')} | Contracts traded: {ov.get('contracts_traded')} | "
        f"Turnover value: {ov.get('turnover_value') or 0:,.0f} {ov.get('currency', '')}".strip()
    )
    return f"""You are a commodity market analyst for PMEX (Pakistan Mercantile Exchange).
Today is {today}. Sector: {sector_label}.

Below are pre-computed, ACCURATE market tables, now including each commodity/contract's
Avg Price and Turnover Value (turnover value = avg price * contracts traded — the total
value traded, and the key figure to discuss alongside price moves). Write THREE short
prose summaries — one for each period. Do NOT output tables or restate every row;
interpret the data: call out the most active commodities, notable price levels or moves,
which commodity/contract had the most turnover value, and overall direction.

Rules:
- 2-3 sentences per section. Plain analytical prose. No markdown headings, no bullet lists.
- "Contracts" = number of contracts (lots) traded. "Volume" = converted physical quantity.
  "Avg Price" and "Turnover Value" always carry a unit/currency — never state either as a
  bare, unitless number.
- Refer to commodities by name; bold nothing.
- Output EXACTLY this format, nothing else:
===DAILY===
<daily summary>
===WEEKLY===
<weekly summary>
===MONTHLY===
<monthly summary>

============ DAILY ({today}) ============
{ov_line(d['overview'])} | Peak hours: {peak_str(d['peak_hours'])}
{commodities_text(d['commodities'])}

============ WEEKLY (last 7 days) ============
{ov_line(w['overview'])}
By commodity:
{commodities_text(w['commodities'])}
Top contracts:
{contracts_text(w['top_contracts'])}

============ MONTHLY (last 30 days) ============
{ov_line(m['overview'])}
{commodities_text(m['commodities'])}

Write the three summaries now:"""


def _split_sections(text: str) -> dict:
    """Parse the ===SECTION=== delimited response into {daily, weekly, monthly}."""
    out = {s.lower(): "" for s in _SECTIONS}
    parts = re.split(r"===\s*(DAILY|WEEKLY|MONTHLY)\s*===", text, flags=re.IGNORECASE)
    # parts = [pre, 'DAILY', body, 'WEEKLY', body, ...]
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].strip().lower()
        out[key] = parts[i + 1].strip()
    return out


def get_summaries(prompt: str) -> dict:
    """Return {'daily':..., 'weekly':..., 'monthly':...} prose summaries."""
    try:
        llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0.0, api_key=GROQ_API_KEY)
        content = llm.invoke(prompt).content
        log.info("LLM response received (%d chars).", len(content))
        sections = _split_sections(content)
        if not any(sections.values()):
            log.warning("LLM output had no parseable sections; using raw text as daily.")
            sections["daily"] = content.strip()
        return sections
    except Exception as exc:
        log.error("LLM call failed: %s", exc)
        return {"daily": "Summary unavailable.", "weekly": "", "monthly": ""}

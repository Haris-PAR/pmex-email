"""Assemble the final HTML + plain-text email from computed tables and LLM prose."""

from tables import (
    commodities_html,
    commodities_text,
    contracts_html,
    contracts_text,
    peak_str,
    stat_cards_html,
)


def _prose(text: str) -> str:
    if not text:
        return ""
    return f'<p style="margin:6px 0 14px; font-size:14px; line-height:1.7; color:#2c3e50;">{text}</p>'


def _section(title: str, subtitle: str, body_html: str) -> str:
    return f"""
    <div style="margin:22px 0 6px;">
      <h2 style="font-size:16px; color:#154360; border-left:4px solid #2e86c1; padding-left:10px; margin:0 0 2px;">{title}</h2>
      <p style="margin:0 0 8px 14px; font-size:11px; color:#999;">{subtitle}</p>
      {body_html}
    </div>"""


def build_html(data: dict, summaries: dict, sector_label: str, today: str, sender_name: str) -> str:
    d, w, m = data["daily"], data["weekly"], data["monthly"]

    daily_body = (
        stat_cards_html(d["overview"], extra=f"Peak trading hours: <strong>{peak_str(d['peak_hours'])}</strong>")
        + _prose(summaries.get("daily"))
        + commodities_html(d["commodities"])
    )
    weekly_body = (
        stat_cards_html(w["overview"])
        + _prose(summaries.get("weekly"))
        + commodities_html(w["commodities"])
        + '<p style="margin:16px 0 2px; font-size:12px; font-weight:bold; color:#2e86c1; text-transform:uppercase; letter-spacing:.05em;">Top Contracts</p>'
        + contracts_html(w["top_contracts"])
    )
    monthly_body = (
        stat_cards_html(m["overview"])
        + _prose(summaries.get("monthly"))
        + commodities_html(m["commodities"])
    )

    content = (
        _section(f"Daily Highlights", today, daily_body)
        + _section("Weekly Overview", "Last 7 days", weekly_body)
        + _section("Monthly Snapshot", "Last 30 days", monthly_body)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="margin:0; padding:0; background:#f0f4f8; font-family:Arial,sans-serif;">
<div style="max-width:640px; margin:28px auto; background:#fff; border-radius:10px; box-shadow:0 3px 12px rgba(0,0,0,.12); overflow:hidden;">
  <div style="background:#154360; padding:24px 28px;">
    <h1 style="color:#fff; margin:0; font-size:21px; letter-spacing:.4px;">PMEX Market Summary</h1>
    <p style="color:#85c1e9; margin:5px 0 0; font-size:13px;">{sector_label} &nbsp;&bull;&nbsp; {today}</p>
  </div>
  <div style="padding:20px 28px;">
    {content}
  </div>
  <div style="background:#eaf4fb; padding:16px 28px; border-top:1px solid #d6eaf8;">
    <p style="margin:0; font-size:13px; color:#555;">Best regards,</p>
    <p style="margin:0; font-weight:bold; color:#154360; font-size:14px;">{sender_name}</p>
    <p style="margin:6px 0 0; font-size:11px; color:#999;">Automated report — Pakistan Mercantile Exchange live market data. Figures computed from end-of-session snapshots.</p>
  </div>
</div>
</body>
</html>"""


def build_plain(data: dict, summaries: dict, sector_label: str, today: str, sender_name: str) -> str:
    d, w, m = data["daily"], data["weekly"], data["monthly"]
    ov = lambda o: f"Active contracts: {o.get('active_contracts')} | Contracts traded: {o.get('contracts_traded')} | Avg change: {o.get('avg_change_pct') or 0:+.2f}%"

    return f"""PMEX MARKET SUMMARY — {sector_label} — {today}

=== DAILY HIGHLIGHTS ({today}) ===
{ov(d['overview'])}
Peak trading hours: {peak_str(d['peak_hours'])}
{summaries.get('daily','')}
{commodities_text(d['commodities'])}

=== WEEKLY OVERVIEW (last 7 days) ===
{ov(w['overview'])}
{summaries.get('weekly','')}
By commodity:
{commodities_text(w['commodities'])}
Top contracts:
{contracts_text(w['top_contracts'])}

=== MONTHLY SNAPSHOT (last 30 days) ===
{ov(m['overview'])}
{summaries.get('monthly','')}
{commodities_text(m['commodities'])}

Best regards,
{sender_name}
Automated report — Pakistan Mercantile Exchange live market data.
"""

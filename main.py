"""
PMEX Market Summary Email

Usage:
  python main.py --sector local          # Domestic physical agri   (close 11:00 PM PKT)
  python main.py --sector international  # International agri       (close 11:45 PM PKT)
  python main.py --sector all            # Both sectors combined
  python main.py --sector auto           # Pick sector from current PKT time (see below)

Deployment model: ONE Railway service, ONE cron schedule that fires twice a day
("0,45 18 * * 1-5" UTC = 23:00 and 23:45 PKT — right after local close
at 11:00 PM and international close at 11:45 PM). SECTOR=auto (the default) makes the
script look at the current PKT time and decide which sector's email to send,
so no second service or second cron schedule is needed. A CLI --sector flag
always overrides SECTOR for manual/test runs.

Numbers (tables, counts, peak hours) are computed in code; the LLM only writes the
prose summaries.
"""

import argparse
import os
import sys
from datetime import datetime

from config import SECTOR_CONFIG, log, resolve_auto_sector
from db import get_connection
from queries import collect_report_data
from llm import build_prompt, get_summaries
from render import build_html, build_plain
from mailer import send_email


def parse_args():
    parser = argparse.ArgumentParser(description="Send PMEX market summary email.")
    parser.add_argument(
        "--sector",
        choices=["local", "international", "all", "auto"],
        default=os.getenv("SECTOR", "auto"),
        help="Which sector to report on (default: auto, or $SECTOR env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the email and write it to preview.html without sending.",
    )
    return parser.parse_args()


def main():
    args   = parse_args()
    sector = args.sector

    if sector == "auto":
        sector = resolve_auto_sector()
        log.info("SECTOR=auto resolved to '%s' based on current PKT time.", sector)

    cfg   = SECTOR_CONFIG[sector]
    today = datetime.now().strftime("%Y-%m-%d")

    log.info("=== PMEX Email Summary | sector=%s | date=%s ===", sector, today)

    try:
        conn, env = get_connection()
    except RuntimeError as exc:
        log.critical("DB connection failed: %s", exc)
        sys.exit(1)

    sender_name = "Pakistan Agriculture Research" if env == "production" else "Muhammad Haris"

    with conn:
        data = collect_report_data(conn, cfg["filter"])
    conn.close()

    if not data["daily"]["commodities"]:
        log.warning("No commodities active today for sector '%s'.", sector)

    summaries = get_summaries(build_prompt(data, today, cfg["label"]))

    html  = build_html(data, summaries, cfg["label"], today, sender_name)
    plain = build_plain(data, summaries, cfg["label"], today, sender_name)

    close_tag = f" | Closes {cfg['close_time']}" if cfg["close_time"] else ""
    subject   = f"PMEX {cfg['label']} Summary{close_tag} — {today}"

    if args.dry_run:
        with open("preview.html", "w") as f:
            f.write(html)
        log.info("Dry run — wrote preview.html (subject: %s)", subject)
        return

    try:
        send_email(subject, html, plain)
    except Exception:
        log.error("Failed to send email. Plain body:\n%s", plain)
        sys.exit(1)
    log.info("=== Done ===")


if __name__ == "__main__":
    main()

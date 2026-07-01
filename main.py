"""
PMEX Market Summary Email

Usage:
  python main.py --sector local          # Domestic physical agri   (close 11:30 PM PKT)
  python main.py --sector international  # International agri       (close 11:00 PM PKT)
  python main.py                         # Both sectors combined

Sector also reads from the SECTOR env var (CLI flag wins if both given) so the
same deployed service can be reused across multiple Railway cron schedules by
only changing an env var, not code:
  SECTOR=international python main.py

Numbers (tables, counts, peak hours) are computed in code; the LLM only writes the
prose summaries. Run each sector after its own market close so the last snapshot of
the day is the true closing figure.
"""

import argparse
import os
import sys
from datetime import datetime

from config import SECTOR_CONFIG, log
from db import get_connection
from queries import collect_report_data
from llm import build_prompt, get_summaries
from render import build_html, build_plain
from mailer import send_email


def parse_args():
    parser = argparse.ArgumentParser(description="Send PMEX market summary email.")
    parser.add_argument(
        "--sector",
        choices=["local", "international", "all"],
        default=os.getenv("SECTOR", "all"),
        help="Which sector to report on (default: all, or $SECTOR env var)",
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
    cfg    = SECTOR_CONFIG[sector]
    today  = datetime.now().strftime("%Y-%m-%d")

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

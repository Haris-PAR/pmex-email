"""Configuration: logging, environment variables, and sector definitions."""

import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "email_summary.log")
        ),
    ],
)
log = logging.getLogger("email_summary")

# ── Config ─────────────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

GROQ_API_KEY  = os.getenv("GROQ_API_KEY")
PROD_DB_URL   = os.getenv("PROD_DATABASE_URL")
LOCAL_DB_URL  = os.getenv("LOCAL_DATABASE_URL")
SMTP_SERVER   = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM    = os.getenv("EMAIL_FROM")
EMAIL_TO      = os.getenv("EMAIL_TO")
GOOGLE_WEBHOOK_URL = os.getenv("GOOGLE_WEBHOOK_URL")

# ── Sector Definitions ─────────────────────────────────────────────────────────
# Sectors are driven by the `category` column, NOT hardcoded commodity codes:
#   Agri     -> Agriculture — International   (ICORN, ICOTTON, ISOYBEAN, IWHEAT, ...)
#   Phy_Agri -> Agriculture — Domestic (Physical)  (LGMRRICE, MAIZELD, ...)
#   Phy      -> Physical Gold  (deliberately excluded from these reports)
_LOCAL_CATS = ["Phy_Agri"]
_INTL_CATS  = ["Agri"]
_ALL_CATS   = ["Agri", "Phy_Agri"]


def _cat_in(cats):
    return "category IN ({})".format(", ".join(f"'{c}'" for c in cats))


SECTOR_CONFIG = {
    "local": {
        "label":      "Local Agriculture (Domestic Physical)",
        "close_time": "11:00 PM PKT",
        "filter":     _cat_in(_LOCAL_CATS),
    },
    "international": {
        "label":      "International Agriculture",
        "close_time": "11:45 PM PKT",
        "filter":     _cat_in(_INTL_CATS),
    },
    "all": {
        "label":      "Agriculture (All Sectors)",
        "close_time": "",
        "filter":     _cat_in(_ALL_CATS),
    },
}

# ── Auto sector resolution (single cron, two fire times) ───────────────────────
# One Railway cron schedule ("0,45 18 * * 1-5" UTC) fires twice: 23:00 and 23:45
# PKT — right after local close (11:00 PM) and international close (11:45 PM).
# The script picks the sector from the current PKT time instead of needing two
# separate services with different SECTOR env vars.
PKT = ZoneInfo("Asia/Karachi")
_AUTO_SCHEDULE = [
    ("local",         23, 0),
    ("international", 23, 45),
]

def resolve_auto_sector(now: datetime = None) -> str:
    """Pick the sector whose scheduled fire-time is closest to `now` (PKT).

    No tolerance cutoff: Railway's cron can fire late (queueing, cold start),
    but the only thing that ever invokes this script on a schedule is that
    cron, so the nearest anchor time is always the right call.
    """
    now = now or datetime.now(PKT)
    now_min = now.hour * 60 + now.minute
    best_sector, best_diff = None, None
    for sector, h, m in _AUTO_SCHEDULE:
        diff = abs(now_min - (h * 60 + m))
        diff = min(diff, 1440 - diff)  # circular distance, handles midnight wraparound
        if best_diff is None or diff < best_diff:
            best_sector, best_diff = sector, diff
    return best_sector

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
        "close_time": "11:30 PM PKT",
        "filter":     _cat_in(_LOCAL_CATS),
    },
    "international": {
        "label":      "International Agriculture",
        "close_time": "11:00 PM PKT",
        "filter":     _cat_in(_INTL_CATS),
    },
    "all": {
        "label":      "Agriculture (All Sectors)",
        "close_time": "",
        "filter":     _cat_in(_ALL_CATS),
    },
}

# ── Auto sector resolution (single cron, two fire times) ───────────────────────
# One Railway cron schedule ("5,35 18 * * 1-5" UTC) fires twice: 23:05 and 23:35
# PKT — right after international close (11:00 PM) and local close (11:30 PM).
# The script picks the sector from the current PKT time instead of needing two
# separate services with different SECTOR env vars.
PKT = ZoneInfo("Asia/Karachi")
_AUTO_SCHEDULE = [
    ("international", 23, 5),
    ("local",         23, 35),
]
_AUTO_TOLERANCE_MIN = 20  # forgive scheduler jitter; outside this, fall back to "all"


def resolve_auto_sector(now: datetime = None) -> str:
    """Pick the sector whose scheduled fire-time is closest to `now` (PKT)."""
    now = now or datetime.now(PKT)
    now_min = now.hour * 60 + now.minute
    best_sector, best_diff = "all", None
    for sector, h, m in _AUTO_SCHEDULE:
        diff = abs(now_min - (h * 60 + m))
        if best_diff is None or diff < best_diff:
            best_sector, best_diff = sector, diff
    return best_sector if best_diff <= _AUTO_TOLERANCE_MIN else "all"

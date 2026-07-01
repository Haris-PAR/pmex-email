"""Database connection handling."""

import os
from urllib.parse import urlparse, unquote

import psycopg2

from config import LOCAL_DB_URL, PROD_DB_URL, log


def connect_from_url(url: str, timeout: int = 10):
    parsed = urlparse(url)
    kwargs = dict(
        host=parsed.hostname or "",
        port=int(parsed.port or 5432),
        dbname=parsed.path.lstrip("/").split("?")[0],
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        connect_timeout=timeout,
    )
    # Add SSL when the URL itself or ENVIRONMENT indicates production
    url_lower = url.lower()
    is_prod = "sslmode=require" in url_lower or os.getenv("APP_ENV", "local").strip().lower() == "production"
    if is_prod:
        kwargs["sslmode"] = "require"
    return psycopg2.connect(**kwargs)


def get_connection():
    """Connect to the DB selected by APP_ENV. Returns (conn, env_label)."""
    app_env = os.getenv("APP_ENV", "local").strip().lower()

    if app_env == "production":
        ordered = [("production", PROD_DB_URL), ("local", LOCAL_DB_URL)]
    else:
        ordered = [("local", LOCAL_DB_URL), ("production", PROD_DB_URL)]

    for label, raw_url in ordered:
        if not raw_url:
            continue
        try:
            conn = connect_from_url(raw_url)
            log.info("Connected to %s database (APP_ENV=%s).", label, app_env)
            return conn, label
        except Exception as exc:
            log.warning("Could not connect to %s DB: %s", label, exc)
    raise RuntimeError("No database connection available.")

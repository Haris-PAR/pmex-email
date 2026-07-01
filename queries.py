"""Data layer for the PMEX summary email.

Data model (crop_snapshots):
  * A snapshot of every quotable contract is stored every ~15 min.
  * `total_volume`     = cumulative NUMBER OF CONTRACTS (lots) traded that day.
  * `contract_size`    = units per contract (e.g. 5000 bushels, 10 MT).
  * `commodity_volume` = total_volume * contract_size = CONVERTED physical value.
  * A contract is ACTIVE when bid > 0 OR ask > 0 (same rule the collector uses).

Because total_volume is cumulative and pre-open snapshots carry the PRIOR
session's value, we always reduce to the LAST snapshot per (contract, fetch_date)
before aggregating — that is the true end-of-day figure for each contract.
"""

import psycopg2.extras

from config import log

ACTIVE = "(COALESCE(bid,0) > 0 OR COALESCE(ask,0) > 0)"


def _daily_last(sf: str, date_clause: str) -> str:
    """CTE body: the last snapshot per contract per day for the sector."""
    return f"""
        SELECT DISTINCT ON (contract, fetch_date) *
        FROM crop_snapshots
        WHERE {date_clause} AND {sf}
        ORDER BY contract, fetch_date, fetch_time DESC
    """


def _rows(conn, sql: str, label: str) -> list[dict]:
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            result = [dict(r) for r in cur.fetchall()]
            log.info("Query '%s' -> %d rows.", label, len(result))
            return result
    except Exception as exc:
        log.error("Query '%s' failed: %s", label, exc)
        conn.rollback()
        return []


# ── Per-commodity aggregates ────────────────────────────────────────────────────
def _commodities(conn, sf: str, date_clause: str, label: str) -> list[dict]:
    sql = f"""
        WITH daily_last AS ({_daily_last(sf, date_clause)})
        SELECT
            commodity_code,
            commodity_name,
            size_unit,
            COUNT(DISTINCT contract)     AS n_contracts,
            SUM(total_volume)            AS contracts_traded,
            SUM(commodity_volume)        AS converted_volume,
            AVG(change_pct)              AS avg_change_pct,
            COUNT(DISTINCT fetch_date)   AS days_traded
        FROM daily_last
        WHERE {ACTIVE}
        GROUP BY commodity_code, commodity_name, size_unit
        ORDER BY converted_volume DESC NULLS LAST;
    """
    return _rows(conn, sql, label)


def _top_contracts(conn, sf: str, date_clause: str, label: str, limit: int = 10) -> list[dict]:
    sql = f"""
        WITH daily_last AS ({_daily_last(sf, date_clause)})
        SELECT
            contract,
            commodity_name,
            size_unit,
            SUM(total_volume)     AS contracts_traded,
            SUM(commodity_volume) AS converted_volume,
            AVG(change_pct)       AS avg_change_pct
        FROM daily_last
        WHERE {ACTIVE}
        GROUP BY contract, commodity_name, size_unit
        ORDER BY converted_volume DESC NULLS LAST
        LIMIT {limit};
    """
    return _rows(conn, sql, label)


def _overview(conn, sf: str, date_clause: str, label: str) -> dict:
    sql = f"""
        WITH daily_last AS ({_daily_last(sf, date_clause)})
        SELECT
            COUNT(DISTINCT contract) FILTER (WHERE {ACTIVE})       AS active_contracts,
            COUNT(DISTINCT commodity_code) FILTER (WHERE {ACTIVE}) AS commodities,
            SUM(total_volume)                                      AS contracts_traded,
            SUM(commodity_volume)                                  AS converted_volume,
            AVG(change_pct)                                        AS avg_change_pct
        FROM daily_last;
    """
    rows = _rows(conn, sql, label)
    return rows[0] if rows else {}


def _peak_hours(conn, sf: str, date_clause: str, label: str, limit: int = 3) -> list[dict]:
    """Real volume traded per hour, derived from per-contract hourly increments.

    total_volume is cumulative, so hourly traded volume = the increase between an
    hour's last cumulative value and the previous hour's (per contract per day).
    LAG has NO default, so the first hour of each contract-day is skipped (never
    counts the stale pre-open carryover); session resets clamp to 0 via GREATEST.
    """
    sql = f"""
        WITH hourly_last AS (
            SELECT DISTINCT ON (contract, fetch_date, date_trunc('hour', fetch_time))
                contract,
                fetch_date,
                date_trunc('hour', fetch_time)     AS hr,
                EXTRACT(HOUR FROM fetch_time)::INT AS hour_of_day,
                total_volume
            FROM crop_snapshots
            WHERE {date_clause} AND {sf} AND {ACTIVE}
            ORDER BY contract, fetch_date, date_trunc('hour', fetch_time), fetch_time DESC
        ),
        deltas AS (
            SELECT hour_of_day,
                GREATEST(total_volume - LAG(total_volume) OVER (
                    PARTITION BY contract, fetch_date ORDER BY hr
                ), 0) AS vol_delta
            FROM hourly_last
        )
        SELECT hour_of_day, SUM(vol_delta) AS contracts_traded
        FROM deltas
        GROUP BY hour_of_day
        HAVING SUM(vol_delta) > 0
        ORDER BY contracts_traded DESC
        LIMIT {limit};
    """
    return _rows(conn, sql, label)


# ── Public entry point ──────────────────────────────────────────────────────────
def collect_report_data(conn, sf: str) -> dict:
    """Gather every table the email needs, for one sector filter (sf)."""
    today  = "fetch_date = CURRENT_DATE"
    last7  = "fetch_date >= CURRENT_DATE - INTERVAL '7 days'"
    last30 = "fetch_date >= CURRENT_DATE - INTERVAL '30 days'"

    return {
        "daily": {
            "overview":    _overview(conn, sf, today, "daily_overview"),
            "commodities": _commodities(conn, sf, today, "daily_commodities"),
            "peak_hours":  _peak_hours(conn, sf, today, "daily_peak"),
        },
        "weekly": {
            "overview":      _overview(conn, sf, last7, "weekly_overview"),
            "commodities":   _commodities(conn, sf, last7, "weekly_commodities"),
            "top_contracts": _top_contracts(conn, sf, last7, "weekly_top_contracts"),
        },
        "monthly": {
            "overview":    _overview(conn, sf, last30, "monthly_overview"),
            "commodities": _commodities(conn, sf, last30, "monthly_commodities"),
        },
    }

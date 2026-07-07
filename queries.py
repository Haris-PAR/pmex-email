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


def _day_calc(sf: str, date_clause: str) -> str:
    """CTE body: per (contract, day) price/turnover, joined onto the day's last snapshot.

    avg_price = AVG(close_price) across the day's ACTIVE snapshots (mirrors
    report_summary.py, so a single quote spike doesn't skew it); contracts_traded/
    volume/change_pct still come from the day's LAST snapshot since total_volume/
    commodity_volume are cumulative intraday. turnover_value = avg_price *
    contracts_traded, same definition as report_summary.py's range report.
    """
    return f"""
        SELECT
            da.contract, da.commodity_code, da.commodity_name, da.size_unit, da.price_unit,
            da.fetch_date, da.avg_price, dl.change_pct, dl.bid, dl.ask,
            COALESCE(dl.total_volume, 0)     AS contracts_traded,
            COALESCE(dl.commodity_volume, 0) AS volume,
            da.avg_price * COALESCE(dl.total_volume, 0) AS turnover_value
        FROM (
            SELECT contract, commodity_code, commodity_name, size_unit, price_unit, fetch_date,
                   AVG(close_price) AS avg_price
            FROM crop_snapshots
            WHERE {date_clause} AND {sf} AND {ACTIVE}
            GROUP BY contract, commodity_code, commodity_name, size_unit, price_unit, fetch_date
        ) da
        JOIN ({_daily_last(sf, date_clause)}) dl
          ON dl.contract = da.contract AND dl.fetch_date = da.fetch_date
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
        WITH day_calc AS ({_day_calc(sf, date_clause)})
        SELECT
            commodity_code,
            commodity_name,
            size_unit,
            COUNT(DISTINCT contract)     AS n_contracts,
            SUM(contracts_traded)        AS contracts_traded,
            SUM(volume)                  AS converted_volume,
            AVG(change_pct)              AS avg_change_pct,
            SUM(turnover_value)          AS turnover_value,
            CASE WHEN SUM(contracts_traded) > 0
                 THEN SUM(turnover_value) / SUM(contracts_traded) ELSE 0 END AS avg_price,
            -- Only a single blended avg price/currency when every contract under this
            -- commodity quotes on the same basis (mirrors report_summary.py's grand_totals).
            CASE WHEN COUNT(DISTINCT price_unit) = 1 THEN MIN(price_unit) ELSE NULL END AS price_unit,
            COUNT(DISTINCT fetch_date)   AS days_traded
        FROM day_calc
        WHERE {ACTIVE}
        GROUP BY commodity_code, commodity_name, size_unit
        ORDER BY converted_volume DESC NULLS LAST;
    """
    return _rows(conn, sql, label)


def _top_contracts(conn, sf: str, date_clause: str, label: str, limit: int = 10) -> list[dict]:
    sql = f"""
        WITH day_calc AS ({_day_calc(sf, date_clause)})
        SELECT
            contract,
            commodity_name,
            size_unit,
            SUM(contracts_traded)   AS contracts_traded,
            SUM(volume)             AS converted_volume,
            AVG(change_pct)         AS avg_change_pct,
            SUM(turnover_value)     AS turnover_value,
            CASE WHEN SUM(contracts_traded) > 0
                 THEN SUM(turnover_value) / SUM(contracts_traded) ELSE 0 END AS avg_price,
            CASE WHEN COUNT(DISTINCT price_unit) = 1 THEN MIN(price_unit) ELSE NULL END AS price_unit
        FROM day_calc
        WHERE {ACTIVE}
        GROUP BY contract, commodity_name, size_unit
        ORDER BY converted_volume DESC NULLS LAST
        LIMIT {limit};
    """
    return _rows(conn, sql, label)


def _overview(conn, sf: str, date_clause: str, label: str) -> dict:
    sql = f"""
        WITH day_calc AS ({_day_calc(sf, date_clause)})
        SELECT
            COUNT(DISTINCT contract) FILTER (WHERE {ACTIVE})       AS active_contracts,
            COUNT(DISTINCT commodity_code) FILTER (WHERE {ACTIVE}) AS commodities,
            SUM(contracts_traded)                                  AS contracts_traded,
            SUM(volume)                                             AS converted_volume,
            AVG(change_pct)                                         AS avg_change_pct,
            SUM(turnover_value)                                     AS turnover_value,
            CASE WHEN SUM(contracts_traded) > 0
                 THEN SUM(turnover_value) / SUM(contracts_traded) ELSE 0 END AS avg_price,
            CASE WHEN COUNT(DISTINCT price_unit) = 1 THEN MIN(price_unit) ELSE NULL END AS avg_price_unit,
            -- Currency alone (e.g. "Rs") is uniform far more often than the full price_unit
            -- string, so turnover value can usually show a currency even when avg_price can't.
            CASE WHEN COUNT(DISTINCT rtrim(split_part(price_unit, ' ', 1), '.')) FILTER (WHERE price_unit IS NOT NULL) = 1
                 THEN MIN(rtrim(split_part(price_unit, ' ', 1), '.'))
                 ELSE '' END AS currency
        FROM day_calc;
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

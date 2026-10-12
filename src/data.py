"""Data access for the dashboard: the only module that talks to the database.

Everything else asks this module for plain dicts and lists, so the SQLite file
can later be swapped for another database without touching the web code.
"""
from __future__ import annotations

import math
import re
import sqlite3
from contextlib import closing

from .build_database import STATUSES, build, db_path, now

RISK_ORDER = "CASE risk_level WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END"
ALERT_ORDER = (f"(a.category = 'disease_outbreak') DESC, {RISK_ORDER}, "
               "a.start_date DESC, a.alert_id")


def connect() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        build(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def query(sql: str, params: tuple | list = ()) -> list[dict]:
    with closing(connect()) as con:
        return [dict(row) for row in con.execute(sql, params)]


# ----------------------------------------------------------------------- alerts
def alerts(type: str | None = None, risk: str | list[str] | None = None,
           status: str | None = None, since: str | None = None,
           animal_id: str | None = None) -> list[dict]:
    """Alerts with their status, most urgent first. `risk` may be one level or a list."""
    where, params = [], []
    for column, value in (("a.type", type), ("s.status", status), ("a.animal_id", animal_id)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if risk:
        levels = [risk] if isinstance(risk, str) else list(risk)
        where.append(f"a.risk_level IN ({', '.join('?' * len(levels))})")
        params.extend(levels)
    if since:
        where.append("a.end_date >= ?")
        params.append(since)
    sql = ("SELECT a.*, s.status, s.updated_at AS status_updated_at "
           "FROM alerts a JOIN alert_status s USING (alert_id)"
           + (" WHERE " + " AND ".join(where) if where else "")
           + f" ORDER BY {ALERT_ORDER}")
    return query(sql, params)


def set_status(alert_id: str, status: str) -> dict | None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    with closing(connect()) as con, con:
        changed = con.execute("UPDATE alert_status SET status = ?, updated_at = ? "
                              "WHERE alert_id = ?", (status, now(), alert_id)).rowcount
    if not changed:
        return None
    return next(iter(query("SELECT a.*, s.status, s.updated_at AS status_updated_at "
                           "FROM alerts a JOIN alert_status s USING (alert_id) "
                           "WHERE a.alert_id = ?", (alert_id,))), None)


def summary() -> dict:
    with closing(connect()) as con:
        def counts(sql: str) -> dict:
            return {row[0]: row[1] for row in con.execute(sql)}

        open_by_risk = counts("SELECT risk_level, COUNT(*) FROM alerts JOIN alert_status "
                              "USING (alert_id) WHERE status = 'open' GROUP BY risk_level")
        first, last = con.execute("SELECT MIN(date), MAX(date) FROM livestock_data").fetchone()
        return {
            "alerts": con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],
            "by_risk": counts("SELECT risk_level, COUNT(*) FROM alerts GROUP BY risk_level"),
            "by_type": counts("SELECT type, COUNT(*) FROM alerts GROUP BY type"),
            "by_status": counts("SELECT status, COUNT(*) FROM alert_status "
                                "JOIN alerts USING (alert_id) GROUP BY status"),
            "open_by_risk": {r: open_by_risk.get(r, 0) for r in ("High", "Medium", "Low")},
            "outbreaks": con.execute("SELECT COUNT(*) FROM alerts "
                                     "WHERE category = 'disease_outbreak'").fetchone()[0],
            "crop_sites": con.execute("SELECT COUNT(*) FROM alerts "
                                      "WHERE type = 'crop'").fetchone()[0],
            "crop_sites_at_risk": con.execute("SELECT COUNT(*) FROM alerts WHERE type = 'crop' "
                                              "AND risk_level != 'Low'").fetchone()[0],
            "animals": con.execute("SELECT COUNT(DISTINCT animal_id) "
                                   "FROM livestock_data").fetchone()[0],
            "flagged_animals": con.execute("SELECT COUNT(DISTINCT animal_id) FROM alerts "
                                           "WHERE animal_id IS NOT NULL "
                                           "AND category != 'normal'").fetchone()[0],
            "district": con.execute("SELECT district FROM livestock_data LIMIT 1").fetchone()[0],
            "livestock_dates": [first, last],
            "missing_setswana": con.execute("SELECT COUNT(*) FROM advice_templates "
                                            "WHERE TRIM(advice_tn) = ''").fetchone()[0],
        }


# -------------------------------------------------------------------- livestock
def livestock_dates() -> list[str]:
    return [row["date"] for row in query("SELECT DISTINCT date FROM livestock_data ORDER BY date")]


def positions(date: str | None = None) -> dict | None:
    """Every animal's position on `date` (default: the latest day), with its alerts.

    `flagged` means an alert covers that day; `earlier_alert` means the animal
    had an alert before that day but not on it.
    """
    date = date or livestock_dates()[-1]
    rows = query("SELECT animal_id, herd_id, district, lat, lon, activity_level, body_temp_c "
                 "FROM livestock_data WHERE date = ? ORDER BY animal_id", (date,))
    if not rows:
        return None
    raised = query("SELECT alert_id, animal_id, risk_level, category, subject, start_date, "
                   f"end_date FROM alerts WHERE animal_id IS NOT NULL AND category != 'normal' "
                   f"AND start_date <= ? "
                   f"ORDER BY {RISK_ORDER}", (date,))
    for row in rows:
        mine = [a for a in raised if a["animal_id"] == row["animal_id"]]
        today = [a for a in mine if a["end_date"] >= date]
        row["flagged"] = bool(today)
        row["earlier_alert"] = bool(mine) and not today
        row["risk_level"] = (today or mine or [{}])[0].get("risk_level")
        row["categories"] = sorted({a["category"] for a in today})
        row["alerts"] = [a["alert_id"] for a in mine]
    return {"date": date, "animals": rows}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(min(1.0, a)))


def animal(animal_id: str) -> dict | None:
    """Daily readings for one animal, the rest-of-herd average activity, and its alerts."""
    days = query(
        "SELECT d.date, d.herd_id, d.lat, d.lon, d.activity_level, d.body_temp_c, "
        "  (SELECT AVG(o.activity_level) FROM livestock_data o WHERE o.date = d.date "
        "   AND o.herd_id = d.herd_id AND o.animal_id != d.animal_id) AS herd_activity "
        "FROM livestock_data d WHERE d.animal_id = ? ORDER BY d.date", (animal_id,))
    if not days:
        return None
    previous = None
    for day in days:
        day["distance_km"] = (None if previous is None else
                              round(haversine_km(previous["lat"], previous["lon"],
                                                 day["lat"], day["lon"]), 3))
        day["herd_activity"] = round(day["herd_activity"], 1)
        previous = day
    return {"animal_id": animal_id, "herd_id": days[0]["herd_id"], "days": days,
            "alerts": alerts(animal_id=animal_id)}


# ------------------------------------------------------------------------ crops
def crops() -> dict:
    seasons = query("SELECT location, date, rainfall_mm, temperature_c, ndvi, yield_t_ha, "
                    "data_source FROM crop_data ORDER BY location, date")
    average = query("SELECT AVG(yield_t_ha) AS avg FROM crop_data")[0]["avg"]
    return {
        "sites": sorted({s["location"] for s in seasons}),
        "seasons": seasons,
        "regional_average_t_ha": round(average, 4),
        "data_sources": sorted({s["data_source"] for s in seasons}),
        "alerts": [crop_prediction(a) for a in alerts(type="crop")],
    }


CROP_DETAILS = re.compile(r"Predicted yield (?P<predicted>[\d.]+) t/ha = (?P<pct>\d+)%")


def crop_prediction(alert: dict) -> dict:
    """Add location, predicted yield and % of average, read from the alert text."""
    match = CROP_DETAILS.search(alert["details"] or "")
    return {**alert,
            "location": alert["subject"].removeprefix("Crops in "),
            "predicted_t_ha": float(match["predicted"]) if match else None,
            "pct_of_average": int(match["pct"]) if match else None}

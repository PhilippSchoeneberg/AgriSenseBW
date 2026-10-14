"""Data access for the dashboard: the only module that talks to the database.

Everything else asks this module for plain dicts and lists, so the SQLite file
can later be swapped for another database without touching the web code.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import statistics
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .build_database import STATUSES, build, db_path, ensure_live_tables, now

RISK_ORDER = "CASE risk_level WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END"
ALERT_ORDER = (f"(a.category = 'disease_outbreak') DESC, {RISK_ORDER}, "
               "a.start_date DESC, a.alert_id")


def connect() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        build(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    ensure_live_tables(con)  # databases built before the sensor table existed
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


# ---------------------------------------------------------------------- sensors
# Live readings from the hardware prototype (firmware/collar_node, firmware/gateway).
LOCAL_TIME = ZoneInfo("Africa/Gaborone")
# The collar measures skin temperature with an infrared sensor, which reads lower than
# body temperature. This threshold is for demonstrating the alert, not a vet's fever line.
LIVE_HIGH_TEMP_C = float(os.environ.get("AGRISENSE_LIVE_HIGH_TEMP_C", "37.5"))
NOT_SEEN_AFTER_S = 60   # a collar not heard for this long is "not seen"
SENSOR_COLUMNS = ["animal_id", "body_temp_c", "ambient_c", "activity", "rssi",
                  "rain_level", "rain_raw", "rain_wet"]


def store_readings(gateway_id: str, lat: float | None, lon: float | None,
                   readings: list[dict]) -> int:
    """Save one batch from a gateway; every reading gets the time it arrived."""
    received = now()
    rows = [(received, gateway_id, r["sensor"], *[r.get(c) for c in SENSOR_COLUMNS], lat, lon)
            for r in readings]
    columns = ", ".join(["received_at", "gateway_id", "sensor", *SENSOR_COLUMNS, "lat", "lon"])
    marks = ", ".join("?" * (len(SENSOR_COLUMNS) + 5))
    with closing(connect()) as con, con:
        con.executemany(f"INSERT INTO sensor_readings ({columns}) VALUES ({marks})", rows)
    return len(rows)


def _age_s(iso: str, now_dt: datetime) -> int:
    return int((now_dt - datetime.fromisoformat(iso)).total_seconds())


def live_sensors(minutes: int = 10) -> dict:
    """Latest state of every collar, the rain sensor and the gateways, plus recent history."""
    now_dt = datetime.now(timezone.utc)
    since = (now_dt - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    gateways = query("SELECT gateway_id, MAX(received_at) AS last_seen, lat, lon "
                     "FROM sensor_readings GROUP BY gateway_id ORDER BY last_seen DESC")
    for g in gateways:
        g["seconds_ago"] = _age_s(g["last_seen"], now_dt)

    collars = []
    latest = query("SELECT r.* FROM sensor_readings r JOIN (SELECT animal_id, MAX(id) AS id "
                   "FROM sensor_readings WHERE sensor = 'collar' GROUP BY animal_id) last "
                   "USING (id) ORDER BY r.animal_id")
    for c in latest:
        age = _age_s(c["received_at"], now_dt)
        temp = c["body_temp_c"]
        status = ("not_seen" if age > NOT_SEEN_AFTER_S
                  else "high_temperature" if temp is not None and temp >= LIVE_HIGH_TEMP_C
                  else "ok")
        series = query("SELECT received_at AS t, body_temp_c, activity FROM sensor_readings "
                       "WHERE sensor = 'collar' AND animal_id = ? AND received_at >= ? "
                       "ORDER BY id", (c["animal_id"], since))
        collars.append({"animal_id": c["animal_id"], "last_seen": c["received_at"],
                        "seconds_ago": age, "status": status, "body_temp_c": temp,
                        "ambient_c": c["ambient_c"], "activity": c["activity"],
                        "rssi": c["rssi"], "gateway_id": c["gateway_id"], "series": series})

    rain = None
    rain_rows = query("SELECT received_at AS t, rain_level, rain_wet, rain_raw FROM sensor_readings "
                      "WHERE sensor = 'rain' ORDER BY id DESC LIMIT 1")
    if rain_rows:
        rain = {**rain_rows[0], "rain_wet": bool(rain_rows[0]["rain_wet"]),
                "seconds_ago": _age_s(rain_rows[0]["t"], now_dt),
                "series": query("SELECT received_at AS t, rain_level FROM sensor_readings "
                                "WHERE sensor = 'rain' AND received_at >= ? ORDER BY id", (since,))}
    return {"now": now_dt.isoformat(timespec="seconds"), "minutes": minutes,
            "high_temp_threshold_c": LIVE_HIGH_TEMP_C, "not_seen_after_s": NOT_SEEN_AFTER_S,
            "gateways": gateways, "collars": collars, "rain": rain}


def sensor_days(herd_id: str = "LIVE", district: str = "Southern") -> dict[str, list[dict]]:
    """Daily summaries of the live readings, in the shape of livestock_data.csv.

    Days follow Botswana time. Body temperature is the daily median (robust against
    single bad readings), activity the daily mean, the position that of the gateway.
    Rain gets its own table: the rain sensor tells wet or dry, not millimetres.
    """
    rows = query("SELECT * FROM sensor_readings ORDER BY id")
    animals: dict[tuple, list[dict]] = {}
    rain: dict[str, list[dict]] = {}
    for r in rows:
        local_day = datetime.fromisoformat(r["received_at"]).astimezone(LOCAL_TIME).date().isoformat()
        if r["sensor"] == "collar":
            animals.setdefault((local_day, r["animal_id"]), []).append(r)
        else:
            rain.setdefault(local_day, []).append(r)

    def middle(values):
        values = [v for v in values if v is not None]
        return round(statistics.median(values), 2) if values else None

    def mean(values):
        values = [v for v in values if v is not None]
        return round(statistics.fmean(values), 1) if values else None

    livestock = [{"date": d, "animal_id": a, "herd_id": herd_id, "district": district,
                  "lat": rs[-1]["lat"], "lon": rs[-1]["lon"],
                  "activity_level": mean(r["activity"] for r in rs),
                  "body_temp_c": middle(r["body_temp_c"] for r in rs),
                  "readings": len(rs)}
                 for (d, a), rs in sorted(animals.items())]
    rain_days = [{"date": d, "district": district,
                  "wet_share": round(sum(bool(r["rain_wet"]) for r in rs) / len(rs), 3),
                  "max_rain_level": max((r["rain_level"] or 0) for r in rs),
                  "readings": len(rs)}
                 for d, rs in sorted(rain.items())]
    return {"livestock": livestock, "rain": rain_days}

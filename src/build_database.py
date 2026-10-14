"""AgriSense BW database: loads the project's CSV files into one SQLite file.

The data tables are rebuilt from the CSVs on every run. The alert_status table
is never dropped, so an alert that was acknowledged or resolved in the
dashboard keeps that status when the alerts are recalculated.

Usage, from the repository folder:
    python -m src.alert_engine && python -m src.build_database

The database file is data/agrisense.db, or the path in the AGRISENSE_DB
environment variable.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DB = DATA / "agrisense.db"
STATUSES = ("open", "acknowledged", "resolved")

SCHEMA = """
CREATE TABLE alerts (
    alert_id    TEXT PRIMARY KEY,
    animal_id   TEXT,  -- derived from alert_id for per-animal alerts, else NULL
    district    TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('crop', 'livestock')),
    risk_level  TEXT NOT NULL CHECK (risk_level IN ('Low', 'Medium', 'High')),
    reason      TEXT NOT NULL,
    advice_en   TEXT NOT NULL,
    advice_tn   TEXT,
    category    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    for_whom    TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    days        INTEGER,
    details     TEXT,
    lat         REAL,
    lon         REAL
);
CREATE INDEX alerts_animal ON alerts (animal_id);
CREATE TABLE crop_data (
    date          TEXT NOT NULL,
    district      TEXT NOT NULL,
    location      TEXT NOT NULL,
    rainfall_mm   REAL,
    temperature_c REAL,
    ndvi          REAL,
    yield_t_ha    REAL,
    data_source   TEXT,
    PRIMARY KEY (location, date)
);
CREATE TABLE livestock_data (
    date           TEXT NOT NULL,
    animal_id      TEXT NOT NULL,
    herd_id        TEXT NOT NULL,
    district       TEXT NOT NULL,
    lat            REAL NOT NULL,
    lon            REAL NOT NULL,
    activity_level REAL,
    body_temp_c    REAL,
    PRIMARY KEY (animal_id, date)
);
CREATE INDEX livestock_data_date ON livestock_data (date);
CREATE TABLE reference_yields (
    year            INTEGER NOT NULL,
    crop            TEXT NOT NULL,
    country         TEXT NOT NULL,
    yield_kg_per_ha REAL,
    yield_t_ha      REAL,
    source          TEXT
);
CREATE TABLE advice_templates (
    type       TEXT NOT NULL,
    category   TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    advice_en  TEXT NOT NULL,
    advice_tn  TEXT,
    PRIMARY KEY (type, category, risk_level)
);
"""

STATUS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS alert_status (
    alert_id   TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN {STATUSES}),
    updated_at TEXT NOT NULL
);
"""

# Live readings from the hardware prototype (firmware/). Like alert_status, this table
# is never dropped: rebuilding from the CSVs keeps every reading.
SENSOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS sensor_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,   -- UTC, ISO 8601
    gateway_id  TEXT NOT NULL,
    sensor      TEXT NOT NULL CHECK (sensor IN ('collar', 'rain')),
    animal_id   TEXT,            -- collar only
    body_temp_c REAL,            -- collar: skin temperature (infrared)
    ambient_c   REAL,            -- collar: air temperature at the sensor
    activity    INTEGER,         -- collar: 0-100, NULL without accelerometer
    rssi        INTEGER,         -- collar: Bluetooth signal strength at the gateway
    rain_level  INTEGER,         -- rain: 0-100
    rain_raw    INTEGER,
    rain_wet    INTEGER,         -- rain: 1 = raining
    lat         REAL,            -- position of the gateway
    lon         REAL
);
CREATE INDEX IF NOT EXISTS sensor_readings_latest
    ON sensor_readings (sensor, animal_id, received_at);
"""

# table name -> (CSV file inside data/, read options)
SOURCES = {
    "alerts": ("alerts/final_alerts.csv", {"encoding": "utf-8-sig"}),
    "crop_data": ("input/crop_data.csv", {}),
    "livestock_data": ("input/livestock_data.csv", {}),
    "reference_yields": ("input/public_reference_yields.csv", {}),
    "advice_templates": ("advice_templates.csv", {}),
}


def db_path() -> Path:
    return Path(os.environ.get("AGRISENSE_DB", DEFAULT_DB))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert(con: sqlite3.Connection, table: str, df: pd.DataFrame) -> None:
    df = df.astype(object).where(df.notna(), None)
    columns = ", ".join(df.columns)
    marks = ", ".join("?" * len(df.columns))
    con.executemany(f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                    df.itertuples(index=False, name=None))


def ensure_live_tables(con: sqlite3.Connection) -> None:
    """Create the tables that hold live data (statuses, sensor readings) if missing."""
    for statement in (STATUS_SCHEMA + SENSOR_SCHEMA).split(";"):
        if statement.strip():
            con.execute(statement)


def build(path: Path | None = None, source_dir: Path = DATA) -> dict[str, int]:
    """(Re)build the database at `path` from the CSVs and return row counts per table.

    Everything happens in one transaction: if a CSV doesn't fit the schema, the
    previous database is left as it was.
    """
    path = Path(path or db_path())
    frames = {table: pd.read_csv(source_dir / name, **options)
              for table, (name, options) in SOURCES.items()}
    for table in ("alerts", "advice_templates"):
        frames[table]["advice_tn"] = frames[table]["advice_tn"].fillna("")
    # Per-animal alert IDs look like LS-20260926-H3-05-STRAY.
    frames["alerts"]["animal_id"] = frames["alerts"]["alert_id"].str.extract(
        r"^LS-\d{8}-(.+)-[A-Z]+$", expand=False)

    con = sqlite3.connect(path, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            for table in SOURCES:
                con.execute(f"DROP TABLE IF EXISTS {table}")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    con.execute(statement)
            ensure_live_tables(con)
            for table, df in frames.items():
                insert(con, table, df)
            con.executemany(
                "INSERT OR IGNORE INTO alert_status (alert_id, status, updated_at) "
                "VALUES (?, 'open', ?)",
                [(alert_id, now()) for alert_id in frames["alerts"]["alert_id"]])
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in [*SOURCES, "alert_status", "sensor_readings"]}
    finally:
        con.close()
    return counts


def main() -> None:
    path = db_path()
    counts = build(path)
    print(f"Wrote {path.name}:")
    for table, n in counts.items():
        print(f"  {table}: {n} rows")


if __name__ == "__main__":
    main()

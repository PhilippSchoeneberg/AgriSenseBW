"""Write the daily summaries of the live sensor readings as CSV files.

data/input/sensor_livestock_daily.csv has the same columns as livestock_data.csv
(plus `readings`), so the livestock notebook can run on real collar data.
data/input/sensor_rain_daily.csv has the share of readings that were wet, because the
rain sensor tells wet or dry, not millimetres.

Usage, from the repository folder:
    python -m src.sensor_export [--herd H9] [--district Southern]
"""
from __future__ import annotations

import argparse

import pandas as pd

from . import data
from .build_database import DATA


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--herd", default="LIVE", help="herd_id to give the collar animals")
    parser.add_argument("--district", default="Southern")
    args = parser.parse_args()

    days = data.sensor_days(herd_id=args.herd, district=args.district)
    files = [("sensor_livestock_daily.csv", days["livestock"],
              ["date", "animal_id", "herd_id", "district", "lat", "lon",
               "activity_level", "body_temp_c", "readings"]),
             ("sensor_rain_daily.csv", days["rain"],
              ["date", "district", "wet_share", "max_rain_level", "readings"])]
    for name, rows, columns in files:
        path = DATA / "input" / name
        pd.DataFrame(rows, columns=columns).to_csv(path, index=False)
        print(f"Wrote {path.relative_to(DATA.parent)}: {len(rows)} rows")


if __name__ == "__main__":
    main()

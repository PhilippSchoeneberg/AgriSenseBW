"""AgriSense BW early-warning engine.

Combines crop_alerts.csv and livestock_alerts.csv into one alert list that
farmers can understand, and saves it as final_alerts.csv:

* the team's shared alert format stays the first six columns,
* every reason is rewritten in plain language; the numbers move to `details`,
* alerts for the same animal and problem on consecutive days become one alert,
* an animal that keeps moving away from its herd is raised to High,
* a disease-outbreak warning for veterinary services is added when 3 or more
  animals in one district are possibly ill within a few days,
* every animal without any alert gets one Low entry ("no alerts"), so each
  animal has a risk level, like each crop site,
* advice in English and Setswana comes from advice_templates.csv.

The outbreak rule counts only the possible-illness flags in livestock_alerts.csv.

Usage, from the repository folder:
    python -m src.alert_engine
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"
CROP_ALERTS = DATA / "alerts" / "crop_alerts.csv"
LIVESTOCK_ALERTS = DATA / "alerts" / "livestock_alerts.csv"
CROP_DATA = DATA / "input" / "crop_data.csv"
LIVESTOCK_DATA = DATA / "input" / "livestock_data.csv"
TEMPLATES = DATA / "advice_templates.csv"
OUTPUT = DATA / "alerts" / "final_alerts.csv"

# Rule settings: name these in the report.
OUTBREAK_MIN_ANIMALS = 3  # "3 or more animals in the same district ..."
OUTBREAK_WINDOW_DAYS = 7  # "... flagged as possibly ill within a few days"
FEVER_C = 39.5            # same value as HIGH_TEMP in the livestock notebook
STRAY_MIN_DAYS = 3        # unusual movement on this many days in a row -> High

SHARED_COLUMNS = ["district", "type", "risk_level", "reason", "advice_en", "advice_tn"]
EXTRA_COLUMNS = ["alert_id", "category", "subject", "for_whom",
                 "start_date", "end_date", "days", "details", "lat", "lon"]
BUILD_COLUMNS = ["district", "type", "risk_level", "reason"] + EXTRA_COLUMNS
RISK_RANK = {"High": 0, "Medium": 1, "Low": 2}

# Reasons used in livestock_alerts.csv, most serious first.
LIVESTOCK_REASONS = {
    "Low activity with high body temperature": "possible_illness",
    "Movement unusually high compared with herd": "unusual_movement",
    "Activity unusually low compared with herd": "low_activity",
}
# Per-animal categories and the short code used in their alert_id.
ANIMAL_CATEGORIES = {"possible_illness": "ILL", "unusual_movement": "MOV",
                     "low_activity": "ACT", "possible_theft_or_straying": "STRAY"}
NORMAL_CODE = "OK"  # alert_id code of the Low "normal" entry for animals without alerts

CROP_REASON = re.compile(
    r"^(?P<location>[^:]+): predicted yield (?P<predicted>[\d.]+) t/ha is "
    r"(?P<pct>\d+)% of the average \((?P<average>[\d.]+)\); "
    r"rainfall (?P<rainfall>\d+) mm, NDVI (?P<ndvi>[\d.]+)$"
)


# --------------------------------------------------------------------- helpers
def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km (same formula as the livestock notebook)."""
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(v, dtype=float))
                              for v in (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def day(d: pd.Timestamp) -> str:
    return f"{d.day} {d:%b %Y}"


def span(start: pd.Timestamp, end: pd.Timestamp, days: int) -> str:
    if days == 1:
        return f"on {day(start)}"
    if (start.year, start.month) == (end.year, end.month):
        dates = f"{start.day}–{end.day} {end:%b %Y}"
    else:
        dates = f"{start.day} {start:%b} – {day(end)}"
    return f"on {days} days in a row ({dates})"


def value_range(values: pd.Series, unit: str = "", digits: int = 1) -> str:
    low, high = values.min(), values.max()
    text = (f"{high:.{digits}f}" if round(low, digits) == round(high, digits)
            else f"{low:.{digits}f}–{high:.{digits}f}")
    return f"{text} {unit}" if unit else text


def slug(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", text.upper()).strip("-")


# ------------------------------------------------------------------ crop alerts
def crop_reason(location: str, pct: int, risk: str) -> str:
    gap = abs(100 - pct)
    if risk == "High":
        return (f"The harvest in {location} is expected to be much smaller than usual, "
                f"about {gap}% below the district average.")
    if risk == "Medium":
        return (f"The harvest in {location} is expected to be about {gap}% smaller "
                f"than the district average.")
    if pct > 100:
        return f"The harvest in {location} is expected to be about {gap}% above the district average."
    if pct == 100:
        return f"The harvest in {location} is expected to match the district average."
    return (f"The harvest in {location} is expected to be close to normal, "
            f"about {gap}% below the district average.")


def build_crop_alerts() -> pd.DataFrame:
    raw = pd.read_csv(CROP_ALERTS)
    parts = raw["reason"].str.extract(CROP_REASON)
    bad = parts.isna().any(axis=1)
    if bad.any():
        raise SystemExit("Unrecognised reason text in crop_alerts.csv:\n  "
                         + "\n  ".join(raw.loc[bad, "reason"]))
    parts = parts.astype({"predicted": float, "pct": int, "average": float,
                          "rainfall": int, "ndvi": float})
    data = pd.read_csv(CROP_DATA, parse_dates=["date"])

    rows = []
    for alert, p in zip(raw.to_dict("records"), parts.to_dict("records")):
        loc, pct, risk = p["location"], p["pct"], alert["risk_level"]
        # The crop alerts carry no date: find the season whose rainfall and NDVI they quote.
        same = data[(data["location"] == loc)
                    & ((data["rainfall_mm"] - p["rainfall"]).abs() <= 0.5)
                    & ((data["ndvi"] - p["ndvi"]).abs() <= 0.005)]
        if same.empty:
            print(f"Note: no season in crop_data.csv matches the {loc} alert; using the latest one.")
            season = data["date"].max()
        else:
            season = same["date"].max()
        expected = "High" if pct < 70 else "Medium" if pct <= 90 else "Low"
        if risk != expected:
            print(f"Note: {loc} is at {pct}% of the average but marked {risk} "
                  f"(the team's rule gives {expected}).")
        rows.append({
            "district": alert["district"], "type": "crop", "risk_level": risk,
            "reason": crop_reason(loc, pct, risk),
            "alert_id": f"CR-{season:%Y}-{slug(loc)}",
            "category": "crop_yield",
            "subject": f"Crops in {loc}",
            "for_whom": "Farmers",
            "start_date": season, "end_date": season, "days": pd.NA,
            "details": (f"Predicted yield {p['predicted']:.2f} t/ha = {pct}% of the district "
                        f"average ({p['average']:.2f} t/ha); rainfall {p['rainfall']} mm; "
                        f"NDVI {p['ndvi']:.2f}; season ending {day(season)}"),
            "lat": np.nan, "lon": np.nan,
        })
    return pd.DataFrame(rows, columns=BUILD_COLUMNS)


# ------------------------------------------------------------- livestock alerts
def load_livestock_data() -> pd.DataFrame:
    """Readings plus daily distance, distance from the herd and rest-of-herd averages."""
    ld = (pd.read_csv(LIVESTOCK_DATA, parse_dates=["date"])
            .sort_values(["animal_id", "date"]).reset_index(drop=True))
    prev = ld.groupby("animal_id")[["lat", "lon"]].shift()
    ld["distance_km"] = haversine_km(prev["lat"], prev["lon"], ld["lat"], ld["lon"])
    herd = ld.groupby(["date", "herd_id"])
    ld["km_from_herd"] = haversine_km(ld["lat"], ld["lon"],
                                      herd["lat"].transform("median"),
                                      herd["lon"].transform("median"))
    for col in ["activity_level", "distance_km"]:
        total = herd[col].transform("sum")
        count = herd[col].transform("count")
        own = ld[col].notna().astype(int)
        ld[f"herd_{col}"] = (total - ld[col].fillna(0)) / (count - own)
    return ld


def classify(reason: str) -> str:
    parts = [part.strip() for part in str(reason).split(";")]
    unknown = [part for part in parts if part not in LIVESTOCK_REASONS]
    if unknown:
        raise SystemExit(f"Unrecognised reason in livestock_alerts.csv: {unknown}")
    order = list(LIVESTOCK_REASONS)
    return LIVESTOCK_REASONS[min(parts, key=order.index)]


def load_livestock_alerts() -> pd.DataFrame:
    la = pd.read_csv(LIVESTOCK_ALERTS, parse_dates=["date"])
    la["category"] = la["reason"].map(classify)
    return la


def build_livestock_alerts(la: pd.DataFrame, ld: pd.DataFrame) -> pd.DataFrame:
    context = ld[["date", "animal_id", "lat", "lon", "km_from_herd",
                  "herd_activity_level", "herd_distance_km"]]
    la = (la.merge(context, on=["date", "animal_id"], how="left")
            .sort_values(["animal_id", "category", "date"]).reset_index(drop=True))
    # A new episode starts unless the same animal had the same problem the day before.
    gap = la.groupby(["animal_id", "category"])["date"].diff().dt.days
    la["episode"] = gap.ne(1).cumsum()

    rows = []
    for _, ep in la.groupby("episode"):
        first, last = ep.iloc[0], ep.iloc[-1]
        animal, herd, category = first["animal_id"], first["herd_id"], first["category"]
        start, end, days = first["date"], last["date"], len(ep)
        risk = min(ep["risk_level"], key=RISK_RANK.get)
        if category == "unusual_movement" and days >= STRAY_MIN_DAYS:
            category, risk = "possible_theft_or_straying", "High"

        who, when = f"Animal {animal} (herd {herd})", span(start, end, days)
        herd_activity = ep["herd_activity_level"].mean()
        herd_distance = ep["herd_distance_km"].mean()
        if category == "possible_illness":
            reason = (f"{who} has a high body temperature (up to {ep['body_temp_c'].max():.1f} °C) "
                      f"and is much less active than its herd {when}.")
            details = (f"Body temperature {value_range(ep['body_temp_c'], '°C', 2)} "
                       f"(fever from {FEVER_C} °C); activity {value_range(ep['activity_level'])} "
                       f"vs. {herd_activity:.1f} for the rest of the herd")
        elif category == "low_activity":
            reason = f"{who} was much less active than the rest of its herd {when}."
            details = (f"Activity {value_range(ep['activity_level'])} vs. {herd_activity:.1f} "
                       f"for the rest of the herd; body temperature "
                       f"{value_range(ep['body_temp_c'], '°C', 2)}")
        elif category == "unusual_movement":
            reason = f"{who} moved much further than the rest of its herd {when}."
            details = (f"Moved {value_range(ep['distance_km'], 'km', 2)} in a day vs. "
                       f"{herd_distance:.2f} km for the rest of the herd")
        else:  # possible_theft_or_straying
            reason = (f"{who} has moved away from its herd {when} and is now about "
                      f"{last['km_from_herd']:.0f} km from it. It may be lost or stolen.")
            details = (f"Moved {value_range(ep['distance_km'], 'km', 1)} per day vs. "
                       f"{herd_distance:.2f} km for the rest of the herd; "
                       f"{last['km_from_herd']:.1f} km from the herd's median position "
                       f"on {day(end)}")
        rows.append({
            "district": first["district"], "type": "livestock", "risk_level": risk,
            "reason": reason,
            "alert_id": f"LS-{start:%Y%m%d}-{animal}-{ANIMAL_CATEGORIES[category]}",
            "category": category,
            "subject": who,
            "for_whom": "Farmers",
            "start_date": start, "end_date": end, "days": days,
            "details": details,
            "lat": last["lat"], "lon": last["lon"],
        })
    return pd.DataFrame(rows, columns=BUILD_COLUMNS)


def build_normal_alerts(la: pd.DataFrame, ld: pd.DataFrame) -> pd.DataFrame:
    """One Low "normal" entry for each animal that has no alert at all.

    The wording only says that no alert was raised: the livestock model can miss
    animals (report() warns when one of them had a fever).
    """
    rows = []
    for animal, days in ld[~ld["animal_id"].isin(la["animal_id"])].groupby("animal_id"):
        first, last = days.iloc[0], days.iloc[-1]
        start, end = days["date"].min(), days["date"].max()
        who = f"Animal {animal} (herd {first['herd_id']})"
        rows.append({
            "district": first["district"], "type": "livestock", "risk_level": "Low",
            "reason": f"{who} had no alerts from {day(start)} to {day(end)}.",
            "alert_id": f"LS-{start:%Y%m%d}-{animal}-{NORMAL_CODE}",
            "category": "normal",
            "subject": who,
            "for_whom": "Farmers",
            "start_date": start, "end_date": end, "days": len(days),
            "details": (f"Body temperature {value_range(days['body_temp_c'], '°C', 2)}; "
                        f"activity {days['activity_level'].mean():.1f} on average vs. "
                        f"{days['herd_activity_level'].mean():.1f} for the rest of the herd; "
                        f"moved {days['distance_km'].mean():.2f} km a day on average"),
            "lat": last["lat"], "lon": last["lon"],
        })
    return pd.DataFrame(rows, columns=BUILD_COLUMNS)


# ----------------------------------------------------------------- outbreak rule
def find_outbreaks(ill: pd.DataFrame, min_animals: int = OUTBREAK_MIN_ANIMALS,
                   window_days: int = OUTBREAK_WINDOW_DAYS) -> list[dict]:
    """Group possibly ill animals into outbreak clusters.

    `ill` has one row per day on which an animal counts as possibly ill, with the
    columns date, district, animal_id and herd_id. A cluster starts on the first
    day on which at least `min_animals` different animals in one district were
    possibly ill within the last `window_days` days, and lasts while that holds.
    """
    clusters = []
    window = pd.Timedelta(window_days, unit="D")
    ill = ill.drop_duplicates(["date", "district", "animal_id"])
    for district, rows in ill.groupby("district"):
        current = None
        for today in sorted(rows["date"].unique()):
            today = pd.Timestamp(today)
            recent = rows[(rows["date"] > today - window) & (rows["date"] <= today)]
            if recent["animal_id"].nunique() >= min_animals:
                if current is None:
                    current = {"district": district, "detected": today,
                               "first_sign": recent["date"].min(),
                               "animals": set(), "herds": set()}
                current["until"] = today
                current["animals"] |= set(recent["animal_id"])
                current["herds"] |= set(recent["herd_id"])
            elif current is not None:
                clusters.append(current)
                current = None
        if current is not None:
            clusters.append(current)
    return clusters


def possibly_ill(la: pd.DataFrame) -> pd.DataFrame:
    return la.loc[la["category"] == "possible_illness",
                  ["date", "district", "animal_id", "herd_id"]]


def build_outbreak_alerts(la: pd.DataFrame, ld: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for c in find_outbreaks(possibly_ill(la)):
        animals, herds = sorted(c["animals"]), sorted(c["herds"])
        herd_text = f"herd {herds[0]}" if len(herds) == 1 else "herds " + ", ".join(herds)
        position = ld[(ld["date"] == c["detected"]) & ld["animal_id"].isin(animals)]
        peak = (ld[ld["animal_id"].isin(animals)
                   & ld["date"].between(c["first_sign"], c["until"])]
                .groupby("animal_id")["body_temp_c"].max())
        rows.append({
            "district": c["district"], "type": "livestock", "risk_level": "High",
            "reason": (f"Possible disease outbreak: {len(animals)} animals in {herd_text} "
                       f"({', '.join(animals)}) have shown signs of illness since "
                       f"{day(c['first_sign'])}."),
            "alert_id": f"OB-{c['detected']:%Y%m%d}-{slug(c['district'])}",
            "category": "disease_outbreak",
            "subject": f"{c['district']} district, {herd_text}",
            "for_whom": "Veterinary services",
            "start_date": c["detected"], "end_date": c["until"],
            "days": (c["until"] - c["detected"]).days + 1,
            "details": (f"Rule: {OUTBREAK_MIN_ANIMALS} or more possibly ill animals in one "
                        f"district within {OUTBREAK_WINDOW_DAYS} days, based on the "
                        "possible-illness flags in livestock_alerts.csv. "
                        "Highest body temperatures: "
                        + ", ".join(f"{a} {t:.2f} °C" for a, t in peak.items())),
            "lat": position["lat"].mean(), "lon": position["lon"].mean(),
        })
    return pd.DataFrame(rows, columns=BUILD_COLUMNS)


# ------------------------------------------------------------- advice and output
def add_advice(alerts: pd.DataFrame) -> pd.DataFrame:
    templates = pd.read_csv(TEMPLATES, dtype=str).fillna("")
    key = ["type", "category", "risk_level"]
    if templates.duplicated(key).any():
        raise SystemExit("advice_templates.csv has more than one row for:\n"
                         + templates.loc[templates.duplicated(key), key].to_string(index=False))
    merged = alerts.merge(templates, on=key, how="left")
    missing = merged["advice_en"].isna()
    if missing.any():
        raise SystemExit("advice_templates.csv has no advice for:\n"
                         + merged.loc[missing, key].drop_duplicates().to_string(index=False))
    return merged


def check(final: pd.DataFrame, crop_in: int, livestock_in: int, animals: set[str]) -> None:
    problems = []
    if final["alert_id"].duplicated().any():
        problems.append("duplicate alert_id: "
                        + ", ".join(final.loc[final["alert_id"].duplicated(), "alert_id"]))
    if not final["type"].isin(["crop", "livestock"]).all():
        problems.append("type must be crop or livestock")
    if not final["risk_level"].isin(list(RISK_RANK)).all():
        problems.append("risk_level must be Low, Medium or High")
    for col in ["district", "type", "risk_level", "reason", "advice_en"]:
        if final[col].fillna("").eq("").any():
            problems.append(f"empty {col}")
    if (final["type"] == "crop").sum() != crop_in:
        problems.append("crop alerts were lost or duplicated")
    animal_days = final.loc[final["category"].isin(list(ANIMAL_CATEGORIES)), "days"].sum()
    if animal_days != livestock_in:
        problems.append(f"livestock alerts cover {animal_days} animal-days, "
                        f"but livestock_alerts.csv has {livestock_in} rows")
    per_animal = final[final["category"].isin([*ANIMAL_CATEGORIES, "normal"])]
    covered = set(per_animal["subject"].str.extract(r"^Animal (\S+)", expand=False))
    if covered != animals:
        problems.append("animals without any livestock entry: "
                        + ", ".join(sorted(animals - covered)))
    if problems:
        raise SystemExit("final_alerts.csv was not written:\n  - " + "\n  - ".join(problems))


def report(out: pd.DataFrame, la: pd.DataFrame) -> None:
    risk = out["risk_level"].value_counts().reindex(list(RISK_RANK), fill_value=0)
    outbreaks = out[out["category"] == "disease_outbreak"]
    animals = out[out["category"].isin(list(ANIMAL_CATEGORIES))]
    print(f"Wrote {OUTPUT.name}: {len(out)} alerts "
          f"(High {risk['High']}, Medium {risk['Medium']}, Low {risk['Low']})")
    normal = (out["category"] == "normal").sum()
    print(f"  crop: {(out['type'] == 'crop').sum()}, livestock: {len(animals)} "
          f"(from {len(la)} daily alerts) + {normal} Low for animals without alerts, "
          f"outbreak warnings: {len(outbreaks)}")
    print("  outbreak rule input: possible-illness flags in livestock_alerts.csv")
    for _, o in outbreaks.iterrows():
        print(f"  {o['alert_id']}: {o['reason']}")
    normal = out[out["category"] == "normal"]
    fever = (load_livestock_data().query("body_temp_c >= @FEVER_C")
             .groupby("animal_id")["date"].agg(["count", "min", "max"]))
    for animal in normal["subject"].str.extract(r"^Animal (\S+)", expand=False):
        if animal in fever.index:
            f = fever.loc[animal]
            print(f"  Note: {animal} is Low (no alerts in livestock_alerts.csv) but had a body "
                  f"temperature of {FEVER_C} °C or more on {f['count']} days "
                  f"({day(f['min'])} to {day(f['max'])}).")
    if outbreaks.empty:
        ill = sorted(possibly_ill(la)["animal_id"].unique())
        print(f"  no outbreak: possibly ill animals are {', '.join(ill) or 'none'} "
              f"({len(ill)} < {OUTBREAK_MIN_ANIMALS})")
    templates = pd.read_csv(TEMPLATES, dtype=str).fillna("")
    no_tn = templates[templates["advice_tn"].str.strip() == ""]
    if len(no_tn):
        print(f"Warning: {len(no_tn)} advice templates still need Setswana "
              f"(fill advice_tn in {TEMPLATES.name} and run again):")
        for _, t in no_tn.iterrows():
            print(f"  - {t['type']} / {t['category']} / {t['risk_level']}")


def main() -> None:
    ld = load_livestock_data()
    la = load_livestock_alerts()
    frames = [build_outbreak_alerts(la, ld),
              build_livestock_alerts(la, ld),
              build_normal_alerts(la, ld),
              build_crop_alerts()]
    final = add_advice(pd.concat([f for f in frames if len(f)], ignore_index=True))

    final["_outbreak_last"] = (final["category"] != "disease_outbreak").astype(int)
    final["_risk"] = final["risk_level"].map(RISK_RANK)
    final = (final.sort_values(["_outbreak_last", "_risk", "start_date", "alert_id"],
                               ascending=[True, True, False, True])
                  .drop(columns=["_outbreak_last", "_risk"]).reset_index(drop=True))
    check(final, crop_in=len(pd.read_csv(CROP_ALERTS)), livestock_in=len(la),
          animals=set(ld["animal_id"]))

    out = final[SHARED_COLUMNS + EXTRA_COLUMNS].copy()
    for col in ["start_date", "end_date"]:
        out[col] = pd.to_datetime(out[col]).dt.strftime("%Y-%m-%d")
    out["days"] = out["days"].astype("Int64")
    out[["lat", "lon"]] = out[["lat", "lon"]].astype(float).round(6)
    out.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    report(out, la)


if __name__ == "__main__":
    main()

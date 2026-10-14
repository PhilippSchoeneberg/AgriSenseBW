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
* advice in English and Setswana comes from advice_templates.csv,
* every text the engine writes (subject, reason, details) is written in English and
  Setswana, from the sentence templates in translations.csv (see src/i18n.py).

The outbreak rule counts only the possible-illness flags in livestock_alerts.csv.

Usage, from the repository folder:
    python -m src.alert_engine
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from .i18n import LANGUAGES, day_label, draft_count, say

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
# The same texts in the other languages, e.g. reason_tn (Setswana).
TRANSLATED = ["subject", "reason", "details"]
LANG_COLUMNS = [f"{col}_{lang}" for lang in LANGUAGES if lang != "en" for col in TRANSLATED]
BUILD_COLUMNS = ["district", "type", "risk_level", "reason"] + EXTRA_COLUMNS + LANG_COLUMNS
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


def day(d: pd.Timestamp, lang: str = "en") -> str:
    """26 Sep 2026 (English) or 26 Lwetse 2026 (Setswana)."""
    return day_label(d, lang)


def span(start: pd.Timestamp, end: pd.Timestamp, days: int, lang: str = "en") -> str:
    """'on 26 Sep 2026' or 'on 5 days in a row (26–30 Sep 2026)', in `lang`."""
    if days == 1:
        return say("when.one", lang, date=day(start, lang))
    if (start.year, start.month) == (end.year, end.month):
        dates = f"{start.day}–{end.day} {say(f'month.{end.month}', lang)} {end.year}"
    else:
        dates = f"{start.day} {say(f'month.{start.month}', lang)} – {day(end, lang)}"
    return say("when.many", lang, days=days, dates=dates)


def texts(column: str, key: str, per_lang=lambda lang: {}, **values) -> dict[str, str]:
    """One text column in every language: {"reason": "...", "reason_tn": "..."}.

    `values` are the same in every language (numbers); `per_lang(lang)` returns the
    parts that depend on the language (dates, "Animal H3-05 (herd H3)", ...).
    """
    return {column if lang == "en" else f"{column}_{lang}": say(key, lang, **values, **per_lang(lang))
            for lang in LANGUAGES}


def value_range(values: pd.Series, unit: str = "", digits: int = 1) -> str:
    low, high = values.min(), values.max()
    text = (f"{high:.{digits}f}" if round(low, digits) == round(high, digits)
            else f"{low:.{digits}f}–{high:.{digits}f}")
    return f"{text} {unit}" if unit else text


def slug(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", text.upper()).strip("-")


# ------------------------------------------------------------------ crop alerts
def crop_reason_key(pct: int, risk: str) -> str:
    """Which reason sentence fits: by risk for High/Medium, by percentage for Low."""
    if risk in ("High", "Medium"):
        return f"reason.crop.{risk}"
    return "reason.crop." + ("above" if pct > 100 else "equal" if pct == 100 else "near")


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
            **texts("reason", crop_reason_key(pct, risk), location=loc, gap=abs(100 - pct)),
            "alert_id": f"CR-{season:%Y}-{slug(loc)}",
            "category": "crop_yield",
            **texts("subject", "subject.crop", location=loc),
            "for_whom": "Farmers",
            "start_date": season, "end_date": season, "days": pd.NA,
            **texts("details", "details.crop", lambda lang: {"season": day(season, lang)},
                    predicted=f"{p['predicted']:.2f}", pct=pct, average=f"{p['average']:.2f}",
                    rainfall=p["rainfall"], ndvi=f"{p['ndvi']:.2f}"),
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

        # The parts of the texts that depend on the language
        def per_lang(lang, animal=animal, herd=herd, start=start, end=end, days=days):
            return {"who": say("subject.animal", lang, animal=animal, herd=herd),
                    "when": span(start, end, days, lang), "date": day(end, lang)}

        herd_activity = f"{ep['herd_activity_level'].mean():.1f}"
        herd_distance = f"{ep['herd_distance_km'].mean():.2f}"
        temps = value_range(ep["body_temp_c"], "°C", 2)
        activity = value_range(ep["activity_level"])
        if category == "possible_illness":
            values = {"max_temp": f"{ep['body_temp_c'].max():.1f}", "temps": temps, "fever": FEVER_C,
                      "activity": activity, "herd_activity": herd_activity}
        elif category == "low_activity":
            values = {"activity": activity, "herd_activity": herd_activity, "temps": temps}
        elif category == "unusual_movement":
            values = {"distance": value_range(ep["distance_km"], "km", 2), "herd_distance": herd_distance}
        else:  # possible_theft_or_straying
            values = {"km": f"{last['km_from_herd']:.0f}", "km_exact": f"{last['km_from_herd']:.1f}",
                      "distance": value_range(ep["distance_km"], "km", 1), "herd_distance": herd_distance}
        rows.append({
            "district": first["district"], "type": "livestock", "risk_level": risk,
            **texts("reason", f"reason.{category}", per_lang, **values),
            "alert_id": f"LS-{start:%Y%m%d}-{animal}-{ANIMAL_CATEGORIES[category]}",
            "category": category,
            **texts("subject", "subject.animal", animal=animal, herd=herd),
            "for_whom": "Farmers",
            "start_date": start, "end_date": end, "days": days,
            **texts("details", f"details.{category}", per_lang, **values),
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
        herd = first["herd_id"]

        def per_lang(lang, animal=animal, herd=herd, start=start, end=end):
            return {"who": say("subject.animal", lang, animal=animal, herd=herd),
                    "start": day(start, lang), "end": day(end, lang)}

        rows.append({
            "district": first["district"], "type": "livestock", "risk_level": "Low",
            **texts("reason", "reason.normal", per_lang),
            "alert_id": f"LS-{start:%Y%m%d}-{animal}-{NORMAL_CODE}",
            "category": "normal",
            **texts("subject", "subject.animal", animal=animal, herd=herd),
            "for_whom": "Farmers",
            "start_date": start, "end_date": end, "days": len(days),
            **texts("details", "details.normal",
                    temps=value_range(days["body_temp_c"], "°C", 2),
                    activity=f"{days['activity_level'].mean():.1f}",
                    herd_activity=f"{days['herd_activity_level'].mean():.1f}",
                    distance=f"{days['distance_km'].mean():.2f}"),
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

        def herd_text(lang, herds=herds):
            return (say("herds.one", lang, herd=herds[0]) if len(herds) == 1
                    else say("herds.many", lang, herds=", ".join(herds)))

        def per_lang(lang, c=c):
            return {"herds": herd_text(lang), "date": day(c["first_sign"], lang)}

        position = ld[(ld["date"] == c["detected"]) & ld["animal_id"].isin(animals)]
        peak = (ld[ld["animal_id"].isin(animals)
                   & ld["date"].between(c["first_sign"], c["until"])]
                .groupby("animal_id")["body_temp_c"].max())
        rows.append({
            "district": c["district"], "type": "livestock", "risk_level": "High",
            **texts("reason", "reason.disease_outbreak", per_lang,
                    count=len(animals), animals=", ".join(animals)),
            "alert_id": f"OB-{c['detected']:%Y%m%d}-{slug(c['district'])}",
            "category": "disease_outbreak",
            **texts("subject", "subject.outbreak", lambda lang: {"herds": herd_text(lang)},
                    district=c["district"]),
            "for_whom": "Veterinary services",
            "start_date": c["detected"], "end_date": c["until"],
            "days": (c["until"] - c["detected"]).days + 1,
            **texts("details", "details.disease_outbreak",
                    min=OUTBREAK_MIN_ANIMALS, window=OUTBREAK_WINDOW_DAYS,
                    peaks=", ".join(f"{a} {t:.2f} °C" for a, t in peak.items())),
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
    drafts, total = draft_count()
    if drafts:
        print(f"Note: {drafts} of {total} Setswana texts in translations.csv are drafts "
              f"(tn_status is not 'checked'): please have a native speaker check them.")
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

    out = final[SHARED_COLUMNS + EXTRA_COLUMNS + LANG_COLUMNS].copy()
    for col in ["start_date", "end_date"]:
        out[col] = pd.to_datetime(out[col]).dt.strftime("%Y-%m-%d")
    out["days"] = out["days"].astype("Int64")
    out[["lat", "lon"]] = out[["lat", "lon"]].astype(float).round(6)
    out.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    report(out, la)


if __name__ == "__main__":
    main()

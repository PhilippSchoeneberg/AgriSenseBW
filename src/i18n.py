"""Texts in English and Setswana, read from data/translations.csv.

Every text of the dashboard and every sentence the alert engine writes has a key,
e.g. "tab.crops", with its English (`en`) and Setswana (`tn`) version. Texts can
contain placeholders in curly brackets, e.g. "Collar {id}", which `say()` fills in.

`tn_status` says whether a native speaker has checked the Setswana: "draft" or
"checked". An empty Setswana text falls back to English.

Edit the CSV file to change or translate a text; the dashboard picks up the change
on the next page load, the engine on its next run.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "data" / "translations.csv"
LANGUAGES = ("en", "tn")
DEFAULT = "en"

_cache: dict = {"mtime": None, "rows": {}}


def _rows() -> dict[str, dict[str, str]]:
    """All rows of translations.csv by key, read again whenever the file changes."""
    mtime = FILE.stat().st_mtime
    if _cache["mtime"] != mtime:
        with FILE.open(encoding="utf-8-sig", newline="") as f:
            _cache["rows"] = {row["key"]: row for row in csv.DictReader(f)}
        _cache["mtime"] = mtime
    return _cache["rows"]


def language(value: str | None) -> str:
    """A supported language code, or the default."""
    return value if value in LANGUAGES else DEFAULT


def texts(lang: str) -> dict[str, str]:
    """Every text in one language; missing Setswana falls back to English."""
    return {key: (row.get(lang) or "").strip() or row["en"] for key, row in _rows().items()}


def say(key: str, lang: str = DEFAULT, **values) -> str:
    """One text in `lang`, with its {placeholders} filled in."""
    row = _rows().get(key)
    if row is None:
        raise KeyError(f"No text with key {key!r} in {FILE.name}")
    text = (row.get(lang) or "").strip() or row["en"]
    return text.format(**values)


def draft_count() -> tuple[int, int]:
    """(Setswana texts not yet checked by a native speaker, all texts)."""
    rows = _rows().values()
    return sum(row.get("tn_status", "").strip() != "checked" for row in rows), len(rows)


def day_label(d: date, lang: str = DEFAULT, weekday: bool = False) -> str:
    """26 Sep 2026 / 26 Lwetse 2026; with weekday: Sat 26 Sep 2026."""
    text = f"{d.day} {say(f'month.{d.month}', lang)} {d.year}"
    return f"{say(f'weekday.{d.weekday()}', lang)} {text}" if weekday else text

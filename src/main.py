"""AgriSense BW dashboard: FastAPI app with one page and a small JSON API.

Run from the repository folder:
    uvicorn src.main:app --reload
then open http://127.0.0.1:8000 (API documentation at /docs).

Live sensor readings arrive at POST /api/readings from the UNO sensor station, through
firmware/serial_bridge.py (see firmware/README.md).

Language: the page is shown in English or Setswana, chosen with the switch in the top
bar (stored in the cookie "lang") or with a link like http://127.0.0.1:8000/?lang=tn.
All texts come from data/translations.csv (see src/i18n.py).
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import data, i18n

HERE = Path(__file__).resolve().parent
AlertType = Literal["crop", "livestock"]
Risk = Literal["Low", "Medium", "High"]
Status = Literal["open", "acknowledged", "resolved"]

app = FastAPI(
    title="AgriSense BW",
    summary="Early-warning alerts for crop and livestock in Botswana (demo with sample data).",
    version="1.0",
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ?v=<last change> on our own CSS/JS links, so browsers never run an outdated cached copy.
templates.env.globals["version"] = lambda path: int((HERE / "static" / path).stat().st_mtime)


class StatusUpdate(BaseModel):
    status: Status


# --------------------------------------------------------- hardware prototype
class CollarReading(BaseModel):
    sensor: Literal["collar"]
    animal_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,6}$")
    body_temp_c: float | None = Field(None, ge=-40, le=100, description="skin temperature (infrared)")
    ambient_c: float | None = Field(None, ge=-40, le=100)
    activity: int | None = Field(None, ge=0, le=100)
    rssi: int | None = Field(None, ge=-127, le=20)


class RainReading(BaseModel):
    sensor: Literal["rain"]
    rain_level: int = Field(ge=0, le=100)
    rain_raw: int | None = Field(None, ge=0, le=4095)
    rain_wet: bool


class ReadingBatch(BaseModel):
    gateway_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    readings: list[Annotated[CollarReading | RainReading, Field(discriminator="sensor")]] = Field(max_length=50)


# If AGRISENSE_SENSOR_KEY is set, gateways must send the same text as X-Sensor-Key.
SENSOR_KEY = os.environ.get("AGRISENSE_SENSOR_KEY", "")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request, lang: str | None = None):
    """The dashboard page, in the language from ?lang=, the "lang" cookie, or English."""
    lang = i18n.language(lang or request.cookies.get("lang"))
    drafts, _ = i18n.draft_count()
    return templates.TemplateResponse(request, "index.html", {
        "summary": data.summary(),
        "dates": data.livestock_dates(),
        "live_high_temp_c": data.LIVE_HIGH_TEMP_C,
        "lang": lang,
        "languages": i18n.LANGUAGES,
        "strings": i18n.texts(lang),   # every text, also handed to app.js
        "t": lambda key, **values: i18n.say(key, lang, **values),
        "day_label": lambda iso: i18n.day_label(dt.date.fromisoformat(iso), lang, weekday=True),
        "tn_drafts": lang == "tn" and drafts > 0,
    })


@app.get("/api/summary", tags=["alerts"])
def get_summary() -> dict:
    """Alert counts by risk, type and status, plus a few facts about the data."""
    return data.summary()


@app.get("/api/alerts", tags=["alerts"])
def get_alerts(type: AlertType | None = None,
               risk: Annotated[list[Risk] | None, Query()] = None,
               status: Status | None = None, since: dt.date | None = None) -> list[dict]:
    """Alerts from final_alerts.csv with their current status, most urgent first.

    `risk` can be given more than once (`?risk=High&risk=Medium`). `since` keeps
    alerts that were still active on or after that date.
    """
    return data.alerts(type=type, risk=risk, status=status,
                       since=since.isoformat() if since else None)


@app.patch("/api/alerts/{alert_id}/status", tags=["alerts"])
def update_status(alert_id: str, update: StatusUpdate) -> dict:
    """Mark an alert as open, acknowledged or resolved. The status survives a database rebuild."""
    alert = data.set_status(alert_id, update.status)
    if alert is None:
        raise HTTPException(404, f"No alert with ID {alert_id}")
    return alert


@app.get("/api/livestock/positions", tags=["livestock"])
def get_positions(date: dt.date | None = None) -> dict:
    """Each animal's position on a day (default: the latest), and whether it has an alert."""
    result = data.positions(date.isoformat() if date else None)
    if result is None:
        raise HTTPException(404, f"No livestock readings on {date}")
    return result


@app.get("/api/livestock/{animal_id}", tags=["livestock"])
def get_animal(animal_id: str) -> dict:
    """Daily activity, body temperature and distance moved for one animal, plus its alerts."""
    result = data.animal(animal_id)
    if result is None:
        raise HTTPException(404, f"No animal with ID {animal_id}")
    return result


@app.get("/api/crops", tags=["crop"])
def get_crops() -> dict:
    """Yield per site and season, and the crop alerts with their predicted yield."""
    return data.crops()


@app.post("/api/readings", status_code=201, tags=["sensors"])
def post_readings(batch: ReadingBatch,
                  x_sensor_key: Annotated[str | None, Header()] = None) -> dict:
    """Receive a batch of live readings, e.g. from firmware/serial_bridge.py."""
    if SENSOR_KEY and not secrets.compare_digest(x_sensor_key or "", SENSOR_KEY):
        raise HTTPException(401, "Missing or wrong X-Sensor-Key")
    stored = data.store_readings(batch.gateway_id, batch.lat, batch.lon,
                                 [r.model_dump() for r in batch.readings])
    return {"stored": stored}


@app.get("/api/sensors/live", tags=["sensors"])
def get_live_sensors(minutes: Annotated[int, Query(ge=1, le=240)] = 10) -> dict:
    """Latest state of every collar and the rain sensor, with the last `minutes` of history."""
    return data.live_sensors(minutes)


@app.get("/api/sensors/daily", tags=["sensors"])
def get_sensor_days() -> dict:
    """Daily summaries of the live readings, shaped like livestock_data.csv."""
    return data.sensor_days()

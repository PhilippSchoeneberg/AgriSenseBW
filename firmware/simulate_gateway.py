"""Pretend to be the ESP32 gateway: send made-up readings to the dashboard.

Useful to test the Sensors tab without hardware, or as a fallback at the booth.
Sends the same JSON as firmware/gateway every 5 seconds:
  - collar H1-01: normal skin temperature
  - collar H1-02: warms up to a high temperature after about a minute
  - collar H1-03: stops sending after a while, so it shows as "not seen"
  - rain sensor: dry, then rain, then dry again

Usage (dashboard running), from the repository folder:
    python firmware/simulate_gateway.py [--url http://127.0.0.1:8000/api/readings] [--key SECRET]
Stop with Ctrl+C.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import time
import urllib.error
import urllib.request


def batch(step: int) -> dict:
    t = step * 5  # seconds since start
    collars = [
        ("H1-01", 33.0 + 0.4 * math.sin(t / 40), 35),
        ("H1-02", min(38.6, 33.2 + max(0, t - 60) * 0.06), 10 if t > 60 else 40),
    ]
    if t < 90:  # H1-03 goes quiet after 90 s
        collars.append(("H1-03", 32.6 + 0.3 * math.cos(t / 30), 55))
    readings = [{"sensor": "collar", "animal_id": a, "body_temp_c": round(temp + random.uniform(-0.1, 0.1), 2),
                 "ambient_c": round(24.5 + random.uniform(-0.3, 0.3), 2),
                 "activity": max(0, min(100, act + random.randint(-5, 5))), "rssi": random.randint(-75, -55)}
                for a, temp, act in collars]
    level = 70 if 40 <= (t % 180) < 100 else 0
    readings.append({"sensor": "rain", "rain_level": level, "rain_raw": 4095 - level * 29, "rain_wet": level >= 20})
    return {"gateway_id": "simulator", "lat": -24.98, "lon": 25.34, "readings": readings}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000/api/readings")
    parser.add_argument("--key", default="", help="X-Sensor-Key, if AGRISENSE_SENSOR_KEY is set")
    parser.add_argument("--steps", type=int, default=0, help="stop after this many batches (0 = run forever)")
    args = parser.parse_args()

    step = 0
    while not args.steps or step < args.steps:
        body = json.dumps(batch(step)).encode()
        headers = {"Content-Type": "application/json", **({"X-Sensor-Key": args.key} if args.key else {})}
        try:
            with urllib.request.urlopen(urllib.request.Request(args.url, body, headers), timeout=5) as r:
                print(f"batch {step}: {r.status} {r.read().decode()}")
        except (urllib.error.URLError, OSError) as err:
            print(f"batch {step}: failed ({err})")
        step += 1
        if not args.steps or step < args.steps:
            time.sleep(5)


if __name__ == "__main__":
    main()

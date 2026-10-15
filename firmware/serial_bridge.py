"""Forward the readings of the UNO sensor station (firmware/uno_station) to the dashboard.

The UNO has no Wi-Fi. It prints one line of JSON per measurement over USB; this script
adds the station's name and position and sends every line to POST /api/readings, as the
ESP32 gateway does over Wi-Fi.

Usage (dashboard running), from the repository folder:
    python firmware/serial_bridge.py [--port /dev/cu.usbmodem1101] [--url http://127.0.0.1:8000/api/readings]
Needs pyserial (pip install pyserial). Close the Arduino IDE's Serial Monitor first: only one
program can use the port at a time. Stop with Ctrl+C.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

import serial
from serial.tools import list_ports

ARDUINO_USB_VENDORS = {0x2341, 0x2A03, 0x1A86, 0x0403}  # Arduino, Arduino.org, CH340 clones, FTDI


def find_port() -> str:
    """The serial port of the UNO; ask for --port if it is not clear which one."""
    ports = [p for p in list_ports.comports() if p.vid in ARDUINO_USB_VENDORS]
    if len(ports) == 1:
        return ports[0].device
    names = ", ".join(p.device for p in list_ports.comports()) or "none"
    raise SystemExit(f"Could not tell which port is the UNO (ports: {names}). Pass it with --port.")


def post(url: str, key: str, body: dict) -> str:
    headers = {"Content-Type": "application/json", **({"X-Sensor-Key": key} if key else {})}
    request = urllib.request.Request(url, json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return str(response.status)
    except urllib.error.HTTPError as err:
        return f"{err.code} {err.read().decode()[:200]}"
    except (urllib.error.URLError, OSError) as err:
        return f"failed ({err}) - is the dashboard running?"


def describe(readings: list[dict]) -> str:
    parts = []
    for r in readings:
        if r["sensor"] == "rain":
            parts.append(f"rain {r['rain_level']} % ({'wet' if r['rain_wet'] else 'dry'}, raw {r['rain_raw']})")
        else:
            skin, air = r.get("body_temp_c"), r.get("ambient_c")
            parts.append(f"{r['animal_id']} skin {skin if skin is not None else '-'} °C, "
                         f"air {air if air is not None else '-'} °C")
    return ", ".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", help="serial port of the UNO (found automatically if left out)")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api/readings")
    parser.add_argument("--key", default="", help="X-Sensor-Key, if AGRISENSE_SENSOR_KEY is set")
    parser.add_argument("--station-id", default="uno-station", help="name shown as the gateway")
    parser.add_argument("--lat", type=float, default=-24.98, help="position of the station")
    parser.add_argument("--lon", type=float, default=25.34)
    args = parser.parse_args()

    port = args.port or find_port()
    while True:
        try:
            with serial.Serial(port, 115200, timeout=10) as uno:
                print(f"Listening to the UNO on {port}")
                while True:
                    line = uno.readline().decode(errors="replace").strip()
                    if line.startswith("{"):
                        try:
                            readings = json.loads(line)["readings"]
                        except (json.JSONDecodeError, KeyError, TypeError):
                            print(f"UNO sent a broken line, skipped: {line[:80]}")
                            continue
                        body = {"gateway_id": args.station_id, "lat": args.lat, "lon": args.lon,
                                "readings": readings}
                        print(f"{describe(readings)} -> {post(args.url, args.key, body)}")
                    elif line:
                        print(f"UNO: {line.lstrip('# ')}")
        except serial.SerialException as err:
            print(f"UNO not reachable on {port} ({err}); trying again in 3 s")
            time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

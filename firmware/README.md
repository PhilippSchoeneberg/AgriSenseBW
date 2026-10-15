# AgriSense BW hardware prototype

An Arduino UNO measures rain and temperature and sends the readings over USB to the laptop,
which forwards them to the dashboard's **Sensors** tab. Tested on the board. Rain and air
temperature work. The skin temperature from the GY-906 we have does not
(see [Limits](#limits-of-this-prototype)).

```
 UNO sensor station              USB      Laptop
 Arduino UNO                   ────────►  serial_bridge.py  ──►  FastAPI dashboard
 + Flying Fish rain sensor                                       POST /api/readings
 + GY-906 infrared thermometer                                   → Sensors tab
```

| File / folder | Runs on | What it does |
|---|---|---|
| `uno_station/uno_station.ino` | Arduino UNO | Reads the rain sensor and the GY-906 every 2 s and prints the readings as a line of JSON over USB |
| `serial_bridge.py` | laptop | Forwards every line to the dashboard (`POST /api/readings`) |

## 1. Wiring

| Sensor | Pin | UNO | Why |
|---|---|---|---|
| GY-906 (MLX90614) | VIN | 3.3V | This GY-906 pulls its data lines up to VIN; with 5V they measured 4.8 V, too much for the 3.3 V sensor chip |
| | GND | GND | |
| | SDA | A4 | |
| | SCL | A5 | |
| Flying Fish rain sensor (control board) | VCC | pin 7 | Powered only while measuring, so the plate corrodes less |
| | GND | another GND (the UNO has three) | |
| | AO | A0 | |
| | DO | – | Not needed |

The plate connects to the rain sensor's control board with the two short wires; either way round works.

## 2. Upload the UNO program

1. Open `uno_station/uno_station.ino` in the Arduino IDE. The UNO's board package (*Arduino AVR Boards*) comes with the IDE, and no libraries are needed.
2. Choose the board *Arduino AVR Boards → Arduino Uno* and its port (`/dev/cu.usbmodem…`), then upload.
3. In the Serial Monitor (115200 baud) you should see `# GY-906 found` and then, every 2 s, a line such as
   `{"readings":[{"sensor":"rain","rain_level":2,"rain_raw":1004,"rain_wet":false},{"sensor":"collar","animal_id":"H1-02",…}]}`.

**Calibrating the rain sensor:** note the `raw` value with a dry plate and with a soaking-wet plate, and put them into `RAIN_DRY_RAW` and `RAIN_WET_RAW` in `uno_station.ino`. The UNO reads 0–1023, so dry is about 1000.

## 3. Run it

1. Install the Python packages once (`pyserial` is needed for the bridge):

   ```bash
   pip install -r requirements.txt
   ```

2. Start the dashboard:

   ```bash
   uvicorn src.main:app --reload
   ```

3. **Close the Arduino IDE's Serial Monitor** (only one program can use the port), then start the bridge in a second terminal:

   ```bash
   python firmware/serial_bridge.py
   ```

   It finds the UNO by itself; otherwise pass `--port /dev/cu.usbmodem…`. Every 2 s it prints what it forwarded and the dashboard's answer (`201`). Stop it with Ctrl+C.

On the **Sensors** tab, the rain sensor has its own card. The GY-906 appears as collar `H1-02` (`TEMP_SENSOR_ID` in `uno_station.ino`), because the dashboard shows temperatures per animal.

Optional protection: start the dashboard with `AGRISENSE_SENSOR_KEY=some-secret` and the bridge with `--key some-secret`. Readings without the key are then rejected.

## 4. Booth demo

- **Rain:** drip a little water on the rain plate. Within 2 s the rain card shows *Raining*.
- **High temperature (needs a working GY-906):** hold a warm cup or your palm close (1–2 cm) in front of the GY-906. Once the skin temperature passes the demo threshold (37.5 °C, configurable with `AGRISENSE_LIVE_HIGH_TEMP_C`), the card of H1-02 turns red. With the current sensor, the card only shows the air temperature.

## 5. From readings to the alert engine

```bash
python -m src.sensor_export
```

This writes daily summaries to `data/input/`:
- `sensor_livestock_daily.csv` has the same columns as `livestock_data.csv`, so the livestock model can run on real sensor data.
- `sensor_rain_daily.csv` has the share of wet readings per day.

## Limits of this prototype

- **The GY-906 we have is probably faulty or a counterfeit.** Cheap GY-906 boards often carry fake MLX90614 chips. The tests found:
  - It only answers right after one pass over all I2C addresses (as an I2C scanner does), and stops answering again within 0.1 s. Calling its own address repeatedly does not wake it.
  - The UNO's I2C hardware (the `Wire` library) gets no answer from it at any speed; a slower software I2C does. The same sensor also failed on a XIAO nRF52840 and an ESP32 tried earlier.
  - Its air temperature is right, but its skin temperature is mostly nonsense, such as −170 °C with a valid checksum, and only sometimes plausible. The station sends temperatures outside −20 to 80 °C as "no value".
  - Its configuration word reads `0xAFF4`; the factory value commonly reported for the usual variant is `0x9FB4`.

  A new GY-906 from a reliable shop should work with the same wiring and code.
- **Skin, not body temperature:** the GY-906 measures surface temperature without contact. It reads lower than body temperature and changes with sun, wind and hair, so a real fever threshold needs calibrating against a vet thermometer.
- **Wet or dry, not millimetres:** the rain sensor shows whether it rains, not how much. Real rainfall needs a tipping-bucket rain gauge.
- **On a cable:** the UNO sends over USB, so it has to sit next to the laptop. A station in the field needs a board or module with Wi-Fi, LoRa or a mobile network, plus a battery or solar power.
- **One fixed position:** all readings carry the position given to the bridge (`--lat`, `--lon`). Animals on the move would need a collar with GPS.

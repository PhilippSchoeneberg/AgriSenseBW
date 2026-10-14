# AgriSense BW hardware prototype

Status: both programs compile; they have not yet been tested on the boards.

A first prototype that measures live data for the dashboard's **Sensors** tab:

```
 Collar node                         Gateway / field station                Laptop
 Seeed XIAO nRF52840        BLE      ESP32-S (NodeMCU-32S)          Wi-Fi   FastAPI dashboard
 + GY-906 (skin temperature) ──────► + Flying Fish rain sensor     ───────► POST /api/readings
 + accelerometer (Sense only)                                                 → Sensors tab
```

| Folder | Board | What it does |
|---|---|---|
| `collar_node/` | Seeed XIAO nRF52840 (Sense) | Measures skin temperature, air temperature and activity; broadcasts them every 2 s over Bluetooth |
| `gateway/` | ESP32 dev board | Listens for collars, reads the rain sensor, sends everything to the dashboard every 5 s |
| `simulate_gateway.py` | none | Sends made-up readings like the gateway does: for testing, or as a backup at the booth |

## 1. Wiring

**GY-906 (MLX90614) → XIAO nRF52840**

| GY-906 | XIAO |
|---|---|
| VIN | 3V3 |
| GND | GND |
| SDA | D4 |
| SCL | D5 |

**Flying Fish rain sensor (control board) → ESP32**

| Rain sensor | ESP32 | Why |
|---|---|---|
| VCC | GPIO 25 | Powered only while measuring, so the plate corrodes less |
| GND | GND | |
| AO | GPIO 34 | Analog input on ADC1, which still works while Wi-Fi is on |
| DO | – | Not needed |

The plate connects to the control board with the two short wires.

## 2. Arduino IDE setup (once)

1. **Board packages** (*Tools → Board → Boards Manager*). First add both URLs under *Settings → Additional boards manager URLs*:
   - `https://espressif.github.io/arduino-esp32/package_esp32_index.json` → install **esp32** by Espressif
   - `https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json` → install **Seeed nRF52 mbed-enabled Boards**
2. **Libraries** (*Tools → Manage Libraries*): **ArduinoBLE**, **Adafruit MLX90614 Library**, **NimBLE-Arduino** and **Seeed Arduino LSM6DS3**.
   For the last one, pick **version 2.0.3** in the version menu: versions 2.0.4 and 2.0.5 don't compile with the Seeed mbed board package.

## 3. Collar node

1. Open `collar_node/collar_node.ino`.
2. Set `ANIMAL_ID` to a name of up to 6 letters, digits or hyphens, e.g. `"H1-01"`. Every collar needs its own.
3. Choose the board *Seeed nRF52 mbed-enabled Boards → XIAO nRF52840 Sense*, or *XIAO nRF52840* for the version without the accelerometer. Either way, the code finds out at start-up whether an accelerometer is there.
4. Upload, then open the Serial Monitor at 115200 baud. It shows `GY-906 found` and a line with the values every 2 s.

LED: blue = starting, short green flash = values sent, red = GY-906 not found.

## 4. Gateway

1. Copy `gateway/config.example.h` to `gateway/config.h` and enter:
   - your Wi-Fi name and password. The ESP32 only uses 2.4 GHz Wi-Fi; a phone hotspot works.
   - the laptop's address in `SERVER_URL`. Find it with `ipconfig getifaddr en0`.

   `config.h` is ignored by git, so the password stays on your computer.
2. Choose the board *esp32 → ESP32 Dev Module* (or *NodeMCU-32S*). Set *Tools → Partition Scheme* to **Huge APP (3MB No OTA/1MB SPIFFS)**, then upload.
   Bluetooth and Wi-Fi together need the space: with the default scheme the program fills 97% of it, with Huge APP 40%.
3. In the Serial Monitor (115200 baud) you should see `Rain sensor raw … -> level …` and `POST … -> 201` every 5 s.

**Calibrating the rain sensor:** note the `raw` value with a dry plate and with a soaking-wet plate, and put them into `RAIN_DRY_RAW` and `RAIN_WET_RAW`.

## 5. Dashboard

The gateway has to reach the laptop over the network, so start the dashboard on all network interfaces:

```bash
uvicorn src.main:app --host 0.0.0.0 --reload
```

Open the **Sensors** tab. macOS may ask whether Python may accept incoming connections: allow it.

Optional protection: start the dashboard with `AGRISENSE_SENSOR_KEY=some-secret` and put the same text into `SENSOR_KEY` in `config.h`. Readings without the key are then rejected.

### Test without hardware

```bash
python firmware/simulate_gateway.py
```

It sends three collars and a rain sensor. One collar warms up past the threshold and one goes quiet, so all three states appear on the Sensors tab.

## 6. Booth demo

- **High temperature:** hold a warm cup or your palm close (1–2 cm) in front of the GY-906. Once the skin temperature passes the demo threshold (37.5 °C, configurable with `AGRISENSE_LIVE_HIGH_TEMP_C`), the collar card turns red.
- **Rain:** drip a little water on the rain plate. Within 5 s the rain card shows *Raining*.
- **Missing animal:** switch the collar off, or walk away with it. After a minute the card shows *Not seen*, as an animal missing from the kraal would.

## 7. From readings to the alert engine

```bash
python -m src.sensor_export
```

This writes daily summaries to `data/input/`:
- `sensor_livestock_daily.csv` has the same columns as `livestock_data.csv`, so the livestock model can run on real collar data.
- `sensor_rain_daily.csv` has the share of wet readings per day.

## Limits of this prototype

- **Skin, not body temperature:** the GY-906 measures surface temperature without contact. It reads lower than body temperature and changes with sun, wind and hair, so a real fever threshold needs calibrating against a vet thermometer.
- **Wet or dry, not millimetres:** the rain sensor shows whether it rains, not how much. Real rainfall needs a tipping-bucket rain gauge.
- **No GPS yet:** collars are placed at the gateway's position. A NEO-6M or NEO-M8N module would add real positions.
- **Short range:** Bluetooth reaches tens of metres. Real herds need LoRa or a mobile network (LTE-M / NB-IoT), plus a solar-powered gateway.
- **Test company ID:** the collars broadcast with company ID 0xFFFF, which is reserved for testing.

/*
  AgriSense BW - gateway and field station (prototype)
  Board: ESP32 dev board, e.g. ESP-32S / NodeMCU-32S
         (Arduino IDE: board package "esp32" by Espressif, board "ESP32 Dev Module",
          Tools > Partition Scheme > "Huge APP": Bluetooth + Wi-Fi need the room)

  - listens for the collar broadcasts (firmware/collar_node) over Bluetooth
  - reads the MH-Sensor-Series "Flying Fish" rain sensor
  - every 5 seconds sends everything as JSON to the dashboard: POST /api/readings

  Wiring (Flying Fish control board -> ESP32):
    VCC -> GPIO 25 (the sensor is only powered while measuring, so the plate corrodes less)
    GND -> GND
    AO  -> GPIO 34 (analog input; ADC1 still works while Wi-Fi is on)
    DO  -> not needed
  Libraries: NimBLE-Arduino (2.x)

  Settings: copy config.example.h to config.h and fill in Wi-Fi and the laptop's address.
*/
#if __has_include("config.h")
#include "config.h"
#else
#include "config.example.h"
#warning "Using config.example.h - copy it to config.h and enter your Wi-Fi and server"
#endif

#include <WiFi.h>
#include <HTTPClient.h>
#include <NimBLEDevice.h>

// ----------------------------------------------------------------- settings
const unsigned long SEND_EVERY_MS = 5000;
const unsigned long COLLAR_FRESH_MS = 15000;  // only send collars heard in the last 15 s
const int RAIN_POWER_PIN = 25;
const int RAIN_ANALOG_PIN = 34;
const int RAIN_DRY_RAW = 4095;   // reading with a dry plate  (calibrate: see Serial Monitor)
const int RAIN_WET_RAW = 1200;   // reading with a soaking-wet plate
const int RAIN_WET_LEVEL = 20;   // level (0-100) from which it counts as raining
const int STATUS_LED = 2;        // on-board LED on most ESP32 dev boards

// Declared up here because the Arduino IDE adds function declarations above the code.
struct Rain { int raw; int level; bool wet; };

// ------------------------------------------------------------ collar table
const uint16_t COMPANY_ID = 0xFFFF;
const int16_t NO_VALUE = INT16_MIN;
const uint8_t NO_ACTIVITY = 255;
const int MAX_COLLARS = 10;

struct Collar {
  char id[7];
  int16_t skin;      // degrees C x100, or NO_VALUE
  int16_t ambient;
  uint8_t activity;  // 0-100, or NO_ACTIVITY
  int rssi;
  uint8_t sequence;
  unsigned long heardMs;
};

Collar collars[MAX_COLLARS];
int collarCount = 0;
portMUX_TYPE collarLock = portMUX_INITIALIZER_UNLOCKED;  // BLE runs in its own task

int16_t readInt16(const uint8_t* at) { return (int16_t)(at[0] | (at[1] << 8)); }

class CollarListener : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* device) override {
    if (!device->haveManufacturerData()) return;
    std::string raw = device->getManufacturerData();
    if (raw.size() < 17) return;
    const uint8_t* d = (const uint8_t*)raw.data();
    if (readInt16(d) != (int16_t)COMPANY_ID || d[2] != 'A' || d[3] != 'S' || d[4] != 1) return;

    char id[7] = {0};
    memcpy(id, d + 5, 6);
    portENTER_CRITICAL(&collarLock);
    int slot = -1;
    for (int i = 0; i < collarCount; i++) {
      if (strcmp(collars[i].id, id) == 0) { slot = i; break; }
    }
    if (slot < 0 && collarCount < MAX_COLLARS) {
      slot = collarCount++;
      strcpy(collars[slot].id, id);
    }
    if (slot >= 0) {
      collars[slot].skin = readInt16(d + 11);
      collars[slot].ambient = readInt16(d + 13);
      collars[slot].activity = d[15];
      collars[slot].sequence = d[16];
      collars[slot].rssi = device->getRSSI();
      collars[slot].heardMs = millis();
    }
    portEXIT_CRITICAL(&collarLock);
  }
} listener;

// ------------------------------------------------------------------- rain
Rain readRain() {
  digitalWrite(RAIN_POWER_PIN, HIGH);
  delay(50);
  long sum = 0;
  for (int i = 0; i < 8; i++) { sum += analogRead(RAIN_ANALOG_PIN); delay(2); }
  digitalWrite(RAIN_POWER_PIN, LOW);
  Rain r;
  r.raw = sum / 8;
  r.level = constrain(map(r.raw, RAIN_DRY_RAW, RAIN_WET_RAW, 0, 100), 0, 100);
  r.wet = r.level >= RAIN_WET_LEVEL;
  return r;
}

// ------------------------------------------------------------------- send
void appendTemperature(String& json, const char* key, int16_t value) {
  json += ",\"";
  json += key;
  json += "\":";
  if (value == NO_VALUE) json += "null"; else json += String(value / 100.0, 2);
}

String buildJson(const Rain& rain) {
  String json = "{\"gateway_id\":\"" GATEWAY_ID "\",\"lat\":" + String(GATEWAY_LAT, 5) +
                ",\"lon\":" + String(GATEWAY_LON, 5) + ",\"readings\":[";
  json += "{\"sensor\":\"rain\",\"rain_level\":" + String(rain.level) +
          ",\"rain_raw\":" + String(rain.raw) + ",\"rain_wet\":" + (rain.wet ? "true" : "false") + "}";

  Collar copy[MAX_COLLARS];
  portENTER_CRITICAL(&collarLock);
  int count = collarCount;
  memcpy(copy, collars, sizeof(Collar) * count);
  portEXIT_CRITICAL(&collarLock);

  unsigned long now = millis();
  for (int i = 0; i < count; i++) {
    if (now - copy[i].heardMs > COLLAR_FRESH_MS) continue;  // not heard recently
    json += ",{\"sensor\":\"collar\",\"animal_id\":\"" + String(copy[i].id) + "\"";
    appendTemperature(json, "body_temp_c", copy[i].skin);
    appendTemperature(json, "ambient_c", copy[i].ambient);
    json += ",\"activity\":";
    json += copy[i].activity == NO_ACTIVITY ? String("null") : String(copy[i].activity);
    json += ",\"rssi\":" + String(copy[i].rssi) + "}";
  }
  return json + "]}";
}

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("Connecting to Wi-Fi \"%s\"", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) { delay(250); Serial.print("."); }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println(" connected, IP " + WiFi.localIP().toString());
  } else {
    Serial.println(" failed, retrying later");
  }
}

void send(const String& json) {
  connectWifi();
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  if (strlen(SENSOR_KEY) > 0) http.addHeader("X-Sensor-Key", SENSOR_KEY);
  int code = http.POST(json);
  Serial.printf("POST %s -> %d\n", SERVER_URL, code);
  if (code < 200 || code >= 300) Serial.println(http.getString());
  http.end();
  digitalWrite(STATUS_LED, code >= 200 && code < 300 ? HIGH : LOW);
}

// ------------------------------------------------------------------ main
unsigned long lastSend = 0;

void setup() {
  Serial.begin(115200);
  pinMode(STATUS_LED, OUTPUT);
  pinMode(RAIN_POWER_PIN, OUTPUT);
  digitalWrite(RAIN_POWER_PIN, LOW);
  analogReadResolution(12);

  WiFi.mode(WIFI_STA);
  connectWifi();

  NimBLEDevice::init("");
  NimBLEScan* scan = NimBLEDevice::getScan();
  scan->setScanCallbacks(&listener, true);  // true: report every broadcast, not just the first
  scan->setActiveScan(false);               // collars only broadcast, no need to ask them
  scan->setInterval(100);
  scan->setWindow(60);                      // leave radio time for Wi-Fi
  scan->setMaxResults(0);                   // do not keep a list, we only need the callback
  scan->start(0, false);                    // 0 = scan forever
  Serial.println("Listening for collars...");
}

void loop() {
  if (millis() - lastSend >= SEND_EVERY_MS) {
    lastSend = millis();
    Rain rain = readRain();
    Serial.printf("Rain sensor raw %d -> level %d%% (%s)\n", rain.raw, rain.level, rain.wet ? "wet" : "dry");
    String json = buildJson(rain);
    Serial.println(json);
    send(json);
  }
  // Restart scanning if the BLE stack stopped it (e.g. after a Wi-Fi reconnect).
  if (!NimBLEDevice::getScan()->isScanning()) NimBLEDevice::getScan()->start(0, false);
  delay(20);
}

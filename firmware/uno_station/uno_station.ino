/*
  AgriSense BW - UNO sensor station (prototype)
  Board: Arduino UNO (Arduino IDE: board package "Arduino AVR Boards", board "Arduino Uno")

  Every 2 seconds the station reads the rain sensor and the GY-906 (MLX90614) infrared
  thermometer and prints the readings as one line of JSON over USB. On the laptop,
  firmware/serial_bridge.py forwards every line to the dashboard (POST /api/readings).
  The UNO has no Wi-Fi, but its pins are labelled, which makes the wiring easy.

  Wiring:
    GY-906:       VIN -> 3.3V,  GND -> GND,  SDA -> A4,  SCL -> A5
                  (this GY-906 pulls its data lines up to VIN: with 5V they measured 4.8 V,
                  too much for the 3.3 V sensor chip)
    Rain sensor:  VCC -> pin 7 (powered only while measuring, so the plate corrodes less),
                  GND -> GND,  AO -> A0,  DO -> not needed
  Libraries: none. The GY-906 is read with a small software I2C, see readMlx().

  Lines starting with "#" are information for people; the bridge prints them but does not
  send them.
*/
// ----------------------------------------------------------------- settings
const unsigned long SEND_EVERY_MS = 2000;
const int RAIN_POWER_PIN = 7;
const int RAIN_ANALOG_PIN = A0;
const int RAIN_DRY_RAW = 1023;           // reading with a dry plate (calibrate: see Serial Monitor)
const int RAIN_WET_RAW = 300;            // reading with a soaking-wet plate
const int RAIN_WET_LEVEL = 20;           // level (0-100) from which it counts as raining
const char TEMP_SENSOR_ID[] = "H1-01";   // the dashboard shows the GY-906 as this collar
const float PLAUSIBLE_MIN_C = -20;       // readings outside this range are sent as "no value"
const float PLAUSIBLE_MAX_C = 80;

// ---------------------------------------------------- GY-906 (MLX90614)
const uint8_t MLX_ADDRESS = 0x5A;
const uint8_t MLX_AMBIENT = 0x06;  // register: the sensor's own (air) temperature
const uint8_t MLX_OBJECT = 0x07;   // register: temperature of what it points at
const int SDA_PIN = A4;
const int SCL_PIN = A5;

uint8_t crc8(const uint8_t* data, int length) {  // SMBus checksum (PEC): CRC-8, x^8 + x^2 + x + 1
  uint8_t crc = 0;
  for (int i = 0; i < length; i++) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; bit++) crc = (crc & 0x80) ? (crc << 1) ^ 0x07 : crc << 1;
  }
  return crc;
}

// The GY-906 is read with a small software I2C ("bit banging") instead of the Wire library.
// Tests showed that the UNO's I2C hardware gets no answer from this sensor at any speed
// (the same happened on the XIAO nRF52840), while this slower software version with long
// pauses between the edges of the two lines works every time. The pins work as open drain:
// they only ever pull a line low (OUTPUT, LOW) or let go of it (INPUT); the GY-906 board's
// resistors pull it high.

void lineLow(int pin) { pinMode(pin, OUTPUT); digitalWrite(pin, LOW); }
void lineRelease(int pin) { pinMode(pin, INPUT); }
void pause() { delayMicroseconds(10); }

void clockHigh() {  // let go of SCL and wait while the sensor holds it low ("clock stretching")
  lineRelease(SCL_PIN);
  unsigned long start = micros();
  while (!digitalRead(SCL_PIN) && micros() - start < 5000) {}
  pause();
}

void clockLow() {
  lineLow(SCL_PIN);
  pause();
}

void busStart() {  // SDA falls while SCL is high; also works as a repeated start
  lineRelease(SDA_PIN);
  clockHigh();
  lineLow(SDA_PIN);
  pause();
  clockLow();
}

void busStop() {  // SDA rises while SCL is high
  lineLow(SDA_PIN);
  pause();
  clockHigh();
  lineRelease(SDA_PIN);
  pause();
}

bool busWrite(uint8_t value) {  // true if the sensor acknowledged the byte
  for (int bit = 7; bit >= 0; bit--) {
    if ((value >> bit) & 1) lineRelease(SDA_PIN); else lineLow(SDA_PIN);
    pause();
    clockHigh();
    clockLow();
  }
  lineRelease(SDA_PIN);
  pause();
  clockHigh();
  bool ack = !digitalRead(SDA_PIN);
  clockLow();
  return ack;
}

uint8_t busRead(bool more) {  // more: acknowledge, so the sensor sends the next byte
  uint8_t value = 0;
  lineRelease(SDA_PIN);
  for (int bit = 0; bit < 8; bit++) {
    pause();
    clockHigh();
    value = (value << 1) | digitalRead(SDA_PIN);
    clockLow();
  }
  if (more) lineLow(SDA_PIN); else lineRelease(SDA_PIN);
  pause();
  clockHigh();
  clockLow();
  lineRelease(SDA_PIN);
  pause();
  return value;
}

// After a pause the sensor does not answer. Tests showed that calling its own address again
// and again (even for 0.25 s) does not help, but one pass over all I2C addresses, as an I2C
// scanner does, wakes it up every time; why is unclear. So every reading starts with such a
// pass (about 80 ms). True if the sensor answered.
bool mlxWake() {
  bool found = false;
  for (uint8_t address = 1; address < 127; address++) {
    busStart();
    bool ack = busWrite(address << 1);
    busStop();
    if (ack && address == MLX_ADDRESS) found = true;
  }
  return found;
}

// One temperature register in degrees C, or NAN if no valid answer came back.
float readMlx(uint8_t reg) {
  const uint8_t writeAddress = MLX_ADDRESS << 1, readAddress = (MLX_ADDRESS << 1) | 1;
  for (int attempt = 0; attempt < 3; attempt++) {
    if (!mlxWake()) continue;
    busStart();
    bool ok = busWrite(writeAddress) && busWrite(reg);
    busStart();                                   // repeated start, as SMBus requires
    ok = ok && busWrite(readAddress);
    uint8_t low = 0, high = 0, pec = 0;
    if (ok) {
      low = busRead(true);
      high = busRead(true);
      pec = busRead(false);
    }
    busStop();
    uint8_t frame[] = {writeAddress, reg, readAddress, low, high};
    uint16_t raw = low | (high << 8);
    // No answer, wrong checksum (garbled), bit 15 (the sensor reports an error),
    // or 0 (the sensor has just started and has no measurement yet).
    if (!ok || crc8(frame, 5) != pec || (raw & 0x8000) || raw == 0) continue;
    return raw * 0.02 - 273.15;  // the sensor counts in steps of 0.02 K
  }
  return NAN;
}

// The GY-906 tested here sometimes reported skin temperatures such as -170 C with a valid
// checksum (its air temperature was always right). Such values are dropped.
float plausible(float celsius) {
  return celsius >= PLAUSIBLE_MIN_C && celsius <= PLAUSIBLE_MAX_C ? celsius : NAN;
}

// ------------------------------------------------------------------- rain
int readRainRaw() {
  digitalWrite(RAIN_POWER_PIN, HIGH);
  delay(50);
  long sum = 0;
  for (int i = 0; i < 8; i++) { sum += analogRead(RAIN_ANALOG_PIN); delay(2); }
  digitalWrite(RAIN_POWER_PIN, LOW);
  return sum / 8;
}

// ------------------------------------------------------------------- send
void printCelsius(const char* key, float celsius) {
  Serial.print(",\"");
  Serial.print(key);
  Serial.print("\":");
  if (isnan(celsius)) Serial.print("null"); else Serial.print(celsius, 2);
}

unsigned long lastSend = 0;

void setup() {
  Serial.begin(115200);
  pinMode(RAIN_POWER_PIN, OUTPUT);
  digitalWrite(RAIN_POWER_PIN, LOW);
  lineRelease(SDA_PIN);
  lineRelease(SCL_PIN);
  delay(300);             // the MLX90614 needs about 0.25 s after power-up
  Serial.println(isnan(readMlx(MLX_AMBIENT)) ? "# GY-906 NOT found - check wiring (VIN 3.3V, SDA A4, SCL A5)"
                                             : "# GY-906 found");
}

void loop() {
  if (millis() - lastSend < SEND_EVERY_MS) return;
  lastSend = millis();

  int raw = readRainRaw();
  int level = constrain(map(raw, RAIN_DRY_RAW, RAIN_WET_RAW, 0, 100), 0, 100);
  float skin = plausible(readMlx(MLX_OBJECT)), ambient = plausible(readMlx(MLX_AMBIENT));

  Serial.print("{\"readings\":[{\"sensor\":\"rain\",\"rain_level\":");
  Serial.print(level);
  Serial.print(",\"rain_raw\":");
  Serial.print(raw);
  Serial.print(",\"rain_wet\":");
  Serial.print(level >= RAIN_WET_LEVEL ? "true" : "false");
  Serial.print("}");
  if (!isnan(skin) || !isnan(ambient)) {  // the GY-906, shown on the dashboard as a collar
    Serial.print(",{\"sensor\":\"collar\",\"animal_id\":\"");
    Serial.print(TEMP_SENSOR_ID);
    Serial.print("\"");
    printCelsius("body_temp_c", skin);
    printCelsius("ambient_c", ambient);
    Serial.print(",\"activity\":null,\"rssi\":null}");
  }
  Serial.println("]}");
}

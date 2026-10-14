/*
  AgriSense BW - collar node (prototype)
  Board: Seeed XIAO nRF52840 or XIAO nRF52840 Sense
         (Arduino IDE: board package "Seeed nRF52 mbed-enabled Boards",
          board "XIAO nRF52840 Sense" or "XIAO nRF52840")

  Every 2 seconds the node measures and broadcasts, as a Bluetooth (BLE) advertisement:
    - skin temperature and ambient temperature from the GY-906 (MLX90614) infrared sensor
    - an activity index 0-100 from the built-in accelerometer (Sense version only)
  The ESP32 gateway (firmware/gateway) picks these broadcasts up; no pairing is needed.
  If the gateway stops hearing a collar, the dashboard shows the animal as "not seen".

  Wiring (GY-906 -> XIAO):  VIN -> 3V3,  GND -> GND,  SDA -> D4,  SCL -> D5
  Libraries: ArduinoBLE, Adafruit MLX90614 Library, Seeed Arduino LSM6DS3 (version 2.0.3;
             2.0.4 and 2.0.5 do not compile with the mbed board package)

  Broadcast format (manufacturer data, company ID 0xFFFF = "no company", for testing):
    'A' 'S' | version (1) | animal ID, 6 chars, zero padded | skin temp x100 (int16, LE)
    | ambient temp x100 (int16, LE) | activity 0-100, 255 = no accelerometer | sequence number
*/
#include <ArduinoBLE.h>
#include <Wire.h>
#include <Adafruit_MLX90614.h>
#include <LSM6DS3.h>

// ----------------------------------------------------------------- settings
const char ANIMAL_ID[] = "H1-01";          // up to 6 characters, one per collar
const unsigned long SEND_EVERY_MS = 2000;  // how often new values are broadcast
const unsigned long IMU_EVERY_MS = 50;     // accelerometer sampling (20 per second)

// --------------------------------------------------------------- broadcast
const uint16_t COMPANY_ID = 0xFFFF;
const uint8_t FORMAT_VERSION = 1;
const int16_t NO_VALUE = INT16_MIN;        // sent when the temperature sensor fails
const uint8_t NO_ACTIVITY = 255;

Adafruit_MLX90614 mlx;
LSM6DS3 imu(I2C_MODE, 0x6A);
bool haveMlx = false;
bool haveImu = false;

float motionSum = 0;      // sum of |acceleration - 1 g| since the last broadcast
unsigned motionCount = 0;
uint8_t sequence = 0;
unsigned long lastSend = 0;
unsigned long lastImu = 0;

void led(bool red, bool green, bool blue) {  // XIAO RGB LED is active LOW
  digitalWrite(LED_RED, red ? LOW : HIGH);
  digitalWrite(LED_GREEN, green ? LOW : HIGH);
  digitalWrite(LED_BLUE, blue ? LOW : HIGH);
}

void putInt16(uint8_t* at, int16_t value) {
  at[0] = value & 0xFF;
  at[1] = (value >> 8) & 0xFF;
}

int16_t hundredths(double celsius) {
  if (isnan(celsius) || celsius < -300 || celsius > 300) return NO_VALUE;
  return (int16_t)lround(celsius * 100.0);
}

void sampleMotion() {
  float x = imu.readFloatAccelX(), y = imu.readFloatAccelY(), z = imu.readFloatAccelZ();
  motionSum += fabs(sqrt(x * x + y * y + z * z) - 1.0f);  // 0 when lying still
  motionCount++;
}

uint8_t activityIndex() {
  if (!haveImu || motionCount == 0) return NO_ACTIVITY;
  float meanDeviation = motionSum / motionCount;     // in g
  motionSum = 0;
  motionCount = 0;
  return (uint8_t)constrain(lround(meanDeviation * 200.0f), 0, 100);  // 0.5 g -> 100
}

void broadcast() {
  double skin = haveMlx ? mlx.readObjectTempC() : NAN;
  double ambient = haveMlx ? mlx.readAmbientTempC() : NAN;
  uint8_t activity = activityIndex();

  uint8_t data[2 + 15];
  putInt16(data, COMPANY_ID);   // company ID first, little endian
  uint8_t* p = data + 2;
  p[0] = 'A';
  p[1] = 'S';
  p[2] = FORMAT_VERSION;
  memset(p + 3, 0, 6);
  memcpy(p + 3, ANIMAL_ID, min(strlen(ANIMAL_ID), (size_t)6));
  putInt16(p + 9, hundredths(skin));
  putInt16(p + 11, hundredths(ambient));
  p[13] = activity;
  p[14] = sequence++;

  BLE.stopAdvertise();
  BLE.setManufacturerData(data, sizeof(data));
  BLE.advertise();

  Serial.print(ANIMAL_ID);
  Serial.print("  skin ");
  Serial.print(skin, 2);
  Serial.print(" C  ambient ");
  Serial.print(ambient, 2);
  Serial.print(" C  activity ");
  if (activity == NO_ACTIVITY) Serial.println("-"); else Serial.println(activity);

  led(!haveMlx, haveMlx, false);   // green flash = sent, red = temperature sensor missing
  delay(30);
  led(false, false, false);
}

void setup() {
  pinMode(LED_RED, OUTPUT);
  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_BLUE, OUTPUT);
  led(false, false, true);
  Serial.begin(115200);
  delay(1500);  // time to open the Serial Monitor

  haveMlx = mlx.begin();
  haveImu = imu.begin() == 0;   // only the XIAO nRF52840 Sense has the accelerometer
  Serial.println(haveMlx ? "GY-906 found" : "GY-906 NOT found - check wiring (SDA D4, SCL D5)");
  Serial.println(haveImu ? "Accelerometer found" : "No accelerometer - activity is not sent");

  if (!BLE.begin()) {
    Serial.println("Bluetooth failed to start");
    while (true) { led(true, false, false); delay(200); led(false, false, false); delay(200); }
  }
  BLE.setDeviceName("AgriSense collar");
  BLE.setAdvertisingInterval(320);  // 320 x 0.625 ms = every 200 ms
  broadcast();
  led(false, false, false);
}

void loop() {
  BLE.poll();
  unsigned long now = millis();
  if (haveImu && now - lastImu >= IMU_EVERY_MS) {
    lastImu = now;
    sampleMotion();
  }
  if (now - lastSend >= SEND_EVERY_MS) {
    lastSend = now;
    broadcast();
  }
}

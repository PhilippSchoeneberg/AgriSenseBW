// Copy this file to config.h (same folder) and fill in your values.
// config.h is ignored by git, so your Wi-Fi password stays on your computer.
#pragma once

#define WIFI_SSID     "your-wifi-name"
#define WIFI_PASSWORD "your-wifi-password"

// The laptop running the dashboard, started with:  uvicorn src.main:app --host 0.0.0.0
// Find its address with: ipconfig getifaddr en0   (macOS)
#define SERVER_URL    "http://192.168.1.50:8000/api/readings"

// Optional: the same text as AGRISENSE_SENSOR_KEY on the laptop. Leave "" if not used.
#define SENSOR_KEY    ""

#define GATEWAY_ID    "booth-1"

// Where the gateway (kraal / water point) is. The collars have no GPS yet, so the
// dashboard places them here. Default: near Kanye, Southern district.
#define GATEWAY_LAT   -24.98
#define GATEWAY_LON   25.34

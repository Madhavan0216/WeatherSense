# ESP32 Firmware Setup

This folder uses the firmware in `weather_sensor.ino`.

## Board / Core
- Board: ESP32 Dev Module (or compatible)
- Arduino core: ESP32 by Espressif

## Required Libraries
- DHT sensor library by Adafruit
- Adafruit Unified Sensor (dependency for DHT library)

## Endpoints Expected by Firmware
- `GET /bm`
- `POST /api/telemetry` with form fields:
  - `rain`, `soil`, `temp`, `relay1`, `relay2`, `relay3`
- `GET /api/device/commands` returning:
  - `r1=0,r2=1,r3=0`

## Wiring (from firmware)
- DHT22: GPIO 4
- Rain digital output: GPIO 26
- Soil analog input: GPIO 33
- Relay 1 (Rain): GPIO 14 (active LOW)
- Relay 2 (Soil): GPIO 27 (active LOW)
- Relay 3 (Temp): GPIO 25 (active LOW)
- Override button: GPIO 32 (INPUT_PULLUP)

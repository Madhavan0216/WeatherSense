#include <WiFi.h>
#include <HTTPClient.h>
#include "DHT.h"

// ===== WIFI =====
const char* ssid = "bm";
const char* password = "12345678";

// ===== SERVER =====
// Use ONE unified backend host for ping + telemetry + commands.
// Replace with your backend machine IP or DNS.
const char* serverBaseUrl = "http://10.163.230.174:8080";
// NOTE: URLs below are kept explicit for reliability on embedded targets.
const char* pingUrl = "http://10.163.230.174:8080/bm";
const char* telemetryUrl = "http://10.163.230.174:8080/api/telemetry";
const char* commandUrl = "http://10.163.230.174:8080/api/device/commands";
const char* DEVICE_API_KEY = "change-me-device-key";

// ===== PINS =====
#define DHTPIN 4
#define DHTTYPE DHT22

#define RAIN_DO 26
#define SOIL_PIN 33

#define RELAY_RAIN 14
#define RELAY_SOIL 27
#define RELAY_TEMP 25

#define BUTTON_PIN 32

DHT dht(DHTPIN, DHTTYPE);

// ===== SETTINGS =====
int soilThreshold = 2000;
float tempThreshold = 20.0;

// ===== TIMERS =====
unsigned long lastSampleMs = 0;
unsigned long lastPingMs = 0;
unsigned long lastTelemetryMs = 0;
unsigned long lastCommandMs = 0;

const unsigned long sampleIntervalMs = 2000;
const unsigned long pingIntervalMs = 10000;
const unsigned long telemetryIntervalMs = 3000;
const unsigned long commandIntervalMs = 3000;

// Desired states from local logic / dashboard.
int relayRainState = HIGH;
int relaySoilState = HIGH;
int relayTempState = HIGH;

void connectWiFi() {
  int attempts = 0;
  WiFi.begin(ssid, password);
  Serial.print("Connecting WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    attempts++;
    if (attempts >= 20) {
      Serial.println("\nWiFi connect failed after 20 attempts; backing off 30s");
      delay(30000);
      attempts = 0;
      WiFi.disconnect(true);
      WiFi.begin(ssid, password);
      Serial.print("Reconnecting WiFi");
    }
  }
  Serial.println("\nConnected!");
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());
}

void callPingEndpoint() {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  HTTPClient http;
  Serial.print("Calling: ");
  Serial.println(pingUrl);

  http.setTimeout(5000);
  http.begin(pingUrl);
  http.addHeader("X-Device-Key", DEVICE_API_KEY);
  int code = http.GET();
  Serial.print("Ping response code: ");
  Serial.println(code);
  http.end();
}

void sendTelemetry(int rain, int soil, float temp) {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  HTTPClient http;
  String payload = "rain=" + String(rain) +
                   "&soil=" + String(soil) +
                   "&temp=" + String(temp, 2) +
                   "&relay1=" + String(relayRainState == LOW ? 1 : 0) +
                   "&relay2=" + String(relaySoilState == LOW ? 1 : 0) +
                   "&relay3=" + String(relayTempState == LOW ? 1 : 0);

  http.setTimeout(5000);
  http.begin(telemetryUrl);
  http.addHeader("Content-Type", "application/x-www-form-urlencoded");
  http.addHeader("X-Device-Key", DEVICE_API_KEY);
  int code = http.POST(payload);

  Serial.print("Telemetry code: ");
  Serial.println(code);
  http.end();
}

void applyRelayOutput() {
  // ACTIVE LOW relays: LOW means ON.
  digitalWrite(RELAY_RAIN, relayRainState);
  digitalWrite(RELAY_SOIL, relaySoilState);
  digitalWrite(RELAY_TEMP, relayTempState);
}

void parseAndApplyCommand(String line) {
  // Expected format: r1=0,r2=1,r3=0
  line.replace(" ", "");
  int idxR1 = line.indexOf("r1=");
  int idxR2 = line.indexOf("r2=");
  int idxR3 = line.indexOf("r3=");

  if (idxR1 >= 0 && idxR2 >= 0 && idxR3 >= 0) {
    int v1 = line.substring(idxR1 + 3, idxR1 + 4).toInt();
    int v2 = line.substring(idxR2 + 3, idxR2 + 4).toInt();
    int v3 = line.substring(idxR3 + 3, idxR3 + 4).toInt();

    relayRainState = (v1 == 1) ? LOW : HIGH;
    relaySoilState = (v2 == 1) ? LOW : HIGH;
    relayTempState = (v3 == 1) ? LOW : HIGH;
  }
}

void fetchCommandsFromDashboard() {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  HTTPClient http;
  http.setTimeout(5000);
  http.begin(commandUrl);
  http.addHeader("X-Device-Key", DEVICE_API_KEY);
  int code = http.GET();

  if (code > 0) {
    String body = http.getString();
    parseAndApplyCommand(body);
  }

  http.end();
}

void setup() {
  Serial.begin(115200);

  dht.begin();

  // INPUTS
  pinMode(RAIN_DO, INPUT_PULLUP);
  pinMode(SOIL_PIN, INPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);

  // OUTPUTS
  pinMode(RELAY_RAIN, OUTPUT);
  pinMode(RELAY_SOIL, OUTPUT);
  pinMode(RELAY_TEMP, OUTPUT);

  // RELAY OFF (ACTIVE LOW)
  digitalWrite(RELAY_RAIN, HIGH);
  digitalWrite(RELAY_SOIL, HIGH);
  digitalWrite(RELAY_TEMP, HIGH);

  connectWiFi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  unsigned long now = millis();

  if (now - lastCommandMs >= commandIntervalMs) {
    fetchCommandsFromDashboard();
    lastCommandMs = now;
  }

  if (now - lastSampleMs >= sampleIntervalMs) {
    // ===== READ =====
    int rain = digitalRead(RAIN_DO);
    int soil = analogRead(SOIL_PIN);
    float temp = dht.readTemperature();

    // ===== RAIN RELAY =====
    relayRainState = rain; // LOW -> ON

    // ===== SOIL RELAY =====
    relaySoilState = (soil < soilThreshold) ? LOW : HIGH;

    // ===== TEMP RELAY =====
    // Keep previous state if DHT read fails, instead of forcing OFF path.
    if (!isnan(temp)) {
      relayTempState = (temp < tempThreshold) ? LOW : HIGH;
    } else {
      Serial.println("Warning: DHT temp read failed (NaN), keeping previous relay3 state");
    }

    // ===== BUTTON =====
    // Pressing button forces ONLY relay 3 ON and keeps relay 2 OFF.
    if (digitalRead(BUTTON_PIN) == LOW) {
      relayTempState = LOW;
      relaySoilState = HIGH;
    }

    applyRelayOutput();

    // ===== DEBUG =====
    Serial.println("---- DATA ----");
    Serial.print("Rain: "); Serial.println(rain);
    Serial.print("Soil: "); Serial.println(soil);
    Serial.print("Temp: "); Serial.println(temp);
    Serial.print("Relay1 (Rain): "); Serial.println(relayRainState == LOW ? "ON" : "OFF");
    Serial.print("Relay2 (Soil): "); Serial.println(relaySoilState == LOW ? "ON" : "OFF");
    Serial.print("Relay3 (Temp): "); Serial.println(relayTempState == LOW ? "ON" : "OFF");
    Serial.print("Ping condition (R2 or R3 ON): ");
    Serial.println((relaySoilState == LOW || relayTempState == LOW) ? "TRUE" : "FALSE");

    if (now - lastTelemetryMs >= telemetryIntervalMs) {
      sendTelemetry(rain, soil, temp);
      lastTelemetryMs = now;
    }

    if (now - lastPingMs >= pingIntervalMs) {
      // Call ping when relay2 OR relay3 is ON (ACTIVE LOW).
      if (relaySoilState == LOW || relayTempState == LOW) {
        callPingEndpoint();
        lastPingMs = now;
      } else {
        Serial.println("Ping skipped: relay2 and relay3 are both OFF");
      }
    }

    lastSampleMs = now;
  }
}

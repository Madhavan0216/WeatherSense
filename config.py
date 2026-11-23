"""
config.py — Central configuration for Weather Monitoring System
All thresholds, ports, and settings are defined here for easy customization.
"""

import os


def _parse_csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]

# ─── Network ───────────────────────────────────────────────────────────────────
SOCKET_HOST = os.getenv("WMS_HOST", "0.0.0.0")
SOCKET_PORT = int(os.getenv("WMS_PORT", 9000))
HTTP_PORT   = int(os.getenv("WMS_HTTP_PORT", 8080))
APP_VERSION = os.getenv("WMS_APP_VERSION", "0.1.0")

# ─── Sensor Thresholds ─────────────────────────────────────────────────────────
TEMP_STAGE1_ON  = 35.0   # °C — LED1 turns ON above this
TEMP_STAGE1_OFF = 33.0   # °C — LED1 turns OFF below this (hysteresis)

RAIN_STAGE2_ON  = 300    # analog units (0–1023) — light rain threshold
RAIN_STAGE3_ON  = 700    # analog units — heavy rain / flooding threshold

WATER_LEVEL_CRITICAL = True  # binary sensor: True = water detected

# ─── Alert Timing ──────────────────────────────────────────────────────────────
BUZZER_BEEP_INTERVAL = 0.5   # seconds between buzzer beeps in CRITICAL mode
LED_BLINK_INTERVAL   = 0.3   # seconds for Stage 3 LED blink

# ─── Simulation ────────────────────────────────────────────────────────────────
SIMULATION_MODE      = True   # Set False when real hardware is connected
SIMULATION_INTERVAL  = 1.5    # seconds between simulated sensor reads

# ─── ML Model ──────────────────────────────────────────────────────────────────
MODEL_PATH           = os.path.join(os.path.dirname(__file__), "model.pkl")
MODEL_RETRAIN_HOURS  = 24     # retrain model every N hours

# ─── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL     = "INFO"
LOG_FILE      = "wms.log"
DB_FILE       = os.getenv("WMS_DB_FILE", "weather_data.db")
CSV_LOG_FILE  = os.getenv("WMS_CSV_LOG_FILE", "sensor_log.csv")

# ─── Security / API ───────────────────────────────────────────────────────────
CORS_ALLOWED_ORIGINS = _parse_csv_env("WMS_CORS_ALLOWED_ORIGINS", "http://localhost:8080")
MAX_INGEST_BODY_BYTES = int(os.getenv("WMS_MAX_INGEST_BODY_BYTES", 32768))

# ─── Authentication / Security ────────────────────────────────────────────────
JWT_SECRET = os.getenv("WMS_JWT_SECRET", "change-me-jwt-secret")
JWT_ALGORITHM = os.getenv("WMS_JWT_ALGORITHM", "HS256")
TCP_HMAC_SECRET = os.getenv("WMS_TCP_HMAC_SECRET", "change-me-tcp-hmac")

RATE_LIMIT_REQUESTS_PER_MINUTE = int(os.getenv("WMS_RATE_LIMIT_RPM", 1000))
DEVICE_API_KEY = os.getenv("WMS_DEVICE_API_KEY", "change-me-device-key")
DEVICE_RATE_LIMIT_RPM = int(os.getenv("WMS_DEVICE_RATE_LIMIT_RPM", 120))

CSP_POLICY = os.getenv(
    "WMS_CSP_POLICY",
    "default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self';",
)

# ─── Feature Names (must match ML training) ────────────────────────────────────
FEATURE_NAMES = ["temperature", "rain_level", "water_level_numeric"]

# ─── Stage Labels ──────────────────────────────────────────────────────────────
STAGE_LABELS = {
    0: "NORMAL",
    1: "WARNING",
    2: "CRITICAL",
}

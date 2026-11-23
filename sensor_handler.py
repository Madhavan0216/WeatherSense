"""
sensor_handler.py — Hardware abstraction layer for all sensors.
Supports real GPIO/ADC hardware and a built-in simulation mode.
"""

import time
import math
import random
import logging
import importlib
from dataclasses import dataclass, asdict
from typing import Any

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import config
from interfaces import SensorPort

logger = logging.getLogger(__name__)


@dataclass
class SensorReading:
    """Immutable snapshot of all sensor values at a point in time."""
    temperature: float          # °C
    rain_level: int             # analog 0–1023 (higher = more rain)
    water_detected: bool        # binary water level sensor
    timestamp: float            # Unix epoch
    source: str = "simulation"  # "hardware" | "simulation"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["water_level_numeric"] = int(self.water_detected)
        return d


class SensorHandler(SensorPort):
    """
    Reads temperature, rain intensity, and water level.
    Falls back to simulation when hardware is unavailable.
    """

    def __init__(self, simulation: bool = config.SIMULATION_MODE):
        self.simulation = simulation
        self._sim_time   = 0.0   # internal clock for realistic patterns
        self._last_temp  = 28.0  # track continuity
        self._last_rain  = 50
        self._gpio_ok    = False
        self._dht_sensor: Any | None = None
        self._rain_adc: Any | None = None
        self._rain_digital_pin: int = 17
        self._water_pin: int = 27

        if not simulation:
            self._init_hardware()

        logger.info("SensorHandler initialised (mode=%s)",
                    "simulation" if simulation else "hardware")

    # ── Hardware Initialisation ──────────────────────────────────────────────
    def _init_hardware(self) -> None:
        """Attempt to bind GPIO / DHT sensor. Falls back to simulation on failure."""
        try:
            GPIO = importlib.import_module("RPi.GPIO")
            adafruit_dht = importlib.import_module("adafruit_dht")
            board = importlib.import_module("board")

            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)

            # DHT22 on GPIO4
            self._dht_sensor = adafruit_dht.DHT22(board.D4)

            # Rain sensor digital out on GPIO17 (LOW = rain detected)
            self._rain_digital_pin = 17
            GPIO.setup(self._rain_digital_pin, GPIO.IN)

            # Water level sensor digital out on GPIO27
            self._water_pin = 27
            GPIO.setup(self._water_pin, GPIO.IN)

            # ADC (MCP3008) for rain analog — use SPI
            try:
                busio = importlib.import_module("busio")
                digitalio = importlib.import_module("digitalio")
                MCP = importlib.import_module("adafruit_mcp3xxx.mcp3008")
                AnalogIn = getattr(importlib.import_module("adafruit_mcp3xxx.analog_in"), "AnalogIn")

                spi  = busio.SPI(clock=board.SCK, MISO=board.MISO, MOSI=board.MOSI)
                cs   = digitalio.DigitalInOut(board.D8)
                mcp  = MCP.MCP3008(spi, cs)
                self._rain_adc = AnalogIn(mcp, MCP.P0)
            except Exception as adc_err:
                logger.warning("ADC init failed (%s) — using digital rain pin only", adc_err)
                self._rain_adc = None

            self._gpio_ok = True
            logger.info("GPIO hardware initialised successfully.")

        except Exception as exc:
            logger.warning("Hardware init failed (%s). Falling back to simulation.", exc)
            self.simulation = True

    # ── Public API ───────────────────────────────────────────────────────────
    def read(self) -> SensorReading:
        """Return the latest sensor reading."""
        if self.simulation:
            return self._simulate()
        return self._read_hardware()

    def close(self) -> None:
        """Release GPIO resources."""
        if self._gpio_ok:
            try:
                GPIO = importlib.import_module("RPi.GPIO")
                GPIO.cleanup()
            except Exception:
                pass

    # ── Hardware Read ────────────────────────────────────────────────────────
    def _read_hardware(self) -> SensorReading:
        temp = self._read_dht_temperature()
        rain = self._read_rain_analog()
        water = self._read_water_level()
        return SensorReading(
            temperature=temp,
            rain_level=rain,
            water_detected=water,
            timestamp=time.time(),
            source="hardware",
        )

    def _read_dht_temperature(self) -> float:
        for attempt in range(3):
            try:
                sensor = self._dht_sensor
                if sensor is None:
                    return self._last_temp
                t = sensor.temperature
                if t is not None:
                    return round(float(t), 1)
            except Exception as exc:
                logger.debug("DHT read attempt %d failed: %s", attempt + 1, exc)
                time.sleep(0.2)
        logger.warning("DHT read failed 3 times — returning last known value.")
        return self._last_temp

    def _read_rain_analog(self) -> int:
        if self._rain_adc:
            # MCP3008 returns 0–65535; normalize to 0–1023
            raw = int(self._rain_adc.value / 64)
            return min(1023, max(0, raw))
        # Fallback: digital pin (LOW = rain, HIGH = dry)
        GPIO = importlib.import_module("RPi.GPIO")
        return 800 if GPIO.input(self._rain_digital_pin) == GPIO.LOW else 0

    def _read_water_level(self) -> bool:
        GPIO = importlib.import_module("RPi.GPIO")
        return bool(GPIO.input(self._water_pin) == GPIO.HIGH)

    # ── Simulation Engine ────────────────────────────────────────────────────
    def _simulate(self) -> SensorReading:
        """
        Realistic multi-phase weather simulation using sinusoidal patterns
        and occasional weather events.
        """
        self._sim_time += config.SIMULATION_INTERVAL

        # Temperature: slow sinusoidal drift 22–42 °C + small noise
        base_temp = 32 + 10 * math.sin(self._sim_time / 60)
        noise_temp = random.gauss(0, 0.4)
        temp = round(max(15.0, min(55.0, base_temp + noise_temp)), 1)

        # Rain: periodic rain events
        rain_cycle = (math.sin(self._sim_time / 45) + 1) / 2  # 0–1
        rain_event  = random.random() < 0.08                   # 8% random spike
        rain_base   = int(rain_cycle * 800)
        rain_spike  = random.randint(700, 1023) if rain_event else 0
        rain = min(1023, max(0, rain_base + rain_spike + random.randint(-20, 20)))

        # Water level: true when rain is heavy or random flood event
        water = rain >= config.RAIN_STAGE3_ON or random.random() < 0.04

        self._last_temp = temp
        self._last_rain = rain

        return SensorReading(
            temperature=temp,
            rain_level=rain,
            water_detected=water,
            timestamp=time.time(),
            source="simulation",
        )

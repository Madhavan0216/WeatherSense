"""Business service layer for telemetry processing and sensor pipeline orchestration."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, cast

import config
from contracts import DeviceTelemetryPayload, RelayCommandPayload, SensorPayload
from interfaces import AlertPort, DecisionPort, SensorPort
from observability import Metrics, get_correlation_id, new_correlation_id
from repository import PersistenceUnavailableError, TelemetryRepository

logger = logging.getLogger(__name__)


class ServiceValidationError(ValueError):
    """Raised when incoming payloads do not satisfy contract requirements."""


class WeatherService:
    """Core domain service managing telemetry state and persistence."""

    def __init__(self, repository: TelemetryRepository, metrics: Metrics, history_size: int = 120) -> None:
        self._repository = repository
        self._metrics = metrics

        self._latest_payload: dict[str, Any] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._alert_log: list[dict[str, Any]] = []
        self._command_state: dict[str, int] = {"r1": 0, "r2": 0, "r3": 0}
        self._last_hardware_relay_state: dict[str, int] = {"r1": 0, "r2": 0, "r3": 0}
        self._last_hardware_ping_ts: float | None = None
        self._last_hardware_ping_ip: str | None = None
        self._last_hardware_telemetry_ts: float | None = None
        self._state_lock = threading.Lock()

        existing = self._repository.latest_command()
        if existing is not None:
            self._command_state = {
                "r1": 1 if int(existing.get("r1", 0)) else 0,
                "r2": 1 if int(existing.get("r2", 0)) else 0,
                "r3": 1 if int(existing.get("r3", 0)) else 0,
            }

    def ingest_payload_dict(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and ingest a payload dictionary."""
        try:
            validated = SensorPayload.model_validate(payload)
        except Exception as exc:
            self._metrics.inc("wms_ingest_failed_total")
            raise ServiceValidationError(str(exc)) from exc

        normalized = validated.model_dump()
        for key, value in payload.items():
            if key not in normalized:
                normalized[key] = value
        self._update_state(normalized)
        self._metrics.inc("wms_ingest_total")
        return normalized

    def _update_state(self, payload: dict[str, Any]) -> None:
        with self._state_lock:
            self._latest_payload = payload
            self._history.append(payload)

            relay_states_obj = payload.get("relay_states")
            if isinstance(relay_states_obj, dict):
                relay_states = cast(dict[str, Any], relay_states_obj)
                for key in ("r1", "r2", "r3"):
                    value = relay_states.get(key)
                    if isinstance(value, bool):
                        self._last_hardware_relay_state[key] = 1 if value else 0
                    elif isinstance(value, int):
                        self._last_hardware_relay_state[key] = 1 if value else 0

            if payload.get("status") in ("WARNING", "CRITICAL"):
                self._alert_log.append(
                    {
                        "time": payload.get("timestamp"),
                        "status": payload.get("status"),
                        "temperature": payload.get("temperature"),
                        "rain_level": payload.get("rain_level"),
                        "water_detected": payload.get("water_detected"),
                    }
                )
                if len(self._alert_log) > 500:
                    self._alert_log.pop(0)

        try:
            self._repository.persist_payload(payload)
        except PersistenceUnavailableError as exc:
            logger.error("persistence unavailable: %s", exc)
        except Exception as exc:
            logger.error("persistence failure: %s", exc)

        self._metrics.inc("wms_payload_emitted_total")

    def ingest_esp_telemetry(
        self,
        rain: int,
        soil: int,
        temp: float,
        relay1: int,
        relay2: int,
        relay3: int,
    ) -> dict[str, Any]:
        """Normalize ESP32 form telemetry into the canonical payload contract."""
        telemetry = DeviceTelemetryPayload(
            rain=rain,
            soil=soil,
            temp=temp,
            relay1=relay1,
            relay2=relay2,
            relay3=relay3,
        )
        sensor_payload = telemetry.to_sensor_payload()
        normalized = self.ingest_payload_dict(sensor_payload.model_dump())

        with self._state_lock:
            self._last_hardware_telemetry_ts = float(normalized.get("timestamp", time.time()))
            self._last_hardware_relay_state = {
                "r1": telemetry.relay1,
                "r2": telemetry.relay2,
                "r3": telemetry.relay3,
            }

        return normalized

    def latest_command_state(self) -> dict[str, int]:
        with self._state_lock:
            return dict(self._command_state)

    def set_command_state(self, r1: int, r2: int, r3: int, source: str = "dashboard") -> dict[str, int]:
        payload = RelayCommandPayload(r1=r1, r2=r2, r3=r3, source=source)

        with self._state_lock:
            self._command_state = {
                "r1": payload.r1,
                "r2": payload.r2,
                "r3": payload.r3,
            }

        try:
            self._repository.persist_command(payload.r1, payload.r2, payload.r3, payload.source)
        except Exception as exc:
            logger.error("command persistence failure: %s", exc)

        logger.info(
            "relay_command_state_updated",
            extra={
                "correlation_id": get_correlation_id(),
                "r1": payload.r1,
                "r2": payload.r2,
                "r3": payload.r3,
                "source": payload.source,
            },
        )

        return self.latest_command_state()

    def command_and_hardware_state(self) -> dict[str, dict[str, int]]:
        with self._state_lock:
            return {
                "commanded": dict(self._command_state),
                "reported": dict(self._last_hardware_relay_state),
            }

    def get_device_commands_line(self) -> str:
        cmd = self.latest_command_state()
        return f"r1={cmd['r1']},r2={cmd['r2']},r3={cmd['r3']}"

    def record_hardware_ping(self, ip: str) -> None:
        with self._state_lock:
            self._last_hardware_ping_ts = time.time()
            self._last_hardware_ping_ip = ip

    def hardware_status(self) -> dict[str, Any]:
        with self._state_lock:
            now = time.time()
            last_telemetry = self._last_hardware_telemetry_ts
            online = bool(last_telemetry and (now - last_telemetry <= 10.0))
            return {
                "last_hardware_ping_ts": self._last_hardware_ping_ts,
                "last_hardware_ping_ip": self._last_hardware_ping_ip,
                "last_hardware_telemetry_ts": self._last_hardware_telemetry_ts,
                "hardware_online": online,
                "commanded": dict(self._command_state),
                "reported": dict(self._last_hardware_relay_state),
            }

    def latest_payload(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._latest_payload)

    def history(self, limit: int = 60) -> list[dict[str, Any]]:
        with self._state_lock:
            return list(self._history)[-limit:]

    def alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._state_lock:
            return list(self._alert_log)[-limit:]

    def config_snapshot(self) -> dict[str, Any]:
        return {
            "temp_threshold": config.TEMP_STAGE1_ON,
            "rain_stage2": config.RAIN_STAGE2_ON,
            "rain_stage3": config.RAIN_STAGE3_ON,
            "simulation_mode": config.SIMULATION_MODE,
        }

    def readiness_checks(self) -> dict[str, bool]:
        checks = {
            "db": self._repository.is_ready(),
            "sensor": True,
        }
        self._metrics.set_gauge("wms_ready", 1.0 if all(checks.values()) else 0.0)
        return checks


class SensorPipeline:
    """Background pipeline for local sensor->decision->alert processing."""

    def __init__(
        self,
        sensor: SensorPort,
        decision: DecisionPort,
        alerter: AlertPort,
        service: WeatherService,
    ) -> None:
        self._sensor = sensor
        self._decision = decision
        self._alerter = alerter
        self._service = service

        self._running = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._running.set()
        self._thread.start()
        logger.info("SensorPipeline started")

    def stop(self) -> None:
        self._running.clear()
        self._alerter.shutdown()
        self._sensor.close()
        logger.info("SensorPipeline stopped")

    def _run(self) -> None:
        while self._running.is_set():
            try:
                new_correlation_id()
                reading = self._sensor.read()
                reading_dict = reading.to_dict()
                stage, confidence, label = self._decision.classify(reading_dict)

                led_stage = self._resolve_led_stage(stage, reading_dict)
                self._alerter.apply(led_stage)

                payload: dict[str, Any] = {
                    **reading_dict,
                    "stage": stage,
                    "status": label,
                    "confidence": round(confidence, 3),
                    "led1": led_stage == 1,
                    "led2": led_stage == 2,
                    "led3": led_stage >= 3,
                    "buzzer": led_stage >= 3 and bool(reading_dict.get("water_level_numeric", 0)),
                    "timestamp": reading.timestamp,
                }
                self._service.ingest_payload_dict(payload)
            except ServiceValidationError as exc:
                logger.warning("pipeline payload validation failed: %s", exc)
            except Exception as exc:
                logger.error("pipeline iteration failed: %s", exc)

            time.sleep(config.SIMULATION_INTERVAL)

    @staticmethod
    def _resolve_led_stage(stage: int, reading: dict[str, Any]) -> int:
        if stage <= 0:
            return 0
        if stage >= 2:
            return 3
        if int(reading.get("rain_level", 0)) >= config.RAIN_STAGE2_ON:
            return 2
        return 1

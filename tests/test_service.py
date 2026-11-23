from __future__ import annotations

from pathlib import Path

from observability import Metrics
from repository import TelemetryRepository
from service import WeatherService


def test_service_ingests_payload(tmp_path: Path) -> None:
    repo = TelemetryRepository(str(tmp_path / "db.sqlite"), str(tmp_path / "log.csv"))
    repo.open()

    service = WeatherService(repository=repo, metrics=Metrics())
    payload: dict[str, object] = {
        "timestamp": 2.0,
        "temperature": 30.0,
        "rain_level": 100,
        "water_detected": False,
        "status": "NORMAL",
        "stage": 0,
        "confidence": 0.9,
        "source": "test",
        "led1": False,
        "led2": False,
        "led3": False,
        "buzzer": False,
    }

    normalized = service.ingest_payload_dict(payload)

    assert normalized["temperature"] == 30.0
    assert service.latest_payload()["stage"] == 0
    repo.close()


def test_service_ingests_esp_telemetry_and_commands(tmp_path: Path) -> None:
    repo = TelemetryRepository(str(tmp_path / "db.sqlite"), str(tmp_path / "log.csv"))
    repo.open()

    service = WeatherService(repository=repo, metrics=Metrics())
    normalized = service.ingest_esp_telemetry(
        rain=0,
        soil=1450,
        temp=19.5,
        relay1=1,
        relay2=0,
        relay3=1,
    )

    assert normalized["status"] == "CRITICAL"
    relay_states = normalized.get("relay_states", {})
    assert relay_states.get("r1") is True
    assert relay_states.get("r3") is True
    assert service.get_device_commands_line() == "r1=0,r2=0,r3=0"
    repo.close()

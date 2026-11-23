from __future__ import annotations

from pathlib import Path

from repository import TelemetryRepository


def test_repository_persists_payload(tmp_path: Path) -> None:
    db_file = tmp_path / "weather_data.db"
    csv_file = tmp_path / "sensor_log.csv"

    repo = TelemetryRepository(str(db_file), str(csv_file))
    repo.open()

    payload = {
        "timestamp": 1.0,
        "temperature": 25.1,
        "rain_level": 10,
        "water_detected": False,
        "status": "NORMAL",
        "stage": 0,
        "confidence": 1.0,
    }
    repo.persist_payload(payload)
    repo.close()

    assert db_file.exists()
    assert csv_file.exists()
    assert "temperature" in csv_file.read_text(encoding="utf-8")

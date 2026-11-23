"""Pydantic contracts for API payload validation and responses."""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SensorPayload(BaseModel):
    """Canonical sensor/decision payload shared by ingest and stream APIs."""

    model_config = ConfigDict(extra="ignore")

    timestamp: float = Field(gt=0)
    temperature: float = Field(ge=-50.0, le=100.0)
    rain_level: int = Field(ge=0, le=1023)
    water_detected: bool
    status: Literal["NORMAL", "WARNING", "CRITICAL"] = "NORMAL"
    stage: int = Field(ge=0, le=2)
    confidence: float = Field(ge=0.0, le=1.0)
    source: str = "simulation"
    soil_moisture: int | None = Field(default=None, ge=0, le=4095)
    relay_states: dict[str, bool] | None = None

    led1: bool = False
    led2: bool = False
    led3: bool = False
    buzzer: bool = False


class RelayCommandPayload(BaseModel):
    r1: int = Field(ge=0, le=1)
    r2: int = Field(ge=0, le=1)
    r3: int = Field(ge=0, le=1)
    source: str = "dashboard"


class DeviceTelemetryPayload(BaseModel):
    rain: int = Field(ge=0, le=1)
    soil: int = Field(ge=0, le=4095)
    temp: float = Field(ge=-40.0, le=80.0)
    relay1: int = Field(ge=0, le=1)
    relay2: int = Field(ge=0, le=1)
    relay3: int = Field(ge=0, le=1)

    def to_sensor_payload(self) -> SensorPayload:
        relay_states = {
            "r1": self.relay1 == 1,
            "r2": self.relay2 == 1,
            "r3": self.relay3 == 1,
        }

        if relay_states["r3"]:
            stage = 2
            status = "CRITICAL"
        elif relay_states["r1"] or relay_states["r2"]:
            stage = 1
            status = "WARNING"
        else:
            stage = 0
            status = "NORMAL"

        return SensorPayload(
            timestamp=time.time(),
            temperature=float(self.temp),
            rain_level=800 if self.rain == 0 else 0,
            water_detected=self.relay1 == 1,
            soil_moisture=int(self.soil),
            relay_states=relay_states,
            status=status,
            stage=stage,
            confidence=1.0,
            source="esp32_hardware",
            led1=relay_states["r1"],
            led2=relay_states["r2"],
            led3=relay_states["r3"],
            buzzer=relay_states["r3"],
        )


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str


class ReadyResponse(BaseModel):
    ready: bool
    checks: dict[str, bool]

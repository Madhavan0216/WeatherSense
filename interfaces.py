"""Core pluggable interfaces for sensors, decision models, and alerters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SensorPort(ABC):
    """Port for sensor providers."""

    @abstractmethod
    def read(self) -> Any:
        """Return a sensor reading object."""

    @abstractmethod
    def close(self) -> None:
        """Release sensor resources."""


class DecisionPort(ABC):
    """Port for decision engines."""

    @abstractmethod
    def classify(self, reading_dict: dict[str, Any]) -> tuple[int, float, str]:
        """Return stage, confidence, and label."""


class AlertPort(ABC):
    """Port for alert output providers."""

    @abstractmethod
    def apply(self, stage: int) -> None:
        """Apply stage-specific outputs."""

    @abstractmethod
    def shutdown(self) -> None:
        """Release alert resources."""

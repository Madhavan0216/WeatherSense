"""Decision engine wrapper around the production ML pipeline."""

from __future__ import annotations

import logging
from typing import Any

import config
from interfaces import DecisionPort
from ml_pipeline import MLPipeline
from observability import Metrics

logger = logging.getLogger(__name__)


class DecisionEngine(DecisionPort):
    """Classifies environmental conditions and exposes action mapping."""

    def __init__(self, metrics: Metrics | None = None) -> None:
        self._pipeline = MLPipeline(metrics=metrics)
        logger.info("DecisionEngine ready")

    def classify(self, reading_dict: dict[str, Any]) -> tuple[int, float, str]:
        return self._pipeline.classify(reading_dict)

    def get_action(self, stage: int) -> dict[str, bool]:
        """Map stage to led/buzzer actions."""
        return {
            0: {"led1": False, "led2": False, "led3": False, "buzzer": False},
            1: {"led1": True, "led2": False, "led3": False, "buzzer": False},
            2: {"led1": False, "led2": True, "led3": False, "buzzer": False},
            3: {"led1": False, "led2": False, "led3": True, "buzzer": True},
        }.get(stage, {"led1": False, "led2": False, "led3": False, "buzzer": False})


__all__ = ["DecisionEngine", "config"]

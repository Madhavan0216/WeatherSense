"""ML pipeline with model registry, async ensemble inference, caching, drift checks, and SHAP-style logging."""
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnnecessaryCast=false

from __future__ import annotations

import asyncio
import json
import logging
import math
import pickle
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, cast, runtime_checkable

import config
from observability import Metrics

logger = logging.getLogger(__name__)


@runtime_checkable
class SupportsPredictProba(Protocol):
    def predict(self, X: list[list[float]]) -> list[int]:
        ...

    def predict_proba(self, X: list[list[float]]) -> list[list[float]]:
        ...


class ModelRegistry:
    """Resolves and loads the latest versioned model artifact."""

    def __init__(self, model_path: str) -> None:
        self._model_path = Path(model_path)

    def latest_model_file(self) -> Path:
        candidates = sorted(
            self._model_path.parent.glob("model_*.pkl"),
            key=lambda p: p.stem,
            reverse=True,
        )
        return candidates[0] if candidates else self._model_path

    def load(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        model_file = self.latest_model_file()
        if not model_file.exists():
            return None, {}

        with model_file.open("rb") as handle:
            bundle = cast(Any, pickle.load(handle))

        version = model_file.stem.replace("model_", "")
        sidecar = model_file.with_suffix(".json")
        metadata_file = sidecar if sidecar.exists() else model_file.with_name("model_metadata.json")

        metadata: dict[str, Any] = {}
        if metadata_file.exists():
            metadata = cast(dict[str, Any], json.loads(metadata_file.read_text(encoding="utf-8")))
            metadata.setdefault("version", version)

        if not isinstance(bundle, dict):
            return None, metadata
        return bundle, metadata


class MLPipeline:
    """Production-oriented ML inference pipeline with drift/ensemble controls."""

    def __init__(self, metrics: Metrics | None = None) -> None:
        self._metrics = metrics or Metrics()
        self._registry = ModelRegistry(config.MODEL_PATH)

        self._bundle: dict[str, Any] | None = None
        self._metadata: dict[str, Any] = {}
        self._feature_stats: dict[str, dict[str, float]] = {}

        self._window: deque[list[float]] = deque(maxlen=200)
        self._prediction_count = 0

        self._cache_lock = Lock()
        self._cache_key: tuple[float, float, float] | None = None
        self._cache_value: tuple[int, float, str] | None = None
        self._cache_ts = 0.0

        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wms-ml")
        self._load_models()

    def _load_models(self) -> None:
        bundle, metadata = self._registry.load()
        self._bundle = bundle
        self._metadata = metadata

        training_distribution = cast(dict[str, Any], metadata.get("training_distribution", {}))
        self._feature_stats = {
            str(name): {
                "mean": float(cast(dict[str, Any], stats).get("mean", 0.0)),
                "std": max(1e-6, float(cast(dict[str, Any], stats).get("std", 1.0))),
            }
            for name, stats in training_distribution.items()
            if isinstance(stats, dict)
        }

        model_source = "rules"
        if bundle is not None:
            model_source = str(metadata.get("version", "model.pkl"))
        logger.info("MLPipeline initialized (source=%s)", model_source)

    def classify(self, reading_dict: dict[str, Any]) -> tuple[int, float, str]:
        """Synchronous wrapper that executes async inference in a thread pool."""
        key = (
            float(reading_dict.get("temperature", 25.0)),
            float(reading_dict.get("rain_level", 0)),
            float(reading_dict.get("water_level_numeric", 0)),
        )

        with self._cache_lock:
            if self._cache_key == key and (time.monotonic() - self._cache_ts) <= 0.5 and self._cache_value is not None:
                return self._cache_value

        try:
            result = asyncio.run(self._classify_async(reading_dict))
        except RuntimeError:
            result = self._classify_sync(reading_dict)

        with self._cache_lock:
            self._cache_key = key
            self._cache_value = result
            self._cache_ts = time.monotonic()

        return result

    async def _classify_async(self, reading_dict: dict[str, Any]) -> tuple[int, float, str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._classify_sync, reading_dict)

    def _classify_sync(self, data: dict[str, Any]) -> tuple[int, float, str]:
        features = [
            float(data.get("temperature", 25.0)),
            float(data.get("rain_level", 0)),
            float(data.get("water_level_numeric", 0)),
        ]

        rule_stage, rule_confidence, rule_label = self._classify_rules(data)
        if self._bundle is None:
            self._post_prediction(features, rule_stage, rule_label, {"rule": 1.0})
            return rule_stage, rule_confidence, rule_label

        dt_model = self._bundle.get("dt")
        rf_model = self._bundle.get("rf")

        votes: list[int] = [rule_stage]
        if isinstance(dt_model, SupportsPredictProba):
            votes.append(int(dt_model.predict([features])[0]))
        if isinstance(rf_model, SupportsPredictProba):
            votes.append(int(rf_model.predict([features])[0]))

        counts = Counter(votes)
        stage, vote_count = counts.most_common(1)[0]
        confidence = vote_count / max(1, len(votes))

        if confidence < 0.6:
            logger.warning("Low ensemble confidence %.3f; deferring to rule engine", confidence)
            stage, confidence, label = rule_stage, rule_confidence, rule_label
        else:
            label = config.STAGE_LABELS.get(stage, "UNKNOWN")

        shap_values = self._approximate_shap(features)
        self._post_prediction(features, stage, label, shap_values)
        return stage, confidence, label

    def _classify_rules(self, data: dict[str, Any]) -> tuple[int, float, str]:
        temp = float(data.get("temperature", 25.0))
        rain = int(data.get("rain_level", 0))
        water = int(data.get("water_level_numeric", 0))

        if water or rain >= config.RAIN_STAGE3_ON:
            return 2, 1.0, "CRITICAL"
        if rain >= config.RAIN_STAGE2_ON:
            return 1, 1.0, "WARNING"
        if temp > config.TEMP_STAGE1_ON:
            return 1, 1.0, "WARNING"
        return 0, 1.0, "NORMAL"

    def _approximate_shap(self, features: list[float]) -> dict[str, float]:
        names = ["temperature", "rain_level", "water_level_numeric"]

        importances = [0.34, 0.33, 0.33]
        bundle = self._bundle
        if bundle is not None:
            best_name = str(bundle.get("best", "rf"))
            best_model = bundle.get(best_name)
            if best_model is not None and hasattr(best_model, "feature_importances_"):
                raw = getattr(best_model, "feature_importances_")
                if hasattr(raw, "tolist"):
                    raw = raw.tolist()
                if isinstance(raw, list) and len(raw) == 3:
                    total = max(1e-6, float(sum(float(v) for v in raw)))
                    importances = [float(v) / total for v in raw]

        shap_values: dict[str, float] = {}
        for idx, name in enumerate(names):
            stats = self._feature_stats.get(name, {"mean": 0.0, "std": 1.0})
            mean = float(stats.get("mean", 0.0))
            std = max(1e-6, float(stats.get("std", 1.0)))
            z = (features[idx] - mean) / std
            shap_values[name] = round(z * importances[idx], 6)

        return shap_values

    def _post_prediction(self, features: list[float], stage: int, label: str, shap_values: dict[str, float]) -> None:
        self._prediction_count += 1
        self._window.append(features)

        if self._prediction_count % 100 == 0 and len(self._window) >= 100:
            drift_detected = self._check_drift()
            self._metrics.set_gauge("wms_model_drift", 1.0 if drift_detected else 0.0)

        logger.info(
            "ml_prediction stage=%d label=%s shap_values=%s",
            stage,
            label,
            json.dumps(shap_values, separators=(",", ":")),
        )

    def _check_drift(self) -> bool:
        if not self._feature_stats:
            return False

        columns = ["temperature", "rain_level", "water_level_numeric"]
        values_by_feature = list(zip(*self._window))
        drift = False

        for idx, name in enumerate(columns):
            baseline = self._feature_stats.get(name)
            if baseline is None:
                continue

            base_mean = float(baseline.get("mean", 0.0))
            base_std = max(1e-6, float(baseline.get("std", 1.0)))

            current_values = values_by_feature[idx]
            curr_mean = float(sum(current_values) / len(current_values))
            variance = sum((v - curr_mean) ** 2 for v in current_values) / max(1, len(current_values) - 1)
            curr_std = max(1e-6, math.sqrt(variance))

            kl = math.log(curr_std / base_std) + ((base_std**2 + (base_mean - curr_mean) ** 2) / (2 * curr_std**2)) - 0.5
            if kl > 0.15:
                drift = True
                logger.critical("Model drift detected for %s: KL=%.4f", name, kl)

        return drift

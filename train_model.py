"""Train Decision Tree + Random Forest and save versioned model registry artifacts."""
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnnecessaryCast=false

from __future__ import annotations

import json
import logging
import pickle
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
import sklearn.metrics as sk_metrics
import sklearn.model_selection as sk_model_selection
from sklearn.tree import DecisionTreeClassifier

import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

accuracy_score: Any = sk_metrics.accuracy_score
classification_report: Any = sk_metrics.classification_report
confusion_matrix: Any = sk_metrics.confusion_matrix
cross_val_score: Any = sk_model_selection.cross_val_score
train_test_split: Any = sk_model_selection.train_test_split


def generate_dataset(n_samples: int = 6000, seed: int = 42) -> pd.DataFrame:
    """Generate synthetic training data for three-stage weather classification."""
    rng = np.random.default_rng(seed)
    rows: list[list[float | int]] = []

    per_class = n_samples // 3
    remainder = n_samples - per_class * 3

    for _ in range(per_class):
        temp = float(rng.uniform(18, config.TEMP_STAGE1_OFF))
        rain = int(cast(Any, rng.integers(0, config.RAIN_STAGE2_ON - 1)))
        rows.append([temp, rain, 0, 0])

    for _ in range(per_class):
        if float(rng.random()) < 0.5:
            temp = float(rng.uniform(config.TEMP_STAGE1_ON + 0.5, 50))
            rain = int(cast(Any, rng.integers(0, config.RAIN_STAGE2_ON - 1)))
            water = 0
        else:
            temp = float(rng.uniform(15, 45))
            rain = int(cast(Any, rng.integers(config.RAIN_STAGE2_ON, config.RAIN_STAGE3_ON - 1)))
            water = 0
        rows.append([temp, rain, water, 1])

    for _ in range(per_class + remainder):
        if float(rng.random()) < 0.5:
            rain = int(cast(Any, rng.integers(config.RAIN_STAGE3_ON, 1024)))
            water = int(float(rng.random()) < 0.7)
        else:
            rain = int(cast(Any, rng.integers(0, 1024)))
            water = 1
        temp = float(rng.uniform(15, 50))
        rows.append([temp, rain, water, 2])

    df = pd.DataFrame(rows, columns=["temperature", "rain_level", "water_level_numeric", "label"])
    df["temperature"] = df["temperature"] + rng.normal(0, 0.5, len(df))
    df["rain_level"] = df["rain_level"].astype(int).clip(0, 1023)
    df["water_level_numeric"] = df["water_level_numeric"].astype(int).clip(0, 1)
    return df.sample(frac=1, random_state=seed).reset_index(drop=True)


def _feature_distribution(df: pd.DataFrame) -> dict[str, dict[str, float]]:
    dist: dict[str, dict[str, float]] = {}
    for feature in config.FEATURE_NAMES:
        values = df[feature].astype(float)
        dist[feature] = {
            "mean": float(values.mean()),
            "std": float(values.std() or 1.0),
            "min": float(values.min()),
            "max": float(values.max()),
        }
    return dist


def train_and_evaluate(df: pd.DataFrame) -> tuple[dict[str, Any], dict[str, Any]]:
    """Train candidate models and return model bundle with metadata."""
    X = df[config.FEATURE_NAMES].values
    y = df["label"].values

    y_array = np.asarray(y)
    split = cast(tuple[Any, Any, Any, Any], train_test_split(
        X,
        y_array,
        test_size=0.2,
        random_state=42,
        stratify=y_array,
    ))
    X_train, X_test, y_train, y_test = split

    models: dict[str, Any] = {
        "dt": DecisionTreeClassifier(
            max_depth=8,
            min_samples_split=10,
            min_samples_leaf=5,
            class_weight="balanced",
            random_state=42,
        ),
        "rf": RandomForestClassifier(
            n_estimators=150,
            max_depth=10,
            min_samples_split=5,
            min_samples_leaf=3,
            class_weight="balanced",
            n_jobs=1,
            random_state=42,
        ),
    }

    results: dict[str, dict[str, Any]] = {}
    for name, model in models.items():
        model.fit(X_train, y_train)
        y_pred = model.predict(X_test)

        acc = float(cast(float, accuracy_score(y_test, y_pred)))
        cv_acc = float(np.mean(cast(Any, cross_val_score(model, X, y_array, cv=5, scoring="accuracy"))))

        report = str(cast(Any, classification_report(y_test, y_pred, target_names=["NORMAL", "WARNING", "CRITICAL"])))
        cm = cast(Any, confusion_matrix(y_test, y_pred))

        results[name] = {
            "model": model,
            "accuracy": acc,
            "cv_acc": cv_acc,
            "report": report,
            "cm": cm,
        }

        logger.info("%s | test_acc=%.4f | cv_acc=%.4f", name.upper(), acc, cv_acc)
        logger.info("\n%s", report)

    best_name = max(results, key=lambda k: float(results[k]["cv_acc"]))
    logger.info("Best model by CV: %s", best_name.upper())

    metadata: dict[str, Any] = {
        "version": datetime.now(UTC).strftime("%Y%m%d%H%M%S"),
        "trained_at": datetime.now(UTC).isoformat(),
        "cv_accuracy": {
            "dt": float(results["dt"]["cv_acc"]),
            "rf": float(results["rf"]["cv_acc"]),
            "best": best_name,
        },
        "feature_names": list(config.FEATURE_NAMES),
        "training_distribution": _feature_distribution(df),
        "labels": dict(config.STAGE_LABELS),
    }

    bundle: dict[str, Any] = {
        "dt": results["dt"]["model"],
        "rf": results["rf"]["model"],
        "best": best_name,
        "meta": metadata,
    }
    return bundle, metadata


def save_artifacts(bundle: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Save canonical and versioned model files with metadata sidecars."""
    model_path = Path(config.MODEL_PATH)
    model_path.parent.mkdir(parents=True, exist_ok=True)

    version = str(metadata["version"])
    versioned_model = model_path.with_name(f"model_{version}.pkl")
    versioned_meta = model_path.with_name(f"model_{version}.json")

    with model_path.open("wb") as handle:
        pickle.dump(bundle, handle)
    with versioned_model.open("wb") as handle:
        pickle.dump(bundle, handle)

    metadata_path = model_path.with_name("model_metadata.json")
    metadata_json = json.dumps(metadata, indent=2)
    metadata_path.write_text(metadata_json, encoding="utf-8")
    versioned_meta.write_text(metadata_json, encoding="utf-8")

    logger.info("Saved model bundle to %s", model_path)
    logger.info("Saved versioned model bundle to %s", versioned_model)
    logger.info("Saved metadata to %s", metadata_path)
    logger.info("Saved versioned metadata to %s", versioned_meta)


def main() -> None:
    logger.info("Generating synthetic dataset...")
    df = generate_dataset(n_samples=6000)
    logger.info("Dataset rows: %d", len(df))

    logger.info("Training models...")
    bundle, metadata = train_and_evaluate(df)
    save_artifacts(bundle, metadata)

    logger.info("Training complete. Best model: %s", str(bundle["best"]).upper())


if __name__ == "__main__":
    main()

"""Persistence layer for telemetry payloads (SQLite + CSV)."""

from __future__ import annotations

import csv
import logging
import os
import sqlite3
import threading
from typing import Any

import config
from resilience import CircuitBreaker, CircuitBreakerOpenError, retry_with_backoff

logger = logging.getLogger(__name__)


class RepositoryError(RuntimeError):
    """Base repository exception."""


class PersistenceUnavailableError(RepositoryError):
    """Raised when a persistence backend is unavailable."""


class TelemetryRepository:
    """Repository responsible for durable telemetry persistence."""

    def __init__(self, db_file: str, csv_file: str) -> None:
        self._db_file = db_file
        self._csv_file = csv_file
        self._db_conn: sqlite3.Connection | None = None
        self._db_lock = threading.Lock()
        self._csv_lock = threading.Lock()

        self._db_breaker = CircuitBreaker(failure_threshold=4, reset_timeout_seconds=20.0)
        self._csv_breaker = CircuitBreaker(failure_threshold=4, reset_timeout_seconds=20.0)

    def open(self) -> None:
        """Open repository connections and ensure schema is present."""
        self._db_conn = sqlite3.connect(self._db_file, check_same_thread=False)
        self._db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL,
                temperature REAL,
                rain_level  INTEGER,
                water       INTEGER,
                status      TEXT,
                stage       INTEGER,
                confidence  REAL
            )
            """
        )
        self._db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS command_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL,
                r1      INTEGER,
                r2      INTEGER,
                r3      INTEGER,
                source  TEXT
            )
            """
        )
        self._db_conn.commit()
        logger.info("Repository opened")

    def close(self) -> None:
        """Close repository resources."""
        if self._db_conn is not None:
            self._db_conn.close()
            self._db_conn = None
            logger.info("Repository closed")

    def is_ready(self) -> bool:
        return self._db_conn is not None

    def persist_payload(self, payload: dict[str, Any]) -> None:
        """Persist payload to all configured sinks with resilience wrappers."""
        try:
            retry_with_backoff(lambda: self._db_breaker.call(lambda: self._write_db(payload)))
        except CircuitBreakerOpenError as exc:
            raise PersistenceUnavailableError("database circuit open") from exc

        try:
            retry_with_backoff(lambda: self._csv_breaker.call(lambda: self._write_csv(payload)))
        except CircuitBreakerOpenError as exc:
            raise PersistenceUnavailableError("csv circuit open") from exc

    def _write_db(self, payload: dict[str, Any]) -> None:
        if self._db_conn is None:
            raise PersistenceUnavailableError("database not initialized")

        with self._db_lock:
            self._db_conn.execute(
                "INSERT INTO readings(ts,temperature,rain_level,water,status,stage,confidence) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    payload.get("timestamp"),
                    payload.get("temperature"),
                    payload.get("rain_level"),
                    int(payload.get("water_detected", False)),
                    payload.get("status"),
                    payload.get("stage"),
                    payload.get("confidence"),
                ),
            )
            self._db_conn.commit()

    def _write_csv(self, payload: dict[str, Any]) -> None:
        file_exists = os.path.exists(self._csv_file)
        with self._csv_lock:
            with open(self._csv_file, "a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "timestamp",
                        "temperature",
                        "rain_level",
                        "water_detected",
                        "status",
                        "stage",
                        "confidence",
                    ],
                )
                if not file_exists:
                    writer.writeheader()
                writer.writerow({k: payload.get(k, "") for k in writer.fieldnames})

    def persist_command(self, r1: int, r2: int, r3: int, source: str) -> None:
        if self._db_conn is None:
            raise PersistenceUnavailableError("database not initialized")

        with self._db_lock:
            self._db_conn.execute(
                "INSERT INTO command_log(ts,r1,r2,r3,source) VALUES(strftime('%s','now'),?,?,?,?)",
                (int(r1), int(r2), int(r3), source),
            )
            self._db_conn.commit()

    def latest_command(self) -> dict[str, int] | None:
        if self._db_conn is None:
            return None

        with self._db_lock:
            row = self._db_conn.execute(
                "SELECT r1, r2, r3 FROM command_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "r1": int(row[0]),
            "r2": int(row[1]),
            "r3": int(row[2]),
        }


def build_repository_from_config() -> TelemetryRepository:
    """Factory for repository from project configuration."""
    return TelemetryRepository(db_file=config.DB_FILE, csv_file=config.CSV_LOG_FILE)

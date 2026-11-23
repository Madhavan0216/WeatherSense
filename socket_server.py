"""Application bootstrap wiring repository, service, and controller layers."""

from __future__ import annotations

import logging
import os
from http.server import HTTPServer

import config
from alert_controller import AlertController
from controller import (
    FirmwareController,
    HmacFrameVerifier,
    HttpControllerFactory,
    JwtVerifier,
    TcpSensorServer,
    TokenBucketRateLimiter,
)
from decision_engine import DecisionEngine
from observability import JsonFormatter, Metrics
from repository import build_repository_from_config
from sensor_handler import SensorHandler
from service import SensorPipeline, WeatherService

logger = logging.getLogger(__name__)


class AppContainer:
    """Dependency-injection container for runtime components."""

    def __init__(self) -> None:
        self.metrics = Metrics()

        self.repository = build_repository_from_config()
        self.service = WeatherService(repository=self.repository, metrics=self.metrics)

        self.sensor = SensorHandler(simulation=config.SIMULATION_MODE)
        self.decision = DecisionEngine(metrics=self.metrics)
        self.alerter = AlertController(simulation=config.SIMULATION_MODE)
        self.pipeline = SensorPipeline(
            sensor=self.sensor,
            decision=self.decision,
            alerter=self.alerter,
            service=self.service,
        )

        self.http_rate_limiter = TokenBucketRateLimiter(
            capacity=config.RATE_LIMIT_REQUESTS_PER_MINUTE,
            per_seconds=60,
        )
        self.device_rate_limiter = TokenBucketRateLimiter(
            capacity=config.DEVICE_RATE_LIMIT_RPM,
            per_seconds=60,
        )
        self.tcp_rate_limiter = TokenBucketRateLimiter(
            capacity=config.RATE_LIMIT_REQUESTS_PER_MINUTE,
            per_seconds=60,
        )

        self.jwt_verifier = JwtVerifier(
            secret=config.JWT_SECRET,
            algorithm=config.JWT_ALGORITHM,
        )
        self.hmac_verifier = HmacFrameVerifier(secret=config.TCP_HMAC_SECRET)
        self.firmware_controller = FirmwareController(
            service=self.service,
            metrics=self.metrics,
            rate_limiter=self.device_rate_limiter,
        )

        http_handler_factory = HttpControllerFactory(
            service=self.service,
            metrics=self.metrics,
            jwt_verifier=self.jwt_verifier,
            rate_limiter=self.http_rate_limiter,
            device_controller=self.firmware_controller,
            frontend_dir=os.path.dirname(__file__),
        )
        self.http_handler = http_handler_factory.build()

        self.http_server = HTTPServer(("0.0.0.0", config.HTTP_PORT), self.http_handler)
        self.tcp_server = TcpSensorServer(
            host=config.SOCKET_HOST,
            port=config.SOCKET_PORT,
            service=self.service,
            frame_verifier=self.hmac_verifier,
            rate_limiter=self.tcp_rate_limiter,
            metrics=self.metrics,
        )

    def start(self) -> None:
        self.repository.open()
        self.tcp_server.start()
        self.pipeline.start()

        logger.info("HTTP dashboard on http://localhost:%d", config.HTTP_PORT)
        self.http_server.serve_forever()

    def stop(self) -> None:
        self.pipeline.stop()
        self.tcp_server.stop()
        self.http_server.shutdown()
        self.repository.close()


def _configure_root_json_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        return
    if any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers):
        return

    for handler in root.handlers:
        handler.setFormatter(JsonFormatter())


def start_all() -> None:
    """Start the full weather monitoring stack."""
    _configure_root_json_logging()

    container = AppContainer()
    try:
        container.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        container.stop()

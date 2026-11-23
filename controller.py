"""Transport/controller layer: HTTP and TCP handlers with security controls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import config
from contracts import DeviceTelemetryPayload, HealthResponse, ReadyResponse, RelayCommandPayload
from observability import Metrics, get_correlation_id, new_correlation_id, set_correlation_id
from service import ServiceValidationError, WeatherService

logger = logging.getLogger(__name__)

MIME = {
    ".html": "text/html",
    ".js": "application/javascript",
    ".css": "text/css",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class AuthError(PermissionError):
    """Raised when authentication fails."""


class RateLimitError(PermissionError):
    """Raised when requests exceed configured rates."""


@dataclass
class TokenBucket:
    capacity: float
    refill_rate_per_second: float
    tokens: float
    updated_at: float


class TokenBucketRateLimiter:
    """In-memory token-bucket rate limiter keyed by identity."""

    def __init__(self, capacity: int, per_seconds: int) -> None:
        self._capacity = float(capacity)
        self._refill_rate = float(capacity) / float(per_seconds)
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(
                    capacity=self._capacity,
                    refill_rate_per_second=self._refill_rate,
                    tokens=self._capacity,
                    updated_at=now,
                )
                self._buckets[key] = bucket

            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.refill_rate_per_second)
            bucket.updated_at = now

            if bucket.tokens < cost:
                return False

            bucket.tokens -= cost
            return True


def _base64url_decode(segment: str) -> bytes:
    padding = "=" * ((4 - len(segment) % 4) % 4)
    return base64.urlsafe_b64decode(segment + padding)


class JwtVerifier:
    """HS256 JWT verifier without third-party dependencies."""

    def __init__(self, secret: str, algorithm: str = "HS256") -> None:
        self._secret = secret.encode("utf-8")
        self._algorithm = algorithm

    def verify(self, token: str) -> dict[str, Any]:
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("malformed jwt")

        header_b64, payload_b64, signature_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")

        header = json.loads(_base64url_decode(header_b64))
        payload = cast(dict[str, Any], json.loads(_base64url_decode(payload_b64)))

        if header.get("alg") != self._algorithm:
            raise AuthError("unsupported jwt algorithm")

        expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        actual = _base64url_decode(signature_b64)
        if not hmac.compare_digest(expected, actual):
            raise AuthError("invalid jwt signature")

        now = int(time.time())
        exp = payload.get("exp")
        if exp is not None and int(exp) < now:
            raise AuthError("jwt expired")

        nbf = payload.get("nbf")
        if nbf is not None and int(nbf) > now:
            raise AuthError("jwt not active")

        return payload


class HmacFrameVerifier:
    """Validates TCP frame envelope signatures using HMAC-SHA256."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode("utf-8")

    def verify_frame(self, frame_data: dict[str, Any]) -> dict[str, Any]:
        payload = frame_data.get("payload")
        signature = frame_data.get("signature", "")

        if not isinstance(payload, dict) or not isinstance(signature, str):
            raise AuthError("invalid frame envelope")

        payload_dict = cast(dict[str, Any], payload)

        message = json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise AuthError("invalid frame signature")

        return payload_dict


class TcpSensorServer:
    """TCP transport that accepts signed telemetry frames."""

    def __init__(
        self,
        host: str,
        port: int,
        service: WeatherService,
        frame_verifier: HmacFrameVerifier,
        rate_limiter: TokenBucketRateLimiter,
        metrics: Metrics,
    ) -> None:
        self._host = host
        self._port = port
        self._service = service
        self._frame_verifier = frame_verifier
        self._rate_limiter = rate_limiter
        self._metrics = metrics

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self._host, self._port))
        self._sock.listen(5)

        self._running = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self._running.set()
        self._thread.start()
        logger.info("TCP server listening on %s:%d", self._host, self._port)

    def stop(self) -> None:
        self._running.clear()
        try:
            self._sock.close()
        except Exception:
            pass

    def _accept_loop(self) -> None:
        while self._running.is_set():
            try:
                conn, addr = self._sock.accept()
                threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()
            except Exception as exc:
                if self._running.is_set():
                    logger.error("tcp accept failed: %s", exc)

    def _handle_client(self, conn: socket.socket, addr: tuple[Any, ...]) -> None:
        ip = str(addr[0])
        logger.info("tcp client connected: %s", addr)
        buffer = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk

                while len(buffer) >= 4:
                    frame_len = int.from_bytes(buffer[:4], "big")
                    if len(buffer) < 4 + frame_len:
                        break

                    frame = buffer[4:4 + frame_len]
                    buffer = buffer[4 + frame_len:]

                    if not self._rate_limiter.allow(f"tcp:{ip}"):
                        self._metrics.inc("wms_ingest_failed_total")
                        logger.warning("tcp rate limited for %s", ip)
                        continue

                    try:
                        envelope = json.loads(frame.decode("utf-8"))
                        payload = self._frame_verifier.verify_frame(envelope)
                        self._service.ingest_payload_dict(payload)
                    except (json.JSONDecodeError, AuthError, ServiceValidationError) as exc:
                        self._metrics.inc("wms_ingest_failed_total")
                        logger.warning("invalid tcp frame from %s: %s", ip, exc)
        finally:
            conn.close()
            logger.info("tcp client disconnected: %s", addr)


class FirmwareController:
    """Firmware-native HTTP endpoints: form telemetry, plain-text commands, and ping."""

    def __init__(self, service: WeatherService, metrics: Metrics, rate_limiter: TokenBucketRateLimiter) -> None:
        self._service = service
        self._metrics = metrics
        self._rate_limiter = rate_limiter

    @staticmethod
    def _client_ip(handler: BaseHTTPRequestHandler) -> str:
        return str(handler.client_address[0])

    @staticmethod
    def _send_text(handler: BaseHTTPRequestHandler, body: str, status: int = 200) -> None:
        raw = body.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    @staticmethod
    def _send_empty(handler: BaseHTTPRequestHandler, status: int = 200) -> None:
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    @staticmethod
    def _extract_device_key(handler: BaseHTTPRequestHandler) -> str:
        header_key = handler.headers.get("X-Device-Key", "").strip()
        if header_key:
            return header_key
        query_key = parse_qs(urlparse(handler.path).query).get("key", [""])[0].strip()
        return query_key

    def _validate_device_access(self, handler: BaseHTTPRequestHandler) -> bool:
        ip = self._client_ip(handler)
        if not self._rate_limiter.allow(f"device:{ip}"):
            self._metrics.inc("wms_ingest_failed_total")
            self._send_text(handler, "rate limit exceeded", status=429)
            return False

        key = self._extract_device_key(handler)
        if key != config.DEVICE_API_KEY:
            self._metrics.inc("wms_ingest_failed_total")
            self._send_text(handler, "unauthorized", status=401)
            return False
        return True

    def handle_ping(self, handler: BaseHTTPRequestHandler) -> None:
        ip = self._client_ip(handler)
        self._service.record_hardware_ping(ip=ip)
        logger.info(
            "hardware_ping",
            extra={
                "event": "hardware_ping",
                "timestamp": time.time(),
                "client_ip": ip,
            },
        )
        self._send_text(handler, "ok", status=200)

    def handle_telemetry_post(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._validate_device_access(handler):
            return

        length = int(handler.headers.get("Content-Length", "0"))
        if length <= 0 or length > config.MAX_INGEST_BODY_BYTES:
            self._metrics.inc("wms_ingest_failed_total")
            self._send_text(handler, "invalid content length", status=413)
            return

        content_type = handler.headers.get("Content-Type", "")
        if "application/x-www-form-urlencoded" not in content_type:
            self._send_text(handler, "unsupported content type", status=415)
            return

        raw = handler.rfile.read(length).decode("utf-8", errors="ignore")
        form = parse_qs(raw, keep_blank_values=True)

        data = {
            "rain": form.get("rain", [""])[0],
            "soil": form.get("soil", [""])[0],
            "temp": form.get("temp", [""])[0],
            "relay1": form.get("relay1", [""])[0],
            "relay2": form.get("relay2", [""])[0],
            "relay3": form.get("relay3", [""])[0],
        }

        try:
            payload = DeviceTelemetryPayload.model_validate(data)
            self._service.ingest_esp_telemetry(
                rain=payload.rain,
                soil=payload.soil,
                temp=payload.temp,
                relay1=payload.relay1,
                relay2=payload.relay2,
                relay3=payload.relay3,
            )
        except Exception as exc:
            self._metrics.inc("wms_ingest_failed_total")
            self._send_text(handler, str(exc), status=400)
            return

        self._send_empty(handler, status=200)

    def handle_commands_get(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._validate_device_access(handler):
            return
        self._send_text(handler, self._service.get_device_commands_line(), status=200)


class HttpControllerFactory:
    """Factory that creates a configured HTTP request handler class."""

    def __init__(
        self,
        service: WeatherService,
        metrics: Metrics,
        jwt_verifier: JwtVerifier,
        rate_limiter: TokenBucketRateLimiter,
        device_controller: FirmwareController,
        frontend_dir: str,
    ) -> None:
        self._service = service
        self._metrics = metrics
        self._jwt_verifier = jwt_verifier
        self._rate_limiter = rate_limiter
        self._device_controller = device_controller
        self._frontend_dir = frontend_dir

    def build(self) -> type[BaseHTTPRequestHandler]:
        service = self._service
        metrics = self._metrics
        jwt_verifier = self._jwt_verifier
        rate_limiter = self._rate_limiter
        device_controller = self._device_controller
        frontend_dir = self._frontend_dir

        class DashboardHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _set_request_context(self) -> None:
                metrics.inc("wms_requests_total")
                incoming = self.headers.get("X-Correlation-ID", "").strip()
                if incoming:
                    set_correlation_id(incoming)
                else:
                    new_correlation_id()

            def _client_ip(self) -> str:
                return str(self.client_address[0])

            def _enforce_rate_limit(self) -> None:
                if not rate_limiter.allow(f"http:{self._client_ip()}"):
                    raise RateLimitError("rate limit exceeded")

            def _enforce_jwt(self) -> None:
                authz = self.headers.get("Authorization", "")
                if not authz.startswith("Bearer "):
                    raise AuthError("missing bearer token")
                token = authz.split(" ", 1)[1].strip()
                jwt_verifier.verify(token)

            def _enforce_sse_token(self, token: str) -> None:
                if not token:
                    raise AuthError("missing sse token")
                jwt_verifier.verify(token)

            def _is_allowed_origin(self, origin: str) -> bool:
                if not origin:
                    return True
                return origin in config.CORS_ALLOWED_ORIGINS

            def _apply_security_headers(self) -> None:
                self.send_header("X-Correlation-ID", get_correlation_id())
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                self.send_header("Content-Security-Policy", config.CSP_POLICY)

                origin = self.headers.get("Origin", "")
                if self._is_allowed_origin(origin):
                    self.send_header("Access-Control-Allow-Origin", origin or config.CORS_ALLOWED_ORIGINS[0])
                    self.send_header("Vary", "Origin")

            def _json(self, payload: dict[str, Any], status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self._apply_security_headers()
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:
                self._set_request_context()
                origin = self.headers.get("Origin", "")
                if not self._is_allowed_origin(origin):
                    self.send_response(403)
                    self.end_headers()
                    return

                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", origin or config.CORS_ALLOWED_ORIGINS[0])
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type,X-Correlation-ID,X-Device-Key")
                self._apply_security_headers()
                self.end_headers()

            def do_GET(self) -> None:
                self._set_request_context()
                parsed = urlparse(self.path)
                path = parsed.path

                if path == "/bm":
                    device_controller.handle_ping(self)
                    return

                if path == "/api/device/commands":
                    device_controller.handle_commands_get(self)
                    return

                public_paths = {
                    "/",
                    "/index.html",
                    "/dashboard.js",
                    "/styles.css",
                    "/manifest.json",
                    "/service-worker.js",
                    "/health",
                    "/api/latest",
                    "/api/data",
                    "/api/history",
                    "/api/alerts",
                    "/api/hardware/status",
                    "/api/relay/state",
                    "/api/stream",
                }
                is_icon = path.startswith("/icons/")

                try:
                    self._enforce_rate_limit()
                    if path not in public_paths and not is_icon:
                        if path == "/api/stream":
                            token = parse_qs(parsed.query).get("token", [""])[0]
                            self._enforce_sse_token(token)
                        else:
                            self._enforce_jwt()
                except (RateLimitError, AuthError) as exc:
                    code = 429 if isinstance(exc, RateLimitError) else 401
                    self._json({"error": str(exc)}, status=code)
                    return

                if path == "/health":
                    data = HealthResponse(status="ok", service="wms", version=config.APP_VERSION).model_dump()
                    self._json(data)
                    return

                if path == "/ready":
                    checks = service.readiness_checks()
                    ready = all(checks.values())
                    data = ReadyResponse(ready=ready, checks=checks).model_dump()
                    self._json(data, status=200 if ready else 503)
                    return

                if path == "/metrics":
                    body = metrics.render_prometheus().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4")
                    self.send_header("Content-Length", str(len(body)))
                    self._apply_security_headers()
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path == "/api/latest":
                    self._json(service.latest_payload())
                    return

                if path == "/api/data":
                    self._json(service.latest_payload())
                    return

                if path == "/api/history":
                    self._json({"history": service.history(60)})
                    return

                if path == "/api/alerts":
                    self._json({"alerts": service.alerts(50)})
                    return

                if path == "/api/config":
                    self._json(service.config_snapshot())
                    return

                if path == "/api/relay/state":
                    self._json(service.command_and_hardware_state())
                    return

                if path == "/api/hardware/status":
                    self._json(service.hardware_status())
                    return

                if path == "/api/stream":
                    self._sse_stream()
                    return

                file_path = os.path.join(frontend_dir, path.lstrip("/") or "index.html")
                self._serve_file(file_path)

            def do_POST(self) -> None:
                self._set_request_context()

                if self.path == "/api/telemetry":
                    device_controller.handle_telemetry_post(self)
                    return

                try:
                    self._enforce_rate_limit()
                    self._enforce_jwt()
                except (RateLimitError, AuthError) as exc:
                    code = 429 if isinstance(exc, RateLimitError) else 401
                    self._json({"error": str(exc)}, status=code)
                    return

                if self.path == "/api/relay/set":
                    self._relay_set()
                    return

                if self.path != "/api/ingest":
                    self._json({"error": "not found"}, status=404)
                    return

                origin = self.headers.get("Origin", "")
                if not self._is_allowed_origin(origin):
                    self._json({"error": "origin not allowed"}, status=403)
                    return

                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > config.MAX_INGEST_BODY_BYTES:
                    metrics.inc("wms_ingest_failed_total")
                    self._json({"error": "invalid content length"}, status=413)
                    return

                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw)
                    service.ingest_payload_dict(parsed)
                    self._json({"ok": True})
                except (json.JSONDecodeError, ServiceValidationError) as exc:
                    metrics.inc("wms_ingest_failed_total")
                    self._json({"error": str(exc)}, status=400)

            def _relay_set(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > config.MAX_INGEST_BODY_BYTES:
                    self._json({"error": "invalid content length"}, status=413)
                    return

                try:
                    payload = cast(dict[str, Any], json.loads(self.rfile.read(length)))
                    command = RelayCommandPayload.model_validate(payload)
                except (json.JSONDecodeError, ValueError) as exc:
                    self._json({"error": str(exc)}, status=400)
                    return

                state = service.set_command_state(command.r1, command.r2, command.r3, source=command.source)
                self._json({"ok": True, "state": state})

            def _sse_stream(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self._apply_security_headers()
                self.end_headers()

                try:
                    while True:
                        payload = service.latest_payload()
                        if payload:
                            message = f"data: {json.dumps(payload)}\\n\\n"
                            self.wfile.write(message.encode("utf-8"))
                            self.wfile.flush()
                        time.sleep(config.SIMULATION_INTERVAL)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def _serve_file(self, file_path: str) -> None:
                if not os.path.isfile(file_path):
                    self.send_response(404)
                    self._apply_security_headers()
                    self.end_headers()
                    return

                ext = os.path.splitext(file_path)[1]
                mime = MIME.get(ext, "application/octet-stream")
                with open(file_path, "rb") as handle:
                    content = handle.read()

                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(content)))
                self._apply_security_headers()
                self.end_headers()
                self.wfile.write(content)

        return DashboardHandler

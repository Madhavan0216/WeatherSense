from __future__ import annotations

import hashlib
import hmac
import json

from controller import HmacFrameVerifier


def test_hmac_frame_verifier_accepts_valid_signature() -> None:
    secret = "test-secret"
    payload = {"timestamp": 1.0, "temperature": 25.0}
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    verifier = HmacFrameVerifier(secret=secret)
    out = verifier.verify_frame({"payload": payload, "signature": signature})

    assert out["temperature"] == 25.0

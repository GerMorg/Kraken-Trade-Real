from __future__ import annotations

import base64
import hashlib
import hmac

from custom_components.kraken_ai_trader.core.crypto import futures_signature, spot_signature


def test_spot_signature_matches_reference_formula():
    secret = base64.b64encode(b"secret-secret").decode()
    body, sig = spot_signature("/0/private/Balance", 123, {}, secret)
    digest = hashlib.sha256(b"123" + body.encode()).digest()
    expected = base64.b64encode(hmac.new(b"secret-secret", b"/0/private/Balance" + digest, hashlib.sha512).digest()).decode()
    assert sig == expected
    assert body == "nonce=123"


def test_futures_signature_matches_reference_formula():
    secret = base64.b64encode(b"secret-secret").decode()
    body, sig = futures_signature("/api/v3/sendOrder", {"symbol": "PI_XBTUSD", "side": "buy"}, secret, 456)
    encoded = body.encode() + b"456" + b"/api/v3/sendOrder"
    digest = hashlib.sha256(encoded).digest()
    expected = base64.b64encode(hmac.new(b"secret-secret", digest, hashlib.sha512).digest()).decode()
    assert sig == expected

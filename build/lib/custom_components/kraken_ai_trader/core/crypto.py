from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import urlencode


def spot_signature(path: str, nonce: int, params: dict[str, object], secret_b64: str) -> tuple[str, str]:
    body_params = {"nonce": str(nonce), **params}
    post_data = urlencode(body_params, doseq=True)
    digest = hashlib.sha256(str(nonce).encode() + post_data.encode()).digest()
    secret = base64.b64decode(secret_b64)
    signature = base64.b64encode(hmac.new(secret, path.encode() + digest, hashlib.sha512).digest()).decode()
    return post_data, signature


def futures_signature(endpoint_path: str, params: dict[str, object], secret_b64: str, nonce: int) -> tuple[str, str]:
    body_params = {k: str(v) for k, v in params.items()}
    post_data = urlencode(body_params, doseq=True)
    encoded = post_data.encode() + str(nonce).encode() + endpoint_path.encode()
    digest = hashlib.sha256(encoded).digest()
    secret = base64.b64decode(secret_b64)
    signature = base64.b64encode(hmac.new(secret, digest, hashlib.sha512).digest()).decode()
    return post_data, signature

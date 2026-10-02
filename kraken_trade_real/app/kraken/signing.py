from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import urlencode


def sign_spot(path: str, data: dict[str, str | int], secret_b64: str) -> str:
    encoded = urlencode(data).encode("utf-8")
    nonce = str(data["nonce"]).encode("utf-8")
    digest = hashlib.sha256(nonce + encoded).digest()
    message = path.encode("utf-8") + digest
    secret = base64.b64decode(secret_b64)
    return base64.b64encode(hmac.new(secret, message, hashlib.sha512).digest()).decode("ascii")


def sign_futures(endpoint_path: str, form_data: str, secret_b64: str) -> str:
    # Kraken Futures uses SHA256(post data + endpoint path), then HMAC-SHA512.
    message = (form_data + endpoint_path).encode("utf-8")
    digest = hashlib.sha256(message).digest()
    secret = base64.b64decode(secret_b64)
    return base64.b64encode(hmac.new(secret, digest, hashlib.sha512).digest()).decode("ascii")

import base64
import secrets
import time

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from src.config import API_KEY, API_SECRET

_private_key = None


def _load_key():
    global _private_key
    if _private_key is not None:
        return _private_key

    secret = API_SECRET.strip()

    # Check if PEM format
    if secret.startswith("-----"):
        pem_bytes = secret.encode("utf-8").decode("unicode_escape").encode("utf-8")
        _private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        return _private_key

    # Raw base64 Ed25519 key (64 bytes: 32 seed + 32 public)
    raw = base64.b64decode(secret)
    if len(raw) == 64:
        _private_key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    elif len(raw) == 32:
        _private_key = Ed25519PrivateKey.from_private_bytes(raw)
    else:
        raise ValueError(f"Unexpected key length: {len(raw)} bytes. Expected 32 or 64.")

    return _private_key


def build_jwt(method: str, path: str) -> str:
    uri = f"{method} api.coinbase.com{path}"
    now = int(time.time())
    key = _load_key()

    payload = {
        "sub": API_KEY,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": uri,
        "nonce": secrets.token_hex(16),
    }
    headers = {
        "kid": API_KEY,
        "typ": "JWT",
        "nonce": payload["nonce"],
    }

    # Ed25519 uses EdDSA, EC keys use ES256
    if isinstance(key, Ed25519PrivateKey):
        algorithm = "EdDSA"
    else:
        algorithm = "ES256"

    return jwt.encode(payload, key, algorithm=algorithm, headers=headers)

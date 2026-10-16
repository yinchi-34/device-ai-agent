"""Test identity provider: an RSA key pair, its JWKS, and a token factory."""

import json
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://idp.test/realms/device"
AUDIENCE = "device-control-api"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
KEY_ID = "test-key"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


JWKS = {"keys": [_public_jwk(_private_key, KEY_ID)]}


def make_token(
    *, signed_by_other_key: bool = False, kid: str = KEY_ID, **overrides: Any
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "email": "reviewer@example.com",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    claims = {key: value for key, value in claims.items() if value is not None}
    key = _other_key if signed_by_other_key else _private_key
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def auth_headers(**overrides: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(**overrides)}"}

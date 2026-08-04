"""Test-only HS256 token factory; never used by production code."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any


def make_token(
    secret: str,
    *,
    subject: str,
    department: str,
    service: str,
    scopes: list[str],
    exp: int = 4_102_444_800,
    **extra: Any,
) -> str:
    def encode(value: Any) -> str:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = encode({"alg": "HS256", "typ": "JWT"})
    payload = encode(
        {
            "sub": subject,
            "department": department,
            "service": service,
            "scopes": scopes,
            "exp": exp,
            **extra,
        }
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{header}.{payload}.{encoded_signature}"


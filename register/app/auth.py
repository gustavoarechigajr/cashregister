"""
PIN handling.

A 4-digit PIN is 10 000 possibilities and hashing does not change that. The
real control is lockout after a few failures, enforced in the database. scrypt
is used because it is in the standard library — no dependency to keep patched
on a machine that must boot reliably in March.
"""

import hashlib
import hmac
import os
import base64

_N, _R, _P = 2 ** 14, 8, 1
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pin.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(pin.encode(), salt=salt, n=int(n), r=int(r), p=int(p),
                            dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)

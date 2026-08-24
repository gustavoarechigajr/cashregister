"""
Barcode normalisation and validation.

The catalogue really contains EAN-13, UPC-A and EAN-8, so all three must work.
UPC-A is EAN-13 with an implied leading zero: a US import scanned as 12 digits
has to match the same product stored as 13, or it silently fails to ring up.
"""

import re

DIGITS = re.compile(r"^\d+$")
# GS1 reserves prefixes 20-29 for in-store use, so codes generated here can
# never collide with a manufacturer's.
INTERNAL_PREFIX = "2"


def normalise(code: str) -> str:
    """Canonical identity of a scanned or stored code."""
    code = (code or "").strip()
    if DIGITS.match(code) and len(code) == 12:
        return "0" + code
    return code


def is_internal(code: str) -> bool:
    code = normalise(code)
    return len(code) == 13 and code.startswith(INTERNAL_PREFIX)


def check_digit(payload: str) -> int:
    """
    Mod-10 check digit for the payload (all digits but the last).

    EAN-13 weights the payload 1,3,1,3...; EAN-8 weights it 3,1,3,1...
    """
    if len(payload) == 7:                       # EAN-8 payload
        weights = (3, 1)
    else:                                       # EAN-13 payload
        weights = (1, 3)
    total = sum(int(c) * weights[i % 2] for i, c in enumerate(payload))
    return (10 - total % 10) % 10


def is_valid(code: str) -> bool | None:
    """True/False for checkable symbologies, None for anything else."""
    code = normalise(code)
    if not DIGITS.match(code) or len(code) not in (8, 13):
        return None
    return check_digit(code[:-1]) == int(code[-1])


def make_internal(sequence: int) -> str:
    """
    Build an in-store EAN-13 from a sequence number.

    Extends the 23033119xxxxx series already present in the catalogue rather
    than starting a competing one.
    """
    if not 0 <= sequence <= 99_999:
        raise ValueError("sequence out of range for the 2303311 series")
    payload = f"2303311{sequence:05d}"          # 12 digits
    return payload + str(check_digit(payload))

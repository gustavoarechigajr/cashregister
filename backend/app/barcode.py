"""
In-store barcode generation for the central service.

Deliberately a copy of the register's app/barcode.py rather than an import:
the two run on different machines with no shared package, and this is ~30
lines of arithmetic that has not changed since EAN-13 was standardised. If it
ever does change, both copies must change -- they produce codes that have to
agree, so a divergence would be a real bug.

Ownership note: generation lives HERE, not on the till. The label sheet is
printed on an ordinary printer, and there is no ordinary printer in the store
-- the admin prints from wherever they are, which is this console.
"""

import re

DIGITS = re.compile(r"^\d+$")
INTERNAL_PREFIX = "2303311"          # continues the series already in the catalogue


def normalise(code: str) -> str:
    """UPC-A is EAN-13 with an implied leading zero; store one identity."""
    code = (code or "").strip()
    if DIGITS.match(code) and len(code) == 12:
        return "0" + code
    return code


def check_digit(payload: str) -> int:
    """Mod-10. EAN-13 weights 1,3,1,3…; EAN-8 weights 3,1,3,1…"""
    weights = (3, 1) if len(payload) == 7 else (1, 3)
    total = sum(int(c) * weights[i % 2] for i, c in enumerate(payload))
    return (10 - total % 10) % 10


def is_valid(code: str) -> bool | None:
    """True/False for checkable symbologies, None for anything else."""
    code = normalise(code)
    if not DIGITS.match(code) or len(code) not in (8, 13):
        return None
    return check_digit(code[:-1]) == int(code[-1])


def is_internal(code: str) -> bool:
    code = normalise(code)
    return len(code) == 13 and code.startswith(INTERNAL_PREFIX)


def make_internal(sequence: int) -> str:
    if not 0 <= sequence <= 99_999:
        raise ValueError("sequence out of range for the 2303311 series")
    payload = "%s%05d" % (INTERNAL_PREFIX, sequence)      # 12 digits
    return payload + str(check_digit(payload))


def next_sequence(existing_codes) -> int:
    """
    Highest sequence already used, plus one.

    Derived from the codes themselves rather than a stored counter, so it
    self-heals if a code is added or deleted by hand -- the same choice the
    register makes, and it matters more here because both sides mint codes
    into one series.
    """
    best = 0
    for code in existing_codes:
        if not code or len(code) != 13 or not code.startswith(INTERNAL_PREFIX):
            continue
        seq = code[len(INTERNAL_PREFIX):-1]
        if seq.isdigit():
            best = max(best, int(seq))
    return best + 1

"""Money is integer centavos, everywhere. Floats never touch a total."""

from decimal import Decimal, ROUND_HALF_UP


def to_cents(value) -> int:
    """Parse user or JSON input into centavos without going through binary float."""
    if value is None or value == "":
        return 0
    d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(d * 100)


def format_mxn(cents: int) -> str:
    """'$1,234.50' — the form printed on receipts and shown on screen."""
    neg = cents < 0
    whole, frac = divmod(abs(int(cents)), 100)
    return f"{'−' if neg else ''}${whole:,}.{frac:02d}"


def plain(cents: int) -> str:
    """'1234.50' — no symbol, for inputs and CSV."""
    whole, frac = divmod(abs(int(cents)), 100)
    return f"{'-' if cents < 0 else ''}{whole}.{frac:02d}"

"""
Receipt printing (ESC/POS, 58 mm thermal).

Two rules shape everything here:

1. **Printing must never break a sale.** By the time we print, the sale is
   already committed and the money is already in the drawer. A printer that is
   out of paper, unplugged, or jammed is an inconvenience; a printer that
   raises an exception into the checkout path would lose the sale that was
   just paid for. So every entry point returns (ok, detail) and nothing here
   raises.

2. **The cashier must be told when it failed.** Silently not printing is worse
   than not printing, because nobody notices until a customer asks for a
   ticket. The result is returned to the client, which surfaces it.

Encoding: cheap POS-58 units power up on CP437, which does carry the Spanish
letters this shop needs -- n-tilde, the accented vowels. We select it
explicitly rather than trusting the default, and fall back to '?' for anything
outside it rather than throwing.
"""

import os
from datetime import datetime

from . import devices, money

WIDTH = 32          # characters per line at font A on 58 mm paper
CODEPAGE = "cp437"

ESC = b"\x1b"
GS = b"\x1d"

INIT = ESC + b"@"
SET_CP437 = ESC + b"t\x00"
ALIGN_L = ESC + b"a\x00"
ALIGN_C = ESC + b"a\x01"
BOLD_ON = ESC + b"E\x01"
BOLD_OFF = ESC + b"E\x00"
BIG_ON = GS + b"!\x11"      # double width + height
BIG_OFF = GS + b"!\x00"
CUT = GS + b"V\x42\x00"     # partial cut, feeding first


def _enc(text: str) -> bytes:
    return text.encode(CODEPAGE, errors="replace")


def _local(ts: str, fmt: str) -> str:
    """
    Render a stored UTC timestamp in the till's own zone.

    Timestamps are written as UTC ISO by db.now_iso(). These used to be sliced
    as raw text -- `sold_at[11:16]` and `sold_at[:10]` -- which printed 20:01
    for a sale rung at 14:01, and, for anything sold after 18:00 local,
    TOMORROW's date on the customer's ticket, because UTC had already rolled
    over. Nothing was ever stored wrong; only the paper was.

    Storage stays UTC deliberately. Central converts it correctly on its own
    (`toLocaleString('es-MX')`) and every row already synced is in UTC, so
    re-basing what is stored would break both.

    Never raises. Rule 1 of this module is that nothing here throws into the
    checkout path, so an unparseable timestamp degrades to the old raw slice
    rather than costing a ticket that has already been paid for.
    """
    try:
        return datetime.fromisoformat(ts).astimezone().strftime(fmt)
    except (ValueError, TypeError, AttributeError):
        return (ts or "")[:16].replace("T", " ")


def _row(left: str, right: str, width: int = WIDTH) -> str:
    """One line with `right` flushed to the margin, truncating `left` if needed."""
    room = width - len(right)
    if room < 1:
        return right[:width]
    left = left[:room - 1] if len(left) >= room else left
    return left.ljust(room) + right


def _wrap(text: str, width: int) -> list[str]:
    """Break on spaces where possible; hard-split a single over-long word."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        while len(w) > width:                   # a word longer than the paper
            if cur:
                lines.append(cur); cur = ""
            lines.append(w[:width]); w = w[width:]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _money(cents: int) -> str:
    # format_mxn uses a real minus sign (U+2212), which CP437 does not carry
    # and would print as '?'. Receipts should not show negatives anyway, but
    # a refund ticket would, so normalise rather than rely on that.
    return money.format_mxn(cents).replace("\u2212", "-")


def build_receipt(sale: dict, *, store_name: str, store_line2: str,
                  cashier: str, reprint: bool = False) -> bytes:
    """
    Render one sale as ESC/POS bytes.

    Deliberately plain: no logo, no QR. This is a cash ticket, not a CFDI --
    the project explicitly has no invoicing (see PLAN.md), so there is no
    fiscal content to get wrong.
    """
    out = [INIT, SET_CP437, ALIGN_C, BOLD_ON, BIG_ON, _enc(store_name), b"\n",
           BIG_OFF, BOLD_OFF, _enc(store_line2), b"\n"]

    if reprint:
        out += [b"\n", BOLD_ON, _enc("*** COPIA ***"), BOLD_OFF, b"\n"]

    out += [ALIGN_L, b"\n"]
    out.append(_enc(_row("Ticket #%d" % sale["seq"], _local(sale["sold_at"], "%H:%M"))))
    out.append(b"\n")
    out.append(_enc(_row("Fecha", _local(sale["sold_at"], "%Y-%m-%d"))))
    out.append(b"\n")
    out.append(_enc(_row("Atendio", cashier[:20])))
    out.append(b"\n")
    out.append(_enc("-" * WIDTH))
    out.append(b"\n")

    for l in sale["lines"]:
        # Name on its own line(s), wrapped rather than truncated. Truncating
        # makes "Coca-Cola Taparosca 500ml" and "Coca-Cola Taparosca Sin
        # Azucar 500ml" print identically, and the customer checking their
        # ticket cannot tell which one they were charged for. Paper is cheap.
        for chunk in _wrap(l["name_at_sale"], WIDTH):
            out.append(_enc(chunk))
            out.append(b"\n")
        qty = int(l["qty"])
        detail = "%d x %s" % (qty, _money(l["unit_price_cents"]))
        out.append(_enc(_row("  " + detail, _money(l["line_total_cents"]))))
        out.append(b"\n")

    out.append(_enc("-" * WIDTH))
    out.append(b"\n")
    # Double-width halves the usable columns, so the TOTAL row is laid out
    # against 16 rather than 32 -- otherwise it wraps and the amount lands on
    # its own line, which is exactly the number people check at a glance.
    out += [BOLD_ON, BIG_ON,
            _enc(_row("TOTAL", _money(sale["total_cents"]), WIDTH // 2)),
            b"\n", BIG_OFF, BOLD_OFF]
    out.append(_enc(_row("Efectivo", _money(sale["tendered_cents"]))))
    out.append(b"\n")
    out.append(_enc(_row("Cambio", _money(sale["change_cents"]))))
    out.append(b"\n\n")

    out += [ALIGN_C, _enc("Gracias por su compra"), b"\n",
            _enc("Conserve su ticket"), b"\n\n\n", CUT]
    return b"".join(out)


def build_shift_report(summary: dict, closed: dict, *, store_name: str,
                       store_line2: str, cashier: str, authorized_by: str = "") -> bytes:
    """
    The corte de caja: what the drawer should hold versus what was counted.

    Printed at close because this is the piece that has to survive the till.
    Everything else can be re-derived from the database later, but the physical
    count only exists in someone's head until it is written down, and the paper
    is what goes in the envelope with the cash.
    """
    out = [INIT, SET_CP437, ALIGN_C, BOLD_ON, BIG_ON, _enc("CORTE DE CAJA"), b"\n",
           BIG_OFF, _enc(store_name), b"\n", BOLD_OFF, _enc(store_line2), b"\n",
           ALIGN_L, b"\n"]

    out.append(_enc(_row("Abierto", _local(summary["opened_at"], "%Y-%m-%d %H:%M"))))
    out.append(b"\n")
    out.append(_enc(_row("Cerrado", _local(closed["closed_at"], "%Y-%m-%d %H:%M"))))
    out.append(b"\n")
    out.append(_enc(_row("Cajero", cashier[:20])))
    out.append(b"\n")
    if authorized_by:
        out.append(_enc(_row("Autorizo", authorized_by[:20])))
        out.append(b"\n")
    out.append(_enc("-" * WIDTH)); out.append(b"\n")

    out.append(_enc(_row("Fondo inicial", _money(summary["opening_float_cents"]))))
    out.append(b"\n")
    out.append(_enc(_row("Ventas (%d)" % summary["sales_count"], _money(summary["sales_cents"]))))
    out.append(b"\n")
    if summary.get("refunds_cents"):
        out.append(_enc(_row("Devoluciones", "-" + _money(summary["refunds_cents"]))))
        out.append(b"\n")

    # Each retiro is listed with its envelope number, not just the total: the
    # envelopes are what someone counts against later, and a lump sum makes a
    # missing envelope invisible.
    for d in summary.get("drops", []):
        label = "Retiro sobre %s" % (d.get("envelope_no") or "?")
        out.append(_enc(_row(label, "-" + _money(d["amount_cents"]))))
        out.append(b"\n")

    # Cash added, in its own loop and with a + sign. These must NOT be folded
    # into the drops list above: that loop hard-codes "-", so a float_in
    # printed through it would show as money leaving and the paper would
    # disagree with Esperado by twice the amount.
    for f in summary.get("float_ins", []):
        out.append(_enc(_row("Efectivo agregado", "+" + _money(f["amount_cents"]))))
        out.append(b"\n")

    out.append(_enc("-" * WIDTH)); out.append(b"\n")
    out.append(_enc(_row("Esperado", _money(closed["expected_cents"]))))
    out.append(b"\n")
    out.append(_enc(_row("Contado", _money(closed["counted_cents"]))))
    out.append(b"\n")

    diff = closed["difference_cents"]
    label = "Diferencia" if diff == 0 else ("SOBRA" if diff > 0 else "FALTA")
    out += [BOLD_ON, _enc(_row(label, _money(diff))), b"\n", BOLD_OFF]

    out += [b"\n", ALIGN_L, _enc("Firma: ______________________"), b"\n\n\n", CUT]
    return b"".join(out)


def print_shift_report(summary: dict, closed: dict, **kw) -> tuple[bool, str]:
    try:
        data = build_shift_report(summary, closed, **kw)
    except Exception as e:
        return False, "render_failed: %s" % e
    return write_raw(data)


def write_raw(data: bytes) -> tuple[bool, str]:
    """
    Push bytes at the printer node. Returns (ok, detail); never raises.

    A short write is treated as failure: a half-sent receipt is a jam waiting
    to happen on the next job.
    """
    nodes = devices.printer_nodes()
    if not nodes:
        return False, "no_printer_node"
    try:
        fd = os.open(nodes[0], os.O_WRONLY)
        try:
            written = os.write(fd, data)
        finally:
            os.close(fd)
        if written != len(data):
            return False, "short_write %d/%d" % (written, len(data))
        return True, nodes[0]
    except OSError as e:
        return False, "%s: %s" % (type(e).__name__, e)


def print_receipt(sale: dict, *, store_name: str, store_line2: str,
                  cashier: str, reprint: bool = False) -> tuple[bool, str]:
    try:
        data = build_receipt(sale, store_name=store_name, store_line2=store_line2,
                             cashier=cashier, reprint=reprint)
    except Exception as e:                      # a formatting bug must not eat a sale
        return False, "render_failed: %s" % e
    return write_raw(data)

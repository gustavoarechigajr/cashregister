"""
Peripheral detection.

The header indicators must reflect reality. A till that says "Escáner listo"
when the dongle is unplugged teaches the cashier to ignore the header, and then
the one time it matters — the printer is out of paper at 11am on Domingo de
Ramos — nobody looks.

Detection reads /sys and /dev directly: no lsusb, no shelling out, nothing that
needs a package installed on a machine that must boot reliably in March.
"""

import glob
import os

# Verified on this hardware 2026-08-23. Extra devices can be added without code
# changes via CASHREGISTER_SCANNER_IDS / CASHREGISTER_PRINTER_IDS ("vid:pid,vid:pid").
KNOWN_SCANNERS = {("0581", "011c")}     # Tera 5100 wireless dongle
KNOWN_PRINTERS = {("0483", "070b")}     # POS-58 thermal (STMicro)


def _extra(var):
    out = set()
    for pair in os.environ.get(var, "").split(","):
        pair = pair.strip().lower()
        if ":" in pair:
            v, p = pair.split(":", 1)
            out.add((v.strip(), p.strip()))
    return out


def usb_devices() -> set[tuple[str, str]]:
    """Every (vendor, product) currently on the USB bus, read from sysfs."""
    found = set()
    for d in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            with open(d) as f:
                vid = f.read().strip().lower()
            with open(d.replace("idVendor", "idProduct")) as f:
                pid = f.read().strip().lower()
            found.add((vid, pid))
        except OSError:
            continue
    return found


def printer_nodes() -> list[str]:
    return sorted(glob.glob("/dev/usb/lp*"))


def status() -> dict:
    """
    Report what is actually attached.

    The printer distinguishes 'attached' from 'usable': the kernel node can exist
    while the service still cannot write to it, which is a permissions problem
    that looks identical to a dead printer from the cashier's side.
    """
    usb = usb_devices()
    scanners = KNOWN_SCANNERS | _extra("CASHREGISTER_SCANNER_IDS")
    printers = KNOWN_PRINTERS | _extra("CASHREGISTER_PRINTER_IDS")

    scanner_on = bool(usb & scanners)
    printer_on = bool(usb & printers)
    nodes = printer_nodes()
    writable = next((n for n in nodes if os.access(n, os.W_OK)), None)

    if not printer_on and not nodes:
        printer_state, printer_note = "missing", "No conectada"
    elif writable:
        printer_state, printer_note = "ok", "Lista"
    elif nodes:
        printer_state, printer_note = "blocked", "Sin permiso de escritura"
    else:
        printer_state, printer_note = "blocked", "Conectada, sin dispositivo"

    return {
        "scanner": {
            "state": "ok" if scanner_on else "missing",
            "note": "Listo" if scanner_on else "No conectado",
        },
        "printer": {
            "state": printer_state,
            "note": printer_note,
            "device": writable or (nodes[0] if nodes else None),
        },
    }

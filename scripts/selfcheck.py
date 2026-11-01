"""Smoke-test the vendored toolkit's Python layer without DSH.

Usage (must be a 32-bit Python 3.11+ with pywin32):

    python scripts/selfcheck.py                 # offline checks only
    python scripts/selfcheck.py --live          # also start Multisim + ping COM
    python scripts/selfcheck.py --live --design "C:\\path\\to\\x.ms14"

Why this exists: when a tool call fails inside DSH, it is not obvious whether
the fault is the worker, COM, the GUI window or the design itself. This script
walks the same layers in order and stops at the first broken one.
"""

from __future__ import annotations

import argparse
import importlib
import os
import struct
import sys
import traceback

WORKER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "vendor",
    "dsh-multisim-toolkit",
    "worker",
)

PASS = "  ok   "
FAIL = "  FAIL "
WARN = "  warn "


def check(label, func, *args, **kwargs):
    try:
        value = func(*args, **kwargs)
    except Exception:  # noqa: BLE001 - report and continue
        print(FAIL + label)
        for line in traceback.format_exc().strip().splitlines()[-4:]:
            print("        " + line)
        return None
    detail = f" ({value})" if value is not None else ""
    print(PASS + label + detail)
    return value if value is not None else True


def python_section():
    print("== interpreter")
    bits = struct.calcsize("P") * 8
    print(f"  {sys.executable}")
    print(f"  python {sys.version.split()[0]} / {bits}-bit")
    if bits != 32:
        print(FAIL + "Multisim's Automation server is 32-bit COM; a 64-bit "
              "interpreter cannot Connect(). Use a 32-bit python.exe.")
        return False
    check("pywin32 import", lambda: bool(
        importlib.import_module("win32com.client") and importlib.import_module("pythoncom")) and "ok")
    return True


def module_section():
    print("== worker modules (offline)")
    if not os.path.isdir(WORKER_DIR):
        print(FAIL + f"missing {WORKER_DIR}")
        return False
    sys.path.insert(0, WORKER_DIR)
    import msim_ms14  # noqa: PLC0415
    import msim_raster  # noqa: PLC0415
    import msim_spice  # noqa: PLC0415
    print(PASS + "msim_ms14 / msim_raster / msim_spice import")
    import msim_deck  # noqa: F401, PLC0415
    print(PASS + "msim_deck import")
    decoder = os.path.join(WORKER_DIR, "bin", "blast_harness.exe")
    if os.path.exists(decoder):
        print(PASS + f"ms14 decoder present ({os.path.getsize(decoder)} bytes)")
    else:
        print(WARN + f"ms14 decoder missing: {decoder} (.ms14 reading will fail)")
    return True


def ms14_section(path):
    print("== .ms14 container")
    if not path:
        print("  skip (no --design)")
        return True
    import msim_ms14  # noqa: PLC0415

    def run():
        info = msim_ms14.summary(path)
        comps = info.get("components") or []
        print(f"         {info.get('counts')}")
        print(f"         decoded {info.get('decoded_bytes')} bytes in "
              f"{info.get('blocks')} block(s)")
        for item in comps[:6]:
            print(f"         {item.get('refdes')}: {item.get('value')}")
        return f"{len(comps)} component(s)"

    return check(os.path.basename(path), run) is not None


def live_section(design):
    print("== live (starts a hidden Multisim instance)")
    import msim_com  # noqa: PLC0415

    session = msim_com.MultisimSession()
    try:
        info = check("connect", lambda: session.connect().get("version"))
        if not info:
            return False
        # Connect() alone leaves no circuit open; the design is the unit of work.
        if design:
            check("open_circuit", lambda: session.open_circuit(design).get("name"))
        else:
            print("  skip (no --design: no circuit open, so there are no outputs "
                  "to enumerate yet)")
        outputs = None
        if design:
            outputs = check("enum_outputs",
                            lambda: len(session.enum_outputs(0)["items"]))
            check("enum_components",
                  lambda: len(session.enum_components(0)["items"]))
        if design and not outputs:
            print(WARN + "the current design exposes no simulation output; "
                  "analyses will report '[not found]'. Use msim_spice or add a probe.")
        return True
    finally:
        try:
            session.shutdown()
            print(PASS + "shutdown")
        except Exception:  # noqa: BLE001
            print(WARN + "shutdown raised (Multisim may still be running)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="also start Multisim and talk to its COM server")
    parser.add_argument("--design", default="",
                        help="a .ms14 to read offline and to open when --live")
    args = parser.parse_args(argv)

    print(f"worker dir: {WORKER_DIR}\n")
    ok = python_section()
    if not ok:
        return 2
    module_section()
    ms14_section(args.design)
    if args.live:
        live_section(args.design)
    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(main())

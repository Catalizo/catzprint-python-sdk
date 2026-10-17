"""Print a receipt built from ESC/POS commands.

    CATZPRINT_API_KEY=cz_test_... python examples/print_receipt.py [printer_id]

Without a printer id, the job goes to the best printer with the "receipt" role.
"""

from __future__ import annotations

import sys

from catzprint import CatzPrint, CatzPrintError
from catzprint import commands as c


def main() -> int:
    printer_id = sys.argv[1] if len(sys.argv) > 1 else None
    receipt = [
        c.text("Corner Cafe", align="center", bold=True, size=2),
        c.text("12 Market Street", align="center"),
        c.line(),
        c.columns(("2 x Latte", 0.7), ("7.00", 0.3, "right")),
        c.columns(("1 x Croissant", 0.7), ("3.20", 0.3, "right")),
        c.line(),
        c.columns(("Total", 0.7, None, True), ("10.20", 0.3, "right", True)),
        c.feed(),
        c.qr("https://example.com/r/1042"),
        c.text("Thank you!", align="center"),
        c.cut(),
    ]
    with CatzPrint() as client:
        try:
            job = client.print_receipt(
                receipt,
                printer_id=printer_id,
                role=None if printer_id else "receipt",
                reference="order-1042",
                title="Order 1042",
            )
        except CatzPrintError as e:
            print(f"Couldn't print: {e}", file=sys.stderr)
            return 1
    mode = "live" if job.live else "test (not billed)"
    print(f"Job {job.id} is {job.status} on {job.printer_name} [{mode}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

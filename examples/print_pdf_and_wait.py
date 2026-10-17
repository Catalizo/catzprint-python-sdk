"""Print a PDF (optionally sealed end-to-end) and wait until it has printed.

    CATZPRINT_API_KEY=cz_live_... python examples/print_pdf_and_wait.py invoice.pdf [printer_id] [--sealed]

--sealed encrypts the file to the server key before upload (pip install "catzprint[sealed]").
"""

from __future__ import annotations

import sys

from catzprint import CatzPrint, CatzPrintError, WaitTimeoutError


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    path = args[0]
    printer_id = args[1] if len(args) > 1 else None
    sealed = "--sealed" in argv

    with CatzPrint() as client:
        try:
            job = client.print_pdf(path, printer_id=printer_id, role=None if printer_id else "office", sealed=sealed)
            print(f"Sent job {job.id} ({job.status}); waiting for the printer...")
            done = client.jobs.wait_for(job.id, timeout=120, interval=2)
        except WaitTimeoutError as e:
            print(f"Still not printed: {e}", file=sys.stderr)
            return 1
        except CatzPrintError as e:
            print(f"Couldn't print: {e}", file=sys.stderr)
            return 1
    if done.status == "printed":
        print(f"Printed at {done.printed_at:%H:%M:%S} on {done.printer_name}")
        return 0
    print(f"Job ended as {done.status}: {done.status_detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

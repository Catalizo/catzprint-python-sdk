"""A restaurant hub that keeps printing kitchen tickets when the internet is down.

    CATZPRINT_API_KEY=cz_live_... CATZPRINT_LOCATION_ID=... python examples/local_hub.py 1042

Jobs go to the CatzPrint station on the local network first (local_first) and to the cloud only when
no station can be reached. The token and the station list are kept in a file, so the hub can still
print after a restart while the internet is down.
"""

from __future__ import annotations

import os
import sys

from catzprint import (
    CatzPrint,
    CatzPrintError,
    FileTokenStore,
    LocalConfig,
    StationUnreachableError,
    is_cover_open,
    is_out_of_paper,
    is_printer_offline,
)
from catzprint import commands as c


def main() -> int:
    order_id = sys.argv[1] if len(sys.argv) > 1 else "1042"
    store = os.path.join(os.path.expanduser("~"), ".catzprint", "local-tokens.json")
    client = CatzPrint(
        local=LocalConfig(
            mode="local_first",
            location_id=os.environ["CATZPRINT_LOCATION_ID"],
            discover=True,
            token_store=FileTokenStore(store),
            device_name="CatzServe hub",
        )
    )
    with client:
        try:
            job = client.jobs.create(
                role="kitchen",
                reference=f"order-{order_id}",
                idempotency_key=f"order-{order_id}-kitchen-r1",  # same key on retries: never prints twice
                commands=[
                    c.text(" ALLERGY: PEANUTS ", align="center", bold=True, invert=True, size=2),
                    c.text("Table 12", bold=True, size=2),
                    c.line(),
                    c.columns(c.column("2 x", 0.15), c.column("Pad thai", bold=True)),
                    c.text("   no egg"),
                    c.cut(),
                ],
            )
        except CatzPrintError as e:
            if is_printer_offline(e) or is_out_of_paper(e) or is_cover_open(e):
                print(f"Check the kitchen printer ({e.code}), then retry with the same key.", file=sys.stderr)
            elif isinstance(e, StationUnreachableError):
                print(f"No station and no cloud: tried {e.urls}; cloud: {e.cloud_error}", file=sys.stderr)
            else:
                print(f"Couldn't print: {e}", file=sys.stderr)
            return 1
        print(f"Job {job.id} is {job.status} via {job.via} (live={job.live})")
        if job.via == "local":
            st = client.local.status()
            print(
                f"Station {st.station.name}: {st.unsynced} jobs to sync, {st.allowance.prints_left} offline prints left"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

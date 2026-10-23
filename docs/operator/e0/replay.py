"""E0 item 3: append Thor's capture-manifest hook rows (plus the filter's noise rows) to one
exercise session on exp Forge, in time order, with their original timestamps (all backfill).
Unknown manifest titles are omitted; see README.md for the historical E0 title defect.

Usage: PYTHONPATH=src:. python docs/operator/e0/replay.py <forge api base> <session id> <out.json>
"""

import json
import sys
import urllib.request

from tests.support.artifacts import load, manifest_log_rows

BATCH = 50


def main() -> None:
    base, sid, out = sys.argv[1], sys.argv[2], sys.argv[3]
    manifest = load("capture-manifest-v1.json")
    rows = sorted(
        manifest_log_rows(manifest, "thor"), key=lambda r: (r.ts, str(r.session_id), r.seq)
    )
    items = [
        {"seq": i, "kind": r.kind, "payload": r.payload, "ts": r.ts.isoformat()}
        for i, r in enumerate(rows, start=1)
    ]
    results = []
    for start in range(0, len(items), BATCH):
        body = json.dumps({"entries": items[start : start + BATCH]}).encode()
        req = urllib.request.Request(
            f"{base}/sessions/{sid}/log",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            results.append({"status": resp.status, **json.loads(resp.read())})
    with open(out, "w") as fh:
        json.dump({"rows": len(items), "batches": results}, fh, indent=1)
    print(len(items), [r["status"] for r in results], results[-1].get("latest_seq"))


if __name__ == "__main__":
    main()

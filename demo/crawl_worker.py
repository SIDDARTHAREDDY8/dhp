"""Mad-demo worker: builds a REAL dataset from REAL web fetches.

Each URL takes real network time (1-3s). The worker checkpoints every
URL and ships it over TCP to the peer host. Kill -9 this process at any
point: a standby on the peer host recovers from the shipped log and
finishes with zero rework.

Usage:
  python3 crawl_worker.py <dhp_root> <handoff_id> <worker_id> <ship_host> <ship_port> <out_csv>
"""
from __future__ import annotations

import csv
import html
import re
import sys
import time
import urllib.request

sys.path.insert(0, "/home/hatch/workspace/inventions/bottleneck-hunt")

import dhp
from dhp.net import NetShipper

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")


def fetch(url: str) -> dict:
    t0 = time.time()
    req = urllib.request.Request(
        url, headers={"User-Agent": "dhp-mad-demo/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read(200_000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return {"url": url, "ok": False, "error": str(e)[:80],
                "ms": int((time.time() - t0) * 1000)}
    ms = int((time.time() - t0) * 1000)
    m = TITLE_RE.search(raw)
    title = html.unescape(m.group(1)).strip() if m else "(no title)"
    text = TAG_RE.sub(" ", raw)
    words = len(text.split())
    return {"url": url, "ok": True, "title": title[:80], "words": words,
            "ms": ms}


def main() -> None:
    root, hid, worker_id, ship_host, ship_port, out_csv = sys.argv[1:7]
    store = dhp.Store(root)
    env = store.get_envelope(hid)
    urls = env["task"]["inputs"]["inline"]["urls"]
    shipper = NetShipper(ship_host, int(ship_port),
                         local_logpath=f"{root}/ship-{worker_id}.log")
    shipper.ship_envelope(hid, env)

    # claim (fresh) or recover (standby) — the orchestrator decides
    mode = sys.argv[7] if len(sys.argv) > 7 else "claim"
    if mode == "recover":
        ctx = dhp.recover(store, hid, worker_id)
    else:
        ctx = dhp.claim(store, hid, worker_id)

    with ctx:  # auto-heartbeat on; no manual beats needed
        done = dhp.last_checkpoint(store, hid) or {"idx": -1, "rows": []}
        rows = done["rows"]
        start = done["idx"] + 1
        print(f"[{worker_id}] resuming at url {start}/{len(urls)}",
              flush=True)
        for i in range(start, len(urls)):
            row = fetch(urls[i])
            row["idx"] = i
            rows.append(row)
            state = {"idx": i, "rows": rows}
            seq = ctx.checkpoint(state)
            shipper.ship_checkpoint(hid, seq, state)
            print(f"[{worker_id}] {i + 1}/{len(urls)} "
                  f"{row['url'][:50]} {row.get('ms', 0)}ms", flush=True)
        ctx.complete({"urls_done": len(urls), "csv": out_csv})
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["idx", "url", "ok", "title",
                                              "words", "ms"])
            w.writeheader()
            w.writerows(rows)
        print(f"[{worker_id}] COMPLETE: {len(rows)} rows -> {out_csv}",
              flush=True)
    shipper.close()


if __name__ == "__main__":
    main()

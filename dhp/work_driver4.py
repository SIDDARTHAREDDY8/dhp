"""work_driver4.py — v0.4 driver: ships every checkpoint to the transport log.

Same role as work_driver.py (fast in-process chunk loop, per-chunk
heartbeats), but against Runner3: ownership is CAS, mutations carry the
cached fencing token via TaskContext.

  python3 work_driver3.py --root R --runner ID [--chunk N] [--pace S]
      [--idle-after K --idle-sleep S] [--die-after K] (claim|recover) <hid>
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhp.runner3 import Runner3  # noqa: E402
from dhp import transport as T  # noqa: E402
from dhp.prime_chunk import chunk  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--runner", required=True)
    ap.add_argument("--chunk", type=int, default=50)
    ap.add_argument("--pace", type=float, default=0.0)
    ap.add_argument("--idle-after", type=int, default=0)
    ap.add_argument("--idle-sleep", type=float, default=60.0)
    ap.add_argument("--die-after", type=int, default=0)
    ap.add_argument("--ship", default=None,
                        help="transport log path; every checkpoint is shipped")
    ap.add_argument("mode", choices=["claim", "recover"])
    ap.add_argument("hid")
    a = ap.parse_args()

    r = Runner3(a.root, runner_id=a.runner)
    ctx = r.claim(a.hid) if a.mode == "claim" else r.recover(a.hid)
    if a.ship:
        T.ship_envelope(a.ship, r._load(a.hid))
    env = r._load(a.hid)
    lo = env["task"]["inputs"]["inline"]["lo"]
    hi = env["task"]["inputs"]["inline"]["hi"]

    st = r.last_checkpoint(a.hid)
    if st is None:
        nxt, count, total, done = lo, 0, 0, 0
    else:
        nxt, count, total = st["next_n"], st["count"], st["total"]
        done = 0
    cps = 0
    t0 = time.time()
    while nxt < hi:
        chi = min(nxt + a.chunk, hi)
        c = chunk(nxt, chi)
        count += c["count"]
        total += c["sum"]
        nxt = chi
        done += 1
        seq = ctx.checkpoint({"next_n": nxt, "count": count, "total": total})
        if a.ship:
            T.ship_checkpoint(a.ship, seq, {"next_n": nxt, "count": count, "total": total})
        ctx.note_step()
        ctx.heartbeat()
        cps += 1
        if a.pace:
            time.sleep(a.pace)
        if a.die_after and cps >= a.die_after:
            os._exit(1)
        if a.idle_after and cps >= a.idle_after:
            time.sleep(a.idle_sleep)
            try:
                ctx.checkpoint({"next_n": -1, "count": -1, "total": -1})
                print(json.dumps({"idle_probe": "UNEXPECTEDLY_ACCEPTED"}))
            except Exception as e:
                print(json.dumps({"idle_probe": f"{type(e).__name__}: {e}"}))
            print(json.dumps({"idled": True, "checkpoints_before_idle": cps}))
            return
    ctx.complete({"lo": lo, "hi": hi, "prime_count": count,
                  "prime_sum": total, "chunks_done": done})
    print(json.dumps({"completed": True, "hid": a.hid[:12],
                      "prime_count": count, "prime_sum": total,
                      "chunks_done": done,
                      "elapsed_s": round(time.time() - t0, 1)}))


if __name__ == "__main__":
    main()

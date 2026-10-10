"""work_driver.py — fast in-process worker driver for chaos tests.

Architecture lesson from the first v0.2 attempt: heartbeats must come from
the FAST execution layer, not the slow reasoning layer. An LLM doing
sequential CLI calls has 5-15s action latency — far above a 6s lease TTL —
so the supervisor false-orphans healthy workers. In production the
framework heartbeats while the model reasons; this driver is that
framework layer.

  python3 work_driver.py --root R --runner ID [--chunk N] [--pace S] [--idle-after K --idle-sleep S] [--die-after K] (claim|recover) <hid>

  --pace S:       sleep S seconds per chunk — simulates realistic worker
                  step latency (LLM workers take seconds per action; without
                  pacing the pure-Python loop is too fast to kill mid-run).
  --die-after K:  crash (os._exit) after K checkpoints — no cleanup, no heartbeat.
  --idle-after K: after K checkpoints stop ALL protocol calls, sleep S, then
                   try one checkpoint (expect LeaseLost after reassignment).
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhp.runner import LocalRunner, LeaseLost  # noqa: E402
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
    ap.add_argument("mode", choices=["claim", "recover"])
    ap.add_argument("hid")
    a = ap.parse_args()

    r = LocalRunner(a.root, runner_id=a.runner)
    if a.mode == "claim":
        r.claim(a.hid)
    else:
        r.recover(a.hid)
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
        r.checkpoint(a.hid, {"next_n": nxt, "count": count, "total": total})
        r._bump(a.hid, steps=1)
        r.heartbeat(a.hid)
        cps += 1
        if a.pace:
            time.sleep(a.pace)
        if a.die_after and cps >= a.die_after:
            os._exit(1)  # crash: no cleanup, heartbeats stop dead
        if a.idle_after and cps >= a.idle_after:
            time.sleep(a.idle_sleep)  # idle: alive, silent
            try:
                r.checkpoint(a.hid, {"next_n": -1, "count": -1, "total": -1})
                print(json.dumps({"idle_probe": "UNEXPECTEDLY_ACCEPTED"}))
            except Exception as e:
                print(json.dumps({"idle_probe": f"{type(e).__name__}: {e}"}))
            print(json.dumps({"idled": True, "checkpoints_before_idle": cps}))
            return
    r.complete(a.hid, {"lo": lo, "hi": hi, "prime_count": count,
                       "prime_sum": total, "chunks_done": done})
    print(json.dumps({"completed": True, "hid": a.hid[:12],
                      "prime_count": count, "prime_sum": total,
                      "chunks_done": done,
                      "elapsed_s": round(time.time() - t0, 1)}))


if __name__ == "__main__":
    main()

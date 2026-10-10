"""bridge_worker.py — worker driver for the A2A+DHP demo.

Works an A2A task through the DHP bridge: claims/recovers the task's
DHP handoff, runs the prime-census chunk loop with per-chunk
heartbeats, completes. The A2A task ID is stable; DHP handles worker
swaps underneath.

  python3 bridge_worker.py --root R --task <a2a-task-id> --worker <id>
      [--chunk N] [--pace S] (claim|recover)
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhp.a2a_bridge import DHPBridge  # noqa: E402
from dhp.prime_chunk import chunk  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--worker", required=True)
    ap.add_argument("--chunk", type=int, default=100)
    ap.add_argument("--pace", type=float, default=0.4)
    ap.add_argument("mode", choices=["claim", "recover"])
    a = ap.parse_args()

    b = DHPBridge(a.root, bridge_id="demo-bridge")
    if a.mode == "claim":
        ctx, hid = b.claim_for_task(a.task, a.worker)
    else:
        ctx, hid = b.recover_for_task(a.task, a.worker)
    env = b.runner._load(hid)
    lo = env["task"]["inputs"]["inline"]["lo"]
    hi = env["task"]["inputs"]["inline"]["hi"]

    st = b.runner.last_checkpoint(hid)
    if st is None:
        nxt, count, total, done = lo, 0, 0, 0
    else:
        nxt, count, total = st["next_n"], st["count"], st["total"]
        done = 0
    t0 = time.time()
    while nxt < hi:
        chi = min(nxt + a.chunk, hi)
        c = chunk(nxt, chi)
        count += c["count"]
        total += c["sum"]
        nxt = chi
        done += 1
        ctx.checkpoint({"next_n": nxt, "count": count, "total": total})
        ctx.note_step()
        ctx.heartbeat()
        if a.pace:
            time.sleep(a.pace)
    ctx.complete({"lo": lo, "hi": hi, "prime_count": count,
                  "prime_sum": total, "chunks_done": done})
    print(json.dumps({"completed": True, "task_id": a.task,
                      "worker": a.worker, "prime_count": count,
                      "prime_sum": total, "chunks_done": done,
                      "elapsed_s": round(time.time() - t0, 1),
                      "a2a_state": b.task_state(a.task)}))


if __name__ == "__main__":
    main()

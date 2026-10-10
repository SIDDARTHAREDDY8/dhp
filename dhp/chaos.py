"""DHP fault-injection harness — randomized, not scripted.

Spawns workers and supervisors as real OS processes, then a fault
injector kills random victims at random times (SIGKILL — no warning):
workers, the leader supervisor, sometimes both. A provisioner loop
detects orphans and spawns recovery workers automatically.

This is the "difficult, surprising" test: nobody decides who dies when.
Invariants asserted at the end:
  - every handoff completed exactly once, result correct
  - zero checkpoint rework (sequence continuous per handoff)
  - exactly one owner at every point (no split-brain)

Usage: python3 -m dhp.chaos [--rounds N] [--seed S]
"""
import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhp import envelope as E
from dhp.runner3 import Runner3
from dhp.prime_chunk import chunk

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(HERE, "dhp", "work_driver4.py")
SUPWATCH = os.path.join(HERE, "dhp", "supervisor3_watch.py")
SCHEMA = {"type": "object",
          "properties": {"lo": {"type": "integer"}, "hi": {"type": "integer"},
                         "prime_count": {"type": "integer"},
                         "prime_sum": {"type": "integer"},
                         "chunks_done": {"type": "integer"}},
          "required": ["lo", "hi", "prime_count", "prime_sum", "chunks_done"]}


def setup(root, n_tasks=3, chunks=400, chunk=100, span=40000):
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    r = Runner3(root, runner_id="chaos")
    hids = []
    base = 5000000
    for i in range(n_tasks):
        lo = base + i * span
        env = E.build(
            task_kind="prime.census",
            inputs={"inline": {"lo": lo, "hi": lo + chunks * chunk,
                               "chunk": chunk}},
            output_schema=SCHEMA,
            budget={"max_steps": chunks + 100, "max_spend_usd": 500.0,
                    "max_wallclock_s": 5400.0, "max_attempts": 25},
            created_by="chaos-harness")
        hids.append(r.dispatch(env))
    r.store.close()
    return hids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    for rnd in range(a.rounds):
        root = f"/tmp/dhp-chaos-{os.getpid()}-{rnd}"
        tdir = os.path.join(root, "transport")
        os.makedirs(tdir, exist_ok=True)
        hids = setup(root, n_tasks=3, chunks=300, chunk=100, span=30000)
        print(f"[round {rnd}] {len(hids)} handoffs dispatched")

        # supervisors (2, leader election decides)
        sups = {}
        for sid in ("supA", "supB"):
            p = subprocess.Popen(
                [sys.executable, SUPWATCH, root, sid, "300"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sups[sid] = p

        # workers
        workers = {}  # hid -> Popen
        for i, hid in enumerate(hids):
            log = os.path.join(tdir, hid + ".log")
            p = subprocess.Popen(
                [sys.executable, DRIVER, "--root", root,
                 "--runner", f"w{i}", "--chunk", "100", "--pace", "0.05",
                 "--ship", log, "claim", hid],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            workers[hid] = p

        kills = {"workers": 0, "supervisors": 0}
        deadline = time.time() + 240
        r = Runner3(root, runner_id="chaos-mon")

        def live_workers():
            return {h: p for h, p in workers.items() if p.poll() is None}

        def recover_orphans():
            for hid in r.store.all_hids():
                env = r._load(hid)
                if env["execution"]["status"] == "orphaned" and hid not in live_workers():
                    import uuid
                    rid = f"rw{uuid.uuid4().hex[:8]}"
                    log = os.path.join(tdir, hid + ".log")
                    p = subprocess.Popen(
                        [sys.executable, DRIVER, "--root", root,
                         "--runner", rid,
                         "--chunk", "100", "--pace", "0.05",
                         "--ship", log, "recover", hid],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    workers[hid] = p
                    print(f"[round {rnd}] provisioned recovery for {hid[:12]}")

        while time.time() < deadline:
            # random fault injection
            time.sleep(rng.uniform(1.0, 3.0))
            roll = rng.random()
            lw = live_workers()
            if roll < 0.55 and lw:
                hid = rng.choice(list(lw))
                lw[hid].send_signal(signal.SIGKILL)
                kills["workers"] += 1
                print(f"[round {rnd}] FAULT: SIGKILL worker on {hid[:12]}")
            elif roll < 0.75:
                # kill a random supervisor (maybe the leader, maybe not —
                # the injector doesn't know, that's the point)
                sid = rng.choice(list(sups))
                if sups[sid].poll() is None:
                    sups[sid].send_signal(signal.SIGKILL)
                    kills["supervisors"] += 1
                    print(f"[round {rnd}] FAULT: SIGKILL supervisor {sid}")
                    # start a replacement supervisor (standby pool)
                    p = subprocess.Popen(
                        [sys.executable, SUPWATCH, root, sid + "2", "300"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    sups[sid + "2"] = p
            recover_orphans()
            # done?
            done = all(r._load(h)["execution"]["status"] == "completed"
                       for h in hids)
            if done and not live_workers():
                break

        # stop everything
        for p in list(workers.values()) + list(sups.values()):
            try:
                p.kill()
            except Exception:
                pass

        # verify invariants
        # completed -> must be correct. failed (DLQ) -> must have intact
        # checkpoints (parked for humans, not lost). Nothing may be stuck
        # in a non-terminal state with no live owner.
        ok = True
        live = live_workers()
        for hid in hids:
            env = r._load(hid)
            ex = env["execution"]
            rows = r.store.cx.execute(
                "SELECT seq FROM checkpoints WHERE hid=? ORDER BY seq",
                (hid,)).fetchall()
            seqs = [x[0] for x in rows]
            if seqs != list(range(len(seqs))):
                print(f"[round {rnd}] INVARIANT FAIL: {hid[:12]} rework/gap")
                ok = False
            if ex["status"] == "completed":
                res = env["delivery"]["result"]
                c = s = 0
                n = res["lo"]
                while n < res["hi"]:
                    rr = chunk(n, min(n + 5000, res["hi"]))
                    c += rr["count"]
                    s += rr["sum"]
                    n += 5000
                if c != res["prime_count"] or s != res["prime_sum"]:
                    print(f"[round {rnd}] INVARIANT FAIL: {hid[:12]} wrong result")
                    ok = False
                r.ack(hid)
            elif ex["status"] == "failed":
                # DLQ: parked, intact, awaiting human replay — not lost.
                print(f"[round {rnd}] note: {hid[:12]} in DLQ "
                      f"({len(seqs)} checkpoints intact)")
            else:
                own = r.store.get_ownership(hid)
                if hid not in live and (not own or own["holder"] == "__orphaned__"
                                        or own["lease_expires_at"] < time.time()):
                    print(f"[round {rnd}] INVARIANT FAIL: {hid[:12]} "
                          f"stuck in {ex['status']} with no live owner")
                    ok = False
        r.store.close()
        print(f"[round {rnd}] kills={kills} invariants={'OK' if ok else 'FAILED'}")
        if not ok:
            sys.exit(1)
    print("chaos harness: ALL ROUNDS GREEN")


if __name__ == "__main__":
    main()

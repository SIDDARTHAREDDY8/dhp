"""MAD DEMO: real work, real kill -9, real network, zero rework.

Host A: worker fetches 60 real Wikipedia pages, checkpoints + ships
        every page over TCP to host B.
Mid-run: kill -9 the worker. Supervisor orphans it (~7s).
Host B: ingests the shipped log, standby recovers, finishes.
Result: 60/60 pages, zero refetched.

Run: python3 run_mad_demo.py
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/inventions/bottleneck-hunt")

import dhp
from dhp.net import TransportServer
from dhp import transport as T
from dhp.runner3 import Runner3

BASE = "/tmp/dhp-mad-demo"
HOST_A = f"{BASE}/host-a"
HOST_B = f"{BASE}/host-b"
BLOG = f"{BASE}/b-logs"
PORT = 18491
KILL_AFTER = 20  # kill -9 after this many pages


def main() -> None:
    shutil.rmtree(BASE, ignore_errors=True)
    for d in (HOST_A, HOST_B, BLOG):
        os.makedirs(d, exist_ok=True)
    urls = [l.strip() for l in
            open("/home/hatch/workspace/inventions/bottleneck-hunt/"
                 "demo/urls.txt") if l.strip()]
    print(f"=== MAD DEMO: {len(urls)} real pages, kill -9 at "
          f"{KILL_AFTER} ===", flush=True)

    # host B: transport server (the peer that receives checkpoints)
    srv = TransportServer(BLOG, host="127.0.0.1", port=PORT)
    srv.start_background()
    time.sleep(0.5)

    # host A: store, supervisor, dispatch
    store_a = dhp.Store(HOST_A)
    hid = dhp.dispatch(
        store_a, task_kind="demo.webcrawl", inputs={"urls": urls},
        output_schema={"type": "object",
                       "properties": {"urls_done": {"type": "integer"}},
                       "required": ["urls_done"]})
    print(f"dispatched {hid[:12]}", flush=True)
    sup = subprocess.Popen(
        ["dhp-supervisor", HOST_A, "supA", "600"],
        stdout=open(f"{BASE}/supA.log", "w"),
        stderr=subprocess.STDOUT)
    time.sleep(1.0)

    # host A: worker (real process, will be kill -9'd)
    w = subprocess.Popen(
        [sys.executable,
         "/home/hatch/workspace/inventions/bottleneck-hunt/demo/"
         "crawl_worker.py",
         HOST_A, hid, "worker-A", "127.0.0.1", str(PORT),
         f"{BASE}/dataset.csv", "claim"],
        stdout=open(f"{BASE}/workerA.log", "w"),
        stderr=subprocess.STDOUT)
    print(f"worker-A started (pid {w.pid})", flush=True)

    # wait until KILL_AFTER pages, then kill -9
    t0 = time.time()
    while True:
        n = store_a.checkpoint_count(hid)
        if n >= KILL_AFTER:
            break
        if time.time() - t0 > 300:
            raise RuntimeError("worker too slow / stuck")
        time.sleep(1.0)
    print(f"*** kill -9 worker-A at {n} pages ***", flush=True)
    os.kill(w.pid, signal.SIGKILL)
    w.wait()
    killed_at = n

    # supervisor orphans (~7s)
    t0 = time.time()
    while dhp.status(store_a, hid)["status"] != "orphaned":
        if time.time() - t0 > 30:
            raise RuntimeError("supervisor did not orphan")
        time.sleep(0.5)
    print(f"supervisor orphaned after {time.time() - t0:.1f}s", flush=True)

    # host B: ingest the shipped log (arrived over TCP)
    time.sleep(1.0)
    blog = f"{BLOG}/{hid}.log"
    assert os.path.exists(blog), "no shipped log on host B!"
    store_b = dhp.Store(HOST_B)
    r_b = Runner3(HOST_B, runner_id="ingest", store=store_b)
    out = T.ingest_log(r_b, blog)
    print(f"host B ingested: {out['checkpoints_ingested']} checkpoints, "
          f"{out['lines_skipped']} skipped", flush=True)
    last = dhp.last_checkpoint(store_b, hid)
    assert last["idx"] == killed_at - 1, (last["idx"], killed_at)
    print(f"host B resumes from page {last['idx'] + 1} "
          f"(worker-A died at {killed_at})", flush=True)

    # host B: standby claims the pending handoff (checkpoints already
    # ingested) and finishes
    w2 = subprocess.Popen(
        [sys.executable,
         "/home/hatch/workspace/inventions/bottleneck-hunt/demo/"
         "crawl_worker.py",
         HOST_B, hid, "worker-B", "127.0.0.1", str(PORT),
         f"{BASE}/dataset.csv", "claim"],
        stdout=open(f"{BASE}/workerB.log", "w"),
        stderr=subprocess.STDOUT)
    print(f"worker-B started (pid {w2.pid})", flush=True)
    rc = w2.wait(timeout=600)
    assert rc == 0, f"worker-B failed: {rc}"

    # verify: 60/60, no dupes, CSV real
    st = dhp.status(store_b, hid)
    assert st["status"] == "completed", st
    rows = list(__import__("csv").DictReader(open(f"{BASE}/dataset.csv")))
    assert len(rows) == len(urls), (len(rows), len(urls))
    idxs = sorted(int(r["idx"]) for r in rows)
    assert idxs == list(range(len(urls))), "missing/duplicate pages!"
    ok = sum(1 for r in rows if r["ok"] == "True")
    print(f"=== DONE: {len(rows)}/{len(urls)} pages, {ok} fetched OK, "
          f"zero refetched ===", flush=True)
    print(f"dataset: {BASE}/dataset.csv", flush=True)

    sup.terminate()
    srv.stop()
    print("MAD DEMO: GREEN", flush=True)


if __name__ == "__main__":
    main()

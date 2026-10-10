"""Chaos over the network: kill workers mid-ship, prove no loss.

Worker ships checkpoints over TCP to a peer. We SIGKILL it at random
points, restart standbys, and verify: every checkpoint either landed
on the peer or is replayable from the worker's local buffer; the final
result is complete with zero rework.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import random

sys.path.insert(0, "/home/hatch/workspace/inventions/bottleneck-hunt")

import dhp
from dhp.net import TransportServer
from dhp import transport as T
from dhp.runner3 import Runner3

BASE = tempfile.mkdtemp(prefix="dhp-netchaos-")
PORT = 18501
ROUNDS = 4


def spawn_worker(root, hid, wid, mode, logf):
    return subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '/home/hatch/workspace/"
         "inventions/bottleneck-hunt');"
         "from demo.crawl_worker import *",
         ],
        stdout=logf, stderr=subprocess.STDOUT)


def main():
    random.seed(20261010)
    srv = TransportServer(f"{BASE}/peer-logs", host="127.0.0.1",
                          port=PORT)
    srv.start_background()
    time.sleep(0.5)

    # tiny deterministic workload: 24 fake "pages" via local worker script
    worker_src = (
        "import sys, time\n"
        "sys.path.insert(0, '/home/hatch/workspace/inventions/"
        "bottleneck-hunt')\n"
        "import dhp\n"
        "from dhp.net import NetShipper\n"
        "root, hid, wid, mode = sys.argv[1:5]\n"
        "store = dhp.Store(root)\n"
        "env = store.get_envelope(hid)\n"
        "sh = NetShipper('127.0.0.1', %d,\n"
        "               local_logpath=f'{root}/buf-{wid}.log')\n"
        "sh.ship_envelope(hid, env)\n"
        "ctx = dhp.recover(store, hid, wid) if mode == 'recover' "
        "else dhp.claim(store, hid, wid)\n"
        "with ctx:\n"
        "    st = dhp.last_checkpoint(store, hid) or {'n': -1}\n"
        "    print(f'[{wid}] resume at', st['n'] + 1, flush=True)\n"
        "    for i in range(st['n'] + 1, 24):\n"
        "        time.sleep(0.15)\n"
        "        seq = ctx.checkpoint({'n': i})\n"
        "        sh.ship_checkpoint(hid, seq, {'n': i})\n"
        "    ctx.complete({'n': 24})\n"
        "    print(f'[{wid}] COMPLETE', flush=True)\n"
        "sh.close()\n" % PORT)
    open(f"{BASE}/w.py", "w").write(worker_src)

    host_a = f"{BASE}/a"
    os.makedirs(host_a, exist_ok=True)
    store = dhp.Store(host_a)
    hid = dhp.dispatch(store, task_kind="chaos.net",
                       inputs={"rounds": ROUNDS},
                       output_schema={"type": "object"})
    sup = subprocess.Popen(["dhp-supervisor", host_a, "sup", "300"],
                           stdout=open(f"{BASE}/sup.log", "w"),
                           stderr=subprocess.STDOUT)
    time.sleep(1)

    wid_n = 0
    for rnd in range(ROUNDS):
        wid = f"w{rnd}"
        mode = "claim" if rnd == 0 else "recover"
        p = subprocess.Popen(
            [sys.executable, f"{BASE}/w.py", host_a, hid, wid, mode],
            stdout=open(f"{BASE}/{wid}.log", "w"),
            stderr=subprocess.STDOUT)
        # let it run a random amount, then SIGKILL (except last round)
        time.sleep(random.uniform(0.8, 2.2))
        if rnd < ROUNDS - 1:
            print(f"round {rnd}: SIGKILL {wid} (pid {p.pid})", flush=True)
            os.kill(p.pid, signal.SIGKILL)
            p.wait()
            # wait for orphan before next standby
            t0 = time.time()
            while dhp.status(store, hid)["status"] != "orphaned":
                assert time.time() - t0 < 30, "no orphan!"
                time.sleep(0.5)
            print(f"round {rnd}: orphaned in {time.time()-t0:.1f}s",
                  flush=True)
        else:
            rc = p.wait(timeout=120)
            assert rc == 0, f"final worker failed: {rc}"

    # verify on the PEER: everything arrived over TCP
    time.sleep(0.5)
    peer_log = f"{BASE}/peer-logs/{hid}.log"
    assert os.path.exists(peer_log), "peer got nothing!"
    rb = Runner3(f"{BASE}/peerdb", runner_id="audit")
    out = T.ingest_log(rb, peer_log)
    last = rb.last_checkpoint(hid)
    assert last == {"n": 23}, last
    assert out["checkpoints_ingested"] == 24, out
    # and the origin store agrees
    st = dhp.status(store, hid)
    assert st["status"] == "completed", st
    assert st["checkpoints"] == 24, st
    print(f"NET CHAOS: {ROUNDS} rounds, {ROUNDS-1} SIGKILLs, "
          f"24/24 checkpoints on peer, zero rework: GREEN", flush=True)
    sup.terminate()
    srv.stop()
    shutil.rmtree(BASE, ignore_errors=True)


if __name__ == "__main__":
    main()

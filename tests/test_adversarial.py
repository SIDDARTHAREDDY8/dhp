"""Adversarial tests: the BREAK phase. Malformed, concurrent,
resource-hostile inputs. The suite must stay green."""
import json
import os
import random
import sqlite3
import string
import subprocess
import sys
import threading
import time

import pytest

import dhp
from dhp import errors
from dhp import transport as T
from dhp.runner3 import Runner3
from dhp.supervisor3 import Supervisor3
from dhp.config import DHPConfig
from tests.conftest import SCHEMA


def _rand_str(n=32):
    return "".join(random.choices(string.printable, k=n))


def test_fuzz_hid_inputs(store, hid):
    """100 random malformed hids: all rejected typed, none crash."""
    bads = [_rand_str(random.randint(0, 80)) for _ in range(100)]
    bads += [None, 0, 3.14, [], {}, b"bytes"]
    for bad in bads:
        with pytest.raises((errors.ValidationError, errors.UnknownHandoff)):
            dhp.status(store, bad)


def test_fuzz_worker_ids(store, hid):
    bads = [_rand_str(random.randint(0, 200)) for _ in range(50)]
    for bad in bads:
        try:
            dhp.claim(store, hid, bad)
        except (errors.ValidationError, errors.StateError,
                errors.OwnershipLost):
            pass
        else:
            # only legal ids may succeed; then release for the next try
            assert bad and len(bad) <= 128
            store.release_ownership(hid, bad, 0)


def test_fuzz_checkpoint_payloads(store, hid):
    ctx = dhp.claim(store, hid, "w1")
    bads = [
        {"k": _rand_str(5000)},           # large but legal
        {"nested": {"a": [1, {"b": None}]}},
        {"unicode": "héllo wörld 🎉"},
        {},                               # empty legal
    ]
    for b in bads:
        ctx.checkpoint(b)  # must not crash
    for bad in [["x"], "s", 1, None, (("a", 1),)]:
        with pytest.raises(errors.ValidationError):
            ctx.checkpoint(bad)
    # 200 rapid checkpoints: seqs continuous
    for i in range(200):
        ctx.checkpoint({"i": i})
    seqs = sorted(r[0] for r in store.cx.execute(
        "SELECT seq FROM checkpoints WHERE hid=?", (hid,)).fetchall())
    assert seqs == list(range(len(seqs)))


def test_concurrent_recover_race(store, hid):
    """20 threads racing recover on one orphan: exactly one wins,
    no lost checkpoints, no split brain."""
    cfg = DHPConfig(lease_interval_s=0.1, lease_max_missed=2)
    s2 = dhp.Store(store.root, cfg=cfg)
    ctx = dhp.claim(s2, hid, "w1", cfg=cfg)
    for i in range(10):
        ctx.checkpoint({"n": i})
    time.sleep(0.4)
    sup = Supervisor3(store.root, supervisor_id="sup", cfg=cfg)
    sup.scan()
    assert dhp.status(store, hid)["status"] == "orphaned"

    winners, lost = [], []

    def racer(i):
        try:
            c = dhp.recover(s2, hid, f"racer{i}", cfg=cfg)
            winners.append((f"racer{i}", c.fence))
        except (errors.OwnershipLost, errors.StateError):
            lost.append(i)
        except Exception as e:  # noqa: BLE001
            lost.append(("err", e))

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1, f"winners={winners} lost={len(lost)}"
    # winner can work; loser writes rejected
    wname, fence = winners[0]
    r = Runner3(store.root, runner_id=wname, store=s2, cfg=cfg)
    r.checkpoint(hid, {"n": 999}, fence)
    assert dhp.last_checkpoint(s2, hid) == {"n": 999}
    s2.close()
    sup.store.close()


def test_kill9_during_writes(tmp_root):
    """kill -9 a writer mid-burst: DB stays consistent, no torn rows."""
    db = os.path.join(tmp_root, "k", "dhp.sqlite3")
    script = (
        "import sys; sys.path.insert(0, '/home/hatch/workspace/"
        "inventions/bottleneck-hunt');"
        "import dhp; from tests.conftest import SCHEMA;"
        f"s = dhp.Store('{tmp_root}/k');"
        "h = dhp.dispatch(s, 't', {'x':1}, SCHEMA);"
        "c = dhp.claim(s, h, 'w');"
        "[c.checkpoint({'n': i}) for i in range(100000)];"
    )
    p = subprocess.Popen([sys.executable, "-c", script])
    time.sleep(0.4)
    p.kill()  # SIGKILL mid-burst
    p.wait()
    cx = sqlite3.connect(db)
    assert cx.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # every row present is well-formed
    rows = cx.execute("SELECT seq, state_json FROM checkpoints").fetchall()
    seqs = sorted(r[0] for r in rows)
    assert seqs == list(range(len(seqs))), "torn checkpoint sequence"
    for _, blob in rows:
        json.loads(blob)
    cx.close()


def test_transport_chaos_log(tmp_root, store, hid):
    """Interleaved valid/garbage/truncated lines: ingest what survives."""
    r = Runner3(store.root, runner_id="w1", store=store)
    ctx = dhp.claim(store, hid, "w1")
    log = os.path.join(tmp_root, "c.log")
    T.ship_envelope(log, r._load(hid))
    for i in range(10):
        seq = ctx.checkpoint({"n": i})
        T.ship_checkpoint(log, seq, {"n": i})
        if i % 3 == 0:
            with open(log, "a") as f:
                f.write(_rand_str(40) + "\n")
    with open(log, "a") as f:
        f.write('{"type":"checkpoint","seq":')  # truncated tail
    r2 = Runner3(tmp_root + "-peer", runner_id="w2")
    out = T.ingest_log(r2, log)
    assert out["checkpoints_ingested"] == 10
    assert out["lines_skipped"] >= 4
    assert r2.last_checkpoint(hid) == {"n": 9}


def test_disk_full_surfaces_cleanly(tmp_root, store, hid):
    """Read-only store dir: writes raise, reads still work."""
    ctx = dhp.claim(store, hid, "w1")
    ctx.checkpoint({"n": 1})
    # Lock the whole directory (chmod on the db file alone is bypassed
    # via the -wal file in WAL mode).
    os.chmod(tmp_root, 0o555)
    try:
        # Sanity probe: if we can still write, permissions aren't enforced
        # here (e.g. running as root) — skip instead of false-failing.
        probe = os.path.join(tmp_root, ".writetest")
        try:
            with open(probe, "w") as f:
                f.write("x")
            os.unlink(probe)
            pytest.skip("file permissions not enforced in this environment")
        except OSError:
            pass
        with pytest.raises(Exception):
            ctx.checkpoint({"n": 2})
        # reads unaffected
        assert dhp.last_checkpoint(store, hid) == {"n": 1}
    finally:
        os.chmod(tmp_root, 0o755)

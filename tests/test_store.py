"""Unit tests: thread-safety, CAS races, schema versioning."""
import threading

import pytest

import dhp
from dhp import errors
from dhp.store import Store, SCHEMA_VERSION
from tests.conftest import SCHEMA


def test_concurrent_checkpoints_no_corruption(store, hid):
    """10 threads hammering checkpoints: seqs unique, none lost."""
    ctx = dhp.claim(store, hid, "w1")
    errs = []

    def worker(i):
        try:
            for j in range(20):
                # each thread needs its own runner but same fence is wrong;
                # use the store directly with the lock held
                store.add_checkpoint(hid, {"t": i, "j": j})
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs, errs[:3]
    seqs = sorted(r[0] for r in store.cx.execute(
        "SELECT seq FROM checkpoints WHERE hid=?", (hid,)).fetchall())
    assert seqs == list(range(200)), "lost or duplicated checkpoints"


def test_concurrent_claim_exactly_one_wins(store, hid):
    winners = []
    errs = []

    def worker(i):
        try:
            dhp.claim(store, hid, f"w{i}")
            winners.append(f"w{i}")
        except (errors.OwnershipLost, errors.StateError):
            # lost the CAS race, or saw the winner's "running" first —
            # both are correct rejections
            pass
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs, errs[:3]
    assert len(winners) == 1, f"expected 1 winner, got {winners}"


def test_schema_version_written(store):
    row = store.cx.execute(
        "SELECT version FROM schema_version").fetchone()
    assert row[0] == SCHEMA_VERSION


def test_future_schema_refused(tmp_root):
    import sqlite3
    import time
    s = Store(tmp_root)
    s.cx.execute("UPDATE schema_version SET version=?", (999,))
    s.close()
    with pytest.raises(errors.ValidationError, match="newer"):
        Store(tmp_root)


def test_concurrent_heartbeat_and_checkpoint(store, hid):
    """Interleaved heartbeats + checkpoints from one owner: no errors."""
    ctx = dhp.claim(store, hid, "w1")
    errs = []

    def beat():
        try:
            for _ in range(30):
                ctx.heartbeat()
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    def cps():
        try:
            for i in range(30):
                ctx.checkpoint({"n": i})
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    # note: TaskContext shares one fence; both threads are the same owner
    import threading as th
    ts = [th.Thread(target=beat), th.Thread(target=cps)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, errs[:3]

"""DHP conformance suite — the protocol as runnable assertions.

Any implementation claiming DHP conformance must pass all of these.
They encode the guarantees v0.1–v0.4, each tied to a real failure mode
found in chaos testing.

Run: python3 -m dhp.conform
"""
import shutil
import sys
import tempfile
import threading
import time

from . import envelope as E
from .runner3 import Runner3, RunnerError, BudgetExceeded, FencingError
from .supervisor3 import Supervisor3
from .store import OwnershipLost
from . import transport as T
from . import errors as DHE

SCHEMA = {"type": "object",
          "properties": {"x": {"type": "integer"}},
          "required": ["x"]}
PASS = []
FAIL = []


def check(name, fn):
    root = tempfile.mkdtemp(prefix="dhp-conform-")
    try:
        fn(root)
        PASS.append(name)
        print(f"  PASS {name}")
    except Exception as e:
        FAIL.append((name, e))
        print(f"  FAIL {name}: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def env(n=1, **kw):
    return E.build(task_kind="t", inputs={"inline": {"n": n}},
                   output_schema=SCHEMA, **kw)


# 1. No lost results: kill mid-run -> recover -> result present & correct.
def t_no_lost_results(root):
    r = Runner3(root, runner_id="w")
    hid = r.dispatch(env())
    ctx = r.claim(hid)
    ctx.checkpoint({"s": 1})
    # simulate death: expire lease, supervisor orphans
    r.store.cx.execute("UPDATE ownership SET lease_expires_at=? WHERE hid=?",
                       (time.time() - 1, hid))
    sup = Supervisor3(root, supervisor_id="s")
    sup.ensure_leadership()
    assert sup.scan()[0]["action"] == "orphaned"
    r2 = Runner3(root, runner_id="w2")
    ctx2 = r2.recover(hid)
    assert r2.last_checkpoint(hid) == {"s": 1}
    ctx2.complete({"x": 7})
    assert r2._load(hid)["execution"]["status"] == "completed"


# 2. No crossed inputs: tampered inputs rejected at dispatch.
def t_no_crossed_inputs(root):
    r = Runner3(root, runner_id="w")
    e = env()
    e["task"]["inputs"]["inline"]["n"] = 999  # tamper after id computed
    try:
        r.dispatch(e)
    except (RunnerError, DHE.TamperError):
        return
    raise AssertionError("tampered inputs accepted")


# 3. No untyped results: schema violation rejected at completion.
def t_no_untyped_results(root):
    r = Runner3(root, runner_id="w")
    hid = r.dispatch(env())
    ctx = r.claim(hid)
    try:
        ctx.complete({"wrong": "shape"})
    except Exception:
        return
    raise AssertionError("schema-violating result accepted")


# 4. No silent budget overrun: runtime raises, worker cannot hide it.
def t_no_silent_overrun(root):
    r = Runner3(root, runner_id="w")
    e = E.build(task_kind="t", inputs={"inline": {"n": 1}},
                output_schema=SCHEMA,
                budget={"max_steps": 2, "max_spend_usd": 1.0,
                        "max_wallclock_s": 60.0, "max_attempts": 3})
    hid = r.dispatch(e)
    ctx = r.claim(hid)
    ctx.note_step(2)
    try:
        ctx.note_step(1)
    except BudgetExceeded:
        return
    raise AssertionError("budget overrun not raised")


# 5. No runner lock-in: suspend here, resume there, complete there.
def t_no_runner_lockin(root):
    r1 = Runner3(root + "/a", runner_id="w1")
    hid = r1.dispatch(env())
    ctx = r1.claim(hid)
    ctx.checkpoint({"s": 1})
    bundle = r1.suspend(hid)
    r2 = Runner3(root + "/b", runner_id="w2")
    hid2 = r2.resume(bundle)
    assert hid2 == hid and r2.last_checkpoint(hid2) == {"s": 1}
    r2.claim(hid2).complete({"x": 1})
    assert r2._load(hid2)["execution"]["status"] == "completed"


# 6. Exactly-one-wins: N simultaneous recoverers, one winner.
def t_exactly_one_wins(root):
    r = Runner3(root, runner_id="w")
    hid = r.dispatch(env())
    r.claim(hid)
    r.store.cx.execute("UPDATE ownership SET lease_expires_at=? WHERE hid=?",
                       (time.time() - 1, hid))
    sup = Supervisor3(root, supervisor_id="s")
    sup.ensure_leadership()
    sup.scan()
    winners = []

    def race(i):
        try:
            Runner3(root, runner_id=f"r{i}").recover(hid)
            winners.append(i)
        except Exception:
            pass

    ts = [threading.Thread(target=race, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(winners) == 1, f"{len(winners)} winners"


# 7. Fencing: stale token cannot commit after orphan+recover.
def t_fencing(root):
    r = Runner3(root, runner_id="w")
    hid = r.dispatch(env())
    ctx = r.claim(hid)
    ctx.checkpoint({"s": 1})
    r.store.cx.execute("UPDATE ownership SET lease_expires_at=? WHERE hid=?",
                       (time.time() - 1, hid))
    sup = Supervisor3(root, supervisor_id="s")
    sup.ensure_leadership()
    sup.scan()
    Runner3(root, runner_id="w2").recover(hid)  # new owner, fence bumped
    try:
        ctx.checkpoint({"s": 2})  # stale token
    except FencingError:
        return
    raise AssertionError("stale fence accepted")


# 8. Leader election: exactly one leader; death -> promotion.
def t_leader_election(root):
    Runner3(root, runner_id="x")  # init db
    s1 = Supervisor3(root, supervisor_id="s1")
    s2 = Supervisor3(root, supervisor_id="s2")
    s1.ensure_leadership()
    s2.ensure_leadership()
    assert sum(s.is_leader for s in (s1, s2)) == 1
    dead = "s1" if s1.is_leader else "s2"
    cx = s1.store.cx
    cx.execute("UPDATE leadership SET expires_at=? WHERE holder=?",
               (time.time() - 1, dead))
    (s2 if dead == "s1" else s1).ensure_leadership()
    info = s1.store.get_leadership()
    assert info["holder"] != dead and info["expires_at"] > time.time()


# 9. Transport: ingest reconstructs exact state; idempotent.
def t_transport(root):
    r = Runner3(root + "/a", runner_id="w")
    hid = r.dispatch(env())
    ctx = r.claim(hid)
    log = root + "/t.log"
    T.ship_envelope(log, r._load(hid))
    for i in range(3):
        seq = ctx.checkpoint({"s": i})
        T.ship_checkpoint(log, seq, {"s": i})
    r2 = Runner3(root + "/b", runner_id="w2")
    out = T.ingest_log(r2, log)
    assert out["handoff_id"] == hid, out
    assert r2.last_checkpoint(hid) == {"s": 2}
    T.ingest_log(r2, log)  # re-ingest: no dupes
    n = r2.store.cx.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE hid=?", (hid,)).fetchone()[0]
    assert n == 3, n


# 10. DLQ: max_attempts exhausted -> failed, not silently dropped.
def t_dlq(root):
    r = Runner3(root, runner_id="w")
    e = E.build(task_kind="t", inputs={"inline": {"n": 1}},
                output_schema=SCHEMA,
                budget={"max_steps": 10, "max_spend_usd": 1.0,
                        "max_wallclock_s": 60.0, "max_attempts": 1})
    hid = r.dispatch(e)
    r.claim(hid)
    r.store.cx.execute("UPDATE ownership SET lease_expires_at=? WHERE hid=?",
                       (time.time() - 1, hid))
    sup = Supervisor3(root, supervisor_id="s")
    sup.ensure_leadership()
    assert sup.scan()[0]["action"] == "orphaned"
    Runner3(root, runner_id="w2").recover(hid)  # attempt -> 1
    r.store.cx.execute("UPDATE ownership SET lease_expires_at=? WHERE hid=?",
                       (time.time() - 1, hid))
    acts = sup.scan()
    assert acts and acts[0]["action"] == "dlq", acts
    assert r._load(hid)["execution"]["status"] == "failed"


def main():
    print("DHP conformance suite (v0.4):")
    for name, fn in [
        ("no-lost-results", t_no_lost_results),
        ("no-crossed-inputs", t_no_crossed_inputs),
        ("no-untyped-results", t_no_untyped_results),
        ("no-silent-budget-overrun", t_no_silent_overrun),
        ("no-runner-lock-in", t_no_runner_lockin),
        ("exactly-one-wins", t_exactly_one_wins),
        ("fencing", t_fencing),
        ("leader-election", t_leader_election),
        ("transport", t_transport),
        ("dead-letter-queue", t_dlq),
    ]:
        check(name, fn)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

"""DHP v0.1 dogfood: fan out, kill a worker mid-run, resume elsewhere.

Proves the five guarantees against a real (simulated) worker death:
  1. no lost results      4. no silent budget overrun
  2. no crossed inputs    5. no runner lock-in (portable suspend/resume)
  3. no untyped results
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhp import envelope as E
from dhp.runner import LocalRunner, RunnerError, BudgetExceeded

ROOT = "/tmp/dhp-demo"
ROOT2 = "/tmp/dhp-demo-2"
SCHEMA = {"type": "object",
          "properties": {"total": {"type": "integer"},
                         "chunks_done": {"type": "integer"}},
          "required": ["total", "chunks_done"]}
BUDGET = {"max_steps": 1000, "max_spend_usd": 50.0, "max_wallclock_s": 600.0}


def sum_job(lo, hi):
    return E.build(task_kind="sum.range",
                   inputs={"inline": {"lo": lo, "hi": hi}},
                   output_schema=SCHEMA, budget=BUDGET)


def work_range(ctx, lo, hi, die_after_checkpoints=None):
    """Checkpoint every 25 numbers. Optionally die (simulated kill)."""
    state = ctx.last_checkpoint() or {"next_i": lo, "acc": 0}
    i, acc, done_chunks = state["next_i"], state["acc"], 0
    cps = 0
    while i < hi:
        for _ in range(25):
            if i >= hi:
                break
            acc += i
            i += 1
            ctx.note_step()
        ctx.checkpoint({"next_i": i, "acc": acc})
        done_chunks += 1
        cps += 1
        if die_after_checkpoints and cps >= die_after_checkpoints:
            raise RuntimeError("worker killed mid-run (simulated)")
    return {"total": acc, "chunks_done": done_chunks}


def main():
    for d in (ROOT, ROOT2):
        shutil.rmtree(d, ignore_errors=True)

    r1 = LocalRunner(ROOT, runner_id="runner-A")
    h1 = r1.dispatch(sum_job(0, 100))
    h2 = r1.dispatch(sum_job(100, 200))
    h3 = r1.dispatch(sum_job(200, 300))

    # worker A: h1 fully
    ctx = r1.claim(h1)
    r1.complete(h1, work_range(ctx, 0, 100))
    r1.ack(h1)

    # worker B: h2, DIES after 2 checkpoints (50 of 100 numbers done)
    ctx = r1.claim(h2)
    try:
        work_range(ctx, 100, 200, die_after_checkpoints=2)
        raise AssertionError("worker should have died")
    except RuntimeError as e:
        print(f"  [supervisor] {e} — handoff {h2[:12]} orphaned mid-run")

    # supervisor: fresh runner picks up the same handoff, resumes
    r2 = LocalRunner(ROOT, runner_id="runner-B")  # same root, new runner
    ctx2 = r2.recover(h2)
    resumed_from = ctx2.last_checkpoint()
    assert resumed_from["next_i"] == 150, resumed_from
    assert resumed_from["acc"] == sum(range(100, 150)), resumed_from
    r2.complete(h2, work_range(ctx2, 100, 200))
    r2.ack(h2)
    print(f"  [supervisor] resumed from checkpoint i={resumed_from['next_i']}, "
          f"zero numbers recomputed before it")

    # worker C: h3 fully
    ctx = r1.claim(h3)
    r1.complete(h3, work_range(ctx, 200, 300))
    r1.ack(h3)

    # cross-root portability: suspend a live handoff, resume on another root
    h4 = r1.dispatch(sum_job(300, 400))
    ctx = r1.claim(h4)
    # do one checkpoint manually, then suspend mid-run
    st = {"next_i": 325, "acc": sum(range(300, 325))}
    ctx.checkpoint(st)
    bundle = r1.suspend(h4)
    r3 = LocalRunner(ROOT2, runner_id="runner-C")
    h4b = r3.resume(bundle)
    assert h4b == h4
    # resume() leaves status=running, so hand a context directly to continue
    from dhp.runner import TaskContext
    c4 = TaskContext(r3, h4b)
    assert c4.last_checkpoint() == st, "checkpoint blobs must travel in the bundle"
    r3.complete(h4b, work_range(c4, 300, 400))
    r3.ack(h4b)
    print("  [portability] suspend→bytes→resume on a different root: checkpoints intact")

    # ---- results ----
    assert r1.result(h1)["total"] == sum(range(0, 100)) == 4950
    assert r2.result(h2)["total"] == sum(range(100, 200)) == 14950
    assert r1.result(h3)["total"] == sum(range(200, 300)) == 24950
    assert r3.result(h4)["total"] == sum(range(300, 400)) == 34950
    for r, h in ((r1, h1), (r2, h2), (r1, h3), (r3, h4)):
        env = r._load(h)
        assert env["delivery"]["outbox"]["ack"] is True
    print("  [results] all four totals correct, all outboxes acked")

    # ---- negative tests ----
    bad = sum_job(0, 10)
    bad["task"]["inputs"]["inline"]["lo"] = 999  # tamper after build
    try:
        r1.dispatch(bad)
        raise AssertionError("tampered dispatch should be rejected")
    except RunnerError:
        print("  [negative] tampered inputs rejected at dispatch (no crossed wires)")

    h5 = r1.dispatch(sum_job(0, 10))
    ctx = r1.claim(h5)
    try:
        r1.complete(h5, {"wrong_shape": True})
        raise AssertionError("invalid result should be rejected")
    except E.EnvelopeError:
        print("  [negative] schema-violating result rejected at completion")

    tiny = E.build(task_kind="sum.range", inputs={"inline": {"lo": 0, "hi": 5}},
                   output_schema=SCHEMA,
                   budget={"max_steps": 3, "max_spend_usd": 50.0,
                           "max_wallclock_s": 600.0})
    h6 = r1.dispatch(tiny)
    ctx = r1.claim(h6)
    try:
        for _ in range(5):
            ctx.note_step()
        raise AssertionError("budget should have blown")
    except BudgetExceeded:
        print("  [negative] budget overrun raised by the runtime, not the prompt")

    print("\nDHP v0.1 dogfood: ALL GREEN — worker death lost zero work, "
          "every result typed, every handoff acked.")


if __name__ == "__main__":
    main()

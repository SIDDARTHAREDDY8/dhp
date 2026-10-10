"""Unit tests: lifecycle, validation, fencing, budgets."""
import time

import pytest

import dhp
from dhp import errors
from dhp.runner3 import Runner3
from tests.conftest import SCHEMA


def test_full_lifecycle(store, hid):
    ctx = dhp.claim(store, hid, "w1")
    assert dhp.status(store, hid)["status"] == "running"
    seq = ctx.checkpoint({"n": 1})
    assert seq == 0
    ctx.heartbeat()
    ctx.complete({"n": 42})
    st = dhp.status(store, hid)
    assert st["status"] == "completed"
    assert st["result"] == {"n": 42}


def test_claim_twice_rejected(store, hid):
    dhp.claim(store, hid, "w1")
    with pytest.raises(errors.StateError):
        dhp.claim(store, hid, "w2")


def test_double_complete_idempotent(store, hid):
    r = Runner3(store.root, runner_id="w1", store=store)
    ctx = dhp.claim(store, hid, "w1")
    ctx.complete({"n": 1})
    r.complete(hid, {"n": 1}, ctx.fence)  # again: no error
    assert dhp.status(store, hid)["result"] == {"n": 1}


def test_result_schema_enforced(store, hid):
    ctx = dhp.claim(store, hid, "w1")
    with pytest.raises(errors.ValidationError):
        ctx.complete({"wrong": "shape"})


def test_malformed_hid_everywhere(store):
    for bad in ["xyz", "", "A" * 64, None, 12345]:
        with pytest.raises(errors.ValidationError):
            dhp.status(store, bad)


def test_unknown_handoff_typed(store):
    with pytest.raises(errors.UnknownHandoff):
        dhp.status(store, "0" * 64)


def test_bad_worker_id(store, hid):
    for bad in ["", "has space", "semi;colon", "x" * 200]:
        with pytest.raises(errors.ValidationError):
            dhp.claim(store, hid, bad)
    # None means auto-generate: legal
    ctx = dhp.claim(store, hid, None)
    assert ctx._runner.runner_id.startswith("r3-")


def test_oversize_checkpoint(store, hid, cfg):
    s2 = dhp.Store(store.root + "-x", cfg=cfg)
    h2 = dhp.dispatch(s2, "t", {"x": 1}, SCHEMA, cfg=cfg)
    ctx = dhp.claim(s2, h2, "w1", cfg=cfg)
    with pytest.raises(errors.OversizePayload):
        ctx.checkpoint({"blob": "x" * 20_000})
    s2.close()


def test_oversize_result(store, hid, cfg):
    s2 = dhp.Store(store.root + "-y", cfg=cfg)
    h2 = dhp.dispatch(s2, "t", {"x": 1}, SCHEMA, cfg=cfg)
    ctx = dhp.claim(s2, h2, "w1", cfg=cfg)
    big_schema = {"type": "object"}  # accept anything
    # result over the limit must be rejected before schema validation
    with pytest.raises(errors.OversizePayload):
        ctx._runner.cfg = cfg
        dhp.Runner3(s2.root, runner_id="w1", store=s2,
                    cfg=cfg).complete(h2, {"b": "x" * 200_000},
                                      ctx.fence)
    s2.close()


def test_non_dict_checkpoint_rejected(store, hid):
    ctx = dhp.claim(store, hid, "w1")
    for bad in [["list"], "string", 42, None]:
        with pytest.raises(errors.ValidationError):
            ctx.checkpoint(bad)


def test_fencing_stale_token(store, hid):
    ctx = dhp.claim(store, hid, "w1")
    r = Runner3(store.root, runner_id="w1", store=store)
    with pytest.raises(errors.FencingError):
        r.checkpoint(hid, {"n": 9}, fence=ctx.fence + 99)


def test_budget_steps_enforced(store):
    h = dhp.dispatch(store, "t", {"x": 1}, SCHEMA,
                     budget={"max_steps": 2, "max_spend_usd": 500.0,
                             "max_wallclock_s": 500.0, "max_attempts": 5})
    ctx = dhp.claim(store, h, "w1")
    ctx.note_step(2)
    with pytest.raises(errors.BudgetExceeded):
        ctx.note_step(1)


def test_release_and_reclaim(store, hid):
    ctx = dhp.claim(store, hid, "w1")
    ctx.release("done for now")
    assert dhp.status(store, hid)["status"] == "pending"
    ctx2 = dhp.claim(store, hid, "w2")
    ctx2.complete({"n": 7})
    assert dhp.status(store, hid)["status"] == "completed"


def test_recover_rejects_terminal(store, hid):
    ctx = dhp.claim(store, hid, "w1")
    ctx.complete({"n": 1})
    with pytest.raises(errors.StateError):
        dhp.recover(store, hid, "w2")


def test_recover_rejects_never_claimed(store, hid):
    with pytest.raises(errors.StateError):
        dhp.recover(store, hid, "w2")

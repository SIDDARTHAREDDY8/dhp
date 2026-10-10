"""Unit tests: supervisor orphaning, DLQ, leadership, metrics."""
import time

import pytest

import dhp
from dhp.supervisor3 import Supervisor3
from dhp.config import DHPConfig
from tests.conftest import SCHEMA


@pytest.fixture
def fast_cfg():
    return DHPConfig(lease_interval_s=0.2, lease_max_missed=2,
                     supervisor_scan_s=0.05, leader_ttl_s=1.0)


def test_orphan_after_lease_expiry(store, hid, fast_cfg):
    s2 = dhp.Store(store.root, cfg=fast_cfg)
    ctx = dhp.claim(s2, hid, "w1", cfg=fast_cfg)
    ctx.checkpoint({"n": 1})
    time.sleep(0.6)  # TTL = 0.4s
    sup = Supervisor3(store.root, supervisor_id="sup1", cfg=fast_cfg)
    actions = sup.scan()
    assert len(actions) == 1
    assert actions[0]["action"] == "orphaned"
    assert dhp.status(store, hid)["status"] == "orphaned"
    sup.store.close()
    s2.close()


def test_recover_after_orphan(store, hid, fast_cfg):
    s2 = dhp.Store(store.root, cfg=fast_cfg)
    ctx = dhp.claim(s2, hid, "w1", cfg=fast_cfg)
    ctx.checkpoint({"n": 7})
    time.sleep(0.6)
    sup = Supervisor3(store.root, supervisor_id="sup1", cfg=fast_cfg)
    sup.scan()
    ctx2 = dhp.recover(s2, hid, "w2", cfg=fast_cfg)
    assert dhp.last_checkpoint(s2, hid) == {"n": 7}
    ctx2.complete({"n": 8})
    assert dhp.status(store, hid)["status"] == "completed"
    sup.store.close()
    s2.close()


def test_dlq_after_max_attempts(store, fast_cfg):
    h = dhp.dispatch(store, "t", {"x": 1}, SCHEMA,
                     budget={"max_steps": 100, "max_spend_usd": 500.0,
                             "max_wallclock_s": 500.0, "max_attempts": 1},
                     )
    s2 = dhp.Store(store.root, cfg=fast_cfg)
    for _ in range(2):
        try:
            ctx = dhp.claim(s2, h, "w1", cfg=fast_cfg)
        except Exception:
            ctx = dhp.recover(s2, h, "w1", cfg=fast_cfg)
        time.sleep(0.6)
        sup = Supervisor3(store.root, supervisor_id="sup1", cfg=fast_cfg)
        actions = sup.scan()
        sup.store.close()
    assert dhp.status(store, h)["status"] == "failed"
    s2.close()


def test_leader_election(store, fast_cfg):
    s1 = Supervisor3(store.root, supervisor_id="sA", cfg=fast_cfg)
    s2 = Supervisor3(store.root, supervisor_id="sB", cfg=fast_cfg)
    assert s1.ensure_leadership()
    assert not s2.ensure_leadership()
    assert s1.is_leader and not s2.is_leader
    # leader dies (TTL 1s); follower promotes
    time.sleep(1.2)
    assert s2.ensure_leadership()
    assert s2.is_leader
    s1.store.close()
    s2.store.close()


def test_metrics_counted(store, hid, fast_cfg):
    s2 = dhp.Store(store.root, cfg=fast_cfg)
    ctx = dhp.claim(s2, hid, "w1", cfg=fast_cfg)
    time.sleep(0.6)
    sup = Supervisor3(store.root, supervisor_id="sup1", cfg=fast_cfg)
    sup.scan()
    m = sup.metrics.snapshot()
    assert m["orphans"] == 1
    assert m["scans"] >= 1
    h = sup.health()
    assert h["is_leader"]
    assert h["pending_work"] == 1
    sup.store.close()
    s2.close()


def test_scan_survives_callback_exception(store, hid, fast_cfg):
    s2 = dhp.Store(store.root, cfg=fast_cfg)
    ctx = dhp.claim(s2, hid, "w1", cfg=fast_cfg)
    time.sleep(0.6)

    def boom(h):
        raise RuntimeError("callback exploded")

    sup = Supervisor3(store.root, supervisor_id="sup1", cfg=fast_cfg,
                      on_orphan=boom)
    actions = sup.scan()  # must not raise
    assert actions[0]["action"] == "orphaned"
    assert sup.metrics.snapshot()["errors"] >= 1
    sup.store.close()
    s2.close()

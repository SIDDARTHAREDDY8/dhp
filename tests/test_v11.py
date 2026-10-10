"""Tests for v1.1: network transport, auto-heartbeats, storage backend."""
import os
import shutil
import socket
import tempfile
import time

import pytest

import dhp
from dhp.backend import StoreBackend
from dhp.config import DHPConfig
from dhp.errors import TransportError
from dhp.net import TransportServer, NetShipper
from dhp.runner3 import Runner3
from dhp.store import Store
from dhp import envelope as E
from dhp import transport as T


@pytest.fixture()
def tmp():
    d = tempfile.mkdtemp(prefix="dhp-v11-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_store_implements_backend():
    assert issubclass(Store, StoreBackend)
    # every abstract method is implemented
    for name in dir(StoreBackend):
        fn = getattr(StoreBackend, name, None)
        if getattr(fn, "__isabstractmethod__", False):
            assert callable(getattr(Store, name, None)), name


def test_net_roundtrip(tmp):
    port = _free_port()
    srv = TransportServer(f"{tmp}/srv", host="127.0.0.1", port=port)
    srv.start_background()
    time.sleep(0.3)
    try:
        env = E.build(task_kind="t", inputs={"inline": {}},
                      output_schema={"type": "object"})
        hid = env["handoff_id"]
        sh = NetShipper("127.0.0.1", port,
                        local_logpath=f"{tmp}/client.log")
        sh.ship_envelope(hid, env)
        for i in range(5):
            sh.ship_checkpoint(hid, i, {"i": i})
        sh.close()
        time.sleep(0.3)
        log = f"{tmp}/srv/{hid}.log"
        assert os.path.exists(log)
        r = Runner3(f"{tmp}/db", runner_id="w")
        out = T.ingest_log(r, log)
        assert out["checkpoints_ingested"] == 5
        assert r.last_checkpoint(hid) == {"i": 4}
        # local buffer also has everything (partition tolerance)
        objs = [o for o, e in T.read_log(f"{tmp}/client.log") if not e]
        assert len(objs) == 6
    finally:
        srv.stop()


def test_net_bad_token_rejected(tmp):
    port = _free_port()
    srv = TransportServer(f"{tmp}/srv", host="127.0.0.1", port=port,
                          token="secret")
    srv.start_background()
    time.sleep(0.3)
    try:
        env = E.build(task_kind="t", inputs={"inline": {}},
                      output_schema={"type": "object"})
        sh = NetShipper("127.0.0.1", port, token="wrong")
        with pytest.raises(TransportError):
            sh.ship_envelope(env["handoff_id"], env)
    finally:
        srv.stop()


def test_net_tampered_checkpoint_rejected(tmp):
    port = _free_port()
    srv = TransportServer(f"{tmp}/srv", host="127.0.0.1", port=port)
    srv.start_background()
    time.sleep(0.3)
    try:
        env = E.build(task_kind="t", inputs={"inline": {}},
                      output_schema={"type": "object"})
        hid = env["handoff_id"]
        # raw socket: send a checkpoint with a wrong hash
        import json
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        f = s.makefile("rwb")
        f.write((json.dumps({"type": "hello", "handoff_id": hid,
                              "token": ""}) + "\n").encode())
        f.flush()
        f.readline()
        f.write((json.dumps({"type": "checkpoint", "seq": 0,
                              "state": {"i": 0},
                              "state_hash": "0" * 64,
                              "at": E.utcnow()}) + "\n").encode())
        f.flush()
        resp = json.loads(f.readline().decode())
        assert resp["ok"] is False
        f.close()
        s.close()
    finally:
        srv.stop()


def test_auto_heartbeat_context_manager(tmp):
    cfg = DHPConfig(lease_interval_s=0.4, lease_max_missed=3)
    store = dhp.Store(f"{tmp}/db")
    hid = dhp.dispatch(store, task_kind="t", inputs={},
                       output_schema={"type": "object"}, cfg=cfg)
    with dhp.claim(store, hid, "w1", cfg=cfg) as ctx:
        for i in range(4):
            ctx.checkpoint({"i": i})
            time.sleep(0.7)  # no manual heartbeats; bg thread covers it
        own = store.get_ownership(hid)
        assert own["holder"] == "w1"  # lease never lapsed
        ctx.complete({"done": True})
    assert dhp.status(store, hid)["status"] == "completed"
    # thread stopped
    assert ctx._hb_thread is None


def test_heartbeat_thread_stops_on_release(tmp):
    store = dhp.Store(f"{tmp}/db")
    hid = dhp.dispatch(store, task_kind="t", inputs={},
                       output_schema={"type": "object"})
    ctx = dhp.claim(store, hid, "w1")
    ctx.start_heartbeat(interval_s=0.2)
    assert ctx._hb_thread.is_alive()
    ctx.release("done early")
    assert ctx._hb_thread is None

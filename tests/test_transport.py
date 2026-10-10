"""Unit tests: transport tamper-resistance and fault tolerance."""
import json
import os

import pytest

import dhp
from dhp import errors
from dhp import transport as T
from dhp.runner3 import Runner3
from tests.conftest import SCHEMA


@pytest.fixture
def shipped(tmp_root, store, hid):
    r = Runner3(store.root, runner_id="w1", store=store)
    ctx = dhp.claim(store, hid, "w1")
    log = os.path.join(tmp_root, "t.log")
    env = r._load(hid)
    T.ship_envelope(log, env)
    for i in range(5):
        seq = ctx.checkpoint({"n": i})
        T.ship_checkpoint(log, seq, {"n": i})
    return log, hid, store


def test_ingest_roundtrip(tmp_root, shipped):
    log, hid, store = shipped
    r2 = Runner3(tmp_root + "-b", runner_id="w2")
    out = T.ingest_log(r2, log)
    assert out["handoff_id"] == hid
    assert out["checkpoints_ingested"] == 5
    assert out["lines_skipped"] == 0
    assert r2.last_checkpoint(hid) == {"n": 4}


def test_ingest_idempotent(tmp_root, shipped):
    log, hid, store = shipped
    r2 = Runner3(tmp_root + "-b", runner_id="w2")
    T.ingest_log(r2, log)
    out = T.ingest_log(r2, log)
    assert out["checkpoints_ingested"] == 5  # replays, dedup by seq
    assert r2.store.checkpoint_count(hid) == 5


def test_tampered_envelope_rejected(tmp_root, shipped):
    log, hid, store = shipped
    # rewrite the envelope line with tampered inputs
    with open(log) as f:
        lines = f.readlines()
    env_line = json.loads(lines[0])
    env_line["envelope"]["task"]["inputs"]["inline"]["x"] = 999
    lines[0] = json.dumps(env_line) + "\n"
    with open(log, "w") as f:
        f.writelines(lines)
    r2 = Runner3(tmp_root + "-b", runner_id="w2")
    with pytest.raises(errors.TamperError):
        T.ingest_log(r2, log)


def test_tampered_checkpoint_rejected(tmp_root, shipped):
    log, hid, store = shipped
    with open(log) as f:
        lines = f.readlines()
    cp = json.loads(lines[3])
    cp["state"]["n"] = 999  # tamper, keep old hash
    lines[3] = json.dumps(cp) + "\n"
    with open(log, "w") as f:
        f.writelines(lines)
    r2 = Runner3(tmp_root + "-b", runner_id="w2")
    with pytest.raises(errors.TamperError):
        T.ingest_log(r2, log)


def test_partial_last_line_skipped(tmp_root, shipped):
    """Crash mid-write: truncated final line is skipped, rest ingests."""
    log, hid, store = shipped
    with open(log, "a") as f:
        f.write('{"type": "checkpoint", "seq": 99, "state": {"n":')
    r2 = Runner3(tmp_root + "-b", runner_id="w2")
    out = T.ingest_log(r2, log)
    assert out["checkpoints_ingested"] == 5
    assert out["lines_skipped"] == 1
    assert r2.last_checkpoint(hid) == {"n": 4}


def test_garbage_lines_skipped(tmp_root, shipped):
    log, hid, store = shipped
    with open(log, "a") as f:
        f.write("not json at all\n")
        f.write('{"no": "type field"}\n')
        f.write("\n")
    r2 = Runner3(tmp_root + "-b", runner_id="w2")
    out = T.ingest_log(r2, log)
    assert out["lines_skipped"] == 2
    assert r2.store.checkpoint_count(hid) == 5


def test_no_envelope_header_rejected(tmp_root):
    log = os.path.join(tmp_root, "empty.log")
    with open(log, "w") as f:
        f.write('{"type": "checkpoint", "seq": 0, "state": {}}\n')
    r2 = Runner3(tmp_root + "-b", runner_id="w2")
    with pytest.raises(errors.TransportError):
        T.ingest_log(r2, log)

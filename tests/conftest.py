"""Shared fixtures for the DHP test suite."""
import os
import shutil
import tempfile

import pytest

import dhp
from dhp.config import DHPConfig


@pytest.fixture
def tmp_root():
    d = tempfile.mkdtemp(prefix="dhp-test-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_root):
    s = dhp.Store(tmp_root)
    yield s
    s.close()


@pytest.fixture
def cfg():
    # fast, small limits for tests
    return DHPConfig(
        lease_interval_s=0.2,
        lease_max_missed=2,
        supervisor_scan_s=0.1,
        leader_ttl_s=1.0,
        max_checkpoint_bytes=10_000,
        max_envelope_bytes=1_000_000,
        max_result_bytes=100_000,
    )


SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
}


@pytest.fixture
def hid(store):
    return dhp.dispatch(store, "test.task", {"x": 1}, SCHEMA)

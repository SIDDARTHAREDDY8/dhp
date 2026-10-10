"""DHP v1.0 — SQLite WAL storage backend (production-hardened).

- Thread-safe: a single lock serializes all DB access (SQLite
  connections are not safe for concurrent use).
- Schema versioned: `schema_version` table; startup refuses a DB with
  an unknown future version instead of corrupting it silently.
- All errors are typed (dhp.errors).

The envelope JSON remains the wire format (portability); SQLite is
the local durability substrate. Checkpoints live as rows, materialized
into the envelope on read — one source of truth.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from . import envelope as E
from .backend import StoreBackend
from .config import DHPConfig, DEFAULT
from .errors import (
    OwnershipLost, LeaseLost, UnknownHandoff, ValidationError,
)
from . import log as dhp_log
from . import validate as V

# backward-compat for existing importers
__all__ = ["Store", "SQLiteStore", "OwnershipLost", "LeaseLost",
           "SCHEMA_VERSION"]

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
    hid TEXT PRIMARY KEY,
    envelope_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    hid TEXT NOT NULL,
    seq INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (hid, seq)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_hid ON checkpoints(hid);
CREATE TABLE IF NOT EXISTS ownership (
    hid TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    fence INTEGER NOT NULL,
    lease_expires_at REAL NOT NULL,
    heartbeat_interval_s REAL NOT NULL,
    max_missed INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS leadership (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    holder TEXT NOT NULL,
    expires_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

_logger = dhp_log.get_logger()


class Store(StoreBackend):
    """SQLite WAL backend — the default StoreBackend.

    Subclass note: ``SQLiteStore = Store``; ``Store`` is kept as the
    public name for backward compatibility.
    """
    def __init__(self, root: str, cfg: DHPConfig = DEFAULT):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.path = os.path.join(self.root, "dhp.sqlite3")
        self.cfg = cfg
        self._lock = threading.RLock()
        self.cx = sqlite3.connect(self.path, timeout=30.0,
                                  isolation_level=None,
                                  check_same_thread=False)
        with self._lock:
            self.cx.executescript(SCHEMA)
            self._check_schema_version()

    def _check_schema_version(self) -> None:
        row = self.cx.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            self.cx.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?,?)",
                (SCHEMA_VERSION, time.time()))
            _logger.info("store: initialized schema v%d at %s",
                         SCHEMA_VERSION, self.path)
        elif row[0] > SCHEMA_VERSION:
            raise ValidationError(
                f"store schema v{row[0]} is newer than this dhp "
                f"(v{SCHEMA_VERSION}); upgrade dhp first")
        elif row[0] < SCHEMA_VERSION:
            raise ValidationError(
                f"store schema v{row[0]} is older than v{SCHEMA_VERSION}; "
                f"migration not implemented — export and reimport")

    # ---- handoffs ----
    def create_handoff(self, hid: str, envelope: Dict[str, Any]) -> None:
        V.check_hid(hid)
        env = dict(envelope)
        env["execution"] = dict(env["execution"])
        env["execution"]["checkpoints"] = []  # rows are the source of truth
        blob = json.dumps(env)
        if len(blob.encode()) > self.cfg.max_envelope_bytes:
            from .errors import OversizePayload
            raise OversizePayload("envelope exceeds max_envelope_bytes")
        with self._lock:
            self.cx.execute(
                "INSERT INTO handoffs (hid, envelope_json, updated_at)"
                " VALUES (?,?,?)",
                (hid, blob, time.time()))

    def _materialize(self, hid: str, env: Dict[str, Any]) -> Dict[str, Any]:
        rows = self.cx.execute(
            "SELECT seq, state_hash, at FROM checkpoints WHERE hid=? ORDER BY seq",
            (hid,)).fetchall()
        env["execution"]["checkpoints"] = [
            {"seq": s, "state_hash": h, "at": a,
             "state_uri": f"sqlite:checkpoints/{hid}/{s}"}
            for (s, h, a) in rows]
        return env

    def get_envelope(self, hid: str) -> Dict[str, Any]:
        V.check_hid(hid)
        with self._lock:
            row = self.cx.execute(
                "SELECT envelope_json FROM handoffs WHERE hid=?",
                (hid,)).fetchone()
            if not row:
                raise UnknownHandoff(f"unknown handoff {hid[:12]}")
            return self._materialize(hid, json.loads(row[0]))

    def save_envelope(self, hid: str, envelope: Dict[str, Any]) -> None:
        V.check_hid(hid)
        env = dict(envelope)
        env["execution"] = dict(env["execution"])
        env["execution"]["checkpoints"] = []
        blob = json.dumps(env)
        if len(blob.encode()) > self.cfg.max_envelope_bytes:
            from .errors import OversizePayload
            raise OversizePayload("envelope exceeds max_envelope_bytes")
        with self._lock:
            cur = self.cx.execute(
                "UPDATE handoffs SET envelope_json=?, updated_at=? WHERE hid=?",
                (blob, time.time(), hid))
            if cur.rowcount == 0:
                raise UnknownHandoff(f"unknown handoff {hid[:12]}")

    def all_hids(self) -> List[str]:
        with self._lock:
            return [r[0] for r in
                    self.cx.execute("SELECT hid FROM handoffs ORDER BY hid")]

    def handoff_count(self) -> int:
        with self._lock:
            return self.cx.execute(
                "SELECT COUNT(*) FROM handoffs").fetchone()[0]

    # ---- checkpoints ----
    def add_checkpoint(self, hid: str, state: Dict[str, Any]) -> int:
        V.check_hid(hid)
        V.check_checkpoint_state(state, self.cfg)
        with self._lock:
            n = self.cx.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE hid=?",
                (hid,)).fetchone()[0]
            if n >= self.cfg.max_checkpoints_per_handoff:
                from .errors import OversizePayload
                raise OversizePayload(
                    f"handoff {hid[:12]} exceeded max checkpoints")
            seq = self.cx.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM checkpoints WHERE hid=?",
                (hid,)).fetchone()[0]
            blob = json.dumps(state, sort_keys=True)
            try:
                self.cx.execute(
                    "INSERT INTO checkpoints"
                    " (hid, seq, state_json, state_hash, at)"
                    " VALUES (?,?,?,?,?)",
                    (hid, seq, blob, E.sha256_hex(blob.encode()), E.utcnow()))
            except sqlite3.IntegrityError:
                raise OwnershipLost("checkpoint race: seq already taken")
            return seq

    def last_checkpoint_state(self, hid: str) -> Optional[Dict[str, Any]]:
        V.check_hid(hid)
        with self._lock:
            row = self.cx.execute(
                "SELECT state_json FROM checkpoints WHERE hid=?"
                " ORDER BY seq DESC LIMIT 1",
                (hid,)).fetchone()
            return json.loads(row[0]) if row else None

    def checkpoint_count(self, hid: str) -> int:
        V.check_hid(hid)
        with self._lock:
            return self.cx.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE hid=?",
                (hid,)).fetchone()[0]

    def get_checkpoints(self, hid: str) -> List[Dict[str, Any]]:
        """All checkpoints as [{seq, state}] in order. For suspend/bundle."""
        V.check_hid(hid)
        with self._lock:
            rows = self.cx.execute(
                "SELECT seq, state_json FROM checkpoints WHERE hid=?"
                " ORDER BY seq", (hid,)).fetchall()
            return [{"seq": s, "state": json.loads(st)} for (s, st) in rows]

    def import_checkpoints(self, hid: str,
                           items: List[Dict[str, Any]]) -> int:
        """INSERT OR IGNORE a batch of {seq, state}. Returns count inserted."""
        V.check_hid(hid)
        n = 0
        with self._lock:
            for s in items:
                blob = json.dumps(s["state"], sort_keys=True)
                cur = self.cx.execute(
                    "INSERT OR IGNORE INTO checkpoints"
                    " (hid, seq, state_json, state_hash, at)"
                    " VALUES (?,?,?,?,?)",
                    (hid, s["seq"], blob,
                     E.sha256_hex(blob.encode()), E.utcnow()))
                n += cur.rowcount
        return n

    def force_orphan(self, hid: str, orphan_holder: str) -> bool:
        """Supervisor CAS: move ownership to orphan_holder, fence+1.
        Returns True if this call performed the orphan."""
        V.check_hid(hid)
        with self._lock:
            cur = self.cx.execute(
                "UPDATE ownership SET holder=?, fence=fence+1,"
                " lease_expires_at=0, updated_at=?"
                " WHERE hid=? AND holder != ?",
                (orphan_holder, time.time(), hid, orphan_holder))
            return cur.rowcount > 0

    # ---- ownership (CAS) ----
    def acquire_ownership(self, hid: str, holder: str,
                          interval_s: float, max_missed: int,
                          expected_fence: Optional[int] = None) -> int:
        V.check_hid(hid)
        V.check_worker_id(holder)
        now = time.time()
        expires = now + interval_s * max_missed
        with self._lock:
            if expected_fence is None:
                cur = self.cx.execute(
                    "INSERT OR IGNORE INTO ownership "
                    "(hid, holder, fence, lease_expires_at,"
                    " heartbeat_interval_s, max_missed, updated_at)"
                    " VALUES (?,?,0,?,?,?,?)",
                    (hid, holder, expires, interval_s, max_missed, now))
                if cur.rowcount == 0:
                    raise OwnershipLost(f"claim race lost for {hid[:12]}")
                return 0
            cur = self.cx.execute(
                "UPDATE ownership SET holder=?, fence=fence+1,"
                " lease_expires_at=?, heartbeat_interval_s=?, max_missed=?,"
                " updated_at=? WHERE hid=? AND fence=?",
                (holder, expires, interval_s, max_missed, now, hid,
                 expected_fence))
            if cur.rowcount == 0:
                raise OwnershipLost(f"recover race lost for {hid[:12]}")
            row = self.cx.execute(
                "SELECT fence FROM ownership WHERE hid=?", (hid,)).fetchone()
            return row[0]

    def refresh_lease(self, hid: str, holder: str, fence: int) -> float:
        with self._lock:
            row = self.cx.execute(
                "SELECT fence, holder, heartbeat_interval_s, max_missed"
                " FROM ownership WHERE hid=?", (hid,)).fetchone()
            if not row or row[1] != holder or row[0] != fence:
                raise LeaseLost(
                    "not the current owner (stale fence or holder)")
            now = time.time()
            expires = now + row[2] * row[3]
            self.cx.execute(
                "UPDATE ownership SET lease_expires_at=?, updated_at=?"
                " WHERE hid=? AND fence=?",
                (expires, now, hid, fence))
            return expires

    def release_ownership(self, hid: str, holder: str, fence: int) -> None:
        with self._lock:
            self.cx.execute(
                "DELETE FROM ownership WHERE hid=? AND holder=? AND fence=?",
                (hid, holder, fence))

    def get_ownership(self, hid: str) -> Optional[Dict[str, Any]]:
        V.check_hid(hid)
        with self._lock:
            row = self.cx.execute(
                "SELECT holder, fence, lease_expires_at, heartbeat_interval_s,"
                " max_missed FROM ownership WHERE hid=?", (hid,)).fetchone()
            if not row:
                return None
            return {"holder": row[0], "fence": row[1],
                    "lease_expires_at": row[2],
                    "heartbeat_interval_s": row[3], "max_missed": row[4]}

    def expired_leases(self, now: Optional[float] = None) -> List[str]:
        now = now if now is not None else time.time()
        with self._lock:
            return [r[0] for r in self.cx.execute(
                "SELECT o.hid FROM ownership o JOIN handoffs h ON h.hid=o.hid"
                " WHERE o.lease_expires_at < ?"
                " AND json_extract(h.envelope_json, '$.execution.status')"
                "     ='running'",
                (now,))]

    # ---- leadership (supervisor election) ----
    def acquire_leadership(self, holder: str, ttl_s: float) -> bool:
        V.check_worker_id(holder)
        now = time.time()
        with self._lock:
            cur = self.cx.execute(
                "INSERT OR IGNORE INTO leadership"
                " (id, holder, expires_at, updated_at) VALUES (1,?,?,?)",
                (holder, now + ttl_s, now))
            if cur.rowcount:
                return True
            cur = self.cx.execute(
                "UPDATE leadership SET holder=?, expires_at=?, updated_at=?"
                " WHERE id=1 AND expires_at < ?",
                (holder, now + ttl_s, now, now))
            return cur.rowcount > 0

    def renew_leadership(self, holder: str, ttl_s: float) -> bool:
        now = time.time()
        with self._lock:
            cur = self.cx.execute(
                "UPDATE leadership SET expires_at=?, updated_at=?"
                " WHERE id=1 AND holder=?",
                (now + ttl_s, now, holder))
            return cur.rowcount > 0

    def get_leadership(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.cx.execute(
                "SELECT holder, expires_at FROM leadership WHERE id=1"
            ).fetchone()
            return {"holder": row[0], "expires_at": row[1]} if row else None

    def close(self) -> None:
        with self._lock:
            self.cx.close()

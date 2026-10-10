"""DHP v1.0 — transport: the wire format for portable failover
(production-hardened).

A transport log is an append-only JSON-lines file. The FORMAT is the
protocol — how bytes move doesn't change it.

Log format (<transport>/<hid>.log):
  line 1: {"type": "envelope", "envelope": {...}}
  lines+: {"type": "checkpoint", "seq": N, "state": {...},
           "state_hash": "...", "at": "..."}

Hardening vs v0.4:
- ingest VERIFIES: envelope identity (verify_id) and per-checkpoint
  hashes are recomputed — tampered logs are rejected, not ingested.
- malformed lines (partial write from a crash) are skipped and counted,
  not fatal — ingest reports {ingested, skipped}.
- line size cap (DHP_TRANSPORT_MAX_LINE_BYTES).

Failure semantics (unchanged):
- Worker dies between local commit and ship → peer misses ≤1 checkpoint.
- Host A destroyed → host B ingests the log and resumes. No shared DB.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterator, Optional, Tuple

from . import envelope as E
from .config import DHPConfig, DEFAULT
from .errors import TamperError, TransportError, ValidationError, OversizePayload
from . import log as dhp_log
from . import validate as V

_logger = dhp_log.get_logger()


def _append_line(path: str, obj: Dict[str, Any],
                 cfg: DHPConfig = DEFAULT, fsync: bool = False) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    line = json.dumps(obj, sort_keys=True) + "\n"
    if len(line.encode()) > cfg.transport_max_line_bytes:
        raise OversizePayload("transport line exceeds max line bytes")
    with open(path, "a") as f:
        f.write(line)
        if fsync:
            f.flush()
            os.fsync(f.fileno())


def ship_envelope(logpath: str, envelope: Dict[str, Any],
                  cfg: DHPConfig = DEFAULT) -> None:
    """Write the envelope header. Idempotent: first writer wins."""
    os.makedirs(os.path.dirname(os.path.abspath(logpath)), exist_ok=True)
    if os.path.exists(logpath) and os.path.getsize(logpath) > 0:
        return
    try:
        fd = os.open(logpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as f:
        env = dict(envelope)
        env["execution"] = dict(env["execution"])
        env["execution"]["checkpoints"] = []
        f.write(json.dumps({"type": "envelope", "envelope": env},
                           sort_keys=True) + "\n")


def ship_checkpoint(logpath: str, seq: int, state: Dict[str, Any],
                    cfg: DHPConfig = DEFAULT) -> None:
    V.check_checkpoint_state(state, cfg)
    blob = json.dumps(state, sort_keys=True)
    _append_line(logpath, {"type": "checkpoint", "seq": seq, "state": state,
                           "state_hash": E.sha256_hex(blob.encode()),
                           "at": E.utcnow()}, cfg)


def read_log(logpath: str, cfg: DHPConfig = DEFAULT,
             ) -> Iterator[Tuple[Optional[Dict[str, Any]], Optional[str]]]:
    """Yield (obj, error) per line. Malformed lines yield (None, reason)
    instead of raising — a crash mid-write must not kill the ingest."""
    with open(logpath, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if len(line.encode()) > cfg.transport_max_line_bytes:
                yield None, f"line {lineno}: exceeds max line bytes"
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                yield None, f"line {lineno}: bad JSON ({e})"
                continue
            if not isinstance(obj, dict) or "type" not in obj:
                yield None, f"line {lineno}: not a transport record"
                continue
            yield obj, None


def ingest_log(runner, logpath: str, cfg: DHPConfig = DEFAULT) -> Dict[str, Any]:
    """Replay a transport log into this runner's store.

    Verifies everything: envelope identity, per-checkpoint hashes.
    Malformed lines are skipped and counted. Idempotent (INSERT OR IGNORE).

    Returns {handoff_id, checkpoints_ingested, lines_skipped}.
    Raises TamperError on envelope-identity or hash mismatch.
    """
    hid: Optional[str] = None
    ingested = 0
    skipped = 0
    for obj, err in read_log(logpath, cfg):
        if err:
            skipped += 1
            _logger.warning("transport ingest: %s (skipped)", err)
            continue
        assert obj is not None
        if obj["type"] == "envelope":
            env = obj["envelope"]
            if not E.verify_id(env):
                raise TamperError(
                    "transport log envelope failed identity check")
            hid = V.check_hid(env["handoff_id"])
            try:
                runner._load(hid)
            except Exception:
                env = dict(env)
                env["execution"] = dict(env["execution"])
                env["execution"]["status"] = "pending"
                runner.store.create_handoff(hid, env)
                _logger.info("transport ingest: created handoff %s",
                             hid[:12])
        elif obj["type"] == "checkpoint":
            if hid is None:
                skipped += 1
                _logger.warning(
                    "transport ingest: checkpoint before envelope (skipped)")
                continue
            state = obj.get("state")
            if not isinstance(state, dict):
                skipped += 1
                continue
            blob = json.dumps(state, sort_keys=True)
            if E.sha256_hex(blob.encode()) != obj.get("state_hash"):
                raise TamperError(
                    f"transport log checkpoint {obj.get('seq')}: hash mismatch")
            seq = obj.get("seq")
            if not isinstance(seq, int) or seq < 0:
                skipped += 1
                continue
            runner.store.cx.execute(
                "INSERT OR IGNORE INTO checkpoints"
                " (hid, seq, state_json, state_hash, at) VALUES (?,?,?,?,?)",
                (hid, seq, blob, obj["state_hash"], obj.get("at", "")))
            ingested += 1
        else:
            skipped += 1
    if hid is None:
        raise TransportError("transport log has no envelope header")
    _logger.info("transport ingest: %s ingested=%d skipped=%d", hid[:12],
                 ingested, skipped)
    return {"handoff_id": hid, "checkpoints_ingested": ingested,
            "lines_skipped": skipped}

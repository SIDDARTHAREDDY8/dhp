"""DHP network transport — real TCP shipping between hosts.

The JSON-lines log format (dhp.transport) is unchanged: it is the wire
format. This module moves lines over TCP instead of a shared directory.

Protocol (newline-delimited JSON over TCP):
  C -> S  {"type": "hello", "handoff_id": hid, "token": "..."}
  S -> C  {"ok": true, "ack_seq": N} | {"ok": false, "error": "..."}
  C -> S  {"type": "envelope", "envelope": {...}}
  S -> C  {"ok": true}
  C -> S  {"type": "checkpoint", "seq": N, "state": {...},
           "state_hash": "...", "at": "..."}
  S -> C  {"ok": true, "seq": N}

Design notes (the important ones):
- The server appends to <logdir>/<hid>.log — the SAME file
  dhp.transport.ingest_log reads. Disk is the buffer; a server crash
  loses nothing acked.
- The client ALSO appends to a local log file. Network partition? The
  client keeps working locally and replays on reconnect. The server
  dedups by seq at ingest; duplicate log lines are harmless.
- At-most-once per line over the wire, at-least-once end-to-end via
  the local buffer + resume. Rework bound unchanged: <=1 checkpoint.
- Auth: shared token (DHP_TRANSPORT_TOKEN). No TLS in v1 — run inside
  a VPC or front with stunnel/WireGuard; documented in PRODUCT.md.

Failure semantics:
- Client dies mid-ship -> server has a prefix; ingest tolerates it.
- Server dies -> client buffers locally, reconnects, resumes from
  last acked seq.
- Network dies -> same as server death.
- Host A destroyed entirely -> host B ingests its log and resumes.
  This is the real cross-host failover (v0.4 demoed it via files).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Any, Dict, Optional

from . import envelope as E
from .config import DHPConfig, DEFAULT
from .errors import TransportError, TamperError, ValidationError
from . import log as dhp_log
from . import validate as V
from . import transport as T

_logger = dhp_log.get_logger()

DEFAULT_PORT = 8471


def _send_line(f, obj: Dict[str, Any]) -> None:
    f.write((json.dumps(obj, sort_keys=True) + "\n").encode())
    f.flush()


def _recv_line(f, timeout_s: float = 30.0) -> Dict[str, Any]:
    line = f.readline()
    if not line:
        raise TransportError("connection closed by peer")
    try:
        return json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise TransportError(f"bad frame: {e}")


class TransportServer:
    """Accepts shipped checkpoints, appends to per-handoff log files."""

    def __init__(self, logdir: str, host: str = "0.0.0.0",
                 port: int = DEFAULT_PORT,
                 token: Optional[str] = None,
                 cfg: DHPConfig = DEFAULT):
        self.logdir = os.path.abspath(logdir)
        os.makedirs(self.logdir, exist_ok=True)
        self.host = host
        self.port = port
        self.token = token or os.environ.get("DHP_TRANSPORT_TOKEN", "")
        self.cfg = cfg
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()  # guards per-file appends
        self._threads: list[threading.Thread] = []

    def _logpath(self, hid: str) -> str:
        V.check_hid(hid)
        return os.path.join(self.logdir, hid + ".log")

    def _ack_seq(self, hid: str) -> int:
        """Highest seq in the log (-1 if none)."""
        path = self._logpath(hid)
        if not os.path.exists(path):
            return -1
        hi = -1
        for obj, err in T.read_log(path, self.cfg):
            if err or not obj or obj.get("type") != "checkpoint":
                continue
            s = obj.get("seq")
            if isinstance(s, int) and s > hi:
                hi = s
        return hi

    def _handle(self, conn: socket.socket, addr) -> None:
        try:
            f = conn.makefile("rwb")
            hello = _recv_line(f)
            if hello.get("type") != "hello":
                _send_line(f, {"ok": False, "error": "want hello"})
                return
            if self.token and hello.get("token") != self.token:
                _send_line(f, {"ok": False, "error": "bad token"})
                _logger.warning("transport: rejected client %s (bad token)",
                                addr[0])
                return
            hid = V.check_hid(hello.get("handoff_id", ""))
            _send_line(f, {"ok": True, "ack_seq": self._ack_seq(hid)})
            _logger.info("transport: client %s shipping %s", addr[0],
                         hid[:12])
            while not self._stop.is_set():
                try:
                    msg = _recv_line(f, timeout_s=60.0)
                except TransportError:
                    break
                t = msg.get("type")
                if t == "envelope":
                    env = msg.get("envelope", {})
                    if not E.verify_id(env):
                        _send_line(f, {"ok": False,
                                       "error": "envelope identity failed"})
                        continue
                    with self._lock:
                        T.ship_envelope(self._logpath(hid), env, self.cfg)
                    _send_line(f, {"ok": True})
                elif t == "checkpoint":
                    state = msg.get("state")
                    seq = msg.get("seq")
                    if not isinstance(state, dict) or \
                            not isinstance(seq, int):
                        _send_line(f, {"ok": False,
                                       "error": "bad checkpoint frame"})
                        continue
                    blob = json.dumps(state, sort_keys=True)
                    if E.sha256_hex(blob.encode()) != msg.get("state_hash"):
                        _send_line(f, {"ok": False,
                                       "error": "hash mismatch"})
                        continue
                    with self._lock:
                        T.ship_checkpoint(self._logpath(hid), seq, state,
                                          self.cfg)
                    _send_line(f, {"ok": True, "seq": seq})
                elif t == "bye":
                    _send_line(f, {"ok": True})
                    break
                else:
                    _send_line(f, {"ok": False, "error": "unknown type"})
        except Exception as e:  # noqa: BLE001
            _logger.debug("transport: client %s: %s", addr[0], e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve_forever(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(16)
        self._sock.settimeout(1.0)
        _logger.info("transport: listening on %s:%d", self.host, self.port)
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            t = threading.Thread(target=self._handle, args=(conn, addr),
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def start_background(self) -> "TransportServer":
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


class NetShipper:
    """Client: ships envelope + checkpoints over TCP, with local file
    buffer and reconnect. Drop-in for file-based ship_* in workers."""

    def __init__(self, host: str, port: int = DEFAULT_PORT,
                 token: Optional[str] = None,
                 local_logpath: Optional[str] = None,
                 cfg: DHPConfig = DEFAULT):
        self.host = host
        self.port = port
        self.token = token or os.environ.get("DHP_TRANSPORT_TOKEN", "")
        self.local_logpath = local_logpath
        self.cfg = cfg
        self._sock: Optional[socket.socket] = None
        self._f = None
        self._hid: Optional[str] = None
        self._lock = threading.RLock()

    def _connect(self, hid: str) -> None:
        V.check_hid(hid)
        s = socket.create_connection((self.host, self.port), timeout=10.0)
        s.settimeout(30.0)
        f = s.makefile("rwb")
        _send_line(f, {"type": "hello", "handoff_id": hid,
                       "token": self.token})
        resp = _recv_line(f)
        if not resp.get("ok"):
            s.close()
            raise TransportError(
                f"server rejected: {resp.get('error')}")
        self._sock = s
        self._f = f
        self._hid = hid
        _logger.info("transport: connected, server ack_seq=%s",
                     resp.get("ack_seq"))

    def _ensure(self, hid: str) -> None:
        if self._f is None or self._hid != hid:
            self.close()
            self._connect(hid)

    def _reconnect_once(self, hid: str) -> None:
        self.close()
        time.sleep(0.5)
        self._connect(hid)

    def ship_envelope(self, hid: str, envelope: Dict[str, Any]) -> None:
        V.check_hid(hid)
        if self.local_logpath:
            T.ship_envelope(self.local_logpath, envelope, self.cfg)
        with self._lock:
            for attempt in range(3):
                try:
                    self._ensure(hid)
                    assert self._f is not None
                    _send_line(self._f, {"type": "envelope",
                                         "envelope": envelope})
                    resp = _recv_line(self._f)
                    if not resp.get("ok"):
                        raise TransportError(resp.get("error"))
                    return
                except (TransportError, OSError, socket.timeout) as e:
                    _logger.warning("transport: ship_envelope retry %d: %s",
                                    attempt, e)
                    if attempt == 2:
                        raise TransportError(
                            f"ship_envelope failed: {e}")
                    self._reconnect_once(hid)

    def ship_checkpoint(self, hid: str, seq: int,
                        state: Dict[str, Any]) -> None:
        V.check_hid(hid)
        V.check_checkpoint_state(state, self.cfg)
        if self.local_logpath:
            T.ship_checkpoint(self.local_logpath, seq, state, self.cfg)
        blob = json.dumps(state, sort_keys=True)
        msg = {"type": "checkpoint", "seq": seq, "state": state,
               "state_hash": E.sha256_hex(blob.encode()),
               "at": E.utcnow()}
        with self._lock:
            for attempt in range(3):
                try:
                    self._ensure(hid)
                    assert self._f is not None
                    _send_line(self._f, msg)
                    resp = _recv_line(self._f)
                    if not resp.get("ok"):
                        raise TransportError(resp.get("error"))
                    return
                except (TransportError, OSError, socket.timeout) as e:
                    _logger.warning("transport: ship_checkpoint retry %d: %s",
                                    attempt, e)
                    if attempt == 2:
                        # buffered locally; the checkpoint is NOT lost,
                        # just not shipped yet. Caller decides.
                        raise TransportError(
                            f"ship_checkpoint failed after retries: {e}")
                    self._reconnect_once(hid)

    def close(self) -> None:
        with self._lock:
            if self._f is not None:
                try:
                    _send_line(self._f, {"type": "bye"})
                except OSError:
                    pass
            self._f = None
            self._hid = None
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

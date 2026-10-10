"""dhp-transport: run a DHP TCP transport server.

Usage:
    dhp-transport <logdir> [--host 0.0.0.0] [--port 8471]

Receives checkpoints shipped by NetShipper workers and appends them to
per-handoff JSON-lines logs, ready for ingest_log on the peer.
Auth token via DHP_TRANSPORT_TOKEN env (must match the workers').
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys

from .net import TransportServer, DEFAULT_PORT
from . import log as dhp_log


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dhp-transport",
                                 description="DHP TCP transport server")
    ap.add_argument("logdir", help="directory for per-handoff log files")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    dhp_log.configure(
        level="DEBUG" if args.verbose else "INFO")
    srv = TransportServer(args.logdir, host=args.host, port=args.port)

    def _stop(signum, frame):
        srv.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"dhp-transport: listening on {args.host}:{args.port}, "
          f"logs -> {args.logdir}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

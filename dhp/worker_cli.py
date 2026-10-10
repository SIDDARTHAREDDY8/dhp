"""worker_cli.py — the ONLY way workers touch DHP state.

Usage:
  python3 worker_cli.py --root <dir> --runner <id> claim <hid>
  python3 worker_cli.py --root <dir> --runner <id> recover <hid>
  python3 worker_cli.py --root <dir> --runner <id> last <hid>
  python3 worker_cli.py --root <dir> --runner <id> heartbeat <hid>
  python3 worker_cli.py --root <dir> --runner <id> checkpoint <hid> '<json-state>'
  python3 worker_cli.py --root <dir> --runner <id> note_step <hid> [n]
  python3 worker_cli.py --root <dir> --runner <id> complete <hid> '<json-result>'
  python3 worker_cli.py --root <dir> --runner <id> release <hid> [reason]
  python3 worker_cli.py --root <dir> --runner <id> status <hid>

--runner must be STABLE for the worker's whole session: it is the lease
holder id. A different id (or an expired lease) gets LeaseLost — fencing.

Workers must NEVER edit envelope.json by hand.
"""
import argparse
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhp.runner import LocalRunner, RunnerError, BudgetExceeded, LeaseLost  # noqa: E402
from dhp import envelope as E  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--runner", default=None,
                    help="stable worker id; defaults to a random one (NOT "
                         "stable across calls — pass explicitly in real use)")
    ap.add_argument("cmd", choices=["claim", "recover", "last", "heartbeat",
                                   "checkpoint", "note_step", "complete",
                                   "release", "status"])
    ap.add_argument("hid")
    ap.add_argument("payload", nargs="?")
    a = ap.parse_args()

    runner_id = a.runner or f"cli-{uuid.uuid4().hex[:8]}"
    r = LocalRunner(a.root, runner_id=runner_id)
    try:
        if a.cmd == "claim":
            ctx = r.claim(a.hid)
            env = r._load(a.hid)
            print(json.dumps({"ok": True, "runner": runner_id,
                              "summary": E.summarize(env),
                              "inputs": env["task"]["inputs"],
                              "contract": env["contract"]}))
        elif a.cmd == "recover":
            ctx = r.recover(a.hid)
            env = r._load(a.hid)
            print(json.dumps({"ok": True, "runner": runner_id,
                              "summary": E.summarize(env),
                              "inputs": env["task"]["inputs"]}))
        elif a.cmd == "last":
            print(json.dumps({"ok": True, "state": r.last_checkpoint(a.hid)}))
        elif a.cmd == "heartbeat":
            print(json.dumps(r.heartbeat(a.hid)))
        elif a.cmd == "checkpoint":
            seq = r.checkpoint(a.hid, json.loads(a.payload))
            print(json.dumps({"ok": True, "seq": seq}))
        elif a.cmd == "note_step":
            r._bump(a.hid, steps=int(a.payload or 1))
            print(json.dumps({"ok": True}))
        elif a.cmd == "complete":
            r.complete(a.hid, json.loads(a.payload))
            print(json.dumps({"ok": True, "completed": True}))
        elif a.cmd == "release":
            r.release(a.hid, a.payload or "")
            print(json.dumps({"ok": True, "released": True}))
        elif a.cmd == "status":
            print(json.dumps({"ok": True, "summary": E.summarize(r._load(a.hid))}))
    except (RunnerError, BudgetExceeded, LeaseLost, E.EnvelopeError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()

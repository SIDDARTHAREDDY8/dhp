"""supervisor3_watch.py <root> <supervisor-id> <seconds> — run the
supervisor loop with leader election. Orphans appended to ORPHANS3.log.
Intervals via env: DHP_SUPERVISOR_SCAN_S, DHP_LEADER_TTL_S.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhp.supervisor3 import Supervisor3
from dhp.config import DHPConfig
from dhp import log as dhp_log


def main():
    root, sup_id, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
    dhp_log.configure()
    orphan_log = os.path.join(root, "ORPHANS3.log")

    def on_orphan(hid):
        with open(orphan_log, "a") as f:
            f.write(f"{sup_id} orphaned {hid}\n")

    cfg = DHPConfig()
    s = Supervisor3(root, supervisor_id=sup_id, on_orphan=on_orphan, cfg=cfg)
    acts = s.watch(seconds)
    print(json.dumps({"supervisor": sup_id, "was_leader": s.is_leader,
                      "actions": acts,
                      "metrics": s.metrics.snapshot()}, indent=1))


if __name__ == "__main__":
    main()

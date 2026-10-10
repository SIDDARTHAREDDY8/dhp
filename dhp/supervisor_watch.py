"""supervisor_watch.py <root> <seconds> — run the DHP supervisor loop.

Orphans are appended to <root>/ORPHANS.log as they are found.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhp.supervisor import Supervisor


def main():
    root = sys.argv[1]
    seconds = float(sys.argv[2])
    orphan_log = os.path.join(root, "ORPHANS.log")
    open(orphan_log, "w").write("")

    def on_orphan(hid):
        with open(orphan_log, "a") as f:
            f.write(hid + "\n")

    s = Supervisor(root, on_orphan=on_orphan, scan_interval_s=1.0)
    acts = s.watch(seconds)
    print(json.dumps({"actions": acts, "final": s.summary()}, indent=1))


if __name__ == "__main__":
    main()

"""scan.py — supervisor view of all handoffs."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhp.runner import LocalRunner

ROOT = os.path.expanduser("~/workspace/inventions/bottleneck-hunt/chaos")


def main():
    r = LocalRunner(ROOT, runner_id="supervisor")
    assigns = {a["hid"]: a for a in json.load(open(os.path.join(ROOT, "ASSIGNMENTS.json")))}
    print(f"{'hid':12} {'range':15} {'status':10} att cps steps runner")
    for hid, a in assigns.items():
        try:
            env = r._load(hid)
            ex = env["execution"]
            print(f"{hid[:12]} {a['lo']:>5}-{a['hi']:<8} {ex['status']:10} "
                  f"{ex['attempt']:>3} {len(ex['checkpoints']):>3} "
                  f"{ex.get('steps', 0):>5} {ex.get('runner_id', '-')}")
        except Exception as e:
            print(f"{hid[:12]} ERROR {e}")


if __name__ == "__main__":
    main()

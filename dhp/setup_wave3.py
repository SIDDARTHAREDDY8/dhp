"""Wave 3: 4 big envelopes for the kill test. Kills land ~75s after spawn."""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhp import envelope as E
from dhp.runner import LocalRunner

ROOT = os.path.expanduser("~/workspace/inventions/bottleneck-hunt/chaos")
SCHEMA = {"type": "object",
          "properties": {"lo": {"type": "integer"},
                         "hi": {"type": "integer"},
                         "prime_count": {"type": "integer"},
                         "prime_sum": {"type": "integer"},
                         "chunks_done": {"type": "integer"}},
          "required": ["lo", "hi", "prime_count", "prime_sum", "chunks_done"]}
BUDGET = {"max_steps": 300, "max_spend_usd": 100.0, "max_wallclock_s": 5400.0}
CHUNK = 200


def main():
    r = LocalRunner(ROOT, runner_id="supervisor")
    ranges = [(100000 + i * 9000, 100000 + i * 9000 + 8000) for i in range(4)]
    assignments = []
    for lo, hi in ranges:
        env = E.build(task_kind="prime.census",
                      inputs={"inline": {"lo": lo, "hi": hi, "chunk": CHUNK}},
                      output_schema=SCHEMA, budget=BUDGET,
                      created_by="chaos-supervisor-wave3")
        hid = r.dispatch(env)
        assignments.append({"hid": hid, "lo": lo, "hi": hi})
    with open(os.path.join(ROOT, "ASSIGNMENTS_W3.json"), "w") as f:
        json.dump(assignments, f, indent=1)
    with open(os.path.join(ROOT, "CHAOS_LOG.md"), "a") as f:
        f.write(f"- wave3: dispatched 4 envelopes x 8000 numbers for kill test "
                f"({datetime.now(timezone.utc).isoformat()})\n")
    for a in assignments:
        print(a["hid"][:12], a["lo"], a["hi"])


if __name__ == "__main__":
    main()

"""Create and dispatch the 8 chaos-test envelopes. Writes ASSIGNMENTS.json."""
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
BUDGET = {"max_steps": 200, "max_spend_usd": 50.0, "max_wallclock_s": 1800.0}
CHUNK = 200


def main():
    os.makedirs(ROOT, exist_ok=True)
    r = LocalRunner(ROOT, runner_id="supervisor")
    ranges = [(5000 + i * 1700, 5000 + i * 1700 + 1600) for i in range(8)]
    assignments = []
    for lo, hi in ranges:
        env = E.build(task_kind="prime.census",
                      inputs={"inline": {"lo": lo, "hi": hi, "chunk": CHUNK}},
                      output_schema=SCHEMA, budget=BUDGET,
                      created_by="chaos-supervisor")
        hid = r.dispatch(env)
        assignments.append({"hid": hid, "lo": lo, "hi": hi})
    with open(os.path.join(ROOT, "ASSIGNMENTS.json"), "w") as f:
        json.dump(assignments, f, indent=1)
    with open(os.path.join(ROOT, "CHAOS_LOG.md"), "w") as f:
        f.write("# Chaos log\n\n")
        f.write(f"- t0: dispatched 8 prime.census envelopes "
                f"({datetime.now(timezone.utc).isoformat()})\n")
    for a in assignments:
        print(a["hid"][:12], a["lo"], a["hi"])
    print("dispatched 8 envelopes to", ROOT)


if __name__ == "__main__":
    main()

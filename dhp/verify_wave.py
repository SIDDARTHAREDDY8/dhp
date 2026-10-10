"""verify_wave.py <assignments.json> — independent verification of a wave.

Checks per envelope: completed + acked, result matches an independent
sieve, result lo/hi match envelope inputs (no crossed wires), schema
validity re-checked, and reports attempts/checkpoints/steps.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhp import envelope as E
from dhp.runner import LocalRunner


def sieve_sum_count(lo, hi):
    sieve = bytearray(b"\x01") * hi
    sieve[0:2] = b"\x00\x00"
    for i in range(2, int(math.isqrt(hi)) + 1):
        if sieve[i]:
            sieve[i * i:hi:i] = b"\x00" * ((hi - 1 - i * i) // i + 1)
    ps = [n for n in range(max(lo, 2), hi) if sieve[n]]
    return len(ps), sum(ps)


def main():
    ap = sys.argv[1]
    root = os.path.dirname(os.path.abspath(ap))
    r = LocalRunner(root, runner_id="verifier")
    assigns = json.load(open(ap))
    ok = True
    for a in assigns:
        hid = a["hid"]
        env = r._load(hid)
        ex = env["execution"]
        tag = hid[:12]
        problems = []
        if ex["status"] != "completed":
            problems.append(f"status={ex['status']}")
        if not env["delivery"]["outbox"]["ack"]:
            problems.append("not acked")
        res = r.result(hid)
        try:
            E.validate_result(env, res)
        except E.EnvelopeError as e:
            problems.append(f"schema: {e}")
        if (res["lo"], res["hi"]) != (a["lo"], a["hi"]):
            problems.append("CROSSED INPUTS: result range != assignment")
        exp_c, exp_s = sieve_sum_count(a["lo"], a["hi"])
        if res["prime_count"] != exp_c or res["prime_sum"] != exp_s:
            problems.append(f"WRONG: got ({res['prime_count']},{res['prime_sum']}) "
                            f"want ({exp_c},{exp_s})")
        n_in = (a["hi"] - a["lo"]) // a.get("chunk", 200)
        status = "OK " if not problems else "FAIL"
        if problems:
            ok = False
        print(f"[{status}] {tag} [{a['lo']},{a['hi']}) "
              f"attempt={ex['attempt']} cps={len(ex['checkpoints'])} "
              f"steps={ex.get('steps', 0)} " + (" ".join(problems)))
    print("WAVE VERIFIED: all correct" if ok else "WAVE HAS FAILURES")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

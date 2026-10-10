#!/usr/bin/env bash
# Daily DHP commit loop: verify -> commit -> push.
# Run from cron. Exits 0 even on test failure (the RED report is the signal).
set -uo pipefail
ROOT="/home/hatch/workspace/inventions/bottleneck-hunt"
cd "$ROOT" || exit 1

DATE=$(date +%F)
python3 scripts/daily_verify.py
STATUS=$?

git add "daily/${DATE}.md" scripts/daily_verify.py 2>/dev/null
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi

if [ "$STATUS" -eq 0 ]; then
  MSG="chore: daily verification ${DATE} — green (45 tests, 10/10 conformance)"
else
  MSG="chore: daily verification ${DATE} — RED, investigate"
fi
git -c user.email="dhp@localhost" -c user.name="DHP" commit -qm "$MSG"

# push via GitHub API (same method as the launch pushes)
python3 - "$MSG" <<'PYEOF' >>/tmp/dhp-daily-push.log 2>&1
import sys, json, base64, subprocess
sys.path.insert(0, '/opt/hatch/skills/skill-creator/bin')
from dynamic_credentials import add_surrogate_to_request, read_json_response
import urllib.request
msg = sys.argv[1]
OWNER, REPO, API = "SIDDARTHAREDDY8", "dhp", "https://api.github.com"
ROOT = "/home/hatch/workspace/inventions/bottleneck-hunt"
def api(method, path, data=None):
    req = urllib.request.Request(API + path, method=method)
    add_surrogate_to_request(req, "custom.github", allowed_hosts=["api.github.com"])
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=60) as resp:
        return read_json_response(resp)
changed = subprocess.run(["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                         cwd=ROOT, capture_output=True, text=True).stdout.split()
ref = api("GET", f"/repos/{OWNER}/{REPO}/git/refs/heads/main")
parent = ref["object"]["sha"]
base_tree = api("GET", f"/repos/{OWNER}/{REPO}/git/commits/{parent}")["tree"]["sha"]
items = []
for f in changed:
    raw = open(f"{ROOT}/{f}", "rb").read()
    b = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
            {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
    items.append({"path": f, "mode": "100644", "type": "blob", "sha": b["sha"]})
tree = api("POST", f"/repos/{OWNER}/{REPO}/git/trees",
           {"base_tree": base_tree, "tree": items})
commit = api("POST", f"/repos/{OWNER}/{REPO}/git/commits",
             {"message": msg, "tree": tree["sha"], "parents": [parent]})
api("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/main", {"sha": commit["sha"]})
print(f"{msg} -> pushed {commit['sha'][:8]}", flush=True)
PYEOF
echo "daily loop done: $MSG"

"""Print budget and spend of the dev key (never the key itself).

  python scripts/keyinfo.py            reads dev.env, else secrets.local.env
"""

import json
import sys
import urllib.request

from _common import DEV_ENV, SECRETS, read_env_file


def main():
    s = read_env_file(DEV_ENV) or read_env_file(SECRETS)
    key, base = s.get("LLM_API_KEY"), (s.get("LLM_BASE_URL") or "").rstrip("/")
    if not key or not base:
        sys.exit("LLM_API_KEY / LLM_BASE_URL missing in dev.env and secrets.local.env")
    root = base[:-3] if base.endswith("/v1") else base
    req = urllib.request.Request(root + "/key/info", headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=20) as r:
        info = json.load(r).get("info", {})
    budget = info.get("max_budget")
    spend = info.get("spend") or 0.0
    print("budget USD : %s" % budget)
    print("spend USD  : %.4f" % spend)
    if budget:
        print("left USD   : %.4f" % (budget - spend))
    print("rpm limit  : %s" % info.get("rpm_limit"))
    print("expires    : %s" % info.get("expires"))


if __name__ == "__main__":
    main()

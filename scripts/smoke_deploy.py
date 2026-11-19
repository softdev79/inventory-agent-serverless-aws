"""End-to-end check of the deployed app through CloudFront. Stdlib only.

    python scripts/smoke_deploy.py https://dxxxx.cloudfront.net
Reads API_KEY from the environment or .env.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request


def api_key() -> str:
    if os.getenv("API_KEY"):
        return os.environ["API_KEY"]
    env = os.path.join(os.path.dirname(__file__), "..", ".env")
    for line in open(env, encoding="utf-8"):
        if line.startswith("API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    sys.exit("API_KEY not found")


def call(base, path, body=None, key=None, retries=1):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-API-Key": key} if key else {}
    if data is not None:
        headers.update({"Content-Type": "application/json",
                        "x-amz-content-sha256": hashlib.sha256(data).hexdigest()})
    for attempt in range(retries):
        req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode()
            if attempt == retries - 1 or status not in (403, 404, 502, 503):
                return status, text
            time.sleep(15)  # CloudFront / permissions still propagating
    return status, text


base = sys.argv[1].rstrip("/")
key = api_key()
ok = True

s, t = call(base, "/", retries=12)
print(f"GET  /                  {s}  {'UI ok' if 'Inventory Planning Agent' in t else t[:120]}")
ok &= s == 200
s, t = call(base, "/api/health", retries=12)
print(f"GET  /api/health        {s}  {t[:120]}")
ok &= s == 200
s, _ = call(base, "/api/inventory/SKU002", key="wrong")
print(f"GET  /api/inventory bad {s}  (expect 403)")
ok &= s == 403
s, t = call(base, "/api/inventory/SKU002", key=key)
print(f"GET  /api/inventory     {s}  {t[:120]}")
ok &= s == 200
t0 = time.time()
s, t = call(base, "/api/chat", {"query": "Should I reorder SKU002? How many units?"}, key=key, retries=3)
print(f"POST /api/chat          {s}  ({time.time() - t0:.1f}s)")
print("     " + (json.loads(t)["response"] if s == 200 else t[:400]).replace("\n", "\n     "))
ok &= s == 200 and "55" in t
print("\nSMOKE TEST PASSED" if ok else "\nSMOKE TEST FAILED")
sys.exit(0 if ok else 1)

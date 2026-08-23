"""Live probe: codex app-server native skills RPCs (skills/list,
skills/config/write, skills/extraRoots/set shape discovery).
Read-only first; config/write probed idempotently (write back the same value).
"""
import json, subprocess, sys, time
proc = subprocess.Popen(
    ["/Users/lianb/.local/bin/codex", "app-server", "--stdio"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)

_next_id = [0]
def call(method, params=None, timeout=30):
    _next_id[0] += 1
    rid = _next_id[0]
    req = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            return {"__error": "eof"}
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == rid:
            return msg
    return {"__error": "timeout"}

init = call("initialize", {"clientInfo": {"name": "laomo-probe", "title": "LAOMO Probe", "version": "0.1.0"}})
print("== initialize:", json.dumps(init.get("result", init.get("error", {})))[:200])

r = call("skills/list", {"cwds": ["/Users/lianb/Downloads/bh"]})
res = r.get("result", r.get("error", {}))
print("\n== skills/list result keys:", list(res.keys()) if isinstance(res, dict) else res)
print(json.dumps(res, ensure_ascii=False)[:3000])

# drill into first 2 skills for full shape
if isinstance(res, dict) and res.get("skills"):
    for s in res["skills"][:2]:
        print("\n-- skill entry:", json.dumps(s, ensure_ascii=False)[:1200])

# config/write idempotent probe: re-write current enabled state of first skill
if isinstance(res, dict) and res.get("skills"):
    s0 = res["skills"][0]
    name = s0.get("name") or s0.get("id")
    enabled = bool(s0.get("enabled", True))
    r2 = call("skills/config/write", {"enabled": enabled, "name": name})
    print(f"\n== skills/config/write (idempotent enabled={enabled} name={name}):",
          json.dumps(r2.get("result", r2.get("error", {})), ensure_ascii=False)[:800])
    # verify by re-list
    r3 = call("skills/list", {"cwds": ["/Users/lianb/Downloads/bh"], "forceReload": True})
    res3 = r3.get("result", {})
    after = next((x for x in (res3.get("skills") or [])
                  if (x.get("name") or x.get("id")) == name), None)
    print("== after reload, entry:", json.dumps(after, ensure_ascii=False)[:600])

# unknown-method shape check for capability detection sanity
r4 = call("skills/nope")
print("\n== skills/nope:", json.dumps(r4.get("error", r4.get("result")), ensure_ascii=False)[:300])

# where does it persist? peek config/read skills-related keys
r5 = call("config/read", {})
cfg = (r5.get("result") or {}).get("config") or {}
print("\n== config keys w/ skill:", [k for k in cfg.keys() if "skill" in k.lower()])
for k in [k for k in cfg.keys() if "skill" in k.lower()]:
    print(f"   {k} = {json.dumps(cfg[k], ensure_ascii=False)[:500]}")

proc.stdin.close()
proc.terminate()

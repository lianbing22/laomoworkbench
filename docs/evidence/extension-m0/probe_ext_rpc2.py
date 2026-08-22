import json, subprocess, time
proc = subprocess.Popen(
    ["/Users/lianb/.local/bin/codex", "app-server", "--stdio"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
_n = [0]
def call(method, params=None, timeout=20):
    _n[0] += 1
    req = {"jsonrpc": "2.0", "id": _n[0], "method": method}
    if params is not None: req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n"); proc.stdin.flush()
    end = time.time() + timeout
    while time.time() < end:
        line = proc.stdout.readline()
        if not line: return {"__error": "eof"}
        try: msg = json.loads(line)
        except: continue
        if msg.get("id") == _n[0]: return msg
    return {"__error": "timeout"}

call("initialize", {"clientInfo": {"name": "laomo-probe", "title": "LAOMO", "version": "0.1"}})

# 1. does config/read expose mcp_servers?
r = call("config/read", {})
cfg = (r.get("result") or {})
keys = sorted((cfg.get("config") or {}).keys())
print("config keys:", keys[:30])
print("mcp_servers in config/read:", "mcp_servers" in (cfg.get("config") or {}))

# 2. how many plugins per marketplace (structure summary of plugin/list)
r = call("plugin/list", {"cwds": ["/Users/lianb/Downloads/bh"]})
mps = (r.get("result") or {}).get("marketplaces", [])
for mp in mps:
    print(f"marketplace: {mp['name']} path={'Y' if mp.get('path') else 'N'} plugins={len(mp.get('plugins', []))}")

# 3. plugin/read on a real plugin
first = mps[0]["plugins"][0] if mps and mps[0].get("plugins") else None
if first:
    r = call("plugin/read", {"pluginName": first["name"],
                             "marketplacePath": mp.get("path"),
                             "remoteMarketplaceName": None})
    p = (r.get("result") or {}).get("plugin") or r.get("error") or {}
    if isinstance(p, dict) and p.get("summary"):
        print("plugin/read keys:", sorted(p.keys()))
        print("skills/hooks/apps/mcp/scheduled:", len(p.get("skills", [])), len(p.get("hooks", [])),
              len(p.get("apps", [])), len(p.get("mcpServers", [])), p.get("scheduledTasks"))
    else:
        print("plugin/read:", json.dumps(p, ensure_ascii=False)[:300])

# 4. unknown RPC error shape
r = call("plugin/nonexistent", {})
print("unknown method error:", json.dumps(r.get("error"), ensure_ascii=False)[:200])

proc.stdin.close(); proc.terminate()

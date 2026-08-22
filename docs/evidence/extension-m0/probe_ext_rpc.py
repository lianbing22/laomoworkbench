import json, subprocess, sys, time
proc = subprocess.Popen(
    ["/Users/lianb/.local/bin/codex", "app-server", "--stdio"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)

_next_id = [0]
def call(method, params=None, timeout=25):
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

# initialize first (required by app-server)
init = call("initialize", {"clientInfo": {"name": "laomo-probe", "title": "LAOMO Probe", "version": "0.1.0"}})
print("== initialize:", json.dumps(init.get("result", init.get("error", {})))[:300])

for method, params in [
    ("plugin/list", {"cwds": ["/Users/lianb/Downloads/bh"]}),
    ("plugin/installed", {"cwds": ["/Users/lianb/Downloads/bh"]}),
    ("mcpServerStatus/list", {"detail": "toolsAndAuthOnly"}),
]:
    r = call(method, params)
    out = r.get("result", r.get("error", {}))
    print(f"\n== {method}:", json.dumps(out, ensure_ascii=False)[:1500])

proc.stdin.close()
proc.terminate()

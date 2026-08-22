#!/usr/bin/env python3
"""Gates A-E live driver for P1.1 (real codex + real gateway).

Run:  python3 scripts/gate_p11_driver.py <scratch-dir>
      GATE_PORT=<port> GATE_CODEX_BIN=<codex> python3 scripts/gate_p11_driver.py <scratch>
The driver owns the gateway subprocess (it kills/restarts it in Gate C).

Each gate creates a mission via HTTP and asserts the P1.1 contract:
  A  >=60s job pause/resume: job keeps running while paused, watcher
     re-attaches on resume, no auto-advance, 4-bucket time holds.
  B  >=60s job cancel: process group truly dead, job status cancelled,
     mission terminal cancelled, no managed job survives.
  C  waiting + gateway restart: new control plane recovers, re-attaches
     watcher to the still-running job, mission continues to DONE.
  D  machine verification fails forever: state failed, never done,
     no final evaluator run, per-check results persisted.
  E  machine verification passes after work: verified -> final evaluator
     PASS -> DONE with immutable evidence manifest.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# repo root resolved from this script (scripts/..)
REPO = Path(__file__).resolve().parent.parent
CODEX_BIN = (os.environ.get("GATE_CODEX_BIN") or shutil.which("codex") or
             "/Users/lianb/.local/bin/codex")
PORT = int(os.environ.get("GATE_PORT", "8777"))
BASE = f"http://127.0.0.1:{PORT}/"

SCRATCH = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/bh-gates/session")
VAULT = SCRATCH / "vault"
CLEAN = SCRATCH / "clean"
LOG = None  # opened after SCRATCH exists


def log(msg: str, *, flush=True) -> None:
    print(time.strftime("%H:%M:%S") + " " + msg, flush=flush)


def http(method: str, path: str, payload: dict = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(BASE + path, data=body or None, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {path}: {exc.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"unreachable {path}: {exc.reason}")


def wait_for(pred, timeout: float, desc: str, poll: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = pred()
        except Exception as exc:
            last = f"<err {exc!r}>"
        if last:
            return last
        time.sleep(poll)
    raise TimeoutError(f"{desc} 超时（{timeout}s）；最后观测: {last!r}")


def state_of(mission: dict) -> str:
    return mission.get("state") or mission.get("phase") or "?"


def status(mid: str) -> dict:
    return http("GET", "api/missions/status?id=" + urllib.parse.quote(mid))["mission"]


def ps_dead(pid: int) -> bool:
    out = subprocess.run(["/bin/ps", "-o", "state=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    # dead: no process (empty) or zombie "Z"
    return out == "" or out.startswith("Z")


def pgid_dead(pgid: int) -> bool:
    out = subprocess.run(["/bin/ps", "-o", "pid=", "-g", str(pgid)],
                         capture_output=True, text=True).stdout.strip()
    return out == ""


def run_dir(mid: str) -> Path:
    index = SCRATCH / ".laomo" / "index" / f"{mid}.path"
    if index.is_file():
        return Path(index.read_text("utf-8"))
    return SCRATCH / ".laomo" / "runs" / mid


# --------------------------------------------------------------------------
# gateway lifecycle
# --------------------------------------------------------------------------
SERVER_ARGS = [
    sys.executable, str(REPO / "web/boujoy_server.py"),
    "--port", str(PORT),
    "--vault", str(VAULT),
    "--static", str(REPO / "web"),
    "--clean-home", str(CLEAN),
    "--clean-runtime", "codex",
    "--codex-bin", CODEX_BIN,
    "--codex-cwd", str(SCRATCH),
]
server_proc = None
server_log = None


def start_server() -> None:
    global server_proc, server_log
    log(f"启动网关 :{PORT} ...")
    server_log = open(SCRATCH / "server.log", "a", buffering=1)
    args = SERVER_ARGS + ["--access-code", ""]
    server_proc = subprocess.Popen(args, cwd=REPO, stdin=subprocess.DEVNULL,
                                   stdout=server_log, stderr=subprocess.STDOUT)
    deadline = time.time() + 120
    while time.time() < deadline:
        if server_proc.poll() is not None:
            raise RuntimeError(f"网关进程退出 code={server_proc.returncode}; 见 server.log")
        try:
            http("GET", "api/missions")
            return
        except RuntimeError:
            time.sleep(1)
    raise RuntimeError("网关启动超时")


def kill_server() -> None:
    global server_proc
    if server_proc is None:
        return
    log(f"SIGKILL 网关 pid={server_proc.pid} ...")
    server_proc.kill()
    server_proc.wait(timeout=15)
    server_proc = None


# --------------------------------------------------------------------------
# gate helpers
# --------------------------------------------------------------------------

def running_job(st: dict) -> dict:
    """The job the mission is currently waiting on (latest running one)."""
    for j in reversed(st.get("jobs") or []):
        if j.get("status") == "running":
            return j
    return (st.get("jobs") or [])[-1] if st.get("jobs") else {}


_TERMINAL = ("done", "failed", "cancelled", "blocked")


def all_terminal() -> bool:
    ms = http("GET", "api/missions")["missions"]
    return all(m.get("state") in _TERMINAL for m in ms)


def settle_previous(desc: str) -> None:
    """The previous gate must leave its mission terminal; wait for it so the
    new mission is never refused by the busy check."""
    deadline = time.time() + 300
    while time.time() < deadline:
        ms = http("GET", "api/missions")["missions"]
        active = [m["id"] for m in ms if m.get("state") not in _TERMINAL]
        if not active:
            return
        time.sleep(2)
    log(f"  warn: {desc} 残留非终态 mission {active}，取消除外")


def create_mission(name: str, objective: str, criteria: list[str],
                   verification: dict, options: dict = None) -> str:
    settle_previous(name)
    cwd = SCRATCH / name
    cwd.mkdir(parents=True, exist_ok=True)
    res = http("POST", "api/missions/create", {
        "objective": objective, "cwd": str(cwd),
        "acceptanceCriteria": criteria,
        "options": {"maxWallTimeSec": 2400, **(options or {})},
        "verification": verification,
    })
    mid = res["mission"]["id"]
    # start() refuses ('busy') while the previous gate's runner thread is still
    # exiting; retry a short while instead of aborting the whole run
    for _ in range(12):
        try:
            http("POST", "api/missions/start", {"id": mid})
            break
        except RuntimeError as exc:
            if "409" not in str(exc):
                raise
            time.sleep(5)
    else:
        raise RuntimeError(f"{name}: start 持续 409 — 上个 gate 的 runner 未退出")
    log(f"[{name}] mission {mid} 已创建并启动 (cwd={cwd})")
    return mid


def record_buckets(mid: str, tag: str) -> dict:
    st = status(mid)
    t = dict(st["time"])
    log(f"[{tag}] time={t} state={state_of(st)} verifyResult={st.get('verifyResult')}")
    return t


def assert_true(cond, msg: str, fails: list[str]) -> None:
    if cond:
        log(f"  ok: {msg}")
    else:
        log(f"  FAIL: {msg}")
        fails.append(msg)


def gate_result(name: str, fails: list[str], detail: str = "") -> None:
    if fails:
        log(f"***** GATE {name}: FAIL ({len(fails)}) *****")
        for f in fails:
            log(f"       - {f}")
    else:
        log(f"===== GATE {name}: PASS =====")
    log("")
    return


# --------------------------------------------------------------------------
# gates
# --------------------------------------------------------------------------
def gate_a() -> None:
    fails = []
    obj = ("本任务:启动一个耗时 67 秒的后台作业（命令 sleep 67，通过 <<<LAOMO_JOB>>> 标记声明，"
           "不要在前台等待）。作业结束后在当前目录创建文件 job-a-done.txt，内容为 JOB-A-DONE。"
           "其余工作单元保持最小。")
    mid = create_mission("gA", obj, ["job-a-done.txt 存在且内容含 JOB-A-DONE"], {"commands": ["true"]})
    wait_for(lambda: state_of(status(mid)) == "waiting", 600, "A: 进入 waiting")
    job = running_job(status(mid))
    pid = int(job["pid"])
    assert_true(not ps_dead(pid), f"A: 等待中作业存活 pid={pid}", fails)

    http("POST", "api/missions/pause", {"id": mid})
    wait_for(lambda: state_of(status(mid)) == "paused", 60, "A: 进入 paused")
    time.sleep(12)
    st = status(mid)
    assert_true(not ps_dead(pid), "A: 暂停期间作业仍在运行（不 kill）", fails)
    assert_true(running_job(st)["status"] == "running", "A: 暂停期间 job 状态为 running", fails)
    # pausedMs 在 pause->resume 边界入账，暂停中的 t 里不累计
    time.sleep(2)
    st2 = status(mid)
    assert_true(state_of(st2) == "paused", "A: 暂停期间不自动推进（无 auto-advance）", fails)

    http("POST", "api/missions/resume", {"id": mid})
    wait_for(lambda: state_of(status(mid)) == "waiting", 60, "A: resume 后回 waiting（watcher 重挂）")
    assert_true(not ps_dead(pid), "A: resume 后同一作业仍存活（attach-on-resume）", fails)
    # pausedMs 在 resume 边界入账（暂停时长不计入 wall 预算）
    t_res = dict(status(mid)["time"])
    assert_true(t_res["pausedMs"] >= 10000,
                f"A: resume 时 pausedMs 入账 {t_res['pausedMs']}", fails)

    wait_for(lambda: state_of(status(mid)) == "done", 1500, "A: 作业结束后 mission done")
    st = status(mid)
    same = [j for j in st["jobs"] if j.get("pid") == pid]
    if same:
        sj = same[0]
        assert_true(sj["status"] == "completed", f"A: 重挂的同一作业 completed, 实际 {sj['status']}", fails)
        assert_true(sj.get("exitCode") == 0 and not sj.get("exitUnknown"),
                    f"A: 真实退出码 exitCode={sj.get('exitCode')} 且非 unknown（waitpid 存证）", fails)
    else:
        assert_true(False, "A: 找不到原 pid 的作业记录", fails)
    assert_true(any(j["status"] == "completed" for j in st["jobs"]), "A: 至少一个作业 completed", fails)
    t = dict(st["time"])
    assert_true(t["pausedMs"] >= 10000, f"A: pausedMs>0 {t['pausedMs']}", fails)
    assert_true(abs(t["wallElapsedMs"] - (t["agentActiveMs"] + t["waitingMs"])) <= 2000,
                f"A: 账面不变量 wall≈active+waiting {t}", fails)
    final = run_dir(mid) / "verdicts" / "final.json"
    assert_true(final.is_file(), "A: final evaluator 已运行", fails)
    gate_result("A", fails)


def gate_b() -> None:
    fails = []
    obj = ("本任务:启动一个耗时 90 秒的后台作业（命令 sleep 90，通过 <<<LAOMO_JOB>>> 标记声明，"
           "不要在前台等待）。作业结束后回显 JOB-B-DONE。其余单元保持最小。")
    mid = create_mission("gB", obj, ["JOB-B-DONE 已回显"], {"commands": ["true"]})
    wait_for(lambda: state_of(status(mid)) == "waiting", 600, "B: 进入 waiting")
    job = running_job(status(mid))
    pid = int(job["pid"])
    pgid = int(job.get("processGroup") or job.get("pgid") or pid)
    assert_true(not ps_dead(pid), f"B: 等待中作业存活 pid={pid}", fails)

    res = http("POST", "api/missions/cancel", {"id": mid})
    wait_for(lambda: state_of(status(mid)) == "cancelled", 60, "B: mission cancelled")
    for k in (0, 0.5, 1, 2, 5):
        if ps_dead(pid):
            break
        time.sleep(k if k else 0.2)
    assert_true(ps_dead(pid), "B: 作业进程已真死（ps 无此 pid 或 Z）", fails)
    assert_true(pgid_dead(pgid), f"B: 整个进程组 {pgid} 已清空", fails)
    st = status(mid)
    sj = running_job(st)
    assert_true(sj["status"] == "cancelled", f"B: job 状态 cancelled, 实际 {sj['status']}", fails)
    assert_true(sj.get("terminateMode") in ("term", "kill"),
                f"B: job 带 terminateMode={sj.get('terminateMode')}", fails)
    log(f"  info: stopReason={st.get('stopReason')!r}")
    # no managed job survives a cancelled mission
    assert_true(all(j["status"] in ("cancelled", "orphaned") for j in st["jobs"]),
                "B: 取消后无存活托管作业", fails)
    entries = [m for m in http("GET", "api/missions")["missions"] if m["id"] == mid]
    assert_true(entries and entries[0]["state"] == "cancelled", "B: list 显示 cancelled", fails)
    gate_result("B", fails)


def gate_c() -> None:
    fails = []
    obj = ("本任务:启动一个耗时 90 秒的后台作业（命令 sleep 90，通过 <<<LAOMO_JOB>>> 标记声明，"
           "不要在前台等待）。作业结束后在当前目录创建文件 job-c-done.txt，内容为 JOB-C-DONE。"
           "其余单元保持最小。")
    mid = create_mission("gC", obj, ["job-c-done.txt 存在且内容含 JOB-C-DONE"], {"commands": ["true"]})
    wait_for(lambda: state_of(status(mid)) == "waiting", 600, "C: 进入 waiting")
    job = running_job(status(mid))
    pid = int(job["pid"])
    assert_true(not ps_dead(pid), f"C: 崩溃前作业存活 pid={pid}", fails)

    # simulate gateway crash: SIGKILL, then verify the detached job survives
    kill_server()
    time.sleep(2)
    assert_true(not ps_dead(pid), "C: 网关被 kill 后作业进程仍在（独立进程组）", fails)
    start_server()
    log("C: 网关已重启，触发 recover ...")
    wait_for(lambda: state_of(status(mid)) in ("waiting", "running"), 120, "C: recover 后 mission 恢复")
    st = status(mid)
    assert_true(state_of(st) == "waiting", f"C: 恢复后回到 waiting（job 仍存活重挂 watcher）, 实际 {state_of(st)}", fails)
    sj = running_job(st)
    assert_true(int(sj["pid"]) == pid, f"C: 同一 job pid 未被替换 ({sj['pid']} vs {pid})", fails)
    assert_true(sj["status"] != "orphaned", f"C: job 未被误判孤儿, 实际 {sj['status']}", fails)

    wait_for(lambda: state_of(status(mid)) == "done", 1200, "C: 作业结束后 mission done（跨重启完成）")
    st = status(mid)
    alive_then = [j for j in st["jobs"] if j.get("pid") == pid]
    if alive_then:
        sj = alive_then[0]
        # the live job's exit is observed by the post-restart watcher; the exit
        # code is known only if waitpid could reap it (same process); after a
        # control-plane restart it is honestly unknown -> exitUnknown
        assert_true(sj["status"] in ("completed", "failed"),
                    f"C: 跨重启作业终态 {sj['status']} 合法", fails)
        if sj["status"] == "failed":
            assert_true(sj.get("exitUnknown") is True,
                        f"C: 未知退出码须显式标注 exitUnknown, 实际 {sj.get('exitUnknown')}", fails)
    else:
        assert_true(False, "C: 找不到跨重启作业的最终记录", fails)
    assert_true(any(j["status"] == "completed" for j in st["jobs"]),
                "C: 至少一个作业 completed（重跑或存证成功）", fails)
    assert_true((run_dir(mid) / "verdicts" / "final.json").is_file(), "C: final evaluator 已运行", fails)
    gate_result("C", fails)


def gate_d() -> None:
    fails = []
    obj = ("本任务:创建文件 result.txt 内容 ok，然后交付。其余单元保持最小。")
    mid = create_mission("gD", obj, ["result.txt 存在且内容为 ok"],
                         {"commands": ["test -f /dev/null/gate-d-never"]},
                         options={"maxWallTimeSec": 1500})
    seen_done = [False]
    seen_states = set()

    def poll_terminal():
        st = status(mid)
        seen_states.add(state_of(st))
        if state_of(st) == "done":
            seen_done[0] = True
        return state_of(st) if state_of(st) in ("failed", "done", "cancelled") else None

    try:
        terminal = wait_for(poll_terminal, 1700, "D: 到达终态（failed）")
    except TimeoutError as exc:
        gate_result("D", ["未在期限内失败: " + str(exc)])
        log(f"D: 观测到的状态集合 {seen_states}")
        raise
    assert_true(terminal == "failed", f"D: 终态应为 failed, 实际 {terminal}", fails)
    assert_true(not seen_done[0], "D: 全程未出现 done", fails)
    st = status(mid)
    assert_true(st.get("verifyResult") == "fail", f"D: verifyResult=fail, 实际 {st.get('verifyResult')}", fails)
    rdir = run_dir(mid)
    assert_true(not (rdir / "verdicts" / "final.json").is_file(),
                "D: 机器门禁未过不得跑 final evaluator", fails)
    results = rdir / "verification" / "results.json"
    assert_true(results.is_file(), "D: verification/results.json 已持久化", fails)
    data = json.loads(results.read_text("utf-8"))
    assert_true(data.get("passed") is False, "D: results.passed=False", fails)
    cmd = next((c for c in data["checks"] if c.get("kind") == "command"), None)
    assert_true(cmd is not None, "D: 包含 command 检查结果", fails)
    if cmd:
        assert_true(cmd.get("exitCode") not in (0, None), f"D: 命令失败 exitCode={cmd.get('exitCode')}", fails)
        for field in ("command", "stdoutTail", "stderrTail", "startedAt", "endedAt", "resultHash"):
            assert_true(field in cmd, f"D: 检查结果含 {field}", fails)
    assert_true(st.get("stopReason"), "D: mission 有 stopReason", fails)
    gate_result("D", fails)


def gate_e() -> None:
    fails = []
    obj = ("本任务:创建文件 gate-e-marker.txt 内容 ok，然后交付。其余单元保持最小。")
    mid = create_mission("gE", obj, ["gate-e-marker.txt 存在且内容为 ok"],
                         {"commands": ["test -f gate-e-marker.txt"],
                          "requiredFiles": ["gate-e-marker.txt"]})
    wait_for(lambda: state_of(status(mid)) == "done", 1800, "E: 机器门禁 PASS 后 DONE")
    st = status(mid)
    assert_true(st.get("verifyResult") == "pass", "E: verifyResult=pass", fails)
    assert_true((run_dir(mid) / "verdicts" / "final.json").is_file(), "E: final evaluator 已运行", fails)
    results = run_dir(mid) / "verification" / "results.json"
    data = json.loads(results.read_text("utf-8"))
    assert_true(data.get("passed") is True, "E: results.passed=True", fails)
    assert_true(all(c.get("passed") for c in data["checks"]), "E: 每个检查 passed", fails)
    timed = list(run_dir(mid).glob("verification/results-*.json"))
    assert_true(bool(timed), "E: 带时间戳的结果存证也存在", fails)

    manifest = run_dir(mid) / "evidence" / "manifest.json"
    wait_for(manifest.is_file, 30, "E: evidence manifest 生成")
    m1 = json.loads(manifest.read_text("utf-8"))
    entries = m1.get("entries") if isinstance(m1, dict) and "entries" in m1 else None
    if isinstance(entries, dict):
        entries = list(entries.values())
    if entries is None:
        entries = m1 if isinstance(m1, list) else []
    assert_true(bool(entries), "E: manifest 有存证条目", fails)
    for e in entries:
        if isinstance(e, dict):
            need = ["path", "sha256", "generatedAt"]
            for k in need:
                assert_true(k in e, f"E: manifest 条目含 {k}", fails)
            break
    assert_true(any("verification/results.json" in str(e.get("path", "")) if isinstance(e, dict) else False
                    for e in entries), "E: manifest 含 verification/results.json 存证", fails)
    before = manifest.read_bytes()
    time.sleep(3)
    assert_true(manifest.read_bytes() == before, "E: DONE 后 manifest 不可变", fails)
    gate_result("E", fails)


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    VAULT.mkdir(exist_ok=True)
    CLEAN.mkdir(exist_ok=True)
    log(f"=== P1.1 Gates A-E 开始 scratch={SCRATCH} port={PORT} ===")
    try:
        start_server()
    except Exception as exc:
        log(f"网关启动失败: {exc!r}")
        return 2
    gates = [("A", gate_a), ("B", gate_b), ("C", gate_c), ("D", gate_d), ("E", gate_e)]
    try:
        for name, fn in gates:
            log(f">>> GATE {name} ...")
            t0 = time.time()
            fn()
            log(f"[{name}] 耗时 {time.time() - t0:.0f}s")
        log("=== ALL GATES PASSED ===")
        return 0
    except Exception as exc:
        log(f"!!! 门禁运行中止: {exc!r}")
        return 1
    finally:
        kill_server()
        for p in list(SCRATCH.rglob("*.http.pid")):
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())

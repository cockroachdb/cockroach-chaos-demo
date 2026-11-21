#!/usr/bin/env python3
"""Checks run on the hosted VM itself, after it was replaced: the cluster, the rebuild switch, the Payments tables'
settings, where latency is shaped, and a walk through every guided scenario that fits the cluster, the way the proof
run walks them. Standard library only. Ends with a summary; exit code 1 if anything failed."""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

PANEL = "http://localhost:8088"
T0 = time.time()
results: list[tuple[str, bool]] = []


def api(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(PANEL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw and raw[:1] in (b"{", b"[") else raw.decode(errors="replace"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except ValueError:
            return e.code, raw.decode(errors="replace")


def state():
    return api("GET", "/api/state")[1]


def sh(*args, timeout=180):
    p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def sql(query):
    rc, out, err = sh("sudo", "docker", "exec", "us-east-1a-n1", "cockroach", "sql", "--insecure", "--format=tsv", "-e", query)
    if rc:
        raise RuntimeError(err.strip()[:300])
    return [line for line in out.splitlines()[1:] if line]


def note(msg):
    print(f"[{time.time() - T0:6.0f}s] {msg}", flush=True)


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    note(("PASS  " if ok else "FAIL  ") + name + (f"  ({detail})" if detail else ""))
    return bool(ok)


def wait_for(pred, timeout, what, every=2.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            got = pred()
            if got:
                return got
        except Exception:
            pass
        time.sleep(every)
    raise TimeoutError(f"timed out after {timeout:.0f} s waiting for {what}")


def healthy(nodes, timeout=1800):
    def ok():
        s = state()
        c = s["cluster"]
        busy = (s.get("operation") or {}).get("status") == "running"
        return s if c["status"] == "healthy" and c["live_nodes"] == c["total_nodes"] == nodes and not busy else None
    return wait_for(ok, timeout, f"a healthy cluster of {nodes} nodes")


def section(fn):
    try:
        fn()
    except Exception as e:
        check(fn.__doc__.strip().split(".")[0], False, f"{type(e).__name__}: {e}")


def cluster():
    """The cluster is the one that was asked for."""
    s = healthy(9, timeout=600)
    c = s["cluster"]
    check("nine nodes, all live, healthy", c["status"] == "healthy" and c["live_nodes"] == 9, f"{c['live_nodes']}/{c['total_nodes']} {c['status']}")
    check("preset aws, v26.4.1, REGION survival", (c["preset"], c["crdb_version"], c["survival_goal"]) == ("aws", "v26.4.1", "region"),
          f"{c['preset']} {c['crdb_version']} {c['survival_goal']}")
    check("three regions of three nodes", sorted(len(r["nodes"]) for r in s["regions"]) == [3, 3, 3],
          str({r["name"]: len(r["nodes"]) for r in s["regions"]}))
    check("Payments are not running after boot", not s["workload"]["running"])
    system = api("GET", "/api/system")[1]
    check("a license is configured", system.get("license") == "configured", str(system.get("license"))[:60])
    for path, marker in (("/static/stage/stage.js", "placeLabels"), ("/static/presenter/presenter.js", "applyRebuildLock"),
                         ("/static/shared/tips.js", "pressed")):
        code, body = api("GET", path)
        check(f"the page code served is the new one ({path.rsplit('/', 1)[1]})", code == 200 and marker in str(body))


def rebuild_switch():
    """Rebuilding is switched off here, and only rebuilding."""
    system = api("GET", "/api/system")[1]
    check("the panel reports rebuild as switched off", system.get("rebuild") is False, f"rebuild={system.get('rebuild')}")
    before = state()["cluster"]
    code, body = api("POST", "/api/cluster/rebuild", {"preset": "gcp"})
    check("a rebuild is refused with the reason", code == 403 and "switched off" in str(body), f"HTTP {code}: {str(body)[:110]}")
    code, _ = api("POST", "/api/cluster/latency", {"setting": "realistic"})
    check("another change is still accepted", code == 200, f"HTTP {code}")
    time.sleep(4)
    after = state()["cluster"]
    check("the cluster is as it was", (after["preset"], after["total_nodes"], after["status"]) == (before["preset"], before["total_nodes"], before["status"]),
          f"{after['preset']} {after['live_nodes']}/{after['total_nodes']} {after['status']}")
    rc, out, _ = sh("sudo", "grep", "-c", "^REBUILD=off$", "/opt/cockroach-chaos-demo/.env")
    check("the VM's .env says REBUILD=off", rc == 0 and out.strip() == "1")


def payments_tables():
    """Payments tables stay small."""
    check("Payments start", api("POST", "/api/workload/start")[0] == 200)
    w = wait_for(lambda: (lambda w: w if w["tps"] > 50 and w["balance"] == 10_000_000 else None)(state()["workload"]), 240,
                 "payments to run and the books to be checked")
    check("Payments run and the books balance", w["money_lost"] == 0, f"{w['tps']}/s, total ${w['balance']:,}, lost {w['money_lost']}")
    nodes = len(w["homing"]["regions"]) if w["homing"]["on"] else 1
    limit = max(100, -(-2 * w["rate"] // nodes))
    create = "\n".join(sql("SHOW CREATE TABLE chaos.public.transfers"))
    check("transfers expire after an hour, with a job every ten minutes", "ttl_expire_after = '01:00:00'" in create and "ttl_job_cron = '*/10 * * * *'" in create)
    check(f"the job may delete {limit} rows a second per node ({w['rate']} payments/s, {nodes} home regions)", f"ttl_delete_rate_limit = {limit}," in create)
    schedule = sql("SELECT recurrence, schedule_status FROM [SHOW SCHEDULES] WHERE label LIKE 'row-level-ttl: transfers%'")
    check("the expiry schedule is active, every ten minutes", schedule == ["*/10 * * * *\tACTIVE"], str(schedule))
    for table in ("accounts", "transfers"):
        zone = "\n".join(sql(f"SHOW ZONE CONFIGURATION FROM TABLE chaos.public.{table}"))
        check(f"{table} keeps old versions for ten minutes", "gc.ttlseconds = 600" in zone)
    api("POST", "/api/workload/stop")


def shaping():
    """Latency is shaped on the interface that holds each node's address."""
    s = state()
    wrong, summary = [], []
    for n in s["nodes"]:
        rc, out, err = sh("sudo", "docker", "run", "--rm", "--network", f"container:{n['name']}", "--entrypoint", "sh",
                          "crdb-chaos-control-plane:local", "-c", "ip -o -4 addr show; echo ---; tc qdisc show")
        addresses, rules = out.split("---") if "---" in out else (out, "")
        devices = {line.split()[1]: line.split()[3].split("/")[0] for line in addresses.splitlines() if line.strip() and " lo " not in line}
        mine = [dev for dev, ip in devices.items() if ip == n["ip"]]
        delayed = {line.split("dev ")[1].split()[0] for line in rules.splitlines() if "netem" in line and "delay" in line}
        ok = rc == 0 and len(mine) == 1 and delayed == set(mine)
        summary.append(f"{n['name']}: {','.join(f'{d}={ip}' for d, ip in devices.items())} shaped {sorted(delayed)}")
        if not ok:
            wrong.append(summary[-1] + (f" [{err.strip()[:80]}]" if rc else ""))
    check("every node is shaped on the interface that holds its own address, and only there", not wrong, "; ".join(wrong) or summary[0])
    check("every node is on one network", all(s.count("=") == 1 for s in summary), "; ".join(x for x in summary if x.count("=") != 1)[:200])


def recover_everything():
    api("POST", "/api/scenarios/stop")
    api("POST", "/api/chaos", {"action": "recover", "kind": "all", "value": ""})
    api("POST", "/api/workload/stop")


def scenarios():
    """Every guided scenario that fits this cluster runs to the end."""
    listed = api("GET", "/api/scenarios")[1]
    fits = [sc for sc in listed if not sc["unmet"]]
    note(f"{len(listed)} scenarios listed, {len(fits)} fit this cluster; not fitting: " +
         (", ".join(f"{sc['title']} ({'; '.join(sc['unmet'])})" for sc in listed if sc["unmet"]) or "none"))
    cur = lambda: api("GET", "/api/scenarios/current")[1]  # noqa: E731
    for sc in fits:
        started = time.time()
        try:
            code, body = api("POST", f"/api/scenarios/{sc['id']}/start")
            if code != 200:
                raise RuntimeError(f"start refused: {body}")
            wait_for(lambda: not cur()["busy"], 900, "the scenario to prepare")
            for _ in range(sc["steps"]):
                api("POST", "/api/scenarios/next")
                time.sleep(1.0)
                wait_for(lambda: not cur()["busy"], 2700, "the step to finish")
            steps = cur()["steps"]
            api("POST", "/api/scenarios/stop")
            failed = [f"{st['label']}: {st['error']}" for st in steps if st["status"] != "done"]
            last = steps[-1]["result"] if steps else None
            check(f"scenario: {sc['title']}", not failed, "; ".join(failed) or f"{len(steps)} steps in {time.time() - started:.0f} s; last: {json.dumps(last, ensure_ascii=False)[:150]}")
        except Exception as e:
            check(f"scenario: {sc['title']}", False, f"{type(e).__name__}: {e}")
        time.sleep(5)
        try:
            recover_everything()
            s = healthy(9)
            w = s["workload"]
            if w["money_lost"] not in (0, None):
                check(f"no money lost after {sc['title']}", False, f"lost {w['money_lost']}")
        except Exception as e:
            check(f"the cluster is healthy again after {sc['title']}", False, f"{type(e).__name__}: {e}")


def final():
    """The instance is left as a presenter expects to find it."""
    recover_everything()
    s = healthy(9)
    c, w = s["cluster"], s["workload"]
    check("left healthy: nine nodes, Payments stopped, no scenario", c["status"] == "healthy" and not w["running"] and not s["scenario"],
          f"{c['live_nodes']}/{c['total_nodes']} {c['status']}, payments running {w['running']}, scenario {bool(s['scenario'])}")
    check("no money was lost in any of it", w["money_lost"] in (0, None), f"lost {w['money_lost']}, failed transfers {w['failed_total']}")
    check("the last operation did not fail", (s.get("operation") or {}).get("status") != "failed", str((s.get("operation") or {}).get("error"))[:120])
    check("rebuild is still switched off", api("GET", "/api/system")[1].get("rebuild") is False)


only = set(sys.argv[1:])
for part in (cluster, rebuild_switch, payments_tables, shaping, scenarios, final):
    if not only or part.__name__ in only:
        note(f"--- {part.__doc__.strip()}")
        section(part)
failed = [name for name, ok in results if not ok]
note(f"SUMMARY: {len(results) - len(failed)} passed, {len(failed)} failed" + ("".join(f"\n    FAILED: {name}" for name in failed)))
sys.exit(1 if failed else 0)

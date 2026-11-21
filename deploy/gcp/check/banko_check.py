#!/usr/bin/env python3
"""Banko on the hosted VM after the VM was replaced: ready, its pipeline up, and a signal inserted by SQL comes back
as a nudge through the changefeed and Kafka, for a throwaway zz- user that is removed again, so the personas stay as
they are. Where Banko still has its username page, the user signs up the way a browser behind the proxy does and
has to get a welcome nudge. Where Banko signs people in from their Google identity, nothing here can sign in: the
check is that Banko refuses a request without that identity, and the user is made by Banko's own code instead.
Standard library only."""
import http.cookiejar
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PUBLIC = sys.argv[1]  # the public host of Banko's address, as its proxy forwards it
BANKO = "http://localhost:5000"
HEADERS = {"Host": PUBLIC, "X-Forwarded-Host": PUBLIC, "X-Forwarded-Proto": "https"}
T0 = time.time()
failed = []


def note(msg):
    print(f"[{time.time() - T0:5.0f}s] {msg}", flush=True)


def check(name, ok, detail=""):
    if not ok:
        failed.append(name)
    note(("PASS  " if ok else "FAIL  ") + name + (f"  ({detail})" if detail else ""))
    return bool(ok)


def sh(*args, timeout=180):
    p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def sql(query):
    rc, out, err = sh("sudo", "docker", "exec", "us-east-1a-n1", "cockroach", "sql", "--insecure", "-d", "defaultdb", "--format=tsv", "-e", query)
    if rc:
        raise RuntimeError(err[:300])
    return out.splitlines()[1:]


def wait_for(pred, timeout, every=3.0):
    end = time.time() + timeout
    while time.time() < end:
        got = pred()
        if got:
            return got
        time.sleep(every)
    return None


opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def get(path, timeout=30):
    req = urllib.request.Request(BANKO + path, headers=HEADERS)
    with opener.open(req, timeout=timeout) as r:
        return r.status, r.read().decode(errors="replace")


def sign_in_on():
    """Whether Banko takes identity only from what Google signs: it then refuses its API to a request without it."""
    try:
        get("/api/v1/me")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return True
        raise
    return False


user_id = None
try:
    status, body = get("/api/health/ready")
    ready = json.loads(body)
    check("Banko says it is ready", status == 200 and ready.get("ready") is True, json.dumps(ready)[:200])

    rc, names, _ = sh("sudo", "docker", "ps", "--format", "{{.Names}}")
    running = set(names.split())
    pipeline = sorted(n for n in running if n.startswith("banko-cdc-"))
    check("Banko and its pipeline's containers are up", "banko" in running and "banko-cdc-kafka" in running and len(pipeline) >= 2, ", ".join(["banko"] + pipeline))

    feeds = sql("SELECT status, count(*) FROM [SHOW CHANGEFEED JOBS] GROUP BY 1")
    check("the pipeline's changefeed is running", any(f.startswith("running\t") for f in feeds), str(feeds))

    personas = sql("SELECT username FROM users WHERE username IN ('maya', 'sam', 'riley') ORDER BY 1")
    check("the three personas exist", personas == ["maya", "riley", "sam"], str(personas))
    others_before = sql("SELECT count(*) FROM users WHERE username NOT LIKE 'zz-%'")[0]

    rc, style, err = sh("sudo", "docker", "exec", "banko", "python", "-c",
                        "from banko_ai.config.catalog import get_catalog; print(sorted(get_catalog().profiles)[0])")
    style = style.splitlines()[-1] if style else ""
    name = f"zz-check-{int(time.time())}"

    def nudges():
        return int(sql(f"SELECT count(*) FROM coach_nudges WHERE user_id = '{user_id}'")[0])

    if sign_in_on():
        check("a request without Google's signed identity is refused", True, "401 from /api/v1/me")
        rc, out, err = sh("sudo", "docker", "exec", "banko", "python", "-c",
                          "from banko_ai.config.settings import get_config; from banko_ai.web.auth import UserManager; "
                          f"print(UserManager(get_config().database_url).create('{name}', '{style}')['user_id'])")
        user_id = out.splitlines()[-1] if rc == 0 and out else None
        check(f"a throwaway account is made by Banko's own code ({name})", bool(user_id), err[-160:])
    else:
        form = urllib.parse.urlencode({"username": name, "spending_style": style}).encode()
        req = urllib.request.Request(BANKO + "/signup", data=form, headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"})
        with opener.open(req, timeout=120) as r:
            signed_up = r.status
        row = sql(f"SELECT user_id FROM users WHERE username = '{name}'")
        user_id = row[0] if row else None
        check(f"a new sign-up is accepted ({name}, style {style})", signed_up == 200 and user_id, f"HTTP {signed_up}, user {user_id}")
        welcome = wait_for(lambda: nudges() >= 1, 300)
        check("the welcome nudge arrives", bool(welcome), f"{nudges()} nudge(s) after {time.time() - T0:.0f} s")

    before = nudges()
    sql("INSERT INTO spending_signals (user_id, signal_type, severity, payload, idempotency_key) VALUES "
        f"('{user_id}', 'budget_threshold', 'warn', "
        "'{\"category\": \"dining\", \"pct_used\": 0.82, \"monthly_budget\": 400.0, \"spent_so_far\": 328.0, \"days_remaining\": 9}', "
        "'check:' || gen_random_uuid()::STRING)")
    t = time.time()
    through = wait_for(lambda: nudges() > before, 180)
    check("a signal inserted by SQL comes back as a nudge, through the changefeed and Kafka", bool(through),
          f"{nudges() - before} new nudge(s) {time.time() - t:.0f} s after the insert")
finally:
    if user_id:
        rc, out, err = sh("sudo", "docker", "exec", "banko", "python", "-c",
                          f"from banko_ai.services.users import delete_users; print(delete_users(['{user_id}']))")
        left = sql(f"SELECT (SELECT count(*) FROM users WHERE user_id = '{user_id}') + (SELECT count(*) FROM coach_nudges WHERE user_id = '{user_id}') "
                   f"+ (SELECT count(*) FROM spending_signals WHERE user_id = '{user_id}')")[0]
        check("the throwaway user and everything it owned are removed", rc == 0 and left == "0", f"delete_users said {out.splitlines()[-1] if out else err[:80]}; rows left {left}")
        others_after = sql("SELECT count(*) FROM users WHERE username NOT LIKE 'zz-%'")[0]
        check("every other user is still there", others_after == others_before, f"{others_before} before, {others_after} after")

note(f"SUMMARY: {'all passed' if not failed else str(len(failed)) + ' failed: ' + '; '.join(failed)}")
sys.exit(1 if failed else 0)

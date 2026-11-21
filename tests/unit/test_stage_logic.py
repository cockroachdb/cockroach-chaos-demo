"""The stage's own rules, run in node: the functions are cut out of stage.js as they are and fed snapshots."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STAGE_JS = (ROOT / "web" / "stage" / "stage.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def cut(start: str) -> str:
    """The statement or function of stage.js that begins with `start`, through its closing brace or semicolon."""
    at = STAGE_JS.index(start)
    if not start.startswith(("function", "async function")):
        return STAGE_JS[at:STAGE_JS.index("\n", at)]
    depth, i = 0, STAGE_JS.index("{", at)
    while True:
        depth += {"{": 1, "}": -1}.get(STAGE_JS[i], 0)
        i += 1
        if depth == 0:
            return STAGE_JS[at:i]


RECOVERY = "\n".join(cut(start) for start in ("const PROVISIONING", "function nodeLook(", "const DISTURBED", "let disturbedSince",
                                              "function watchRecovery("))


def celebrations(snapshots: list[dict]) -> list[float]:
    """Play the snapshots through the stage's recovery watcher: the times at which it celebrated a recovery."""
    script = (RECOVERY + "\nconst celebrated = [];\nfunction celebrate(s) { celebrated.push(s.ts); }\n"
              f"for (const s of {json.dumps(snapshots)}) watchRecovery(s);\nconsole.log(JSON.stringify(celebrated));\n")
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def node(name, region, *, runtime="running", liveness="live"):
    return {"name": name, "region": region, "runtime": runtime, "liveness": liveness, "partitioned": False, "isolated": False,
            "brownout": None}


def snap(ts, nodes, *, down=(), op=None):
    regions = [{"name": r, "state": "down" if r in down else "healthy"} for r in sorted({n["region"] for n in nodes})]
    live = sum(n["liveness"] == "live" for n in nodes)
    return {"ts": ts, "cluster": {"live_nodes": live, "total_nodes": len(nodes)}, "telemetry": {"sql": "ok"}, "nodes": nodes,
            "regions": regions, "operation": op}


STAYING = [node("us-east-1a-n1", "us-east-1"), node("us-west-2a-n1", "us-west-2")]
TOKYO = [node("ap-northeast-1a-n1", "ap-northeast-1"), node("ap-northeast-1c-n2", "ap-northeast-1")]
TOKYO_STOPPED = [node(n["name"], "ap-northeast-1", runtime="stopped", liveness="dead") for n in TOKYO]


def test_a_region_that_comes_back_from_an_outage_is_celebrated():
    seen = celebrations([snap(100, STAYING + TOKYO), snap(101, STAYING + TOKYO_STOPPED, down=("ap-northeast-1",)),
                         snap(105, STAYING + TOKYO_STOPPED, down=("ap-northeast-1",)), snap(106, STAYING + TOKYO)])
    assert seen == [106]


def test_retiring_a_region_on_purpose_is_not_a_recovery():
    """Its nodes are stopped a few seconds before the region leaves the picture. Nothing failed and nothing recovered:
    'Fully recovered' over a planned scale-in tells the audience there was an outage."""
    removal = {"kind": "remove-region", "status": "running",
               "steps": ["Checking Payments accounts in ap-northeast-1", "Removing ap-northeast-1 from the multi-region databases",
                         "Decommissioning ap-northeast-1a-n1, ap-northeast-1c-n2", "Releasing capacity"]}
    seen = celebrations([snap(100, STAYING + TOKYO, op=removal), snap(101, STAYING + TOKYO_STOPPED, down=("ap-northeast-1",), op=removal),
                         snap(105, STAYING + TOKYO_STOPPED, down=("ap-northeast-1",), op=removal),
                         snap(106, STAYING, op=dict(removal, steps=removal["steps"] + ["Updating the global load balancer"])),
                         snap(108, STAYING, op=dict(removal, status="done"))])
    assert seen == []


def test_decommissioning_a_node_on_purpose_is_not_a_recovery():
    extra = node("us-east-1a-n4", "us-east-1")
    stopped = node("us-east-1a-n4", "us-east-1", runtime="stopped", liveness="dead")
    drain = {"kind": "decommission", "status": "running", "steps": ["Decommissioning us-east-1a-n4", "Releasing capacity"]}
    seen = celebrations([snap(100, STAYING + [extra], op=drain), snap(101, STAYING + [stopped], op=drain),
                         snap(105, STAYING + [stopped], op=drain), snap(106, STAYING, op=dict(drain, status="done"))])
    assert seen == []


def test_a_real_failure_during_a_planned_change_still_counts():
    removal = {"kind": "remove-region", "status": "running", "steps": ["Removing ap-northeast-1 from the multi-region databases"]}
    crashed = [node("us-east-1a-n1", "us-east-1", runtime="stopped", liveness="dead"), STAYING[1]]
    seen = celebrations([snap(100, STAYING + TOKYO, op=removal), snap(101, crashed + TOKYO, op=removal),
                         snap(105, crashed + TOKYO, op=removal), snap(106, STAYING + TOKYO, op=removal)])
    assert seen == [106]


def looks(nodes: list[dict], op: dict) -> dict:
    """How the stage shows each node while `op` runs: {node name: look}."""
    script = (cut("function nodeLook(") + f"\nconst op = {json.dumps(op)};\n"
              f"console.log(JSON.stringify(Object.fromEntries({json.dumps(nodes)}.map((n) => [n.name, nodeLook(n, op)[0]]))));\n")
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def removal_log(payments_line: str) -> dict:
    return {"kind": "remove-region", "status": "running",
            "steps": ["Checking Payments accounts in ap-northeast-1", payments_line,
                      "Removing ap-northeast-1 from the multi-region databases"]}


def test_only_the_region_that_leaves_looks_as_if_it_were_draining():
    """The stage finds the draining nodes by the region named in the removal's log. Payments adds a line to that log
    (Workload.evacuate); when the line also named the regions that took the accounts, every node on the stage drained."""
    from test_workload import FOUR, running_workload
    w, db = running_workload(FOUR)
    line = w.evacuate("ap-northeast-1")
    seen = looks(STAYING + TOKYO, removal_log(line))
    assert seen == {"us-east-1a-n1": "live", "us-west-2a-n1": "live", "ap-northeast-1a-n1": "draining", "ap-northeast-1c-n2": "draining"}
    # the rule this guards against breaking: a line that names another region paints that region too
    careless = looks(STAYING + TOKYO, removal_log("2,500 Payments accounts moved from ap-northeast-1 to us-east-1"))
    assert careless["us-east-1a-n1"] == "draining"


def run(script: str):
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_n_key_presses_next_the_way_a_click_does():
    """A step's "Running…" reaches the stage with the next snapshot, up to a few seconds after the press. A click on Next
    shows at once that it was taken. The N key left the screen as it was for that long, which is when a presenter
    presses it a second time."""
    calls = run("\n".join(cut(start) for start in ("function onKey(", "let stepping", "async function nextStep(")) + """
const calls = [];
const canControl = true, state = { scenario: { id: 'agent-budget' } };
function renderScenario() {}
async function press(key, fn) { calls.push(['press', key]); await fn(); }
async function post(path) { calls.push(['post', path]); return true; }
for (const key of ['n', 'N', 'ArrowRight']) onKey({ key, target: null });
setTimeout(() => console.log(JSON.stringify(calls)), 0);
""")
    assert calls == [["press", "sc-next"], ["post", "/api/scenarios/next"]] * 3


# The scenario panel, with just enough of a page under it: what its result box shows and whether Next can be pressed.
PANEL = "\n".join(cut(start) for start in ("let stepping", "async function nextStep(", "function renderScenario(")) + """
const els = {};
const $ = (id) => els[id] || (els[id] = { textContent: '', className: '', hidden: false, disabled: false, shown: null,
                                          replaceChildren(...children) { this.shown = children.map((c) => c.text).join(' '); } });
const el = (tag, cls, text) => ({ text });
const document = { body: { classList: { toggle() {} } } };
function renderResult(box, result) { box.shown = result ? JSON.stringify(result) : ''; }
let state = null, accepted = true, clock = 0;
Date.now = () => clock;
async function post() { return accepted; }
function snapshot(scenario) { state = { scenario }; renderScenario(state); return panel(); }
const panel = () => ({ shows: els['sc-result'].shown, next: els['sc-next'].disabled ? 'off' : 'on' });
"""
OVERSPENT = {"Agents approved": "50 of 50", "Budget": "OVERSPENT by $2,500 ✗"}


def race(step=-1, *, busy=False, result=None) -> dict:
    """The budget race as a snapshot carries it: before its first step, while a step runs, or with a step's result."""
    steps = [{"label": label, "caption": "", "observe": "", "status": "pending", "result": None, "error": None}
             for label in ("Race under READ COMMITTED", "Race under SERIALIZABLE")]
    if result:
        steps[step].update(status="done", result=result)
    return {"id": "agent-budget", "title": "Agents race for one budget", "tier": 2, "summary": "", "step": step, "busy": busy,
            "steps": steps}


def panel(js: str) -> dict:
    return run(PANEL + f"(async () => {{ const seen = {{}};\n{js}\nconsole.log(JSON.stringify(seen)); }})();\n")


def test_a_step_started_from_the_stage_shows_as_running_at_once():
    """The snapshot that says so is a few seconds away: the collector builds one every 1.5 s and the stream sends one a
    second. Until it comes the panel says "Running…" itself, and an older snapshot that arrives first does not undo it."""
    seen = panel(f"""
        seen.ready = snapshot({json.dumps(race())});
        await nextStep();
        seen.pressed = panel();
        seen.stale = snapshot({json.dumps(race())});
        seen.running = snapshot({json.dumps(race(0, busy=True))});
        seen.done = snapshot({json.dumps(race(0, result=OVERSPENT))});""")
    assert seen["ready"] == {"shows": "", "next": "on"}
    assert seen["pressed"] == seen["stale"] == seen["running"] == {"shows": "Running…", "next": "off"}
    assert seen["done"] == {"shows": json.dumps(OVERSPENT, ensure_ascii=False, separators=(",", ":")), "next": "on"}


def test_a_step_that_finishes_before_any_snapshot_shows_its_result_straight_away():
    seen = panel(f"""
        snapshot({json.dumps(race())});
        await nextStep();
        seen.done = snapshot({json.dumps(race(0, result=OVERSPENT))});""")
    assert seen["done"]["next"] == "on" and "OVERSPENT" in seen["done"]["shows"]


def test_a_press_the_control_plane_refuses_does_not_claim_a_step_is_running():
    seen = panel(f"""
        snapshot({json.dumps(race(0, result=OVERSPENT))});
        accepted = false;  // "a step is still running", or this stage has no control
        await nextStep();
        seen.after = panel();""")
    assert seen["after"]["next"] == "on" and "OVERSPENT" in seen["after"]["shows"]


def test_the_panel_stops_waiting_for_a_snapshot_after_a_few_seconds():
    """If no snapshot ever shows the step, the stage goes back to what the control plane says."""
    seen = panel(f"""
        snapshot({json.dumps(race())});
        await nextStep();
        clock += 7000;
        seen.later = snapshot({json.dumps(race())});""")
    assert seen["later"] == {"shows": "", "next": "on"}


def test_a_scenario_that_is_getting_ready_still_says_preparing():
    seen = panel(f"seen.preparing = snapshot({json.dumps(race(busy=True))});")
    assert seen["preparing"] == {"shows": "Preparing…", "next": "off"}


CHAR = 7.2  # the width of one character of the chart's 12 px monospace labels
CRASH, RECOVERED, RECOVER = "CRASH · US-EAST-1B-N2", "RECOVERED IN 5.4 S", "RECOVER · US-EAST-1B-N2"


def placed(marks: list[tuple[float, str]]) -> list[dict | None]:
    """Where the chart puts the labels of these markers, given as (x of the marker's line, its text), oldest first."""
    return run("\n".join(cut(start) for start in ("const LABEL_GAP", "function placeLabels(")) +
               f"\nconst marks = {json.dumps([{'x': x, 'text': text} for x, text in marks])};\n"
               f"console.log(JSON.stringify(placeLabels(marks, (text) => text.length * {CHAR})));\n")


def overlapping(marks, places) -> list:
    """Pairs of drawn labels that share a row and run into each other. A label ends at its x and extends to the left."""
    drawn = [(p["row"], p["x"] - len(text) * CHAR, p["x"]) for (_, text), p in zip(marks, places) if p]
    return [(a, b) for i, a in enumerate(drawn) for b in drawn[i + 1:] if a[0] == b[0] and a[1] < b[2] and b[1] < a[2]]


def test_a_crash_and_a_recover_seconds_apart_both_get_a_readable_label():
    """Kill a node, talk for twenty seconds, bring it back: the two markers are about 190 px apart on the stage. Their
    labels used to be drawn towards each other, one printed over the other."""
    marks = [(700, CRASH), (888, RECOVER)]
    places = placed(marks)
    assert all(places) and not overlapping(marks, places)


def test_the_recovery_time_is_labelled_beside_the_crash_it_belongs_to():
    marks = [(700, CRASH), (745, RECOVERED), (888, RECOVER)]  # payments were back 5.4 s after the crash
    places = placed(marks)
    assert all(places) and not overlapping(marks, places)
    assert [p["row"] for p in places] == [0, 1, 0]


def test_labels_keep_their_rows_while_the_chart_scrolls():
    """Every marker moves left at the same speed. A label that changed sides or rows on the way would jump about on the
    big screen."""
    marks = [(700, CRASH), (745, RECOVERED), (888, RECOVER)]
    rows = [[p and p["row"] for p in placed([(x - shift, text) for x, text in marks])] for shift in range(0, 700, 50)]
    assert all(r == rows[0] for r in rows)


def test_a_label_with_no_room_on_any_row_is_left_out():
    """Its line is still drawn. Three rows fit above the traffic; a fourth would cover it."""
    marks = [(700 + 10 * i, CRASH) for i in range(4)]
    assert [p and p["row"] for p in placed(marks)] == [0, 1, 2, None]

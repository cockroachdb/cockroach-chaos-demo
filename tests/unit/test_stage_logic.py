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
    if not start.startswith("function"):
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

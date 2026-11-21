"""The presenter's own rules, run in node: functions are cut out of presenter.js as they are and given a page that is
just enough to carry them."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PRESENTER_JS = (Path(__file__).resolve().parents[2] / "web" / "presenter" / "presenter.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def cut(start: str) -> str:
    """The statement or function of presenter.js that begins with `start`, through its closing brace or semicolon."""
    at = PRESENTER_JS.index(start)
    if not start.startswith(("function", "async function")):
        return PRESENTER_JS[at:PRESENTER_JS.index("\n", at)]
    depth, i = 0, PRESENTER_JS.index("{", at)
    while True:
        depth += {"{": 1, "}": -1}.get(PRESENTER_JS[i], 0)
        i += 1
        if depth == 0:
            return PRESENTER_JS[at:i]


PAGE = """
const els = {};
const $ = (id) => els[id] || (els[id] = { textContent: '', disabled: false, hidden: false, dataset: {}, children: [],
                                          replaceChildren(...made) { this.children = made; } });
const h = (tag, attrs, ...children) => ({ tag, attrs, children: children.flat(Infinity).filter((c) => c !== null && c !== false) });
const G = { rebuild: 'Rebuild: delete this cluster and build a fresh one from the preset. All data is wiped.' };
const state = { cluster: { preset: 'aws' } };
const presets = ['aws', 'gcp'].map((name) => ({ name, display_name: 'Acme Payments', survival_goal: 'region',
                                                regions: [{ name: 'r1', nodes: 2 }, { name: 'r2', nodes: 2 }] }));
let lastPresetRender = '';
function rebuild() {}
const texts = (node) => (typeof node === 'string' ? [node] : (node.children || []).flatMap(texts));
const offered = () => els.presets.children.flatMap(texts).filter((t) => t === 'Rebuild with this').length;
const seen = {};
"""
CODE = "\n".join(cut(start) for start in ("let rebuildAllowed", "const REBUILD_OFF", "function applyRebuildLock(", "function renderPresets("))


def presenter(js: str) -> dict:
    done = subprocess.run(["node", "-e", PAGE + CODE + "\n" + js + "\nconsole.log(JSON.stringify(seen));\n"],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_every_preset_offers_a_rebuild_on_a_laptop():
    seen = presenter("""
        $('preset-rebuild').disabled = false;  // a custom preset has just been saved
        applyRebuildLock();
        renderPresets();
        seen.offered = offered();
        seen.custom = $('preset-rebuild').disabled;
        seen.hint = $('rebuild-hint').textContent;
        seen.model = Boolean($('model-row').hidden);""")
    assert seen == {"offered": 2, "custom": False, "hint": "", "model": False}  # the page is left as it is


def test_an_instance_with_rebuild_switched_off_does_not_offer_one_and_says_why():
    """The API refuses a rebuild there. A button that only ever answers "refused" is worse than no button and a reason."""
    seen = presenter("""
        renderPresets();
        seen.before = offered();
        $('preset-rebuild').disabled = false;
        rebuildAllowed = false;
        applyRebuildLock();
        seen.offered = offered();
        seen.custom = $('preset-rebuild').disabled;
        seen.hint = $('rebuild-hint').textContent;
        seen.tip = $('preset-rebuild').dataset.tip;
        seen.model = $('model-row').hidden;
        seen.presets = els.presets.children.length;""")
    assert seen["before"] == 2 and seen["offered"] == 0 and seen["custom"] is True
    assert seen["model"] is True  # the deployment model is a choice for a rebuild: with none on offer it does nothing
    assert seen["presets"] == 2  # the presets are still listed: what runs here and what else there is
    assert "switched off on this instance" in seen["hint"] and "wipe every database" in seen["hint"]
    assert seen["tip"] == seen["hint"]

"""The hover tooltips of the stage and the presenter (web/shared/tips.js), run in node against a page that is just
enough to carry them: the listeners they register, one tooltip box, and timers a test fires by hand."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

TIPS_JS = Path(__file__).resolve().parents[2] / "web" / "shared" / "tips.js"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

PAGE = """
const handlers = {};
const box = { className: '', hidden: true, textContent: '', style: {}, offsetWidth: 100, offsetHeight: 20,
              setAttribute() {}, contains: () => false };
const rect = () => ({ left: 300, top: 600, width: 80, height: 30, bottom: 630 });
globalThis.document = {
  createElement: () => box,
  addEventListener: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); },
  body: { append() {}, getBoundingClientRect: () => ({ left: 0, top: 0, width: 1920, height: 1080 }) },
  contains: () => true,
  elementFromPoint: () => null,
};
globalThis.MutationObserver = class { observe() {} };
const timers = new Map();
let nextTimer = 1;
globalThis.setTimeout = (fn) => { timers.set(nextTimer, fn); return nextTimer++; };
globalThis.clearTimeout = (id) => { timers.delete(id); };
const later = () => { const due = [...timers.values()]; timers.clear(); due.forEach((fn) => fn()); };  // the delay passes

const isolate = { dataset: { tip: 'Isolate: cut this region off' }, closest() { return this; }, getBoundingClientRect: rect };
const crash = { closest: () => null };  // an item of the node menu: it has no tooltip
const fire = (type, target, x, y) => { for (const fn of handlers[type] || []) fn({ target, clientX: x, clientY: y }); };
const tooltip = () => (box.hidden ? null : box.textContent);
const seen = {};
"""


def tips(js: str) -> dict:
    """Play pointer events through the tooltips: {name: the tooltip's text at that moment, or None}."""
    script = (f"const {{ initTips }} = await import({json.dumps(TIPS_JS.as_uri())});\n" + PAGE +
              "initTips();\n" + js + "\nconsole.log(JSON.stringify(seen));\n")
    done = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_moving_the_pointer_onto_a_button_shows_its_tooltip():
    seen = tips("""
        fire('mousemove', isolate, 380, 640);
        fire('mouseover', isolate, 380, 640);
        seen.before = tooltip();
        later();
        seen.after = tooltip();""")
    assert seen == {"before": None, "after": "Isolate: cut this region off"}


def test_a_button_that_ends_up_under_a_resting_pointer_does_not_show_its_tooltip():
    """Click "Crash" in a node's menu and the menu closes over the region's buttons. The pointer has not moved: the
    button came to it. Its tooltip used to open across the node tiles, over the node that had just been killed, and
    stay there while the presenter talked."""
    seen = tips("""
        fire('mousemove', crash, 380, 640);
        fire('pointerdown', crash, 380, 640);
        fire('mouseover', isolate, 380, 640);   // the menu is gone: the browser reports what is under the pointer now
        fire('mousemove', isolate, 380, 640);   // ... with a move that goes nowhere
        later();
        seen.resting = tooltip();
        fire('mousemove', isolate, 381, 641);   // a hand on a trackpad is never quite still
        later();
        seen.trembling = tooltip();""")
    assert seen == {"resting": None, "trembling": None}


def test_the_tooltip_comes_back_once_the_pointer_really_moves():
    seen = tips("""
        fire('pointerdown', crash, 380, 640);
        fire('mouseover', isolate, 380, 640);
        later();
        fire('mousemove', isolate, 396, 640);   // moved on purpose, still over the same button: no new mouseover comes
        seen.moving = tooltip();
        later();
        seen.moved = tooltip();""")
    assert seen == {"moving": None, "moved": "Isolate: cut this region off"}


def test_a_click_closes_the_tooltip_that_is_showing():
    seen = tips("""
        fire('mouseover', isolate, 380, 640);
        later();
        seen.shown = tooltip();
        fire('pointerdown', isolate, 380, 640);
        seen.pressed = tooltip();""")
    assert seen == {"shown": "Isolate: cut this region off", "pressed": None}

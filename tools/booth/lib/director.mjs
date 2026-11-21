// What a scene calls. A scene is `export default { id, title, premise, prepare?(d), run(d), outro?(facts) }`.
// The director moves the camera and the pointer on the real stage, waits on the real cluster, and writes down what
// happened and when: captions, shots, clicks, waits worth fast-forwarding, and the numbers measured in this take.
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { FRAME, cameraFor, readingSeconds, wrapCaption } from './timeline.mjs';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** The take can't be used: a step failed, or a caption would claim something that didn't happen. */
export class TakeInvalid extends Error {}

const FULL = { x: 0, y: 0, w: 1920, h: 1080 };
const SHOTS = {
  top: ['#kpis', '#regions'], kpis: ['#kpis'], regions: ['#regions'], chart: ['#chart-box'], scenario: ['#scenario'],
  events: ['#side'], bottom: ['#chart-box', '#scenario', '#side'],
};
const KEYS = { Escape: 27, Enter: 13, ArrowRight: 39 };

export function createDirector({ page, panel, clock, rehearse = false, stillsDir = null, log = () => {} }) {
  const events = []; const facts = {}; const checks = [];
  let stills = 0;
  let readableAt = 0;   // when the caption on screen has been up long enough to read
  const pointer = { x: 1500, y: 800, shown: false, timer: null };
  // Rehearsals keep the order and the cluster's real waits, but don't sit through the reading time.
  const pace = rehearse ? 0.25 : 1;
  const hold = (seconds) => sleep(rehearse ? Math.max(400, seconds * 250) : seconds * 1000);
  /** A caption shown with wait: false still gets its reading time before the next one replaces it. */
  const letItBeRead = async () => { const left = readableAt - clock(); if (left > 0) await sleep(left * 1000); };
  const fill = (text) => text.replace(/\{([^{}]+)\}/g, (_, key) => {
    if (!(key in facts)) throw new Error(`caption uses {${key}} before d.fact('${key}', ...)`);
    return String(facts[key]);
  });
  const still = async (name) => {
    if (!stillsDir) return;
    const { data } = await page.send('Page.captureScreenshot', { format: 'png' });
    const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 40);
    writeFileSync(join(stillsDir, `${String(++stills).padStart(2, '0')}-${slug}.png`), Buffer.from(data, 'base64'));
  };

  const showPointer = async () => {
    clearTimeout(pointer.timer);
    if (pointer.shown) return;
    await page.evaluate(`(__booth.ptr(${pointer.x}, ${pointer.y}), __booth.show(true))`);
    pointer.shown = true;
    await sleep(260);
  };
  const restPointer = () => {
    clearTimeout(pointer.timer);
    pointer.timer = setTimeout(() => {   // idle: fade the drawn pointer and park the real mouse, so nothing stays hovered
      pointer.shown = false;
      page.evaluate('__booth.show(false)').catch(() => {});
      page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 3, y: 3 }).catch(() => {});
    }, 2200);
  };
  /** Glide along a slight arc, eased, placing real mouse-move events on the way so hover states follow. */
  const glide = async (x1, y1) => {
    await showPointer();
    const { x: x0, y: y0 } = pointer;
    const dx = x1 - x0, dy = y1 - y0, dist = Math.hypot(dx, dy);
    if (dist >= 2) {
      const ms = Math.min(1100, Math.max(350, 250 + dist * 0.85));
      const bulge = Math.min(46, dist * 0.09) * (dx >= 0 ? -1 : 1);
      const nx = -dy / dist, ny = dx / dist;
      const started = performance.now();
      for (;;) {
        const p = Math.min(1, (performance.now() - started) / ms);
        const e = p < 0.5 ? 4 * p * p * p : 1 - ((-2 * p + 2) ** 3) / 2;
        const off = Math.sin(Math.PI * e) * bulge;
        const x = x0 + dx * e + nx * off, y = y0 + dy * e + ny * off;
        await Promise.all([page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y }), page.evaluate(`__booth.ptr(${x.toFixed(1)}, ${y.toFixed(1)})`)]);
        if (p >= 1) break;
        await sleep(8);
      }
    }
    pointer.x = x1; pointer.y = y1;
  };
  const target = async (sel, what) => {
    const p = await page.evaluate(`__booth.point(${JSON.stringify(sel)})`);
    if (!p) throw new Error(`${what} "${sel}": it isn't on screen`);
    if (p.x < 0 || p.x > FRAME.w || p.y < 0 || p.y > FRAME.h - FRAME.band) throw new Error(`${what} "${sel}": it is outside the picture; move the camera first`);
    return p;
  };

  const d = {
    events, facts, checks, rehearse,

    /** Show a caption from now on, and (unless wait is false) give the viewer time to read it. */
    async say(text, { hold: seconds, wait = true } = {}) {
      const caption = fill(text);
      const lines = wrapCaption(caption);
      await letItBeRead();
      events.push({ t: clock(), type: 'say', text: caption });
      log(`"${caption}"`);
      const reading = seconds ?? readingSeconds(caption);
      readableAt = clock() + reading * pace;
      if (rehearse) await page.evaluate(`__booth.caption(${JSON.stringify(lines)})`);
      if (wait) await hold(reading);
      if (rehearse) await still(caption);
    },
    async clear() {
      await letItBeRead();
      events.push({ t: clock(), type: 'clear' });
      if (rehearse) await page.evaluate('__booth.caption([])');
    },
    /** Time passing with nothing said (shortened in rehearsals). */
    pause: (seconds) => hold(seconds),
    /** Real time passing, also in rehearsals: for the cluster, not for the viewer. */
    settle: (seconds) => sleep(seconds * 1000),

    /** Move the camera: 'wide', a named area (top, kpis, regions, chart, scenario, events, bottom), 'region:<name>',
     *  'node:<name>', 'kpi:<nodes|unavail|tps|p99|money|recovery>', any CSS selector, several joined with '+', or a
     *  stage rectangle {x, y, w, h}. */
    async shot(where, { ms = 900, pad = 28, max = 2.6 } = {}) {
      let rect = where;
      if (where === 'wide') rect = FULL;
      else if (typeof where === 'string') {
        const found = await page.evaluate(`__booth.rect(${JSON.stringify(SHOTS[where] || where.split('+'))})`);
        if (!found) throw new Error(`shot "${where}": nothing on screen matches`);
        rect = { x: found.x - pad, y: found.y - pad, w: found.w + 2 * pad, h: found.h + 2 * pad };
      }
      await page.evaluate(`__booth.camera(${JSON.stringify(cameraFor(rect, FRAME, max))}, ${ms})`);
      events.push({ t: clock(), type: 'shot', where: typeof where === 'string' ? where : 'rect', ms });
      await sleep(ms + 200);
    },

    async hover(sel) {
      const p = await target(sel, 'hover');
      await glide(p.x, p.y);
      clearTimeout(pointer.timer);
    },
    /** Take the pointer off whatever it hovers (its tooltip closes) and let it fade. The real mouse moves to the
     *  recorder's own corner element (see stageset.js), where the app has nothing to react to. */
    async unhover() {
      clearTimeout(pointer.timer);
      pointer.shown = false;
      await page.evaluate('__booth.show(false)');
      await page.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 3, y: 3 });
      await sleep(260);
    },
    async click(sel) {
      const p = await target(sel, 'click');
      if (p.disabled) throw new Error(`click "${sel}": the button is disabled`);
      await glide(p.x, p.y);
      await sleep(160);
      await page.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: p.x, y: p.y, button: 'left', buttons: 1, clickCount: 1 });
      await page.evaluate(`__booth.down(${p.x}, ${p.y})`);
      await sleep(90);
      await page.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: p.x, y: p.y, button: 'left', buttons: 0, clickCount: 1 });
      await page.evaluate('__booth.up()');
      events.push({ t: clock(), type: 'click', sel });
      log(`click ${sel}`);
      restPointer();
    },
    async key(key) {
      const code = KEYS[key] || key.toUpperCase().charCodeAt(0);
      await page.send('Input.dispatchKeyEvent', { type: 'keyDown', key, windowsVirtualKeyCode: code });
      await page.send('Input.dispatchKeyEvent', { type: 'keyUp', key, windowsVirtualKeyCode: code });
    },

    /** Wait until fn(state) is truthy and return it. With lapse, a long wait is marked for fast-forward (its first
     *  `lead` and last `tail` seconds stay at normal speed). */
    async until(fn, { timeout = 180, lapse = false, lead = 2.5, tail = 1.5, what = 'the cluster' } = {}) {
      const started = clock(); const deadline = Date.now() + timeout * 1000;
      for (;;) {
        const seen = await panel.state();
        const value = fn(seen);
        if (value) {
          // The stage redraws a moment after the API answers: wait until it shows this state.
          if (!(await page.evaluate(`__booth.drawn(${Number(seen.ts) || 0}, 15000)`))) throw new TakeInvalid(`the stage did not show ${what} within 15 s of it happening`);
          const finished = clock();
          if (lapse && finished - tail > started + lead) events.push({ t: started + lead, type: 'lapse', end: finished - tail });
          return value;
        }
        if (Date.now() > deadline) throw new TakeInvalid(`timed out after ${timeout} s waiting for ${what}`);
        await sleep(500);
      }
    },

    /** Click the stage's own Next button. -> a run with done(): wait for the step and return { label, result, seconds }.
     *  Captions said between next() and done() play at normal speed; only the wait inside done() can fast-forward. */
    async next() {
      const before = await panel.scenario();
      if (!before) throw new TakeInvalid('no scenario is running');
      const index = before.step + 1;
      if (index >= before.steps.length) throw new Error('that was the last step');
      await d.click('#sc-next');
      const started = clock();
      events.push({ t: started, type: 'step', index, of: before.steps.length, label: before.steps[index].label });
      log(`step ${index + 1}/${before.steps.length}: ${before.steps[index].label}`);
      return {
        index, label: before.steps[index].label,
        async done({ lapse = true, timeout = 900, lead = 2.5, tail = 1.5 } = {}) {
          const waiting = Math.max(clock(), started + lead); const deadline = Date.now() + timeout * 1000;
          for (;;) {
            const cur = await panel.scenario();
            if (!cur) throw new TakeInvalid('the scenario ended by itself');
            const step = cur.steps[index];
            if (cur.step >= index && !cur.busy && step.status !== 'running') {
              if (step.error || step.status === 'failed') throw new TakeInvalid(`step "${step.label}" failed: ${step.error}`);
              // The stage shows "Running…" a moment longer than the API does: wait until its panel shows the result.
              if (!(await page.evaluate(`__booth.stepShown(${index + 1}, 15000)`))) throw new TakeInvalid(`the stage did not show the result of "${step.label}" within 15 s`);
              const finished = clock();
              if (lapse && finished - tail > waiting) events.push({ t: waiting, type: 'lapse', end: finished - tail });
              log(`  done in ${(finished - started).toFixed(1)} s: ${JSON.stringify(step.result || {})}`);
              return { index, label: step.label, result: step.result || {}, seconds: finished - started };
            }
            if (Date.now() > deadline) throw new TakeInvalid(`step "${before.steps[index].label}" did not finish in ${timeout} s`);
            await sleep(400);
          }
        },
      };
    },
    async step(opts) { return (await d.next()).done(opts); },

    /** A number measured in this take; captions use it as {key}. */
    fact(key, value) { facts[key] = value; events.push({ t: clock(), type: 'fact', key, value }); log(`fact ${key} = ${value}`); return value; },
    /** What a caption is about to claim must be true in this take, or the take is thrown away. */
    check(ok, claim) {
      checks.push({ t: clock(), ok: Boolean(ok), claim });
      if (!ok) throw new TakeInvalid(`this take does not show: ${claim}`);
    },
    label(text) { events.push({ t: clock(), type: 'label', text }); },
    api: (method, path, body) => panel.api(method, path, body),
    state: () => panel.state(),
    scenario: () => panel.scenario(),
    /** Forget what happened so far (used after the off-camera preparation); facts stay. */
    rewind() { events.length = 0; },
  };
  return d;
}

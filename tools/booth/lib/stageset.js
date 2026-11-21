// Evaluated inside the stage page by the booth recorder. It adds only what a recording needs around the real app:
//   - a strip of page background at the bottom where captions go (composited later), so the app never shows there
//   - a drawn pointer that sits where the recorder's real mouse events are
//   - a camera: the app's own #stage transform (the app already scales #stage; its tooltips and canvases follow)
//   - a tick: Chrome hands over a frame only when the picture changes, and its capture sampler locks onto whatever
//     small region is animating: when the whole picture then changes (a camera move, a menu opening) it rejects
//     frames for a quarter of a second. A full-frame layer that draws nothing visible (1/255 alpha) and moves by a
//     hundredth of a pixel keeps the whole frame "in motion": frames arrive at a steady rate, camera moves included,
//     and a gap always means the capture really fell behind.
// Nothing inside the app is changed.
(() => {
  if (window.__booth) return 'already';
  const BAND = 150;
  const css = document.createElement('style');
  css.textContent = `
    #booth-band { position: fixed; left: 0; right: 0; bottom: 0; height: ${BAND}px; z-index: 9000; pointer-events: none;
      background: var(--bg); border-top: 1px solid var(--line); display: flex; align-items: center; justify-content: center; }
    #booth-caption { font: 400 42px/1.24 var(--font); letter-spacing: -.4px; color: var(--fg); text-align: center; }
    #booth-ptr { position: fixed; left: 0; top: 0; z-index: 9999; pointer-events: none; opacity: 0; transition: opacity .25s ease;
      transform: translate(-100px, -100px); filter: drop-shadow(0 2px 3px rgba(0, 0, 0, .5)); }
    #booth-ptr.on { opacity: 1; }
    #booth-ptr svg { display: block; margin: -2.5px 0 0 -6.25px; transform-origin: 6.25px 2.5px; transition: transform .08s ease; }
    #booth-ptr.down svg { transform: scale(.86); }
    .booth-ring { position: fixed; z-index: 9998; pointer-events: none; width: 40px; height: 40px; margin: -20px 0 0 -20px; border-radius: 50%;
      border: 3px solid var(--brand); animation: booth-ring .55s ease-out forwards; }
    @keyframes booth-ring { from { transform: scale(.4); opacity: .95; } to { transform: scale(1.9); opacity: 0; } }
    #booth-tick { position: fixed; inset: 0; z-index: 9001; pointer-events: none; background: rgba(128, 128, 128, .004);
      animation: booth-tick .5s linear infinite alternate; }
    @keyframes booth-tick { from { transform: translateX(0); } to { transform: translateX(.01px); } }
    /* Where the mouse goes to stop hovering: over this corner the app sees an element with no tooltip and no hover state. */
    #booth-park { position: fixed; left: 0; top: 0; width: 6px; height: 6px; z-index: 9999; }
  `;
  document.head.append(css);
  const band = document.createElement('div'); band.id = 'booth-band';
  const cap = document.createElement('div'); cap.id = 'booth-caption'; band.append(cap);   // rehearsal preview only
  const ptr = document.createElement('div'); ptr.id = 'booth-ptr';
  ptr.innerHTML = '<svg width="30" height="30" viewBox="0 0 24 24"><path d="M5 2 L5 19.5 L9.4 15.4 L12.3 22 L14.9 20.9 L12 14.4 L18 14.4 Z" fill="#000" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/></svg>';
  const tick = document.createElement('div'); tick.id = 'booth-tick';
  const park = document.createElement('div'); park.id = 'booth-park';
  document.body.append(band, ptr, tick, park);
  const stage = document.getElementById('stage');
  const CENTRE = { x: 960, y: 540 };                 // the stage's transform origin
  const VIEW = { x: 960, y: (1080 - BAND) / 2 };     // the middle of the area above the caption band
  let cam = { scale: 1, tx: 0, ty: 0 }; let moving = 0;

  const find = (sel) => {
    if (sel.startsWith('region:')) return [...document.querySelectorAll('.region')].find((r) => r.querySelector('.rname')?.textContent.trim() === sel.slice(7)) || null;
    if (sel.startsWith('node:')) return document.querySelector(`.node[data-name="${sel.slice(5)}"]`);
    if (sel.startsWith('kpi:')) return document.getElementById(`k-${sel.slice(4)}`)?.parentElement || null;
    if (sel.startsWith('text:')) {   // text:<css>|<what it starts with>, e.g. text:#menu button|Crash
      const [cssSel, text] = sel.slice(5).split('|');
      return [...document.querySelectorAll(cssSel)].find((el) => el.textContent.trim().startsWith(text)) || null;
    }
    return document.querySelector(sel);
  };
  const shown = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };

  // What the stage has been told so far: it redraws on each message of this same stream, a second or two after the
  // control plane's API already answers with the new state. The recorder waits on this before a caption describes
  // something, so a caption never gets ahead of the picture.
  let seenTs = 0;
  const feed = new EventSource('/api/events');
  feed.onmessage = (m) => { try { seenTs = JSON.parse(m.data).ts || seenTs; } catch { /* not a snapshot */ } };

  window.__booth = {
    /** Resolves once the stage has drawn a snapshot at least as new as `ts` (true), or after `ms` (false). */
    drawn(ts, ms) {
      return new Promise((resolve) => {
        const started = performance.now();
        const check = () => {
          if (seenTs >= ts) requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
          else if (performance.now() - started > ms) resolve(false);
          else setTimeout(check, 50);
        };
        check();
      });
    },
    /** Resolves once the scenario panel shows step `n` (1-based) finished: its result, not "Running…". */
    stepShown(n, ms) {
      return new Promise((resolve) => {
        const started = performance.now();
        const shown = () => { const step = document.getElementById('sc-step'); return Boolean(step && step.textContent.startsWith(`Step ${n} `) && !document.querySelector('#sc-result .busy')); };
        const check = () => {
          if (shown()) requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
          else if (performance.now() - started > ms) resolve(false);
          else setTimeout(check, 50);
        };
        check();
      });
    },
    /** The union of the elements' boxes in stage pixels (as if the camera were not applied); null if none is shown. */
    rect(sels) {
      const s = stage.getBoundingClientRect(); const k = s.width / 1920;
      const boxes = sels.map(find).filter((el) => el && shown(el)).map((el) => el.getBoundingClientRect());
      if (!boxes.length) return null;
      const l = Math.min(...boxes.map((b) => b.left)), t = Math.min(...boxes.map((b) => b.top));
      const r = Math.max(...boxes.map((b) => b.right)), b = Math.max(...boxes.map((x) => x.bottom));
      return { x: (l - s.left) / k, y: (t - s.top) / k, w: (r - l) / k, h: (b - t) / k };
    },
    /** Where to click an element: its centre in frame pixels; null when it isn't shown. */
    point(sel) {
      const el = find(sel);
      if (!el || !shown(el)) return null;
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2, disabled: Boolean(el.disabled) };
    },
    /** Move the camera to c = { scale, tx, ty } over ms. Animated frame by frame, not with a CSS transition: for a
     *  transition Chrome first redraws the whole stage at the largest scale of the move, which freezes the picture
     *  for a third of a second at the start of every zoom-in. Scale moves geometrically and the point in view
     *  linearly, so the zoom feels even. */
    camera(c, ms) {
      const run = ++moving; const from = cam;
      const put = (k) => { cam = k; stage.style.transform = `translate(${k.tx}px, ${k.ty}px) scale(${k.scale})`; };
      if (!ms) { put(c); return; }
      const focus = (k) => ({ x: CENTRE.x + (VIEW.x - CENTRE.x - k.tx) / k.scale, y: CENTRE.y + (VIEW.y - CENTRE.y - k.ty) / k.scale });
      const f0 = focus(from), f1 = focus(c); const started = performance.now();
      const step = (now) => {
        if (run !== moving) return;
        const p = Math.min(1, (now - started) / ms);
        if (p >= 1) { put(c); return; }
        const e = p < 0.5 ? 4 * p * p * p : 1 - ((-2 * p + 2) ** 3) / 2;
        const scale = from.scale * (c.scale / from.scale) ** e;
        const fx = f0.x + (f1.x - f0.x) * e, fy = f0.y + (f1.y - f0.y) * e;
        put({ scale, tx: VIEW.x - CENTRE.x - scale * (fx - CENTRE.x), ty: VIEW.y - CENTRE.y - scale * (fy - CENTRE.y) });
        requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    },
    ptr(x, y) { ptr.style.transform = `translate(${x}px, ${y}px)`; },
    show(on) { ptr.classList.toggle('on', on); },
    down(x, y) {
      ptr.classList.add('down');
      const ring = document.createElement('div'); ring.className = 'booth-ring';
      ring.style.left = `${x}px`; ring.style.top = `${y}px`;
      document.body.append(ring); setTimeout(() => ring.remove(), 700);
    },
    up() { ptr.classList.remove('down'); },
    caption(lines) { cap.replaceChildren(...lines.map((l) => { const d = document.createElement('div'); d.textContent = l; return d; })); },
  };
  return 'ok';
})()

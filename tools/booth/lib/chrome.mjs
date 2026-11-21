// Headless Chrome for the booth recorder: launch it and talk to one page over the DevTools protocol.
// Needs Node >= 22 (built-in WebSocket) and Google Chrome or Chromium. No npm packages.
import { spawn } from 'node:child_process';
import { existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROMES = [process.env.CHROME, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser'];

/** -> { page, close() }. page: send(method, params), on(method, fn) -> off(), evaluate(expression), goto(url). */
export async function launch({ width = 1920, height = 1080 } = {}) {
  const chromePath = CHROMES.find((p) => p && existsSync(p));
  if (!chromePath) throw new Error('Chrome not found (set CHROME=/path/to/chrome)');
  const profile = join(tmpdir(), `booth-chrome-${process.pid}-${Date.now()}`);
  // Scale factor 1: frames are exactly width x height. The timer and renderer flags keep the page running at full
  // rate although no window is ever in front.
  const chrome = spawn(chromePath, ['--headless=new', '--remote-debugging-port=0', '--hide-scrollbars', '--force-device-scale-factor=1',
    `--window-size=${width},${height}`, '--autoplay-policy=no-user-gesture-required', '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding', `--user-data-dir=${profile}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const wsUrl = await new Promise((resolve, reject) => {
    let buf = '';
    chrome.stderr.on('data', (d) => { buf += d; const m = buf.match(/DevTools listening on (ws:\/\/\S+)/); if (m) resolve(m[1]); });
    chrome.on('exit', () => reject(new Error('Chrome exited before it was ready')));
  });
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = () => reject(new Error('could not connect to Chrome')); });

  let id = 0;
  const pending = new Map();
  const listeners = new Map();   // method -> Set of fn(params)
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); return; }
    for (const fn of listeners.get(msg.method) || []) fn(msg.params);
  };
  let closed = false;
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    if (closed) { reject(new Error(`${method}: Chrome is closed`)); return; }
    const i = ++id;
    pending.set(i, (msg) => (msg.error ? reject(new Error(`${method}: ${msg.error.message}`)) : resolve(msg.result)));
    ws.send(JSON.stringify({ id: i, method, params, sessionId }));
  });

  const { targetId } = await send('Target.createTarget', { url: 'about:blank', newWindow: true });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  const page = {
    send: (method, params) => send(method, params, sessionId),
    on(method, fn) {
      if (!listeners.has(method)) listeners.set(method, new Set());
      listeners.get(method).add(fn);
      return () => listeners.get(method).delete(fn);
    },
    /** Evaluate in the page; promises are awaited; the value comes back by value. Throws what the page threw. */
    async evaluate(expression) {
      const r = await page.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true });
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
      return r.result.value;
    },
    async goto(url) {
      const loaded = new Promise((resolve) => { const off = page.on('Page.loadEventFired', () => { off(); resolve(); }); });
      const r = await page.send('Page.navigate', { url });
      if (r.errorText) throw new Error(`could not open ${url}: ${r.errorText}`);
      await loaded;
    },
  };
  await page.send('Page.enable');
  await page.send('Runtime.enable');
  await page.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 0, mobile: false });

  const close = () => {
    if (closed) return;
    closed = true;
    try { ws.close(); } catch { /* already gone */ }
    chrome.kill();
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* Chrome may still hold files; it is a temp dir */ }
  };
  return { page, close };
}

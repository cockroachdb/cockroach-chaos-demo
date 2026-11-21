#!/usr/bin/env node
// Record one booth scene from the real stage against the running cluster.
//   node tools/booth/record.mjs survive-region --theme dark              # a take: master.mkv + take.json
//   node tools/booth/record.mjs survive-region --theme dark --rehearse   # no video: a still per caption, short holds
// A take lands in recordings/takes/<scene>-<theme>/; render it with tools/booth/render.mjs.
// The control plane must be running (./run.sh up). The cluster is put back the way the take found it, also on Ctrl-C.
// A take starts on a clean stage: the control plane is restarted first (it reconnects to the running cluster in a
// few seconds), so the chart, the event list and "Recovered in" carry nothing from an earlier run. --keep-stage skips it.
import { spawnSync } from 'node:child_process';
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { startCapture } from './lib/capture.mjs';
import { launch } from './lib/chrome.mjs';
import { TakeInvalid, createDirector } from './lib/director.mjs';
import { panel } from './lib/panel.mjs';
import { FRAME, cameraFor } from './lib/timeline.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..', '..');
const TITLE_SECONDS = 4.5;   // the title card sits over the first seconds
const END_SECONDS = 7;       // and the end card over the last
const argv = process.argv.slice(2);
const sceneId = argv.find((a) => !a.startsWith('--'));
const opt = (name, fallback) => { const i = argv.indexOf(`--${name}`); return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : fallback; };
const theme = opt('theme', 'dark');
const rehearse = argv.includes('--rehearse');
const base = opt('url', 'http://localhost:8088').replace(/\/$/, '');
if (!sceneId || !['dark', 'light'].includes(theme)) { console.error('usage: record.mjs <scene> --theme dark|light [--rehearse]'); process.exit(2); }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const stamp = () => new Date().toTimeString().slice(0, 8);
const log = (line) => console.log(`${stamp()} ${line}`);
const scene = (await import(`./scenes/${sceneId}.mjs`)).default;
const p = panel(base);

if (!rehearse && !argv.includes('--keep-stage')) {
  const cli = spawnSync('docker', ['--version']).status === 0 ? 'docker' : 'podman';
  // A previous take may still be settling (its scenario resetting, a survival-goal change finishing): wait for it.
  const was = await p.waitFor(async () => {
    const s = await p.state();
    return !s.scenario && !(s.operation && s.operation.status === 'running') && !(s.workload && s.workload.running) ? s : null;
  }, 600, 'the scenario, cluster operation or Payments that is still running to finish (stop it if it is yours)').catch((e) => { console.error(`not recording: ${e.message}`); process.exit(2); });
  if (spawnSync(cli, ['restart', 'crdb-chaos-control-plane']).status !== 0) { console.error(`could not restart the control plane with ${cli}`); process.exit(2); }
  await p.waitFor(async () => {
    const s = await p.state();
    return s.cluster && s.cluster.status === 'healthy' && s.cluster.live_nodes === was.cluster.total_nodes && s.operation && s.operation.kind === 'adopt' && s.operation.status === 'done';
  }, 180, 'the control plane to reconnect to the cluster');
  log('clean stage: the control plane was restarted and reconnected');
}

// The take starts from a quiet, healthy cluster, and nothing else may be driving it.
const s0 = await p.state();
const problems = [];
if (!s0.cluster || s0.cluster.status !== 'healthy' || s0.cluster.live_nodes !== s0.cluster.total_nodes) problems.push(`the cluster is ${s0.cluster ? s0.cluster.status : 'missing'}`);
if (s0.scenario) problems.push(`scenario "${s0.scenario.title}" is running`);
if (s0.booth && s0.booth.enabled) problems.push('booth mode is on');
if (s0.operation && s0.operation.status === 'running') problems.push(`a cluster operation (${s0.operation.kind}) is running`);
if (problems.length) { console.error(`not recording: ${problems.join('; ')}`); process.exit(2); }
const before = await p.snapshot();

const dir = join(ROOT, 'recordings', rehearse ? 'rehearsal' : 'takes', `${sceneId}-${theme}`);
rmSync(dir, { recursive: true, force: true });
mkdirSync(dir, { recursive: true });
const { page, close } = await launch({ width: FRAME.w, height: FRAME.h });
let capture = null; let t0 = Date.now() / 1000; let restored = false;
const clock = () => Date.now() / 1000 - t0;
const d = createDirector({ page, panel: p, clock, rehearse, stillsDir: rehearse ? dir : null, log });

const putBack = async () => {
  if (restored) return;
  restored = true;
  try { await p.restore(before, log); } catch (e) { console.error(`!! the cluster was NOT fully restored: ${e.message}`); }
};
const bail = async (why, code) => {
  try { log(`${why}: stopping the take and restoring the cluster`); } catch { /* nowhere left to write */ }
  if (capture) await capture.stop().catch(() => {});
  await putBack();
  close();
  process.exit(code);
};
for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(signal, () => bail(signal, 130));
// Whatever kills the take, the cluster is put back. A closed output pipe (`| head`) must not kill it at all.
process.stdout.on('error', () => {});
process.stderr.on('error', () => {});
process.on('uncaughtException', (e) => bail(`crashed (${e.message})`, 1));
process.on('unhandledRejection', (e) => bail(`crashed (${e && e.message ? e.message : e})`, 1));

let error = null; let stats = null; let outro = [];
try {
  await page.goto(`${base}/?theme=${theme}`);
  await page.evaluate(`document.fonts.ready.then(() => new Promise((r) => { const t = setInterval(() => {
    if (document.querySelectorAll('.node').length && document.body.classList.contains('can-control')) { clearInterval(t); r(true); } }, 100); }))`);
  await page.evaluate(readFileSync(join(HERE, 'lib', 'stageset.js'), 'utf8'));
  await page.evaluate(`__booth.camera(${JSON.stringify(cameraFor({ x: 0, y: 0, w: 1920, h: 1080 }))}, 0)`);
  await sleep(1200);
  if (scene.prepare) { log('preparing (off camera)'); await scene.prepare(d); d.rewind(); }
  if (!rehearse) { capture = await startCapture(page, join(dir, 'master.mkv')); t0 = capture.t0(); } else t0 = Date.now() / 1000;
  log(`${rehearse ? 'rehearsing' : 'recording'} "${scene.title}" (${theme})`);
  d.events.push({ t: 0, type: 'title' });
  d.label(scene.title);
  await sleep(rehearse ? 600 : TITLE_SECONDS * 1000);
  await scene.run(d);
  await d.clear();
  outro = scene.outro ? scene.outro(d.facts) : [];
  d.events.push({ t: clock(), type: 'end' });
  await sleep(rehearse ? 600 : END_SECONDS * 1000);
} catch (e) {
  error = e;
} finally {
  if (capture) stats = await capture.stop().catch((e) => { error = error || e; return null; });
  const reasons = [...(stats ? stats.reasons : []), ...(error ? [error.message] : [])];
  const take = { scene: scene.id, title: scene.title, premise: scene.premise, theme, recordedAt: new Date().toISOString(),
    seconds: stats ? stats.seconds : clock(), titleSeconds: TITLE_SECONDS, endSeconds: END_SECONDS, events: d.events, facts: d.facts,
    checks: d.checks, outro, capture: stats, valid: reasons.length === 0, reasons };
  writeFileSync(join(dir, 'take.json'), `${JSON.stringify(take, null, 1)}\n`);
  await putBack();
  close();
  if (error && !(error instanceof TakeInvalid)) console.error(error.stack || error);
  log(`${scene.id} ${theme}: ${take.seconds.toFixed(1)} s` + (stats ? `, ${stats.repeated} repeated of ${stats.written} frames, longest gap ${stats.maxGapMs} ms` : '') +
    `: ${take.valid ? 'VALID' : `INVALID (${reasons.join('; ')})`}`);
  console.log(dir);
  process.exit(take.valid ? 0 : 2);
}

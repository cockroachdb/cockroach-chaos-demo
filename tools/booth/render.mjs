#!/usr/bin/env node
// Turn a take into the booth video: fast-forward the long waits (with a badge), composite the captions, the label
// and the title and end cards, and encode. Captions can be edited in the take or the scene and re-rendered without
// touching the cluster.
//   node tools/booth/render.mjs survive-region --theme dark
// Reads  recordings/takes/<scene>-<theme>/{master.mkv,take.json}
// Writes recordings/final/<scene>-<theme>.mp4 + .srt + .vtt, recordings/clean/<scene>-<theme>.mp4 (no captions),
//        recordings/review/<scene>-<theme>/sheet-N.png (a frame per caption, for checking).
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launch } from './lib/chrome.mjs';
import { FPS, FRAME, buildCues, planLapses, retimer, srt, vtt, wrapCaption } from './lib/timeline.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..', '..');
const argv = process.argv.slice(2);
const sceneId = argv.find((a) => !a.startsWith('--'));
const opt = (name, fallback) => { const i = argv.indexOf(`--${name}`); return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : fallback; };
const theme = opt('theme', 'dark');
if (!sceneId) { console.error('usage: render.mjs <scene> --theme dark|light [--force]'); process.exit(2); }
const name = `${sceneId}-${theme}`;
const takeDir = join(ROOT, 'recordings', 'takes', name);
const take = JSON.parse(readFileSync(join(takeDir, 'take.json'), 'utf8'));
if (!take.valid && !argv.includes('--force')) { console.error(`not rendering ${name}: the take is invalid (${take.reasons.join('; ')}). --force to render anyway.`); process.exit(2); }
const master = join(takeDir, 'master.mkv');
const FADE = 0.22;

const run = (cmd, args) => new Promise((ok, fail) => {
  const p = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'] });
  let out = ''; let err = '';
  p.stdout.on('data', (d) => { out += d; }); p.stderr.on('data', (d) => { err += d; });
  p.on('exit', (code) => (code === 0 ? ok(out) : fail(new Error(`${cmd} exited with ${code}: ${err.slice(-1500)}`))));
});

// ---- the cut: which frames stay, and where everything lands on the output timeline ----
const frames = take.capture.written;
const plan = planLapses(take.events.filter((e) => e.type === 'lapse').map((e) => ({ start: e.t, end: e.end })));
const rt = retimer(plan);
const outSeconds = rt.outFrames(frames) / FPS;
const captions = buildCues(take.events, take.seconds, rt.toOut).map((c) => ({ ...c, lines: wrapCaption(c.text) }));
// The label (scenario title, step n of m) changes when a step starts.
const labelEvents = []; let label = { title: take.title };
for (const e of [...take.events].sort((a, b) => a.t - b.t)) {
  if (e.type === 'label') label = { ...label, title: e.text };
  else if (e.type === 'step') label = { ...label, step: e.index + 1, of: e.of };
  else continue;
  labelEvents.push({ t: e.t, type: 'say', text: JSON.stringify(label) });
}
const endEvent = take.events.find((e) => e.type === 'end');
const endStart = endEvent ? rt.toOut(endEvent.t) : outSeconds;
if (endEvent) labelEvents.push({ t: endEvent.t, type: 'clear' });
const labels = buildCues(labelEvents, take.seconds, rt.toOut).map((c) => ({ ...c, data: JSON.parse(c.text) }))
  .map((c) => ({ ...c, start: Math.max(c.start, take.titleSeconds) })).filter((c) => c.end > c.start);
const badges = plan.map((p) => ({ start: rt.outFrames(p.from) / FPS, end: rt.outFrames(p.to) / FPS, text: `×${p.every}` }));
const facts = take.facts || {};
const note = facts.cluster || 'CockroachDB';

// ---- artwork, drawn by Chrome with the app's own fonts and colours ----
const artDir = join(takeDir, 'art');
mkdirSync(artDir, { recursive: true });
const tokens = readFileSync(join(ROOT, 'web', 'shared', 'tokens.css'), 'utf8')
  .replace(/url\("\/static\/shared\/fonts\/([\w-]+\.woff2)"\)/g, (_, f) => `url("data:font/woff2;base64,${readFileSync(join(ROOT, 'web', 'shared', 'fonts', f)).toString('base64')}")`);
const logo = `data:image/svg+xml;base64,${readFileSync(join(ROOT, 'web', 'shared', 'crl-logo.svg')).toString('base64')}`;
// The end card invites the viewer to run the demo: the QR code (tools/booth/assets/repo-qr.svg) encodes https://REPO.
const REPO = 'github.com/viragtripathi/cockroach-chaos-demo';
const qr = `data:image/svg+xml;base64,${readFileSync(join(HERE, 'assets', 'repo-qr.svg')).toString('base64')}`;
const html = readFileSync(join(HERE, 'overlay.html'), 'utf8').replace('__THEME__', theme).replace('__TOKENS__', () => tokens).replace('__LOGO__', () => logo).replace('__QR__', () => qr);
const { page, close } = await launch({ width: FRAME.w, height: FRAME.h });
const art = async (kind, data) => {
  const file = join(artDir, `${kind}-${createHash('sha1').update(html).update(JSON.stringify(data)).digest('hex').slice(0, 12)}.png`);
  if (!existsSync(file)) {
    const clip = await page.evaluate(`card(${JSON.stringify(kind)}, ${JSON.stringify(data)})`);
    const shot = await page.send('Page.captureScreenshot', { format: 'png', clip: { ...clip, scale: 1 } });
    writeFileSync(file, Buffer.from(shot.data, 'base64'));
  }
  return file;
};
let overlays;   // { file, start, end, y }
try {
  const { frameTree } = await page.send('Page.getFrameTree');
  await page.send('Page.setDocumentContent', { frameId: frameTree.frame.id, html });
  await page.send('Emulation.setDefaultBackgroundColorOverride', { color: { r: 0, g: 0, b: 0, a: 0 } });
  await page.evaluate('document.fonts.ready.then(() => Promise.all([document.fonts.load(\'400 42px Geist\'), document.fonts.load(\'300 92px Geist\'), document.fonts.load(\'500 12px "Geist Mono"\')])).then(() => true)');
  const band = FRAME.h - FRAME.band;
  overlays = [];
  for (const c of labels) overlays.push({ file: await art('label', c.data), start: c.start, end: Math.min(c.end, endStart), y: band });
  for (const c of captions) overlays.push({ file: await art('caption', { lines: c.lines }), start: c.start, end: c.end, y: band });
  const badgeOnly = [];
  for (const c of badges) badgeOnly.push({ file: await art('badge', { text: c.text }), start: c.start, end: c.end, y: band });
  overlays.push(...badgeOnly);
  overlays.push({ file: await art('title', { title: take.title, premise: take.premise, note }), start: 0, end: take.titleSeconds, y: 0, noFadeIn: true });
  if (endEvent && take.outro && take.outro.length) overlays.push({ file: await art('end', { title: take.title, lines: take.outro, note, repo: REPO }), start: endStart + 0.4, end: outSeconds, y: 0, noFadeOut: true });
  overlays.badgeOnly = badgeOnly;
} finally {
  close();
}

// ---- encode ----
const encode = async (file, list) => {
  const inputs = ['-i', master];
  const chains = [`[0:v]select='${rt.selectExpr()}',setpts=N/${FPS}/TB[v0]`];
  let last = 'v0'; let n = 0;
  for (const o of list) {
    const dur = o.end - o.start;
    if (dur < 0.1) continue;
    n++;
    const f = Math.min(FADE, dur / 3);
    inputs.push('-loop', '1', '-framerate', String(FPS), '-t', dur.toFixed(3), '-i', o.file);
    const fades = [o.noFadeIn ? null : `fade=t=in:st=0:d=${f.toFixed(3)}:alpha=1`, o.noFadeOut ? null : `fade=t=out:st=${(dur - f).toFixed(3)}:d=${f.toFixed(3)}:alpha=1`].filter(Boolean);
    chains.push(`[${n}:v]${['format=rgba', ...fades, `setpts=PTS+${o.start.toFixed(3)}/TB`].join(',')}[o${n}]`);
    chains.push(`[${last}][o${n}]overlay=x=0:y=${o.y}:eof_action=pass:format=gbrp[v${n}]`);
    last = `v${n}`;
  }
  // One conversion from the lossless RGB master to video YUV, with the right matrix and range.
  chains.push(`[${last}]scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int+bicubic,format=yuv420p[out]`);
  await run('ffmpeg', ['-y', '-loglevel', 'error', ...inputs, '-filter_complex', chains.join(';'), '-map', '[out]', '-an',
    '-c:v', 'libx264', '-crf', '16', '-preset', 'slow', '-pix_fmt', 'yuv420p', '-r', String(FPS), '-g', String(2 * FPS),
    '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv', '-movflags', '+faststart', file]);
};
for (const d of ['final', 'clean', join('review', name)]) mkdirSync(join(ROOT, 'recordings', d), { recursive: true });
const final = join(ROOT, 'recordings', 'final', `${name}.mp4`);
const clean = join(ROOT, 'recordings', 'clean', `${name}.mp4`);
await encode(final, overlays);
await encode(clean, overlays.badgeOnly);   // no captions or cards; the fast-forward badge stays, so a cut is never hidden
const cueText = captions.map((c) => ({ start: c.start, end: c.end, text: c.lines.join('\n') }));
writeFileSync(final.replace(/\.mp4$/, '.srt'), srt(cueText));
writeFileSync(final.replace(/\.mp4$/, '.vtt'), vtt(cueText));

// ---- review sheets: the middle frame of every caption, nine to a sheet ----
const reviewDir = join(ROOT, 'recordings', 'review', name);
rmSync(reviewDir, { recursive: true, force: true }); mkdirSync(reviewDir, { recursive: true });
const picks = [Math.round(take.titleSeconds / 2 * FPS), ...captions.map((c) => Math.round((c.start + c.end) / 2 * FPS)), Math.round((outSeconds - 1.5) * FPS)];
for (let i = 0; i * 9 < picks.length; i++) {
  const part = picks.slice(i * 9, i * 9 + 9);
  await run('ffmpeg', ['-y', '-loglevel', 'error', '-i', final, '-vf', `select='${part.map((f) => `eq(n,${f})`).join('+')}',scale=640:360,tile=3x3`, '-frames:v', '1', join(reviewDir, `sheet-${i + 1}.png`)]);
}

const probe = JSON.parse(await run('ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_name,width,height,pix_fmt,r_frame_rate,color_space,nb_frames', '-of', 'json', final])).streams[0];
const short = captions.filter((c) => c.end - c.start < 1.5).map((c) => `"${c.text.slice(0, 40)}" ${(c.end - c.start).toFixed(1)} s`);
console.log(`${name}: ${outSeconds.toFixed(1)} s (${take.seconds.toFixed(1)} s recorded, ${plan.length} fast-forward${plan.length === 1 ? '' : 's'}${plan.length ? ` ${plan.map((p) => `×${p.every}`).join(' ')}` : ''}), ` +
  `${captions.length} captions, ${probe.codec_name} ${probe.width}x${probe.height} ${probe.pix_fmt} ${probe.r_frame_rate} ${probe.color_space}, ${(statSync(final).size / 1e6).toFixed(1)} MB`);
if (short.length) console.log(`  captions on screen under 1.5 s: ${short.join('; ')}`);
console.log(final);

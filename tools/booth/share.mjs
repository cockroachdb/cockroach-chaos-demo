#!/usr/bin/env node
// Build recordings/share/: what the team gets. The booth videos in play order (numbered, with their caption files and
// a playlist per theme), one back-to-back reel per theme, and an index.html to click through. Everything in it is
// linked relatively, so the folder works wherever it is copied.
//   node tools/booth/share.mjs
import { spawnSync } from 'node:child_process';
import { existsSync, linkSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const REC = join(ROOT, 'recordings');
const SHARE = join(REC, 'share');
const THEMES = ['dark', 'light'];
// The play order: resilience first, then scale and change, then multi-region placement, then correctness and data tools.
const ORDER = ['survive-zone', 'survive-region', 'zone-vs-region', 'split-brain', 'elastic-scale', 'online-schema-change', 'regional-by-row',
  'follower-reads', 'global-tables', 'data-domicile', 'write-skew', 'time-travel', 'changefeeds'];

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const clock = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, '0')}`;
const run = (cmd, args) => { const r = spawnSync(cmd, args, { encoding: 'utf8' }); if (r.status !== 0) throw new Error(`${cmd} failed: ${r.stderr.slice(0, 600)}`); return r.stdout; };
const duration = (file) => Number(run('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', file]));
const mb = (file) => `${Math.round(statSync(file).size / 1e6)} MB`;

/** [{ start, text }] from an .srt file. */
function cues(file) {
  return readFileSync(file, 'utf8').trim().split(/\n\n+/).map((block) => {
    const [, times, ...text] = block.split('\n');
    const [h, m, s] = times.split(' --> ')[0].replace(',', '.').split(':').map(Number);
    return { start: h * 3600 + m * 60 + s, text: text.join(' ') };
  });
}

const missing = ORDER.flatMap((id) => THEMES.map((t) => join(REC, 'final', `${id}-${t}.mp4`))).filter((f) => !existsSync(f));
if (missing.length) { console.error(`not building the share folder; missing:\n  ${missing.join('\n  ')}`); process.exit(2); }

const scenes = ORDER.map((id, i) => {
  const take = JSON.parse(readFileSync(join(REC, 'takes', `${id}-dark`, 'take.json'), 'utf8'));
  return { id, n: String(i + 1).padStart(2, '0'), title: take.title, premise: take.premise, cues: cues(join(REC, 'final', `${id}-dark.srt`)), videos: {} };
});

const reels = {};
for (const theme of THEMES) {
  const dir = join(SHARE, theme);
  mkdirSync(dir, { recursive: true });
  for (const sc of scenes) {
    for (const ext of ['mp4', 'srt', 'vtt']) {
      const to = join(dir, `${sc.n}-${sc.id}.${ext}`);
      rmSync(to, { force: true });
      linkSync(join(REC, 'final', `${sc.id}-${theme}.${ext}`), to);   // a hard link: no second copy on this disk
    }
    const file = join(dir, `${sc.n}-${sc.id}.mp4`);
    sc.videos[theme] = { href: `${theme}/${sc.n}-${sc.id}.mp4`, seconds: duration(file), size: mb(file) };
  }
  writeFileSync(join(dir, 'playlist.m3u'), `#EXTM3U\n${scenes.map((sc) => `${sc.n}-${sc.id}.mp4`).join('\n')}\n`);
  // The reel: the same videos joined without re-encoding (they share one encoder setting), then decoded once as a check.
  const list = join(tmpdir(), `booth-reel-${theme}.txt`);
  writeFileSync(list, scenes.map((sc) => `file '${join(dir, `${sc.n}-${sc.id}.mp4`)}'\n`).join(''));
  const reel = join(SHARE, `booth-reel-${theme}.mp4`);
  run('ffmpeg', ['-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy', '-movflags', '+faststart', reel]);
  rmSync(list, { force: true });
  const bad = spawnSync('ffmpeg', ['-v', 'error', '-i', reel, '-f', 'null', '-'], { encoding: 'utf8' }).stderr.trim();
  if (bad) throw new Error(`the ${theme} reel does not decode cleanly: ${bad.slice(0, 300)}`);
  const parts = scenes.reduce((sum, sc) => sum + sc.videos[theme].seconds, 0);
  reels[theme] = { href: `booth-reel-${theme}.mp4`, seconds: duration(reel), size: mb(reel) };
  if (Math.abs(reels[theme].seconds - parts) > 1) throw new Error(`the ${theme} reel is ${reels[theme].seconds} s but its parts add up to ${parts} s`);
}

// Each reel starts every scenario at a known time: the page's "in the reel" links jump there.
let at = 0;
for (const sc of scenes) { sc.reelAt = at; at += sc.videos.dark.seconds; }

const sections = scenes.map((sc) => {
  const players = THEMES.map((t) => {
    const v = sc.videos[t]; const base = v.href.replace(/\.mp4$/, '');
    return `<figure><video controls preload="metadata" src="${esc(v.href)}"></video><figcaption>${t} · ${clock(v.seconds)} · ${v.size} · ` +
      `<a href="${esc(v.href)}" download>download</a> · captions <a href="${esc(base)}.srt">.srt</a> <a href="${esc(base)}.vtt">.vtt</a></figcaption></figure>`;
  }).join('');
  return `<section id="${esc(sc.id)}"><h2><span class="n">${sc.n}</span>${esc(sc.title)}</h2><p class="premise">${esc(sc.premise)}</p><div class="players">${players}</div>` +
    `<details><summary>What the captions say</summary><ol>${sc.cues.map((c) => `<li><span>${clock(c.start)}</span>${esc(c.text)}</li>`).join('')}</ol>` +
    '<p class="small">From the dark video. The numbers differ a little in the light one: each was measured in its own recording.</p></details></section>';
}).join('\n');

const reelCards = THEMES.map((t) => `<figure><video controls loop preload="metadata" src="${esc(reels[t].href)}"></video><figcaption>${t} reel · all ${scenes.length} back to back · ` +
  `${clock(reels[t].seconds)} · ${reels[t].size} · <a href="${esc(reels[t].href)}" download>download</a></figcaption></figure>`).join('');

writeFileSync(join(SHARE, 'index.html'), `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>CockroachDB chaos demo: booth videos</title>
<style>
  body { margin: 0 auto; max-width: 1500px; padding: 32px; background: #0b0b0b; color: #f2f1ea; font: 16px/1.5 system-ui, sans-serif; }
  h1 { font-weight: 300; font-size: 40px; margin: 0 0 6px; } h2 { font-weight: 400; font-size: 26px; margin: 0; } h3 { font-weight: 400; font-size: 20px; margin: 28px 0 10px; }
  .lead, .premise, figcaption, .small { color: #a3a29b; } .lead { max-width: 900px; } .premise { margin: 2px 0 14px; } .small { font-size: 13px; }
  .n { display: inline-block; width: 44px; color: #77766f; font: 20px ui-monospace, monospace; }
  section { margin: 36px 0; padding-top: 28px; border-top: 1px solid #262626; }
  .players { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; } figure { margin: 0; } video { width: 100%; aspect-ratio: 16 / 9; border-radius: 10px; background: #000; }
  figcaption { font-size: 13px; margin-top: 6px; } a { color: #c8acff; }
  details { margin-top: 12px; } summary { cursor: pointer; color: #a3a29b; } ol { list-style: none; padding: 0; margin: 10px 0; columns: 2; column-gap: 40px; }
  li { break-inside: avoid; margin-bottom: 6px; } li span { display: inline-block; width: 46px; color: #77766f; font: 13px ui-monospace, monospace; }
  nav { margin-top: 18px; columns: 3; column-gap: 30px; max-width: 1000px; } nav a { display: flex; align-items: baseline; break-inside: avoid; text-decoration: none; padding: 2px 0; } nav a span { flex: none; color: #77766f; font: 13px ui-monospace, monospace; margin-right: 10px; }
  ul.how { padding-left: 20px; max-width: 900px; } ul.how li { margin-bottom: 6px; }
  @media (max-width: 900px) { .players { grid-template-columns: 1fr; } ol, nav { columns: 1; } }
</style></head><body>
<h1>CockroachDB chaos demo: booth videos</h1>
<p class="lead">${scenes.length} scenarios recorded from the demo running against a real ${esc((JSON.parse(readFileSync(join(REC, 'takes', `${ORDER[0]}-dark`, 'take.json'), 'utf8')).facts || {}).cluster || 'CockroachDB cluster')},
each in a dark and a light version. 1920×1080, captions in the picture, no sound.</p>

<h3>Play everything</h3>
<div class="players">${reelCards}</div>
<ul class="how">
  <li><b>On a booth screen:</b> play one reel on repeat (VLC: Playback → Repeat One; QuickTime: View → Loop). Use the dark reel in a dim hall and the light one in a bright one.</li>
  <li><b>To choose or reorder scenarios:</b> open <code>dark/playlist.m3u</code> or <code>light/playlist.m3u</code> in VLC with Repeat All. The files are numbered in play order.</li>
  <li><b>Captions</b> are part of the picture, so nothing needs switching on. The .srt and .vtt files are for players and sites that want a separate caption track.</li>
  <li>This page needs the folder next to it: download the whole folder, then open <code>index.html</code>.</li>
</ul>

<h3>Scenarios, in play order</h3>
<nav>${scenes.map((sc) => `<a href="#${esc(sc.id)}"><span>${sc.n} · ${clock(sc.reelAt)}</span>${esc(sc.title)}</a>`).join('')}</nav>
<p class="small">The time next to each name is where it starts in the reels.</p>
${sections}
</body></html>
`);
console.log(`${SHARE}: ${scenes.length} scenarios × ${THEMES.length} themes, reels ${THEMES.map((t) => `${t} ${clock(reels[t].seconds)}`).join(', ')}, index.html`);

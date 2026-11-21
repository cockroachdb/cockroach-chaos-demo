#!/usr/bin/env node
// Write recordings/index.html: every booth video, dark and light side by side, with its length and its captions.
//   node tools/booth/index.mjs
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const REC = join(ROOT, 'recordings');
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const clock = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, '0')}`;

/** [{ start, text }] from an .srt file. */
function cues(file) {
  if (!existsSync(file)) return [];
  return readFileSync(file, 'utf8').trim().split(/\n\n+/).map((block) => {
    const [, times, ...text] = block.split('\n');
    const [h, m, s] = times.split(' --> ')[0].replace(',', '.').split(':').map(Number);
    return { start: h * 3600 + m * 60 + s, text: text.join(' ') };
  });
}

const scenes = new Map();   // scene id -> { title, themes: { dark: {...}, light: {...} } }
for (const file of existsSync(join(REC, 'final')) ? readdirSync(join(REC, 'final')).filter((f) => f.endsWith('.mp4')).sort() : []) {
  const [, id, theme] = /^(.*)-(dark|light)\.mp4$/.exec(file) || [];
  const takeFile = join(REC, 'takes', `${id}-${theme}`, 'take.json');
  if (!id || id === 'smoke' || !existsSync(takeFile)) continue;   // smoke is the recorder's own test scene
  const take = JSON.parse(readFileSync(takeFile, 'utf8'));
  const list = cues(join(REC, 'final', file.replace(/\.mp4$/, '.srt')));
  const last = list.at(-1);
  if (!scenes.has(id)) scenes.set(id, { title: take.title, premise: take.premise, themes: {} });
  scenes.get(id).themes[theme] = { file: `final/${file}`, clean: `clean/${file}`, mb: statSync(join(REC, 'final', file)).size / 1e6, recordedAt: take.recordedAt,
    seconds: last ? last.start + 12 : take.seconds, cues: list, outro: take.outro || [], capture: take.capture || {} };
}

const sections = [...scenes.entries()].map(([id, sc]) => {
  const themes = ['dark', 'light'].filter((t) => sc.themes[t]);
  const first = sc.themes[themes[0]];
  const players = themes.map((t) => {
    const v = sc.themes[t];
    return `<figure><video controls preload="none" src="${esc(v.file)}"></video><figcaption>${t} · ${v.mb.toFixed(0)} MB · ` +
      `${v.capture.repeated ?? '?'} repeated of ${v.capture.written ?? '?'} frames · <a href="${esc(v.file)}">video</a> · <a href="${esc(v.file.replace(/\.mp4$/, '.srt'))}">.srt</a> · ` +
      `<a href="${esc(v.file.replace(/\.mp4$/, '.vtt'))}">.vtt</a> · <a href="${esc(v.clean)}">no captions</a></figcaption></figure>`;
  }).join('');
  const captions = first.cues.map((c) => `<li><span>${clock(c.start)}</span>${esc(c.text)}</li>`).join('');
  return `<section id="${esc(id)}"><h2>${esc(sc.title)}</h2><p class="premise">${esc(sc.premise)}</p><div class="players">${players}</div>` +
    `<details><summary>Captions (${themes[0]} take: numbers differ a little between takes, each is measured in its own)</summary><ol>${captions}</ol>` +
    `<p class="outro">End card: ${first.outro.map(esc).join(' · ')}</p></details></section>`;
}).join('\n');

writeFileSync(join(REC, 'index.html'), `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Booth recordings</title>
<style>
  body { margin: 0 auto; max-width: 1500px; padding: 32px; background: #0b0b0b; color: #f2f1ea; font: 16px/1.5 system-ui, sans-serif; }
  h1 { font-weight: 300; font-size: 40px; margin: 0 0 4px; } h2 { font-weight: 400; font-size: 26px; margin: 0; }
  .lead, .premise, figcaption, .outro { color: #a3a29b; } .premise { margin: 2px 0 14px; }
  section { margin: 36px 0; padding-top: 28px; border-top: 1px solid #262626; }
  .players { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; } figure { margin: 0; } video { width: 100%; border-radius: 10px; background: #000; }
  figcaption { font-size: 13px; margin-top: 6px; } a { color: #c8acff; }
  details { margin-top: 12px; } summary { cursor: pointer; color: #a3a29b; } ol { list-style: none; padding: 0; margin: 10px 0; columns: 2; column-gap: 40px; }
  li { break-inside: avoid; margin-bottom: 6px; } li span { display: inline-block; width: 46px; color: #77766f; font: 13px ui-monospace, monospace; }
  nav a { margin-right: 14px; white-space: nowrap; }
</style></head><body>
<h1>Booth recordings</h1>
<p class="lead">${scenes.size} scenarios, recorded live from the stage against the running cluster. 1920×1080, 30 frames a second, captions in the picture.
Re-record one with <code>node tools/booth/produce.mjs &lt;scene&gt;</code>; change a caption in <code>tools/booth/scenes/</code>.</p>
<nav>${[...scenes.entries()].map(([id, sc]) => `<a href="#${esc(id)}">${esc(sc.title)}</a>`).join('')}</nav>
${sections}
</body></html>
`);
console.log(`${join(REC, 'index.html')}: ${scenes.size} scenarios, ${[...scenes.values()].reduce((n, s) => n + Object.keys(s.themes).length, 0)} videos`);

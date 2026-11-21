#!/usr/bin/env node
// Record and render a booth scene in both themes, retaking a take whose capture stuttered.
//   node tools/booth/produce.mjs survive-region            # dark and light
//   node tools/booth/produce.mjs survive-region --theme light
// A take that is invalid for what happened in it (a step failed, a caption's claim did not hold) is not retried:
// that needs a look, not another roll of the dice.
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..', '..');
const argv = process.argv.slice(2);
const scene = argv.find((a) => !a.startsWith('--'));
const only = argv.includes('--theme') ? argv[argv.indexOf('--theme') + 1] : null;
if (!scene) { console.error('usage: produce.mjs <scene> [--theme dark|light]'); process.exit(2); }
const TAKES = 3;
const SETUP = ['cluster', 'places', 'cloud', 'primary', 'primaryPlace', 'primaryNodes', 'primaryZones', 'nodes', 'regions', 'home', 'homePlace'];
const tail = (text, lines) => (text || '').trim().split('\n').slice(-lines).join('\n');

let failed = false;
for (const theme of only ? [only] : ['dark', 'light']) {
  const file = join(ROOT, 'recordings', 'takes', `${scene}-${theme}`, 'take.json');
  let take = null;
  for (let attempt = 1; attempt <= TAKES; attempt++) {
    const started = Date.now();
    const r = spawnSync('node', [join(HERE, 'record.mjs'), scene, '--theme', theme], { stdio: ['ignore', 'pipe', 'pipe'], encoding: 'utf8' });
    take = existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')) : null;
    if (!take || Date.parse(take.recordedAt) < started) {            // the recorder never got as far as a take
      console.log(`${scene} ${theme} take ${attempt}: no take was recorded:\n${tail(r.stdout, 6)}\n${tail(r.stderr, 6)}`);
      take = null;
      break;
    }
    const capture = take.capture || {};
    console.log(`${scene} ${theme} take ${attempt}: ${take.seconds.toFixed(0)} s, ${capture.repeated ?? '?'} repeated frames, longest gap ${capture.maxGapMs ?? '?'} ms: ` +
      `${take.valid ? 'valid' : `invalid (${take.reasons.join('; ')})`}`);
    if (take.valid) break;
    const stutterOnly = capture.reasons && take.reasons.length === capture.reasons.length;   // nothing wrong but the capture
    if (!stutterOnly) { console.log(tail(r.stdout, 12)); break; }
    if (capture.gaps && capture.gaps.length) console.log(`  longest stalls at ${capture.gaps.slice(0, 3).map((g) => `${g.t} s (${g.ms} ms)`).join(', ')}`);
  }
  if (!take || !take.valid) { failed = true; break; }   // the other theme would fail the same way: fix the scene first
  const r = spawnSync('node', [join(HERE, 'render.mjs'), scene, '--theme', theme], { stdio: ['ignore', 'pipe', 'pipe'], encoding: 'utf8' });
  console.log(tail(r.stdout, 3));
  if (r.status !== 0) { failed = true; console.log(tail(r.stderr, 12)); }
  console.log(`  facts: ${Object.entries(take.facts || {}).filter(([k]) => !SETUP.includes(k)).map(([k, v]) => `${k}=${v}`).join(', ')}`);
}
process.exit(failed ? 1 : 0);

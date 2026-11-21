// Unit tests for the booth recorder's timing, caption and camera math: node --test tools/booth/test/*.test.mjs
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FRAME, Slotter, buildCues, cameraFor, planLapses, readingSeconds, retimer, srt, vtt, wrapCaption } from '../lib/timeline.mjs';

test('steady frames each fill one slot of the 30 fps grid', () => {
  const s = new Slotter();
  for (const ts of [100, 100 + 1 / 30, 100 + 2 / 30]) assert.deepEqual(s.push(ts), { repeat: 0, write: true });
  assert.deepEqual(s.stats(), { frames: 3, written: 3, repeated: 0, replaced: 0, maxGapMs: 33 });
});

test('a late frame repeats the previous one to keep time; an early one replaces it', () => {
  const s = new Slotter();
  s.push(0); s.push(1 / 30); s.push(2 / 30);
  assert.deepEqual(s.push(2 / 30 + 0.1), { repeat: 2, write: true });      // 100 ms later: slots 3 and 4 were missed
  assert.deepEqual(s.push(2 / 30 + 0.108), { repeat: 0, write: false });   // 8 ms later: same slot
  assert.deepEqual(s.stats(), { frames: 5, written: 6, repeated: 2, replaced: 1, maxGapMs: 100 });
});

test('at the end the last frame is held up to the stop time, without counting as a hiccup', () => {
  const s = new Slotter();
  s.push(0); s.push(1 / 30);
  assert.equal(s.tail(0.2), 5);                 // slots 2..6 are filled with the last frame
  assert.equal(s.tail(0.2), 0);
  assert.deepEqual(s.stats(), { frames: 2, written: 7, repeated: 0, replaced: 0, maxGapMs: 33 });
  assert.equal(new Slotter().tail(5), 0);       // nothing captured, nothing to hold
});

test('reading time grows with the words, within 2.5 to 8 seconds', () => {
  assert.equal(readingSeconds('Two words'), 2.5);
  assert.equal(readingSeconds('one two three four five six seven eight nine ten eleven twelve'), 4.96);
  assert.equal(readingSeconds(Array(40).fill('word').join(' ')), 8);
});

test('a caption is one or two lines of at most 60 characters', () => {
  const short = 'Payments keep committing while a whole region is dark.';
  assert.deepEqual(wrapCaption(short), [short]);
  const long = 'Every node in N. Virginia just went dark. Watch the transactions: the other two regions keep paying.';
  const lines = wrapCaption(long);
  assert.equal(lines.length, 2);
  assert.equal(lines.join(' '), long);
  assert.ok(lines.every((l) => l.length <= 60), lines.join(' | '));
  assert.ok(Math.abs(lines[0].length - lines[1].length) <= 12, lines.join(' | '));   // balanced, not one long and one short
  assert.throws(() => wrapCaption(Array(30).fill('word').join(' ')), RangeError);
  assert.throws(() => wrapCaption('x'.repeat(61) + ' ' + 'y'.repeat(20)), RangeError); // a word that cannot fit
});

test('only long waits are time-lapsed, down to about five seconds', () => {
  assert.deepEqual(planLapses([{ start: 2, end: 8 }]), []);
  assert.deepEqual(planLapses([{ start: 10, end: 70 }]), [{ from: 300, to: 2100, every: 12 }]);
});

test('the retimer keeps every Nth frame inside a lapse and maps capture time to output time', () => {
  const r = retimer([{ from: 300, to: 2100, every: 12 }]);
  assert.deepEqual([299, 300, 301, 312, 2100].map((n) => r.keep(n)), [true, true, false, true, true]);
  assert.equal(r.outFrames(2100), 450);
  assert.equal(r.toOut(70), 15);
  assert.equal(r.toOut(40), 12.5);
  assert.equal(r.toOut(5), 5);
  assert.equal(r.selectExpr(), 'if(between(n,300,2099),not(mod(n-300,12)),1)');
  assert.equal(retimer([]).selectExpr(), '1');
  assert.equal(retimer([{ from: 30, to: 330, every: 2 }, { from: 600, to: 900, every: 3 }]).selectExpr(),
    'if(between(n,30,329),not(mod(n-30,2)),if(between(n,600,899),not(mod(n-600,3)),1))');
});

test('each caption lasts until the next one, a clear, or the end', () => {
  const same = (t) => t;
  assert.deepEqual(buildCues([{ t: 1, type: 'say', text: 'A' }, { t: 5, type: 'say', text: 'B' }, { t: 9, type: 'clear' }], 12, same),
    [{ start: 1, end: 5, text: 'A' }, { start: 5, end: 9, text: 'B' }]);
  assert.deepEqual(buildCues([{ t: 10, type: 'say', text: 'C' }], 12, same), [{ start: 10, end: 12, text: 'C' }]);
  assert.deepEqual(buildCues([{ t: 1, type: 'say', text: '' }, { t: 2, type: 'shot' }], 12, same), []);
  assert.deepEqual(buildCues([{ t: 40, type: 'say', text: 'D' }, { t: 70, type: 'clear' }], 80, retimer([{ from: 300, to: 2100, every: 12 }]).toOut),
    [{ start: 12.5, end: 15, text: 'D' }]);
});

test('caption files use the SRT and WebVTT time formats', () => {
  const cues = [{ start: 3.5, end: 7.25, text: 'First line\nsecond line' }, { start: 3661.004, end: 3662, text: 'Late' }];
  assert.equal(srt(cues), '1\n00:00:03,500 --> 00:00:07,250\nFirst line\nsecond line\n\n2\n01:01:01,004 --> 01:01:02,000\nLate\n');
  assert.equal(vtt(cues), 'WEBVTT\n\n00:00:03.500 --> 00:00:07.250\nFirst line\nsecond line\n\n01:01:01.004 --> 01:01:02.000\nLate\n');
});

test('the camera fits a stage rectangle into the area above the caption band', () => {
  assert.deepEqual(FRAME, { w: 1920, h: 1080, band: 150 });
  const wide = cameraFor({ x: 0, y: 0, w: 1920, h: 1080 });
  assert.equal(Number(wide.scale.toFixed(4)), 0.8611);
  assert.equal(wide.tx, 0);
  assert.equal(wide.ty, -75);
  const card = { x: 24, y: 284, w: 604, h: 396 };
  const c = cameraFor(card);
  assert.equal(Number(c.scale.toFixed(3)), 2.348);                       // limited by height: 930 / 396
  const lands = (p) => [960 + c.scale * (p[0] - 960) + c.tx, 540 + c.scale * (p[1] - 540) + c.ty];  // transform-origin: centre
  const [x, y] = lands([card.x + card.w / 2, card.y + card.h / 2]);
  assert.ok(Math.abs(x - 960) < 1e-9 && Math.abs(y - 465) < 1e-9, `${x}, ${y}`);
  assert.equal(cameraFor({ x: 900, y: 500, w: 100, h: 40 }).scale, 2.6);  // never past the cap
  assert.equal(cameraFor({ x: 900, y: 500, w: 100, h: 40 }, FRAME, 4).scale, 4);
});

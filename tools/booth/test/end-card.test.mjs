// The end card invites the viewer to run the demo: the repository's address in words and a QR code beside it. Nobody
// scans their own video, so the code is read here the way a phone reads it and compared with the words.
// node --test tools/booth/test/*.test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const BOOTH = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = /^const REPO = '([^']+)';/m.exec(readFileSync(join(BOOTH, 'render.mjs'), 'utf8'))[1];
const QR_SVG = readFileSync(join(BOOTH, 'assets', 'repo-qr.svg'), 'utf8');

// The dark squares of a code drawn as one SVG path of horizontal strokes (M x y, h length, m dx dy). The three corner
// squares of a QR code reach its top, left, right and bottom edges, so cropping to the dark squares drops any margin.
function modules(svg) {
  const dark = [];
  let x = 0, y = 0;
  for (const [, cmd, a, b] of / d="([^"]+)"/.exec(svg)[1].matchAll(/([Mmh])\s*(-?[\d.]+)(?:\s+(-?[\d.]+))?/g)) {
    if (cmd === 'M') [x, y] = [Number(a), Number(b)];
    else if (cmd === 'm') [x, y] = [x + Number(a), y + Number(b)];
    else { for (let i = 0; i < Number(a); i++) dark.push([Math.floor(y), x + i]); x += Number(a); }
  }
  const top = Math.min(...dark.map(([r]) => r)), left = Math.min(...dark.map(([, c]) => c));
  const n = Math.max(...dark.map(([r]) => r)) - top + 1;
  assert.equal(Math.max(...dark.map(([, c]) => c)) - left + 1, n, 'the code is square');
  const grid = Array.from({ length: n }, () => new Array(n).fill(false));
  for (const [r, c] of dark) grid[r - top][c - left] = true;
  return grid;
}

// Data codewords in each block, by version and error correction level (ISO/IEC 18004), for versions 1 to 6:
// enough for an address of up to 130 characters.
const BLOCKS = {
  '1L': [19], '1M': [16], '1Q': [13], '1H': [9],
  '2L': [34], '2M': [28], '2Q': [22], '2H': [16],
  '3L': [55], '3M': [44], '3Q': [17, 17], '3H': [13, 13],
  '4L': [80], '4M': [32, 32], '4Q': [24, 24], '4H': [9, 9, 9, 9],
  '5L': [108], '5M': [43, 43], '5Q': [15, 15, 16, 16], '5H': [11, 11, 12, 12],
  '6L': [68, 68], '6M': [27, 27, 27, 27], '6Q': [19, 19, 19, 19], '6H': [15, 15, 15, 15],
};
const MASKS = [
  (r, c) => (r + c) % 2 === 0, (r) => r % 2 === 0, (r, c) => c % 3 === 0, (r, c) => (r + c) % 3 === 0,
  (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0, (r, c) => (r * c) % 2 + (r * c) % 3 === 0,
  (r, c) => ((r * c) % 2 + (r * c) % 3) % 2 === 0, (r, c) => ((r + c) % 2 + (r * c) % 3) % 2 === 0,
];
const FORMAT_BITS = [[0, 8], [1, 8], [2, 8], [3, 8], [4, 8], [5, 8], [7, 8], [8, 8], [8, 7], [8, 5], [8, 4], [8, 3], [8, 2], [8, 1], [8, 0]];

// The text a QR code holds, read from its squares. A drawing has no damage, so the error correction codewords are
// skipped; the format information's own checksum still has to hold, which catches a misread grid.
function readQr(grid) {
  const n = grid.length, version = (n - 17) / 4;
  assert.ok(Number.isInteger(version) && version >= 1 && version <= 6, `a code of ${n} squares is outside versions 1 to 6`);
  let format = 0;
  FORMAT_BITS.forEach(([r, c], bit) => { if (grid[r][c]) format |= 1 << bit; });
  format ^= 0x5412;
  let check = format >> 10;
  for (let i = 0; i < 10; i++) check = (check << 1) ^ ((check >> 9) * 0x537);
  assert.equal(format & 0x3ff, check, 'the format information reads cleanly');
  const level = 'MLHQ'[format >> 13], masked = MASKS[(format >> 10) & 7];

  const centre = 4 * version + 10;   // versions 2 to 6 have one alignment pattern, near the bottom right
  const fixed = (r, c) => (r < 9 && (c < 9 || c >= n - 8)) || (r >= n - 8 && c < 9) || r === 6 || c === 6
    || (version > 1 && Math.abs(r - centre) <= 2 && Math.abs(c - centre) <= 2);
  const bits = [];
  for (let right = n - 1; right >= 1; right -= 2) {   // two columns at a time from the right, up then down
    if (right === 6) right = 5;
    for (let step = 0; step < n; step++) {
      for (const c of [right, right - 1]) {
        const r = ((right + 1) & 2) === 0 ? n - 1 - step : step;
        if (!fixed(r, c)) bits.push(grid[r][c] !== masked(r, c) ? 1 : 0);
      }
    }
  }
  const codewords = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) codewords.push(parseInt(bits.slice(i, i + 8).join(''), 2));
  const sizes = BLOCKS[`${version}${level}`], blocks = sizes.map(() => []);
  let next = 0;   // the blocks' data codewords are dealt out in turn, a longer block's extra one last
  for (let i = 0; i < Math.max(...sizes); i++) sizes.forEach((size, b) => { if (i < size) blocks[b].push(codewords[next++]); });
  const data = blocks.flat().map((cw) => cw.toString(2).padStart(8, '0')).join('');
  assert.equal(data.slice(0, 4), '0100', 'the code holds its text as bytes');
  const length = parseInt(data.slice(4, 12), 2);
  return Buffer.from(Array.from({ length }, (_, i) => parseInt(data.slice(12 + 8 * i, 20 + 8 * i), 2))).toString('utf8');
}

test('the QR code on the end card leads to the address printed beside it', () => {
  assert.equal(readQr(modules(QR_SVG)), `https://${REPO}`);
});

test('the end card sends the viewer to the repository in the cockroachdb organisation', () => {
  assert.equal(REPO, 'github.com/cockroachdb/cockroach-chaos-demo');
});

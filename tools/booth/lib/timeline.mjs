// Timing, caption and camera math for the booth recorder. Pure: no browser, no ffmpeg, no clock.
// Tests: node --test tools/booth/test/*.test.mjs

export const FPS = 30;
// The picture: the stage lives above a caption band at the bottom of the frame.
export const FRAME = { w: 1920, h: 1080, band: 150 };

/** Maps captured frame timestamps (seconds, any epoch) onto a constant-rate grid: a late frame means the one before
 *  it is repeated to keep time; a frame landing in a slot that is already filled is skipped (the caller still keeps
 *  it as the newest picture for later repeats). */
export class Slotter {
  constructor(fps = FPS) {
    this.fps = fps; this.t0 = null; this.slot = -1; this.prev = null;
    this.count = { frames: 0, written: 0, repeated: 0, replaced: 0, maxGap: 0 };
  }

  push(ts) {
    const c = this.count;
    c.frames++;
    if (this.t0 === null) this.t0 = ts;
    if (this.prev !== null) c.maxGap = Math.max(c.maxGap, ts - this.prev);
    this.prev = ts;
    const slot = Math.round((ts - this.t0) * this.fps);
    if (slot <= this.slot) { c.replaced++; return { repeat: 0, write: false }; }
    const repeat = this.slot < 0 ? 0 : slot - this.slot - 1;
    this.slot = slot;
    c.repeated += repeat; c.written += repeat + 1;
    return { repeat, write: true };
  }

  /** How many more times to write the last frame so the video runs up to `ts` (the page was still: no new frames). */
  tail(ts) {
    if (this.t0 === null) return 0;
    const extra = Math.max(0, Math.floor((ts - this.t0) * this.fps) - this.slot);
    this.slot += extra; this.count.written += extra;
    return extra;
  }

  stats() {
    const c = this.count;
    return { frames: c.frames, written: c.written, repeated: c.repeated, replaced: c.replaced, maxGapMs: Math.round(c.maxGap * 1000) };
  }
}

/** Seconds a passer-by needs to read a caption: 1 s to find it, a third of a second per word, 2.5 to 8 s. */
export function readingSeconds(text) {
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  return Math.round(Math.min(8, Math.max(2.5, 1 + 0.33 * words)) * 100) / 100;
}

/** One line, or two balanced lines, of at most `maxChars`. Throws when the caption needs more: shorten it. */
export function wrapCaption(text, maxChars = 60) {
  const t = text.trim().replace(/\s+/g, ' ');
  if (t.length <= maxChars) return [t];
  let best = null;
  for (let i = 0; i < t.length; i++) {
    if (t[i] !== ' ') continue;
    const a = t.slice(0, i), b = t.slice(i + 1);
    if (a.length > maxChars || b.length > maxChars) continue;
    if (!best || Math.abs(a.length - b.length) < Math.abs(best[0].length - best[1].length)) best = [a, b];
  }
  if (!best) throw new RangeError(`caption needs more than 2 lines of ${maxChars} characters: "${t}"`);
  return best;
}

/** Waits (capture seconds) worth fast-forwarding, as frame ranges [from, to) of which every Nth frame is kept. */
export function planLapses(lapses, { fps = FPS, minSeconds = 8, targetSeconds = 5 } = {}) {
  return lapses
    .filter((l) => l.end - l.start >= minSeconds)
    .map((l) => ({ from: Math.round(l.start * fps), to: Math.round(l.end * fps), every: Math.max(2, Math.ceil((l.end - l.start) / targetSeconds)) }))
    .sort((a, b) => a.from - b.from);
}

/** Capture frames/time -> output frames/time once the lapses are cut, and the ffmpeg `select` expression that cuts them. */
export function retimer(plan, fps = FPS) {
  const keep = (n) => { const seg = plan.find((p) => n >= p.from && n < p.to); return seg ? (n - seg.from) % seg.every === 0 : true; };
  const outFrames = (n) => n - plan.reduce((dropped, p) => {
    const inside = Math.min(Math.max(n, p.from), p.to) - p.from;
    return dropped + inside - Math.ceil(inside / p.every);
  }, 0);
  const toOut = (seconds) => outFrames(Math.round(seconds * fps)) / fps;
  const selectExpr = () => plan.reduceRight((rest, p) => `if(between(n,${p.from},${p.to - 1}),not(mod(n-${p.from},${p.every})),${rest})`, '1');
  return { keep, outFrames, toOut, selectExpr };
}

/** Caption cues in output seconds: each `say` lasts until the next `say`, a `clear`, or the end. */
export function buildCues(events, endSeconds, toOut) {
  const cues = []; let open = null;
  const close = (t) => { if (open && toOut(t) > open.start) cues.push({ start: open.start, end: toOut(t), text: open.text }); open = null; };
  for (const e of [...events].sort((a, b) => a.t - b.t)) {
    if (e.type !== 'say' && e.type !== 'clear') continue;
    close(e.t);
    if (e.type === 'say' && e.text) open = { start: toOut(e.t), text: e.text };
  }
  close(endSeconds);
  return cues;
}

function clock(seconds, separator) {
  const ms = Math.round(seconds * 1000);
  const pad = (v, n = 2) => String(v).padStart(n, '0');
  return `${pad(Math.floor(ms / 3600000))}:${pad(Math.floor(ms / 60000) % 60)}:${pad(Math.floor(ms / 1000) % 60)}${separator}${pad(ms % 1000, 3)}`;
}

export function srt(cues) {
  return cues.map((c, i) => `${i + 1}\n${clock(c.start, ',')} --> ${clock(c.end, ',')}\n${c.text}\n`).join('\n');
}

export function vtt(cues) {
  return `WEBVTT\n\n${cues.map((c) => `${clock(c.start, '.')} --> ${clock(c.end, '.')}\n${c.text}\n`).join('\n')}`;
}

/** The `#stage` transform (`translate(tx, ty) scale(scale)`, origin at the stage centre) that fits a stage rectangle
 *  into the area above the caption band, centred. */
export function cameraFor(rect, frame = FRAME, maxScale = 2.6) {
  const safeH = frame.h - frame.band;
  const scale = Math.min(frame.w / rect.w, safeH / rect.h, maxScale);
  return {
    scale,
    tx: scale * (frame.w / 2 - (rect.x + rect.w / 2)),
    ty: safeH / 2 - frame.h / 2 + scale * (frame.h / 2 - (rect.y + rect.h / 2)),
  };
}

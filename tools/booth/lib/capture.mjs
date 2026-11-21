// Screen capture for the booth recorder: Chrome hands over lossless frames of the page, they are placed on a 30 fps
// grid (see Slotter) and encoded into a lossless master. One lossy step is left for the final video.
import { spawn } from 'node:child_process';
import { FPS, Slotter } from './timeline.mjs';

// Past these a take shows visible stutter: retake it. One or two short hitches in a take that runs for minutes are
// let through (a ten-minute take would otherwise almost never pass); a freeze of 0.4 s never is.
const MAX_REPEATED_SHARE = 0.03;
const HITCH_MS = 200;
const MAX_HITCHES = 2;
const MAX_GAP_MS = 400;

/** Start recording `page` into `file` (.mkv). -> { t0(): epoch seconds of the first frame, stop(): Promise<stats> }.
 *  stats: { frames, written, repeated, replaced, maxGapMs, seconds, backlogMaxMB, gaps[], valid, reasons[] }. */
export async function startCapture(page, file) {
  const ffmpeg = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'png', '-i', '-',
    '-c:v', 'libx264rgb', '-qp', '0', '-preset', 'ultrafast', '-pix_fmt', 'rgb24', file], { stdio: ['pipe', 'inherit', 'inherit'] });
  let encoderError = null;
  const exited = new Promise((resolve) => ffmpeg.on('exit', (code) => { if (code) encoderError = new Error(`ffmpeg exited with ${code}`); resolve(); }));
  ffmpeg.on('error', (e) => { encoderError = e; });
  ffmpeg.stdin.on('error', (e) => { encoderError = encoderError || e; });

  const slotter = new Slotter(FPS);
  let last = null; let backlogMax = 0; let previous = null;
  const gaps = [];   // where the picture stalled: { t: seconds into the take, ms }
  let hitches = 0;
  let first; const firstFrame = new Promise((resolve) => { first = resolve; });
  const off = page.on('Page.screencastFrame', (p) => {
    page.send('Page.screencastFrameAck', { sessionId: p.sessionId }).catch(() => {});
    if (encoderError) return;
    const frame = Buffer.from(p.data, 'base64');
    const ts = p.metadata.timestamp;
    if (previous !== null && ts - previous > 0.06) gaps.push({ t: Number((previous - slotter.t0).toFixed(2)), ms: Math.round((ts - previous) * 1000) });
    if (previous !== null && (ts - previous) * 1000 > HITCH_MS) hitches++;
    previous = ts;
    const { repeat, write } = slotter.push(ts);
    for (let i = 0; i < repeat; i++) ffmpeg.stdin.write(last);
    if (write) ffmpeg.stdin.write(frame);
    last = frame;
    backlogMax = Math.max(backlogMax, ffmpeg.stdin.writableLength);
    first();
  });
  // Every 2nd compositor frame: 30 a second from a 60 Hz page.
  await page.send('Page.startScreencast', { format: 'png', maxWidth: 1920, maxHeight: 1080, everyNthFrame: 2 });
  await firstFrame;

  return {
    t0: () => slotter.t0,
    async stop() {
      await page.send('Page.stopScreencast').catch(() => {});
      off();
      if (!encoderError) for (let i = slotter.tail(Date.now() / 1000); i > 0; i--) ffmpeg.stdin.write(last);
      ffmpeg.stdin.end();
      await exited;
      if (encoderError) throw encoderError;
      const s = slotter.stats();
      const reasons = [];
      if (s.repeated / Math.max(1, s.written) > MAX_REPEATED_SHARE) reasons.push(`${s.repeated} of ${s.written} frames were repeats (stutter)`);
      if (s.maxGapMs > MAX_GAP_MS) reasons.push(`a ${s.maxGapMs} ms gap between frames (a visible freeze)`);
      else if (hitches > MAX_HITCHES) reasons.push(`${hitches} hitches longer than ${HITCH_MS} ms`);
      return { ...s, seconds: s.written / FPS, backlogMaxMB: Math.round(backlogMax / 1e6), gaps: gaps.sort((a, b) => b.ms - a.ms).slice(0, 25),
        valid: reasons.length === 0, reasons };
    },
  };
}

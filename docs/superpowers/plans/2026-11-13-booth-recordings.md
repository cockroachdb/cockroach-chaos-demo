# Booth Recordings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Captioned 1080p videos of every guided scenario (except stretch), in dark and light, recorded from the real
stage against the real cluster, for the re:Invent booth.

**Architecture:** `record.mjs` drives the stage in headless Chrome over CDP, captures lossless frames to a master and
logs a timeline (captions, camera, clicks, waits, measured facts). `render.mjs` turns master + timeline into the final
MP4: time-lapse for long waits, caption text and cards composited by ffmpeg, plus .srt/.vtt. All timing math is pure
and unit-tested; browser and ffmpeg glue is verified by looking at its output.

**Tech Stack:** Node >= 22 (built-in WebSocket, fetch, `node --test`), Google Chrome, ffmpeg. No npm packages
(same rule as tools/export-explainer.mjs and tools/proof-browser.mjs).

**Spec:** docs/superpowers/specs/2026-11-13-booth-recordings-design.md

## Global Constraints

- Frame 1920x1080, 30 fps. Caption band: bottom 150 px. Safe area: 1920x930.
- Capture: `Page.startScreencast` format png, everyNthFrame 2, Chrome flags `--headless=new
  --force-device-scale-factor=1 --window-size=1920,1080 --hide-scrollbars`.
- Master: `libx264rgb -qp 0 -preset ultrafast -pix_fmt rgb24`, Matroska.
- Final: `libx264 -crf 16 -preset slow -pix_fmt yuv420p`, bt709 tags, `+faststart`, no audio.
- Reading time: 1.0 s + 0.33 s per word, clamped to 2.5..8 s. Captions: at most 2 lines of 60 characters.
- Time-lapse only waits longer than 8 s, down to about 5 s, always with the badge.
- No cluster rebuilds (Fineract shares the cluster). Every take ends with: scenario stopped, everything recovered,
  Payments stopped if the take started it, cluster healthy 9/9.
- Captions follow the re:Invent guardrails (see spec). No em dashes in captions.
- Everything new is private: `tools/booth` and `tests/unit/test_booth_tools.py` go in
  `tools/public-branch-exclusions.txt`. `recordings/` is git-ignored.
- Commit on local `main`; never push. Commit footer: `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.

## Review Focus

1. A capture hiccup (cluster busy) leaves repeated frames: the take must report repeats and its longest gap, and be
   marked invalid past 3% repeats or a gap over 400 ms.
2. A caption longer than 2 lines of 60 characters: refused when the scene is loaded, not discovered in the video.
3. A scenario step that errors mid-take: the take is invalid, and the cluster is still restored.
4. The recorder dying (exception, Ctrl-C): the cluster is restored on the way out.
5. Colours shifting in the final encode: a frame from the MP4 must match the master within 3 levels per channel on
   flat areas.

## File Structure

- `tools/booth/lib/timeline.mjs`: pure timing, caption and camera math.
- `tools/booth/lib/chrome.mjs`: launch Chrome, CDP client.
- `tools/booth/lib/capture.mjs`: screencast to master, with stats.
- `tools/booth/lib/panel.mjs`: control-plane client, health wait, restore.
- `tools/booth/lib/stageset.js`: injected into the stage page: caption band strip, pointer, camera.
- `tools/booth/lib/director.mjs`: what a scene calls (`say`, `shot`, `click`, `until`, `step`, `fact`, `check`).
- `tools/booth/record.mjs`, `tools/booth/render.mjs`: the two commands.
- `tools/booth/overlay.html`: caption, label, badge and card artwork, rendered to PNG by Chrome.
- `tools/booth/scenes/<id>.mjs`: one per video.
- `tools/booth/test/timeline.test.mjs`; `tests/unit/test_booth_tools.py` runs it under pytest.

---

### Task 1: Timing, caption and camera math

**Files:** Create `tools/booth/lib/timeline.mjs`, `tools/booth/test/timeline.test.mjs`,
`tests/unit/test_booth_tools.py`. Modify `.gitignore` (add `recordings/`), `tools/public-branch-exclusions.txt`.

**Interfaces (Produces):**

```js
export const FPS = 30;
export const FRAME = { w: 1920, h: 1080, band: 150 };
export class Slotter {            // maps frame timestamps (seconds) onto a constant-rate grid
  constructor(fps = FPS)
  push(ts)                        // -> { repeat: number, write: boolean }: write the previous frame `repeat` more
                                  //    times, then this one if `write` (false: same slot as the previous frame)
  stats()                         // -> { frames, written, repeated, replaced, maxGapMs }
}
export function readingSeconds(text)                       // -> number
export function wrapCaption(text, maxChars = 60)           // -> string[] (1 or 2 lines); RangeError if it needs more
export function planLapses(lapses, opts)                   // [{start,end}] seconds -> [{from,to,every}] frames
export function retimer(plan, fps = FPS)                   // -> { keep(n), outFrames(n), toOut(sec), selectExpr() }
export function buildCues(events, endSeconds, toOut)       // -> [{start,end,text}] output seconds
export function srt(cues)  ;  export function vtt(cues)    // -> string
export function cameraFor(rect, frame = FRAME, maxScale = 2.6)   // -> { scale, tx, ty }
```

- [ ] **Step 1: Write the failing tests** (`node --test tools/booth/test/`), with these cases:
  - Slotter: timestamps 0, 1/30, 2/30 -> each `{repeat:0, write:true}`; a 100 ms gap -> `repeat: 2`; two frames
    8 ms apart -> second is `write:false` and counts as replaced; `maxGapMs` reports 100.
  - readingSeconds: 2 words -> 2.5; 12 words -> 4.96; 40 words -> 8.
  - wrapCaption: a 50-character line stays one line; a 100-character sentence splits into two lines each <= 60,
    breaking at a space nearest the middle; 150 characters throws RangeError.
  - planLapses: a 6 s wait is dropped; a 60 s wait from t=10 -> `{from:300, to:2100, every:12}`.
  - retimer for that plan: `keep(299)` true, `keep(300)` true, `keep(301)` false, `keep(312)` true;
    `outFrames(2100)` = 300 + 150; `toOut(70)` = 15; `toOut(40)` = 12.5;
    `selectExpr()` = `if(between(n,300,2099),not(mod(n-300,12)),1)`.
  - buildCues: say at 1 and 5, clear at 9, end 12 -> two cues `[1,5)` and `[5,9)`; a say at 10 with no clear ends
    at 12; empty text is skipped.
  - srt/vtt: cue 3.5..7.25 -> `00:00:03,500 --> 00:00:07,250` and `00:00:03.500 --> 00:00:07.250`; vtt starts
    with `WEBVTT`.
  - cameraFor: the whole stage -> scale 0.8611 (4 dp), tx 0, ty -75; rect `{x:24,y:284,w:604,h:396}` (one region
    card) -> scale 930/396 capped by width 1920/604, i.e. 2.348; the rect's centre lands on (960, 465).
- [ ] **Step 2: Run them and see them fail** (module not found).
- [ ] **Step 3: Implement `timeline.mjs`.**
- [ ] **Step 4: Run the tests: all pass. Run `.venv/bin/python -m pytest -q tests/unit`: all pass.**
- [ ] **Step 5: Commit.**

### Task 2: Chrome, capture, a smoke take

**Files:** Create `tools/booth/lib/chrome.mjs`, `tools/booth/lib/capture.mjs`, `tools/booth/record.mjs`.

**Interfaces (Produces):**

```js
// chrome.mjs
export async function launch({ width = 1920, height = 1080 })   // -> { page, close() }
// page: { send(method, params), on(method, fn) -> off, evaluate(expr, {awaitPromise}) , goto(url) }
// capture.mjs
export async function startCapture(page, file)   // -> { t0(): number, stop(): Promise<stats> }
// stats: Slotter.stats() plus { seconds, valid, reasons[] }   (invalid: repeated > 3% or maxGapMs > 400)
```

- [ ] **Step 1:** Implement both; `record.mjs smoke --theme dark` opens the stage and records 10 s.
- [ ] **Step 2: Verify:** `ffprobe` shows 1920x1080, 30/1 fps, about 300 frames, rgb24; stats print; a frame
  extracted from the master, viewed, shows the stage.
- [ ] **Step 3: Commit.**

### Task 3: The set and the director

**Files:** Create `tools/booth/lib/stageset.js`, `tools/booth/lib/panel.mjs`, `tools/booth/lib/director.mjs`,
`tools/booth/scenes/smoke.mjs`. Modify `record.mjs`.

**Interfaces (Produces):**

```js
// panel.mjs
export function panel(base = 'http://localhost:8088')
//   -> { api(method, path, body) -> {status, data}, state(), healthy(nodes, timeoutS), restore(startedPayments) }
// director.mjs: a scene is `export default { id, title, premise, async run(d) }`
// d.say(text, {hold, wait = true})  d.clear()  d.shot(nameOrRect, {ms = 900})  d.pause(s)
// d.click(selector)  d.hover(selector)  d.until(fn(state), {timeout = 120, lapse = false})
// d.step({lapse})  -> the scenario step that just ran {label, result}   (clicks #sc-next, waits for not busy)
// d.fact(key, value)  d.check(ok, message)  d.label(text)  d.api(method, path, body)  d.state()
// Named shots: wide, top, kpis, regions, chart, scenario, bottom, region:<name>, node:<name>, kpi:<id>
// take.json: { scene, theme, startedAt, seconds, events[], facts{}, checks[], capture{...}, valid, reasons[] }
```

- [ ] **Step 1:** Implement. `record.mjs <scene> --theme X --rehearse` runs a scene without capture and saves a
  still at every `say` to `recordings/rehearsal/<scene>-<theme>/NN.png`.
- [ ] **Step 2: Verify** with `scenes/smoke.mjs` (wide, a close-up on one region, hover a KPI, a pointer click on
  the theme-neutral node tile menu then Escape): stills viewed; pointer visible; band strip clean; close-up sharp.
- [ ] **Step 3: Verify restore:** kill the recorder mid-scene with Ctrl-C after it crashed a node; the cluster
  returns to healthy 9/9.
- [ ] **Step 4: Commit.**

### Task 4: Render

**Files:** Create `tools/booth/overlay.html`, `tools/booth/render.mjs`.

- [ ] **Step 1:** Implement: load take, lint captions, plan lapses, render cue/label/badge/card PNGs with Chrome
  from `overlay.html?theme=`, build the ffmpeg graph (select + setpts, chained overlays with fades), encode final
  and clean, write .srt/.vtt.
- [ ] **Step 2: Verify** on a smoke take: `ffprobe` (h264, yuv420p, 1920x1080, 30 fps, bt709); frames at each cue
  midpoint viewed; colour check against the master (Review Focus 5); .srt opens with the right times.
- [ ] **Step 3: Commit.**

### Task 5: Pilot video: Survive a region outage

**Files:** Create `tools/booth/scenes/survive-region.mjs`.

- [ ] **Step 1:** Write the scene. Rehearse; fix camera and wording from the stills.
- [ ] **Step 2:** Record dark and light. Both takes valid.
- [ ] **Step 3:** Render both. Review a frame per cue, the title and end cards, and a contact sheet.
- [ ] **Step 4:** Tell the user where the pilot is. Commit.

### Task 6: The other scenes

One scene file each, same loop as Task 5 (rehearse, record x2, render x2, review):
`survive-zone`, `zone-vs-region`, `elastic-scale`, `regional-by-row`, `follower-reads`, `global-tables`,
`data-domicile`, `write-skew`, `online-schema-change`, `changefeeds`, `split-brain`, `time-travel`.

- [ ] One checkbox per scene in the ledger; commit after each.

### Task 7: Wrap up

- [ ] Contact sheet per video; `recordings/index.html` listing every video with its duration and captions.
- [ ] Whole-set review by a fresh reader of the captions and stills (does a passer-by get it; any claim not shown).
- [ ] Cluster left healthy; tell the user about the 9 nodes and how to go back to 6.
- [ ] Update memory with where the recordings and recorder live.

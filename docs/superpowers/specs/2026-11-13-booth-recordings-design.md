# Booth recordings: design

Date: 2026-11-13. Private (docs/superpowers is excluded from public builds).

## What was asked

The user's words:

- "I need to make booth screen recordings ... showcasing what we have built here and it needs to be real as if I am
  recording the screen. I need videos of different scenarios in both dark and light theme. It needs be good and
  professional quality with real screen."
- "make the video a little longer if need be so each scenarios are explained completely and let's use closed
  captioning so anyone walking by the booth know what's going on. everytbng is happeninng in the browser anyways so
  noting else we need to show. please be the best video recorder and explainer of these demos"
- "Should we use 9 node scenario and I am assumign you're doing all aws since it's for re:invent"

So: videos for the AWS re:Invent 2026 booth (Nov 30 to Dec 4), looping on a screen with nobody narrating. A passer-by
must understand what is happening from the captions alone. Real app, real cluster, real failures; no mock-ups.

## Goals

- One video per guided scenario, in dark and in light, each explaining the scenario completely.
- Captions always visible in the picture, written like a good SE talking: where to look, what just happened, what it
  proves. Numbers in captions are the ones measured in that take.
- Smooth, sharp, steady: no stutter, no blurry text, no stray UI.
- Repeatable: one command re-records a scene, another re-renders it after a caption edit.

## Non-goals

- No narration audio. No desktop, browser frame or menu bar in the picture.
- No stretch-cluster video for re:Invent (it shows Azure and Google Cloud nodes).
- Not published: the recorder stays out of the public repo until the user says otherwise.

## Anti-goals (a video that does any of these is a failure)

- A caption that claims something the take did not show (for example "no payment failed" when one did).
- Anything staged: fake data, sped-up footage without a visible fast-forward badge, a UI state the app did not reach.
- Breaking the re:Invent guardrails: no savings percentages, never name or knock AWS database services, no Bedrock
  integration claims. (Memory: reinvent-2026-content-guardrails.)
- Harming the cluster's other tenant: Fineract runs on this cluster (databases fineract_default, fineract_tenants).
  No rebuilds. Every take ends with the cluster healthy.

## Constraints found by measurement (2026-11-13, M3 Pro, cluster in a 10-vCPU Podman VM)

- Chrome screencast (headless, PNG, every 2nd frame) under Payments load on the 9-node cluster:
  1080p holds 29.7 fps with about 1% repeated frames; 4K falls to 24 fps with 100 ms+ gaps; two tabs at once fall to
  21 fps each. So: 1080p, 30 fps, one theme per run.
- Chrome tab capture (getDisplayMedia + MediaRecorder) fails with NotReadableError headless and headed, even with
  Screen Recording permission. Not used.
- Screen Recording permission is granted to iTerm now. A real-display capture (about 3456x1944 native) is possible
  later, but it needs the user's screen to itself for every take. Offered as a later upgrade, not built.
- Frame timestamps from Chrome are wall-clock (offset about 10 to 20 ms from Node's clock): one clock for frames and
  events.
- The cluster was grown live from 6 to 9 nodes (one added per region) to avoid a rebuild. Preset `aws`: us-east-1,
  us-west-2, eu-west-1, three zones each. All scenarios but stretch-cloud are ready on it.

## Approaches considered

1. **Headless Chrome screencast at 1080p, camera in the page, captions composited afterwards.** Chosen. Runs in the
   background, pixel-exact frames, captions editable without re-recording.
2. **Real display capture with ffmpeg avfoundation.** Higher resolution, but takes over the user's screen for hours
   and a notification ruins a take. Kept as an optional later upgrade.
3. **Dark and light from one run (two tabs, mirrored clicks).** Would halve cluster time and make both themes show
   the same run. Capture can't keep up with two tabs under load. Dropped.
4. **4K screencast with zoom done afterwards.** Capture can't keep up at 4K. Dropped.

## The picture

- Frame 1920x1080. The bottom 150 px is the caption band, a strip of page background the recorder adds to the page
  (so it follows the theme). The stage lives in the 1920x930 area above it.
- Camera = the `#stage` CSS transform (the app already scales `#stage` and its tooltips and canvases follow the
  scale). Wide shot: the whole stage at 0.861. Close-ups: a stage rectangle scaled to fill the safe area, eased
  0.9 s. Text and canvases re-render at the new scale, so close-ups are sharp.
- Pointer: an arrow the recorder draws on top of the page at the position of the real (synthetic) mouse events, with
  a ring on click. It turns into a hand over clickable things. It fades out when idle.
- Scenario steps are advanced by clicking the stage's own "Next" button, so the viewer sees cause and effect.
- Composited afterwards: caption text, a label (scenario title and step), a fast-forward badge, a title card over
  the first seconds and an end card with the measured results. Long waits are time-lapsed and always carry the
  badge.

## Captions

- At most 2 lines, about 60 characters each, 42 px Geist, centred in the band.
- Reading time per caption: 1.0 s + 0.33 s per word, at least 2.5 s, at most 8 s. The recorder holds that long.
- Plain words. Define a term the first time ("lease: the copy that answers reads and writes").
- Facts come from the take: `d.fact()` values are interpolated; `d.check()` failures make the take invalid.
- Sidecar .srt and .vtt for each video, same cues.

## Deliverables

`recordings/` (git-ignored):

- `final/<scene>-<theme>.mp4`: captioned, 1080p30 H.264. `.srt` and `.vtt` next to it.
- `clean/<scene>-<theme>.mp4`: same cut without captions and cards.
- `takes/<scene>-<theme>/master.mkv` (lossless) and `take.json` (timeline, facts, capture stats).

## Left to the builder

Scene-by-scene caption wording and camera choices, judged by looking at frames from the result.

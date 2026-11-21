# Practice script: the two local demos

For the re:Invent 2026 session, slide 20 of the deck ("Demo: 01 · Stays up, 02 · Stays correct"). Both run on this
laptop with no network. Screen time is about four minutes at a talking pace.

The two Continuum demos on slide 35 (virtual clusters, Aegis) are not in here. They need the shared service account.

Everything below was run end to end on 2026-11-19 with the same keys and clicks, in Chrome, against a cluster like the
one on this laptop now: Acme Payments, CockroachDB v26.4.1, nine nodes, us-east-1 / us-west-2 / eu-west-1. Six
reference screenshots are in `recordings/session-practice/`. The cluster itself was built again on 2026-11-20, when
the full proof run passed on this code (62 checks, none failed); the layout and the version are the same.

## Before you travel

- **Do not run `./run.sh down`.** It deletes the cluster. `./run.sh up` afterwards builds a new one with six nodes, and
  you would have to add a node to each region again (`P` → Cluster → Scale → Add node, one region at a time).
- After a restart of the laptop: start Podman, run `./run.sh up`, and check `./run.sh status`. The control plane
  reconnects to the cluster and starts its nodes again. That path exists in the code; it was not tried today, so try it
  once at home.
- Do one full rehearsal with Wi-Fi off. The stage loads nothing from the internet (checked), but nobody has run it
  offline yet.
- The deck is Google Slides. Turn on offline access for it, or bring a PDF.
- The cluster's license is good until the end of January 2028 (read from the cluster's own expiry metric).

## Ten minutes before

1. Close everything else that works the laptop: builds, tests, other databases, other containers. Plug in the power.
2. `./run.sh status` says `Acme Payments (aws, multi-region): healthy, 9/9 nodes live, survival goal region`.
3. Chrome, `http://localhost:8088`. Reload once with Cmd+Shift+R the first time (the stage changed on 2026-11-19).
   `F` for full screen. `T` switches between dark and light.
4. `P` opens the drawer. **Workload** → **Stop** if Payments is running, then **Start**. Starting it fresh empties the
   "Recovered in" tile of whatever a rehearsal left there.
5. Still in the drawer: **Scenarios** → scroll down to **Agents race for one budget** (second entry under Tier 2) →
   `Esc`. The drawer remembers the tab and the scroll position. Do steps 4 and 5 in this order: once the list is
   scrolled, the tabs are out of sight.
6. Wait two minutes for the chart to fill.

**Go or no-go:** Transactions / s holds about 250 and the chart is a flat band. If it sits below about 230, or the band
is ragged, something else is using the laptop. Find it and stop it. With another database test running during rehearsal,
Payments fell to 100–190 a second and each race took 14–32 seconds instead of 7–10. The answers were still right; it
just looked bad.

The tiles should read: Live nodes **9/9** · Unavailable ranges **0** · Transactions / s **about 250** ·
Money lost **$0**, "Total $10,000,000 ✓" · Recovered in **–**, "no failures yet".

## Demo 1: Stays up

Slide: "Kill a node mid-workload. Queries keep running." About 90 seconds.

| Do | See | Say |
| --- | --- | --- |
| Switch to the stage. | The tiles as above. | "This is a payments system on CockroachDB, running on this laptop: nine nodes laid out as three AWS regions, with the real network delay between them. About 250 payments a second. Each one moves money between two accounts, so the total never changes: ten million dollars. This tile checks it." |
| Click a node tile in us-east-1, then **Crash** ("hard kill, no warning"). | Within 2 seconds the tile turns red and reads "down". A red **1 NODE DOWN** pill appears top right. us-east-1 reads DEGRADED. The chart gets a red CRASH line. | "When a database goes down, every agent on it stops. So I'll kill a node. Not a clean shutdown: the process dies with payments in flight." |
| Wait. Count to seven. | Live nodes **8/9**, "1 down" (about 7 seconds after the click: that is the cluster declaring the node dead). Unavailable ranges still **0**, with "under-replicated · healing" beneath. Transactions / s still about 250; it can sag for a few seconds. Money lost **$0**. Recovered in: **0.0 s**, "no dip in throughput", or 2 to 5 seconds, "back to 80% of normal". | "Eight of nine. Unavailable ranges: zero. Payments are still committing. Money lost: zero dollars." Then read the last tile. Either "No dip at all." or "About N seconds to get back to full speed. That node was leading some of the data, and another copy took over. Payments in flight retried. None failed." |
| Click the red tile, then **Recover**. Or press `9`, which recovers everything. | The tile is green again in 4 to 8 seconds and Live nodes reads 9/9. A green RECOVER line on the chart. "Under-replicated" counts down to "all data reachable" in 10 to 15 seconds. | "I bring it back. It rejoins, catches up on what it missed, and takes traffic again. Nobody ran a runbook." Then: "Staying up is half of it. The other half is staying correct, and agents make that harder." |

Which node: any. The number after `L` on a tile is how many ranges that node leads; the higher it is, the more likely
you see a dip of a few seconds. In twelve kills on 2026-11-19 there was no dip in eight, and 2 to 5.4 seconds in four.
No payment failed and no money was lost in any of them. Say what the tile says.

`1` kills a random node without the mouse. You do not choose which, so point after it happens.

## Demo 2: Stays correct

Slide: "Fifty agents share one budget. Overspend is rejected and retried." About 75 seconds. Leave Payments running.

| Do | See | Say |
| --- | --- | --- |
| `P`. The drawer opens on the scenario. **START** beside "Agents race for one budget". `Esc`. | A panel opens bottom right: "Agents race for one budget", "Preparing…" for 3 to 5 seconds, then a green button **START: RACE UNDER READ COMMITTED ▸**. Wait for that button. | "Same cluster, payments still running. Now fifty agents share one budget: ten thousand dollars. Each agent reads what has been spent and, if there is room, spends two hundred and fifty. Forty of them fit. All fifty go at the same instant." |
| `N` | "Running…" at once. After 7 to 10 seconds: Agents approved **50 of 50** · Declined **0** · Conflicts retried **0** · Spent **$12,500 of $10,000** · Budget **OVERSPENT by $2,500 ✗** | While it runs: "First under READ COMMITTED, the default isolation level in many databases." On the result: "Fifty of fifty approved. Twelve thousand five hundred spent from a ten thousand dollar budget. Nothing errored. Every agent saw money left, so every agent spent. Each decision was right on its own, and the total is wrong." |
| `N` | "Running…", then after 6 to 10 seconds: Agents approved **40 of 50** · Declined **10** · Conflicts retried **300 to 340** · Spent **$10,000 of $10,000** · Budget **held ✓**. The button reads DONE. | While it runs: "Same agents, same code. One change: SERIALIZABLE, which is CockroachDB's default." On the result: "Forty approved, ten declined, exactly ten thousand dollars. Where agents collided, the database rejected one and it tried again." Read the retry count off the screen. "No agent had to know about the other forty-nine." Then the deck's line: "Agents race. The numbers still add up." |
| Click **END** in the panel. Back to the slides. | The panel closes. | "That is one database. Now a thousand of them." |

The two results do not vary: 50 of 50 and $12,500, then 40, 10 and $10,000. Only the retry count and the seconds do.
Payments can sag for a few seconds while a race runs. The fifty agents are on the same cluster.

`→` does the same as `N`. If your slide clicker sends `→`, it works on the stage too, which also means a stray press
there runs the next step.

Say "READ COMMITTED, the default in many databases". The deck's notes name PostgreSQL; on AWS's floor that reads as a
knock on a database AWS sells.

## If something goes wrong

- **`N` does nothing.** The drawer still has the keyboard. Press `Esc` or click the stage, then `N`. The green button in
  the panel does the same thing.
- **"a step is still running"** appears at the bottom. You pressed twice. It is running; wait.
- **The node does not come back.** `9` recovers everything.
- **A race shows a "No outcome" line.** Some agents could not finish. **END**, then start the scenario again. It did not
  happen in any rehearsal.
- **The stage stops updating.** Reload the page. Nothing is lost: the state is in the control plane. The drawer keeps
  its tab after a reload but not its scroll position, so scroll down to the scenario again.
- **Nothing works.** `recordings/share/dark/01-survive-zone.mp4` covers the first demo. There is no recording of the
  budget race yet.

## To run it again

**END** the scenario if its panel is open. `9`. Then `P` → scroll up → **Workload** → **Stop**, **Start** →
**Scenarios** → scroll back to the scenario → `Esc`. Give the chart a minute.

## If you are asked

- **"Would SELECT FOR UPDATE have fixed the first run?"** Yes. So would putting the check in the UPDATE itself. It works
  when every agent's code does it. SERIALIZABLE makes the database refuse the bad result whoever wrote the agent.
- **"What do the retries cost?"** Here, about 320 retries for fifty writers on one row at the same instant, finished in
  under ten seconds. That is the worst case on purpose. The client has to retry; each agent here does it in a short loop.
- **"Is that a real cluster?"** Nine CockroachDB v26.4.1 nodes in containers on this laptop, the same binary as
  production, with network delay between the regions set from their real distances. It is on GitHub
  (`viragtripathi/cockroach-chaos-demo`); anyone can run the same thing.
- **"Why did the node count change a few seconds late?"** The tile turns red when the process dies. The count changes
  when the cluster itself declares the node dead.

The stage rules from the story brief still apply: no savings percentages, no AWS database service by name, Aegis only
observes and recommends.

## What the numbers were in rehearsal

Measured on 2026-11-19. Other work was running on the laptop for part of it, which is where the right-hand column
comes from.

| | Laptop to itself | Laptop busy |
| --- | --- | --- |
| Payments at speed after Start | 4 to 15 s (up to 40 s the first time after the control plane starts) | the same, at a lower rate |
| Payments rate | 245 to 260 a second | 100 to 190 a second |
| Tile turns red after Crash | about 2 s | about 2 s |
| Live nodes reads 8/9 | about 7 s after the click | 10 s or more |
| Recovered in | 0.0 s in 8 of 12 kills; 2.2, 4.5, 5.1 and 5.4 s in the others | the same |
| Node back after Recover | 4 to 8 s | the same |
| Scenario ready after START | 3 to 5 s | the same |
| Race under READ COMMITTED | 7 to 10 s | 14 to 23 s |
| Race under SERIALIZABLE | 6 to 10 s | 10 to 32 s |
| Conflicts retried | 300 to 340 | up to 500 |

## Variant: a whole zone instead of one node

Slides 14 and 15 are about losing a zone. The guided scenario **Survive a zone outage** (second under Tier 1) does it
in four presses of `N`: Start payments · Take down us-east-1a · Measure the impact · Bring us-east-1a back. Its panel
shows Recovery time, Money lost and Transfers retried. In four runs: recovery 0 to 9 seconds, $0 lost, 0 to 21
transfers retried.

On this layout a zone holds one node, so it looks like the node kill with a results panel. One thing to know: with
Payments already running the first step finishes at once, so a double press of `N` takes the zone down straight away.

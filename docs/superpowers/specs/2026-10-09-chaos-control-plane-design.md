# CockroachDB Chaos Demo: configurable control plane, showcase and explainers

- **Date:** 2026-10-09
- **Status:** Draft for review
- **Branch:** `feature/control-plane` (local only; nothing is pushed until the user has tested locally and approves)

## 1. Purpose

This repo is a live demo of CockroachDB resilience. Presenters run it on a laptop, on stage or at a conference booth, often alongside the [Banko AI Assistant](https://github.com/cockroachlabs-field/banko-ai-assistant) loader. This redesign turns it into a configurable, cloud-looking showcase. Region names, clouds and node counts come from presets instead of being hardcoded, and audiences never see that it runs locally.

### 1.1 What the user asked for

1. Fix whatever is broken.
2. Inject chaos into individual nodes, not just whole regions.
3. Add more regions, and scale out and back in.
4. **Most important:** make region names fully configurable, so they can match AWS, GCP, Azure, IBM or any other naming.
5. Rethink "Simulate Transactions". The demo should work with or without Banko.
6. The **full showcase** in this round: resilience plus isolation levels, schema changes, CDC, follower reads, REGIONAL BY ROW and GLOBAL tables.
7. Presets plus a panel picker, **hidden from the audience**, so the demo mimics the cloud.
8. The Cockroach Labs **Continuum** brand look, legible on big screens and in booths, with a **dark/light switch**.
9. Hooks for future **physical triggers**, such as plastic roaches smashed with a hammer.
10. Motion-graphics **explainers** for sales engineers and executives: one for the chaos demo, one for Continuum.
11. Keep working with Banko.
12. Test locally before anything is pushed.

### 1.2 Assumptions (confirmed during brainstorming)

- The typical setting is one presenter on a laptop, on Docker Desktop or rootless Podman.
- Booths also need an unattended, smashable mode.
- Topology changes (add node, add region, decommission) happen **live**, because watching rebalancing is the compelling part.
- The Banko contract stays as it is: HAProxy on `localhost:26257`, with `defaultdb`'s multi-region configuration owned by this repo.

### 1.3 Success criteria

- Picking a preset (or typing custom regions) produces a correctly named multi-region cluster that looks like a real cloud deployment, in the stage view and in the DB Console alike.
- Any node, zone or region can be crashed, stopped, partitioned, isolated or browned out, then recovered, from the stage, the presenter console, the keyboard or an HTTP trigger.
- Nodes and regions can be added and removed live, and the stage shows replicas and leases moving.
- A live chart shows any client's traffic (including Banko's) riding through failures. A built-in workload proves that **$0 is lost**.
- Every Tier 1 and Tier 2 scenario runs from the stage with a guided narrative.
- Everything works on CockroachDB v25.4 and v26.x, on rootless Podman and on Docker Desktop.
- Both explainers play in the browser in both themes and export to 1080p MP4.

## 2. Current state and defects (verified 2026-10-09 against a running v26.3.2 stack on rootless Podman)

| # | Defect | Evidence | Fixed by |
|---|---|---|---|
| D1 | `init.sql` fails on v26 at its first statement (access to `system`/`crdb_internal` is restricted). Nothing else in it runs: no `demo_transactions` table, no primary region, no `SURVIVE REGION FAILURE`. | `crdb-bootstrap` exited with code 1. `SHOW REGIONS FROM DATABASE defaultdb` returns 0 rows. | §4.6 bootstrap in the control plane, which applies `allow_unsafe_internals` and falls back on older versions |
| D2 | "Simulate Transactions" never succeeds on v26. Errors are swallowed. | `POST /api/simulate-writes?count=3` returns `{"success":0,"failed":3}` | §7 workload replaces it |
| D3 | "Active Nodes" counts dead nodes (it counts liveness records that aren't decommissioning). | Query semantics of `crdb_internal.gossip_liveness` | §7.1 liveness from `gossip_nodes.is_live` |
| D4 | The transaction counter lives in each of 4 gunicorn worker processes, so polled values flicker. | `gunicorn.conf.py` has `workers=4`; the counter is a module global | §4.4 single process with one cached snapshot |
| D5 | Partition can silently do nothing. Network detection depends on the folder name, its fallback matches any network containing "default", and errors are swallowed. | `_get_docker_network()` | §4.2 fixed network name, plus §12 verification of every action |
| D6 | A browned-out region still shows green "REACHABLE". | The `status-degraded` CSS is never applied | §9 node and region state model |
| D7 | The CLI demo scripts fail on v26 (`crdb_internal` without the opt-in) and mislabel nodes: node IDs depend on join order (on the live cluster, n2 = `crdb-c1`, n5 = `crdb-e1b`). | Live `gossip_nodes` output | §8 scenarios replace the scripts |
| D8 | The README says SUSPECT after ~5s and DEAD after ~9s. Actually nodes are suspect after ~9s and dead after 5 minutes (`server.time_until_store_dead`), so up-replication never shows during a demo. | `SHOW CLUSTER SETTING server.time_until_store_dead` → `00:05:00` | §6.4 sets it to the minimum; docs rewritten |
| D9 | The CRDB version is set in 4 places that disagree (compose v25.4.14, `.env` v25.4.13, README v25.4.10, running v26.3.2). | Files in the repo | §4.7 a single source |
| D10 | The Dockerfile installs the whole Docker Engine (`get.docker.com`) when only the CLI is needed. | `backend/Dockerfile` | §15 CLI-only install |
| D11 | The backend runs `privileged: true` for no reason. | `docker-compose.yml` | §15 removed |
| D12 | The DB Console link points at `crdb-e1a`, so it dies when that node is killed. | `get_config()` | §4.3 HAProxy fronts the console |
| D13 | "3 regions / 5 nodes" is hardcoded in compose, HAProxy, Toxiproxy, SQL, backend, UI, scripts and docs. | Many files | §5 presets |
| D14 | The `replicas` metric equals `ranges` (it counts rows, not replicas). | `/api/cluster-health` returned `ranges: 58, replicas: 58` | §7.1 |
| D15 | `run.sh` reports success even when the backend never came up. | `run.sh` | §12 preflight and readiness checks |
| D16 | The isolation demo claims READ COMMITTED produces an anomaly, but its UPDATEs re-read balances, so no anomaly ever occurs. | `demo_isolation.py` | §8 a real write-skew scenario |

## 3. Decisions made during brainstorming

| Decision | Choice |
|---|---|
| Scope | Full showcase in one spec, delivered in six milestones (§16) |
| Topology UX | Presets plus a presenter-only picker; rebuilding from the panel is allowed |
| Core approach | **The control plane provisions and operates the nodes itself.** Compose runs only the fixed pieces |
| Stage layout | **B: region columns** (big clickable node tiles, full-width traffic chart) |
| Brand | Continuum tokens and Geist type; dark/light switch |
| Brownout and latency | `tc netem` via a helper container (verified on rootless Podman). Toxiproxy is removed |
| CLI scripts | Retired once their scenarios exist in the panel |
| Explainers | Two (chaos, Continuum), shipped with the panel, exportable to MP4 |
| Git | Local branch `feature/control-plane`, one commit per milestone, no pushes until the user approves |

## 4. Architecture

```
   Stage screen / presenter device
              │  HTTP + Server-Sent Events
┌─────────────▼──────────────── docker compose: 2 fixed services ─┐
│ control-plane (FastAPI, one process, background workers)        │
│   provisioner · chaos · elasticity · netem · collectors         │
│   workload · scenarios · booth/triggers · explainers (static)   │
│ load-balancer (HAProxy)  :26257 SQL  :8080 DB Console  :8404    │
└─────────────┬────────────────────────────────┬──────────────────┘
              │ Docker/Podman API (socket)     │ SQL + node HTTP
┌─────────────▼────────────────────────────────▼──────────────────┐
│ CockroachDB nodes, created at runtime, one /24 subnet per region │
│ each node: netem rules toward every other region's subnet        │
└──────────────────────────────────────────────────────────────────┘
```

### 4.1 Compose

`docker-compose.yml` declares only two services:

- **`control-plane`** is built from `controlplane/` and tagged `crdb-chaos-control-plane:local`, so it can reuse its own image as the netem helper.
  - Mounts: the Docker socket (configurable `DOCKER_SOCKET`), `./presets` (rw, for saving custom presets), `./catalog` (ro), the named volume `control-data` (settings) and the named volume `haproxy-config` (rw).
  - Port: `${PANEL_PORT:-8088}`.
- **`haproxy`** is `haproxy:lts`. The service and container keep the name `haproxy`, so containerized clients such as Banko can keep using `haproxy:26257`.
  - Its config is read from the `haproxy-config` volume. The command waits for the file before exec'ing `haproxy -W -db`.
  - Ports: 26257, 8080, 8404.

### 4.2 Network

- **Network:** a fixed name, `crdb-chaos`, with subnet `${CHAOS_SUBNET:-10.77.0.0/16}`.
- **Infrastructure addresses:** `10.77.0.10` (HAProxy) and `10.77.0.11` (control plane).
- **Region k** (1-based) gets `10.77.k.0/24`.
- **Node m** in a region gets `.(10+m)`.
- **Static IPs** survive kill/start and are restored on reconnect with `docker network connect --ip` (verified on rootless Podman).

### 4.3 HAProxy

- **Ownership:** the control plane renders the full config on every membership change, writes it to the shared volume and sends `SIGUSR2` to reload it. Existing connections stay on old workers, with `hard-stop-after 10m`.
- **Frontends:**
  - **SQL :26257:** `roundrobin`, `option pgsql-check user root`, `on-marked-down shutdown-sessions`, 30-minute client/server timeouts (as today), servers addressed by static IP.
  - **DB Console :8080:** `balance first`, with `option httpchk GET /health`. The console stays reachable whichever node dies.
  - **Stats :8404:** linked only from the presenter console.

### 4.4 Process model

- **Server:** a single uvicorn process (no multi-worker gunicorn).
- **Background workers:**
  - **Collector:** every 1–2s it builds one immutable **snapshot**.
  - **Operations executor:** long jobs, serialized per target.
  - **Workload:** see §7.
  - **Booth:** auto-heal and attract loop.
  - **Event bus:** chaos events plus cluster reactions.
- **Serving:** every GET serves the latest snapshot, and SSE pushes snapshots and events. 50 viewers cost the same as one.

### 4.5 Persistence

- **Settings:** stored as JSON on the `control-data` volume (active preset, latency mode, booth settings, trigger map, counters, theme default).
- **Runtime truth:** node containers carry labels (`crdb-chaos.node`, `.region`, `.zone`, `.provider`, `.ordinal`, `.ip`). On startup the control plane **adopts** labeled containers instead of recreating them. It then re-renders the HAProxy config and re-applies the netem rules.

### 4.6 Bootstrap (replaces `init.sql`)

Executed by the control plane over psycopg2:

1. **Internals access:** connect with `options='-c allow_unsafe_internals=on'`. If the server rejects the variable (pre-v26), retry without it.
2. **License** (optional): `SET CLUSTER SETTING cluster.organization`, `enterprise.license` (from `CRDB_ORG` / `CRDB_LICENSE`).
3. **Demo settings:** `server.time_until_store_dead` = the minimum allowed (expected `1m15s`, configurable). `kv.rangefeed.enabled = true` (for the CDC scenario).
4. **Locations:** `UPSERT INTO system.locations` for each region (map coordinates for the DB Console).
5. **Databases:**
   - For `defaultdb` (Banko) and `chaos` (workload and scenarios): `SET PRIMARY REGION`, then `ADD REGION` for each remaining region.
   - Then `SET SECONDARY REGION` when one is designated and the survival goal is REGION, and `SURVIVE {ZONE|REGION} FAILURE` per §5.5.

Each statement is idempotent and its errors are surfaced, never swallowed.

### 4.7 Versions and images

- **Single source:** `CRDB_VERSION` in `.env`, overridable per preset. Default `v26.3.2`, supported back to v25.4.
- **Offline booths:** `./run.sh prefetch` pulls the CockroachDB image ahead of time. The netem helper uses the control-plane image, so nothing is pulled at runtime.

### 4.8 Node container spec

```
docker run -d --name <host> --hostname <host> --network crdb-chaos --ip <ip>
  --label crdb-chaos.node=1 --label crdb-chaos.region=<region> ...
  cockroachdb/cockroach:<version> start --insecure
  --advertise-addr=<host>:26257 --listen-addr=:26257 --http-addr=:8080
  --locality=cloud=<provider>,region=<region>,zone=<zone>
  --cluster-name=<cluster_id> --join=<up to 3 live seeds>
  --cache=<node_memory.cache> --max-sql-memory=<node_memory.sql>
```

- Default `node_memory`: 256 MiB cache and 256 MiB SQL. 9 nodes need about 3–4 GB.
- The first provisioning runs `cockroach init` against node 1, then the §4.6 bootstrap.

## 5. Topology and presets

### 5.1 Preset schema (`presets/*.yaml`)

```yaml
display_name: Global Payments      # stage header
cluster_id: global-payments        # --cluster-name, lowercase-hyphen slug
crdb_version: v26.3.2              # optional override
survival_goal: auto                # auto | zone | region | none
latency: realistic                 # off | realistic | <scale factor, e.g. 1.5>
node_memory: { cache: 256MiB, sql: 256MiB }   # optional
regions:
  - name: us-east-1                # CockroachDB region name (any valid name)
    provider: aws                  # aws | gcp | azure | ibm | oci | onprem | custom
    nodes: 3
    primary: true
    # optional: label, location: [lat, lon], zones: [...]
  - { name: us-west-2, provider: aws, nodes: 3, secondary: true }
  - { name: eu-west-1, provider: aws, nodes: 3 }
```

**Validation rules:**

- Region names: `^[a-z0-9][a-z0-9-]{0,39}$`, unique.
- Node count per region: 1–12. Total nodes ≤ `MAX_NODES` (default 15).
- Exactly one primary region (the first region if none is marked). At most one secondary region.
- Unknown providers are allowed.
- Validation errors are shown in the presenter console and printed by `./run.sh`.

### 5.2 Shipped presets

| Preset | Regions (nodes) |
|---|---|
| `aws` (default) | us-east-1 (3, primary), us-west-2 (3, secondary), eu-west-1 (3) |
| `gcp` | us-east1 (3), us-central1 (3), europe-west1 (3) |
| `azure` | eastus (3), westus2 (3), westeurope (3) |
| `ibm` | us-south (3), us-east (3), eu-de (3) |
| `multi-cloud` | aws us-east-1 (3), gcp us-central1 (3), azure westus2 (3) |
| `classic` | today's shape with real AWS names: us-east-1 (2), us-west-2 (2), us-east-2 (1) |
| `small` | aws us-east-1, us-west-2, eu-west-1, 1 node each (laptops with 8 GB) |

### 5.3 Region catalog (`catalog/regions.yaml`)

- Per provider: region name, city label, `[lat, lon]`, and real zone names (e.g., GCP `us-east1-b/c/d`, Azure `eastus-1/2/3`, IBM `us-south-1/2/3`, AWS `us-east-1a/b/c`).
- An approximate RTT table for pairs used by the shipped presets, documented as approximate.
- The presenter picker offers catalog dropdowns per provider, plus free text.

### 5.4 Naming and latency model

- **Hostname:** `<zone>-n<k>` (e.g., `us-east-1a-n1`, `eastus-2-n4`), with the provider prefixed only on collisions. Zones are assigned round-robin.
- **Inter-region RTT:** the catalog override if present. Otherwise `2 · distance_km / 200 · 1.5 + 5` ms (great-circle distance), times the preset's scale factor.
- **Intra-region (cross-zone) RTT:** 1 ms.
- **netem implementation:** one-way delay = RTT/2 on each node's egress, via a `prio` qdisc with one `netem` band per remote region subnet (u32 dst filters). An extra default band carries clients and infrastructure.
- **Jitter:** 0 by default, to avoid packet reordering.
- **Toggle:** latency can be switched live in the presenter console (≈2s to re-apply to all nodes).

### 5.5 Survival goal (`auto`)

`auto` picks the first rule that matches:

1. **REGION** when there are ≥3 regions, ≥5 nodes, and the primary region has ≥2 nodes. That gives 5 voters, 2 of them in the primary region.
2. **ZONE** when the primary region has ≥3 nodes. That gives 3 voters in the primary region.
3. **`none`** otherwise.
   - The databases are not made multi-region; the cluster uses plain 3× replication.
   - That still spreads one replica per region, so it survives a region outage.
   - Scenarios that need multi-region SQL show as unavailable, with the reason.
   - This is the outcome for the `small` preset.

The validator warns when an explicitly chosen goal can't be fully satisfied, and the stage would otherwise show permanently under-replicated ranges. Scenario requirements also cover per-region node counts. For example, REGIONAL BY ROW under REGION survival needs ≥2 nodes in every region that homes rows.

### 5.6 Selection and rebuild

- **Command line:** `./run.sh up [--preset NAME]`, `./run.sh down`, `./run.sh reset`, `./run.sh prefetch`, `./run.sh status`. The default comes from `PRESET` in `.env`.
- **Presenter console:** choose a preset or edit a custom topology, then **Provision**. Custom topologies can be saved to `presets/custom/<slug>.yaml`.
- **Rebuild job:**
  1. Drain the HAProxy config.
  2. Remove the node containers.
  3. Provision the new topology.
  4. Init.
  5. Bootstrap.
  6. Apply netem.
  7. Render the HAProxy config.

  About 60–90s. The stage shows a cloud-style provisioning sequence.
- A rebuild wipes all data (Banko must be restarted). Live add/remove of regions doesn't wipe data.

## 6. Chaos, elasticity, booth mode, triggers

### 6.1 Failure actions

| Target | Action (stage label) | Mechanism | Undo |
|---|---|---|---|
| Node | Crash | `docker kill -s KILL` | start |
| Node | Planned stop | `docker stop` (SIGTERM, CockroachDB drains) | start |
| Node | Partition | `docker network disconnect` | reconnect with the same IP, then re-apply netem |
| Node | Brownout | netem delay/jitter/loss added to all bands (defaults 700 ms ±200, 2% loss) | re-render netem |
| Zone | Zone outage | crash every node in the zone | start |
| Region | Region outage | crash every node in the region | start |
| Region | Partition | disconnect every node in the region | reconnect |
| Region | Isolate (split-brain test) | netem 100% loss toward other regions only; clients still connect | re-render netem |
| Region | Brownout | node brownout on every node in the region | re-render |
| Any | Recover | idempotent: start / reconnect / re-render netem | — |
| Cluster | Recover all | Recover on every node | — |

### 6.2 Elasticity

- **Add node (region):**
  1. Allocate a name, IP and zone, then `docker run`.
  2. Wait for liveness.
  3. Apply netem and update the HAProxy config.
  4. The stage animates replica and lease counts rising on the new node.
- **Decommission node:**
  1. `cockroach node decommission <id> --wait=none` via `docker exec` on a live node.
  2. Poll the node's replica count down to 0 and its membership to `decommissioned` (progress shown).
  3. Stop and remove the container, then update the HAProxy config.

  The panel refuses if this would leave the region with 0 nodes (use Remove region instead). CockroachDB's pre-check errors are surfaced.
- **Add region:**
  1. Allocate the next subnet and create its nodes.
  2. `UPSERT system.locations`.
  3. `ALTER DATABASE defaultdb ADD REGION` and the same for `chaos` (so Banko sees it).
  4. Re-render netem on **all** nodes and update the HAProxy config.
  5. If the cluster now qualifies for REGION survival, offer the upgrade.
- **Remove region:**
  - Refused for the primary region (the panel offers "make another region primary" first).
  - If it's the secondary region, `DROP SECONDARY REGION` first.
  - Then `ALTER DATABASE … DROP REGION` on both databases, decommission its nodes, remove them, and re-render netem and the HAProxy config.

### 6.3 Survival budget guard

Given the survival goal and the current state, the panel predicts whether an action would make ranges unavailable. Examples:

- a second region outage under REGION survival
- a primary-region outage under ZONE survival
- losing quorum within the primary region under ZONE survival

Presenter: a warning plus confirmation. Booth mode: refused, and random targets pick only safe victims.

### 6.4 Visible self-healing

`server.time_until_store_dead` is set to the minimum (verified during M1), so a crashed node becomes DEAD and its replicas are rebuilt elsewhere within about 2 minutes. That only happens where placement constraints allow it; for example, a whole-region outage can't re-replicate until the region returns, and the stage says so.

### 6.5 Booth mode

- **Auto-heal:** Recover all, N seconds after the last hit (default 45, with a countdown ring on stage).
- **Safe random targeting:** see §6.3.
- **Persistent counters:** nodes crashed, regions taken down, transactions served, money lost.
- **Sound effects:** optional, off by default.
- **Attract loop:** after N minutes idle (default 2), play the explainer playlist. Any key or trigger returns to the stage.

### 6.6 Triggers (hardware hooks)

- **Endpoint:** `POST /api/triggers/{id}`, with an optional `X-Trigger-Token` (required if `TRIGGER_TOKEN` is set). The response echoes the resolved action and target.
- **Mapping** (presenter console): `id → { action: crash|stop|partition|brownout|zone-outage|region-outage|isolate|recover-all, target: node|zone|region|random-node|random-region|random-safe [, value] }`.
- **Keyboard:** stage keys `1`–`9` map to trigger IDs, so USB HID devices (Makey Makey, big red button, foot pedal) work with no code.
- **Docs:** `docs/hardware-triggers.md` has ESP32 (HTTP) and Makey Makey examples.

## 7. Traffic

### 7.1 Cluster-side metrics (any client, including Banko)

- **Node metrics, every 2s:** the control plane fetches `http://<node-ip>:8080/_status/vars` from each reachable node (in parallel, 700 ms timeout).
  - It computes rates (counter resets are treated as 0): statements/s, `sql_txn_commit_count`, aborts, `txn_restarts` (retries), `sql_failure_count`, `sql_conns`, and p99 from `sql_service_latency` histogram bucket deltas.
  - The results are aggregated per region and per cluster.
- **SQL, every 2s:**
  - `crdb_internal.gossip_nodes`: `is_live`, `ranges`, `leases`
  - `crdb_internal.gossip_liveness`: draining, membership
  - `crdb_internal.kv_store_status`: `ranges.unavailable` and `ranges.underreplicated`, summed (about 10s stale, labeled as such)
- **HAProxy:** stats CSV for per-node up/down and current sessions.
- **Node state model** (stage):
  - live
  - suspect (liveness lapsed for less than `time_until_store_dead`)
  - dead
  - draining or decommissioning
  - plus control-plane flags: crashed, stopped, partitioned, isolated, browned out

### 7.2 Built-in "Payments" workload

- **Data:** database `chaos`, table `accounts(id INT PRIMARY KEY, balance INT NOT NULL)` holding 10,000 accounts × $1,000 = **$10,000,000**.
- **Workers:** 16 by default, each with a dedicated connection through HAProxy. Each records its gateway region with `SELECT gateway_region()` per connection.
- **Transfers:** debit and credit between random accounts in one SERIALIZABLE transaction, paced to a target rate (default 250 tx/s).
  - Client-side retry with jittered exponential backoff on 40001.
  - Reconnect with backoff when a connection drops.
  - `statement_timeout = 5s`.
- **Statistics:** per second, for commits, retries, and errors by class (connection lost, timeout, ambiguous result 40003), plus p50/p99.
- **Invariant:** every 5s, `SELECT sum(balance)` must equal $10,000,000. The stage shows **"Total balance $10,000,000 ✓ · Money lost $0"**. Every transfer leaves the total unchanged, so ambiguous commits cannot change it.
- **Recovery time:** from the failure action to the first 2-second window in which commits reach ≥80% of the pre-failure 10-second average.
- **Controls:** start/stop on stage; workers, rate and read/write mix in the presenter console.

## 8. Showcase scenarios

Each scenario is a module with:

- **requirements:** minimum regions and nodes, whether latency must be on
- **steps:** label, large caption, action, observation
- **live results**
- **reset:** drops its tables and restores settings

The stage renders a guided card, and the presenter can advance with buttons or the keyboard.

**Tier 1: resilience and multi-region**

1. **Survive a region outage.** With the workload running, a region goes down. The scenario shows recovery time, unavailable ranges at 0, and $0 lost. Then recover and watch self-healing.
2. **ZONE vs REGION survival.**
   - Under ZONE survival, a primary-region outage stalls writes (ranges become unavailable).
   - Recover, switch live with `ALTER DATABASE chaos SURVIVE REGION FAILURE` (waiting for rebalancing, with progress shown), and repeat. This time it survives.
3. **Elastic scale.** Add nodes or a region and watch replicas and leases rebalance. Decommission a node and watch it drain.
4. **REGIONAL BY ROW.** Rows are pinned to each region. A latency heatmap measures reads and writes from each region's gateway (direct node connections): local ≈1–2 ms vs remote ≈ round trip.
5. **Follower reads.** The same query from a far region, normally and with `AS OF SYSTEM TIME follower_read_timestamp()`.
6. **GLOBAL tables.** Reads are local everywhere; writes cost more. The scenario explains when to use which.

**Tier 2: transactions and operations**

7. **Isolation levels: real write skew.**
   - A joint-account rule (checking + savings ≥ 0). Two concurrent withdrawals each check the sum.
   - READ COMMITTED lets both commit and breaks the rule. SERIALIZABLE retries one and keeps it.
8. **Online schema change under load.** `ADD COLUMN` and `CREATE INDEX` while the workload runs, with job progress from `SHOW JOBS` and zero errors.
9. **Changefeeds.**
   - A `CREATE CHANGEFEED` streams into a webhook sink hosted by the control plane (HTTPS with a self-signed certificate, `insecure_tls_skip_verify=true`). Events stream on stage.
   - Crash the node running the changefeed, and it resumes elsewhere with no gaps (resolved timestamps keep advancing).
10. **Split-brain test.** Isolate a region (§6.1). The isolated side refuses to commit while the majority continues. After healing, the data hasn't diverged.
11. **Time travel.** An "accidental" `DELETE`, then read with `AS OF SYSTEM TIME '-30s'` and restore with `INSERT … SELECT`.

**Tier 3: later.** Rolling version upgrade under load, backup/restore, virtual clusters. The node lifecycle stays compatible with adding per-node volumes later.

## 9. UI

### 9.1 Stage view (`/`), layout B

- **Header:** wordmark, cluster display name, provider and region-count chips, survival-goal chip, live incident chip, clock.
- **KPI strip:** live nodes, unavailable ranges, under-replicated, transactions/s (all clients), p99, money lost, recovery time.
- **Region columns:**
  - Provider badge, region name, city and status.
  - Zone sub-columns with **node tiles** showing name, state, and replica and lease counts.
  - Region action buttons; click a node tile for its menu.
  - With more than 4 regions, the columns become a horizontally scrolling or wrapped grid.
- **Traffic chart:** full width, 2 minutes rolling, stacked by gateway region, with event markers.
- **Overlays:** event ticker, scenario card, booth counters.
- **Scaling:** a 1920×1080 design scaled proportionally to any screen.
- **More than 4 regions:** up to 4 region columns per row. Further regions wrap to a second row with compact node tiles.
- **Stage hotkeys:** `1`–`9` fire triggers (§6.6), `P` toggles the presenter drawer, `T` switches theme, `F` toggles fullscreen, `E` starts the explainer playlist and `Esc` closes overlays.

### 9.2 Presenter console (`/presenter`)

- Responsive, so it works on a phone. It can also open as a hidden drawer on the stage via a hotkey.
- **Tabs:**
  - **Cluster:** presets, custom editor, provision, latency, version, license status.
  - **Chaos:** every target and action.
  - **Workload**
  - **Scenarios**
  - **Booth:** auto-heal, triggers, keyboard map, counters, sound, attract loop.
  - **System:** real endpoints (localhost URLs, connection string for Banko, DB Console, HAProxy stats), logs, operations.

### 9.3 Cloud illusion rules (stage only)

- Never show "docker", "container", "podman", "localhost" or ports.
- Use cloud-style hostnames and zones.
- No connection string.
- Chaos wording is cloud-native: crash, outage, partition, brownout.
- Provisioning reads like a cloud ("Allocating capacity in us-east-1…").
- Kiosk/fullscreen is recommended; the docs cover the optional hosts-file hostname trick.

### 9.4 Themes and brand

Tokens from cockroachlabs.com (Continuum, September 2026):

| Token | Dark | Light |
|---|---|---|
| Background | Digital Black `#000000` | Timeless Taupe Light `#f9f8f3` |
| Text | Clarity White `#ffffff` | `#0b0b0b` |
| Muted | `#888884` | `#5f5f5a` |
| Healthy | Bio Green `#aede54` | fill `#aede54`, line/text `#43541f` |
| Info/traffic | Sci-Fi Blue `#60ecff` | `#144f6b` |
| Brand/actions | Data Purple `#9059ff` / `#c8acff` | `#7a3ff0` / `#502482` |
| Degraded (off-palette) | `#ffb547` | `#b86e00` |
| Failed (off-palette) | `#ff4d5e` | `#d42a41` |

- **Type:** Geist for headlines (weight 200 at large sizes, 300–400 for numbers), Geist Mono for eyebrows (letter-spacing 2–4px).
- **Fonts:** self-hosted (OFL), so booths work offline.
- **Logo:** the new wordmark SVG, green on dark, near-black on light.
- **Theme switching:** the toggle persists in `localStorage`, and `?theme=` sets it from the URL.

### 9.5 Frontend tech

- Vanilla ES modules, no build step.
- Canvas for the map, particles and charts; DOM for type and panels.
- One `EventSource` for live state.
- All assets are local (fonts, land mask, logo).

## 10. Explainers

| Explainer | Length | Chapters |
|---|---|---|
| `chaos` | 2:13 | intro, topology, consensus, survive (hammer), heal, gray failure, elastic, locality, panel outro |
| `continuum` | 2:42 | intro, sprawl, fixed islands, one estate, Plenum (Preview), Aegis (Preview), the bill, foundation, editions outro |

- **Engine:** deterministic (every frame is a pure function of `t`). It supports scrubbing, chapter keys, `?theme=`, `?loop=1` and `?capture=1&t=`.
- **Packaging:**
  - The two prototypes (now in `.superpowers/brainstorm/.../content/`) are refactored into `web/explainers/engine.js`, `chaos.js` and `continuum.js`, with no visual changes.
  - They're served at `/explainer/chaos`, `/explainer/continuum` and `/explainer/playlist` (alternating, for booths).
- **Preset-driven chaos explainer:** when served by the panel, the chaos explainer takes region names, cities and coordinates from the active preset (templated copy), so an Azure demo plays with Azure regions. The Continuum explainer is static.
- **Exporter:** `tools/export-explainer.mjs` (Node ≥ 22, Chrome, ffmpeg; no npm dependencies) renders 1080p30 MP4s. It was already used to produce the four prototype videos.
- **Accuracy guardrails (Continuum):**
  - Every claim comes from public launch sources (press release, product page, the Kimball announcement post, the Aegis post, "9 reasons").
  - Aegis and Plenum usage-based storage are labeled **Preview**; the investigation is labeled "example".
  - Auto-scaling compute is omitted.
  - Product marketing should review it before external use (including "cluster" vs "database" virtualization naming).

## 11. Banko compatibility contract

- **Unchanged:** HAProxy at `localhost:26257`, database `defaultdb`.
- This repo configures `defaultdb`'s regions, primary/secondary and survival goal at bootstrap. Banko's `migration.py` defers to an existing primary region.
- **Region adds:** a live region add also runs `ALTER DATABASE defaultdb ADD REGION`, so Banko's signup offers the new region.
- **Rebuilds:** a rebuild wipes `defaultdb`, so restart Banko. This is documented, and the presenter console shows a notice.
- **Latency:** realistic latency slows Banko's loader. The toggle is in the presenter console.
- **Containerized Banko:** it can join the `crdb-chaos` network and use `haproxy:26257`. The docs give the exact flags.
- The workload and scenarios use the separate `chaos` database, so they never touch Banko's tables.

## 12. Reliability and error handling

- **Verification:** every action is checked after it runs (container state, network attachment, presence of the netem qdisc). The outcome is published as an event. The presenter console shows failures; the stage only reflects real state.
- **Concurrency:**
  - Operations are serialized per target and run as jobs with steps and progress.
  - Booth triggers that hit a busy target are queued, up to 1 pending per target; anything beyond that is dropped and logged.
  - Recover is idempotent.
- **Graceful telemetry:** short timeouts, with fallback from HAProxy to direct node connections. When the cluster is unreachable, the stage shows "telemetry delayed" instead of zeros.
- **Restart safety:** see §4.5 (adopt containers, re-render the HAProxy config, re-apply netem).
- **Version tolerance:** the `allow_unsafe_internals` fallback, plus queries that tolerate missing columns. Tested on v25.4.x and v26.3.x.
- **Preflight (`./run.sh up`):**
  - the container runtime and compose are present
  - free memory against the preset's estimate
  - ports 26257, 8080, 8088 and 8404 are free
  - the subnet doesn't collide with existing networks
  - the image is present (or pullable)
  - readiness wait with a clear failure message
- **Optional control token:** with `CONTROL_TOKEN` set, every mutating endpoint requires it (a cookie set via `/presenter?token=…`). The stage stays read-only for everyone else. A warning is logged when the panel is exposed without a token.

## 13. Testing

- **Unit tests** (`pytest`, no containers needed):
  - preset validation and the catalog
  - naming and IP allocation
  - the latency matrix and netem command generation
  - HAProxy config rendering
  - metric rates and histogram p99
  - survival-budget rules
  - trigger resolution
  - snapshot assembly from fixtures
- **Integration tests** (`pytest -m integration`, real containers, `small` preset; run on rootless Podman and Docker Desktop):
  - provision and adopt after a control-plane restart
  - crash and start a node
  - partition and heal (same IP, netem re-applied)
  - brownout measured end to end
  - isolate a region
  - add and decommission a node
  - add and remove a region
  - the workload invariant holding through a region outage
  - a raw psycopg client through HAProxy surviving a region outage (Banko stand-in)
- **UI checks:** headless-Chrome screenshots of the stage and presenter console in both themes at 1920×1080 and on a phone viewport (the harness used for the explainers), plus a manual big-screen checklist.
- **Explainers:** frame snapshots at chapter boundaries in both themes, and a 5-second export smoke test.

## 14. Docs

- **README rewrite:** quick start, presets and custom regions, stage vs presenter, booth mode, scenarios, Banko integration, Podman notes, resource sizing, troubleshooting, explainers and export.
- **`docs/hardware-triggers.md`:** see §6.6.
- **`docs/presenting.md`:** 5-, 15- and 30-minute run-of-show using the scenarios and explainers, including cloud-illusion tips.
- **User files:** the untracked `DEMO_SUMMARY.txt` and `TALKING_POINTS.md` belong to the user and are left untouched. Updating the talking points is a separate request.

## 15. Repository layout and cleanup

```
controlplane/            FastAPI app (package), Dockerfile (CLI-only Docker install, iproute2)
  app.py  snapshot.py  docker_ops.py  provisioner.py  bootstrap.py  netem.py  lb.py
  chaos.py  elastic.py  collector.py  metrics.py  workload.py  booth.py  events.py
  scenarios/  topology.py  settings.py
web/                     stage/, presenter/, explainers/, shared/ (tokens, fonts, landmask, logo)
presets/  catalog/  tools/export-explainer.mjs  tests/{unit,integration,ui}  docs/
docker-compose.yml  run.sh  .env.example
```

- **Removed:**
  - `toxiproxy/`, `init.sql`, `haproxy.cfg` (now generated)
  - `backend/` (replaced by `controlplane/` and `web/`)
  - `demo_*.py` and `test_demo_scripts.sh` (after their scenarios exist)
  - `privileged: true`, and the full Docker Engine install
- **`.gitignore`:** adds `.superpowers/` and exported videos.

## 16. Delivery milestones

Each milestone ends runnable and is committed on `feature/control-plane`.

1. **Control plane foundation:**
   - compose, network, provisioner and adoption
   - bootstrap with the v26 fix
   - presets, catalog and the HAProxy generator
   - snapshot, SSE and a minimal stage
   - `run.sh` with preflight
2. **Chaos and elasticity:**
   - every §6.1 action, plus netem latency, brownout and isolate
   - add/decommission node, add/remove region
   - survival budget guard, booth mode, triggers and keyboard
3. **Stage and presenter:**
   - layout B, the presenter console and themes
   - the cloud-illusion pass
   - explainers integrated and refactored, plus the exporter
4. **Traffic:** cluster-side metrics, the workload, the invariant and recovery time.
5. **Scenarios:** Tier 1, then Tier 2.
6. **Docs, cleanup and verification:** the full integration suite on rootless Podman and Docker Desktop, then the user's local test. Push only on the user's approval.

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| netem unavailable on some kernels or runtimes | Verified on rootless Podman (Fedora VM, kernel 6.19). Docker Desktop is tested in M2. If unavailable: latency, brownout and isolate are disabled with a clear presenter warning, and everything else works |
| CockroachDB license throttling on long-lived clusters (24.3+ licensing model) | Verify the behavior on v26.3 in M1. Support `CRDB_LICENSE`/`CRDB_ORG` and show license status. A rebuild resets the cluster age |
| Laptop resources | Per-node memory flags, a preset size validator, the `small` preset and a preflight memory check |
| Docker vs Podman CLI differences | The static-IP connect/disconnect flow is verified on Podman. The integration suite runs on both |
| `crdb_internal` drift across versions | Tolerant queries, tested on v25.4 and v26.3 |
| `time_until_store_dead` minimum differs from 75s | Read the validation error and clamp. Verified in M1 |
| HAProxy reloads with long-lived connections | `hard-stop-after 10m` |
| Explainer claim accuracy | Public sources only, Preview labels, marketing review |
| Region names with unusual characters | Validator (§5.1) |
| Booth API exposure on shared Wi-Fi | Optional `CONTROL_TOKEN` and `TRIGGER_TOKEN`, plus a warning when unset |

## 18. Out of scope

- Building the physical trigger hardware (only the hooks are in scope).
- Multi-presenter accounts or RBAC.
- Simulating client location (per-client latency).
- Kubernetes.
- Tier 3 scenarios.
- Pushing to GitHub before the user approves.

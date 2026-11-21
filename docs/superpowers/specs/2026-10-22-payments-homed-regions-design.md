# Payments homed in regions: design

- **Date:** 2026-10-22
- **Status:** Approved in outline by Virag ("let's do the fuller story"); the details below are the builder's calls
- **Branch:** `feature/control-plane` (local only; nothing is pushed until tested locally and approved)
- **Builds on:** `2026-10-09-chaos-control-plane-design.md` §7.2 (the Payments workload)

## 1. Why

With realistic latency on, the Payments workload shows a p50 near 300 ms, and the number reads as a blemish rather
than a lesson. Measured on the running aws preset (us-east-1, us-west-2, eu-west-1; region survival):

- The demo tables are `REGIONAL BY TABLE IN PRIMARY REGION`, so every leaseholder is in us-east-1.
- Workers connect through HAProxy, which is `balance roundrobin`, so two thirds of them land on a gateway in another
  region. A point read of one account costs 1.2 ms from us-east-1, 61 ms from us-west-2 and 89 ms from eu-west-1.
- A transfer is three statements and a commit. From a remote gateway each statement pays a round trip to the
  leaseholder before the commit pays the cross-region quorum: about 70 ms from us-east-1, 240 ms from us-west-2,
  320 ms from eu-west-1, and the mix lands near 300 ms.

Real applications do not do this. They run in every region, serve their own region's customers from a nearby gateway,
and keep those customers' rows close. That is the story to tell: every statement local, only the commit crosses a
region, and the occasional cross-region payment visibly costs more. On this cluster a single write from the home
gateway on a `REGIONAL BY ROW` table measured 63 ms in us-east-1, 63 ms in us-west-2 and 97 ms in eu-west-1 (the
nearest other voter region sets the floor: 58 ms between the US regions, 87 ms from Ireland to Virginia). Double
digits, honestly, with no change to the survival goal.

## 2. What changes

On a multi-region cluster the Payments workload becomes region-aware.

### 2.1 Data

- `accounts` becomes `REGIONAL BY ROW` with `PRIMARY KEY (crdb_region, id)`. The 10,000 accounts are spread round
  robin across the regions of the `chaos` database at seeding time, so the $10,000,000 total is unchanged and every
  region holds a third (or a half, or all) of the money.
- `transfers` becomes `REGIONAL BY ROW` with `PRIMARY KEY (crdb_region, id)`, still `ttl_expire_after = '1 hour'`.
  Every transfer is written with the worker's home region set explicitly, so a retry after an ambiguous commit still
  finds its own row even when it runs through a failover gateway.
- Putting `crdb_region` in the primary key is what keeps writes local: CockroachDB adds no cross-region uniqueness
  check (measured: the INSERT plans as "insert fast path"), and `WHERE crdb_region = $1 AND id = $2` is one
  partition's point lookup.
- Tables that exist in the old layout (plain, or with the demo's hand-set zone config) are dropped and recreated at
  the next workload start, the way integer-keyed tables already are. Nothing in them is worth keeping: `transfers`
  expires hourly and `accounts` is reseeded.
- After creating or converting the tables, each partition's lease is moved to its home region at once
  (`SHOW RANGE ... FOR ROW` then `ALTER RANGE ... RELOCATE LEASE`), as the REGIONAL BY ROW scenario already does;
  the allocator takes minutes to get there on its own under load.
- On a cluster whose `chaos` database is not multi-region (the `small` preset: survival goal `none`) nothing changes:
  plain tables, HAProxy, today's behaviour.

### 2.2 Workers

- Workers are split evenly across the database's regions (worker i serves region i mod n).
- Each worker connects straight to a live node in its home region, chosen from the collector's latest snapshot
  (`liveness == "live"`, running, not partitioned, not isolated), never through HAProxy. HAProxy stays for Banko,
  psql, the invariant check and setup.
- A worker's source account is always one of its home accounts. Its destination is a home account too, except for a
  configurable share of cross-region transfers (default 10%) whose destination is homed elsewhere. Both UPDATEs name
  the account's region and id.
- Failover: when the home region has no usable node, the worker connects to the nearest live region (by the regions'
  catalog coordinates; the primary region when a location is unknown) and keeps writing to its home rows, which the
  cluster is serving from surviving regions by then. Connections live at most 30 s as today, and every reconnect
  tries home first, so traffic returns on its own when the region heals. A connection attempt that fails moves on
  to the next candidate; a worker never stalls on one dead node for more than the 3 s connect timeout.

### 2.3 Regions added or removed live

- A region added live joins the database but gets no accounts and no workers until the workload is restarted
  (Stop/Start or Apply). The next start notices the region set changed and reseeds the accounts across it.
- CockroachDB refuses `DROP REGION` while any REGIONAL BY ROW row is homed there (verified: "could not remove enum
  value ... being used by accounts"). Before Elastic drops a region from the databases it asks the workload to
  evacuate it: rows homed there move to the primary region, and a running workload restarts so its workers and
  account lists match the new region set.

### 2.4 What people see

- Stage: under the traffic chart, one line of chips, "Payments by home region: us-east-1 64 ms · us-west-2 66 ms ·
  eu-west-1 95 ms · cross-region 150 ms", each region in its chart colour, p50 over the last 10 s. A region served
  through failover says so ("via us-west-2"). The chips appear only when Payments is running and homed.
- Presenter, Workload tab: the hint says what the workload now does; a "Cross-region share" input (0 to 50%) next to
  workers and rate; the stats grid gains one row per home region (p50, p95, transfers/s) and a cross-region row; a
  "Homing" item says on, off (single region) or which regions are on failover.
- Glossary tips for the new numbers.
- README and docs/presenting.md describe the behaviour and the numbers to expect.

## 3. Interfaces

- `Workload(connect, *, host, events=None, clock=time.time, sleep=time.sleep, cluster=None)`: `cluster` is a callable
  returning the collector's latest snapshot (`{"nodes": [...], "regions": [...]}`), set from `__main__` once the
  collector exists. Without it the workload runs in plain mode.
- `Workload.configure(*, workers, rate, cross_region_pct=None)`; `PUT /api/workload` accepts `cross_region_pct`
  (0 to 50, default 10).
- `Workload.evacuate(region)`; `Elastic.before_region_removal: list[Callable[[str], None]]`, called inside the
  remove-region operation before the `DROP REGION` statements, with its own operation step.
- `Workload.status()` adds `homing` (`{"on", "regions", "cross_region_pct", "workers": {region: n},
  "failover": {region: gateway_region | null}}`), `by_home` (`{region: {"p50_ms", "p95_ms", "tps"}}`) and
  `cross_region` (`{"p50_ms", "p95_ms", "tps"}`). `history[].regions` keeps today's meaning (commits by gateway
  region).

## 4. Non-goals

- No change to HAProxy, to Banko, to survival goals or to the scenarios (the REGIONAL BY ROW scenario keeps its own
  tables).
- No attempt to reach single-digit milliseconds: with region survival a commit needs one cross-region round trip.
- No rebalancing of accounts while the workload runs; a region change takes effect at the next start.

## 5. Risks and how the build handles them

- A worker's snapshot of live nodes is up to 1.5 s stale: a connection attempt to a node that just died costs one
  connect timeout, then the worker moves on. Acceptable; the recovery-time number stays honest.
- Evacuating a region moves up to a third of the accounts in one statement; 10,000 rows take seconds. The operation
  step names it so the presenter sees why removal pauses.
- The existing stage chart draws cluster metrics by gateway region and is unchanged; the new chips come from the
  workload's own numbers, so they describe Payments only.

## 6. Verification

- Unit tests with the existing fakes (no cluster): table layout and seeding per region, worker homing and failover
  choice, the transfer SQL, cross-region share, stats per home region, evacuation order in Elastic, API validation.
- Live: restart the control plane on the running aws preset (it adopts the cluster), start Payments, read the chips
  and the presenter numbers, take a region down and watch that region's workers fail over and come back; remove and
  re-add a region. Done with Virag's go-ahead, since it interrupts the running demo for a minute.

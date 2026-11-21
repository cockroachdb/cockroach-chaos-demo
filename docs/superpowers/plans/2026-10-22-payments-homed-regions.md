# Payments Homed in Regions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On a multi-region cluster, the Payments workload homes every account and every worker in a region, connects each worker to a gateway in its own region, writes transfers whose statements are all local (only the commit crosses a region, plus a configurable share of cross-region transfers), fails over when a region is down, and shows latency per home region on the stage and in the presenter.

**Architecture:** The pure choices (which region a worker serves, the order in which regions are tried, which live nodes qualify as gateways) live in a new `homing.py` with no database. `workload.py` keeps the SQL: on a multi-region `chaos` database it drops the old tables and creates `REGIONAL BY ROW` tables keyed `(crdb_region, id)`, seeds accounts round robin across the regions, pins partition leases, and runs workers that connect straight to a node in their region. `Stats` learns per-home latency. Elastic gets two hook lists so the workload can evacuate a region's rows before `DROP REGION` and resume after. The stage and presenter read three new keys from `workload.status()`.

**Tech Stack:** Python 3.12+ (control plane, psycopg 3, FastAPI), pytest with the fakes in `tests/unit`, vanilla ES-module JavaScript for the stage and presenter.

**Spec:** `docs/superpowers/specs/2026-10-22-payments-homed-regions-design.md`

## Global Constraints

- `accounts` and `transfers` on a multi-region database: `LOCALITY REGIONAL BY ROW`, `PRIMARY KEY (crdb_region, id)`; `transfers` keeps `WITH (ttl_expire_after = '1 hour')`.
- 10,000 accounts, $1,000 each, total $10,000,000 (`ACCOUNTS`, `START_BALANCE`, `TOTAL` unchanged); accounts spread round robin over the database's regions, the first `ACCOUNTS % n` regions getting one more.
- Cross-region share: `cross_region_pct`, default 10, allowed 0 to 50.
- Homed workers connect to node IPs from the collector snapshot, never to HAProxy. Setup, the invariant check and `evacuate` keep using HAProxy (`self._host`).
- A usable gateway node: `liveness == "live"`, `runtime == "running"`, `partitioned` false, `isolated` false.
- Connections still live at most `CONN_MAX_AGE` (30 s); the connect timeout stays the partial's 3 s.
- Plain mode (database not multi-region, or no cluster snapshot): behaviour identical to today, including the integer-key table replacement.
- Stage copy never mentions docker, container, podman or localhost (`tests/unit/test_stage_copy.py`).
- Prose in docs and UI copy: plain sentences, no em dashes.
- Run unit tests with `.venv/bin/pytest tests/unit -q -p no:cacheprovider` from the repo root. Never run the integration suite (it rebuilds the live cluster).
- Commits: one per task, no bot trailers.

## Review Focus

- A snapshot without `nodes` or `regions` (the collector before its first refresh): a homed worker must fail its connection attempt and retry, never raise out of its thread. Pinned in Task 4 (`test_a_homed_worker_with_no_snapshot_counts_a_connection_error`).
- A region whose nodes are all dead when Payments starts: its workers fail over at once and `status()["homing"]["failover"]` says to where. Pinned in Task 4 (`test_a_worker_fails_over_to_the_nearest_live_region_and_comes_home`).
- 10,000 accounts over 3 regions must still total 10,000. Pinned in Task 3 (`test_accounts_are_seeded_round_robin_with_an_exact_total`).
- `evacuate` on a plain-mode workload, or for a region it does not home, must run no SQL and not restart anything. Pinned in Task 5 (`test_evacuate_does_nothing_in_plain_mode`).
- Restarting the workload before `DROP REGION` has run would reseed rows into the region being removed. Pinned in Task 5 (`test_remove_region_evacuates_before_dropping_and_resumes_after`): the resume hook runs only after the region is gone.

---

### Task 1: Latency per home region in Stats

**Files:**
- Modify: `controlplane/controlplane/workload.py:45-108` (class `Stats`)
- Test: `tests/unit/test_workload.py`

**Interfaces:**
- Produces: `Stats.commit(ms: float, region: str | None, now: float | None = None, *, home: str | None = None, cross: bool = False) -> None`; `Stats.window(now=None)` returns, in addition to today's keys, `"by_home": {home: {"p50_ms": float | None, "p95_ms": float | None, "tps": int}}` and `"cross_region": {"p50_ms", "p95_ms", "tps"}`. Percentiles cover the last 10 s; `tps` is the commit count of the last complete second (`history[-1]`), 0 when none.

- [ ] **Step 1: Write the failing test**

```python
def test_stats_report_latency_per_home_region_and_for_cross_region_transfers():
    s = Stats(clock=lambda: 10.4)
    for ms in range(60, 70):
        s.commit(ms, "us-east-1", now=10.4, home="us-east-1")
    for ms in range(90, 100):
        s.commit(ms, "eu-west-1", now=10.4, home="eu-west-1")
    s.commit(150, "us-east-1", now=10.4, home="us-east-1", cross=True)
    snap = s.window(now=11.5)
    assert snap["by_home"]["us-east-1"]["p50_ms"] == pytest.approx(65, abs=1)
    assert snap["by_home"]["eu-west-1"]["p50_ms"] == pytest.approx(95, abs=1)
    assert snap["by_home"]["us-east-1"]["tps"] == 11 and snap["by_home"]["eu-west-1"]["tps"] == 10
    assert snap["cross_region"] == {"p50_ms": 150, "p95_ms": 150, "tps": 1}
    assert Stats().window(now=5)["by_home"] == {} and Stats().window(now=5)["cross_region"]["tps"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_workload.py::test_stats_report_latency_per_home_region_and_for_cross_region_transfers -q -p no:cacheprovider`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'home'`

- [ ] **Step 3: Implement it in `Stats`**

Add `home` and `cross` to `commit`; keep per-second bucket counts under `b["homes"][home]` and `b["cross"]`; keep `self._latencies` entries as `(now, ms, home, cross)`. In `window`, compute percentiles per home over the last 10 s with the existing `pct` logic and `tps` from the last bucket. Keep `history[].regions` and the existing keys exactly as they are. Existing tests must still pass.

- [ ] **Step 4: Run the workload tests**

Run: `.venv/bin/pytest tests/unit/test_workload.py -q -p no:cacheprovider`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add controlplane/controlplane/workload.py tests/unit/test_workload.py
git commit -m "Payments stats: latency per home region and for cross-region transfers"
```

---

### Task 2: The pure homing choices

**Files:**
- Create: `controlplane/controlplane/homing.py`
- Test: `tests/unit/test_homing.py`

**Interfaces:**
- Produces:
  - `home_of(index: int, regions: Sequence[str]) -> str` returns `regions[index % len(regions)]`.
  - `workers_by_region(workers: int, regions: Sequence[str]) -> dict[str, int]`.
  - `region_order(home: str, regions: Sequence[dict]) -> list[str]`: `regions` are snapshot region dicts (`name`, `location` as `[lat, lon]` or `None`, `primary`); result starts with `home`, then the others by great-circle distance from home (reuse the haversine in `scenarios/library.py:_distance` by moving it here and importing it there), regions with no location after those, the primary first among them, then snapshot order.
  - `gateway_candidates(home: str, snapshot: dict, rng: random.Random) -> list[tuple[str, str]]`: `(ip, region)` of usable nodes (Global Constraints) in `region_order`, shuffled within each region; `[]` when the snapshot has no usable node (including a snapshot without `nodes`/`regions`).

- [ ] **Step 1: Write the failing tests**

```python
import random
from controlplane.homing import gateway_candidates, home_of, region_order, workers_by_region

REGIONS = [{"name": "us-east-1", "location": [38.9, -77.4], "primary": True},
           {"name": "us-west-2", "location": [45.8, -119.7], "primary": False},
           {"name": "eu-west-1", "location": [53.3, -6.3], "primary": False}]

def node(name, region, ip, **over):
    return {"name": name, "region": region, "ip": ip, "liveness": "live", "runtime": "running",
            "partitioned": False, "isolated": False, **over}

NODES = [node("us-east-1a-n1", "us-east-1", "10.77.1.11"), node("us-east-1b-n2", "us-east-1", "10.77.1.12"),
         node("us-west-2a-n1", "us-west-2", "10.77.2.11"), node("eu-west-1a-n1", "eu-west-1", "10.77.3.11")]

def test_workers_and_homes_split_round_robin():
    regions = [r["name"] for r in REGIONS]
    assert [home_of(i, regions) for i in range(5)] == ["us-east-1", "us-west-2", "eu-west-1", "us-east-1", "us-west-2"]
    assert workers_by_region(48, regions) == {"us-east-1": 16, "us-west-2": 16, "eu-west-1": 16}
    assert workers_by_region(4, regions) == {"us-east-1": 2, "us-west-2": 1, "eu-west-1": 1}

def test_regions_are_tried_home_first_then_by_distance():
    assert region_order("us-east-1", REGIONS) == ["us-east-1", "us-west-2", "eu-west-1"]
    assert region_order("eu-west-1", REGIONS) == ["eu-west-1", "us-east-1", "us-west-2"]
    blind = [dict(r, location=None) for r in REGIONS]
    assert region_order("us-west-2", blind) == ["us-west-2", "us-east-1", "eu-west-1"]

def test_gateway_candidates_skip_unusable_nodes_and_fall_back_by_distance():
    snap = {"regions": REGIONS, "nodes": NODES}
    cands = gateway_candidates("us-east-1", snap, random.Random(1))
    assert [c[1] for c in cands] == ["us-east-1", "us-east-1", "us-west-2", "eu-west-1"]
    assert {c[0] for c in cands[:2]} == {"10.77.1.11", "10.77.1.12"}
    down = [dict(n, liveness="dead") if n["region"] == "us-east-1" else n for n in NODES]
    assert gateway_candidates("us-east-1", {"regions": REGIONS, "nodes": down}, random.Random(1))[0] == ("10.77.2.11", "us-west-2")
    flagged = [dict(NODES[0], isolated=True), dict(NODES[1], partitioned=True), dict(NODES[2], runtime="stopped")]
    assert gateway_candidates("us-east-1", {"regions": REGIONS, "nodes": flagged}, random.Random(1)) == []
    assert gateway_candidates("us-east-1", {}, random.Random(1)) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_homing.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'controlplane.homing'`

- [ ] **Step 3: Implement `controlplane/controlplane/homing.py`**

Module docstring: where a Payments worker lives and where it connects. Move `_distance` from `scenarios/library.py:22-28` here as `distance_km(a, b)` and import it back in `library.py` (its one caller `_far`).

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/unit/test_homing.py tests/unit/test_scenarios.py -q -p no:cacheprovider`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add controlplane/controlplane/homing.py controlplane/controlplane/scenarios/library.py tests/unit/test_homing.py
git commit -m "Payments: pure choices for a worker's home region and gateway"
```

---

### Task 3: The homed layout at setup

**Files:**
- Modify: `controlplane/controlplane/workload.py` (`Workload.__init__`, `_setup_once`, new `_pin_leases`)
- Test: `tests/unit/test_workload.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Workload(..., cluster: Callable[[], dict] | None = None)`; attributes `homed: bool`, `regions: list[str]`, `primary: str | None`, `_homes: dict[str, list[str]]` (account ids by home region; `_ids` stays the flat list); `_setup_once` SQL per the steps below.

Setup, in order, on each start:
1. `SHOW REGIONS FROM DATABASE chaos` → rows `(database, region, primary, secondary, zones)`; `regions` in row order, `primary` the row with `primary` true. Empty rows, or `cluster is None`, means plain mode: run today's code path unchanged and set `homed = False`.
2. `SELECT table_name, locality FROM [SHOW TABLES] WHERE table_name IN ('accounts', 'transfers')`; for each table present, `SHOW CREATE TABLE <name>` (second column). The table is homed when `locality == 'REGIONAL BY ROW'` and the create statement contains `PRIMARY KEY (crdb_region`. Any table present and not homed: `DROP TABLE <name>`.
3. Create (IF NOT EXISTS):
   - `CREATE TABLE IF NOT EXISTS accounts (crdb_region crdb_internal_region NOT NULL, id UUID NOT NULL DEFAULT gen_random_uuid(), balance INT NOT NULL, PRIMARY KEY (crdb_region, id)) LOCALITY REGIONAL BY ROW`
   - `CREATE TABLE IF NOT EXISTS transfers (crdb_region crdb_internal_region NOT NULL, id UUID NOT NULL, src UUID NOT NULL, dst UUID NOT NULL, amount INT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (crdb_region, id)) WITH (ttl_expire_after = '1 hour') LOCALITY REGIONAL BY ROW`
4. `SELECT crdb_region, count(*) FROM accounts GROUP BY 1`; expected count for region i is `ACCOUNTS // n + (1 if i < ACCOUNTS % n else 0)`. On any difference: `TRUNCATE accounts`, then per region `INSERT INTO accounts (crdb_region, balance) SELECT %s, %s FROM generate_series(1, %s)` with `(region, START_BALANCE, expected)`.
5. `SELECT crdb_region, id FROM accounts` → `_homes` and `_ids`.
6. `_pin_leases(cur)`: for each region, for `accounts` with the region's first id and for `transfers` with `'00000000-0000-0000-0000-000000000000'`: `SELECT range_id, lease_holder, replicas, replica_localities FROM [SHOW RANGE FROM TABLE <t> FOR ROW ('<region>', '<id>')]`; when the holder's store is not among the replicas whose locality contains `region=<region>,`, run `ALTER RANGE <range_id> RELOCATE LEASE TO <first home store>`; every error here is swallowed (best effort, as `scenarios/library.py:_pin_leases`). Region names match `^[a-z0-9][a-z0-9-]{0,39}$` and ids are UUIDs, so literal interpolation is safe.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_workload.py` a fake for the multi-region answers:

```python
class HomedDB(FakeDB):
    """A multi-region chaos database. layout: {table: (locality, create_stmt)} for tables that exist."""
    def __init__(self, regions=("us-east-1", "us-west-2", "eu-west-1"), layout=None, counts=None, holder_home=True):
        super().__init__(count=ACCOUNTS)
        self.regions, self.layout, self.counts, self.holder_home = list(regions), layout or {}, counts, holder_home

    def cursor(self):  # used through FakeDB.connect: override FakeCursor answers below
        return HomedCursor(self)

class HomedCursor(FakeCursor):
    def fetchall(self):
        last = self.last
        if "SHOW REGIONS FROM DATABASE" in last:
            return [("chaos", r, i == 0, False, []) for i, r in enumerate(self.db.regions)]
        if "SHOW TABLES" in last:
            return [(t, loc) for t, (loc, _) in self.db.layout.items()]
        if "crdb_region, count(*)" in last:
            return list((self.db.counts or {}).items())
        if "SELECT crdb_region, id FROM accounts" in last:
            return [(r, f"00000000-0000-0000-000{i}-{k:012d}") for i, r in enumerate(self.db.regions) for k in range(1, 4)]
        return super().fetchall()

    def fetchone(self):
        last = self.last
        if "SHOW CREATE TABLE" in last:
            name = last.split()[-1]
            return (name, self.db.layout[name][1])
        if "SHOW RANGE FROM TABLE" in last:
            region = last.split("FOR ROW ('")[1].split("'")[0]
            return (42, 7 if self.db.holder_home else 9, [7, 8, 9], [f"region={region},zone=a", f"region={region},zone=b", "region=elsewhere,zone=a"])
        return super().fetchone()
```

(Make `FakeDB.connect` build its cursor through a `cursor()` method on the db so the subclass can swap it in.)

```python
HOMED = ("REGIONAL BY ROW", "CREATE TABLE accounts (... CONSTRAINT accounts_pkey PRIMARY KEY (crdb_region ASC, id ASC) ...)")
PLAIN = ("REGIONAL BY TABLE IN PRIMARY REGION", "CREATE TABLE accounts (... PRIMARY KEY (id ASC) ...)")

def test_a_multi_region_database_gets_tables_keyed_by_region():
    db = HomedDB(layout={"accounts": PLAIN, "transfers": PLAIN})
    w = Workload(db.connect, host="haproxy", cluster=dict)
    w.setup()
    joined = "\n".join(db.sql)
    assert joined.index("DROP TABLE accounts") < joined.index("CREATE TABLE IF NOT EXISTS accounts")
    assert "DROP TABLE transfers" in joined
    assert "PRIMARY KEY (crdb_region, id)) LOCALITY REGIONAL BY ROW" in joined
    assert "WITH (ttl_expire_after = '1 hour') LOCALITY REGIONAL BY ROW" in joined
    assert w.homed and w.regions == ["us-east-1", "us-west-2", "eu-west-1"] and w.primary == "us-east-1"
    assert sorted(w._homes) == ["eu-west-1", "us-east-1", "us-west-2"] and len(w._ids) == 9

def test_homed_tables_are_kept():
    db = HomedDB(layout={"accounts": HOMED, "transfers": HOMED}, counts={"us-east-1": 3334, "us-west-2": 3333, "eu-west-1": 3333})
    Workload(db.connect, host="haproxy", cluster=dict).setup()
    assert not any(s.startswith("DROP TABLE") or s.startswith("TRUNCATE") for s in db.sql)

def test_accounts_are_seeded_round_robin_with_an_exact_total():
    db = HomedDB(layout={"accounts": HOMED, "transfers": HOMED}, counts={"us-east-1": 10000})
    Workload(db.connect, host="haproxy", cluster=dict).setup()
    seeds = [p for s, p in db.calls if s.startswith("INSERT INTO accounts (crdb_region, balance)")]
    assert seeds == [("us-east-1", 1000, 3334), ("us-west-2", 1000, 3333), ("eu-west-1", 1000, 3333)]
    assert "TRUNCATE accounts" in db.sql

def test_partition_leases_are_moved_home_when_they_are_elsewhere():
    db = HomedDB(layout={"accounts": HOMED, "transfers": HOMED}, counts={"us-east-1": 3334, "us-west-2": 3333, "eu-west-1": 3333}, holder_home=False)
    Workload(db.connect, host="haproxy", cluster=dict).setup()
    assert db.sql.count("ALTER RANGE 42 RELOCATE LEASE TO 7") == 6  # two tables, three regions
    db2 = HomedDB(layout={"accounts": HOMED, "transfers": HOMED}, counts={"us-east-1": 3334, "us-west-2": 3333, "eu-west-1": 3333})
    Workload(db2.connect, host="haproxy", cluster=dict).setup()
    assert not any("RELOCATE LEASE" in s for s in db2.sql)

def test_a_single_region_database_keeps_the_plain_layout():
    db = HomedDB(regions=())
    w = Workload(db.connect, host="haproxy", cluster=dict)
    w.setup()
    assert any("id UUID PRIMARY KEY DEFAULT gen_random_uuid()" in s for s in db.sql) and not w.homed
```

`db.calls` is a new list of `(sql, params)` recorded by `FakeCursor.execute` beside `db.sql`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_workload.py -q -p no:cacheprovider -k "multi_region or homed_tables or round_robin or partition_leases or single_region"`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'cluster'`

- [ ] **Step 3: Implement the setup**

Split `_setup_once` into `_setup_plain(cur)` (today's body) and `_setup_homed(cur, regions, primary)`; `_setup_once` runs the regions query first and dispatches. Keep `statement_timeout="120s"`.

- [ ] **Step 4: Run the workload tests**

Run: `.venv/bin/pytest tests/unit/test_workload.py -q -p no:cacheprovider`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add controlplane/controlplane/workload.py tests/unit/test_workload.py
git commit -m "Payments: REGIONAL BY ROW accounts and transfers, homed round robin across the regions"
```

---

### Task 4: Workers homed in a region, with failover

**Files:**
- Modify: `controlplane/controlplane/workload.py` (`configure`, `_open`, `_send`, `_transfer`, `_worker`, `status`)
- Test: `tests/unit/test_workload.py`

**Interfaces:**
- Consumes: Task 2 (`home_of`, `workers_by_region`, `gateway_candidates`), Task 1 (`Stats.commit(..., home=, cross=)`), Task 3 (`homed`, `regions`, `_homes`).
- Produces:
  - `Workload.cross_region_pct: int = 10`; `configure(*, workers: int, rate: int, cross_region_pct: int | None = None)` raises `ValueError("cross-region share must be 0-50%")` outside 0 to 50.
  - `_open(self, state: dict)` returns `(conn, gateway_region)`; homed: tries `gateway_candidates(state["home"], self.cluster(), rng)` in order, raises the last connection error when every candidate fails or the list is empty; sets `state["failover"] = gateway_region != home` and records `self._failover[home] = gateway_region if failover else None`. Plain: today's behaviour.
  - `_transfer(self, conn, tid, a, b, amount, *, home=None, a_home=None, b_home=None)`: homed SQL `INSERT INTO transfers (crdb_region, id, src, dst, amount) VALUES (%s, %s, %s, %s, %s)` with `home`, then `UPDATE accounts SET balance = balance - %s WHERE crdb_region = %s AND id = %s` with `(amount, a_home, a)` and the `+` twin with `(amount, b_home, b)`; plain SQL unchanged when `home is None`.
  - `_send(self, state, a, b, amount, rng, *, a_home=None, b_home=None)`; commits record `home=state.get("home")` and `cross=(a_home != b_home)`.
  - `_worker(index)`: homed: `home = home_of(index, self.regions)`, `a = rng.choice(self._homes[home])`, `b` from `self._homes[home]` unless `rng.random() < self.cross_region_pct / 100` and another region exists, then from `self._homes[rng.choice(others)]`; `a != b`.
  - `status()` adds `"homing": {"on": bool, "regions": list, "cross_region_pct": int, "workers": dict, "failover": dict}`, `"by_home"`, `"cross_region"` (Task 1 keys).

- [ ] **Step 1: Write the failing tests**

```python
LIVE = {"regions": [{"name": "us-east-1", "location": [38.9, -77.4], "primary": True},
                    {"name": "us-west-2", "location": [45.8, -119.7], "primary": False},
                    {"name": "eu-west-1", "location": [53.3, -6.3], "primary": False}],
        "nodes": [{"name": "us-east-1a-n1", "region": "us-east-1", "ip": "10.77.1.11", "liveness": "live", "runtime": "running", "partitioned": False, "isolated": False},
                  {"name": "us-west-2a-n1", "region": "us-west-2", "ip": "10.77.2.11", "liveness": "live", "runtime": "running", "partitioned": False, "isolated": False},
                  {"name": "eu-west-1a-n1", "region": "eu-west-1", "ip": "10.77.3.11", "liveness": "live", "runtime": "running", "partitioned": False, "isolated": False}]}

def homed_workload(snapshot, refuse=()):
    """A workload whose setup already ran on a 3-region database; connects to `refuse` fail."""
    db = HomedDB(layout={"accounts": HOMED, "transfers": HOMED}, counts={"us-east-1": 3334, "us-west-2": 3333, "eu-west-1": 3333})
    db.refuse = set(refuse)  # FakeDB.connect raises psycopg.OperationalError("connection refused") for these hosts and records every host tried in db.hosts
    w = Workload(db.connect, host="haproxy", cluster=lambda: snapshot, sleep=lambda s: None)
    w.setup()
    return w, db

def test_a_homed_worker_connects_to_its_own_region():
    w, db = homed_workload(LIVE)
    state = {"conn": None, "home": "eu-west-1"}
    conn, region = w._open(state)
    assert db.hosts == ["10.77.3.11"] and region == "eu-west-1" and state["failover"] is False
    assert "haproxy" not in db.hosts

def test_a_worker_fails_over_to_the_nearest_live_region_and_comes_home():
    down = dict(LIVE, nodes=[dict(n, liveness="dead") if n["region"] == "eu-west-1" else n for n in LIVE["nodes"]])
    snap = {"now": down}
    w, db = homed_workload(None)
    w.cluster = lambda: snap["now"]
    state = {"conn": None, "home": "eu-west-1"}
    _, region = w._open(state)
    assert region == "us-east-1" and state["failover"] is True  # Ireland's nearest voter region is Virginia
    assert w.status()["homing"]["failover"] == {"eu-west-1": "us-east-1"}
    snap["now"] = LIVE
    _, region = w._open(state)
    assert region == "eu-west-1" and w.status()["homing"]["failover"] == {"eu-west-1": None}

def test_a_refused_node_is_skipped_for_the_next_one_in_the_region():
    two = dict(LIVE, nodes=LIVE["nodes"] + [dict(LIVE["nodes"][0], name="us-east-1b-n2", ip="10.77.1.12")])
    w, db = homed_workload(two, refuse={"10.77.1.11"})
    _, region = w._open({"conn": None, "home": "us-east-1"})
    assert region == "us-east-1" and db.hosts[-1] == "10.77.1.12"

def test_a_homed_worker_with_no_snapshot_counts_a_connection_error():
    w, db = homed_workload({})
    w._stop.wait = lambda t=None: False
    w._transfer = lambda *a, **k: None
    out = w._send({"conn": None, "home": "us-east-1"}, "a", "b", 5, random.Random(1), a_home="us-east-1", b_home="us-east-1")
    assert out == "connection" and w.stats.window(now=1e12)["errors_total"] == {"connection": MAX_RETRIES}

def test_a_homed_transfer_names_every_row_by_region():
    w, db = homed_workload(LIVE)
    w._transfer(db.connect("x"), "t-1", "a", "b", 5, home="us-east-1", a_home="us-east-1", b_home="eu-west-1")
    assert db.calls[-3] == ("INSERT INTO transfers (crdb_region, id, src, dst, amount) VALUES (%s, %s, %s, %s, %s)", ("us-east-1", "t-1", "a", "b", 5))
    assert db.calls[-2] == ("UPDATE accounts SET balance = balance - %s WHERE crdb_region = %s AND id = %s", (5, "us-east-1", "a"))
    assert db.calls[-1] == ("UPDATE accounts SET balance = balance + %s WHERE crdb_region = %s AND id = %s", (5, "eu-west-1", "b"))

def test_about_one_transfer_in_ten_crosses_a_region():
    w, _ = homed_workload(LIVE)
    picks = [w._pick(random.Random(i), "us-west-2") for i in range(1000)]  # (a, a_home, b, b_home)
    assert all(a_home == "us-west-2" and a != b for a, a_home, b, b_home in picks)
    cross = sum(b_home != "us-west-2" for _, _, _, b_home in picks)
    assert 60 <= cross <= 140
    w.configure(workers=3, rate=10, cross_region_pct=0)
    assert all(w._pick(random.Random(i), "us-west-2")[3] == "us-west-2" for i in range(200))
    with pytest.raises(ValueError):
        w.configure(workers=3, rate=10, cross_region_pct=51)

def test_status_describes_the_homing():
    w, _ = homed_workload(LIVE)
    h = w.status()["homing"]
    assert h["on"] and h["regions"] == ["us-east-1", "us-west-2", "eu-west-1"] and h["cross_region_pct"] == 10
    assert h["workers"] == {"us-east-1": 16, "us-west-2": 16, "eu-west-1": 16}
    plain = Workload(FakeDB(count=ACCOUNTS).connect, host="haproxy")
    plain.setup()
    assert plain.status()["homing"] == {"on": False, "regions": [], "cross_region_pct": 10, "workers": {}, "failover": {}}
```

`_pick(self, rng, home) -> tuple[str, str, str, str]` is the account choice factored out of `_worker` so it can be tested. `ScriptedWorkload._open` changes to `def _open(self, state)`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_workload.py -q -p no:cacheprovider`
Expected: the new tests FAIL (`TypeError` on `_open`/`_pick`/`cross_region_pct`); existing tests still pass

- [ ] **Step 3: Implement the worker side**

`_open` loops over candidates with `try/except Exception as e`, keeping the last error; verify `gateway_region()` as today but fall back to the candidate's region when the query fails. `_worker` keeps its pacing loop and calls `_pick` then `_send`.

- [ ] **Step 4: Run the workload tests**

Run: `.venv/bin/pytest tests/unit/test_workload.py tests/unit/test_homing.py -q -p no:cacheprovider`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add controlplane/controlplane/workload.py tests/unit/test_workload.py
git commit -m "Payments workers serve their own region, fail over to the nearest one and come home"
```

---

### Task 5: Evacuating a region before it is removed

**Files:**
- Modify: `controlplane/controlplane/workload.py` (`evacuate`, `resume`)
- Modify: `controlplane/controlplane/elastic.py:111-140` (`remove_region`) and `Elastic.__init__`
- Modify: `controlplane/controlplane/__main__.py:60-62` (wiring)
- Test: `tests/unit/test_workload.py`, `tests/unit/test_elastic.py`

**Interfaces:**
- Produces:
  - `Workload.evacuate(region: str) -> None`: no-op unless `self.homed` and `region in self.regions`; otherwise remembers `self._paused_for_removal = self.running`, calls `stop()` when running, then through `self._host` with `statement_timeout="120s"` runs `UPDATE accounts SET crdb_region = %s WHERE crdb_region = %s` and `UPDATE transfers SET crdb_region = %s WHERE crdb_region = %s` with `(target, region)` where `target` is `self.primary` unless that is the region, else the first other region; removes the region from `self.regions` and `self._homes`.
  - `Workload.resume(region: str) -> None`: `start()` when `_paused_for_removal` was set by `evacuate`, then clears it.
  - `Elastic.before_region_removal: list[Callable[[str], None]]` and `Elastic.after_region_removal: list[Callable[[str], None]]`; `remove_region.work` calls the before hooks (with an operation step `f"Moving Payments accounts out of {name}"`) right before the `DROP REGION` statements and the after hooks after `_finish_change`.
  - `__main__`: `elastic.before_region_removal.append(workload.evacuate)`, `elastic.after_region_removal.append(workload.resume)`, and `workload.cluster = collector.latest` set once the collector exists (keep the `Workload(...)` construction where it is).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_workload.py`:

```python
def test_evacuate_moves_a_regions_rows_to_the_primary_and_pauses_a_running_workload():
    w, db = homed_workload(LIVE)
    w.running = True
    w.evacuate("eu-west-1")
    assert ("UPDATE accounts SET crdb_region = %s WHERE crdb_region = %s", ("us-east-1", "eu-west-1")) in db.calls
    assert ("UPDATE transfers SET crdb_region = %s WHERE crdb_region = %s", ("us-east-1", "eu-west-1")) in db.calls
    assert w.running is False and w.regions == ["us-east-1", "us-west-2"] and "eu-west-1" not in w._homes
    w.resume("eu-west-1")
    assert w.running is True

def test_evacuating_the_primary_moves_rows_to_the_next_region():
    w, db = homed_workload(LIVE)
    w.evacuate("us-east-1")
    assert ("UPDATE accounts SET crdb_region = %s WHERE crdb_region = %s", ("us-west-2", "us-east-1")) in db.calls

def test_evacuate_does_nothing_in_plain_mode():
    db = FakeDB(count=ACCOUNTS)
    w = Workload(db.connect, host="haproxy")
    w.setup()
    before = list(db.sql)
    w.evacuate("us-east-1")
    w.resume("us-east-1")
    assert db.sql == before and w.running is False
```

In `tests/unit/test_elastic.py`:

```python
def test_remove_region_evacuates_before_dropping_and_resumes_after(tmp_path):
    e, docker, db, prov, lb, chaos, ops = make(tmp_path, topo=AWS)
    order = []
    e.before_region_removal.append(lambda name: (order.append(("evacuate", name)), db.sql.append("-- evacuated")))
    e.after_region_removal.append(lambda name: order.append(("resume", name, len(prov.topology.regions))))
    e.remove_region("us-west-2")
    assert ops.snapshot()["status"] == "done"
    joined = "\n".join(db.sql)
    assert joined.index("-- evacuated") < joined.index('DROP REGION IF EXISTS "us-west-2"')
    assert order == [("evacuate", "us-west-2"), ("resume", "us-west-2", 2)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_workload.py tests/unit/test_elastic.py -q -p no:cacheprovider -k "evacuat or resume"`
Expected: FAIL with `AttributeError` (`evacuate`, `before_region_removal`)

- [ ] **Step 3: Implement evacuate, resume, the hooks and the wiring**

The before hooks run after the survival-goal change block and before the `DROP REGION` statements; the after hooks run after `_finish_change(op, new_topo)`.

- [ ] **Step 4: Run the unit suite**

Run: `.venv/bin/pytest tests/unit -q -p no:cacheprovider`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add controlplane/controlplane/workload.py controlplane/controlplane/elastic.py controlplane/controlplane/__main__.py tests/unit/test_workload.py tests/unit/test_elastic.py
git commit -m "Removing a region moves the Payments accounts out first and restarts the workload after"
```

---

### Task 6: What people see: API, presenter, stage, glossary, docs

**Files:**
- Modify: `controlplane/controlplane/app.py:82-84, 349-355` (`WorkloadRequest`, `PUT /api/workload`)
- Modify: `web/presenter/index.html:104-116`, `web/presenter/presenter.js:261-281, 459-460`
- Modify: `web/stage/index.html:40-45`, `web/stage/stage.js` (`renderKpis` or a new `renderHomes` called from the same place), `web/stage/stage.css`
- Modify: `web/shared/glossary.js`
- Modify: `README.md:119-137`, `docs/presenting.md:9-10, 25`
- Test: `tests/unit/test_app_control.py`, `tests/unit/test_stage_copy.py` (existing)

**Interfaces:**
- Consumes: `status()["homing"]`, `["by_home"]`, `["cross_region"]` from Task 4; `configure(..., cross_region_pct=)`.
- Produces: `WorkloadRequest(workers: int, rate: int, cross_region_pct: int = 10)`; `PUT /api/workload` passes `cross_region_pct` through; 400 on `ValueError` as today.

- [ ] **Step 1: Write the failing API test**

Extend `tests/unit/test_app_control.py`: `FakeWorkload.configure(self, *, workers, rate, cross_region_pct=None)` records `("configure", workers, rate, cross_region_pct)` and raises `ValueError` when `cross_region_pct` is not None and not `0 <= cross_region_pct <= 50`. In `test_workload_endpoints`: `c.put("/api/workload", json={"workers": 8, "rate": 100})` records `("configure", 8, 100, 10)`; `json={"workers": 8, "rate": 100, "cross_region_pct": 25}` records `("configure", 8, 100, 25)`; `cross_region_pct: 80` returns 400.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_app_control.py::test_workload_endpoints -q -p no:cacheprovider`
Expected: FAIL (the recorded tuple has three fields)

- [ ] **Step 3: Implement the API change and the UI**

Presenter (`index.html`): hint becomes "Transfers between 10,000 accounts that always total $10,000,000. On a multi-region cluster every account and every worker lives in a region: workers connect to their own region, so each transfer's statements are local and only the commit crosses a region. A share of transfers goes to another region on purpose. Uses the separate <code>chaos</code> database." Add `<label>Cross-region <input id="wl-cross" type="number" min="0" max="50" value="10"> %</label>` before the Apply button. `presenter.js`: `renderWorkload` adds, after the p99 item, one item per `w.homing.regions` named by the region with value `${p50} ms · p95 ${p95} ms · ${tps}/s` (`–` when null), an item `Cross-region` with the same shape, and an item `Homing` reading `off (single region)` when `!w.homing.on`, else `on` or `on · <region> via <gateway>` for each failover entry; `#wl-apply` sends `cross_region_pct: +$('wl-cross').value`; sync `#wl-cross` like the other inputs; tooltips: `G.crossShare` on the input, `G.byHome` on region items, `G.crossRegion` and `G.homing` on theirs.

Stage: in `index.html` add `<div class="homes" id="homes" hidden></div>` after `#legend` inside `#chart-box`. `stage.js`: `renderHomes(s)` called where `renderKpis` is; it hides `#homes` unless `w && w.running && w.homing && w.homing.on`; otherwise renders one `.chip.home` per `s.regions` entry (chart colour swatch via `regionColor(i)`) reading `${name} ${p50} ms` (`–` without data) plus ` via ${gateway}` when `w.homing.failover[name]`, and a final `.chip.home` `cross-region ${p50} ms` when `w.cross_region.p50_ms != null`. `stage.css`: `.homes { position: absolute; left: 22px; bottom: 14px; display: flex; gap: 10px; z-index: 2; } .homes .chip { padding: 6px 12px; } .homes .chip i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 7px; vertical-align: -1px; }`.

Glossary: `homing: 'Payments by home region: every account and every worker belongs to a region. A worker connects to its own region, so each statement of a transfer is local and only the commit crosses a region. Via another region means that region is down and its workers are being served from the nearest one.'`, `byHome: 'p50 latency of transfers made by this region\'s workers over the last 10 seconds, and their rate.'`, `crossRegion: 'Transfers that deliberately pay an account in another region: one more round trip than a local one, as a real cross-border payment would.'`, `crossShare: 'Cross-region share: the percentage of transfers sent to an account in another region.'`.

README Payments section: replace the first paragraph with the behaviour (homed accounts and workers, direct gateway per region, 10% cross-region by default, failover to the nearest region, single-region clusters unchanged) and add the numbers measured on the aws preset with realistic latency: a single write from the home gateway 63 ms in us-east-1 and us-west-2, 97 ms in eu-west-1; the old layout from a remote gateway 180 to 240 ms. `docs/presenting.md` line 25: say that when a region goes down its Payments workers reconnect to the nearest region and its chip reads "via" until it heals.

- [ ] **Step 4: Verify**

Run: `.venv/bin/pytest tests/unit -q -p no:cacheprovider` → all pass. Run: `node --check web/stage/stage.js && node --check web/presenter/presenter.js && node --check web/shared/glossary.js` (ES modules: use `node --input-type=module --check < file` if `--check` rejects them) → no output. Grep the changed prose for em dashes: `grep -n "—" README.md docs/presenting.md web/presenter/index.html web/shared/glossary.js` → only lines that existed before.

- [ ] **Step 5: Commit**

```bash
git add controlplane/controlplane/app.py web/presenter web/stage web/shared/glossary.js README.md docs/presenting.md tests/unit/test_app_control.py
git commit -m "Show Payments latency per home region, with a cross-region share control"
```

---

### Task 7: Whole-change review and the live test plan

- [ ] **Step 1: Run the full unit suite one more time**

Run: `.venv/bin/pytest tests/unit -q -p no:cacheprovider`
Expected: all pass (326 before this work, plus the new tests)

- [ ] **Step 2: Independent review**

Dispatch a fresh reviewer (most capable model) on `git diff <base>..HEAD` with the spec and this plan; fix what it finds; commit the fixes.

- [ ] **Step 3: Hand over**

Report to Virag: what changed, the commits, and the live test to run with his go-ahead, since it interrupts the demo: `./run.sh` (rebuilds and restarts the control plane; it adopts the running cluster), Presenter → Workload → Start, read the chips under the chart and the per-region rows, take eu-west-1 down and watch its chip read "via us-east-1", recover it, then Elastic: remove a region and add it back.

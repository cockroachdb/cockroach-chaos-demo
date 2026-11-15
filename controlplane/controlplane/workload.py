"""Built-in "Payments" workload: transfers between accounts, an invariant that money is never lost, recovery time."""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from collections import deque
from typing import Callable

from .homing import deal, gateway_candidates, rehoming, shares, workers_by_region
from .retry import RETRYABLE, abandon_after, backoff_delay, classify, may_have_committed, retry_with_backoff

log = logging.getLogger(__name__)

ACCOUNTS = 10_000
START_BALANCE = 1_000
TOTAL = ACCOUNTS * START_BALANCE
HISTORY_SECONDS = 120
RATE_SECONDS = 3  # rates shown on the stage average this many complete seconds: one slow second isn't a collapse
MAX_RETRIES = 12
TRANSFER_TIMEOUT = 10.0  # client-side limit per attempt; COMMIT can hang past statement_timeout during an outage
CONN_MAX_AGE = 30.0  # like a pool's max lifetime: recovered nodes get traffic back within ~30 s
# The homed layout (homing.py): the region is part of the key, so an INSERT with an explicit region needs no
# cross-region uniqueness check and WHERE crdb_region = $1 AND id = $2 is one partition's point lookup.
HOMED_ACCOUNTS_DDL = ("CREATE TABLE IF NOT EXISTS accounts (crdb_region crdb_internal_region NOT NULL, "
                      "id UUID NOT NULL DEFAULT gen_random_uuid(), balance INT NOT NULL, "
                      "PRIMARY KEY (crdb_region, id)) LOCALITY REGIONAL BY ROW")
HOMED_TRANSFERS_DDL = ("CREATE TABLE IF NOT EXISTS transfers (crdb_region crdb_internal_region NOT NULL, id UUID NOT NULL, "
                       "src UUID NOT NULL, dst UUID NOT NULL, amount INT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                       "PRIMARY KEY (crdb_region, id)) WITH (ttl_expire_after = '1 hour') LOCALITY REGIONAL BY ROW")
NO_ROW = "00000000-0000-0000-0000-000000000000"
SETTLE_SECONDS = 20.0  # how long a re-homing waits for the payments already on their way to an account that moves
FORGET_BATCH = 5_000  # transfer rows deleted per statement when a region leaves
# x 5 s: how long Payments keeps trying to bring a new region's leases home. After ADD REGION the allocator has every
# range of the databases to place; the new region's partitions usually get replicas within a minute or two, but once
# waited more than seven.
LEASE_ATTEMPTS = 240


def baseline_before(history: list[dict], failure_at: float) -> float:
    before = [h["commits"] for h in history if failure_at - 10 <= h["ts"] < failure_at]
    return sum(before) / len(before) if before else 0.0


def recovery_time(history: list[dict], failure_at: float, baseline: float | None = None) -> float | None:
    """Seconds from the failure to the first 2 s window back at >=80% of the 10 s pre-failure average.
    0.0 when commits never dipped; None while still recovering or without a baseline."""
    if baseline is None:
        baseline = baseline_before(history, failure_at)
    if baseline <= 0:
        return None
    after = [h for h in history if h["ts"] >= int(failure_at)]
    for i in range(len(after) - 1):
        a, b = after[i], after[i + 1]
        if (a["commits"] + b["commits"]) / 2 >= 0.8 * baseline:
            return 0.0 if i == 0 else float(b["ts"] - failure_at)
    return None


def _pct(sorted_ms: list[float], q: float) -> float | None:
    return round(sorted_ms[min(len(sorted_ms) - 1, int(q * len(sorted_ms)))], 1) if sorted_ms else None


class Stats:
    """Per-second counters for the stage chart, plus a rolling latency sample for percentiles, overall and per
    home region (the region a worker and its accounts live in; see homing.py)."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._seconds: dict[int, dict] = {}
        self._latencies: deque = deque(maxlen=20_000)
        self.errors_total: dict[str, int] = {}  # transient errors seen, by class (most were retried successfully)
        self.failed_total = 0  # transfers that gave up
        self.deduplicated = 0  # retries that found the transfer had already committed

    def _bucket(self, now: float) -> dict:
        sec = int(now)
        b = self._seconds.get(sec)
        if b is None:
            b = self._seconds[sec] = {"ts": sec, "commits": 0, "retries": 0, "errors": 0, "regions": {},
                                      "homes": {}, "cross": 0}
            for old in [s for s in self._seconds if s < sec - HISTORY_SECONDS]:
                del self._seconds[old]
        return b

    def commit(self, ms: float, region: str | None, now: float | None = None, *, home: str | None = None,
               cross: bool = False) -> None:
        """region: the gateway the transfer went through; home: the worker's region, when homed."""
        now = self._clock() if now is None else now
        with self._lock:
            b = self._bucket(now)
            b["commits"] += 1
            if region:
                b["regions"][region] = b["regions"].get(region, 0) + 1
            if home:
                b["homes"][home] = b["homes"].get(home, 0) + 1
            if cross:
                b["cross"] += 1
            self._latencies.append((now, ms, home, cross))

    def retry(self, now: float | None = None) -> None:
        with self._lock:
            self._bucket(self._clock() if now is None else now)["retries"] += 1

    def fail(self) -> None:
        with self._lock:
            self.failed_total += 1

    def dedup(self) -> None:
        with self._lock:
            self.deduplicated += 1

    def error(self, kind: str, now: float | None = None) -> None:
        with self._lock:
            self._bucket(self._clock() if now is None else now)["errors"] += 1
            self.errors_total[kind] = self.errors_total.get(kind, 0) + 1

    def window(self, now: float | None = None) -> dict:
        now = self._clock() if now is None else now
        with self._lock:
            first = min(self._seconds, default=int(now))
            empty = {"commits": 0, "retries": 0, "errors": 0, "regions": {}, "homes": {}, "cross": 0}
            done = []
            for sec in range(max(first, int(now) - HISTORY_SECONDS), int(now)):  # dense: a stall shows as zeros
                b = self._seconds.get(sec, {**empty, "ts": sec})
                done.append(dict(b, regions=dict(b["regions"]), homes=dict(b["homes"])))
            window = [(ms, home, cross) for ts, ms, home, cross in self._latencies if ts >= now - 10]
            errors = dict(self.errors_total)

        recent = sorted(ms for ms, _, _ in window)
        lately = done[-RATE_SECONDS:]  # only seconds since the first commit: no zeros from before the start

        def rate(count: Callable[[dict], int]) -> int:
            return round(sum(count(b) for b in lately) / len(lately)) if lately else 0

        by_home = {}
        for home in sorted({h for _, h, _ in window if h}):
            ms = sorted(m for m, h, _ in window if h == home)
            by_home[home] = {"p50_ms": _pct(ms, 0.5), "p95_ms": _pct(ms, 0.95), "tps": rate(lambda b: b["homes"].get(home, 0))}
        cross_ms = sorted(m for m, _, c in window if c)
        return {"history": [{k: v for k, v in h.items() if k not in ("homes", "cross")} for h in done[-HISTORY_SECONDS:]],
                "tps": rate(lambda b: b["commits"]), "retries_s": rate(lambda b: b["retries"]),
                "errors_s": rate(lambda b: b["errors"]),
                "p50_ms": _pct(recent, 0.5), "p95_ms": _pct(recent, 0.95), "p99_ms": _pct(recent, 0.99),
                "errors_total": errors, "by_home": by_home,
                "cross_region": {"p50_ms": _pct(cross_ms, 0.5), "p95_ms": _pct(cross_ms, 0.95), "tps": rate(lambda b: b["cross"])}}


class Workload:
    def __init__(self, connect: Callable, *, host: str, events=None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, cluster: Callable[[], dict] | None = None):
        self._connect = connect
        self._host = host
        self.cluster = cluster  # the collector's latest snapshot; without it the workload runs in plain mode
        self.homed = False
        self.regions: list[str] = []
        self.primary: str | None = None
        self._homes: dict[str, list[str]] = {}  # account ids by home region
        self.cross_region_pct = 10  # share of transfers that pay an account in another region on purpose
        self._failover: dict[str, str | None] = {}  # home region -> the region serving it instead, or None
        self._leaving: str | None = None  # a region being removed: nothing is homed there any more
        self._routes = threading.Lock()  # a worker's pick and a re-homing never interleave (see _rehome)
        self._background: Callable[[Callable[[], None]], None] = (
            lambda work: threading.Thread(target=work, daemon=True, name="payments-leases").start())
        self._inflight: dict[int, tuple[str, str, str]] = {}  # worker -> (home, source, destination) of its transfer
        self._dealt: list[str] = []  # every worker's home region (homing.deal); redealt when the regions change
        self._events = events
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.workers = 48  # enough that throughput is paced by the rate, not by cross-region latency
        self.rate = 250
        self.stats = Stats(clock)
        self._balance: int | None = None
        self._last_check: float | None = None
        self._failure_at: float | None = None
        self._baseline: float | None = None
        self._recovery: float | None = None
        self._last_error: str | None = None
        self._ids: list[str] = []
        self.running = False

    # -- control --

    def configure(self, *, workers: int, rate: int, cross_region_pct: int | None = None) -> None:
        if not 1 <= workers <= 64 or not 1 <= rate <= 5000:
            raise ValueError("workers must be 1-64 and rate 1-5000 tx/s")
        if cross_region_pct is not None and not 0 <= cross_region_pct <= 50:
            raise ValueError("cross-region share must be 0-50%")
        restart = self.running
        if restart:
            self.stop()
        self.workers, self.rate = workers, rate
        if cross_region_pct is not None:
            self.cross_region_pct = int(cross_region_pct)
        if restart:
            self.start()

    def set_cross_region(self, pct: int) -> None:
        """Change the cross-region share without a restart: every worker reads it on its next pick."""
        if not 0 <= pct <= 50:
            raise ValueError("cross-region share must be 0-50%")
        self.cross_region_pct = int(pct)

    def setup(self) -> None:
        retry_with_backoff(self._setup_once, max_retries=5, initial_backoff=0.5, sleep=self._sleep)

    def _setup_once(self) -> None:
        conn = self._connect(self._host, statement_timeout="120s")  # seeding 10k rows multi-region takes a while
        try:
            with conn.cursor() as cur:
                regions, primary = self._database_regions(cur) if self.cluster is not None else ([], None)
                if regions:
                    self._setup_homed(cur, self._serving(regions) or regions, primary)
                else:
                    self._setup_plain(cur)
        finally:
            conn.close()

    def _tie_breakers(self) -> set[str]:
        """Regions the topology marks as tie-breakers, from the collector's latest snapshot."""
        snap = self.cluster() if self.cluster else None
        return {r["name"] for r in (snap or {}).get("regions", []) if r.get("tie_breaker")}

    def _serving(self, regions: list[str]) -> list[str]:
        """The regions that home accounts and workers: not one that is leaving, and not a tie-breaker, which only
        votes."""
        staying = [r for r in regions if r != self._leaving]
        tie = self._tie_breakers()
        return [r for r in staying if r not in tie] or staying

    @staticmethod
    def _database_regions(cur) -> tuple[list[str], str | None]:
        """The chaos database's regions in order, and its primary. Empty on a cluster without multi-region
        databases (survival goal none), where the plain layout stays."""
        cur.execute("SHOW REGIONS FROM DATABASE chaos")
        rows = cur.fetchall()
        regions = [str(r[1]) for r in rows]
        primary = next((str(r[1]) for r in rows if r[2]), regions[0] if regions else None)
        return regions, primary

    def _setup_plain(self, cur) -> None:
        # Random UUID keys spread writes across ranges; sequential integers would hot-spot one range.
        cur.execute("SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'accounts' AND column_name = 'id'")
        row = cur.fetchone()
        if row and str(row[0]).lower() != "uuid":
            cur.execute("DROP TABLE accounts")  # created by an older version with integer keys
        cur.execute("CREATE TABLE IF NOT EXISTS accounts "
                    "(id UUID PRIMARY KEY DEFAULT gen_random_uuid(), balance INT NOT NULL)")
        # One row per transfer id makes retries after ambiguous commits safe; rows expire after an hour.
        cur.execute("CREATE TABLE IF NOT EXISTS transfers (id UUID PRIMARY KEY, src UUID NOT NULL, dst UUID NOT NULL, "
                    "amount INT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now()) WITH (ttl_expire_after = '1 hour')")
        cur.execute("SELECT count(*) FROM accounts")
        if cur.fetchone()[0] != ACCOUNTS:
            cur.execute("TRUNCATE accounts")
            cur.execute(f"INSERT INTO accounts (balance) SELECT {START_BALANCE} FROM generate_series(1, {ACCOUNTS})")
        cur.execute("SELECT id FROM accounts")
        self._ids = [str(r[0]) for r in cur.fetchall()]
        self.homed, self.regions, self.primary, self._homes = False, [], None, {}

    def _setup_homed(self, cur, regions: list[str], primary: str | None) -> None:
        """Accounts and transfers keyed by region, the accounts spread round robin over the regions."""
        cur.execute("SELECT table_name, locality FROM [SHOW TABLES] WHERE table_name IN ('accounts', 'transfers')")
        for name, locality in cur.fetchall():
            cur.execute(f"SHOW CREATE TABLE {name}")
            create = str(cur.fetchone()[1])
            if locality != "REGIONAL BY ROW" or "PRIMARY KEY (crdb_region" not in create:
                cur.execute(f"DROP TABLE {name}")  # the plain layout: transfers expire hourly and accounts are reseeded
        cur.execute(HOMED_ACCOUNTS_DDL)
        cur.execute(HOMED_TRANSFERS_DDL)
        expected = shares(ACCOUNTS, regions)
        cur.execute("SELECT crdb_region, count(*) FROM accounts GROUP BY 1")
        have = {str(r): int(c) for r, c in cur.fetchall()}
        if have != expected:
            cur.execute("TRUNCATE accounts")
            for region, count in expected.items():
                cur.execute("INSERT INTO accounts (crdb_region, balance) SELECT %s, %s FROM generate_series(1, %s)",
                            (region, START_BALANCE, count))
        cur.execute("SELECT crdb_region, id FROM accounts")
        homes: dict[str, list[str]] = {r: [] for r in regions}
        for region, ident in cur.fetchall():
            homes.setdefault(str(region), []).append(str(ident))
        self._homes = homes
        self._ids = [i for ids in homes.values() for i in ids]
        self.homed, self.regions, self.primary = True, list(regions), primary
        self._dealt = deal([None] * self.workers, self.regions)
        self._pin_leases(cur)

    def _pin_leases(self, cur, regions: list[str] | None = None) -> bool:
        """Move each partition's lease to its home region now instead of waiting minutes for the allocator.
        Best effort: the allocator gets there on its own. Region names are validated by the topology and
        the ids are UUIDs from the database, so they are safe to inline. Returns whether every lease looked at
        was at home already; one that is not may have nowhere to go yet (a new region holds no replica at first)."""
        at_home = True
        for region in self.regions if regions is None else regions:
            first = (self._homes.get(region) or [None])[0]
            for table, ident in (("accounts", first), ("transfers", NO_ROW)):
                if ident is None:
                    continue
                try:
                    cur.execute(f"SELECT range_id, lease_holder, replicas, replica_localities "
                                f"FROM [SHOW RANGE FROM TABLE {table} FOR ROW ('{region}', '{ident}')]")
                    info = cur.fetchone()
                    if not info:
                        continue
                    range_id, holder, replicas, localities = info
                    home = [int(r) for r, loc in zip(replicas, localities) if f"region={region}," in f"{loc},"]
                    if int(holder) in home:
                        continue
                    at_home = False
                    if home:
                        cur.execute(f"ALTER RANGE {int(range_id)} RELOCATE LEASE TO {home[0]}")
                except Exception as e:
                    at_home = False
                    log.debug("lease for %s in %s not pinned: %s", table, region, e)
        return at_home

    def _settle_leases(self, region: str) -> None:
        """In the background after a region joined: right after ADD REGION its partitions hold no replica there, so
        their leases cannot move home yet and every statement of the region's payments goes to another region.
        Look every few seconds and move each lease as soon as the region has a replica to hold it. Ends when the
        leases are home, when Payments stops, when the region leaves again, or after twenty minutes."""
        for _ in range(LEASE_ATTEMPTS):
            if self._stop.wait(5.0) or region not in self.regions:
                return
            try:
                conn = self._connect(self._host, statement_timeout="20s")
                try:
                    with conn.cursor() as cur:
                        if self._pin_leases(cur, [region]):
                            return
                finally:
                    conn.close()
            except Exception as e:
                log.debug("leases of %s not settled yet: %s", region, e)

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self.setup()
            self._failover = {}
            self._stop.clear()
            self.running = True
            self._threads = [threading.Thread(target=self._worker, args=(i,), daemon=True, name=f"payments-{i}")
                             for i in range(self.workers)]
            self._threads.append(threading.Thread(target=self._checker, daemon=True, name="payments-invariant"))
            for t in self._threads:
                t.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            for t in self._threads:
                t.join(timeout=8)
            self._threads = []
            self.running = False
            self._failure_at = self._baseline = None  # a recovery can't be measured without traffic

    # -- regions join and leave while Payments runs --

    def expand(self, region: str) -> str | None:
        """After a region joined the database, with Payments running: the region takes its share of the customers.
        Accounts move there from the other regions and the workers are dealt over all of them again, the way an
        application starts serving a new region. Nothing stops (see _rehome). Without Payments running there is
        nothing to do: the next start seeds every region. Returns a line for the operation log, or None."""
        with self._lock:
            if not (self.running and self.homed) or region in self._tie_breakers():
                return None
            conn = self._connect(self._host, statement_timeout="120s")
            try:
                with conn.cursor() as cur:
                    regions, _ = self._database_regions(cur)
                    if region not in regions:
                        return None
                    moved = self._rehome(conn, cur, self._serving(regions))
                    if moved and not self._pin_leases(cur, [region]):
                        self._background(lambda: self._settle_leases(region))
            finally:
                conn.close()
        return f"Payments now serves {region}: {moved:,} accounts are homed there" if moved else None

    def evacuate(self, region: str) -> str | None:
        """Before a region leaves the database, with Payments still running: its customers get a new home. Their
        accounts are spread over the regions that stay (CockroachDB refuses DROP REGION while a row is homed in
        the region) and the workers that served them connect there instead, the way an application re-points its
        clients before a region is retired. Nothing stops (see _rehome). The region's transfer rows go last: they
        only dedupe the retries of transfers sent from there, and those have all finished by then. The database is
        the authority on the layout and the regions: after a control-plane restart nothing in this process has run
        setup() yet. Until removal_done() nothing is homed in the region again, whoever starts Payments.
        Returns a line for the operation log, or None when the region homed no account."""
        with self._lock:
            conn = self._connect(self._host, statement_timeout="120s")
            try:
                with conn.cursor() as cur:
                    regions, _ = self._database_regions(cur)
                    if region not in regions or not self._homed_in_database(cur):
                        return None
                    self._leaving = region
                    try:
                        staying = self._serving(regions)
                        if not staying:
                            raise RuntimeError(f"{region} is the only region with Payments accounts; nothing can take them")
                        moved = self._rehome(conn, cur, staying, leaving=region)
                    except BaseException:
                        self._leaving = None  # nothing moved: the region homes its accounts as before
                        raise
                    self._forget_transfers(cur, region)
            finally:
                conn.close()
            if self.primary == region:
                self.primary = staying[0]
        if not moved:
            return None
        # Names only the region that leaves: the stage shows a region's nodes as draining when a step names it.
        how = "while Payments kept running" if self.running else "(Payments is stopped)"
        return f"{moved:,} Payments accounts moved out of {region} to the {len(staying)} other region{'' if len(staying) == 1 else 's'} {how}"

    def removal_done(self, region: str) -> None:
        """After the region left, or the removal gave up: if it is still a region, accounts may be homed there again."""
        if self._leaving == region:
            self._leaving = None

    def _rehome(self, conn, cur, regions: list[str], *, leaving: str | None = None) -> int:
        """Spread the accounts over `regions` (homing.rehoming) and return how many moved.

        With Payments running nothing stops. The accounts that are about to move are first taken out of the
        workers' reach, and no worker is homed in a leaving region any more; a pick and this step exclude each
        other (_routes), so every transfer that chose one of them earlier is known to be in flight. Those finish
        (_settle), and only then do the rows change region, in one transaction that no payment can meet. Then the
        accounts are back in reach at their new home and the workers are dealt over the new regions.

        If the transfers do not finish or the rows do not move, the accounts go back in reach where they were and
        the error is raised: nothing has changed."""
        cur.execute("SELECT crdb_region, id FROM accounts")
        have: dict[str, list[str]] = {}
        for region, ident in cur.fetchall():
            have.setdefault(str(region), []).append(str(ident))
        moves = rehoming(have, regions)
        moving = {ident for _, _, ids in moves for ident in ids}
        live = self.running and self.homed
        before = (self._homes, self.regions, self._dealt)
        try:
            if live:
                with self._routes:
                    self._homes = {r: [i for i in ids if i not in moving] for r, ids in self._homes.items() if r != leaving}
                    self.regions = [r for r in self.regions if r != leaving]
                    self._dealt = deal(self._dealt, self.regions)
                self._settle(moving, leaving)
            if moves:
                try:
                    with conn.transaction():
                        for source, target, ids in moves:
                            cur.execute("UPDATE accounts SET crdb_region = %s WHERE crdb_region = %s AND id = ANY(%s::UUID[])",
                                        (target, source, ids))
                            if cur.rowcount != len(ids):
                                raise RuntimeError(f"{len(ids)} accounts were to move from {source} to {target}, but "
                                                   f"{cur.rowcount} did; nothing was moved")
                except Exception as e:
                    # A commit that was never answered may have happened. The accounts must end up in reach where
                    # their rows are, so ask the database: one transaction moved them all, and one account tells.
                    if not (may_have_committed(e) and self._landed(*moves[0])):
                        raise
        except BaseException:
            if live:
                with self._routes:
                    self._homes, self.regions, self._dealt = before
            raise
        if self.homed:
            after = {r: [i for i in have.get(r, ()) if i not in moving] for r in regions}
            for _, target, ids in moves:
                after[target].extend(ids)
            with self._routes:
                self._homes, self.regions = after, list(regions)
                self._dealt = deal(self._dealt, self.regions)
                self._ids = [i for ids in after.values() for i in ids]
                self._failover = {r: via for r, via in self._failover.items() if r in after}
        return len(moving)

    def _landed(self, source: str, target: str, ids: list[str]) -> bool:
        """Whether a move whose commit was not answered took place: is its first account homed in the target now?"""
        def once() -> bool:
            conn = self._connect(self._host, statement_timeout="20s")
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM accounts WHERE crdb_region = %s AND id = %s", (target, ids[0]))
                    return bool(cur.fetchone()[0])
            finally:
                conn.close()
        try:
            return retry_with_backoff(once, max_retries=5, initial_backoff=0.5, sleep=self._sleep)
        except Exception as e:
            log.warning("could not tell whether accounts moved from %s to %s: %s", source, target, e)
            return False

    def _settle(self, moving: set[str], leaving: str | None) -> None:
        """Wait for the transfers already on their way to an account that is about to move, or sent from a region
        that is leaving (its transfer row is homed there). Normally a fraction of a second."""
        busy: list = []
        for _ in range(int(SETTLE_SECONDS / 0.05)):
            busy = [t for t in list(self._inflight.values())
                    if (leaving is not None and t[0] == leaving) or t[1] in moving or t[2] in moving]
            if not busy:
                return
            self._sleep(0.05)
        raise RuntimeError(f"{len(busy)} payments still in flight after {SETTLE_SECONDS:.0f} s; no account was moved")

    @staticmethod
    def _forget_transfers(cur, region: str) -> None:
        """Delete the transfer rows homed in a region that is leaving, a batch per statement: the other regions' rows
        still dedupe retries of live transfers, so the table cannot simply be truncated."""
        while True:
            cur.execute("DELETE FROM transfers WHERE crdb_region = %s LIMIT %s", (region, FORGET_BATCH))
            if cur.rowcount < FORGET_BATCH:
                return

    @staticmethod
    def _homed_in_database(cur) -> bool:
        cur.execute("SELECT table_name, locality FROM [SHOW TABLES] WHERE table_name = 'accounts'")
        return any(str(name) == "accounts" and locality == "REGIONAL BY ROW" for name, locality in cur.fetchall())

    def mark_failure(self, at: float | None = None) -> None:
        if self.running:
            self._failure_at = self._clock() if at is None else at
            self._baseline = baseline_before(self.stats.window()["history"], self._failure_at)
            self._recovery = None

    # -- status --

    def status(self) -> dict:
        w = self.stats.window()
        history = w["history"]
        if self._failure_at is not None and self._recovery is None:
            self._recovery = recovery_time(history, self._failure_at, self._baseline)
        balance = self._balance
        return {
            "running": self.running, "workers": self.workers, "rate": self.rate,
            "tps": w["tps"], "retries_s": w["retries_s"], "errors_s": w["errors_s"],
            "p50_ms": w["p50_ms"], "p95_ms": w["p95_ms"], "p99_ms": w["p99_ms"], "errors_total": w["errors_total"],
            "failed_total": self.stats.failed_total, "deduplicated": self.stats.deduplicated,
            "balance": balance, "expected": TOTAL,
            "money_lost": None if balance is None else TOTAL - balance,
            "invariant_ok": None if balance is None else balance == TOTAL,
            "last_check": self._last_check, "failure_at": self._failure_at,
            "recovery_s": self._recovery, "recovering": self._failure_at is not None and self._recovery is None,
            "last_error": self._last_error,
            "homing": {"on": self.homed, "regions": list(self.regions), "cross_region_pct": self.cross_region_pct,
                       "workers": workers_by_region(self.workers, self.regions) if self.homed and self.regions else {},
                       "failover": dict(self._failover), "leaving": self._leaving},
            "by_home": w["by_home"], "cross_region": w["cross_region"],
            "history": [{"ts": h["ts"], "commits": h["commits"], "retries": h["retries"], "errors": h["errors"],
                         "regions": h["regions"]} for h in history[-HISTORY_SECONDS:]],
        }

    def check_invariant(self) -> None:
        retry_with_backoff(self._check_once, max_retries=3, initial_backoff=0.2, sleep=self._sleep)

    def _check_once(self) -> None:
        conn = self._connect(self._host, statement_timeout="20s")
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT sum(balance) FROM accounts")
                self._balance = int(cur.fetchone()[0] or 0)
                self._last_check = self._clock()
        finally:
            conn.close()

    # -- workers --

    def _transfer(self, conn, tid: str, a: str, b: str, amount: int, *, home: str | None = None,
                  a_home: str | None = None, b_home: str | None = None) -> None:
        """One transfer, idempotent: the transfer id is written in the same transaction, so a retry after an
        ambiguous commit either applies it or fails with a duplicate key because it already landed. Homed
        (home given): the transfer row lives in the worker's region, so that retry finds it even through a
        failover gateway, and each account is addressed by its region and id, one partition's point lookup."""
        with conn.transaction():
            with conn.cursor() as cur:
                if home is None:
                    cur.execute("INSERT INTO transfers (id, src, dst, amount) VALUES (%s, %s, %s, %s)", (tid, a, b, amount))
                    cur.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s", (amount, a))
                    cur.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s", (amount, b))
                else:
                    cur.execute("INSERT INTO transfers (crdb_region, id, src, dst, amount) VALUES (%s, %s, %s, %s, %s)",
                                (home, tid, a, b, amount))
                    cur.execute("UPDATE accounts SET balance = balance - %s WHERE crdb_region = %s AND id = %s",
                                (amount, a_home, a))
                    if cur.rowcount != 1:  # the account is homed elsewhere now: roll back (_rehome never lets this happen)
                        raise RuntimeError(f"account {a} has moved out of {a_home}")
                    cur.execute("UPDATE accounts SET balance = balance + %s WHERE crdb_region = %s AND id = %s",
                                (amount, b_home, b))
                    if cur.rowcount != 1:
                        raise RuntimeError(f"account {b} has moved out of {b_home}")

    def _open(self, state: dict):
        """A connection and its gateway region. A homed worker connects straight to a usable node in its own
        region, or the nearest region that has one (failover); plain mode goes through the load balancer."""
        home = state.get("home")
        if not (self.homed and home):
            conn = self._connect(self._host)
            region = None
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT gateway_region()")
                    region = cur.fetchone()[0]
            except Exception:
                pass  # single-region clusters have no gateway region
            return conn, region
        snapshot = self.cluster() if self.cluster else None
        last: Exception | None = None
        for ip, region in gateway_candidates(home, snapshot or {}, state.get("rng") or random.Random()):
            try:
                conn = self._connect(ip)
            except Exception as e:  # the node died since the snapshot, or refuses: try the next one
                last = e
                continue
            state["failover"] = region != home
            self._failover[home] = region if state["failover"] else None
            return conn, region
        raise last or ConnectionError(f"no usable node to serve {home}")

    def _pick(self, rng: random.Random, home: str) -> tuple[str, str, str, str] | None:
        """(source, its region, destination, its region): both at home, except for the cross-region share.
        None when no account of the region is in reach (for a moment, while they change region)."""
        mine = self._homes.get(home)
        if not mine:
            return None
        a = rng.choice(mine)
        others = [r for r in self.regions if r != home and self._homes.get(r)]
        if others and rng.random() < self.cross_region_pct / 100:
            b_home = rng.choice(others)
            return a, home, rng.choice(self._homes[b_home]), b_home
        b = rng.choice(mine)
        while b == a and len(mine) > 1:
            b = rng.choice(mine)
        return a, home, b, home

    def _close(self, state: dict) -> None:
        conn, state["conn"] = state.get("conn"), None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _send(self, state: dict, a: str, b: str, amount: int, rng: random.Random, *, a_home: str | None = None,
              b_home: str | None = None) -> str:
        """Apply one transfer exactly once, retrying every transient error class.
        Returns committed | deduplicated | the error class that made it give up."""
        tid = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        started = self._clock()
        kind = "gave-up"
        home = state.get("home") if self.homed else None
        cross = bool(a_home and b_home and a_home != b_home)
        for attempt in range(MAX_RETRIES):
            if attempt:
                self._stop.wait(backoff_delay(attempt - 1, initial_backoff=0.05, max_backoff=3.0))
                if self._stop.is_set():
                    return "stopped"
            if state.get("conn") is None:
                try:
                    state["conn"], state["region"] = self._open(state)
                    state["opened"] = self._clock() - rng.random() * CONN_MAX_AGE / 2
                except Exception as e:
                    self.stats.error("connection")
                    self._last_error = f"connect: {e}"
                    kind = "connection"
                    continue
                if self._stop.is_set():  # stop() arrived while connecting: nothing new goes out
                    self._close(state)
                    return "stopped"
            try:
                with abandon_after(state["conn"], TRANSFER_TIMEOUT):
                    self._transfer(state["conn"], tid, a, b, amount, home=home, a_home=a_home, b_home=b_home)
                self.stats.commit((self._clock() - started) * 1000, state.get("region"), home=home, cross=cross)
                return "committed"
            except Exception as e:
                kind = classify(e)
                if kind == "duplicate" and attempt:  # an earlier attempt committed after all
                    self.stats.commit((self._clock() - started) * 1000, state.get("region"), home=home, cross=cross)
                    self.stats.dedup()
                    return "deduplicated"
                self._last_error = f"{kind}: {e}"
                if kind not in RETRYABLE:
                    self.stats.fail()
                    self._close(state)
                    return kind
                self.stats.retry()
                if kind != "serialization":  # the connection may be gone and the commit may have happened
                    self.stats.error(kind)
                    self._close(state)
        self.stats.fail()
        return kind

    def _next(self, index: int, rng: random.Random) -> tuple[str, str, str, str, str] | None:
        """Choose worker `index`'s next transfer and mark it in flight: (the worker's home, source, its region,
        destination, its region). The home is the region the worker is dealt now: it can change when a region
        joins or leaves. One step with the choice, so a re-homing sees every transfer that chose before it
        (_rehome)."""
        with self._routes:
            if len(self._dealt) != self.workers:  # the worker count changed since the last deal
                self._dealt = deal([None] * self.workers, self.regions)
            home = self._dealt[index]
            pick = self._pick(rng, home)
            if pick is None:
                return None
            self._inflight[index] = (home, pick[0], pick[2])
            return (home, *pick)

    def _turn(self, index: int, state: dict, rng: random.Random) -> str | None:
        """One transfer by worker `index`; None when its region has no account in reach right now."""
        if not (self.homed and self.regions):
            a, b = rng.sample(self._ids, 2)
            return self._send(state, a, b, rng.randint(1, 100), rng)
        chosen = self._next(index, rng)
        if chosen is None:
            return None
        try:
            home, a, a_home, b, b_home = chosen
            if home != state.get("home"):  # its customers live elsewhere now: connect where they are
                self._close(state)
                state["home"] = home
            return self._send(state, a, b, rng.randint(1, 100), rng, a_home=a_home, b_home=b_home)
        finally:
            self._inflight.pop(index, None)

    def _worker(self, index: int) -> None:
        rng = random.Random(index * 7919 + int(self._clock()))
        state: dict = {"conn": None, "home": None, "rng": rng}
        interval = self.workers / self.rate
        next_at = self._clock() + rng.random() * interval
        while not self._stop.is_set():
            if state["conn"] is not None and self._clock() - state.get("opened", 0) > CONN_MAX_AGE:
                self._close(state)
            delay = next_at - self._clock()
            if delay > 0 and self._stop.wait(delay):
                break
            next_at = max(next_at + interval, self._clock() - interval)
            self._turn(index, state, rng)
        self._close(state)

    def _checker(self) -> None:
        while not self._stop.wait(5.0):
            try:
                self.check_invariant()
            except Exception as e:
                log.debug("invariant check failed: %s", e)

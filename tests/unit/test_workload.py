import random

import psycopg
import pytest

from controlplane.homing import shares
from controlplane.workload import ACCOUNTS, MAX_RETRIES, TOTAL, Stats, Workload, classify, recovery_time


class SqlState(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.sqlstate = code


@pytest.mark.parametrize("exc, kind", [
    (SqlState("40001"), "serialization"),
    (SqlState("40003"), "ambiguous"),
    (SqlState("57014"), "timeout"),
    (psycopg.OperationalError("server closed the connection unexpectedly"), "connection"),
    (SqlState("XX000"), "other"),
])
def test_errors_are_classified(exc, kind):
    assert classify(exc) == kind


def test_stats_roll_up_per_second_with_latency_percentiles():
    s = Stats(clock=lambda: 10.4)
    for ms in range(1, 101):
        s.commit(ms, "us-east-1", now=10.4)
    s.retry(now=10.4)
    s.error("connection", now=10.4)
    snap = s.window(now=11.5)
    sec = snap["history"][-1]
    assert sec["commits"] == 100 and sec["retries"] == 1 and sec["errors"] == 1
    assert sec["regions"] == {"us-east-1": 100}
    assert snap["p50_ms"] == pytest.approx(50, abs=1) and snap["p99_ms"] == pytest.approx(99, abs=1)
    assert snap["p95_ms"] == pytest.approx(95, abs=1)
    assert snap["errors_total"] == {"connection": 1}


def test_recovery_time_is_first_2s_window_back_at_80_percent_of_baseline():
    history = [{"ts": t, "commits": 100} for t in range(0, 10)]           # baseline 100/s
    history += [{"ts": t, "commits": 0} for t in range(10, 16)]            # outage at t=10
    history += [{"ts": t, "commits": 90} for t in range(16, 20)]           # back
    assert recovery_time(history, failure_at=10.0) == 7.0                  # window [16,17] ends at 17
    assert recovery_time(history[:14], failure_at=10.0) is None            # not recovered yet
    assert recovery_time([{"ts": 0, "commits": 0}], failure_at=0.5) is None


class FakeCursor:
    def __init__(self, db):
        self.db = db

    @property
    def rowcount(self):
        return self.db.rowcount

    def execute(self, sql, params=None):
        self.db.sql.append(sql)
        self.db.calls.append((sql, params))
        self.last = sql

    def fetchall(self):
        if "SELECT id FROM accounts" in self.last:
            return [(f"00000000-0000-0000-0000-{i:012d}",) for i in range(1, 6)]
        return []

    def fetchone(self):
        if "information_schema" in self.last:
            return self.db.id_type
        if "count(*)" in self.last:
            return (self.db.count,)
        if "sum(balance)" in self.last:
            return (self.db.total,)
        if "gateway_region" in self.last:
            return ("us-east-1",)
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDB:
    def __init__(self, count=0, total=TOTAL):
        self.sql, self.calls, self.count, self.total, self.id_type = [], [], count, total, None
        self.hosts, self.refuse = [], set()  # every host connected to; hosts whose connection is refused
        self.rowcount = 1  # what every UPDATE reports

    def cursor(self):
        return FakeCursor(self)

    def connect(self, host, **kw):
        db = self
        self.hosts.append(host)
        if host in self.refuse:
            raise psycopg.OperationalError("connection refused")

        class Conn:
            autocommit = True

            def cursor(self):
                return db.cursor()

            def transaction(self):
                import contextlib
                return contextlib.nullcontext()

            def close(self):
                pass

        return Conn()


def test_setup_creates_and_seeds_accounts_only_when_needed():
    db = FakeDB(count=0)
    w = Workload(db.connect, host="haproxy")
    w.setup()
    assert any("CREATE TABLE IF NOT EXISTS accounts" in s for s in db.sql)
    assert any("generate_series(1, 10000)" in s for s in db.sql)
    db2 = FakeDB(count=ACCOUNTS)
    Workload(db2.connect, host="haproxy").setup()
    assert not any("generate_series" in s for s in db2.sql)


def test_invariant_check_reports_money_lost():
    db = FakeDB(count=ACCOUNTS, total=TOTAL - 250)
    w = Workload(db.connect, host="haproxy")
    w.check_invariant()
    st = w.status()
    assert st["balance"] == TOTAL - 250 and st["money_lost"] == 250 and st["invariant_ok"] is False


def test_one_transfer_records_its_id_and_moves_money_inside_one_transaction():
    db = FakeDB(count=ACCOUNTS)
    w = Workload(db.connect, host="haproxy")
    conn = db.connect("haproxy")
    w._transfer(conn, "t-1", "a", "b", 5)
    assert db.sql[0].startswith("INSERT INTO transfers")
    assert len([s for s in db.sql if s.startswith("UPDATE accounts")]) == 2


class ScriptedWorkload(Workload):
    """Each transfer attempt raises the next scripted error (None = success)."""
    def __init__(self, script):
        super().__init__(FakeDB().connect, host="haproxy", sleep=lambda s: None)
        self.script, self.ids, self.opened = list(script), [], 0
        self._stop.wait = lambda t=None: False

    def _open(self, state):
        self.opened += 1
        return object(), "us-east-1"

    def _transfer(self, conn, tid, a, b, amount, **kw):
        self.ids.append(tid)
        err = self.script.pop(0)
        if err:
            raise err


def test_every_transient_error_class_is_retried_with_the_same_transfer_id():
    w = ScriptedWorkload([SqlState("40001"), SqlState("40003"), SqlState("08006"), SqlState("57P01"), SqlState("57014"), None])
    assert w._send({}, "a", "b", 5, __import__("random").Random(1)) == "committed"
    assert len(set(w.ids)) == 1 and len(w.ids) == 6
    assert w.opened == 5  # serialization keeps the connection; every maybe-committed error reconnects
    errors = w.stats.window(now=1e12)["errors_total"]
    assert errors == {"ambiguous": 1, "connection": 2, "timeout": 1}


def test_a_duplicate_after_an_ambiguous_commit_means_it_was_already_applied():
    w = ScriptedWorkload([SqlState("40003"), SqlState("23505")])
    assert w._send({}, "a", "b", 5, __import__("random").Random(1)) == "deduplicated"
    assert w.status()["deduplicated"] == 1 and w.status()["failed_total"] == 0


def test_non_retryable_errors_fail_the_transfer_once():
    w = ScriptedWorkload([SqlState("42601")])
    assert w._send({}, "a", "b", 5, __import__("random").Random(1)) == "other"
    assert w.status()["failed_total"] == 1


def test_configure_validates_ranges():
    w = Workload(FakeDB().connect, host="haproxy")
    with pytest.raises(ValueError):
        w.configure(workers=0, rate=100)
    w.configure(workers=4, rate=50)
    assert (w.status()["workers"], w.status()["rate"]) == (4, 50)


def test_history_has_a_row_for_every_second_even_without_commits():
    s = Stats(clock=lambda: 0)
    s.commit(5, "r", now=10.2)
    s.commit(5, "r", now=14.2)
    secs = [h["ts"] for h in s.window(now=16.0)["history"]]
    assert secs == [10, 11, 12, 13, 14, 15]
    assert s.window(now=16.0)["history"][2]["commits"] == 0


def test_accounts_use_uuid_keys_and_old_integer_tables_are_replaced():
    db = FakeDB(count=0)
    db.id_type = ("INT8",)
    w = Workload(db.connect, host="haproxy")
    w.setup()
    assert "DROP TABLE accounts" in db.sql
    assert any("id UUID PRIMARY KEY DEFAULT gen_random_uuid()" in s for s in db.sql)
    assert len(w._ids) == 5


def test_recovery_uses_the_baseline_captured_at_failure_even_after_it_scrolls_out():
    history = [{"ts": t, "commits": 0} for t in range(200, 300)] + [{"ts": t, "commits": 90} for t in range(300, 303)]
    assert recovery_time(history, failure_at=190.0, baseline=100) == 111.0
    assert recovery_time(history, failure_at=190.0) is None  # without the stored baseline it would be stuck


def test_stopping_clears_a_pending_recovery_measurement():
    w = Workload(FakeDB().connect, host="haproxy")
    w.running = True
    w.mark_failure(at=5.0)
    w.stop()
    assert w.status()["failure_at"] is None and w.status()["recovering"] is False


def test_rates_average_the_last_three_seconds_so_the_stage_number_holds_steady():
    # one slow second must not read as a collapse on the big screen: 250, 250, 156 -> 219, not 156
    s = Stats(clock=lambda: 13.5)
    for sec, n in ((10, 250), (11, 250), (12, 156)):
        for i in range(n):
            s.commit(80, "us-east-1", now=sec + 0.5, home="us-east-1", cross=i < n // 10)
    snap = s.window(now=13.5)
    assert snap["tps"] == 219
    assert snap["by_home"]["us-east-1"]["tps"] == 219
    assert snap["cross_region"]["tps"] == 22  # (25 + 25 + 15) / 3


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


class HomedDB(FakeDB):
    """A multi-region chaos database. layout: {table: (locality, create_stmt)} for the tables that exist."""
    def __init__(self, regions=("us-east-1", "us-west-2", "eu-west-1"), layout=None, counts=None, holder_home=True):
        super().__init__(count=ACCOUNTS)
        self.regions, self.layout, self.counts, self.holder_home = list(regions), layout or {}, counts, holder_home

    def cursor(self):
        return HomedCursor(self)


class HomedCursor(FakeCursor):
    def fetchall(self):
        last = self.last
        if "SHOW REGIONS FROM DATABASE" in last:
            primary = getattr(self.db, "primary", None) or (self.db.regions[0] if self.db.regions else None)
            return [("chaos", r, r == primary, False, []) for r in self.db.regions]
        if "SHOW TABLES" in last:
            return [(t, loc) for t, (loc, _) in self.db.layout.items()]
        if "crdb_region, count(*)" in last:
            return list((self.db.counts or {}).items())
        if "SELECT crdb_region, id FROM accounts" in last:
            homed = getattr(self.db, "account_regions", None) or self.db.regions
            return [(r, f"00000000-0000-0000-000{i}-{k:012d}") for i, r in enumerate(homed) for k in range(1, 4)]
        return super().fetchall()

    def fetchone(self):
        last = self.last
        if "SHOW CREATE TABLE" in last:
            name = last.split()[-1]
            return (name, self.db.layout[name][1])
        if "SHOW RANGE FROM TABLE" in last:
            region = last.split("FOR ROW ('")[1].split("'")[0]
            if getattr(self.db, "range_of", None):  # a test says where each partition's range is
                return self.db.range_of(last.split("FROM TABLE ")[1].split()[0], region)
            return (42, 7 if self.db.holder_home else 9, [7, 8, 9],
                    [f"region={region},zone=a", f"region={region},zone=b", "region=elsewhere,zone=a"])
        return super().fetchone()


HOMED = ("REGIONAL BY ROW", "CREATE TABLE accounts (... CONSTRAINT accounts_pkey PRIMARY KEY (crdb_region ASC, id ASC) ...)")
PLAIN = ("REGIONAL BY TABLE IN PRIMARY REGION", "CREATE TABLE accounts (... PRIMARY KEY (id ASC) ...)")
FULL = {"us-east-1": 3334, "us-west-2": 3333, "eu-west-1": 3333}


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
    db = HomedDB(layout={"accounts": HOMED, "transfers": HOMED}, counts=FULL)
    Workload(db.connect, host="haproxy", cluster=dict).setup()
    assert not any(s.startswith("DROP TABLE") or s.startswith("TRUNCATE") for s in db.sql)


def test_accounts_are_seeded_round_robin_with_an_exact_total():
    db = HomedDB(layout={"accounts": HOMED, "transfers": HOMED}, counts={"us-east-1": 10000})
    Workload(db.connect, host="haproxy", cluster=dict).setup()
    seeds = [p for s, p in db.calls if s.startswith("INSERT INTO accounts (crdb_region, balance)")]
    assert seeds == [("us-east-1", 1000, 3334), ("us-west-2", 1000, 3333), ("eu-west-1", 1000, 3333)]
    assert "TRUNCATE accounts" in db.sql


def test_partition_leases_are_moved_home_when_they_are_elsewhere():
    db = HomedDB(layout={"accounts": HOMED, "transfers": HOMED}, counts=FULL, holder_home=False)
    Workload(db.connect, host="haproxy", cluster=dict).setup()
    assert db.sql.count("ALTER RANGE 42 RELOCATE LEASE TO 7") == 6  # two tables, three regions
    db2 = HomedDB(layout={"accounts": HOMED, "transfers": HOMED}, counts=FULL)
    Workload(db2.connect, host="haproxy", cluster=dict).setup()
    assert not any("RELOCATE LEASE" in s for s in db2.sql)


# eu-west-1 sorts first alphabetically, as SHOW REGIONS lists them: it must still never receive accounts
TIE = {"regions": [{"name": "us-east-1", "primary": True}, {"name": "us-west-2", "secondary": True},
                   {"name": "eu-west-1", "tie_breaker": True}], "nodes": []}


def test_payments_home_no_accounts_or_workers_in_a_tie_breaker():
    db = HomedDB(layout={"accounts": HOMED, "transfers": HOMED}, counts={})
    db.account_regions = ["us-east-1", "us-west-2"]
    w = Workload(db.connect, host="haproxy", cluster=lambda: TIE)
    w.setup()
    seeds = [p for s, p in db.calls if s.startswith("INSERT INTO accounts (crdb_region, balance)")]
    assert seeds == [("us-east-1", 1000, 5000), ("us-west-2", 1000, 5000)]
    assert w.regions == ["us-east-1", "us-west-2"] and sorted(w._homes) == ["us-east-1", "us-west-2"]


def test_evacuating_never_moves_accounts_into_a_tie_breaker():
    db = LedgerDB(ledger(("us-east-1", "us-west-2")), regions=("eu-west-1", "us-east-1", "us-west-2"))
    db.primary = "us-east-1"
    w = Workload(db.connect, host="haproxy", cluster=lambda: TIE)
    w.evacuate("us-east-1")
    assert {r: len(ids) for r, ids in db.accounts.items() if ids} == {"us-west-2": ACCOUNTS}


def test_a_single_region_database_keeps_the_plain_layout():
    db = HomedDB(regions=())
    w = Workload(db.connect, host="haproxy", cluster=dict)
    w.setup()
    assert any("id UUID PRIMARY KEY DEFAULT gen_random_uuid()" in s for s in db.sql) and not w.homed


LIVE = {"regions": [{"name": "us-east-1", "location": [38.9, -77.4], "primary": True},
                    {"name": "us-west-2", "location": [45.8, -119.7], "primary": False},
                    {"name": "eu-west-1", "location": [53.3, -6.3], "primary": False}],
        "nodes": [{"name": "us-east-1a-n1", "region": "us-east-1", "ip": "10.77.1.11", "liveness": "live", "runtime": "running", "partitioned": False, "isolated": False},
                  {"name": "us-west-2a-n1", "region": "us-west-2", "ip": "10.77.2.11", "liveness": "live", "runtime": "running", "partitioned": False, "isolated": False},
                  {"name": "eu-west-1a-n1", "region": "eu-west-1", "ip": "10.77.3.11", "liveness": "live", "runtime": "running", "partitioned": False, "isolated": False}]}


def homed_workload(snapshot, refuse=()):
    """A workload whose setup already ran on a 3-region database; connecting to a host in `refuse` fails."""
    db = HomedDB(layout={"accounts": HOMED, "transfers": HOMED}, counts=FULL)
    db.refuse = set(refuse)
    w = Workload(db.connect, host="haproxy", cluster=lambda: snapshot, sleep=lambda s: None)
    w.setup()
    db.hosts.clear()
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


def test_the_cross_region_share_changes_on_the_fly():
    """Scenarios change it mid-run: no restart, so the history and the recovery baseline stay intact."""
    w, _ = homed_workload(LIVE)
    w.stop = w.start = lambda: (_ for _ in ()).throw(AssertionError("restarted"))
    w.set_cross_region(0)
    assert all(w._pick(random.Random(i), "us-west-2")[3] == "us-west-2" for i in range(200))
    assert w.status()["homing"]["cross_region_pct"] == 0
    with pytest.raises(ValueError):
        w.set_cross_region(51)


def test_status_describes_the_homing():
    w, _ = homed_workload(LIVE)
    h = w.status()["homing"]
    assert h["on"] and h["regions"] == ["us-east-1", "us-west-2", "eu-west-1"] and h["cross_region_pct"] == 10
    assert h["workers"] == {"us-east-1": 16, "us-west-2": 16, "eu-west-1": 16}
    plain = Workload(FakeDB(count=ACCOUNTS).connect, host="haproxy")
    plain.setup()
    assert plain.status()["homing"] == {"on": False, "regions": [], "cross_region_pct": 10, "workers": {}, "failover": {},
                                        "leaving": None}


# -- the regions change while Payments runs: accounts and workers follow, and nothing stops --

class LedgerDB(HomedDB):
    """A homed chaos database that keeps its accounts by region, so rows can move from one region to another.
    transfer_rows: {region: rows homed there}; fail_move: raised by the statement that moves accounts."""
    def __init__(self, accounts, regions=None):
        super().__init__(regions=regions or list(accounts), layout={"accounts": HOMED, "transfers": HOMED})
        self.accounts = {r: list(ids) for r, ids in accounts.items()}
        self.transfer_rows: dict[str, int] = {}
        self.fail_move = None
        self.moved_anyway = False  # with fail_move: the move committed before the error reached the client

    def cursor(self):
        return LedgerCursor(self)

    def connect(self, host, **kw):
        conn, db = super().connect(host, **kw), self

        class Commit:  # the rows moved, but the client never hears the commit's answer
            def __enter__(self):
                return self

            def __exit__(self, failed, *rest):
                if failed is None and db.fail_move and db.moved_anyway:
                    raise db.fail_move
                return False
        conn.transaction = Commit
        return conn


class LedgerCursor(HomedCursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        self.db.rowcount = 1
        if sql.startswith("UPDATE accounts SET crdb_region"):
            if self.db.fail_move and not self.db.moved_anyway:
                raise self.db.fail_move
            target, source, ids = params
            there = set(self.db.accounts.get(source, ()))
            found = [i for i in ids if i in there]
            self.db.accounts[source] = [i for i in self.db.accounts.get(source, ()) if i not in set(found)]
            self.db.accounts.setdefault(target, []).extend(found)
            self.db.rowcount = len(found)
        elif sql.startswith("DELETE FROM transfers"):
            left = self.db.transfer_rows.get(params[0], 0)
            self.db.rowcount = min(left, params[1])
            self.db.transfer_rows[params[0]] = left - self.db.rowcount

    def fetchall(self):
        if "SELECT crdb_region, id FROM accounts" in self.last:
            return [(r, i) for r, ids in self.db.accounts.items() for i in ids]
        if "crdb_region, count(*)" in self.last:
            return [(r, len(ids)) for r, ids in self.db.accounts.items() if ids]
        return super().fetchall()

    def fetchone(self):
        sql, params = self.db.calls[-1]
        if sql.startswith("SELECT count(*) FROM accounts WHERE crdb_region = %s AND id = %s"):
            return (int(params[1] in self.db.accounts.get(params[0], ())),)
        return super().fetchone()


THREE = ("us-east-1", "us-west-2", "eu-west-1")
FOUR = (*THREE, "ap-northeast-1")


def ledger(regions=THREE):
    """Account ids by region, the way setup seeds a database with these regions."""
    return {r: [f"{k:08d}-0000-0000-0000-{i:012d}" for i in range(n)] for k, (r, n) in enumerate(shares(ACCOUNTS, regions).items())}


def running_workload(regions=THREE, snapshot=LIVE):
    """Payments running on a database with these regions. No threads: a test plays the workers' turns itself."""
    db = LedgerDB(ledger(regions))
    w = Workload(db.connect, host="haproxy", cluster=lambda: snapshot, sleep=lambda s: None)
    w.setup()
    w.running = True
    w.stop = lambda: (_ for _ in ()).throw(AssertionError("Payments were stopped"))
    w._background = lambda work: None  # no threads in tests; the one about leases below runs the work itself
    db.sql.clear()
    db.calls.clear()
    return w, db


def homed(db):
    return {r: len(ids) for r, ids in db.accounts.items() if ids}


def moved(db):
    return [s for s in db.sql if s.startswith("UPDATE accounts SET crdb_region")]


def test_a_region_is_evacuated_while_payments_keep_running():
    w, db = running_workload(FOUR)
    db.transfer_rows["ap-northeast-1"] = 12_000
    note = w.evacuate("ap-northeast-1")
    assert w.running is True  # stop() would have raised
    assert homed(db) == {"us-east-1": 3334, "us-west-2": 3333, "eu-west-1": 3333}
    assert w.regions == list(THREE) and {r: sorted(ids) for r, ids in w._homes.items()} == {r: sorted(db.accounts[r]) for r in THREE}
    assert len(w._ids) == ACCOUNTS
    assert db.transfer_rows["ap-northeast-1"] == 0  # the region homes no row any more: DROP REGION can run
    assert not any(s.startswith("TRUNCATE") for s in db.sql)  # other regions' transfer rows still dedupe live retries
    assert w.status()["homing"]["leaving"] == "ap-northeast-1" and "2,500" in note
    # The stage shows a region's nodes as draining when a step of the removal names the region: the line names only
    # the one that leaves, or every node on the stage would look as if it were draining.
    assert "ap-northeast-1" in note and not any(region in note for region in THREE)
    w.removal_done("ap-northeast-1")
    assert w.status()["homing"]["leaving"] is None


@pytest.mark.parametrize("in_flight", [
    lambda w: ("us-east-1", w._homes["us-east-1"][0], w._homes["ap-northeast-1"][0]),   # pays an account that will move
    lambda w: ("ap-northeast-1", w._homes["ap-northeast-1"][0], w._homes["us-east-1"][0]),  # sent from the leaving region
], ids=["to-an-account-that-moves", "homed-in-the-leaving-region"])
def test_rows_move_only_after_the_payments_already_on_their_way_have_finished(in_flight):
    w, db = running_workload(FOUR)
    w._inflight[7] = in_flight(w)
    moved_at_each_wait = []

    def wait(seconds):
        moved_at_each_wait.append(len(moved(db)))
        if len(moved_at_each_wait) == 3:
            w._inflight.pop(7)  # the payment finishes
    w._sleep = wait
    w.evacuate("ap-northeast-1")
    assert moved_at_each_wait == [0, 0, 0] and moved(db)


def test_no_new_payment_can_choose_an_account_while_it_is_moving():
    w, db = running_workload(FOUR)
    leaving = set(w._homes["ap-northeast-1"])
    w._inflight[7] = ("ap-northeast-1", w._homes["ap-northeast-1"][0], w._homes["ap-northeast-1"][1])
    seen = []

    def wait(seconds):  # while the evacuation waits, the other workers keep paying
        for index in range(12):
            seen.append(w._next(index, random.Random(index)))
        w._inflight.clear()
    w._sleep = wait
    w.evacuate("ap-northeast-1")
    assert seen and all(home != "ap-northeast-1" and a not in leaving and b not in leaving for home, a, _, b, _ in seen)


def test_an_evacuation_that_cannot_wait_out_a_payment_changes_nothing():
    w, db = running_workload(FOUR)
    w._inflight[7] = ("ap-northeast-1", w._homes["ap-northeast-1"][0], w._homes["ap-northeast-1"][1])
    before = {r: list(ids) for r, ids in w._homes.items()}
    with pytest.raises(RuntimeError, match="in flight"):
        w.evacuate("ap-northeast-1")  # the payment never finishes
    assert not any(s.startswith(("UPDATE", "DELETE")) for s in db.sql)
    assert w.regions == list(FOUR) and w._homes == before and w.status()["homing"]["leaving"] is None


def test_accounts_that_could_not_move_are_back_in_reach():
    w, db = running_workload(FOUR)
    db.fail_move = SqlState("XX000")
    before = {r: list(ids) for r, ids in w._homes.items()}
    with pytest.raises(SqlState):
        w.evacuate("ap-northeast-1")
    assert w.regions == list(FOUR) and w._homes == before and w.status()["homing"]["leaving"] is None


def test_a_move_whose_commit_was_never_answered_is_checked_in_the_database():
    """A dropped connection or an ambiguous result at COMMIT leaves the client not knowing. Putting the accounts back
    in reach at their old home would then be wrong if the rows did move: every payment to them would miss its row."""
    w, db = running_workload(FOUR)
    db.fail_move, db.moved_anyway = SqlState("40003"), True
    note = w.evacuate("ap-northeast-1")
    assert "2,500" in note and w.regions == list(THREE)
    assert {r: sorted(ids) for r, ids in w._homes.items()} == {r: sorted(db.accounts[r]) for r in THREE}

    w, db = running_workload(FOUR)
    db.fail_move = SqlState("40003")  # same answer, but this time nothing moved
    before = {r: list(ids) for r, ids in w._homes.items()}
    with pytest.raises(SqlState):
        w.evacuate("ap-northeast-1")
    assert w.regions == list(FOUR) and w._homes == before


def test_a_move_that_misses_rows_is_an_error_not_a_silent_loss_of_track():
    w, db = running_workload(FOUR)
    db.accounts["ap-northeast-1"].pop()  # the database holds one account fewer than the plan was made for
    orig = LedgerCursor.fetchall
    stale = [(r, i) for r, ids in ledger(FOUR).items() for i in ids]
    LedgerCursor.fetchall = lambda self: stale if "SELECT crdb_region, id FROM accounts" in self.last else orig(self)
    try:
        with pytest.raises(RuntimeError, match="were to move"):
            w.evacuate("ap-northeast-1")
    finally:
        LedgerCursor.fetchall = orig
    assert w.regions == list(FOUR)


def test_a_worker_follows_its_customers_to_their_new_region():
    w, db = running_workload(FOUR)
    sent, closed = [], []
    w._send = lambda state, a, b, amount, rng, **kw: sent.append((state["home"], kw["a_home"]))
    w._close = lambda state: closed.append(state.get("home"))
    state, rng = {"conn": None, "home": None}, random.Random(1)
    w._turn(3, state, rng)  # the fourth worker serves the fourth region
    assert sent[-1] == ("ap-northeast-1", "ap-northeast-1")
    w.evacuate("ap-northeast-1")
    w._turn(3, state, rng)
    assert sent[-1] == ("us-east-1", "us-east-1")  # dealt over three regions now
    assert closed == [None, "ap-northeast-1"]  # it left its connection in the old region and connects in the new one


def test_a_worker_whose_region_stays_keeps_its_home_and_its_connection():
    """Dealing the workers afresh whenever the regions change would move three in four of them and reconnect each one:
    a storm of new connections at the very moment the cluster is busy changing shape."""
    w, db = running_workload()
    sent, closed = [], []
    w._send = lambda state, a, b, amount, rng, **kw: sent.append(state["home"])
    w._close = lambda state: closed.append(state.get("home"))
    states = [{"conn": None, "home": None} for _ in range(w.workers)]
    for index, state in enumerate(states):
        w._turn(index, state, random.Random(index))
    first, closed[:] = list(sent), []
    db.regions.append("ap-northeast-1")
    w.expand("ap-northeast-1")
    for index, state in enumerate(states):
        w._turn(index, state, random.Random(index))
    second = sent[len(first):]
    moved = [i for i in range(w.workers) if first[i] != second[i]]
    assert len(moved) == 12 and all(second[i] == "ap-northeast-1" for i in moved)
    assert len(closed) == 12  # only the workers that moved left their connection
    closed[:] = []
    w.evacuate("ap-northeast-1")
    for index, state in enumerate(states):
        w._turn(index, state, random.Random(index))
    third = sent[len(first) + len(second):]
    assert len(closed) == 12 and "ap-northeast-1" not in third
    assert {r: third.count(r) for r in THREE} == {r: 16 for r in THREE}


def test_a_transfer_counts_as_in_flight_until_it_has_finished_whatever_happens():
    w, db = running_workload()
    during = {}

    def send(state, a, b, amount, rng, **kw):
        during.update(w._inflight)
        raise RuntimeError("boom")
    w._send = send
    with pytest.raises(RuntimeError, match="boom"):
        w._turn(5, {"conn": None, "home": None}, random.Random(2))
    (home, a, b), = during.values()
    assert home == "eu-west-1" and a in w._homes["eu-west-1"]  # the sixth worker serves the third region
    assert w._inflight == {}


def test_a_worker_whose_region_has_no_account_in_reach_skips_its_turn():
    w, db = running_workload()
    w._homes["us-west-2"] = []
    w._send = lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent"))
    assert w._turn(1, {"conn": None, "home": None}, random.Random(1)) is None and w._inflight == {}


def test_payments_started_during_a_removal_home_nothing_in_the_leaving_region():
    db = LedgerDB(ledger(FOUR))
    w = Workload(db.connect, host="haproxy", cluster=lambda: LIVE, sleep=lambda s: None)
    w.evacuate("ap-northeast-1")  # Payments is not running: the rows simply move
    assert homed(db) == {"us-east-1": 3334, "us-west-2": 3333, "eu-west-1": 3333}
    db.sql.clear()
    w.setup()  # DROP REGION has not run yet: the database still lists the region
    assert w.regions == list(THREE) and "ap-northeast-1" not in w._homes
    assert not any(s.startswith(("TRUNCATE", "INSERT INTO accounts")) for s in db.sql)  # nothing reseeded
    w.removal_done("ap-northeast-1")
    w.setup()  # the removal gave up and the region stayed: accounts may live there again
    assert w.regions == list(FOUR)


def test_evacuating_the_primary_spreads_its_accounts_over_the_other_regions():
    w, db = running_workload()
    w.evacuate("us-east-1")
    assert homed(db) == {"us-west-2": 5000, "eu-west-1": 5000} and w.primary == "us-west-2"


def test_evacuate_does_nothing_in_plain_mode():
    db = FakeDB(count=ACCOUNTS)
    w = Workload(db.connect, host="haproxy")
    w.setup()
    w.running = True
    assert w.evacuate("us-east-1") is None
    assert not any(s.startswith(("UPDATE", "TRUNCATE", "DELETE")) for s in db.sql)  # a look at the layout is fine
    assert w.running is True and w.status()["homing"]["leaving"] is None
    w.removal_done("us-east-1")
    assert w.running is True


def test_evacuate_works_on_a_workload_that_never_started_in_this_process():
    """After a control-plane restart nothing has called setup(), but the tables still hold homed rows."""
    db = LedgerDB(ledger())
    w = Workload(db.connect, host="haproxy", cluster=dict)
    w.evacuate("eu-west-1")
    assert homed(db) == {"us-east-1": 5000, "us-west-2": 5000}


def test_evacuating_the_only_region_is_refused_with_a_reason():
    db = LedgerDB(ledger(("us-east-1",)))
    w = Workload(db.connect, host="haproxy", cluster=dict)
    with pytest.raises(RuntimeError, match="only region"):
        w.evacuate("us-east-1")
    assert not any(s.startswith(("UPDATE", "TRUNCATE", "DELETE")) for s in db.sql)
    assert w.status()["homing"]["leaving"] is None


def test_a_region_with_nothing_homed_in_it_leaves_without_a_word():
    w, db = running_workload()          # three regions serve...
    db.regions.append("ap-northeast-1")  # ...and a fourth joined the database but never got accounts
    assert w.evacuate("ap-northeast-1") is None
    assert not moved(db) and w.regions == list(THREE) and w.running is True


def test_a_region_that_joins_takes_its_share_of_customers_while_payments_run():
    w, db = running_workload()
    db.regions.append("ap-northeast-1")  # ALTER DATABASE ... ADD REGION has run
    note = w.expand("ap-northeast-1")
    assert w.running is True  # stop() would have raised
    assert homed(db) == {r: 2500 for r in FOUR}
    assert w.regions == list(FOUR) and {r: sorted(ids) for r, ids in w._homes.items()} == {r: sorted(db.accounts[r]) for r in FOUR}
    assert w.status()["homing"]["workers"] == {r: 12 for r in FOUR}
    assert "ap-northeast-1" in note and "2,500" in note


def new_region_ranges(db, region, phase):
    """Where the partitions of a region that just joined are: phase["now"] is "no replica yet", "replicas arrived"
    or "home". Every other region's lease is at home."""
    def range_of(table, asked):
        at_home = [f"region={asked},zone=a", f"region={asked},zone=b"]
        if asked != region or phase["now"] == "home":
            return (42, 7, [7, 8, 9], [*at_home, "region=elsewhere,zone=a"])
        if phase["now"] == "no replica yet":
            return (42, 9, [9, 10, 11], ["region=us-east-1,zone=a", "region=us-west-2,zone=a", "region=eu-west-1,zone=a"])
        return (42, 9, [7, 8, 9], [*at_home, "region=us-east-1,zone=a"])
    db.range_of = range_of


def test_a_new_regions_leases_come_home_once_it_holds_replicas():
    """Right after ADD REGION the region's partitions have no replica there yet, so their leases cannot move home and
    every statement of its payments crosses an ocean. Payments keeps looking and moves each lease as soon as it can."""
    w, db = running_workload()
    later, phase = [], {"now": "no replica yet"}
    w._background = later.append
    new_region_ranges(db, "ap-northeast-1", phase)
    db.regions.append("ap-northeast-1")
    w.expand("ap-northeast-1")
    assert len(later) == 1 and not any("RELOCATE" in s for s in db.sql)  # nothing there to hold the lease yet
    waits = []

    def wait(seconds):
        waits.append(seconds)
        phase["now"] = ["replicas arrived", "home"][min(len(waits) - 1, 1)]
        return False
    w._stop.wait = wait
    later[0]()
    assert db.sql.count("ALTER RANGE 42 RELOCATE LEASE TO 7") == 2  # accounts and transfers, once replicas were there
    assert len(waits) == 2  # and it stopped looking when both were home


def test_the_search_for_a_new_regions_leases_ends_with_payments_or_with_the_region():
    w, db = running_workload()
    later, phase = [], {"now": "no replica yet"}
    w._background = later.append
    new_region_ranges(db, "ap-northeast-1", phase)
    db.regions.append("ap-northeast-1")
    w.expand("ap-northeast-1")
    db.sql.clear()
    w._stop.wait = lambda seconds: True  # Payments stopped
    later[0]()
    assert db.sql == []
    waits = []
    w._stop.wait = lambda seconds: waits.append(seconds) or False
    w.regions = [r for r in w.regions if r != "ap-northeast-1"]  # the region left again
    later[0]()
    assert db.sql == [] and len(waits) == 1
    w.regions = list(FOUR)  # it stays without replicas: the search gives up in the end instead of running for ever
    del waits[:]
    later[0]()
    # ...but not soon: the allocator has every range of the databases to place after ADD REGION, and in one take the
    # new region's partitions waited more than seven minutes for their turn. Five-second looks for at least 15 minutes.
    assert 180 <= len(waits) < 1000 and all(seconds == 5.0 for seconds in waits) and not any("RELOCATE" in s for s in db.sql)


def test_a_region_that_joins_while_payments_are_stopped_is_left_for_the_next_start():
    db = LedgerDB(ledger())
    w = Workload(db.connect, host="haproxy", cluster=lambda: LIVE, sleep=lambda s: None)
    w.setup()
    db.sql.clear()
    db.regions.append("ap-northeast-1")
    assert w.expand("ap-northeast-1") is None and db.sql == []


def test_a_tie_breaker_that_joins_gets_no_customers():
    w, db = running_workload(("us-east-1", "us-west-2"), snapshot=TIE)
    db.regions.append("eu-west-1")
    assert w.expand("eu-west-1") is None and not moved(db) and w.regions == ["us-east-1", "us-west-2"]


def test_a_plain_workload_ignores_a_region_that_joins():
    db = FakeDB(count=ACCOUNTS)
    w = Workload(db.connect, host="haproxy")
    w.setup()
    w.running = True
    db.sql.clear()
    assert w.expand("us-east-1") is None and db.sql == []


def test_a_homed_update_that_hits_no_row_rolls_the_transfer_back():
    w, db = homed_workload(LIVE)
    db.rowcount = 0  # the account moved to another region since the worker chose it
    with pytest.raises(RuntimeError, match="moved"):
        w._transfer(db.connect("x"), "t-1", "a", "b", 5, home="us-east-1", a_home="us-east-1", b_home="us-east-1")


def test_a_stopping_worker_does_not_start_a_transfer_after_it_connects():
    w = ScriptedWorkload([None])
    w._stop.set()
    assert w._send({}, "a", "b", 5, random.Random(1)) == "stopped" and w.ids == []

import random

from controlplane.homing import deal, gateway_candidates, home_of, region_order, rehoming, shares, workers_by_region

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


def test_failover_goes_to_a_serving_region_before_a_nearer_tie_breaker():
    # one metro: the GCP tie-breaker is 14 km from us-east-1, Azure 240 km; clients still go to Azure
    regions = [{"name": "us-east-1", "location": [38.9, -77.4], "primary": True},
               {"name": "eastus", "location": [37.4, -79.8], "secondary": True},
               {"name": "us-east4", "location": [39.0, -77.5], "tie_breaker": True}]
    assert region_order("us-east-1", regions) == ["us-east-1", "eastus", "us-east4"]


# -- accounts follow the regions: a region that joins takes its share, one that leaves gives everything up --

def _accounts(region, count):
    return [f"{region}-{i}" for i in range(count)]


def test_accounts_are_shared_evenly_with_the_remainder_to_the_first_regions():
    assert shares(10_000, ["a", "b", "c"]) == {"a": 3334, "b": 3333, "c": 3333}
    assert shares(10_000, ["a", "b", "c", "d"]) == {"a": 2500, "b": 2500, "c": 2500, "d": 2500}


def test_a_region_that_leaves_gives_every_account_to_the_regions_that_stay():
    have = {r: _accounts(r, 2500) for r in ("a", "b", "c", "d")}
    moves = rehoming(have, ["a", "b", "c"])
    assert {(source, target): len(ids) for source, target, ids in moves} == {("d", "a"): 834, ("d", "b"): 833, ("d", "c"): 833}
    assert sorted(i for _, _, ids in moves for i in ids) == sorted(have["d"])  # every one of them, each once


def test_a_region_that_joins_takes_its_share_from_the_others():
    have = {"a": _accounts("a", 3334), "b": _accounts("b", 3333), "c": _accounts("c", 3333)}
    moves = rehoming(have, ["a", "b", "c", "d"])
    assert {(source, target): len(ids) for source, target, ids in moves} == {("a", "d"): 834, ("b", "d"): 833, ("c", "d"): 833}
    assert len({i for _, _, ids in moves for i in ids}) == 2500


def test_a_layout_that_is_already_even_needs_no_moves():
    have = {"a": _accounts("a", 3334), "b": _accounts("b", 3333), "c": _accounts("c", 3333)}
    assert rehoming(have, ["a", "b", "c"]) == []


def test_the_moves_leave_exactly_the_layout_a_fresh_setup_seeds():
    have = {"a": _accounts("a", 9000), "b": _accounts("b", 1000), "old": _accounts("old", 0)}
    after = {r: len(ids) for r, ids in have.items()}
    for source, target, ids in rehoming(have, ["a", "b", "c"]):
        after[source] -= len(ids)
        after[target] = after.get(target, 0) + len(ids)
    assert after == {"a": 3334, "b": 3333, "c": 3333, "old": 0}


# -- workers follow the regions too, and only as many move as must --

def test_a_fresh_deal_is_round_robin():
    regions = ["a", "b", "c"]
    assert deal([None] * 5, regions) == [home_of(i, regions) for i in range(5)] == ["a", "b", "c", "a", "b"]


def test_when_a_region_joins_only_its_share_of_the_workers_moves():
    before = deal([None] * 48, ["a", "b", "c"])
    after = deal(before, ["a", "b", "c", "d"])
    assert {r: after.count(r) for r in "abcd"} == workers_by_region(48, ["a", "b", "c", "d"]) == {"a": 12, "b": 12, "c": 12, "d": 12}
    moved = [i for i in range(48) if before[i] != after[i]]
    assert len(moved) == 12 and all(after[i] == "d" for i in moved)  # the other 36 keep their region and their connection


def test_when_a_region_leaves_only_its_workers_move():
    before = deal([None] * 48, ["a", "b", "c", "d"])
    after = deal(before, ["a", "b", "c"])
    assert {r: after.count(r) for r in "abc"} == {"a": 16, "b": 16, "c": 16}
    assert all(before[i] == "d" for i in range(48) if before[i] != after[i])
    assert [after[i] for i in range(48) if before[i] == "d"][:4] == ["a", "b", "c", "a"]  # spread over the regions that stay


def test_a_deal_copes_with_uneven_numbers():
    before = deal([None] * 4, ["a", "b", "c"])  # a, b, c, a
    after = deal(before, ["a", "b"])
    assert sorted(after) == ["a", "a", "b", "b"] and after[:2] == ["a", "b"]

"""Where a Payments worker lives and where it connects.

On a multi-region cluster every account and every worker belongs to a region. A worker connects to a node in its
own region, so each statement of a transfer is local and only the commit crosses a region. When its region has no
usable node it connects to the nearest region that has one, and tries home again on every reconnect.

The regions can change while Payments runs: a region that joins takes its share of the accounts and workers, and
one that leaves hands them to the regions that stay (see Workload.expand and Workload.evacuate).
"""
from __future__ import annotations

import math
import random
from typing import Sequence


def distance_km(a: Sequence[float], b: Sequence[float]) -> float:
    """Great-circle distance between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def home_of(index: int, regions: Sequence[str]) -> str:
    """The region worker `index` serves: workers go round robin over the regions."""
    return regions[index % len(regions)]


def workers_by_region(workers: int, regions: Sequence[str]) -> dict[str, int]:
    counts = {r: 0 for r in regions}
    for i in range(workers):
        counts[home_of(i, regions)] += 1
    return counts


def deal(homes: Sequence[str | None], regions: Sequence[str]) -> list[str]:
    """Every worker's home region once the regions are `regions`. Each region gets its round-robin share
    (workers_by_region), and a worker keeps its home whenever its region still has room for it: when a region joins
    or leaves, only as many workers move, and reconnect, as must. `homes`: the homes so far in worker order, None
    for a worker without one. A fresh deal ([None] * n) is home_of for every worker."""
    room = workers_by_region(len(homes), regions)
    kept: list[str | None] = []
    for home in homes:
        stays = home in room and room[home] > 0
        if stays:
            room[home] -= 1
        kept.append(home if stays else None)
    spare: list[str] = []
    while any(room.values()):  # the places left, taken region by region in turn
        for region in regions:
            if room[region] > 0:
                room[region] -= 1
                spare.append(region)
    places = iter(spare)
    return [home if home is not None else next(places) for home in kept]


def shares(total: int, regions: Sequence[str]) -> dict[str, int]:
    """How many of `total` accounts each region homes: an even split, the remainder going to the first regions."""
    n = len(regions)
    return {r: total // n + (1 if i < total % n else 0) for i, r in enumerate(regions)}


def rehoming(have: dict[str, list[str]], regions: Sequence[str]) -> list[tuple[str, str, list[str]]]:
    """The moves (from, to, account ids) that spread the accounts over `regions` the way a fresh setup seeds them,
    so a later restart finds nothing to reseed. A region that is not in `regions` (it is leaving) gives up every
    account; one that has none yet (it just joined) takes its share from the others. `have`: account ids by the
    region they are homed in now."""
    want = shares(sum(len(ids) for ids in have.values()), regions)
    spare = ((source, ident) for source, ids in have.items() for ident in ids[want.get(source, 0):])
    moves: dict[tuple[str, str], list[str]] = {}
    for target in regions:
        for _ in range(want[target] - len(have.get(target, ()))):
            source, ident = next(spare)
            moves.setdefault((source, target), []).append(ident)
    return [(source, target, ids) for (source, target), ids in moves.items()]


def region_order(home: str, regions: Sequence[dict]) -> list[str]:
    """Home first, then the other regions nearest first. Regions without coordinates come after those with them,
    the primary region first among them, then in the order given. Tie-breakers come last whatever their distance:
    they hold copies to vote, not to serve clients."""
    by_name = {r["name"]: r for r in regions}
    origin = (by_name.get(home) or {}).get("location")
    others = [r for r in regions if r["name"] != home]

    def key(item):
        index, r = item
        loc = r.get("location")
        last = bool(r.get("tie_breaker"))
        if origin and loc:
            return (last, 0, distance_km(origin, loc), index)
        return (last, 1, 0 if r.get("primary") else 1, index)

    return [home] + [r["name"] for _, r in sorted(enumerate(others), key=key)]


def usable(n: dict) -> bool:
    """A node a worker can connect to: live, running, on the network and not cut off from the other regions."""
    return (n.get("liveness") == "live" and n.get("runtime") == "running"
            and not n.get("partitioned") and not n.get("isolated"))


def gateway_candidates(home: str, snapshot: dict, rng: random.Random) -> list[tuple[str, str]]:
    """(ip, region) of the nodes a worker homed in `home` should try, in order: its own region's usable nodes in
    random order, then the nearest other region's, and so on. Empty when nothing usable is known."""
    regions = (snapshot or {}).get("regions") or []
    nodes = [n for n in (snapshot or {}).get("nodes") or [] if usable(n)]
    out: list[tuple[str, str]] = []
    for region in region_order(home, regions):
        mine = [n for n in nodes if n.get("region") == region]
        rng.shuffle(mine)
        out += [(n["ip"], region) for n in mine]
    return out

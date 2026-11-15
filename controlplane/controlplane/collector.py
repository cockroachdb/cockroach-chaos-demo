"""Background thread that rebuilds the snapshot every interval."""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Callable

from .docker_ops import DockerError
from .snapshot import assemble_snapshot

log = logging.getLogger(__name__)

DECOMMISSIONED = 2  # liveness membership: 0 active, 1 decommissioning, 2 decommissioned
# A lapsed liveness record is not, by itself, a dead node. When the node that leads CockroachDB's liveness range dies,
# no node can renew its record until the range has a new leader: every record lapses for a few seconds although the
# nodes are up and serving. A node the panel can vouch for (its container runs, it is on the network, no chaos is
# applied to it) keeps counting as live until its record is this many seconds past its expiry.
LIVENESS_GRACE = 8.0
# A look that failed is not a look that found nothing. While the cluster changes shape the status API can go quiet for
# seconds, and a node removed between the two calls of a container listing fails the listing. Reporting "no live node"
# or "no container" then showed an unreachable cluster on the stage and left Payments workers with nowhere to connect.
# What was last seen stands for this many seconds instead.
OBSERVATION_GRACE = 15.0


def _fetch_json(host: str, path: str) -> dict:
    with urllib.request.urlopen(f"http://{host}:8080{path}", timeout=2) as resp:
        return json.load(resp)


def parse_status(nodes: dict, liveness: dict, now: float, *, vouched: frozenset[str] | set[str] = frozenset(),
                 grace: float = LIVENESS_GRACE) -> tuple[list[dict], dict]:
    """Node rows and range health from the HTTP status API. Telemetry over HTTP never shows up as SQL traffic.
    Live means the node's liveness record hasn't expired (what crdb_internal.gossip_nodes.is_live reports); the
    endpoint's 'statuses' also folds in the answering node's own connections, which flap after a restart.
    A node named in `vouched` stays live until its record is `grace` seconds past expiry: see LIVENESS_GRACE."""
    records = {int(r["nodeId"]): r for r in liveness.get("livenesses") or []}
    rows, unavailable, underreplicated = [], 0, 0
    for n in nodes.get("nodes") or []:
        node_id = int(n["desc"]["nodeId"])
        record = records.get(node_id, {})
        if str(record.get("membership", 0)) in (str(DECOMMISSIONED), "DECOMMISSIONED"):
            continue  # gone for good; its name may belong to a new node now
        metrics = [s.get("metrics") or {} for s in n.get("storeStatuses") or []]
        address = n["desc"]["address"]["addressField"]
        expires = int((record.get("expiration") or {}).get("wallTime") or 0) / 1e9
        live = expires > now or (str(address).rsplit(":", 1)[0] in vouched and now - expires < grace)
        rows.append({"node_id": node_id, "address": address, "is_live": live,
                     "ranges": int(sum(m.get("replicas", 0) for m in metrics)),
                     "leases": int(sum(m.get("replicas.leaseholders", 0) for m in metrics))})
        if live:  # a dead node's last report is stale
            unavailable += sum(m.get("ranges.unavailable", 0) for m in metrics)
            underreplicated += sum(m.get("ranges.underreplicated", 0) for m in metrics)
    return rows, {"unavailable": int(unavailable), "underreplicated": int(underreplicated)}


class Collector:
    def __init__(self, docker, provisioner, operations, *, status_host: str, fetch_json: Callable = _fetch_json,
                 interval: float = 1.5, clock: Callable[[], float] = time.time, chaos=None, network: str | None = None,
                 extras: Callable[[], dict] | None = None):
        self._docker = docker
        self._provisioner = provisioner
        self._ops = operations
        self._status_host = status_host
        self._fetch = fetch_json
        self._interval = interval
        self._clock = clock
        self._chaos = chaos
        self._network = network
        self._extras = extras
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._latest = assemble_snapshot(None, [], None, None, None, clock())
        self._listed: tuple[float, list] | None = None  # when the containers were last listed, and the listing
        self._answered: tuple[float, list, dict] | None = None  # when the status API last answered, and what it said

    def latest(self) -> dict:
        with self._lock:
            return self._latest

    def refresh(self) -> dict:
        topo = self._provisioner.topology
        try:
            containers = self._docker.list_nodes()
            self._listed = (self._clock(), containers)
        except DockerError:
            fresh = self._listed and self._clock() - self._listed[0] < OBSERVATION_GRACE
            containers = self._listed[1] if fresh else []
        flags = self._chaos.flags() if self._chaos else None
        sql_nodes, ranges = (None, None)
        if topo is not None:
            hosts = [self._status_host] + [n.ip for n in topo.nodes]
            vouched = self._vouched(topo, containers, flags)
            sql_nodes, ranges = self._query(hosts, vouched)
            if sql_nodes is not None:
                self._answered = (self._clock(), sql_nodes, ranges)
            elif self._answered and self._clock() - self._answered[0] < OBSERVATION_GRACE:
                # Nobody answered. The last answer stands, but only for the nodes the panel itself knows are up.
                _, rows, ranges = self._answered
                sql_nodes = [dict(r, is_live=r["is_live"] and str(r["address"]).rsplit(":", 1)[0] in vouched) for r in rows]
        latency = self._chaos.latency_setting() if (self._chaos and topo) else None
        snap = assemble_snapshot(topo, containers, sql_nodes, ranges, self._ops.snapshot(), self._clock(),
                                 network=self._network, flags=flags, latency=latency)
        if self._extras:
            snap.update(self._extras())
        with self._lock:
            self._latest = snap
        return snap

    def _vouched(self, topo, containers, flags) -> set[str]:
        """Nodes the panel itself knows are up and connected: running, on the cluster network, no chaos applied."""
        troubled = {name for name, f in (flags or {}).items() if f.get("isolated") or f.get("brownout")}
        attached = {c.name for c in containers if c.running and (not self._network or self._network in c.ips)}
        return {n.name for n in topo.nodes if n.name in attached} - troubled

    def _query(self, hosts, vouched=frozenset()):
        for host in hosts:
            try:
                return parse_status(self._fetch(host, "/_status/nodes"), self._fetch(host, "/_admin/v1/liveness"),
                                    self._clock(), vouched=vouched)
            except Exception as e:  # node down or partitioned: try the next one
                log.debug("status query via %s failed: %s", host, e)
        return None, None

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                try:
                    self.refresh()
                except Exception:
                    log.exception("snapshot refresh failed")
                self._stop.wait(self._interval)
        threading.Thread(target=loop, daemon=True, name="collector").start()

    def stop(self) -> None:
        self._stop.set()

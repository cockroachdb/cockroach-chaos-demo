# M1: Control-Plane Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded 5-node compose stack with a control plane that provisions a correctly named multi-region CockroachDB cluster from a preset, fronts it with a generated HAProxy config, survives its own restarts, and shows live state on a minimal stage page.

**Architecture:** Compose runs two fixed services: `control-plane` (FastAPI, single process) and `haproxy`. The control plane reads a preset YAML and validates it into a `Topology`. It creates node containers through the Docker CLI over the mounted socket, using static IPs from one `/24` per region. It then runs `cockroach init` and an idempotent SQL bootstrap (which fixes init.sql failing on v26), renders the HAProxy config, and reloads it. A collector thread builds one snapshot every 1.5s, served at `/api/state` and pushed over SSE to the stage.

**Tech Stack:**
- Python 3.12 in the image (3.14 works for host tests): FastAPI, uvicorn, PyYAML, psycopg 3, pytest
- HAProxy LTS
- CockroachDB v26.3.2 (also v25.4)
- Vanilla ES-module JavaScript, Geist fonts self-hosted
- Bash plus a stdlib-only Python preflight

**Spec:** `docs/superpowers/specs/2026-10-09-chaos-control-plane-design.md`. This plan implements milestone 1 (§16.1) and the parts of §4, §5, §11, §12 and §15 that M1 needs.

## Global Constraints

- Default CockroachDB version: `v26.3.2`, overridable with `CRDB_VERSION`. Must also work on v25.4.
- Image references are fully qualified: `docker.io/cockroachdb/cockroach:<version>` and `docker.io/library/haproxy:lts`. Podman rejects short names in some setups.
- Network name: `crdb-chaos`. Subnet: `${CHAOS_SUBNET:-10.77.0.0/16}` (must be IPv4 /16).
- Region k (1-based) uses `<a>.<b>.k.0/24`. Node m in a region gets `.(10+m)`. Infrastructure containers get dynamic IPs in `<a>.<b>.0.x`.
- Region and zone names: `^[a-z0-9][a-z0-9-]{0,39}$`.
- Nodes per region: 1–12. Total nodes ≤ `MAX_NODES` (default 15).
- Hostname: `<zone>-n<k>`, prefixed with `<provider>-` only on collisions. Maximum 63 characters.
- Node memory default: `--cache=256MiB --max-sql-memory=256MiB`.
- Node labels: `crdb-chaos.node=1`, plus `.cluster`, `.region`, `.zone`, `.provider`, `.ordinal`, `.ip`.
- The HAProxy service and container are named `haproxy`. Ports: 26257 (SQL), 8080 (DB Console), 8404 (stats). Panel: `${PANEL_PORT:-8088}`.
- Databases configured at bootstrap: `defaultdb` (Banko's) and `chaos` (workload and scenarios).
- Survival goal `auto`:
  - `region` if ≥3 regions, ≥5 nodes, and ≥2 nodes in the primary region
  - otherwise `zone` if ≥3 nodes in the primary region
  - otherwise `none`
- Stage copy never contains "docker", "container", "podman" or "localhost".
- The control plane is a single uvicorn process. No multi-worker gunicorn.
- Python dependencies are minimum versions, so the 3.12 image and a 3.14 host both resolve wheels. No npm dependencies.
- Git: commit on local branch `feature/control-plane` only. **Never push.** Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **The pre-redesign stack is still running.** Its containers `crdb-e1a…`, `toxiproxy-*`, `chaos-backend` and its own `haproxy` hold ports 26257/8080/8088/8404. `./run.sh up` must stop before doing anything and say: run `./run.sh down --legacy`. (Task 13)
2. **`CHAOS_SUBNET` overlaps an existing Docker network or VPN.** Preflight must name the conflicting network and suggest another subnet, instead of letting compose fail cryptically. (Task 13)
3. **Preset typos** (string node counts, uppercase names, unknown preset, broken YAML). These must produce one precise sentence. The API returns 400, and a bad `PRESET` at startup marks the operation failed instead of crash-looping the container. (Tasks 3, 10)
4. **The image can't be pulled** (offline booth, mistyped version). The operation must fail with `Could not pull image …`, and `./run.sh up` must print that and exit, not hang. (Tasks 8, 13)
5. **The control plane restarts mid-provision,** leaving half-created nodes behind. The next start must rebuild cleanly, not adopt a half-initialized cluster. (Task 8)

## File Map

```
pyproject.toml                          pytest config (paths, markers)
controlplane/
  Dockerfile                            python:3.12-slim-trixie + docker-cli + iproute2
  requirements.txt / requirements-dev.txt
  controlplane/
    __init__.py
    __main__.py                         wires real collaborators, runs uvicorn on :8088
    settings.py                         Settings.from_env()
    catalog.py                          CatalogRegion, Catalog, load_catalog()
    topology.py                         RegionSpec, NodeSpec, Topology, parse/load/list presets
    state.py                            persisted {"topology", "provisioned"}
    docker_ops.py                       Docker CLI wrapper, node_run_args(), parse_inspect()
    lb.py                               render_haproxy_config(), LoadBalancer.apply()/ensure_config()
    sqlconn.py                          connect() with allow_unsafe_internals
    bootstrap.py                        bootstrap_statements(), run_bootstrap()
    operations.py                       Operation, Operations (one long op at a time)
    provisioner.py                      Provisioner.ensure()/provision()/rebuild()
    snapshot.py                         assemble_snapshot() (pure)
    collector.py                        Collector thread
    app.py                              create_app(), start_cluster()
catalog/regions.yaml                    real regions per provider
presets/{aws,gcp,azure,ibm,multi-cloud,classic,small}.yaml
web/shared/{tokens.css,crl-logo.svg,favicon.ico,fonts/*.woff2}
web/stage/{index.html,stage.css,stage.js}
tools/preflight.py                      host-side checks (stdlib only)
tests/unit/test_*.py                    one file per module
tests/integration/test_stack.py         opt-in end-to-end checks
docker-compose.yml  run.sh  .env.example  README.md (rewritten/updated)
removed: backend/ toxiproxy/ init.sql haproxy.cfg
```

---

### Task 1: Package scaffolding and settings

**Files:**
- Create: `pyproject.toml`
- Create: `controlplane/requirements.txt`
- Create: `controlplane/requirements-dev.txt`
- Create: `controlplane/controlplane/__init__.py`
- Create: `controlplane/controlplane/settings.py`
- Test: `tests/unit/test_settings.py`

**Interfaces:**
- Produces: `Settings` (frozen dataclass) and `Settings.from_env(env: Mapping[str, str] = os.environ) -> Settings`, with fields `crdb_version, preset, subnet, network, max_nodes, presets_dir, catalog_path, data_dir, haproxy_cfg_dir, haproxy_container, web_dir, time_until_store_dead, crdb_license, crdb_org`.

- [ ] **Step 1: Create the project files**

`pyproject.toml`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests/unit"]
pythonpath = ["controlplane", "tools"]
markers = ["integration: needs a running stack started by ./run.sh (set CHAOS_INTEGRATION=1)"]
```

`controlplane/requirements.txt`:
```
fastapi>=0.115
uvicorn>=0.30
PyYAML>=6.0.3
psycopg[binary]>=3.2.10
```

`controlplane/requirements-dev.txt`:
```
-r requirements.txt
pytest>=8
httpx>=0.27
```

`controlplane/controlplane/__init__.py`:
```python
"""CockroachDB chaos demo control plane."""
```

- [ ] **Step 2: Create the virtualenv**

Run: `python3 -m venv .venv && .venv/bin/pip install -q -r controlplane/requirements-dev.txt && .venv/bin/python -c "import fastapi, psycopg, yaml; print('ok')"`
Expected: `ok`. (`.venv` is already in `.gitignore`.)

- [ ] **Step 3: Write the failing test** at `tests/unit/test_settings.py`

```python
from pathlib import Path

from controlplane.settings import Settings


def test_defaults_match_the_spec():
    s = Settings.from_env({})
    assert s.crdb_version == "v26.3.2"
    assert s.preset == "aws"
    assert s.subnet == "10.77.0.0/16"
    assert s.network == "crdb-chaos"
    assert s.max_nodes == 15
    assert s.haproxy_container == "haproxy"
    assert s.time_until_store_dead == "1m15s"
    assert s.data_dir == Path("/data")
    assert s.presets_dir == Path("/app/presets")
    assert s.crdb_license is None and s.crdb_org is None


def test_environment_overrides_and_blank_values_fall_back():
    s = Settings.from_env({
        "CRDB_VERSION": "v25.4.14", "PRESET": "gcp", "MAX_NODES": "9",
        "CHAOS_SUBNET": "  ", "CRDB_LICENSE": "key", "CRDB_ORG": "Acme",
    })
    assert s.crdb_version == "v25.4.14"
    assert s.preset == "gcp"
    assert s.max_nodes == 9
    assert s.subnet == "10.77.0.0/16"
    assert (s.crdb_license, s.crdb_org) == ("key", "Acme")
```

- [ ] **Step 4: Run it to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_settings.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'controlplane.settings'`

- [ ] **Step 5: Implement** `controlplane/controlplane/settings.py`

```python
"""Runtime configuration for the control plane, read once from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    crdb_version: str
    preset: str
    subnet: str
    network: str
    max_nodes: int
    presets_dir: Path
    catalog_path: Path
    data_dir: Path
    haproxy_cfg_dir: Path
    haproxy_container: str
    web_dir: Path
    time_until_store_dead: str
    crdb_license: str | None
    crdb_org: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        def get(name: str, default: str) -> str:
            return env.get(name, "").strip() or default

        return cls(
            crdb_version=get("CRDB_VERSION", "v26.3.2"),
            preset=get("PRESET", "aws"),
            subnet=get("CHAOS_SUBNET", "10.77.0.0/16"),
            network=get("CHAOS_NETWORK", "crdb-chaos"),
            max_nodes=int(get("MAX_NODES", "15")),
            presets_dir=Path(get("PRESETS_DIR", "/app/presets")),
            catalog_path=Path(get("CATALOG_PATH", "/app/catalog/regions.yaml")),
            data_dir=Path(get("DATA_DIR", "/data")),
            haproxy_cfg_dir=Path(get("HAPROXY_CFG_DIR", "/haproxy")),
            haproxy_container=get("HAPROXY_CONTAINER", "haproxy"),
            web_dir=Path(get("WEB_DIR", "/app/web")),
            time_until_store_dead=get("TIME_UNTIL_STORE_DEAD", "1m15s"),
            crdb_license=env.get("CRDB_LICENSE", "").strip() or None,
            crdb_org=env.get("CRDB_ORG", "").strip() or None,
        )
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_settings.py -q`
Expected: `2 passed`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml controlplane/requirements.txt controlplane/requirements-dev.txt controlplane/controlplane/__init__.py controlplane/controlplane/settings.py tests/unit/test_settings.py
git commit -m "Add control-plane package scaffolding and settings" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Region catalog

**Files:**
- Create: `catalog/regions.yaml`
- Create: `controlplane/controlplane/catalog.py`
- Test: `tests/unit/test_catalog.py`

**Interfaces:**
- Produces:
  - `CatalogRegion(provider: str, name: str, label: str, location: tuple[float, float], zones: tuple[str, ...])`
  - `Catalog.lookup(provider, name) -> CatalogRegion | None`
  - `Catalog.providers() -> list[str]`
  - `Catalog.regions_for(provider) -> list[CatalogRegion]`
  - `parse_catalog(data: dict) -> Catalog`
  - `load_catalog(path: Path) -> Catalog`

- [ ] **Step 1: Write the failing test** at `tests/unit/test_catalog.py`

```python
from pathlib import Path

from controlplane.catalog import load_catalog, parse_catalog

ROOT = Path(__file__).resolve().parents[2]


def test_lookup_returns_label_location_and_zones():
    cat = parse_catalog({"providers": {"gcp": {"us-east1": {
        "label": "South Carolina", "location": [33.2, -80.0],
        "zones": ["us-east1-b", "us-east1-c", "us-east1-d"]}}}})
    r = cat.lookup("gcp", "us-east1")
    assert r.label == "South Carolina"
    assert r.location == (33.2, -80.0)
    assert r.zones == ("us-east1-b", "us-east1-c", "us-east1-d")
    assert cat.lookup("gcp", "nowhere1") is None
    assert cat.lookup("aws", "us-east1") is None


def test_shipped_catalog_covers_every_region_used_by_presets():
    cat = load_catalog(ROOT / "catalog" / "regions.yaml")
    assert {"aws", "gcp", "azure", "ibm", "oci"} <= set(cat.providers())
    for provider, region in [
        ("aws", "us-east-1"), ("aws", "us-west-2"), ("aws", "eu-west-1"), ("aws", "us-east-2"),
        ("gcp", "us-east1"), ("gcp", "us-central1"), ("gcp", "europe-west1"),
        ("azure", "eastus"), ("azure", "westus2"), ("azure", "westeurope"),
        ("ibm", "us-south"), ("ibm", "us-east"), ("ibm", "eu-de"),
    ]:
        entry = cat.lookup(provider, region)
        assert entry is not None, (provider, region)
        assert len(entry.zones) >= 2
        lat, lon = entry.location
        assert -90 <= lat <= 90 and -180 <= lon <= 180


def test_regions_for_lists_a_providers_regions_sorted():
    cat = load_catalog(ROOT / "catalog" / "regions.yaml")
    names = [r.name for r in cat.regions_for("azure")]
    assert names == sorted(names) and "eastus" in names
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_catalog.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'controlplane.catalog'`

- [ ] **Step 3: Create** `catalog/regions.yaml`

```yaml
# Real cloud regions: city label, map coordinates [lat, lon] and zone names.
# Presets and the presenter picker use these; unknown regions fall back to naming rules.
providers:
  aws:
    us-east-1: { label: N. Virginia, location: [38.9, -77.4], zones: [us-east-1a, us-east-1b, us-east-1c] }
    us-east-2: { label: Ohio, location: [40.0, -83.0], zones: [us-east-2a, us-east-2b, us-east-2c] }
    us-west-1: { label: N. California, location: [37.4, -121.9], zones: [us-west-1a, us-west-1c] }
    us-west-2: { label: Oregon, location: [45.8, -119.7], zones: [us-west-2a, us-west-2b, us-west-2c] }
    ca-central-1: { label: Canada Central, location: [45.5, -73.6], zones: [ca-central-1a, ca-central-1b, ca-central-1d] }
    eu-west-1: { label: Ireland, location: [53.3, -6.3], zones: [eu-west-1a, eu-west-1b, eu-west-1c] }
    eu-west-2: { label: London, location: [51.5, -0.1], zones: [eu-west-2a, eu-west-2b, eu-west-2c] }
    eu-central-1: { label: Frankfurt, location: [50.1, 8.7], zones: [eu-central-1a, eu-central-1b, eu-central-1c] }
    ap-northeast-1: { label: Tokyo, location: [35.7, 139.7], zones: [ap-northeast-1a, ap-northeast-1c, ap-northeast-1d] }
    ap-southeast-1: { label: Singapore, location: [1.3, 103.8], zones: [ap-southeast-1a, ap-southeast-1b, ap-southeast-1c] }
    ap-southeast-2: { label: Sydney, location: [-33.9, 151.2], zones: [ap-southeast-2a, ap-southeast-2b, ap-southeast-2c] }
    ap-south-1: { label: Mumbai, location: [19.1, 72.9], zones: [ap-south-1a, ap-south-1b, ap-south-1c] }
    sa-east-1: { label: São Paulo, location: [-23.5, -46.6], zones: [sa-east-1a, sa-east-1b, sa-east-1c] }
  gcp:
    us-east1: { label: South Carolina, location: [33.2, -80.0], zones: [us-east1-b, us-east1-c, us-east1-d] }
    us-east4: { label: N. Virginia, location: [39.0, -77.5], zones: [us-east4-a, us-east4-b, us-east4-c] }
    us-central1: { label: Iowa, location: [41.3, -95.9], zones: [us-central1-a, us-central1-b, us-central1-c] }
    us-west1: { label: Oregon, location: [45.6, -121.2], zones: [us-west1-a, us-west1-b, us-west1-c] }
    europe-west1: { label: Belgium, location: [50.4, 3.8], zones: [europe-west1-b, europe-west1-c, europe-west1-d] }
    europe-west2: { label: London, location: [51.5, -0.1], zones: [europe-west2-a, europe-west2-b, europe-west2-c] }
    europe-west3: { label: Frankfurt, location: [50.1, 8.7], zones: [europe-west3-a, europe-west3-b, europe-west3-c] }
    asia-northeast1: { label: Tokyo, location: [35.7, 139.7], zones: [asia-northeast1-a, asia-northeast1-b, asia-northeast1-c] }
    asia-southeast1: { label: Singapore, location: [1.3, 103.8], zones: [asia-southeast1-a, asia-southeast1-b, asia-southeast1-c] }
    australia-southeast1: { label: Sydney, location: [-33.9, 151.2], zones: [australia-southeast1-a, australia-southeast1-b, australia-southeast1-c] }
  azure:
    eastus: { label: Virginia, location: [37.4, -79.8], zones: [eastus-1, eastus-2, eastus-3] }
    eastus2: { label: Virginia, location: [36.7, -78.4], zones: [eastus2-1, eastus2-2, eastus2-3] }
    centralus: { label: Iowa, location: [41.6, -93.6], zones: [centralus-1, centralus-2, centralus-3] }
    westus2: { label: Washington, location: [47.2, -119.9], zones: [westus2-1, westus2-2, westus2-3] }
    westus3: { label: Arizona, location: [33.4, -112.1], zones: [westus3-1, westus3-2, westus3-3] }
    northeurope: { label: Ireland, location: [53.3, -6.3], zones: [northeurope-1, northeurope-2, northeurope-3] }
    westeurope: { label: Netherlands, location: [52.4, 4.9], zones: [westeurope-1, westeurope-2, westeurope-3] }
    uksouth: { label: London, location: [51.5, -0.1], zones: [uksouth-1, uksouth-2, uksouth-3] }
    japaneast: { label: Tokyo, location: [35.7, 139.7], zones: [japaneast-1, japaneast-2, japaneast-3] }
    southeastasia: { label: Singapore, location: [1.3, 103.8], zones: [southeastasia-1, southeastasia-2, southeastasia-3] }
    australiaeast: { label: New South Wales, location: [-33.9, 151.2], zones: [australiaeast-1, australiaeast-2, australiaeast-3] }
  ibm:
    us-south: { label: Dallas, location: [32.8, -96.8], zones: [us-south-1, us-south-2, us-south-3] }
    us-east: { label: Washington DC, location: [38.9, -77.0], zones: [us-east-1, us-east-2, us-east-3] }
    ca-tor: { label: Toronto, location: [43.7, -79.4], zones: [ca-tor-1, ca-tor-2, ca-tor-3] }
    eu-de: { label: Frankfurt, location: [50.1, 8.7], zones: [eu-de-1, eu-de-2, eu-de-3] }
    eu-gb: { label: London, location: [51.5, -0.1], zones: [eu-gb-1, eu-gb-2, eu-gb-3] }
    jp-tok: { label: Tokyo, location: [35.7, 139.7], zones: [jp-tok-1, jp-tok-2, jp-tok-3] }
    au-syd: { label: Sydney, location: [-33.9, 151.2], zones: [au-syd-1, au-syd-2, au-syd-3] }
    br-sao: { label: São Paulo, location: [-23.5, -46.6], zones: [br-sao-1, br-sao-2, br-sao-3] }
  oci:
    us-ashburn-1: { label: Ashburn, location: [39.0, -77.5], zones: [us-ashburn-1-ad1, us-ashburn-1-ad2, us-ashburn-1-ad3] }
    us-phoenix-1: { label: Phoenix, location: [33.4, -112.1], zones: [us-phoenix-1-ad1, us-phoenix-1-ad2, us-phoenix-1-ad3] }
    eu-frankfurt-1: { label: Frankfurt, location: [50.1, 8.7], zones: [eu-frankfurt-1-ad1, eu-frankfurt-1-ad2, eu-frankfurt-1-ad3] }
```

- [ ] **Step 4: Implement** `controlplane/controlplane/catalog.py`

```python
"""Known cloud regions: city label, map coordinates and real zone names."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CatalogRegion:
    provider: str
    name: str
    label: str
    location: tuple[float, float]
    zones: tuple[str, ...]


class Catalog:
    def __init__(self, regions: dict[tuple[str, str], CatalogRegion]):
        self._regions = regions

    def lookup(self, provider: str, name: str) -> CatalogRegion | None:
        return self._regions.get((provider, name))

    def providers(self) -> list[str]:
        return sorted({provider for provider, _ in self._regions})

    def regions_for(self, provider: str) -> list[CatalogRegion]:
        return sorted((r for (p, _), r in self._regions.items() if p == provider), key=lambda r: r.name)


def parse_catalog(data: dict) -> Catalog:
    regions: dict[tuple[str, str], CatalogRegion] = {}
    for provider, entries in (data.get("providers") or {}).items():
        for name, entry in (entries or {}).items():
            lat, lon = entry["location"]
            regions[(provider, name)] = CatalogRegion(
                provider=provider,
                name=name,
                label=str(entry["label"]),
                location=(float(lat), float(lon)),
                zones=tuple(str(z) for z in entry["zones"]),
            )
    return Catalog(regions)


def load_catalog(path: Path) -> Catalog:
    with open(path, encoding="utf-8") as f:
        return parse_catalog(yaml.safe_load(f) or {})
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_catalog.py -q`
Expected: `3 passed`

- [ ] **Step 6: Commit**

```bash
git add catalog/regions.yaml controlplane/controlplane/catalog.py tests/unit/test_catalog.py
git commit -m "Add the cloud region catalog" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Topology and shipped presets

**Files:**
- Create: `controlplane/controlplane/topology.py`
- Create: `presets/aws.yaml`, `presets/gcp.yaml`, `presets/azure.yaml`, `presets/ibm.yaml`, `presets/multi-cloud.yaml`, `presets/classic.yaml`, `presets/small.yaml`
- Test: `tests/unit/test_topology.py`

**Interfaces:**
- Consumes: `Catalog.lookup()` (Task 2)
- Produces:
  - `NAME_RE`, `PresetError(ValueError)`
  - `RegionSpec(name, provider, nodes, label, location: tuple[float, float] | None, zones: tuple[str, ...], primary, secondary, index)`
  - `NodeSpec(name, region, zone, provider, ordinal, ip)`
  - `Topology(preset, display_name, cluster_id, crdb_version, survival_goal, latency, cache, sql_memory, regions, nodes, warnings)`, with properties `.primary`, `.secondary` and `.image`
  - `parse_preset(name, data, catalog, *, default_version, subnet, max_nodes) -> Topology`
  - `load_preset(presets_dir, name, catalog, *, default_version, subnet, max_nodes) -> Topology`
  - `list_presets(presets_dir) -> list[str]`
  - `resolve_survival_goal(requested, regions) -> str`

- [ ] **Step 1: Write the failing tests** at `tests/unit/test_topology.py`

```python
import re
from pathlib import Path

import pytest

from controlplane.catalog import load_catalog
from controlplane.topology import PresetError, list_presets, load_preset, parse_preset

ROOT = Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT / "catalog" / "regions.yaml")
KW = dict(default_version="v26.3.2", subnet="10.77.0.0/16", max_nodes=15)


def parse(data, name="t"):
    return parse_preset(name, data, CATALOG, **KW)


def aws3():
    return {"display_name": "Global Payments", "cluster_id": "global-payments", "regions": [
        {"name": "us-east-1", "provider": "aws", "nodes": 3, "primary": True},
        {"name": "us-west-2", "provider": "aws", "nodes": 3, "secondary": True},
        {"name": "eu-west-1", "provider": "aws", "nodes": 3},
    ]}


def test_nodes_get_cloud_style_names_zones_and_region_subnets():
    t = parse(aws3())
    assert [n.name for n in t.nodes[:3]] == ["us-east-1a-n1", "us-east-1b-n2", "us-east-1c-n3"]
    assert [n.ip for n in t.nodes[:3]] == ["10.77.1.11", "10.77.1.12", "10.77.1.13"]
    assert (t.nodes[3].name, t.nodes[3].ip) == ("us-west-2a-n1", "10.77.2.11")
    assert t.nodes[-1].ip == "10.77.3.13"
    assert t.image == "docker.io/cockroachdb/cockroach:v26.3.2"
    assert t.primary.name == "us-east-1" and t.secondary.name == "us-west-2"
    assert t.regions[0].label == "N. Virginia" and t.regions[0].location == (38.9, -77.4)


def test_zones_round_robin_when_a_region_has_more_nodes_than_zones():
    t = parse({"regions": [
        {"name": "us-east-1", "provider": "aws", "nodes": 4},
        {"name": "us-west-2", "provider": "aws", "nodes": 1},
        {"name": "eu-west-1", "provider": "aws", "nodes": 1},
    ]})
    assert [n.zone for n in t.nodes if n.region == "us-east-1"] == ["us-east-1a", "us-east-1b", "us-east-1c", "us-east-1a"]


def test_catalog_supplies_real_zone_names_and_rules_cover_unknown_regions():
    t = parse({"regions": [
        {"name": "eastus", "provider": "azure", "nodes": 2},
        {"name": "us-east1", "provider": "gcp", "nodes": 1},
        {"name": "dc-frankfurt", "provider": "onprem", "nodes": 1, "location": [50.1, 8.7]},
    ]})
    assert [n.name for n in t.nodes] == ["eastus-1-n1", "eastus-2-n2", "us-east1-b-n1", "dc-frankfurt-a-n1"]
    assert t.regions[2].label == "dc-frankfurt" and t.regions[2].location == (50.1, 8.7)


def test_colliding_hostnames_get_a_provider_prefix():
    t = parse({"regions": [
        {"name": "edge-1", "provider": "custom", "nodes": 1, "zones": ["edge-z"]},
        {"name": "edge-2", "provider": "onprem", "nodes": 1, "zones": ["edge-z"]},
        {"name": "edge-3", "provider": "custom", "nodes": 1},
    ]})
    assert [n.name for n in t.nodes] == ["custom-edge-z-n1", "onprem-edge-z-n1", "edge-3-a-n1"]


@pytest.mark.parametrize("counts, expected", [
    ([3, 3, 3], "region"),
    ([2, 2, 1], "region"),
    ([1, 1, 1], "none"),
    ([3, 1], "zone"),
    ([2, 2], "none"),
])
def test_auto_survival_goal(counts, expected):
    data = {"regions": [{"name": f"r{i}", "provider": "custom", "nodes": n} for i, n in enumerate(counts)]}
    assert parse(data).survival_goal == expected


def test_explicit_goal_that_cannot_be_satisfied_produces_a_warning():
    t = parse({"survival_goal": "zone", "regions": [{"name": f"r{i}", "nodes": 1} for i in range(3)]})
    assert t.survival_goal == "zone"
    assert any("ZONE survival needs 3+ nodes in the primary region" in w for w in t.warnings)


def test_first_region_becomes_primary_when_none_is_marked():
    t = parse({"regions": [{"name": "a1", "nodes": 1}, {"name": "b1", "nodes": 1}]})
    assert t.primary.name == "a1" and t.regions[0].primary


@pytest.mark.parametrize("data, message", [
    ({}, "'regions' must be a non-empty list"),
    ({"regions": [{"name": "US-East-1", "nodes": 1}]}, "lowercase letters, digits and hyphens"),
    ({"regions": [{"name": "a1", "nodes": "3"}]}, "'nodes' between 1 and 12"),
    ({"regions": [{"name": "a1", "nodes": 13}]}, "'nodes' between 1 and 12"),
    ({"regions": [{"name": "a1", "nodes": 1}, {"name": "a1", "nodes": 1}]}, "listed twice"),
    ({"regions": [{"name": "a1", "nodes": 1, "primary": True}, {"name": "b1", "nodes": 1, "primary": True}]}, "only one region can be primary"),
    ({"regions": [{"name": "a1", "nodes": 1, "location": [91, 0]}]}, "location"),
    ({"survival_goal": "planet", "regions": [{"name": "a1", "nodes": 1}]}, "survival_goal"),
    ({"regions": [{"name": f"r{i}", "nodes": 4} for i in range(4)]}, "exceeds MAX_NODES=15"),
    ({"regions": [{"name": "a1", "nodes": 1, "zones": ["Zone A"]}]}, "zone name"),
])
def test_invalid_presets_explain_what_is_wrong(data, message):
    with pytest.raises(PresetError, match=re.escape(message)):
        parse(data)


def test_subnet_must_be_an_ipv4_slash_16():
    with pytest.raises(PresetError, match="IPv4 /16"):
        parse_preset("t", aws3(), CATALOG, default_version="v26.3.2", subnet="10.77.0.0/24", max_nodes=15)


def test_every_shipped_preset_parses_without_warnings():
    names = list_presets(ROOT / "presets")
    assert {"aws", "gcp", "azure", "ibm", "multi-cloud", "classic", "small"} <= set(names)
    for name in names:
        t = load_preset(ROOT / "presets", name, CATALOG, **KW)
        assert t.nodes and not t.warnings, name


def test_shipped_presets_resolve_the_expected_goals():
    goals = {n: load_preset(ROOT / "presets", n, CATALOG, **KW).survival_goal for n in ("aws", "classic", "small")}
    assert goals == {"aws": "region", "classic": "region", "small": "none"}


def test_unknown_preset_and_invalid_yaml(tmp_path):
    with pytest.raises(PresetError, match="unknown preset"):
        load_preset(tmp_path, "nope", CATALOG, **KW)
    (tmp_path / "broken.yaml").write_text("regions: [", encoding="utf-8")
    with pytest.raises(PresetError, match="invalid YAML"):
        load_preset(tmp_path, "broken", CATALOG, **KW)


def test_custom_presets_live_in_the_custom_folder(tmp_path):
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "mine.yaml").write_text("regions:\n  - {name: us-east-1, provider: aws, nodes: 1}\n", encoding="utf-8")
    assert load_preset(tmp_path, "mine", CATALOG, **KW).nodes[0].name == "us-east-1a-n1"
    assert list_presets(tmp_path) == ["mine"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_topology.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'controlplane.topology'`

- [ ] **Step 3: Implement** `controlplane/controlplane/topology.py`

```python
"""Presets -> validated topology: regions, zones, node names and static IPs."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from .catalog import Catalog

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SURVIVAL_GOALS = ("auto", "zone", "region", "none")
MAX_NODES_PER_REGION = 12
DEFAULT_MEMORY = {"cache": "256MiB", "sql": "256MiB"}


class PresetError(ValueError):
    """A preset that cannot become a cluster. The message is shown to the presenter as-is."""


@dataclass(frozen=True)
class RegionSpec:
    name: str
    provider: str
    nodes: int
    label: str
    location: tuple[float, float] | None
    zones: tuple[str, ...]
    primary: bool
    secondary: bool
    index: int  # 1-based; selects the region's /24


@dataclass(frozen=True)
class NodeSpec:
    name: str
    region: str
    zone: str
    provider: str
    ordinal: int  # 1-based within its region
    ip: str


@dataclass(frozen=True)
class Topology:
    preset: str
    display_name: str
    cluster_id: str
    crdb_version: str
    survival_goal: str  # resolved: "region" | "zone" | "none"
    latency: str
    cache: str
    sql_memory: str
    regions: tuple[RegionSpec, ...]
    nodes: tuple[NodeSpec, ...]
    warnings: tuple[str, ...] = ()

    @property
    def primary(self) -> RegionSpec:
        return next(r for r in self.regions if r.primary)

    @property
    def secondary(self) -> RegionSpec | None:
        return next((r for r in self.regions if r.secondary), None)

    @property
    def image(self) -> str:
        return f"docker.io/cockroachdb/cockroach:{self.crdb_version}"


def default_zones(provider: str, region: str, count: int = 3) -> tuple[str, ...]:
    letters = "abcdefgh"
    if provider == "aws":
        return tuple(f"{region}{letters[i]}" for i in range(count))
    if provider in ("azure", "ibm"):
        return tuple(f"{region}-{i + 1}" for i in range(count))
    if provider == "oci":
        return tuple(f"{region}-ad{i + 1}" for i in range(count))
    return tuple(f"{region}-{letters[i]}" for i in range(count))


def resolve_survival_goal(requested: str, regions) -> str:
    if requested != "auto":
        return requested
    total = sum(r.nodes for r in regions)
    primary = next(r for r in regions if r.primary)
    if len(regions) >= 3 and total >= 5 and primary.nodes >= 2:
        return "region"
    if primary.nodes >= 3:
        return "zone"
    return "none"


def survival_warnings(goal: str, regions) -> list[str]:
    total = sum(r.nodes for r in regions)
    primary = next(r for r in regions if r.primary)
    warnings = []
    if goal == "region" and (len(regions) < 3 or total < 5 or primary.nodes < 2):
        warnings.append("REGION survival needs 3+ regions, 5+ nodes and 2+ nodes in the primary region; "
                        "ranges will stay under-replicated")
    if goal == "zone" and primary.nodes < 3:
        warnings.append("ZONE survival needs 3+ nodes in the primary region; ranges will stay under-replicated")
    return warnings


def subnet_prefix(subnet: str) -> str:
    try:
        net = ipaddress.ip_network(subnet)
    except ValueError as e:
        raise PresetError(f"CHAOS_SUBNET {subnet!r} is not a valid network") from e
    if net.version != 4 or net.prefixlen != 16:
        raise PresetError(f"CHAOS_SUBNET must be an IPv4 /16, got {subnet}")
    a, b, _, _ = str(net.network_address).split(".")
    return f"{a}.{b}"


def node_specs(regions, subnet: str) -> tuple[NodeSpec, ...]:
    prefix = subnet_prefix(subnet)
    plan = [(r, r.zones[(k - 1) % len(r.zones)], k) for r in regions for k in range(1, r.nodes + 1)]
    names = [f"{zone}-n{k}" for _, zone, k in plan]
    duplicates = {n for n in names if names.count(n) > 1}
    specs = []
    for (region, zone, k), name in zip(plan, names):
        if name in duplicates:
            name = f"{region.provider}-{name}"
        if len(name) > 63:
            raise PresetError(f"node name {name!r} is longer than 63 characters")
        specs.append(NodeSpec(name=name, region=region.name, zone=zone, provider=region.provider,
                              ordinal=k, ip=f"{prefix}.{region.index}.{10 + k}"))
    return tuple(specs)


def _location(raw, preset: str, region: str) -> tuple[float, float] | None:
    if raw is None:
        return None
    try:
        lat, lon = (float(v) for v in raw)
    except (TypeError, ValueError):
        raise PresetError(f"preset {preset}: region {region} location must be [lat, lon]") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise PresetError(f"preset {preset}: region {region} location {raw} is out of range")
    return (lat, lon)


def parse_preset(name: str, data, catalog: Catalog, *, default_version: str, subnet: str, max_nodes: int) -> Topology:
    if not isinstance(data, dict):
        raise PresetError(f"preset {name}: expected a mapping at the top level")
    raw_regions = data.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise PresetError(f"preset {name}: 'regions' must be a non-empty list")

    seen: set[str] = set()
    regions: list[RegionSpec] = []
    for i, raw in enumerate(raw_regions, start=1):
        if not isinstance(raw, dict):
            raise PresetError(f"preset {name}: region #{i} must be a mapping")
        rname = str(raw.get("name", "")).strip()
        if not NAME_RE.match(rname):
            raise PresetError(f"preset {name}: region name {rname!r} must be lowercase letters, digits and hyphens (max 40)")
        if rname in seen:
            raise PresetError(f"preset {name}: region {rname} is listed twice")
        seen.add(rname)
        provider = str(raw.get("provider", "custom")).strip().lower()
        nodes = raw.get("nodes", 3)
        if not isinstance(nodes, int) or isinstance(nodes, bool) or not 1 <= nodes <= MAX_NODES_PER_REGION:
            raise PresetError(f"preset {name}: region {rname} needs 'nodes' between 1 and {MAX_NODES_PER_REGION}")
        known = catalog.lookup(provider, rname)
        zones = tuple(str(z) for z in raw["zones"]) if raw.get("zones") else (known.zones if known else default_zones(provider, rname))
        for zone in zones:
            if not NAME_RE.match(zone):
                raise PresetError(f"preset {name}: zone name {zone!r} in {rname} must be lowercase letters, digits and hyphens")
        location = _location(raw.get("location"), name, rname) or (known.location if known else None)
        regions.append(RegionSpec(
            name=rname, provider=provider, nodes=nodes,
            label=str(raw.get("label") or (known.label if known else rname)),
            location=location, zones=zones,
            primary=bool(raw.get("primary")), secondary=bool(raw.get("secondary")), index=i,
        ))

    if sum(r.primary for r in regions) > 1:
        raise PresetError(f"preset {name}: only one region can be primary")
    if not any(r.primary for r in regions):
        regions[0] = replace(regions[0], primary=True)
    if sum(r.secondary for r in regions) > 1:
        raise PresetError(f"preset {name}: only one region can be secondary")
    if any(r.primary and r.secondary for r in regions):
        raise PresetError(f"preset {name}: the primary region cannot also be secondary")
    total = sum(r.nodes for r in regions)
    if total > max_nodes:
        raise PresetError(f"preset {name}: {total} nodes exceeds MAX_NODES={max_nodes}")

    requested = str(data.get("survival_goal", "auto")).strip().lower()
    if requested not in SURVIVAL_GOALS:
        raise PresetError(f"preset {name}: survival_goal must be one of {', '.join(SURVIVAL_GOALS)}")
    cluster_id = str(data.get("cluster_id") or name)
    if not NAME_RE.match(cluster_id):
        raise PresetError(f"preset {name}: cluster_id {cluster_id!r} must be lowercase letters, digits and hyphens")
    memory = {**DEFAULT_MEMORY, **(data.get("node_memory") or {})}
    frozen = tuple(regions)
    return Topology(
        preset=name,
        display_name=str(data.get("display_name") or name),
        cluster_id=cluster_id,
        crdb_version=str(data.get("crdb_version") or default_version),
        survival_goal=resolve_survival_goal(requested, frozen),
        latency=str(data.get("latency", "realistic")),
        cache=str(memory["cache"]),
        sql_memory=str(memory["sql"]),
        regions=frozen,
        nodes=node_specs(frozen, subnet),
        warnings=tuple(survival_warnings(requested, frozen)) if requested != "auto" else (),
    )


def load_preset(presets_dir: Path, name: str, catalog: Catalog, *, default_version: str, subnet: str, max_nodes: int) -> Topology:
    if not NAME_RE.match(name):
        raise PresetError(f"unknown preset {name!r}")
    for candidate in (presets_dir / f"{name}.yaml", presets_dir / "custom" / f"{name}.yaml"):
        if candidate.is_file():
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            except yaml.YAMLError as e:
                raise PresetError(f"preset {name}: invalid YAML ({e})") from e
            return parse_preset(name, data, catalog, default_version=default_version, subnet=subnet, max_nodes=max_nodes)
    raise PresetError(f"unknown preset {name!r}")


def list_presets(presets_dir: Path) -> list[str]:
    names = {p.stem for p in presets_dir.glob("*.yaml")}
    names |= {p.stem for p in (presets_dir / "custom").glob("*.yaml")}
    return sorted(names)
```

- [ ] **Step 4: Create the seven presets**

`presets/aws.yaml`:
```yaml
# Three AWS regions, three nodes each. Survives a full region outage.
display_name: Global Payments
cluster_id: global-payments
survival_goal: auto
latency: realistic
regions:
  - { name: us-east-1, provider: aws, nodes: 3, primary: true }
  - { name: us-west-2, provider: aws, nodes: 3, secondary: true }
  - { name: eu-west-1, provider: aws, nodes: 3 }
```

`presets/gcp.yaml`:
```yaml
# Three Google Cloud regions, three nodes each.
display_name: Global Payments
cluster_id: global-payments
survival_goal: auto
latency: realistic
regions:
  - { name: us-east1, provider: gcp, nodes: 3, primary: true }
  - { name: us-central1, provider: gcp, nodes: 3, secondary: true }
  - { name: europe-west1, provider: gcp, nodes: 3 }
```

`presets/azure.yaml`:
```yaml
# Three Azure regions, three nodes each.
display_name: Global Payments
cluster_id: global-payments
survival_goal: auto
latency: realistic
regions:
  - { name: eastus, provider: azure, nodes: 3, primary: true }
  - { name: westus2, provider: azure, nodes: 3, secondary: true }
  - { name: westeurope, provider: azure, nodes: 3 }
```

`presets/ibm.yaml`:
```yaml
# Three IBM Cloud regions, three nodes each.
display_name: Global Payments
cluster_id: global-payments
survival_goal: auto
latency: realistic
regions:
  - { name: us-south, provider: ibm, nodes: 3, primary: true }
  - { name: us-east, provider: ibm, nodes: 3, secondary: true }
  - { name: eu-de, provider: ibm, nodes: 3 }
```

`presets/multi-cloud.yaml`:
```yaml
# One region on each of AWS, Google Cloud and Azure.
display_name: Global Payments
cluster_id: global-payments
survival_goal: auto
latency: realistic
regions:
  - { name: us-east-1, provider: aws, nodes: 3, primary: true }
  - { name: us-central1, provider: gcp, nodes: 3, secondary: true }
  - { name: westus2, provider: azure, nodes: 3 }
```

`presets/classic.yaml`:
```yaml
# The original demo shape (2 + 2 + 1 nodes) with real AWS region names.
display_name: Global Payments
cluster_id: global-payments
survival_goal: auto
latency: realistic
regions:
  - { name: us-east-1, provider: aws, nodes: 2, primary: true }
  - { name: us-west-2, provider: aws, nodes: 2, secondary: true }
  - { name: us-east-2, provider: aws, nodes: 1 }
```

`presets/small.yaml`:
```yaml
# One node per region for laptops with 8 GB. Plain 3x replication still
# survives a region outage; scenarios that need multi-region SQL are unavailable.
display_name: Global Payments
cluster_id: global-payments
survival_goal: auto
latency: realistic
regions:
  - { name: us-east-1, provider: aws, nodes: 1, primary: true }
  - { name: us-west-2, provider: aws, nodes: 1 }
  - { name: eu-west-1, provider: aws, nodes: 1 }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_topology.py -q`
Expected: `26 passed` (including parametrized cases).

- [ ] **Step 6: Commit**

```bash
git add controlplane/controlplane/topology.py presets tests/unit/test_topology.py
git commit -m "Add preset parsing, validation, naming and the shipped presets" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Persisted state

**Files:**
- Create: `controlplane/controlplane/state.py`
- Test: `tests/unit/test_state.py`

**Interfaces:**
- Consumes: `Topology`, `RegionSpec`, `NodeSpec` (Task 3)
- Produces:
  - `topology_to_dict(t: Topology) -> dict`
  - `topology_from_dict(d: dict) -> Topology`
  - `load_state(data_dir: Path) -> dict`
  - `save_state(data_dir: Path, state: dict) -> None`

  The state shape is `{"topology": dict, "provisioned": bool}`.

- [ ] **Step 1: Write the failing test** at `tests/unit/test_state.py`

```python
from pathlib import Path

from controlplane.catalog import load_catalog
from controlplane.state import load_state, save_state, topology_from_dict, topology_to_dict
from controlplane.topology import load_preset

ROOT = Path(__file__).resolve().parents[2]
TOPO = load_preset(ROOT / "presets", "classic", load_catalog(ROOT / "catalog" / "regions.yaml"),
                   default_version="v26.3.2", subnet="10.77.0.0/16", max_nodes=15)


def test_topology_round_trips_through_json_shapes():
    assert topology_from_dict(topology_to_dict(TOPO)) == TOPO


def test_save_then_load(tmp_path: Path):
    save_state(tmp_path, {"topology": topology_to_dict(TOPO), "provisioned": True})
    state = load_state(tmp_path)
    assert state["provisioned"] is True
    assert topology_from_dict(state["topology"]).nodes == TOPO.nodes
    assert not list(tmp_path.glob(".state-*")), "temporary files are cleaned up"


def test_missing_or_corrupt_state_is_empty(tmp_path: Path):
    assert load_state(tmp_path) == {}
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    assert load_state(tmp_path) == {}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_state.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'controlplane.state'`

- [ ] **Step 3: Implement** `controlplane/controlplane/state.py`

```python
"""Persisted control-plane state: the active topology and whether provisioning finished."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .topology import NodeSpec, RegionSpec, Topology

STATE_FILE = "state.json"


def topology_to_dict(t: Topology) -> dict:
    return asdict(t)


def topology_from_dict(d: dict) -> Topology:
    regions = tuple(
        RegionSpec(**{**r, "location": tuple(r["location"]) if r.get("location") else None, "zones": tuple(r["zones"])})
        for r in d["regions"]
    )
    nodes = tuple(NodeSpec(**n) for n in d["nodes"])
    return Topology(**{**d, "regions": regions, "nodes": nodes, "warnings": tuple(d.get("warnings", ()))})


def load_state(data_dir: Path) -> dict:
    path = data_dir / STATE_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(data_dir: Path, state: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, data_dir / STATE_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_state.py -q`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add controlplane/controlplane/state.py tests/unit/test_state.py
git commit -m "Persist the active topology and provisioning status" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Docker CLI wrapper

**Files:**
- Create: `controlplane/controlplane/docker_ops.py`
- Test: `tests/unit/test_docker_ops.py`

**Interfaces:**
- Consumes: `NodeSpec`, `Topology` (Task 3)
- Produces:
  - `NODE_LABEL = "crdb-chaos.node"`, `DockerError(RuntimeError)`
  - `Runner = Callable[[list[str], float], subprocess.CompletedProcess]`, which takes args without the leading `docker` plus a timeout
  - `cli_runner`
  - `ContainerInfo(name, id, running: bool, status: str, labels: dict, ips: dict)`
  - `node_run_args(spec, topo, join: Sequence[str], network: str) -> list[str]`
  - `parse_inspect(raw: str) -> list[ContainerInfo]`
  - `Docker(run: Runner = cli_runner)`, with methods:
    - `.list_nodes() -> list[ContainerInfo]`
    - `.image_present(image) -> bool`
    - `.pull(image) -> None`
    - `.run_node(spec, topo, join, network) -> None`
    - `.remove(name) -> None`
    - `.exec(name, cmd, timeout=60.0, check=True) -> CompletedProcess`
    - `.kill(name, signal) -> None`

- [ ] **Step 1: Write the failing tests** at `tests/unit/test_docker_ops.py`

```python
import json
import subprocess
from pathlib import Path

import pytest

from controlplane.catalog import load_catalog
from controlplane.docker_ops import Docker, DockerError, node_run_args, parse_inspect
from controlplane.topology import load_preset

ROOT = Path(__file__).resolve().parents[2]
TOPO = load_preset(ROOT / "presets", "classic", load_catalog(ROOT / "catalog" / "regions.yaml"),
                   default_version="v26.3.2", subnet="10.77.0.0/16", max_nodes=15)


class FakeRunner:
    def __init__(self, responses):
        self.responses = responses  # list of (returncode, stdout, stderr) consumed in order
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        code, out, err = self.responses.pop(0)
        return subprocess.CompletedProcess(args, code, out, err)


def test_node_run_args_carry_identity_locality_and_limits():
    spec = TOPO.nodes[0]
    args = node_run_args(spec, TOPO, ["us-east-1a-n1:26257", "us-east-1b-n2:26257"], "crdb-chaos")
    assert args[:12] == ["run", "-d", "--name", "us-east-1a-n1", "--hostname", "us-east-1a-n1",
                         "--network", "crdb-chaos", "--ip", "10.77.1.11", "--label", "crdb-chaos.node=1"]
    assert "crdb-chaos.region=us-east-1" in args and "crdb-chaos.ip=10.77.1.11" in args
    i = args.index("docker.io/cockroachdb/cockroach:v26.3.2")
    assert args[i + 1:i + 3] == ["start", "--insecure"]
    assert "--advertise-addr=us-east-1a-n1:26257" in args
    assert "--locality=cloud=aws,region=us-east-1,zone=us-east-1a" in args
    assert "--cluster-name=global-payments" in args
    assert "--join=us-east-1a-n1:26257,us-east-1b-n2:26257" in args
    assert "--cache=256MiB" in args and "--max-sql-memory=256MiB" in args


def test_parse_inspect_handles_leading_slash_and_missing_networks():
    raw = json.dumps([
        {"Name": "/us-east-1a-n1", "Id": "abc", "State": {"Running": True, "Status": "running"},
         "Config": {"Labels": {"crdb-chaos.node": "1"}},
         "NetworkSettings": {"Networks": {"crdb-chaos": {"IPAddress": "10.77.1.11"}}}},
        {"Name": "us-east-1b-n2", "Id": "def", "State": {"Running": False, "Status": "exited"},
         "Config": {"Labels": None}, "NetworkSettings": {}},
    ])
    a, b = parse_inspect(raw)
    assert (a.name, a.running, a.ips) == ("us-east-1a-n1", True, {"crdb-chaos": "10.77.1.11"})
    assert (b.name, b.running, b.status, b.labels, b.ips) == ("us-east-1b-n2", False, "exited", {}, {})


def test_list_nodes_filters_by_label_then_inspects():
    inspect = json.dumps([{"Name": "/n1", "Id": "1", "State": {"Running": True}, "Config": {"Labels": {}}, "NetworkSettings": {"Networks": {}}}])
    run = FakeRunner([(0, "n1\n", ""), (0, inspect, "")])
    nodes = Docker(run).list_nodes()
    assert [n.name for n in nodes] == ["n1"]
    assert run.calls[0] == ["ps", "-a", "--filter", "label=crdb-chaos.node=1", "--format", "{{.Names}}"]
    assert run.calls[1] == ["inspect", "n1"]


def test_list_nodes_with_none_skips_inspect():
    run = FakeRunner([(0, "", "")])
    assert Docker(run).list_nodes() == []
    assert len(run.calls) == 1


def test_failures_raise_with_stderr_and_timeouts_are_reported():
    with pytest.raises(DockerError, match="no such image"):
        Docker(FakeRunner([(1, "", "no such image")])).pull("x")

    def slow(args, timeout):
        raise subprocess.TimeoutExpired(args, timeout)

    with pytest.raises(DockerError, match="timed out"):
        Docker(slow).pull("x")


def test_remove_tolerates_missing_containers_and_image_present_is_boolean():
    d = Docker(FakeRunner([(1, "", "no such container"), (1, "", "missing"), (0, "[]", "")]))
    d.remove("ghost")
    assert d.image_present("a") is False
    assert d.image_present("b") is True
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_docker_ops.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'controlplane.docker_ops'`

- [ ] **Step 3: Implement** `controlplane/controlplane/docker_ops.py`

```python
"""Thin wrapper around the docker CLI. Works against Docker and Podman's Docker-compatible API."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .topology import NodeSpec, Topology

NODE_LABEL = "crdb-chaos.node"

Runner = Callable[[list[str], float], subprocess.CompletedProcess]


class DockerError(RuntimeError):
    """A docker command failed; the message carries the command and its stderr."""


def cli_runner(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    id: str
    running: bool
    status: str
    labels: dict = field(default_factory=dict)
    ips: dict = field(default_factory=dict)  # network name -> IP


def node_run_args(spec: NodeSpec, topo: Topology, join: Sequence[str], network: str) -> list[str]:
    return [
        "run", "-d", "--name", spec.name, "--hostname", spec.name,
        "--network", network, "--ip", spec.ip,
        "--label", f"{NODE_LABEL}=1",
        "--label", f"crdb-chaos.cluster={topo.cluster_id}",
        "--label", f"crdb-chaos.region={spec.region}",
        "--label", f"crdb-chaos.zone={spec.zone}",
        "--label", f"crdb-chaos.provider={spec.provider}",
        "--label", f"crdb-chaos.ordinal={spec.ordinal}",
        "--label", f"crdb-chaos.ip={spec.ip}",
        topo.image, "start", "--insecure",
        f"--advertise-addr={spec.name}:26257",
        "--listen-addr=0.0.0.0:26257",
        "--http-addr=0.0.0.0:8080",
        f"--locality=cloud={spec.provider},region={spec.region},zone={spec.zone}",
        f"--cluster-name={topo.cluster_id}",
        f"--join={','.join(join)}",
        f"--cache={topo.cache}",
        f"--max-sql-memory={topo.sql_memory}",
    ]


def parse_inspect(raw: str) -> list[ContainerInfo]:
    out = []
    for c in json.loads(raw or "[]"):
        state = c.get("State") or {}
        networks = (c.get("NetworkSettings") or {}).get("Networks") or {}
        out.append(ContainerInfo(
            name=str(c.get("Name", "")).lstrip("/"),
            id=str(c.get("Id", "")),
            running=bool(state.get("Running")),
            status=str(state.get("Status", "")),
            labels=dict((c.get("Config") or {}).get("Labels") or {}),
            ips={name: v.get("IPAddress", "") for name, v in networks.items()},
        ))
    return out


class Docker:
    def __init__(self, run: Runner = cli_runner):
        self._run = run

    def _call(self, args: list[str], timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = self._run(list(args), timeout)
        except subprocess.TimeoutExpired as e:
            raise DockerError(f"docker {' '.join(args)} timed out after {timeout:g}s") from e
        if check and proc.returncode != 0:
            raise DockerError(f"docker {' '.join(args)} failed: {(proc.stderr or proc.stdout or '').strip()}")
        return proc

    def list_nodes(self) -> list[ContainerInfo]:
        names = self._call(["ps", "-a", "--filter", f"label={NODE_LABEL}=1", "--format", "{{.Names}}"], timeout=15).stdout.split()
        if not names:
            return []
        return parse_inspect(self._call(["inspect", *names], timeout=15).stdout)

    def image_present(self, image: str) -> bool:
        return self._call(["image", "inspect", image], timeout=15, check=False).returncode == 0

    def pull(self, image: str) -> None:
        self._call(["pull", image], timeout=900)

    def run_node(self, spec: NodeSpec, topo: Topology, join: Sequence[str], network: str) -> None:
        self._call(node_run_args(spec, topo, join, network), timeout=120)

    def remove(self, name: str) -> None:
        self._call(["rm", "-f", name], timeout=60, check=False)

    def exec(self, name: str, cmd: Sequence[str], timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess:
        return self._call(["exec", name, *cmd], timeout=timeout, check=check)

    def kill(self, name: str, signal: str) -> None:
        self._call(["kill", "-s", signal, name], timeout=30)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_docker_ops.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add controlplane/controlplane/docker_ops.py tests/unit/test_docker_ops.py
git commit -m "Add the Docker CLI wrapper for node containers" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: HAProxy config rendering and hot reload

**Files:**
- Create: `controlplane/controlplane/lb.py`
- Test: `tests/unit/test_lb.py`

**Interfaces:**
- Consumes: `NodeSpec` (Task 3); `Docker.exec()`, `Docker.kill()`, `DockerError` (Task 5)
- Produces:
  - `render_haproxy_config(nodes: Sequence[NodeSpec]) -> str`
  - `LoadBalancer(docker, cfg_dir: Path, container: str = "haproxy")`, with:
    - `.apply(nodes) -> None`: writes `haproxy.cfg.next`, validates it when the container is up, swaps it in atomically, and sends USR2 only when HAProxy is PID 1
    - `.ensure_config() -> None`: writes an empty-backend config if none exists

  Inside the HAProxy container the shared volume is mounted at `/cfg`.

- [ ] **Step 1: Write the failing tests** at `tests/unit/test_lb.py`

```python
import subprocess
from pathlib import Path

import pytest

from controlplane.docker_ops import DockerError
from controlplane.lb import LoadBalancer, render_haproxy_config
from controlplane.topology import NodeSpec

NODES = [NodeSpec("us-east-1a-n1", "us-east-1", "us-east-1a", "aws", 1, "10.77.1.11"),
         NodeSpec("us-west-2a-n1", "us-west-2", "us-west-2a", "aws", 1, "10.77.2.11")]


class FakeDocker:
    def __init__(self, container_up=True, pid1="haproxy", valid=True):
        self.container_up, self.pid1, self.valid = container_up, pid1, valid
        self.execs, self.kills = [], []

    def exec(self, name, cmd, timeout=60.0, check=True):
        self.execs.append(cmd)
        if not self.container_up:
            raise DockerError("container not running")
        if cmd[0] == "cat":
            return subprocess.CompletedProcess(cmd, 0, self.pid1 + "\n", "")
        if cmd[0] == "haproxy" and not self.valid:
            raise DockerError("config invalid")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def kill(self, name, signal):
        self.kills.append((name, signal))


def test_config_routes_sql_and_console_to_every_node():
    cfg = render_haproxy_config(NODES)
    assert "server us-east-1a-n1 10.77.1.11:26257" in cfg
    assert "server us-west-2a-n1 10.77.2.11:8080" in cfg
    assert "option pgsql-check user root" in cfg
    assert "on-marked-down shutdown-sessions" in cfg
    assert "balance first" in cfg and "option httpchk GET /health" in cfg
    assert "hard-stop-after 10m" in cfg
    for port in ("*:26257", "*:8080", "*:8404"):
        assert f"bind {port}" in cfg


def test_empty_topology_still_renders_all_frontends_without_servers():
    cfg = render_haproxy_config([])
    assert "backend crdb_sql_nodes" in cfg and "backend crdb_console_nodes" in cfg
    assert "  server " not in cfg


def test_apply_validates_swaps_and_reloads(tmp_path: Path):
    d = FakeDocker()
    LoadBalancer(d, tmp_path).apply(NODES)
    assert "10.77.1.11:26257" in (tmp_path / "haproxy.cfg").read_text()
    assert not (tmp_path / "haproxy.cfg.next").exists()
    assert ["haproxy", "-c", "-q", "-f", "/cfg/haproxy.cfg.next"] in d.execs
    assert d.kills == [("haproxy", "USR2")]


def test_apply_does_not_signal_while_haproxy_is_still_waiting_for_its_first_config(tmp_path: Path):
    d = FakeDocker(pid1="sh")
    LoadBalancer(d, tmp_path).apply(NODES)
    assert (tmp_path / "haproxy.cfg").exists()
    assert d.kills == []


def test_apply_writes_without_validating_when_haproxy_is_down(tmp_path: Path):
    d = FakeDocker(container_up=False)
    LoadBalancer(d, tmp_path).apply(NODES)
    assert (tmp_path / "haproxy.cfg").exists() and d.kills == []


def test_invalid_config_is_rejected_and_the_live_one_is_kept(tmp_path: Path):
    (tmp_path / "haproxy.cfg").write_text("live", encoding="utf-8")
    with pytest.raises(DockerError):
        LoadBalancer(FakeDocker(valid=False), tmp_path).apply(NODES)
    assert (tmp_path / "haproxy.cfg").read_text() == "live"


def test_ensure_config_only_writes_when_missing(tmp_path: Path):
    lb = LoadBalancer(FakeDocker(container_up=False), tmp_path)
    lb.ensure_config()
    first = (tmp_path / "haproxy.cfg").read_text()
    assert "backend crdb_sql_nodes" in first
    (tmp_path / "haproxy.cfg").write_text("keep", encoding="utf-8")
    lb.ensure_config()
    assert (tmp_path / "haproxy.cfg").read_text() == "keep"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_lb.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'controlplane.lb'`

- [ ] **Step 3: Implement** `controlplane/controlplane/lb.py`

```python
"""Render and hot-reload the HAProxy config that fronts every node."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from .docker_ops import DockerError
from .topology import NodeSpec

CONFIG_NAME = "haproxy.cfg"
CONTAINER_CFG_DIR = "/cfg"


def render_haproxy_config(nodes: Sequence[NodeSpec]) -> str:
    sql = "".join(f"  server {n.name} {n.ip}:26257\n" for n in nodes)
    console = "".join(f"  server {n.name} {n.ip}:8080\n" for n in nodes)
    return f"""# Generated by the control plane on every membership change. Do not edit.
global
  maxconn 4096
  log stdout format raw local0
  hard-stop-after 10m

defaults
  mode tcp
  log global
  timeout connect 3s
  # 30m so long-lived SQL connections (changefeeds, long transactions) aren't cut mid-stream.
  timeout client 30m
  timeout server 30m

frontend crdb_sql
  bind *:26257
  default_backend crdb_sql_nodes

backend crdb_sql_nodes
  balance roundrobin
  option pgsql-check user root
  default-server check inter 2000 rise 2 fall 2 on-marked-down shutdown-sessions
{sql}
frontend crdb_console
  bind *:8080
  mode http
  default_backend crdb_console_nodes

backend crdb_console_nodes
  mode http
  balance first
  option httpchk GET /health
  default-server check inter 2000 rise 2 fall 2
{console}
frontend stats
  bind *:8404
  mode http
  stats enable
  stats uri /stats
  stats refresh 2s
  stats show-legends
"""


class LoadBalancer:
    def __init__(self, docker, cfg_dir: Path, container: str = "haproxy"):
        self._docker = docker
        self._dir = cfg_dir
        self._container = container

    def ensure_config(self) -> None:
        """Give HAProxy something to start with before the first cluster exists."""
        if not (self._dir / CONFIG_NAME).exists():
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / CONFIG_NAME).write_text(render_haproxy_config([]), encoding="utf-8")

    def apply(self, nodes: Sequence[NodeSpec]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        staged = self._dir / f"{CONFIG_NAME}.next"
        staged.write_text(render_haproxy_config(nodes), encoding="utf-8")
        pid1 = self._pid1()
        if pid1 is not None:
            try:
                self._docker.exec(self._container, ["haproxy", "-c", "-q", "-f", f"{CONTAINER_CFG_DIR}/{CONFIG_NAME}.next"], timeout=30)
            except DockerError:
                staged.unlink(missing_ok=True)
                raise
        os.replace(staged, self._dir / CONFIG_NAME)
        # Before HAProxy execs, PID 1 is the shell waiting for the file; USR2 would kill it.
        if pid1 == "haproxy":
            self._docker.kill(self._container, "USR2")

    def _pid1(self) -> str | None:
        """PID 1's name inside the HAProxy container, or None when the container isn't running."""
        try:
            proc = self._docker.exec(self._container, ["cat", "/proc/1/comm"], timeout=10, check=False)
        except DockerError:
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_lb.py -q`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add controlplane/controlplane/lb.py tests/unit/test_lb.py
git commit -m "Generate and hot-reload the HAProxy config" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: SQL connections and bootstrap (fixes init.sql on v26)

**Files:**
- Create: `controlplane/controlplane/sqlconn.py`
- Create: `controlplane/controlplane/bootstrap.py`
- Test: `tests/unit/test_sqlconn.py`
- Test: `tests/unit/test_bootstrap.py`

**Interfaces:**
- Consumes: `Topology`, `NAME_RE` (Task 3)
- Produces:
  - `connect(host: str, port: int = 26257, *, dbname="defaultdb", timeout: int = 5, _connect=None) -> psycopg.Connection`. The connection is autocommit and passes `options="-c allow_unsafe_internals=on"`, retrying without the option only if the error mentions it.
  - `Statement(sql: str, optional: bool = False)`
  - `BootstrapError(RuntimeError)`, carrying `.statement`
  - `bootstrap_statements(topo, *, time_until_store_dead: str, license: str | None = None, org: str | None = None) -> list[Statement]`
  - `run_bootstrap(conn, statements, log=print) -> list[str]`, which returns warnings

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_sqlconn.py`:
```python
import psycopg
import pytest

from controlplane.sqlconn import INTERNALS_OPTION, connect


def test_internals_are_requested_on_every_connection():
    calls = []
    conn = connect("10.77.1.11", _connect=lambda **kw: calls.append(kw) or "conn")
    assert conn == "conn"
    assert calls[0]["options"] == INTERNALS_OPTION
    assert calls[0]["autocommit"] is True and calls[0]["host"] == "10.77.1.11"


def test_servers_that_reject_the_option_get_a_plain_retry():
    calls = []

    def fake(**kw):
        calls.append(kw)
        if "options" in kw:
            raise psycopg.OperationalError('unrecognized configuration parameter "allow_unsafe_internals"')
        return "plain"

    assert connect("h", _connect=fake) == "plain"
    assert "options" not in calls[1]


def test_other_connection_errors_propagate():
    def fake(**kw):
        raise psycopg.OperationalError("connection refused")

    with pytest.raises(psycopg.OperationalError, match="refused"):
        connect("h", _connect=fake)
```

`tests/unit/test_bootstrap.py`:
```python
from pathlib import Path

import pytest

from controlplane.bootstrap import BootstrapError, Statement, bootstrap_statements, run_bootstrap
from controlplane.catalog import load_catalog
from controlplane.topology import load_preset

ROOT = Path(__file__).resolve().parents[2]
CAT = load_catalog(ROOT / "catalog" / "regions.yaml")
KW = dict(default_version="v26.3.2", subnet="10.77.0.0/16", max_nodes=15)


def topo(name):
    return load_preset(ROOT / "presets", name, CAT, **KW)


def sqls(statements):
    return [s.sql for s in statements]


def test_region_survival_configures_both_databases_in_order():
    s = sqls(bootstrap_statements(topo("aws"), time_until_store_dead="1m15s"))
    assert s[0] == "SET CLUSTER SETTING server.time_until_store_dead = '1m15s'"
    assert s[1] == "SET CLUSTER SETTING kv.rangefeed.enabled = true"
    assert s[2].startswith('UPSERT INTO system.locations ("localityKey", "localityValue", latitude, longitude) VALUES ')
    assert "('region', 'us-east-1', 38.9, -77.4)" in s[2] and "('region', 'eu-west-1', 53.3, -6.3)" in s[2]
    assert s[3] == "CREATE DATABASE IF NOT EXISTS chaos"
    for db in ("defaultdb", "chaos"):
        block = [x for x in s if x.startswith(f"ALTER DATABASE {db} ")]
        assert block == [
            f'ALTER DATABASE {db} SET PRIMARY REGION "us-east-1"',
            f'ALTER DATABASE {db} ADD REGION IF NOT EXISTS "us-west-2"',
            f'ALTER DATABASE {db} ADD REGION IF NOT EXISTS "eu-west-1"',
            f'ALTER DATABASE {db} SET SECONDARY REGION "us-west-2"',
            f"ALTER DATABASE {db} SURVIVE REGION FAILURE",
        ]


def test_store_dead_setting_is_optional_because_minimums_vary_by_version():
    first = bootstrap_statements(topo("aws"), time_until_store_dead="1m15s")[0]
    assert first.optional is True


def test_no_survival_goal_skips_multi_region_but_keeps_locations():
    s = sqls(bootstrap_statements(topo("small"), time_until_store_dead="1m15s"))
    assert not [x for x in s if x.startswith("ALTER DATABASE")]
    assert any(x.startswith("UPSERT INTO system.locations") for x in s)
    assert "CREATE DATABASE IF NOT EXISTS chaos" in s


def test_zone_survival_has_no_secondary_region(tmp_path):
    (tmp_path / "z.yaml").write_text("regions:\n  - {name: us-east-1, provider: aws, nodes: 3, primary: true}\n"
                                     "  - {name: us-west-2, provider: aws, nodes: 1, secondary: true}\n", encoding="utf-8")
    s = sqls(bootstrap_statements(load_preset(tmp_path, "z", CAT, **KW), time_until_store_dead="1m15s"))
    assert "ALTER DATABASE defaultdb SURVIVE ZONE FAILURE" in s
    assert not [x for x in s if "SECONDARY" in x]


def test_license_comes_first_with_quotes_escaped():
    s = sqls(bootstrap_statements(topo("aws"), time_until_store_dead="1m15s", license="crl-0-abc", org="O'Brien Labs"))
    assert s[0] == "SET CLUSTER SETTING cluster.organization = 'O''Brien Labs'"
    assert s[1] == "SET CLUSTER SETTING enterprise.license = 'crl-0-abc'"


class FakeCursor:
    def __init__(self, fail_on=()):
        self.executed, self.fail_on = [], fail_on

    def execute(self, sql):
        self.executed.append(sql)
        if sql in self.fail_on:
            raise RuntimeError("nope")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_optional_failures_become_warnings_and_required_failures_stop():
    cur = FakeCursor(fail_on={"A"})
    warnings = run_bootstrap(FakeConn(cur), [Statement("A", optional=True), Statement("B")], log=lambda m: None)
    assert cur.executed == ["A", "B"] and warnings and "A" in warnings[0]
    with pytest.raises(BootstrapError) as exc:
        run_bootstrap(FakeConn(FakeCursor(fail_on={"B"})), [Statement("B"), Statement("C")], log=lambda m: None)
    assert exc.value.statement.sql == "B"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_sqlconn.py tests/unit/test_bootstrap.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'controlplane.sqlconn'` (and `controlplane.bootstrap`)

- [ ] **Step 3: Implement** `controlplane/controlplane/sqlconn.py`

```python
"""Connections to CockroachDB with internal tables enabled on v26+."""
from __future__ import annotations

from typing import Callable

import psycopg

INTERNALS_OPTION = "-c allow_unsafe_internals=on"


def connect(host: str, port: int = 26257, *, dbname: str = "defaultdb", timeout: int = 5,
            _connect: Callable[..., object] | None = None):
    """Autocommit connection as root. v26 gates crdb_internal and system behind
    allow_unsafe_internals; v26 ignores unknown startup options, but retry without
    it if a server ever rejects the variable by name."""
    do_connect = _connect or psycopg.connect
    kwargs = dict(host=host, port=port, user="root", dbname=dbname, connect_timeout=timeout,
                  autocommit=True, sslmode="disable")
    try:
        return do_connect(**kwargs, options=INTERNALS_OPTION)
    except psycopg.Error as e:
        if "allow_unsafe_internals" not in str(e):
            raise
        return do_connect(**kwargs)
```

- [ ] **Step 4: Implement** `controlplane/controlplane/bootstrap.py`

```python
"""Cluster bootstrap: settings, map locations and multi-region databases (replaces init.sql)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .topology import NAME_RE, Topology

DATABASES = ("defaultdb", "chaos")


@dataclass(frozen=True)
class Statement:
    sql: str
    optional: bool = False  # failures become warnings instead of stopping bootstrap


class BootstrapError(RuntimeError):
    def __init__(self, statement: Statement, cause: Exception):
        super().__init__(f"bootstrap failed on: {statement.sql} ({cause})")
        self.statement = statement
        self.cause = cause


def _ident(name: str) -> str:
    if not NAME_RE.match(name):
        raise ValueError(f"unsafe identifier {name!r}")
    return f'"{name}"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def bootstrap_statements(topo: Topology, *, time_until_store_dead: str,
                         license: str | None = None, org: str | None = None) -> list[Statement]:
    out: list[Statement] = []
    if license and org:
        out.append(Statement(f"SET CLUSTER SETTING cluster.organization = {_literal(org)}"))
        out.append(Statement(f"SET CLUSTER SETTING enterprise.license = {_literal(license)}"))
    out.append(Statement(f"SET CLUSTER SETTING server.time_until_store_dead = {_literal(time_until_store_dead)}", optional=True))
    out.append(Statement("SET CLUSTER SETTING kv.rangefeed.enabled = true"))
    located = [r for r in topo.regions if r.location]
    if located:
        values = ", ".join(f"('region', {_literal(r.name)}, {r.location[0]}, {r.location[1]})" for r in located)
        out.append(Statement(f'UPSERT INTO system.locations ("localityKey", "localityValue", latitude, longitude) VALUES {values}'))
    out.append(Statement("CREATE DATABASE IF NOT EXISTS chaos"))
    if topo.survival_goal != "none":
        for db in DATABASES:
            out.append(Statement(f"ALTER DATABASE {db} SET PRIMARY REGION {_ident(topo.primary.name)}"))
            for region in topo.regions:
                if not region.primary:
                    out.append(Statement(f"ALTER DATABASE {db} ADD REGION IF NOT EXISTS {_ident(region.name)}"))
            if topo.survival_goal == "region" and topo.secondary:
                out.append(Statement(f"ALTER DATABASE {db} SET SECONDARY REGION {_ident(topo.secondary.name)}"))
            out.append(Statement(f"ALTER DATABASE {db} SURVIVE {topo.survival_goal.upper()} FAILURE"))
    return out


def run_bootstrap(conn, statements: Iterable[Statement], log: Callable[[str], None] = print) -> list[str]:
    """Execute in order. Required failures raise BootstrapError; optional ones come back as warnings."""
    warnings: list[str] = []
    with conn.cursor() as cur:
        for st in statements:
            try:
                cur.execute(st.sql)
            except Exception as e:  # driver errors; kept generic so this module stays testable without a server
                if not st.optional:
                    raise BootstrapError(st, e) from e
                warnings.append(f"{st.sql}: {e}")
                log(f"bootstrap warning: {st.sql}: {e}")
    return warnings
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_sqlconn.py tests/unit/test_bootstrap.py -q`
Expected: `9 passed`

- [ ] **Step 6: Commit**

```bash
git add controlplane/controlplane/sqlconn.py controlplane/controlplane/bootstrap.py tests/unit/test_sqlconn.py tests/unit/test_bootstrap.py
git commit -m "Bootstrap the cluster from the topology, fixing init.sql on v26" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Operations and the provisioner

**Files:**
- Create: `controlplane/controlplane/operations.py`
- Create: `controlplane/controlplane/provisioner.py`
- Test: `tests/unit/test_provisioner.py`

**Interfaces:**
- Consumes:
  - `Docker` methods (Task 5)
  - `LoadBalancer.apply()` (Task 6)
  - `bootstrap_statements`, `run_bootstrap` (Task 7)
  - `load_state`, `save_state`, `topology_to_dict`, `topology_from_dict` (Task 4)
  - `Topology` (Task 3)
  - `Settings` fields `data_dir`, `network`, `time_until_store_dead`, `crdb_license`, `crdb_org` (Task 1)
- Produces:
  - `Operation(id, kind, status, steps, error, started_at, finished_at)` with `.to_dict()`
  - `OperationBusy(RuntimeError)`
  - `Operations`, with methods:
    - `.start(kind) -> Operation` (raises `OperationBusy`)
    - `.step(op, text)`
    - `.finish(op, error=None)`
    - `.snapshot() -> dict | None`
  - `ProvisionError(RuntimeError)`
  - `Provisioner(docker, lb, operations, settings, *, connect, sleep=time.sleep)`, with `.topology: Topology | None`, `.ensure(topo)`, `.provision(topo, kind="provision")` and `.rebuild(topo, kind="rebuild")`. All three run synchronously, never raise, and record failures on the operation.

- [ ] **Step 1: Write the failing tests** at `tests/unit/test_provisioner.py`

```python
import subprocess
from pathlib import Path

import pytest

from controlplane.catalog import load_catalog
from controlplane.docker_ops import ContainerInfo, DockerError
from controlplane.operations import OperationBusy, Operations
from controlplane.provisioner import Provisioner
from controlplane.settings import Settings
from controlplane.state import load_state, save_state, topology_to_dict
from controlplane.topology import load_preset

ROOT = Path(__file__).resolve().parents[2]
CAT = load_catalog(ROOT / "catalog" / "regions.yaml")
KW = dict(default_version="v26.3.2", subnet="10.77.0.0/16", max_nodes=15)
CLASSIC = load_preset(ROOT / "presets", "classic", CAT, **KW)
SMALL = load_preset(ROOT / "presets", "small", CAT, **KW)


class FakeDocker:
    def __init__(self, existing=(), image=True, pull_error=None, init_results=None):
        self.existing = list(existing)
        self.image = image
        self.pull_error = pull_error
        self.init_results = list(init_results or [(0, "Cluster successfully initialized", "")])
        self.pulled, self.ran, self.removed, self.execs = [], [], [], []

    def list_nodes(self):
        return list(self.existing)

    def image_present(self, image):
        return self.image

    def pull(self, image):
        if self.pull_error:
            raise DockerError(self.pull_error)
        self.pulled.append(image)

    def run_node(self, spec, topo, join, network):
        self.ran.append((spec.name, tuple(join), network))
        self.existing.append(ContainerInfo(spec.name, spec.name, True, "running", {}, {}))

    def remove(self, name):
        self.removed.append(name)
        self.existing = [c for c in self.existing if c.name != name]

    def exec(self, name, cmd, timeout=60.0, check=True):
        self.execs.append((name, list(cmd)))
        code, out, err = self.init_results.pop(0) if len(self.init_results) > 1 else self.init_results[0]
        return subprocess.CompletedProcess(cmd, code, out, err)


class FakeLB:
    def __init__(self):
        self.applied = []

    def apply(self, nodes):
        self.applied.append([n.name for n in nodes])

    def ensure_config(self):
        pass


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def execute(self, sql):
        self.db.executed.append(sql)
        self._last = sql

    def fetchall(self):
        if "SHOW REGIONS FROM CLUSTER" in self._last:
            self.db.region_polls += 1
            return [(r,) for r in self.db.regions_by_poll(self.db.region_polls)]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDB:
    def __init__(self, regions_by_poll):
        self.executed, self.region_polls, self.regions_by_poll = [], 0, regions_by_poll

    def connect(self, host, **kw):
        db = self

        class Conn:
            def cursor(self):
                return FakeCursor(db)

            def close(self):
                pass

        return Conn()


def make(tmp_path, docker, topo=CLASSIC, regions=None):
    settings = Settings.from_env({"DATA_DIR": str(tmp_path)})
    db = FakeDB(regions or (lambda poll: [r.name for r in topo.regions]))
    p = Provisioner(docker, FakeLB(), Operations(), settings, connect=db.connect, sleep=lambda s: None)
    return p, db


def test_provision_creates_every_node_inits_bootstraps_and_routes(tmp_path):
    docker = FakeDocker(image=False)
    p, db = make(tmp_path, docker)
    p.provision(CLASSIC)
    op = p._ops.snapshot()
    assert op["status"] == "done", op
    assert docker.pulled == ["docker.io/cockroachdb/cockroach:v26.3.2"]
    assert [r[0] for r in docker.ran] == [n.name for n in CLASSIC.nodes]
    assert docker.ran[0][1] == ("us-east-1a-n1:26257", "us-east-1b-n2:26257", "us-west-2a-n1:26257")
    init = docker.execs[0][1]
    assert init[:2] == ["/cockroach/cockroach", "init"] and "--cluster-name=global-payments" in init
    assert 'ALTER DATABASE defaultdb SET PRIMARY REGION "us-east-1"' in db.executed
    assert p._lb.applied[-1] == [n.name for n in CLASSIC.nodes]
    assert load_state(tmp_path)["provisioned"] is True
    assert "Allocating capacity in us-east-1" in op["steps"]
    assert not [s for s in op["steps"] if any(w in s.lower() for w in ("docker", "container", "podman", "localhost"))]


def test_init_retries_and_treats_already_initialized_as_success(tmp_path):
    docker = FakeDocker(init_results=[(1, "", "connection refused"), (1, "", "connection refused"),
                                      (1, "", "ERROR: cluster has already been initialized")])
    p, _ = make(tmp_path, docker)
    p.provision(CLASSIC)
    assert p._ops.snapshot()["status"] == "done"
    assert len(docker.execs) == 3


def test_pull_failure_fails_the_operation_with_a_clear_message(tmp_path):
    p, _ = make(tmp_path, FakeDocker(image=False, pull_error="manifest unknown"))
    p.provision(CLASSIC)
    op = p._ops.snapshot()
    assert op["status"] == "failed"
    assert op["error"].startswith("Could not pull image docker.io/cockroachdb/cockroach:v26.3.2")


def test_provisioning_waits_for_every_region_to_join(tmp_path):
    names = [r.name for r in CLASSIC.regions]
    p, db = make(tmp_path, FakeDocker(), regions=lambda poll: names[:2] if poll < 3 else names)
    p.provision(CLASSIC)
    assert p._ops.snapshot()["status"] == "done" and db.region_polls == 3


def test_regions_that_never_join_fail_with_their_names(tmp_path):
    p, _ = make(tmp_path, FakeDocker(), regions=lambda poll: ["us-east-1"])
    p.provision(CLASSIC)
    op = p._ops.snapshot()
    assert op["status"] == "failed" and "us-west-2" in op["error"] and "us-east-2" in op["error"]


def test_ensure_adopts_a_finished_cluster_without_recreating_nodes(tmp_path):
    existing = [ContainerInfo(n.name, n.name, True, "running", {}, {}) for n in CLASSIC.nodes]
    docker = FakeDocker(existing=existing)
    save_state(tmp_path, {"topology": topology_to_dict(CLASSIC), "provisioned": True})
    p, _ = make(tmp_path, docker)
    p.ensure(SMALL)
    assert docker.ran == [] and docker.removed == []
    assert p.topology == CLASSIC
    assert p._lb.applied == [[n.name for n in CLASSIC.nodes]]
    assert p._ops.snapshot()["kind"] == "adopt"


def test_ensure_rebuilds_a_half_built_cluster_instead_of_adopting_it(tmp_path):
    docker = FakeDocker(existing=[ContainerInfo("us-east-1a-n1", "x", True, "running", {}, {})])
    save_state(tmp_path, {"topology": topology_to_dict(CLASSIC), "provisioned": False})
    p, _ = make(tmp_path, docker, topo=SMALL)
    p.ensure(SMALL)
    assert docker.removed == ["us-east-1a-n1"]
    assert [r[0] for r in docker.ran] == [n.name for n in SMALL.nodes]
    assert p._ops.snapshot()["status"] == "done"


def test_ensure_provisions_when_nothing_exists(tmp_path):
    docker = FakeDocker()
    p, _ = make(tmp_path, docker, topo=SMALL)
    p.ensure(SMALL)
    assert [r[0] for r in docker.ran] == [n.name for n in SMALL.nodes]


def test_rebuild_drains_the_load_balancer_before_removing_nodes(tmp_path):
    existing = [ContainerInfo(n.name, n.name, True, "running", {}, {}) for n in CLASSIC.nodes]
    docker = FakeDocker(existing=existing)
    p, _ = make(tmp_path, docker, topo=SMALL)
    p.rebuild(SMALL)
    assert p._lb.applied[0] == []
    assert sorted(docker.removed) == sorted(n.name for n in CLASSIC.nodes)
    assert p._lb.applied[-1] == [n.name for n in SMALL.nodes]
    assert p.topology == SMALL


def test_only_one_operation_runs_at_a_time():
    ops = Operations()
    op = ops.start("provision")
    with pytest.raises(OperationBusy):
        ops.start("rebuild")
    ops.finish(op)
    assert ops.start("rebuild").kind == "rebuild"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_provisioner.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'controlplane.operations'`

- [ ] **Step 3: Implement** `controlplane/controlplane/operations.py`

```python
"""Tracks the one long-running cluster operation (provision, rebuild, adopt) for the stage and API."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Operation:
    id: str
    kind: str
    status: str = "running"  # running | done | failed
    steps: list[str] = field(default_factory=list)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status, "steps": list(self.steps),
                "error": self.error, "started_at": self.started_at, "finished_at": self.finished_at}


class OperationBusy(RuntimeError):
    """Another operation is still running."""


class Operations:
    def __init__(self):
        self._lock = threading.Lock()
        self._current: Operation | None = None

    def start(self, kind: str) -> Operation:
        with self._lock:
            if self._current and self._current.status == "running":
                raise OperationBusy(f"{self._current.kind} already running")
            self._current = Operation(id=uuid.uuid4().hex[:8], kind=kind)
            return self._current

    def step(self, op: Operation, text: str) -> None:
        with self._lock:
            op.steps.append(text)

    def finish(self, op: Operation, error: str | None = None) -> None:
        with self._lock:
            op.status = "failed" if error else "done"
            op.error = error
            op.finished_at = time.time()

    def snapshot(self) -> dict | None:
        with self._lock:
            return self._current.to_dict() if self._current else None
```

- [ ] **Step 4: Implement** `controlplane/controlplane/provisioner.py`

```python
"""Creates, adopts and rebuilds the CockroachDB nodes described by a topology."""
from __future__ import annotations

import logging
import time
from typing import Callable

from .bootstrap import bootstrap_statements, run_bootstrap
from .docker_ops import DockerError
from .operations import Operation, Operations
from .state import load_state, save_state, topology_from_dict, topology_to_dict
from .topology import NodeSpec, Topology

log = logging.getLogger(__name__)

INIT_ATTEMPTS = 90
SQL_ATTEMPTS = 60
REGION_ATTEMPTS = 90


class ProvisionError(RuntimeError):
    """Provisioning cannot continue; the message is shown to the presenter."""


class Provisioner:
    def __init__(self, docker, lb, operations: Operations, settings, *, connect: Callable,
                 sleep: Callable[[float], None] = time.sleep):
        self._docker = docker
        self._lb = lb
        self._ops = operations
        self._settings = settings
        self._connect = connect
        self._sleep = sleep
        self.topology: Topology | None = None

    # -- entry points: synchronous, never raise, outcome recorded on the operation --

    def ensure(self, preset_topology: Topology) -> None:
        """Startup: adopt a finished cluster if one exists, otherwise build a clean one."""
        state = load_state(self._settings.data_dir)
        containers = self._docker.list_nodes()
        if containers and state.get("provisioned") and state.get("topology"):
            self._adopt(topology_from_dict(state["topology"]))
        elif containers:
            self.rebuild(preset_topology, kind="provision")  # leftovers from an interrupted run
        else:
            self.provision(preset_topology)

    def provision(self, topo: Topology, kind: str = "provision") -> None:
        op = self._ops.start(kind)
        self._run(op, lambda: self._provision(op, topo))

    def rebuild(self, topo: Topology, kind: str = "rebuild") -> None:
        op = self._ops.start(kind)

        def work():
            self._ops.step(op, "Draining the global load balancer")
            self._lb.apply([])
            self._ops.step(op, "Releasing existing capacity")
            for c in self._docker.list_nodes():
                self._docker.remove(c.name)
            self._provision(op, topo)

        self._run(op, work)

    # -- internals --

    def _run(self, op: Operation, work: Callable[[], None]) -> None:
        try:
            work()
        except Exception as e:  # surfaced to the presenter, never crashes the control plane
            log.exception("%s failed", op.kind)
            self._ops.finish(op, error=str(e))
        else:
            self._ops.finish(op)

    def _adopt(self, topo: Topology) -> None:
        op = self._ops.start("adopt")
        def work():
            self._ops.step(op, "Reconnecting to the running cluster")
            self.topology = topo
            self._lb.apply(topo.nodes)
        self._run(op, work)

    def _provision(self, op: Operation, topo: Topology) -> None:
        self.topology = topo
        save_state(self._settings.data_dir, {"topology": topology_to_dict(topo), "provisioned": False})
        if not self._docker.image_present(topo.image):
            self._ops.step(op, f"Downloading CockroachDB {topo.crdb_version}")
            try:
                self._docker.pull(topo.image)
            except DockerError as e:
                raise ProvisionError(f"Could not pull image {topo.image}: {e}") from e
        seeds = [f"{n.name}:26257" for n in topo.nodes[:3]]
        for region in topo.regions:
            self._ops.step(op, f"Allocating capacity in {region.name}")
            for node in topo.nodes:
                if node.region == region.name:
                    self._docker.run_node(node, topo, seeds, self._settings.network)
        self._ops.step(op, "Forming the cluster")
        self._init(topo.nodes[0], topo.cluster_id)
        self._ops.step(op, "Configuring the multi-region database")
        for warning in self._bootstrap(topo):
            self._ops.step(op, f"Note: {warning}")
        self._ops.step(op, "Configuring the global load balancer")
        self._lb.apply(topo.nodes)
        save_state(self._settings.data_dir, {"topology": topology_to_dict(topo), "provisioned": True})
        self._ops.step(op, "Cluster ready")

    def _init(self, node: NodeSpec, cluster_id: str) -> None:
        cmd = ["/cockroach/cockroach", "init", "--insecure", f"--host={node.name}:26257", f"--cluster-name={cluster_id}"]
        last = ""
        for _ in range(INIT_ATTEMPTS):
            proc = self._docker.exec(node.name, cmd, timeout=30, check=False)
            output = f"{proc.stdout}{proc.stderr}"
            if proc.returncode == 0 or "already been initialized" in output:
                return
            last = output.strip()
            self._sleep(1)
        raise ProvisionError(f"The cluster did not initialize: {last}")

    def _sql(self, host: str):
        last: Exception | None = None
        for _ in range(SQL_ATTEMPTS):
            try:
                return self._connect(host)
            except Exception as e:  # node still starting
                last = e
                self._sleep(1)
        raise ProvisionError(f"SQL never became available on {host}: {last}")

    def _bootstrap(self, topo: Topology) -> list[str]:
        conn = self._sql(topo.nodes[0].ip)
        try:
            wanted = {r.name for r in topo.regions}
            for _ in range(REGION_ATTEMPTS):
                with conn.cursor() as cur:
                    cur.execute("SELECT region FROM [SHOW REGIONS FROM CLUSTER]")
                    have = {row[0] for row in cur.fetchall()}
                if wanted <= have:
                    break
                self._sleep(1)
            else:
                missing = ", ".join(sorted(wanted - have))
                raise ProvisionError(f"These regions never joined the cluster: {missing}")
            statements = bootstrap_statements(topo, time_until_store_dead=self._settings.time_until_store_dead,
                                              license=self._settings.crdb_license, org=self._settings.crdb_org)
            return run_bootstrap(conn, statements, log=log.warning)
        finally:
            conn.close()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_provisioner.py -q`
Expected: `10 passed`

- [ ] **Step 6: Commit**

```bash
git add controlplane/controlplane/operations.py controlplane/controlplane/provisioner.py tests/unit/test_provisioner.py
git commit -m "Provision, adopt and rebuild clusters from a topology" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Snapshot and collector

**Files:**
- Create: `controlplane/controlplane/snapshot.py`
- Create: `controlplane/controlplane/collector.py`
- Test: `tests/unit/test_snapshot.py`
- Test: `tests/unit/test_collector.py`

**Interfaces:**
- Consumes:
  - `ContainerInfo`, `DockerError` (Task 5)
  - `Topology` (Task 3)
  - `Operations.snapshot()` (Task 8)
  - `Provisioner.topology` (Task 8)
  - `connect(host, timeout=…)` (Task 7)
- Produces:
  - `assemble_snapshot(topo, containers, sql_nodes, ranges, operation, now) -> dict`, with keys `ts, cluster, regions, nodes, ranges, operation, telemetry`
  - `Collector(docker, provisioner, operations, *, sql_host, connect, interval=1.5, clock=time.time)`, with `.latest() -> dict`, `.refresh() -> dict`, `.start()` and `.stop()`
- Snapshot node fields: `name, region, zone, provider, ip, runtime ("running"|"stopped"|"missing"), liveness ("live"|"dead"|"joining"|"unknown"), node_id, replicas, leases`. The field is called `runtime`, not `container`, because stage files must never contain that word (Task 12).
- Region `state`: `healthy | degraded | down | unknown`.
- Cluster `status`: `empty | provisioning | unreachable | critical | degraded | healthy`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_snapshot.py`:
```python
from pathlib import Path

from controlplane.catalog import load_catalog
from controlplane.docker_ops import ContainerInfo
from controlplane.snapshot import assemble_snapshot
from controlplane.topology import load_preset

ROOT = Path(__file__).resolve().parents[2]
TOPO = load_preset(ROOT / "presets", "classic", load_catalog(ROOT / "catalog" / "regions.yaml"),
                   default_version="v26.3.2", subnet="10.77.0.0/16", max_nodes=15)


def running(*names, stopped=()):
    return [ContainerInfo(n, n, n not in stopped, "running" if n not in stopped else "exited", {}, {}) for n in names]


def rows(live=None):
    live = live or {n.name: True for n in TOPO.nodes}
    return [{"node_id": i + 1, "address": f"{name}:26257", "is_live": ok, "ranges": 50, "leases": 10}
            for i, (name, ok) in enumerate(live.items())]


def test_healthy_cluster():
    snap = assemble_snapshot(TOPO, running(*[n.name for n in TOPO.nodes]), rows(), {"unavailable": 0, "underreplicated": 0}, None, 1.0)
    assert snap["cluster"]["status"] == "healthy"
    assert (snap["cluster"]["live_nodes"], snap["cluster"]["total_nodes"]) == (5, 5)
    assert snap["cluster"]["providers"] == ["aws"]
    assert [r["state"] for r in snap["regions"]] == ["healthy"] * 3
    node = snap["nodes"][0]
    assert node == {"name": "us-east-1a-n1", "region": "us-east-1", "zone": "us-east-1a", "provider": "aws",
                    "ip": "10.77.1.11", "runtime": "running", "liveness": "live", "node_id": 1, "replicas": 50, "leases": 10}
    assert snap["regions"][0]["label"] == "N. Virginia" and snap["regions"][0]["primary"] is True
    assert snap["telemetry"] == {"sql": "ok"}


def test_dead_region_degrades_the_cluster():
    live = {n.name: n.region != "us-east-1" for n in TOPO.nodes}
    names = [n.name for n in TOPO.nodes]
    snap = assemble_snapshot(TOPO, running(*names, stopped=("us-east-1a-n1", "us-east-1b-n2")), rows(live), {"unavailable": 0, "underreplicated": 12}, None, 1.0)
    assert snap["cluster"]["status"] == "degraded"
    assert snap["regions"][0]["state"] == "down" and snap["regions"][1]["state"] == "healthy"
    assert snap["nodes"][0]["runtime"] == "stopped" and snap["nodes"][0]["liveness"] == "dead"
    assert snap["ranges"] == {"unavailable": 0, "underreplicated": 12}


def test_unavailable_ranges_are_critical():
    snap = assemble_snapshot(TOPO, running(*[n.name for n in TOPO.nodes]), rows(), {"unavailable": 3, "underreplicated": 0}, None, 1.0)
    assert snap["cluster"]["status"] == "critical"


def test_missing_sql_telemetry_is_unreachable_not_zero():
    snap = assemble_snapshot(TOPO, running(*[n.name for n in TOPO.nodes]), None, None, None, 1.0)
    assert snap["cluster"]["status"] == "unreachable"
    assert {n["liveness"] for n in snap["nodes"]} == {"unknown"}
    assert snap["ranges"] == {"unavailable": None, "underreplicated": None}
    assert snap["telemetry"] == {"sql": "delayed"}


def test_running_operation_shows_provisioning_and_nodes_not_yet_gossiped_are_joining():
    op = {"id": "1", "kind": "provision", "status": "running", "steps": ["Allocating capacity in us-east-1"], "error": None}
    snap = assemble_snapshot(TOPO, running("us-east-1a-n1"), [], None, op, 1.0)
    assert snap["cluster"]["status"] == "provisioning"
    assert snap["nodes"][0]["liveness"] == "joining" and snap["nodes"][1]["runtime"] == "missing"
    assert snap["operation"] == op


def test_no_topology_is_empty():
    snap = assemble_snapshot(None, [], None, None, None, 1.0)
    assert snap["cluster"] is None and snap["regions"] == [] and snap["nodes"] == []
```

`tests/unit/test_collector.py`:
```python
from types import SimpleNamespace

from controlplane.collector import Collector
from controlplane.docker_ops import ContainerInfo
from controlplane.operations import Operations
from controlplane.topology import NodeSpec, RegionSpec, Topology

REGION = RegionSpec("us-east-1", "aws", 1, "N. Virginia", (38.9, -77.4), ("us-east-1a",), True, False, 1)
NODE = NodeSpec("us-east-1a-n1", "us-east-1", "us-east-1a", "aws", 1, "10.77.1.11")
TOPO = Topology("t", "T", "t", "v26.3.2", "none", "realistic", "256MiB", "256MiB", (REGION,), (NODE,))


class Docker:
    def list_nodes(self):
        return [ContainerInfo(NODE.name, "1", True, "running", {}, {})]


class Cursor:
    description = [SimpleNamespace(name=c) for c in ("node_id", "address", "is_live", "ranges", "leases")]

    def __init__(self):
        self.queries = []

    def execute(self, sql):
        self.queries.append(sql)

    def fetchall(self):
        return [(1, "us-east-1a-n1:26257", True, 40, 12)]

    def fetchone(self):
        return (0.0, 2.0)

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False


class Conn:
    def cursor(self):
        return Cursor()

    def close(self):
        pass


def test_refresh_combines_docker_sql_and_operation_state():
    tried = []

    def connect(host, **kw):
        tried.append(host)
        return Conn()

    c = Collector(Docker(), SimpleNamespace(topology=TOPO), Operations(), sql_host="haproxy", connect=connect, clock=lambda: 5.0)
    snap = c.refresh()
    assert tried == ["haproxy"]
    assert snap["nodes"][0]["liveness"] == "live" and snap["nodes"][0]["leases"] == 12
    assert snap["ranges"] == {"unavailable": 0, "underreplicated": 2}
    assert c.latest() is snap


def test_falls_back_to_a_node_when_the_load_balancer_is_unreachable():
    tried = []

    def connect(host, **kw):
        tried.append(host)
        if host == "haproxy":
            raise OSError("no route")
        return Conn()

    snap = Collector(Docker(), SimpleNamespace(topology=TOPO), Operations(), sql_host="haproxy", connect=connect).refresh()
    assert tried == ["haproxy", "10.77.1.11"]
    assert snap["telemetry"] == {"sql": "ok"}


def test_unreachable_sql_yields_delayed_telemetry():
    def connect(host, **kw):
        raise OSError("down")

    snap = Collector(Docker(), SimpleNamespace(topology=TOPO), Operations(), sql_host="haproxy", connect=connect).refresh()
    assert snap["telemetry"] == {"sql": "delayed"} and snap["cluster"]["status"] == "unreachable"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_snapshot.py tests/unit/test_collector.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'controlplane.snapshot'`

- [ ] **Step 3: Implement** `controlplane/controlplane/snapshot.py`

```python
"""The single state document every viewer receives: built by the collector, served by the API."""
from __future__ import annotations

from typing import Sequence

from .docker_ops import ContainerInfo
from .topology import Topology


def _liveness(row, runtime: str, telemetry_ok: bool) -> str:
    if not telemetry_ok:
        return "unknown"
    if row is not None:
        return "live" if row["is_live"] else "dead"
    return "joining" if runtime == "running" else "dead"


def assemble_snapshot(topo: Topology | None, containers: Sequence[ContainerInfo], sql_nodes: Sequence[dict] | None,
                      ranges: dict | None, operation: dict | None, now: float) -> dict:
    telemetry_ok = sql_nodes is not None
    by_name = {c.name: c for c in containers}
    by_host = {str(r["address"]).rsplit(":", 1)[0]: r for r in (sql_nodes or [])}
    nodes, regions = [], []
    if topo:
        for n in topo.nodes:
            c = by_name.get(n.name)
            runtime = "missing" if c is None else ("running" if c.running else "stopped")
            row = by_host.get(n.name)
            nodes.append({
                "name": n.name, "region": n.region, "zone": n.zone, "provider": n.provider, "ip": n.ip,
                "runtime": runtime, "liveness": _liveness(row, runtime, telemetry_ok),
                "node_id": row["node_id"] if row else None,
                "replicas": row["ranges"] if row else None,
                "leases": row["leases"] if row else None,
            })
        for r in topo.regions:
            members = [x for x in nodes if x["region"] == r.name]
            live = sum(x["liveness"] == "live" for x in members)
            state = ("unknown" if not telemetry_ok else
                     "healthy" if live == len(members) else "down" if live == 0 else "degraded")
            regions.append({
                "name": r.name, "provider": r.provider, "label": r.label,
                "location": list(r.location) if r.location else None,
                "primary": r.primary, "secondary": r.secondary, "zones": list(r.zones),
                "state": state, "nodes": [x["name"] for x in members],
            })

    unavailable = (ranges or {}).get("unavailable")
    underreplicated = (ranges or {}).get("underreplicated")
    live_nodes = sum(x["liveness"] == "live" for x in nodes)
    if operation and operation.get("status") == "running":
        status = "provisioning"
    elif topo is None:
        status = "empty"
    elif not telemetry_ok:
        status = "unreachable"
    elif unavailable:
        status = "critical"
    elif live_nodes == len(nodes):
        status = "healthy"
    else:
        status = "degraded" if live_nodes else "critical"

    return {
        "ts": now,
        "cluster": None if topo is None else {
            "preset": topo.preset, "display_name": topo.display_name, "cluster_id": topo.cluster_id,
            "crdb_version": topo.crdb_version, "survival_goal": topo.survival_goal,
            "providers": sorted({r.provider for r in topo.regions}), "status": status,
            "live_nodes": live_nodes, "total_nodes": len(nodes), "warnings": list(topo.warnings),
        },
        "regions": regions,
        "nodes": nodes,
        "ranges": {"unavailable": unavailable, "underreplicated": underreplicated},
        "operation": operation,
        "telemetry": {"sql": "ok" if telemetry_ok else "delayed"},
    }
```

- [ ] **Step 4: Implement** `controlplane/controlplane/collector.py`

```python
"""Background thread that rebuilds the snapshot every interval."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .docker_ops import DockerError
from .snapshot import assemble_snapshot

log = logging.getLogger(__name__)

NODES_SQL = "SELECT node_id, address, is_live, ranges, leases FROM crdb_internal.gossip_nodes"
RANGES_SQL = ("SELECT COALESCE(sum((metrics->>'ranges.unavailable')::FLOAT), 0), "
              "COALESCE(sum((metrics->>'ranges.underreplicated')::FLOAT), 0) FROM crdb_internal.kv_store_status")


class Collector:
    def __init__(self, docker, provisioner, operations, *, sql_host: str, connect: Callable,
                 interval: float = 1.5, clock: Callable[[], float] = time.time):
        self._docker = docker
        self._provisioner = provisioner
        self._ops = operations
        self._sql_host = sql_host
        self._connect = connect
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._latest = assemble_snapshot(None, [], None, None, None, clock())

    def latest(self) -> dict:
        with self._lock:
            return self._latest

    def refresh(self) -> dict:
        topo = self._provisioner.topology
        try:
            containers = self._docker.list_nodes()
        except DockerError:
            containers = []
        sql_nodes, ranges = (None, None)
        if topo is not None:
            hosts = [self._sql_host] + [n.ip for n in topo.nodes]
            sql_nodes, ranges = self._query(hosts)
        snap = assemble_snapshot(topo, containers, sql_nodes, ranges, self._ops.snapshot(), self._clock())
        with self._lock:
            self._latest = snap
        return snap

    def _query(self, hosts):
        for host in hosts:
            try:
                conn = self._connect(host)
            except Exception:
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute(NODES_SQL)
                    cols = [d.name for d in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                    cur.execute(RANGES_SQL)
                    unavailable, underreplicated = cur.fetchone()
                return rows, {"unavailable": int(unavailable or 0), "underreplicated": int(underreplicated or 0)}
            except Exception as e:
                log.debug("telemetry query via %s failed: %s", host, e)
            finally:
                conn.close()
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_snapshot.py tests/unit/test_collector.py -q`
Expected: `9 passed`

- [ ] **Step 6: Commit**

```bash
git add controlplane/controlplane/snapshot.py controlplane/controlplane/collector.py tests/unit/test_snapshot.py tests/unit/test_collector.py
git commit -m "Build the live cluster snapshot from Docker and SQL telemetry" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: HTTP API, startup and entry point

**Files:**
- Create: `controlplane/controlplane/app.py`
- Create: `controlplane/controlplane/__main__.py`
- Test: `tests/unit/test_app.py`

**Interfaces:**
- Consumes:
  - `Collector.latest()` (Task 9)
  - `Operations` (Task 8)
  - `Provisioner.ensure`/`.rebuild` (Task 8)
  - `PresetError`, `list_presets` (Task 3)
  - `LoadBalancer.ensure_config` (Task 6)
- Produces:
  - `create_app(*, settings, provisioner, collector, operations, load_topology, run_in_background=None) -> FastAPI`
  - `start_cluster(preset: str, *, load_topology, provisioner, operations, lb) -> None`
  - Routes:
    - `GET /api/state`
    - `GET /api/events` (SSE, one snapshot per second; `?once=1` sends a single event)
    - `GET /api/presets`
    - `POST /api/cluster/rebuild {"preset"}`: 202, or 400 for a bad preset, or 409 while busy
    - `GET /` (the stage)
    - static files under `/static/` (the `web/` directory)

- [ ] **Step 1: Write the failing tests** at `tests/unit/test_app.py`

```python
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from controlplane.app import create_app, start_cluster
from controlplane.catalog import load_catalog
from controlplane.operations import Operations
from controlplane.topology import PresetError, load_preset

ROOT = Path(__file__).resolve().parents[2]
CAT = load_catalog(ROOT / "catalog" / "regions.yaml")


def load(name):
    return load_preset(ROOT / "presets", name, CAT, default_version="v26.3.2", subnet="10.77.0.0/16", max_nodes=15)


class Recorder:
    def __init__(self):
        self.rebuilt, self.ensured = [], []

    def rebuild(self, topo):
        self.rebuilt.append(topo.preset)

    def ensure(self, topo):
        self.ensured.append(topo.preset)


@pytest.fixture
def web(tmp_path):
    (tmp_path / "stage").mkdir()
    (tmp_path / "stage" / "index.html").write_text("<html>stage</html>", encoding="utf-8")
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "tokens.css").write_text(":root{}", encoding="utf-8")
    return tmp_path


def make_client(web, operations=None, provisioner=None):
    settings = SimpleNamespace(presets_dir=ROOT / "presets", web_dir=web)
    collector = SimpleNamespace(latest=lambda: {"cluster": None, "nodes": []})
    app = create_app(settings=settings, provisioner=provisioner or Recorder(), collector=collector,
                     operations=operations or Operations(), load_topology=load, run_in_background=lambda fn: fn())
    return TestClient(app)


def test_state_returns_the_latest_snapshot(web):
    assert make_client(web).get("/api/state").json() == {"cluster": None, "nodes": []}


def test_presets_lists_shipped_presets_with_their_regions(web):
    presets = {p["name"]: p for p in make_client(web).get("/api/presets").json()}
    assert presets["aws"]["survival_goal"] == "region"
    assert [r["name"] for r in presets["aws"]["regions"]] == ["us-east-1", "us-west-2", "eu-west-1"]


def test_rebuild_accepts_valid_presets(web):
    rec = Recorder()
    resp = make_client(web, provisioner=rec).post("/api/cluster/rebuild", json={"preset": "gcp"})
    assert resp.status_code == 202 and rec.rebuilt == ["gcp"]


def test_rebuild_rejects_unknown_presets_with_the_reason(web):
    resp = make_client(web).post("/api/cluster/rebuild", json={"preset": "mars"})
    assert resp.status_code == 400 and "unknown preset" in resp.json()["detail"]


def test_rebuild_is_refused_while_another_operation_runs(web):
    ops = Operations()
    ops.start("provision")
    resp = make_client(web, operations=ops).post("/api/cluster/rebuild", json={"preset": "gcp"})
    assert resp.status_code == 409


def test_stage_and_static_assets_are_served(web):
    client = make_client(web)
    assert "stage" in client.get("/").text
    assert client.get("/static/shared/tokens.css").status_code == 200


def test_events_stream_sends_snapshots(web):
    resp = make_client(web).get("/api/events?once=1")
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text.startswith("data: ") and resp.text.endswith("\n\n")


def test_start_cluster_records_bad_presets_instead_of_crashing():
    ops, rec = Operations(), Recorder()
    lb = SimpleNamespace(ensure_config=lambda: None)

    def bad(name):
        raise PresetError("preset x: region name 'US' must be lowercase")

    start_cluster("x", load_topology=bad, provisioner=rec, operations=ops, lb=lb)
    op = ops.snapshot()
    assert op["status"] == "failed" and "lowercase" in op["error"] and rec.ensured == []


def test_start_cluster_writes_a_bootstrap_lb_config_then_ensures():
    calls, rec = [], Recorder()
    lb = SimpleNamespace(ensure_config=lambda: calls.append("lb"))
    start_cluster("aws", load_topology=load, provisioner=rec, operations=Operations(), lb=lb)
    assert calls == ["lb"] and rec.ensured == ["aws"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_app.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'controlplane.app'`

- [ ] **Step 3: Implement** `controlplane/controlplane/app.py`

```python
"""HTTP API and static pages for the control plane."""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .operations import OperationBusy
from .topology import PresetError, list_presets


class RebuildRequest(BaseModel):
    preset: str


def _background(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def start_cluster(preset: str, *, load_topology, provisioner, operations, lb) -> None:
    """Startup work, run on a background thread: never let a bad preset crash-loop the container."""
    lb.ensure_config()
    try:
        topo = load_topology(preset)
    except PresetError as e:
        op = operations.start("provision")
        operations.finish(op, error=str(e))
        return
    provisioner.ensure(topo)


def create_app(*, settings, provisioner, collector, operations, load_topology,
               run_in_background: Callable[[Callable[[], None]], None] | None = None) -> FastAPI:
    run_bg = run_in_background or _background
    app = FastAPI(title="CockroachDB chaos control plane")

    @app.get("/api/state")
    def state():
        return collector.latest()

    @app.get("/api/events")
    async def events(request: Request, once: bool = False):
        async def stream():
            while not await request.is_disconnected():
                yield f"data: {json.dumps(collector.latest())}\n\n"
                if once:  # single event; used by tests and quick curl checks
                    break
                await asyncio.sleep(1.0)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/presets")
    def presets():
        out = []
        for name in list_presets(settings.presets_dir):
            try:
                t = load_topology(name)
            except PresetError as e:
                out.append({"name": name, "error": str(e)})
                continue
            out.append({"name": name, "display_name": t.display_name, "survival_goal": t.survival_goal,
                        "regions": [{"name": r.name, "provider": r.provider, "nodes": r.nodes} for r in t.regions],
                        "warnings": list(t.warnings)})
        return out

    @app.post("/api/cluster/rebuild", status_code=202)
    def rebuild(req: RebuildRequest):
        try:
            topo = load_topology(req.preset)
        except PresetError as e:
            raise HTTPException(status_code=400, detail=str(e))
        current = operations.snapshot()
        if current and current["status"] == "running":
            raise HTTPException(status_code=409, detail=f"{current['kind']} already running")

        def work():
            try:
                provisioner.rebuild(topo)
            except OperationBusy:
                pass  # lost a race with another request; that operation reports itself
        run_bg(work)
        return {"accepted": True, "preset": req.preset}

    @app.get("/")
    def stage():
        return FileResponse(settings.web_dir / "stage" / "index.html")

    app.mount("/static", StaticFiles(directory=settings.web_dir), name="static")
    return app
```

- [ ] **Step 4: Implement** `controlplane/controlplane/__main__.py`

```python
"""Entry point: wire the real collaborators, start background work, serve on :8088."""
from __future__ import annotations

import logging
import threading
from functools import partial

import uvicorn

from .app import create_app, start_cluster
from .catalog import load_catalog
from .collector import Collector
from .docker_ops import Docker
from .lb import LoadBalancer
from .operations import Operations
from .provisioner import Provisioner
from .settings import Settings
from .sqlconn import connect
from .topology import load_preset


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    catalog = load_catalog(settings.catalog_path)
    docker = Docker()
    operations = Operations()
    lb = LoadBalancer(docker, settings.haproxy_cfg_dir, settings.haproxy_container)
    provisioner = Provisioner(docker, lb, operations, settings, connect=connect)

    def load_topology(name: str):
        return load_preset(settings.presets_dir, name, catalog, default_version=settings.crdb_version,
                           subnet=settings.subnet, max_nodes=settings.max_nodes)

    collector = Collector(docker, provisioner, operations, sql_host=settings.haproxy_container,
                          connect=partial(connect, timeout=2))
    app = create_app(settings=settings, provisioner=provisioner, collector=collector,
                     operations=operations, load_topology=load_topology)
    threading.Thread(target=start_cluster, args=(settings.preset,), daemon=True, name="startup",
                     kwargs=dict(load_topology=load_topology, provisioner=provisioner, operations=operations, lb=lb)).start()
    collector.start()
    uvicorn.run(app, host="0.0.0.0", port=8088, log_level="info")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_app.py -q`
Expected: `9 passed`

- [ ] **Step 6: Run the whole unit suite**

Run: `.venv/bin/pytest -q`
Expected: every test so far passes, with no warnings that mention our modules.

- [ ] **Step 7: Commit**

```bash
git add controlplane/controlplane/app.py controlplane/controlplane/__main__.py tests/unit/test_app.py
git commit -m "Serve state, presets and rebuilds over HTTP" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Control-plane image, compose file and env template

**Files:**
- Create: `controlplane/Dockerfile`
- Modify (full rewrite): `docker-compose.yml`
- Modify (full rewrite): `.env.example`

**Interfaces:**
- Consumes: `python -m controlplane` (Task 10); `presets/`, `catalog/` (Tasks 2–3); `web/` (Task 12, mounted read-only and created in the next task)
- Produces:
  - image `crdb-chaos-control-plane:local`, which contains `docker` (CLI only) and `tc`
  - compose project `crdb-chaos` with services `haproxy` and `control-plane`
  - network `crdb-chaos`
  - volumes `haproxy-config` (mounted at `/cfg` in HAProxy and `/haproxy` in the control plane) and `control-data` (`/data`)

- [ ] **Step 1: Create** `controlplane/Dockerfile`

```dockerfile
FROM python:3.12-slim-trixie

# docker-cli drives the nodes through the mounted socket (Docker or Podman).
# iproute2 lets this same image act as the netem helper in milestone 2.
RUN apt-get update \
 && apt-get install -y --no-install-recommends docker-cli iproute2 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY controlplane ./controlplane

EXPOSE 8088
CMD ["python", "-m", "controlplane"]
```

- [ ] **Step 2: Rewrite** `docker-compose.yml`

```yaml
# Two fixed services. The control plane creates the CockroachDB nodes itself from a preset (see presets/).
name: crdb-chaos

services:
  haproxy:
    image: docker.io/library/haproxy:lts
    container_name: haproxy
    restart: unless-stopped
    # Wait for the control plane's first config, then hand PID 1 to HAProxy so SIGUSR2 reloads it.
    entrypoint: ["/bin/sh", "-c", "until [ -s /cfg/haproxy.cfg ]; do sleep 1; done; exec haproxy -W -db -f /cfg/haproxy.cfg"]
    ports:
      - "26257:26257"   # SQL, e.g. Banko: postgresql://root@localhost:26257/defaultdb?sslmode=disable
      - "8080:8080"     # DB Console, served by whichever node is up
      - "8404:8404"     # HAProxy stats
    volumes:
      - haproxy-config:/cfg:ro
    networks: [chaos]

  control-plane:
    build: ./controlplane
    image: crdb-chaos-control-plane:local
    container_name: crdb-chaos-control-plane
    restart: unless-stopped
    environment:
      PRESET: ${PRESET:-aws}
      CRDB_VERSION: ${CRDB_VERSION:-v26.3.2}
      CHAOS_SUBNET: ${CHAOS_SUBNET:-10.77.0.0/16}
      CHAOS_NETWORK: crdb-chaos
      MAX_NODES: ${MAX_NODES:-15}
      TIME_UNTIL_STORE_DEAD: ${TIME_UNTIL_STORE_DEAD:-1m15s}
      CRDB_LICENSE: ${CRDB_LICENSE:-}
      CRDB_ORG: ${CRDB_ORG:-}
    ports:
      - "${PANEL_PORT:-8088}:8088"
    volumes:
      # Docker socket. Podman without the /var/run/docker.sock symlink: set DOCKER_SOCKET in .env.
      - ${DOCKER_SOCKET:-/var/run/docker.sock}:/var/run/docker.sock
      - ./presets:/app/presets
      - ./catalog:/app/catalog:ro
      - ./web:/app/web:ro
      - control-data:/data
      - haproxy-config:/haproxy
    networks: [chaos]
    depends_on: [haproxy]

networks:
  chaos:
    name: crdb-chaos
    ipam:
      config:
        - subnet: ${CHAOS_SUBNET:-10.77.0.0/16}

volumes:
  haproxy-config:
  control-data:
```

- [ ] **Step 3: Rewrite** `.env.example`

```bash
# Copy to .env to override defaults. Everything here is optional.

# Preset that ./run.sh up starts (see presets/). Override per run with --preset NAME.
# PRESET=aws

# CockroachDB version for new clusters (a preset can pin its own).
# CRDB_VERSION=v26.3.2

# Port for the stage and API.
# PANEL_PORT=8088

# Change if 10.77.0.0/16 collides with a VPN or another network. Must be an IPv4 /16.
# CHAOS_SUBNET=10.77.0.0/16

# Optional CockroachDB license, applied at bootstrap.
# CRDB_ORG=
# CRDB_LICENSE=

# Docker socket. Docker Desktop, and Podman with the /var/run/docker.sock symlink, need nothing here.
# Podman on macOS without the symlink:
# DOCKER_SOCKET=/Users/yourusername/.local/share/containers/podman/machine/podman.sock
# Podman on Linux (replace 502 with the output of `id -u`):
# DOCKER_SOCKET=/run/user/502/podman/podman.sock
```

- [ ] **Step 4: Validate and build**

Run: `docker compose config -q && docker compose build control-plane`
Expected: no output from `config -q`; the build finishes with `crdb-chaos-control-plane:local` tagged.

- [ ] **Step 5: Check the image contents**

Run: `docker run --rm --entrypoint docker crdb-chaos-control-plane:local --version && docker run --rm --entrypoint tc crdb-chaos-control-plane:local -V && docker run --rm crdb-chaos-control-plane:local python -c "import controlplane.app, controlplane.provisioner; print('imports ok')"`
Expected: `Docker version 26.1.5…`, a `tc utility, iproute2-…` line, then `imports ok`.

- [ ] **Step 6: Commit**

```bash
git add controlplane/Dockerfile docker-compose.yml .env.example
git commit -m "Run the control plane and HAProxy as the only compose services" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Brand assets and the minimal stage

**Files:**
- Create: `web/shared/tokens.css`, `web/shared/fonts/Geist-Variable.woff2`, `web/shared/fonts/GeistMono-Variable.woff2`, `web/shared/fonts/OFL.txt`, `web/shared/crl-logo.svg`
- Move: `backend/static/favicon.ico` → `web/shared/favicon.ico`
- Create: `web/stage/index.html`, `web/stage/stage.css`, `web/stage/stage.js`
- Test: `tests/unit/test_stage_copy.py`

**Interfaces:**
- Consumes:
  - the `GET /api/events` snapshot (Task 9 shape): `cluster{display_name, providers, total_nodes, live_nodes, survival_goal, status}`, `regions[{name, provider, label, primary, secondary, state, nodes}]`, `nodes[{name, zone, liveness, replicas, leases}]`, `ranges{unavailable, underreplicated}` and `operation{status, steps, error}`
  - static files under `/static/` (Task 10)
- Produces: the stage at `/`, plus the shared tokens that M3 builds layout B and the presenter console on. The theme comes from `?theme=` or `localStorage['crdb-stage-theme']`.

- [ ] **Step 1: Write the failing test** at `tests/unit/test_stage_copy.py`

```python
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STAGE = [ROOT / "web" / "stage" / name for name in ("index.html", "stage.js", "stage.css")]
FORBIDDEN = re.compile(r"\b(docker|container|podman|localhost)\b", re.IGNORECASE)


def test_stage_never_mentions_local_infrastructure():
    for path in STAGE:
        hits = [m.group(0) for m in FORBIDDEN.finditer(path.read_text(encoding="utf-8"))]
        assert not hits, f"{path.name} mentions {hits}"


def test_stage_uses_shared_brand_tokens_and_live_events():
    html = (ROOT / "web" / "stage" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "stage" / "stage.js").read_text(encoding="utf-8")
    tokens = (ROOT / "web" / "shared" / "tokens.css").read_text(encoding="utf-8")
    assert "/static/shared/tokens.css" in html and "/static/stage/stage.js" in html
    assert "EventSource('/api/events')" in js
    for token in ("#aede54", "#9059ff", "#60ecff", "#f9f8f3", "[data-theme=light]", "Geist-Variable.woff2"):
        assert token in tokens
    for asset in ("fonts/Geist-Variable.woff2", "fonts/GeistMono-Variable.woff2", "fonts/OFL.txt", "crl-logo.svg", "favicon.ico"):
        assert (ROOT / "web" / "shared" / asset).is_file(), asset
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_stage_copy.py -q`
Expected: FAIL with `FileNotFoundError` for `web/stage/index.html`

- [ ] **Step 3: Fetch the fonts and their license, copy the wordmark, and move the favicon**

```bash
mkdir -p web/shared/fonts web/stage
curl -fsSL -o web/shared/fonts/Geist-Variable.woff2 https://unpkg.com/geist@1.3.1/dist/fonts/geist-sans/Geist-Variable.woff2
curl -fsSL -o web/shared/fonts/GeistMono-Variable.woff2 https://unpkg.com/geist@1.3.1/dist/fonts/geist-mono/GeistMono-Variable.woff2
curl -fsSL -o web/shared/fonts/OFL.txt https://raw.githubusercontent.com/vercel/geist-font/main/OFL.txt
cp .superpowers/brainstorm/1978-1791561011/content/crl-logo.svg web/shared/crl-logo.svg
git mv backend/static/favicon.ico web/shared/favicon.ico
```
Expected: two woff2 files of about 57 KB each, an `OFL.txt` that starts with "Copyright", and a `crl-logo.svg` of about 10 KB containing `#AEDE54`.

- [ ] **Step 4: Create** `web/shared/tokens.css`

```css
/* Continuum brand tokens (cockroachlabs.com, September 2026). Degraded/failed colors are off-palette on purpose. */
@font-face { font-family: "Geist"; src: url("/static/shared/fonts/Geist-Variable.woff2") format("woff2"); font-weight: 100 900; font-display: swap; }
@font-face { font-family: "Geist Mono"; src: url("/static/shared/fonts/GeistMono-Variable.woff2") format("woff2"); font-weight: 100 900; font-display: swap; }

:root {
  --bg: #000000;
  --fg: #ffffff;
  --muted: #888884;
  --line: #232323;
  --panel: #070707;
  --healthy: #aede54;
  --healthy-line: #aede54;
  --info: #60ecff;
  --brand: #9059ff;
  --brand-text: #c8acff;
  --degraded: #ffb547;
  --failed: #ff4d5e;
  --font: "Geist", system-ui, sans-serif;
  --mono: "Geist Mono", ui-monospace, monospace;
}

[data-theme=light] {
  --bg: #f9f8f3;
  --fg: #0b0b0b;
  --muted: #5f5f5a;
  --line: #e2e1d8;
  --panel: #ffffff;
  --healthy: #aede54;
  --healthy-line: #43541f;
  --info: #144f6b;
  --brand: #7a3ff0;
  --brand-text: #502482;
  --degraded: #b86e00;
  --failed: #d42a41;
}
```

- [ ] **Step 5: Create** `web/stage/index.html`

```html
<!doctype html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CockroachDB</title>
  <link rel="icon" href="/static/shared/favicon.ico">
  <link rel="stylesheet" href="/static/shared/tokens.css">
  <link rel="stylesheet" href="/static/stage/stage.css">
</head>
<body>
  <header class="top">
    <img class="logo" src="/static/shared/crl-logo.svg" alt="Cockroach Labs">
    <span class="sep"></span>
    <h1 id="cluster-name">Connecting…</h1>
    <span class="chip" id="chip-footprint"></span>
    <span class="chip chip-goal" id="chip-goal"></span>
    <button class="theme" id="theme" type="button" aria-label="Switch theme">☀</button>
  </header>
  <section class="kpis">
    <div class="kpi"><div class="label">Live nodes</div><div class="value" id="kpi-nodes">–</div></div>
    <div class="kpi"><div class="label">Unavailable ranges</div><div class="value" id="kpi-unavailable">–</div></div>
    <div class="kpi"><div class="label">Under-replicated</div><div class="value" id="kpi-under">–</div></div>
    <div class="kpi"><div class="label">Cluster</div><div class="value small" id="kpi-status">–</div></div>
  </section>
  <main class="regions" id="regions"></main>
  <section class="provisioning" id="provisioning" hidden>
    <div class="label">Provisioning</div>
    <ol id="steps"></ol>
    <p class="error" id="op-error" hidden></p>
  </section>
  <script type="module" src="/static/stage/stage.js"></script>
</body>
</html>
```

- [ ] **Step 6: Create** `web/stage/stage.css`

```css
* { box-sizing: border-box; margin: 0; }
html, body { background: var(--bg); color: var(--fg); font-family: var(--font); min-height: 100%; }
body { padding: 28px 40px 48px; }
.top { display: flex; align-items: center; gap: 16px; padding-bottom: 20px; border-bottom: 1px solid var(--line); }
.logo { height: 16px; }
[data-theme=light] .logo { filter: brightness(0); }
.sep { width: 1px; height: 24px; background: var(--line); }
h1 { font-weight: 300; font-size: 26px; }
.chip { font-family: var(--mono); font-size: 11px; letter-spacing: 1.6px; text-transform: uppercase; padding: 6px 12px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); }
.chip:empty { display: none; }
.chip-goal { border-color: var(--healthy-line); color: var(--healthy-line); }
.theme { margin-left: auto; background: none; border: 1px solid var(--line); color: var(--fg); border-radius: 999px; width: 36px; height: 36px; cursor: pointer; font-size: 16px; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); margin: 24px 0; border: 1px solid var(--line); border-radius: 16px; background: var(--panel); }
.kpi { padding: 18px 22px; border-left: 1px solid var(--line); }
.kpi:first-child { border-left: none; }
.label { font-family: var(--mono); font-size: 12px; letter-spacing: 2px; text-transform: uppercase; color: var(--muted); }
.value { font-weight: 300; font-size: 52px; line-height: 1.1; margin-top: 6px; font-variant-numeric: tabular-nums; }
.value.small { font-size: 30px; padding-top: 12px; text-transform: capitalize; }
.good { color: var(--healthy-line); }
.warn { color: var(--degraded); }
.bad { color: var(--failed); }
.regions { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 16px; }
.region { border: 1px solid var(--line); border-radius: 16px; background: var(--panel); padding: 18px 20px; }
.region.down { border-color: var(--failed); }
.region.degraded { border-color: var(--degraded); }
.region-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.provider { font-family: var(--mono); font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; padding: 2px 7px; border: 1px solid var(--line); border-radius: 4px; color: var(--muted); }
.region-name { font-weight: 300; font-size: 24px; }
.region-label { font-size: 13px; color: var(--muted); }
.region-role { margin-left: auto; font-family: var(--mono); font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; color: var(--brand-text); }
.zones { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 10px; margin-top: 16px; }
.node { border-radius: 8px; padding: 9px 10px; min-height: 76px; display: flex; flex-direction: column; justify-content: space-between; border: 1px solid var(--line); }
.node .zone { font-family: var(--mono); font-size: 10px; color: var(--muted); }
.node .name { font-family: var(--mono); font-size: 12px; }
.node .meta { font-family: var(--mono); font-size: 10px; color: var(--muted); }
.node.live { border-color: var(--healthy-line); background: color-mix(in srgb, var(--healthy) 14%, transparent); }
.node.live .name { color: var(--healthy-line); }
.node.joining { border-color: var(--brand); background: color-mix(in srgb, var(--brand) 14%, transparent); }
.node.dead { border-color: var(--failed); background: repeating-linear-gradient(135deg, color-mix(in srgb, var(--failed) 16%, transparent) 0 6px, transparent 6px 12px); }
.node.dead .name { color: var(--failed); }
.provisioning { margin-top: 24px; border: 1px solid var(--line); border-radius: 16px; padding: 18px 22px; background: var(--panel); }
.provisioning ol { margin: 10px 0 0 18px; font-size: 16px; line-height: 1.8; }
.provisioning li:last-child { color: var(--healthy-line); }
.error { margin-top: 10px; color: var(--failed); font-family: var(--mono); font-size: 13px; }
```

- [ ] **Step 7: Create** `web/stage/stage.js`

```js
// Minimal stage (milestone 1): live cluster state from /api/events. Layout B grows from this in milestone 3.
const $ = (id) => document.getElementById(id);
const THEME_KEY = 'crdb-stage-theme';
const STATUS_CLASS = { healthy: 'good', degraded: 'warn', critical: 'bad', unreachable: 'bad', provisioning: 'warn', empty: '' };
const NODE_CLASS = { live: 'live', joining: 'joining', dead: 'dead', unknown: '' };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('theme').textContent = theme === 'dark' ? '☀' : '☾';
}

function initTheme() {
  const fromUrl = new URLSearchParams(location.search).get('theme');
  applyTheme(fromUrl || localStorage.getItem(THEME_KEY) || 'dark');
  $('theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
  });
}

function renderHeader(cluster) {
  $('cluster-name').textContent = cluster ? cluster.display_name : 'Waiting for the cluster…';
  if (!cluster) {
    $('chip-footprint').textContent = '';
    $('chip-goal').textContent = '';
    return;
  }
  const providers = cluster.providers.map((p) => p.toUpperCase()).join(' · ');
  $('chip-footprint').textContent = `${providers} · ${cluster.total_nodes} nodes`;
  $('chip-goal').textContent = cluster.survival_goal === 'none' ? 'Replicated ×3' : `Survives ${cluster.survival_goal} failure`;
}

function setValue(id, text, cls) {
  const node = $(id);
  node.textContent = text;
  node.className = ['value', id === 'kpi-status' ? 'small' : '', cls || ''].filter(Boolean).join(' ');
}

function renderKpis(state) {
  const c = state.cluster;
  if (!c) {
    for (const id of ['kpi-nodes', 'kpi-unavailable', 'kpi-under', 'kpi-status']) setValue(id, '–');
    return;
  }
  const { unavailable, underreplicated } = state.ranges;
  setValue('kpi-nodes', `${c.live_nodes}/${c.total_nodes}`, c.live_nodes === c.total_nodes ? 'good' : 'bad');
  setValue('kpi-unavailable', unavailable ?? '–', unavailable ? 'bad' : 'good');
  setValue('kpi-under', underreplicated ?? '–', underreplicated ? 'warn' : '');
  setValue('kpi-status', c.status, STATUS_CLASS[c.status]);
}

function nodeTile(node) {
  const tile = el('div', `node ${NODE_CLASS[node.liveness] || ''}`);
  const meta = node.replicas == null ? node.liveness : `R ${node.replicas} · L ${node.leases}`;
  tile.append(el('span', 'zone', node.zone), el('span', 'name', node.name), el('span', 'meta', meta));
  return tile;
}

function renderRegions(state) {
  const byName = new Map(state.nodes.map((n) => [n.name, n]));
  $('regions').replaceChildren(...state.regions.map((region) => {
    const card = el('article', `region ${region.state}`);
    const head = el('div', 'region-head');
    head.append(el('span', 'provider', region.provider), el('span', 'region-name', region.name), el('span', 'region-label', region.label));
    if (region.primary || region.secondary) head.append(el('span', 'region-role', region.primary ? 'Primary' : 'Secondary'));
    const grid = el('div', 'zones');
    grid.append(...region.nodes.map((name) => nodeTile(byName.get(name))));
    card.append(head, grid);
    return card;
  }));
}

function renderOperation(op) {
  const visible = Boolean(op) && (op.status === 'running' || op.status === 'failed');
  $('provisioning').hidden = !visible;
  if (!visible) return;
  $('steps').replaceChildren(...op.steps.map((s) => el('li', '', s)));
  $('op-error').hidden = !op.error;
  $('op-error').textContent = op.error || '';
}

function render(state) {
  renderHeader(state.cluster);
  renderKpis(state);
  renderRegions(state);
  renderOperation(state.operation);
}

initTheme();
const events = new EventSource('/api/events');
events.onmessage = (msg) => render(JSON.parse(msg.data));
```

- [ ] **Step 8: Run the tests and a syntax check**

Run: `.venv/bin/pytest tests/unit/test_stage_copy.py -q && node --check web/stage/stage.js && echo js-ok`
Expected: `2 passed` and `js-ok`

- [ ] **Step 9: Commit**

```bash
git add web tests/unit/test_stage_copy.py
git commit -m "Add the Continuum brand tokens and a live minimal stage" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Preflight checks and the new `run.sh`

**Files:**
- Create: `tools/preflight.py`
- Test: `tests/unit/test_preflight.py`
- Modify (full rewrite): `run.sh`

**Interfaces:**
- Consumes:
  - `GET /api/state` and `POST /api/cluster/rebuild` (Task 10)
  - compose services (Task 11)
  - presets (Task 3)
- Produces:
  - `Check(name, ok, message, level="error")`
  - `run_checks(*, nodes, subnet, panel_port, docker=docker_cli, in_use=port_in_use) -> list[Check]`
  - `preset_nodes(path) -> int`
  - `main(argv) -> int`, which returns 1 only on error-level failures
  - `./run.sh up [--preset NAME] | down [--legacy] | reset [--preset NAME] | status | prefetch | test [pytest args]`

- [ ] **Step 1: Write the failing tests** at `tests/unit/test_preflight.py`

```python
import json
import subprocess

import preflight
from preflight import Check, main, preset_nodes, run_checks

BASE = dict(nodes=9, subnet="10.77.0.0/16", panel_port=8088)


def fake_docker(ps="", networks=None, mem=8 * 1024**3, info_rc=0):
    networks = networks or []

    def run(args):
        if args[0] == "info":
            return subprocess.CompletedProcess(args, info_rc, json.dumps(mem), "")
        if args[0] == "ps":
            return subprocess.CompletedProcess(args, 0, ps, "")
        if args[:2] == ["network", "ls"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(n["Id"] for n in networks), "")
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(networks), "")
        raise AssertionError(args)
    return run


def by_name(checks):
    return {c.name: c for c in checks}


def test_clean_machine_passes():
    checks = by_name(run_checks(**BASE, docker=fake_docker(), in_use=lambda p: False))
    assert all(c.ok for c in checks.values())


def test_legacy_stack_is_detected_with_the_fix():
    ps = "crdb-e1a\tcockroach-chaos-demo\nhaproxy\tcockroach-chaos-demo\nchaos-backend\tcockroach-chaos-demo\n"
    c = by_name(run_checks(**BASE, docker=fake_docker(ps=ps), in_use=lambda p: True))
    assert not c["legacy"].ok
    assert "./run.sh down --legacy" in c["legacy"].message and "crdb-e1a" in c["legacy"].message
    assert "ports" not in c


def test_our_own_haproxy_is_not_legacy():
    ps = "haproxy\tcrdb-chaos\ncrdb-chaos-control-plane\tcrdb-chaos\n"
    c = by_name(run_checks(**BASE, docker=fake_docker(ps=ps), in_use=lambda p: True))
    assert c["legacy"].ok and "ports" not in c


def test_busy_ports_are_named():
    c = by_name(run_checks(**BASE, docker=fake_docker(), in_use=lambda p: p in (8080, 8088)))
    assert not c["ports"].ok and "8080, 8088" in c["ports"].message


def test_overlapping_subnet_names_the_network_and_suggests_another():
    nets = [{"Id": "n1", "Name": "vpn-bridge", "IPAM": {"Config": [{"Subnet": "10.77.4.0/24"}]}},
            {"Id": "n2", "Name": "crdb-chaos", "IPAM": {"Config": [{"Subnet": "10.77.0.0/16"}]}}]
    c = by_name(run_checks(**BASE, docker=fake_docker(networks=nets), in_use=lambda p: False))
    assert not c["subnet"].ok
    assert "vpn-bridge (10.77.4.0/24)" in c["subnet"].message and "CHAOS_SUBNET=" in c["subnet"].message


def test_low_memory_is_a_warning_not_an_error():
    c = by_name(run_checks(**BASE, docker=fake_docker(mem=2 * 1024**3), in_use=lambda p: False))
    assert not c["memory"].ok and c["memory"].level == "warn" and "--preset small" in c["memory"].message


def test_unreachable_runtime_stops_early():
    checks = run_checks(**BASE, docker=fake_docker(info_rc=1), in_use=lambda p: False)
    assert len(checks) == 1 and not checks[0].ok and "not reachable" in checks[0].message


def test_preset_nodes_sums_block_and_flow_styles(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text("regions:\n  - { name: a1, nodes: 3 }\n  - name: b1\n    nodes: 2\n", encoding="utf-8")
    assert preset_nodes(p) == 5


def test_main_exits_nonzero_only_on_errors(tmp_path, monkeypatch):
    p = tmp_path / "p.yaml"
    p.write_text("regions:\n  - { name: a1, nodes: 1 }\n", encoding="utf-8")
    args = ["--preset-file", str(p), "--subnet", "10.77.0.0/16", "--panel-port", "8088"]
    monkeypatch.setattr(preflight, "run_checks", lambda **kw: [Check("memory", False, "low", level="warn")])
    assert main(args) == 0
    monkeypatch.setattr(preflight, "run_checks", lambda **kw: [Check("ports", False, "busy")])
    assert main(args) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_preflight.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'preflight'`

- [ ] **Step 3: Implement** `tools/preflight.py`

```python
#!/usr/bin/env python3
"""Host-side checks before ./run.sh starts the stack. Standard library only."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PROJECT = "crdb-chaos"
LEGACY_NAMES = {"crdb-e1a", "crdb-e1b", "crdb-w2a", "crdb-w2b", "crdb-c1", "crdb-init", "crdb-bootstrap",
                "toxiproxy-east", "toxiproxy-west", "toxiproxy-central", "chaos-backend"}
NODE_MIB, BASE_MIB = 450, 700


@dataclass
class Check:
    name: str
    ok: bool
    message: str
    level: str = "error"  # error | warn


def docker_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def preset_nodes(path: Path) -> int:
    return sum(int(n) for n in re.findall(r"\bnodes:\s*(\d+)", path.read_text(encoding="utf-8")))


def run_checks(*, nodes: int, subnet: str, panel_port: int,
               docker: Callable[[list[str]], subprocess.CompletedProcess] = docker_cli,
               in_use: Callable[[int], bool] = port_in_use) -> list[Check]:
    info = docker(["info", "--format", "{{json .MemTotal}}"])
    if info.returncode != 0:
        return [Check("runtime", False, "Docker/Podman is not reachable. Start Docker Desktop or run `podman machine start`.")]
    checks = [Check("runtime", True, "runtime reachable")]

    listing = docker(["ps", "-a", "--format", '{{.Names}}\t{{.Label "com.docker.compose.project"}}'])
    rows = [(line.split("\t") + [""])[:2] for line in listing.stdout.splitlines() if line.strip()]
    ours = any(project == PROJECT for _, project in rows)
    legacy = sorted(name for name, project in rows if name in LEGACY_NAMES or (name == "haproxy" and project != PROJECT))
    if legacy:
        checks.append(Check("legacy", False, f"The pre-redesign demo is still running ({', '.join(legacy)}). "
                                             "Stop it with: ./run.sh down --legacy"))
    else:
        checks.append(Check("legacy", True, "no pre-redesign demo running"))

    if not ours and not legacy:
        busy = [p for p in (26257, 8080, 8404, panel_port) if in_use(p)]
        checks.append(Check("ports", not busy,
                            f"ports in use by another program: {', '.join(map(str, busy))}" if busy
                            else f"ports 26257, 8080, 8404 and {panel_port} are free"))

    try:
        wanted = ipaddress.ip_network(subnet)
    except ValueError:
        checks.append(Check("subnet", False, f"CHAOS_SUBNET {subnet!r} is not a valid network"))
    else:
        clashes = []
        ids = docker(["network", "ls", "-q"]).stdout.split()
        if ids:
            for net in json.loads(docker(["network", "inspect", *ids]).stdout or "[]"):
                if net.get("Name") == PROJECT:
                    continue
                for cfg in (net.get("IPAM") or {}).get("Config") or []:
                    cidr = cfg.get("Subnet")
                    if cidr and ipaddress.ip_network(cidr, strict=False).overlaps(wanted):
                        clashes.append(f"{net.get('Name')} ({cidr})")
        checks.append(Check("subnet", not clashes,
                            f"CHAOS_SUBNET {subnet} overlaps {', '.join(clashes)}; set another /16 in .env, "
                            "e.g. CHAOS_SUBNET=10.88.0.0/16" if clashes else f"subnet {subnet} is free"))

    try:
        total_mib = int(json.loads(info.stdout or "0")) // (1024 * 1024)
    except (ValueError, TypeError):
        total_mib = 0
    need = nodes * NODE_MIB + BASE_MIB
    enough = not total_mib or total_mib >= need
    checks.append(Check("memory", enough,
                        f"memory looks sufficient for {nodes} nodes" if enough
                        else f"~{need} MiB recommended for {nodes} nodes but the runtime has {total_mib} MiB; "
                             "consider --preset small", level="warn"))
    return checks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset-file", type=Path, required=True)
    parser.add_argument("--subnet", required=True)
    parser.add_argument("--panel-port", type=int, required=True)
    args = parser.parse_args(argv)
    failed = False
    for c in run_checks(nodes=preset_nodes(args.preset_file), subnet=args.subnet, panel_port=args.panel_port):
        print(f"  {'✓' if c.ok else ('!' if c.level == 'warn' else '✗')} {c.message}")
        failed |= not c.ok and c.level == "error"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_preflight.py -q`
Expected: `9 passed`

- [ ] **Step 5: Rewrite** `run.sh`

```bash
#!/usr/bin/env bash
# Usage: ./run.sh up [--preset NAME] | down [--legacy] | reset [--preset NAME] | status | prefetch | test [pytest args]
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then set -a; . ./.env; set +a; fi
PANEL_PORT="${PANEL_PORT:-8088}"
CHAOS_SUBNET="${CHAOS_SUBNET:-10.77.0.0/16}"
CRDB_VERSION="${CRDB_VERSION:-v26.3.2}"
export PANEL_PORT CHAOS_SUBNET CRDB_VERSION

if command -v docker >/dev/null 2>&1; then DOCKER=docker
elif command -v podman >/dev/null 2>&1; then DOCKER=podman
else echo "Neither docker nor podman found. Install one first." >&2; exit 1; fi
if $DOCKER compose version >/dev/null 2>&1; then DC="$DOCKER compose"
elif command -v docker-compose >/dev/null 2>&1; then DC="docker-compose"
else echo "docker compose is not available." >&2; exit 1; fi

PANEL="http://localhost:${PANEL_PORT}"
LEGACY_PROJECT="cockroach-chaos-demo"
LEGACY_CONTAINERS="crdb-e1a crdb-e1b crdb-w2a crdb-w2b crdb-c1 crdb-init crdb-bootstrap toxiproxy-east toxiproxy-west toxiproxy-central chaos-backend"

# Prints "operation-status|cluster-status|preset|operation-error" from /api/state.
read -r -d '' STATE_PY <<'PY' || true
import json, sys
d = json.load(sys.stdin)
op = d.get("operation") or {}
c = d.get("cluster") or {}
print(op.get("status", ""), c.get("status", ""), c.get("preset", ""), (op.get("error") or "").replace("\n", " "), sep="|")
PY

read -r -d '' STATUS_PY <<'PY' || true
import json, sys
d = json.load(sys.stdin)
c = d.get("cluster")
if not c:
    print("No cluster yet.")
    sys.exit()
print(f"{c['display_name']} ({c['preset']}): {c['status']}, {c['live_nodes']}/{c['total_nodes']} nodes live, survival goal {c['survival_goal']}")
for r in d["regions"]:
    print(f"  {r['name']:<16} {r['state']:<9} {len(r['nodes'])} nodes")
op = d.get("operation") or {}
if op.get("status") == "failed":
    print(f"Last operation failed: {op.get('error')}")
PY

usage() { sed -n '2p' "$0" | sed 's/^# //'; exit 1; }

preset_file() {
  if [ -f "presets/$1.yaml" ]; then echo "presets/$1.yaml"
  elif [ -f "presets/custom/$1.yaml" ]; then echo "presets/custom/$1.yaml"
  else
    echo "Unknown preset '$1'. Available: $(cd presets && ls *.yaml | sed 's/\.yaml$//' | tr '\n' ' ')" >&2
    exit 1
  fi
}

cluster_state() { curl -fsS "$PANEL/api/state" 2>/dev/null | python3 -c "$STATE_PY" 2>/dev/null || echo "|||"; }

wait_ready() {
  local want="$1" asked=0 op cl preset err
  printf 'Waiting for the cluster'
  for _ in $(seq 1 600); do
    IFS='|' read -r op cl preset err <<<"$(cluster_state)"
    if [ "$op" = "failed" ]; then echo; echo "Provisioning failed: $err" >&2; exit 1; fi
    if [ -n "$preset" ] && [ "$preset" != "$want" ] && [ "$op" != "running" ] && [ "$asked" = 0 ]; then
      echo; echo "The running cluster uses preset '$preset'; rebuilding it as '$want' (this wipes cluster data)."
      curl -fsS -X POST "$PANEL/api/cluster/rebuild" -H 'Content-Type: application/json' -d "{\"preset\":\"$want\"}" >/dev/null
      asked=1
    elif [ "$preset" = "$want" ] && [ "$op" = "done" ] && [ "$cl" = "healthy" ]; then
      echo; echo "Ready: preset '$want'."
      echo "  Stage:       $PANEL"
      echo "  SQL (Banko): postgresql://root@localhost:26257/defaultdb?sslmode=disable"
      echo "  DB Console:  http://localhost:8080"
      return 0
    fi
    printf '.'
    sleep 1
  done
  echo; echo "Timed out waiting for the cluster. Check: $DC logs control-plane" >&2; exit 1
}

cmd_up() {
  local preset="${PRESET:-aws}"
  while [ $# -gt 0 ]; do
    case "$1" in --preset) preset="${2:?--preset needs a name}"; shift 2;; *) usage;; esac
  done
  local file; file="$(preset_file "$preset")"
  echo "Preflight:"
  python3 tools/preflight.py --preset-file "$file" --subnet "$CHAOS_SUBNET" --panel-port "$PANEL_PORT"
  PRESET="$preset" $DC up -d --build
  wait_ready "$preset"
}

cmd_down() {
  if [ "${1:-}" = "--legacy" ]; then
    echo "Removing the pre-redesign demo..."
    $DOCKER rm -f $LEGACY_CONTAINERS >/dev/null 2>&1 || true
    if [ "$($DOCKER inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' haproxy 2>/dev/null || true)" = "$LEGACY_PROJECT" ]; then
      $DOCKER rm -f haproxy >/dev/null
    fi
    $DOCKER network rm "${LEGACY_PROJECT}_default" >/dev/null 2>&1 || true
    echo "Done. Start the new stack with: ./run.sh up"
    return
  fi
  local ids; ids="$($DOCKER ps -aq --filter label=crdb-chaos.node=1)"
  if [ -n "$ids" ]; then $DOCKER rm -f $ids >/dev/null; fi
  $DC down -v --remove-orphans
  echo "Stopped."
}

cmd_status() { curl -fsS "$PANEL/api/state" | python3 -c "$STATUS_PY"; }

cmd_prefetch() {
  $DOCKER pull "docker.io/cockroachdb/cockroach:${CRDB_VERSION}"
  $DOCKER pull docker.io/library/haproxy:lts
  $DC build
}

cmd_test() {
  if [ ! -x .venv/bin/pytest ]; then
    python3 -m venv .venv
    .venv/bin/pip install -q -r controlplane/requirements-dev.txt
  fi
  .venv/bin/pytest "$@"
}

case "${1:-}" in
  up) shift; cmd_up "$@";;
  down) shift; cmd_down "$@";;
  reset) shift; cmd_down; cmd_up "$@";;
  status) cmd_status;;
  prefetch) cmd_prefetch;;
  test) shift; cmd_test "$@";;
  *) usage;;
esac
```

- [ ] **Step 6: Check the script**

Run: `bash -n run.sh && ./run.sh; echo "exit=$?"; ./run.sh test -q`
Expected: no syntax errors; the usage line followed by `exit=1`; then the whole unit suite passes.

- [ ] **Step 7: Commit**

```bash
git add tools/preflight.py tests/unit/test_preflight.py run.sh
git commit -m "Add preflight checks and preset-aware run.sh commands" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Retire the legacy stack files and refresh the README

**Files:**
- Delete: `backend/` (the rest of it), `toxiproxy/`, `init.sql`, `haproxy.cfg`
- Modify (full rewrite): `README.md`

**Interfaces:**
- Consumes: commands and endpoints from Tasks 10–13
- Produces: a README that is accurate for M1. The full rewrite comes in M6.

- [ ] **Step 1: Remove the replaced files**

Run: `git rm -r -q backend toxiproxy init.sql haproxy.cfg && git status --short`
Expected: deletions only. `web/shared/favicon.ico` was already moved in Task 12. The untracked `DEMO_SUMMARY.txt` and `TALKING_POINTS.md` are not touched.

- [ ] **Step 2: Rewrite** `README.md`

```markdown
# CockroachDB Multi-Region Chaos Demo

A live, cloud-looking CockroachDB cluster on your laptop. A control plane builds the cluster from a
**preset** with whatever region names you need (AWS, Google Cloud, Azure, IBM, multi-cloud or your own),
puts HAProxy in front of it, and shows live state on a stage page for big screens.

> Redesign in progress on `feature/control-plane`. Milestone 1 (this state) provisions clusters from presets.
> Chaos controls, the presenter console, the workload and scenarios arrive in milestones 2–5.
> Design: [`docs/superpowers/specs/2026-10-09-chaos-control-plane-design.md`](docs/superpowers/specs/2026-10-09-chaos-control-plane-design.md)

## Quick start

    ./run.sh up                 # default preset: aws (3 regions × 3 nodes)
    ./run.sh up --preset gcp    # rebuilds the cluster with Google Cloud regions
    ./run.sh status
    ./run.sh down

| Endpoint | URL |
|---|---|
| Stage | http://localhost:8088 |
| SQL (Banko and any Postgres client) | `postgresql://root@localhost:26257/defaultdb?sslmode=disable` |
| DB Console (stays up when nodes die) | http://localhost:8080 |
| HAProxy stats | http://localhost:8404/stats |

## Presets

| Preset | Regions (nodes) | Survival goal |
|---|---|---|
| `aws` | us-east-1 (3), us-west-2 (3), eu-west-1 (3) | region |
| `gcp` | us-east1 (3), us-central1 (3), europe-west1 (3) | region |
| `azure` | eastus (3), westus2 (3), westeurope (3) | region |
| `ibm` | us-south (3), us-east (3), eu-de (3) | region |
| `multi-cloud` | aws us-east-1 (3), gcp us-central1 (3), azure westus2 (3) | region |
| `classic` | us-east-1 (2), us-west-2 (2), us-east-2 (1) | region |
| `small` | us-east-1, us-west-2, eu-west-1 (1 each), for 8 GB laptops | none (plain 3× replication) |

To make your own, copy a file in `presets/` to `presets/custom/<name>.yaml`, edit the regions, and run
`./run.sh up --preset <name>`. Real zone names and map coordinates come from `catalog/regions.yaml`.
Unknown regions work too; add `location: [lat, lon]` to place them on maps.

## Upgrading from the previous version

The old stack (`crdb-e1a`, toxiproxy, `chaos-backend`) holds the same ports. `./run.sh up` detects it and
asks you to run:

    ./run.sh down --legacy

If your `.env` pins an old `CRDB_VERSION`, remove that line to use the default (v26.3.2).

## Banko AI Assistant

Point Banko at the SQL URL above. This repo configures `defaultdb` as a multi-region database, and Banko
detects the regions automatically. Switching presets rebuilds the cluster and wipes data, so restart Banko afterwards.

## Podman

Rootless Podman works. If `/var/run/docker.sock` isn't linked to the Podman socket, set `DOCKER_SOCKET`
in `.env` (see `.env.example`).

## Development

    ./run.sh test                              # unit tests (creates .venv on first run)
    CHAOS_INTEGRATION=1 .venv/bin/pytest tests/integration -m integration -v   # rebuilds the local cluster

The `demo_*.py` scripts predate the redesign; panel scenarios replace them in milestone 5.

**Security note:** insecure mode, for demos only.
```

- [ ] **Step 3: Check that nothing still references the removed files**

Run: `git grep -n -E "toxiproxy|init\.sql|backend/|chaos-backend" -- ':!docs/superpowers' ':!tools/preflight.py' ':!run.sh' ':!tests/unit/test_preflight.py' ':!README.md'`
Expected: no output. (The legacy names stay only where they detect or remove the old stack.)

- [ ] **Step 4: Run the unit suite**

Run: `./run.sh test -q`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add -A README.md
git commit -m "Remove the toxiproxy stack and document the preset workflow" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: End-to-end verification on the local runtime

**Files:**
- Create: `tests/integration/test_stack.py`

**Interfaces:**
- Consumes: everything above, running for real
- Produces: the M1 acceptance evidence: integration tests passing on rootless Podman, plus screenshots of the stage.

- [ ] **Step 1: Write the integration tests** at `tests/integration/test_stack.py`

```python
"""End-to-end checks against a real stack. Rebuilds the local cluster: opt in with CHAOS_INTEGRATION=1."""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import psycopg
import pytest

from controlplane.sqlconn import connect

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PANEL = f"http://localhost:{os.environ.get('PANEL_PORT', '8088')}"
CLASSIC_REGIONS = {"us-east-1", "us-west-2", "us-east-2"}


def state():
    with urllib.request.urlopen(f"{PANEL}/api/state", timeout=5) as resp:
        return json.load(resp)


def wait_for(predicate, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            snap = state()
            if predicate(snap):
                return snap
        except OSError:
            pass
        time.sleep(2)
    raise AssertionError("condition not met in time")


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="module")
def classic():
    if os.environ.get("CHAOS_INTEGRATION") != "1":
        pytest.skip("set CHAOS_INTEGRATION=1 (rebuilds the local cluster as the 'classic' preset)")
    subprocess.run([str(ROOT / "run.sh"), "up", "--preset", "classic"], cwd=ROOT, check=True, timeout=1200)
    return state()


def test_every_node_is_live_with_preset_names(classic):
    c = classic["cluster"]
    assert (c["preset"], c["status"], c["live_nodes"], c["total_nodes"]) == ("classic", "healthy", 5, 5)
    assert {r["name"] for r in classic["regions"]} == CLASSIC_REGIONS
    assert [n["name"] for n in classic["nodes"]] == ["us-east-1a-n1", "us-east-1b-n2", "us-west-2a-n1", "us-west-2b-n2", "us-east-2a-n1"]


def test_sql_through_haproxy_sees_the_multi_region_databases(classic):
    with psycopg.connect(host="localhost", port=26257, user="root", dbname="defaultdb", sslmode="disable", autocommit=True) as conn:
        regions = {r[0]: (r[1], r[2]) for r in conn.execute('SELECT region, "primary", secondary FROM [SHOW REGIONS FROM DATABASE defaultdb]')}
        assert set(regions) == CLASSIC_REGIONS
        assert regions["us-east-1"][0] is True and regions["us-west-2"][1] is True
        assert conn.execute("SELECT survival_goal FROM [SHOW SURVIVAL GOAL FROM DATABASE chaos]").fetchone()[0] == "region"
        assert conn.execute("SHOW CLUSTER SETTING kv.rangefeed.enabled").fetchone()[0] is True


def test_map_locations_were_written(classic):
    with connect("localhost") as conn:
        count = conn.execute('SELECT count(*) FROM system.locations WHERE "localityKey" = %s AND "localityValue" = ANY(%s)',
                             ("region", list(CLASSIC_REGIONS))).fetchone()[0]
    assert count == 3


def test_db_console_is_served_through_the_load_balancer(classic):
    with urllib.request.urlopen("http://localhost:8080/health", timeout=5) as resp:
        assert resp.status == 200


def test_control_plane_restart_adopts_the_running_nodes(classic):
    names = [n["name"] for n in classic["nodes"]]
    before = docker("inspect", "-f", "{{.Id}}", *names).split()
    docker("restart", "crdb-chaos-control-plane")
    snap = wait_for(lambda s: (s.get("operation") or {}).get("kind") == "adopt"
                    and s["operation"]["status"] == "done" and (s.get("cluster") or {}).get("status") == "healthy")
    assert snap["cluster"]["preset"] == "classic"
    assert docker("inspect", "-f", "{{.Id}}", *names).split() == before
```

- [ ] **Step 2: Check for a stale `.env`**

Run: `grep -nE '^(CRDB_VERSION|PRESET|CHAOS_SUBNET)=' .env 2>/dev/null || echo "no overrides"`
Expected: `no overrides`. If `CRDB_VERSION=v25.4.x` appears, **ask the user** whether to remove that line. `.env` is their untracked local file, and v26.3.2 is the default.

- [ ] **Step 3: CHECKPOINT. Ask the user before stopping their running pre-redesign demo**, then run:

Run: `./run.sh down --legacy`
Expected: `Done. Start the new stack with: ./run.sh up`

- [ ] **Step 4: Start the default preset**

Run: `./run.sh up`
Expected: preflight shows all ✓ (memory may show `!`), the build completes, then `Ready: preset 'aws'.` with the three URLs. First provisioning takes about 1–3 minutes.

- [ ] **Step 5: Look at the stage in both themes**

Run: `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --window-size=1920,1080 --virtual-time-budget=4000 --screenshot=/tmp/stage-dark.png http://localhost:8088/ && "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --window-size=1920,1080 --virtual-time-budget=4000 --screenshot=/tmp/stage-light.png "http://localhost:8088/?theme=light"`
Expected: the screenshots show the "Global Payments" header, `AWS · 9 nodes`, `Survives region failure`, KPIs `9/9`, `0`, `0`, `Healthy`, and three region cards with green node tiles named `us-east-1a-n1`… Open both PNGs and check legibility.

- [ ] **Step 6: Run the integration suite**

Run: `CHAOS_INTEGRATION=1 .venv/bin/pytest tests/integration -m integration -v`
Expected: `5 passed`. This rebuilds the cluster as `classic`, and the restart test exercises adoption.

- [ ] **Step 7: Banko-style smoke test through HAProxy**

Run: `.venv/bin/python -c "import psycopg; c = psycopg.connect('postgresql://root@localhost:26257/defaultdb?sslmode=disable', autocommit=True); c.execute('CREATE TABLE IF NOT EXISTS smoke (id INT PRIMARY KEY)'); c.execute('UPSERT INTO smoke VALUES (1)'); print(c.execute('SELECT count(*) FROM smoke').fetchone()[0]); c.execute('DROP TABLE smoke')"`
Expected: `1`

- [ ] **Step 8 (optional, about 500 MB download): v25.4 compatibility**

Run: `CRDB_VERSION=v25.4.14 ./run.sh reset --preset small && ./run.sh status`
Expected: `Ready: preset 'small'.`, then a status line with `healthy, 3/3 nodes live, survival goal none`.

- [ ] **Step 9: Leave the default running**

Run: `./run.sh reset --preset aws`
Expected: `Ready: preset 'aws'.`

- [ ] **Step 10: Commit**

```bash
git add tests/integration/test_stack.py
git commit -m "Add end-to-end checks for provisioning, bootstrap and adoption" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Out of scope for M1 (next plans)

- **M2:** chaos actions, netem latency/brownout/isolation, add/decommission nodes, add/remove regions, survival budget, booth mode, triggers and keyboard, `CONTROL_TOKEN` / `TRIGGER_TOKEN`.
- **M3:** layout B at 1920×1080 scale, the presenter console, the cloud-illusion pass, and the explainers moved into `web/explainers/` with the exporter.
- **M4:** cluster-side metrics, the Payments workload, the $0-lost invariant and recovery time.
- **M5:** Tier 1–2 scenarios and retiring the `demo_*.py` scripts.
- **M6:** the full README and docs, plus integration on Docker Desktop.

## Self-review notes

- **Spec coverage (§16.1):**
  - compose and network: Tasks 11 and 3
  - provisioner and adoption: Task 8
  - bootstrap with the v26 fix: Task 7
  - presets and catalog: Tasks 2–3
  - HAProxy generator: Task 6
  - snapshot, SSE and the minimal stage: Tasks 9, 10 and 12
  - `run.sh` with preflight: Task 13

  Cleanup (§15) is in Task 14, and §12 reliability items for M1 (verification, graceful telemetry, restart safety, preflight) are in Tasks 6, 8, 9 and 13.
- **Defects fixed in M1:** D1 (Task 7), D3 (Task 9 liveness), D4 (single process, Tasks 9–10), D5 (fixed network and verified operations), D9 (single version source), D10–D11 (Task 11), D12 (Task 6 console frontend), D13 (presets), D14 (replicas from `gossip_nodes.ranges`), D15 (Task 13). D2, D6–D8 and D16 belong to M2–M5.
- **Review Focus coverage:**
  1. legacy stack: Task 13 test
  2. subnet: Task 13 test
  3. preset typos: Task 3 and Task 10 tests
  4. pull failure: Task 8 test, plus `run.sh` printing it
  5. half-built cluster: Task 8 test


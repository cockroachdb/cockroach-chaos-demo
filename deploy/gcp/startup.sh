#!/usr/bin/env bash
# Runs as root on every boot of the demo VM: installs Docker once, unpacks the
# demo, brings the stack up, grows the cluster, and then builds and starts
# Banko beside it, with its pipeline. Safe to run again.
#
# Payments are left off, as they are on a laptop: whoever presents starts
# them from the stage.
set -euo pipefail

# The startup-script runner sets no HOME, and docker and gcloud both want one.
export HOME=/root

DIR=/opt/cockroach-chaos-demo
PANEL=http://localhost:8088
BANKO_DIR=/opt/banko
BANKO=http://localhost:5000

attr() {
  curl -fsS -H 'Metadata-Flavor: Google' \
    "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"
}

install_docker() {
  command -v docker >/dev/null && return

  apt-get update
  apt-get install -y ca-certificates curl python3
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
}

# A new version of the demo arrives as a new VM, so what is here is current.
unpack() {
  [ -f "$DIR/run.sh" ] && return

  mkdir -p "$DIR"
  gcloud storage cp "$(attr demo-archive)" /tmp/demo.tar.gz
  tar -xzf /tmp/demo.tar.gz -C "$DIR"
}

# No CONTROL_TOKEN: the sign-in in front is the gate, and behind it the stage
# is meant to be driven by whoever is presenting, as it is on a laptop.
#
# One thing is switched off: rebuilding the cluster. Everybody shares it and
# Banko banks on it, and a rebuild wipes every database: Banko's goes, and its
# pipeline's changefeed with it. A new cluster comes with a new VM.
write_env() {
  local license
  license="$(attr crdb-license)"

  (
    umask 077
    {
      echo "PRESET=$(attr preset)"
      echo "CRDB_VERSION=$(attr crdb-version)"
      [ -z "$license" ] || echo "CRDB_LICENSE=$license"
      echo "REBUILD=off"
    } > "$DIR/.env"
  )
}

# The presets have two nodes per region and several scenarios want three, so
# nodes are added the way the stage's own button adds them. Regions that
# already have enough are left alone, which is what makes a reboot harmless.
grow() {
  python3 - "$PANEL" "$(attr nodes-per-region)" <<'PY'
import json
import sys
import time
import urllib.request

panel, want = sys.argv[1], int(sys.argv[2])


def state():
    with urllib.request.urlopen(f"{panel}/api/state", timeout=10) as r:
        return json.load(r)


def nodes_in(region):
    return len(next(r for r in state()["regions"] if r["name"] == region)["nodes"])


def add_node(region):
    body = json.dumps({"region": region}).encode()
    req = urllib.request.Request(f"{panel}/api/cluster/nodes", data=body,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).close()


def settle(total):
    for _ in range(600):
        s = state()
        op, cluster = s.get("operation") or {}, s["cluster"]
        if op.get("status") == "failed":
            sys.exit(f"adding a node failed: {op.get('error')}")
        if op.get("status") == "done" and cluster["live_nodes"] == total:
            return
        time.sleep(2)
    sys.exit(f"the cluster did not reach {total} live nodes in 20 minutes")


for region in [r["name"] for r in state()["regions"]]:
    while nodes_in(region) < want:
        total = state()["cluster"]["total_nodes"] + 1
        add_node(region)
        settle(total)
        print(f"{region}: {nodes_in(region)} nodes", flush=True)
PY
}

# Banko banks on the demo's own cluster, so a region taken down on the stage
# is something its customers live through. It is built here because its
# published image is a major version behind the repository.
#
# The limits are what keep it from starving the database nodes it shares the
# VM with: embeddings run on the CPU and would otherwise take every core.
start_banko() {
  if [ ! -f "$BANKO_DIR/src/Dockerfile" ]; then
    mkdir -p "$BANKO_DIR/src"
    gcloud storage cp "$(attr banko-archive)" /tmp/banko.tar.gz
    tar -xzf /tmp/banko.tar.gz -C "$BANKO_DIR/src"
  fi
  docker build -t banko:local "$BANKO_DIR/src"

  # Turns on the coach's signed webhook, which Banko's own checks post to.
  # Nothing outside the VM can reach it, so the secret is made here and never
  # leaves.
  local webhook_secret
  webhook_secret="$(openssl rand -hex 32)"

  # Behind the sign-in in front, Banko signs each person in as who Google
  # says they are, once it is told which service that sign-in vouches for.
  # "off" leaves its username page, where anyone can be anyone.
  local iap_audience
  iap_audience="$(attr banko-iap-audience)"

  (
    umask 077
    {
      echo "DATABASE_URL=cockroachdb://root@haproxy:26257/defaultdb?sslmode=disable"
      echo "CHAOS_CONTROL_URL=http://crdb-chaos-control-plane:8088"
      # The demo's regions are a realistic distance apart, and under row-level
      # security every statement first asks a system range that may be far away.
      echo "RLS_MODE=off"
      echo "SECRET_KEY=$(attr banko-secret-key)"
      echo "CDC_WEBHOOK_HMAC_SECRET=$webhook_secret"
      # The coach also reads signals from the pipeline start_pipeline brings
      # up. Banko keeps asking for the brokers until they are there.
      echo "COACH_KAFKA_ENABLED=true"
      echo "KAFKA_BOOTSTRAP_SERVERS=kafka:9092"
      echo "SESSION_COOKIE_SECURE=true"
      [ "$iap_audience" = off ] || echo "IAP_AUDIENCE=$iap_audience"
      echo "OMP_NUM_THREADS=2"
      attr banko-env
      echo
    } > "$BANKO_DIR/env"
  )

  docker rm -f banko > /dev/null 2>&1 || true
  docker run -d --name banko --restart unless-stopped --network crdb-chaos -p 5000:5000 \
    --cpus 2 --memory 6g --env-file "$BANKO_DIR/env" banko:local > /dev/null

  # Ready means the schema and the personas are in place, which takes a while
  # on a cluster with real distances in it.
  for _ in $(seq 1 360); do
    curl -fsS "$BANKO/api/health/ready" > /dev/null 2>&1 && return 0
    sleep 5
  done
  echo "Banko was not ready after 30 minutes" >&2
  return 1
}

# Banko's own pipeline: a changefeed on its signals table into Kafka, and
# Debezium routing that to the topic the coach reads. Banko's script brings it
# up and puts Kafka on the demo's network under the name the changefeed uses,
# so no node joins a second network, which the stage is not built for.
start_pipeline() {
  command -v unzip > /dev/null || apt-get install -y unzip
  "$BANKO_DIR/src/scripts/coach/cdc-demo/run-cdc-demo.sh"
}

# Latency between regions is shaped with netem. Without the module the stack
# still starts, and every region is silently next door.
modprobe sch_netem || { echo "sch_netem is not available in this kernel" >&2; exit 1; }

install_docker
unpack
write_env
cd "$DIR"
./run.sh up
grow
start_banko
start_pipeline

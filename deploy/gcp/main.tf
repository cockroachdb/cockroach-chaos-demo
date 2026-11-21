terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.12"
    }
    # Only for the IAP service agent, which the GA provider cannot create.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 8.12"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # The state holds the license key and Banko's keys, and losing it strands
  # billable resources, so it lives in a bucket rather than on a laptop. Which
  # bucket is said at init, from backend.hcl.
  backend "gcs" {
    prefix = "chaos-demo"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

locals {
  name       = "chaos-demo"
  panel_port = 8088

  # Clear of the demo's own Docker network, which is 10.77.0.0/16 by default.
  subnet_cidr = "10.90.0.0/24"
  vm_ip       = "10.90.0.10"

  # What the presenter's links open besides the stage. On a laptop these are
  # ports on localhost; a run.app address has no ports to offer, so each gets
  # a proxy and a sign-in of its own.
  extras = {
    console = { name = "${local.name}-console", port = 8080 } # the DB Console, through HAProxy to whichever node is up
    lb      = { name = "${local.name}-lb", port = 8404 }      # HAProxy's stats, at /stats
    banko   = { name = "banko-demo", port = 5000 }            # Banko, banking on the same cluster
  }
}

# The sign-in in front of Banko signs who each visitor is into every request,
# and names the service it signed for. This is that name, which Banko checks
# before it believes the rest. "off" leaves Banko's own username page.
data "google_project" "this" {}

locals {
  banko_iap_audience = var.banko_sign_in ? "/projects/${data.google_project.this.number}/locations/${var.region}/services/${local.extras.banko.name}" : "off"
}

resource "google_project_service" "this" {
  for_each = toset(["compute.googleapis.com", "iap.googleapis.com", "run.googleapis.com"])

  service            = each.value
  disable_on_destroy = false
}

# --- A private network: no public address on the VM, NAT for its image pulls ---

resource "google_compute_network" "this" {
  name                    = local.name
  auto_create_subnetworks = false

  depends_on = [google_project_service.this]
}

resource "google_compute_subnetwork" "this" {
  name          = local.name
  network       = google_compute_network.this.id
  ip_cidr_range = local.subnet_cidr
}

resource "google_compute_router" "this" {
  name    = local.name
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  name                               = local.name
  router                             = google_compute_router.this.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

# The proxies reach the VM from addresses in the subnet (direct VPC egress).
# Only what they forward is opened: SQL stays closed.
resource "google_compute_firewall" "proxy" {
  name          = "${local.name}-proxy"
  network       = google_compute_network.this.id
  source_ranges = [local.subnet_cidr]
  target_tags   = [local.name]

  allow {
    protocol = "tcp"
    ports    = concat([local.panel_port], [for e in local.extras : e.port])
  }
}

# SSH only through IAP's forwarding range: gcloud compute ssh --tunnel-through-iap.
resource "google_compute_firewall" "iap_ssh" {
  name          = "${local.name}-iap-ssh"
  network       = google_compute_network.this.id
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.name]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# --- The VM that runs the demo ---

resource "google_service_account" "vm" {
  account_id   = "${local.name}-vm"
  display_name = "Chaos demo VM"
}

resource "google_project_iam_member" "vm" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"])

  project = var.project_id
  role    = each.value
  member  = google_service_account.vm.member
}

# --- The demo itself: an archive of the branch, packed by tf.sh ---

# Beside each archive tf.sh writes the ID git gives what it holds. That ID,
# and not the archive's own bytes, decides when the VM is replaced: it is the
# same on every machine, and for every commit that leaves the packed files
# alone.
locals {
  archive    = "${path.module}/.build/demo.tar.gz"
  archive_id = trimspace(file("${path.module}/.build/demo.id"))

  banko_archive    = "${path.module}/.build/banko.tar.gz"
  banko_archive_id = trimspace(file("${path.module}/.build/banko.id"))
}

resource "google_storage_bucket" "demo" {
  name                        = "${var.project_id}-chaos-demo"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
}

# Named by what it holds, so a change to the packed files is a new object and
# a new VM.
resource "google_storage_bucket_object" "demo" {
  bucket = google_storage_bucket.demo.name
  name   = "demo-${substr(local.archive_id, 0, 16)}.tar.gz"
  source = local.archive
}

resource "google_storage_bucket_object" "banko" {
  bucket = google_storage_bucket.demo.name
  name   = "banko-${substr(local.banko_archive_id, 0, 16)}.tar.gz"
  source = local.banko_archive
}

# Signs Banko's session cookies. Kept here so sign-ins survive a rebuild.
resource "random_password" "banko_secret_key" {
  length  = 48
  special = false
}

resource "google_storage_bucket_iam_member" "vm_reads_demo" {
  bucket = google_storage_bucket.demo.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.vm.member
}

# Changing metadata alone would not rerun the startup script, so a change to
# anything it reads replaces the VM instead: about ten minutes, and the
# cluster is rebuilt from exactly what was asked for.
#
# The license goes in as a fingerprint: this resource prints its input in
# plans, and the key itself has no business on a terminal.
resource "terraform_data" "startup_inputs" {
  input = [
    local.archive_id,
    local.banko_archive_id,
    var.preset,
    var.nodes_per_region,
    var.crdb_version,
    nonsensitive(sha256(var.crdb_license)),
    nonsensitive(sha256(jsonencode(var.banko_env))),
  ]
}

# Reserved, so the proxy's target survives the VM being replaced.
resource "google_compute_address" "vm" {
  name         = local.name
  address_type = "INTERNAL"
  subnetwork   = google_compute_subnetwork.this.id
  address      = local.vm_ip
}

resource "google_compute_instance" "this" {
  name         = local.name
  machine_type = var.machine_type
  zone         = var.zone
  tags         = [local.name]

  # SSD, because every node syncs its own log: six of them wrote about 5,800
  # times a second, and a balanced disk this size is throttled well below that.
  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 100
      type  = "pd-ssd"
    }
  }

  # No access_config, which is what keeps it off the internet.
  network_interface {
    subnetwork = google_compute_subnetwork.this.id
    network_ip = google_compute_address.vm.address
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  metadata = {
    enable-oslogin   = "TRUE"
    demo-archive     = "gs://${google_storage_bucket.demo.name}/${google_storage_bucket_object.demo.name}"
    preset           = var.preset
    nodes-per-region = var.nodes_per_region
    crdb-version     = var.crdb_version
    crdb-license     = var.crdb_license
    banko-archive    = "gs://${google_storage_bucket.demo.name}/${google_storage_bucket_object.banko.name}"
    banko-secret-key = random_password.banko_secret_key.result
    banko-env        = join("\n", [for k, v in var.banko_env : "${k}=${v}"])

    # Not among the startup inputs above: turning Banko's sign-in on or off is
    # no reason for a new cluster. The README says how it is applied.
    banko-iap-audience = local.banko_iap_audience
  }

  metadata_startup_script = file("${path.module}/startup.sh")

  allow_stopping_for_update = true

  lifecycle {
    replace_triggered_by = [terraform_data.startup_inputs]
  }

  depends_on = [
    google_compute_router_nat.this,
    google_storage_bucket_iam_member.vm_reads_demo,
  ]
}

# --- Google sign-in in front: a proxy on Cloud Run with IAP switched on ---

resource "google_service_account" "proxy" {
  account_id   = "${local.name}-proxy"
  display_name = "Chaos demo sign-in proxy"
}

resource "google_cloud_run_v2_service" "stage" {
  name                = local.name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true
  deletion_protection = false

  template {
    service_account = google_service_account.proxy.email

    # The stage holds one event stream open per viewer; the browser reconnects
    # when this runs out.
    timeout = "3600s"

    scaling {
      max_instance_count = 2
    }

    # The stock image reads its config from a file, so the file is written
    # from the environment first. The Caddyfile says why it is not a one-line
    # reverse proxy.
    containers {
      image   = "docker.io/library/caddy:2-alpine"
      command = ["sh", "-c"]
      args    = ["printf '%s' \"$CADDYFILE\" > /tmp/Caddyfile && exec caddy run --config /tmp/Caddyfile --adapter caddyfile"]

      env {
        name = "CADDYFILE"
        value = templatefile("${path.module}/Caddyfile", {
          upstream    = "${local.vm_ip}:${local.panel_port}"
          console_url = google_cloud_run_v2_service.extra["console"].uri
          lb_url      = google_cloud_run_v2_service.extra["lb"].uri
        })
      }

      ports {
        container_port = 8080
      }
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"

      network_interfaces {
        network    = google_compute_network.this.name
        subnetwork = google_compute_subnetwork.this.name
      }
    }
  }

  depends_on = [google_project_service.this]
}

# Plain pass-throughs: none of these pages holds a stream open for good. The
# proxy sees plain HTTP from Google's front end and would tell the upstream
# so; Banko checks where a request came from against that, so it is told the
# truth.
resource "google_cloud_run_v2_service" "extra" {
  for_each = local.extras

  name                = each.value.name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true
  deletion_protection = false

  template {
    service_account = google_service_account.proxy.email

    scaling {
      max_instance_count = 2
    }

    containers {
      image   = "docker.io/library/caddy:2-alpine"
      command = ["caddy"]
      args = [
        "reverse-proxy",
        "--from", ":8080",
        "--to", "http://${local.vm_ip}:${each.value.port}",
        "--header-up", "X-Forwarded-Proto: https",
      ]

      ports {
        container_port = 8080
      }
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"

      network_interfaces {
        network    = google_compute_network.this.name
        subnetwork = google_compute_subnetwork.this.name
      }
    }
  }

  depends_on = [google_project_service.this]
}

# IAP calls the service as its own service agent, which has to be allowed to.
resource "google_project_service_identity" "iap" {
  provider = google-beta

  project = var.project_id
  service = "iap.googleapis.com"

  depends_on = [google_project_service.this]
}

resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  name     = google_cloud_run_v2_service.stage.name
  location = google_cloud_run_v2_service.stage.location
  role     = "roles/run.invoker"
  member   = google_project_service_identity.iap.member
}

resource "google_cloud_run_v2_service_iam_member" "iap_invoker_extra" {
  for_each = google_cloud_run_v2_service.extra

  name     = each.value.name
  location = each.value.location
  role     = "roles/run.invoker"
  member   = google_project_service_identity.iap.member
}

# Who gets through the sign-in.
resource "google_iap_web_cloud_run_service_iam_member" "staff" {
  cloud_run_service_name = google_cloud_run_v2_service.stage.name
  location               = google_cloud_run_v2_service.stage.location
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = "domain:${var.staff_domain}"
}

resource "google_iap_web_cloud_run_service_iam_member" "staff_extra" {
  for_each = google_cloud_run_v2_service.extra

  cloud_run_service_name = each.value.name
  location               = each.value.location
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = "domain:${var.staff_domain}"
}

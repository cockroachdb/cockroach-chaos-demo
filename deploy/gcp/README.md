# The demo on GCP, for staff

One VM runs the demo exactly as `./run.sh up` does on a laptop: the same code as this branch, the
same stage, the same controls. It has no public address. A small proxy on Cloud Run sits in front
with Identity-Aware Proxy switched on, so the stage is a `run.app` link that anyone in your Google
Workspace domain opens after signing in with Google.

    browser -> Google sign-in (IAP) -> Cloud Run proxy -> VM :8088 (the panel)
                                    -> Cloud Run proxy -> VM :8080 (the DB Console)
                                    -> Cloud Run proxy -> VM :8404 (HAProxy's stats)
                                    -> Cloud Run proxy -> VM :5000 (Banko)

The sign-in is the only gate. There is no `CONTROL_TOKEN` behind it, so whoever is signed in can
drive the stage, open `/presenter` and run scenarios, and can equally break the one cluster
everybody shares. One thing is switched off: rebuilding it (`REBUILD=off`), because a rebuild wipes
every database on it. SQL stays inside the VM's network.

On a laptop the DB Console and the load balancer's stats are ports on localhost, and the presenter
links to them by putting the page's hostname in front of the port. A `run.app` address answers on
443 and nothing else, so each of the two has a proxy and an address of its own, and the panel's
proxy corrects the addresses the panel reports so that the presenter's links still work. None of
this needs the cluster to be secure: Google terminates TLS in front of every address, and behind
it the traffic never leaves the VM's private network.

The proxy is not a plain pass-through. The stage keeps one never-ending event stream open, and the
inspecting web gateway that staff laptops send their traffic through holds a never-ending response
back whole: the stage sat at "Connecting..." while the same stream arrived every second on a
machine without one. So the [Caddyfile](Caddyfile) answers `/api/events` in complete pieces, one
event and a request to come back in a second, which the browser does by itself. Test changes to it
from a staff laptop, not from a cloud shell.

## Banko

Banko runs on the same VM and banks on the demo's own cluster (`defaultdb`, through HAProxy), so a
region taken down on the stage is something Banko's customers live through, and Banko's Platform
page can take one down itself. It is built on the VM from an archive of Banko's `main`, because its
published image is a major version behind. `tf.sh` packs it from `../../../banko-ai-assistant`;
`BANKO_DIR` and `BANKO_REF` point it elsewhere.

Its model provider is whatever `banko_env` says, as the environment variables Banko reads. In
`terraform.tfvars`, which git ignores:

    banko_env = {
      AI_SERVICE         = "watsonx"
      WATSONX_API_KEY    = "..."
      WATSONX_PROJECT_ID = "..."
    }

Left empty, Banko runs with no model: its pages work and its assistant does not. No model runs on
the VM in any case. Gemini through Vertex AI needs `aiplatform.googleapis.com` enabled in the
project, which this one does not have.

Banko is capped at two cores and 6 GB so that its embeddings cannot starve the database nodes. At
rest it uses about 0.5 GB and no CPU to speak of, and its pipeline's three containers about 1.5 GB
and a few percent of one core. Measured on `t2d-standard-8` with nine nodes: payments alone leave
about a quarter of the CPU idle; payments plus Banko's load simulator at 100 customers and 50
operations a second leave about a tenth, with both still at full rate and no errors. Driving both
hard at once is where `t2d-standard-16` starts to earn its cost.

The Coach page works as it does on a laptop: its simulated signals go to the real coach agent, and
a new sign-up gets a welcome nudge. That nudge is why Banko's proxy differs from the other two.
Banko posts it to itself at whatever host the sign-up request named, and the public address is the
sign-in, where the post is lost. So the proxy names the VM's own address as the host
(`--change-host-header`) and the public one travels in `X-Forwarded-Host`. The webhook's secret is
made on the VM at each boot.

Banko's Kafka pipeline runs beside it: a changefeed on `spending_signals` into Kafka, and Debezium
routing that to the topic the coach reads. Banko's own `scripts/coach/cdc-demo/run-cdc-demo.sh`
brings it up. A row inserted into that table by SQL comes back as a nudge on the Coach page a few
seconds later. Nothing outside the VM reaches SQL, so that is a demo for whoever has a shell on it:

    sudo docker exec -i us-east-1a-n1 cockroach sql --insecure <<'SQL'
    INSERT INTO spending_signals (user_id, signal_type, severity, payload, idempotency_key)
    VALUES ((SELECT user_id FROM users WHERE username = 'maya'), 'budget_threshold', 'warn',
            '{"category": "dining", "pct_used": 0.82, "monthly_budget": 400.0, "spent_so_far": 328.0, "days_remaining": 9}',
            'demo:' || gen_random_uuid()::STRING);
    SQL

One thing is done differently from that script. It joins every node to the pipeline's network so
that the changefeed can find Kafka, and a node on two networks is not what the stage is built for:
it used to come back from a restart with the latency shaped on the wrong interface. Here Kafka joins
the demo's network under the same name instead, and the nodes are taken back off. The pipeline kept
delivering through a region outage, and picked up by itself after the whole cluster went down and
came back.

Three things to know. The stage cannot rebuild the cluster here. A rebuild wipes `defaultdb`: Banko
then needs a restart, and the pipeline's changefeed is gone with the old cluster until the VM is
replaced. So the VM's `.env` says `REBUILD=off`, the presenter does not offer a rebuild, and a new
cluster comes with a new VM. Banko streams each answer as it is written; through
that gateway a staff browser gets the answer whole when it is finished. And nothing outside the VM
can post to the coach's webhook, which sits behind the sign-in like everything else.

## Once

Nothing here names a project, a bucket or a domain. They are yours to supply, in two files that git
ignores:

    cp terraform.tfvars.example terraform.tfvars    # the project, who gets in, the license, Banko's model
    cp backend.hcl.example backend.hcl              # the bucket that holds the Terraform state

The state lives in that bucket, which has to exist before the first init:

    PROJECT=your-gcp-project
    gcloud storage buckets create gs://$PROJECT-tfstate --project $PROJECT \
      --location us-east1 --uniform-bucket-level-access --public-access-prevention
    gcloud storage buckets update gs://$PROJECT-tfstate --versioning

On another machine the same two files and `./tf.sh init` pick up where the last one left off: the
state is in the bucket, not on a laptop. The two files can live there too, beside the state, where
only those who can already read the state can read them:

    gcloud storage cp terraform.tfvars backend.hcl gs://$PROJECT-tfstate/chaos-demo-config/

The same command the other way round brings them back. Copy them again after changing either.

## Apply

Use `./tf.sh` wherever you would type `terraform`. It packs the demo as committed on `main`
(`DEMO_REF=other-branch ./tf.sh apply` for another branch), hands `init` the bucket named in
`backend.hcl`, and passes a short-lived token from your gcloud login:

    cd deploy/gcp
    ./tf.sh init
    ./tf.sh apply

The VM then installs Docker, unpacks the archive, builds the cluster and adds nodes until every
region has `nodes_per_region`, which takes about two minutes. Banko follows, about seven minutes to
build its image and seed its personas, and then its pipeline, about two more. Payments are left
off, as on a laptop: whoever presents starts them from the stage, and should stop them afterwards.
Left running they hold about an hour of transfers: the rows expire after an hour and are deleted
every ten minutes. Watch the startup with:

    gcloud compute instances get-serial-port-output chaos-demo --zone us-east1-b --project $PROJECT \
      | grep startup-script

## Use

    ./tf.sh output stage_url       # the stage; add /presenter for the console
    ./tf.sh output console_url     # the DB Console
    ./tf.sh output lb_stats_url    # HAProxy's stats
    ./tf.sh output banko_url       # Banko; sign in as maya, sam or riley

Each address asks for the Google sign-in once per browser. A shell on the VM: `./tf.sh output ssh`.

## Change

- **A newer version of the demo or of Banko:** commit it to that repository's `main` and apply.
  Uncommitted work is not shipped.
- **A different CockroachDB version:** `crdb_version`.
- **A license key:** put `crdb_license = "..."` in `terraform.tfvars` (git ignores it) and apply.
  Without one CockroachDB throttles the cluster 7 days after it was built.
- **Who gets in:** `staff_domain`, or change the member on `google_iap_web_cloud_run_service_iam_member`
  to a Google group.

A change to either code base, the CockroachDB version, the preset, the node count, the license,
Banko's provider or the startup script replaces the VM, because nothing else would rerun the script.
A commit that leaves the packed files alone is not a change: the VM is tied to what the archives
hold, not to the commit they came from or the machine that packed them.
That takes about ten minutes with Banko and rebuilds the cluster from scratch, so whatever a
presenter had set up on it is gone.

## Remove

    ./tf.sh destroy

That deletes everything here. The state bucket is yours to delete by hand.

## Cost

About $265 a month at list price: roughly $247 for the VM (`t2d-standard-8`) and $17 for its 100 GB
SSD, plus a NAT gateway at a few dollars. The proxy scales to zero and costs nothing when nobody is
watching.

The size was measured, not guessed. On `e2-standard-8` with a balanced disk the default six nodes
ran at a load average of 15 on 8 vCPUs and the disk was throttled.

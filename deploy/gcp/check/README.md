# Checking the hosted instance after the VM was replaced

Nothing outside the VM can pass the Google sign-in, so these run on the VM itself. Both use only Python's standard
library and say PASS or FAIL per check, with a summary at the end and exit code 1 if anything failed. They were
written and used on 2026-11-20 for two replacements in a row.

    P=<project>; Z=us-east1-b
    gcloud compute scp deploy/gcp/check/*.py chaos-demo:/tmp/ --zone $Z --project $P --tunnel-through-iap

Wait for the startup script first: `sudo journalctl -u google-startup-scripts | grep "Finished running startup scripts"`
on the VM, about eight minutes after the replacement.

**The stage** (`vm_check.py`), about 35 minutes for everything:

    gcloud compute ssh chaos-demo --zone $Z --project $P --tunnel-through-iap --command \
      'python3 /tmp/vm_check.py cluster rebuild_switch payments_tables shaping'      # a minute and a half
    gcloud compute ssh chaos-demo --zone $Z --project $P --tunnel-through-iap --command \
      'nohup python3 /tmp/vm_check.py shaping scenarios final > /tmp/vm_check.log 2>&1 < /dev/null &'
    # then read /tmp/vm_check.log on the VM until it says SUMMARY

It checks the cluster that was asked for (nine nodes, the preset, the version, a license, the page code that is
served), that rebuilding is refused and nothing else is, the Payments tables' expiry and history settings, that every
node's latency is shaped on the interface that holds its own address, and then walks every guided scenario that fits
the cluster the way `tools/prove.py` walks them, recovering everything after each. It leaves the instance healthy
with Payments stopped. Name the parts to run only some of them.

**Banko** (`banko_check.py`), about two minutes. It takes the public host of Banko's address, which its proxy
forwards:

    HOST=$(cd deploy/gcp && ./tf.sh output -raw banko_url); HOST=${HOST#https://}
    gcloud compute ssh chaos-demo --zone $Z --project $P --tunnel-through-iap --command "python3 /tmp/banko_check.py '$HOST'"

It checks that Banko is ready and that its pipeline's containers and changefeed are up, inserts a signal by SQL for a
throwaway `zz-` user and waits for it to come back as a nudge through the changefeed and Kafka, and removes the user
again. The three personas are not touched. Each nudge is one small call to Banko's model.

How the user comes to be depends on `banko_sign_in`. On, nothing on the VM can sign in, so the check is that Banko
refuses a request without Google's signed identity, and the user is made by Banko's own code. Off, the user signs up
the way a browser behind the proxy does and has to get its welcome nudge.

What neither can check is the sign-in itself and what a staff browser gets through the company's web gateway: open
the four addresses from a staff laptop.

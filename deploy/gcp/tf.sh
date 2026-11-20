#!/usr/bin/env bash
# terraform, for this directory: ./tf.sh apply, ./tf.sh output stage_url, ./tf.sh destroy.
#
# It first packs the demo as committed on a branch (main unless DEMO_REF says
# otherwise), because the VM runs exactly that archive rather than cloning a
# repository. Banko is packed the same way from its own repository (BANKO_DIR,
# BANKO_REF): its published image is a major version behind. Then it runs
# terraform with a short-lived token from your gcloud login.
set -euo pipefail
cd "$(dirname "$0")"

demo_ref="${DEMO_REF:-main}"
demo_files=(run.sh docker-compose.yml controlplane web presets catalog tools/preflight.py)
banko_dir="${BANKO_DIR:-../../../banko-ai-assistant}"
banko_ref="${BANKO_REF:-main}"

# What the demo's archive holds at a commit, as git names it.
demo_id() {
  git -C ../.. ls-tree -r "$1" -- "${demo_files[@]}" | git hash-object --stdin
}

# An archive carries the commit it was packed from. Packing the last commit
# that touched the demo's files keeps the archive the same, byte for byte,
# across commits that leave them alone.
demo_commit="$(git -C ../.. log -1 --format=%H "$demo_ref" -- "${demo_files[@]}")"
[ "$(demo_id "$demo_commit")" = "$(demo_id "$demo_ref")" ] || demo_commit="$demo_ref"

mkdir -p .build
git -C ../.. archive --format=tar.gz -o "$PWD/.build/demo.tar.gz" "$demo_commit" -- "${demo_files[@]}"
git -C "$banko_dir" archive --format=tar.gz -o "$PWD/.build/banko.tar.gz" "$banko_ref"

# Terraform replaces the VM when what an archive holds changes, and these say
# what that is. Unlike an archive's own bytes they are the same on every
# machine.
demo_id "$demo_ref" > .build/demo.id
git -C "$banko_dir" rev-parse "$banko_ref^{tree}" > .build/banko.id

# init has to be told which bucket holds the state. backend.hcl names it.
if [ "${1:-}" = init ]; then
  shift
  set -- init -backend-config=backend.hcl "$@"
fi

GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)" exec terraform "$@"

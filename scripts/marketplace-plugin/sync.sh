#!/usr/bin/env bash
# Publish scripts/marketplace-plugin's assembled tree to the listing repo.
# Fast-forwards main. Skips the commit when the tree is already current.
# omarchy plugin update fast-forwards the installed checkout, so this script
# does not force-push.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/marketplace-plugin/listing.env"

: "${LISTING_REPO:?LISTING_REPO is missing from scripts/marketplace-plugin/listing.env}"

if [[ -z "${SOURCE_SHA:-}" ]]; then
  SOURCE_SHA="$(git -C "$ROOT" rev-parse HEAD)"
fi

token="${MARKETPLACE_PLUGIN_SYNC_TOKEN:-}"
if [[ -z "${LISTING_REMOTE:-}" && -z "${token//[[:space:]]/}" ]]; then
  echo "::error::MARKETPLACE_PLUGIN_SYNC_TOKEN is not set. Add a PAT or fine-grained token with contents:write on ${LISTING_REPO}. See scripts/marketplace-plugin/README.md. No listing commit was created."
  exit 1
fi

workdir="$(mktemp -d)"
cleanup() {
  rm -rf "$workdir"
}
trap cleanup EXIT

tree="$workdir/tree"
python3 "$ROOT/scripts/marketplace-plugin/assemble.py" "$tree"

askpass="$workdir/askpass"
cat >"$askpass" <<'EOF'
#!/bin/sh
case "$1" in
  *[Uu]sername*) printf '%s\n' "x-access-token" ;;
  *) printf '%s\n' "$MARKETPLACE_PLUGIN_SYNC_TOKEN" ;;
esac
EOF
chmod 700 "$askpass"

if [[ -n "${LISTING_REMOTE:-}" ]]; then
  clone_url="$LISTING_REMOTE"
  auth_prefix=()
else
  clone_url="https://github.com/${LISTING_REPO}.git"
  auth_prefix=(env GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0)
fi

git_identity=(-c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" -c commit.gpgsign=false)

commit_message() {
  cat <<EOF
Sync omarchy-ai.settings from Omarchy-AI ${SOURCE_SHA}

Source-of-truth commit:
https://github.com/omribenami/Omarchy-AI/commit/${SOURCE_SHA}
EOF
}

# Sets synced=0 when the index matches HEAD. set -e stays active because
# callers invoke this as a normal command, not as an if/|| condition.
commit_tree() {
  local listing="$1"
  git -C "$listing" add -A
  if git -C "$listing" diff --cached --quiet; then
    synced=0
    echo "Listing repository already matches Omarchy-AI ${SOURCE_SHA}; skipping commit."
    return 0
  fi
  synced=1
  git -C "$listing" "${git_identity[@]}" commit -m "$(commit_message)"
}

copy_tree_into() {
  local listing="$1"
  find "$listing" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  cp -a "$tree"/. "$listing"/
}

if ! refs="$("${auth_prefix[@]}" git ls-remote "$clone_url" 2>"$workdir/ls-remote.err")"; then
  echo "::error::Could not read ${LISTING_REPO}. Create the public repository and push scripts/marketplace-plugin/seed (see scripts/marketplace-plugin/README.md)."
  cat "$workdir/ls-remote.err" >&2
  exit 1
fi

if printf '%s\n' "$refs" | grep -q 'refs/heads/main$'; then
  listing="$workdir/listing"
  "${auth_prefix[@]}" git clone --depth 1 --branch main "$clone_url" "$listing"
  copy_tree_into "$listing"
  synced=0
  commit_tree "$listing"
  if [[ "$synced" -eq 1 ]]; then
    "${auth_prefix[@]}" git -C "$listing" push origin HEAD:main
    echo "Pushed omarchy-ai.settings sync for Omarchy-AI ${SOURCE_SHA} to ${LISTING_REPO} main."
  fi
else
  listing="$workdir/initial"
  git init -b main "$listing"
  copy_tree_into "$listing"
  git -C "$listing" "${git_identity[@]}" add -A
  git -C "$listing" "${git_identity[@]}" commit -m "$(commit_message)"
  git -C "$listing" remote add origin "$clone_url"
  "${auth_prefix[@]}" git -C "$listing" push origin HEAD:main
  echo "Pushed initial omarchy-ai.settings listing for Omarchy-AI ${SOURCE_SHA} to ${LISTING_REPO} main."
fi

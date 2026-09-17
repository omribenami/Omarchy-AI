#!/usr/bin/env bash
# Build a self-contained release bundle from a committed checkout.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo 'Refusing to package uncommitted source. Commit the release first.' >&2
  exit 1
fi

version="$(awk -F '"' '/^version = / { print $2; exit }' pyproject.toml)"
staging_dir="$(mktemp -d)"
trap 'rm -rf -- "$staging_dir"' EXIT
package_dir="$staging_dir/omarchy-ai-${version}-linux-x86_64"
archive="dist/omarchy-ai-${version}-linux-x86_64.tar.gz"
mkdir -p "$package_dir" dist

echo '==> Building Python wheel and source distribution'
uv build --out-dir "$package_dir/python"

echo '==> Building Android receiver APK'
(cd android-receiver && mise exec -- ./gradlew :app:assembleDebug --offline)
install -Dm644 android-receiver/app/build/outputs/apk/debug/app-debug.apk \
  "$package_dir/android/omarchy-ai-receiver.apk"

echo '==> Staging exact release source'
# Release archives are committed under dist/ for convenient installation.
# Excluding that directory here prevents the last archive from being packed
# inside the next one, which otherwise grows the bundle recursively.
git archive --format=tar HEAD -- . ':(exclude)dist' | tar -x -C "$package_dir"
chmod 0755 "$package_dir/install.sh"

tar -C "$staging_dir" -czf "$archive" "$(basename "$package_dir")"
(cd dist && sha256sum "$(basename "$archive")" >"$(basename "$archive").sha256")
echo "Built $archive"

#!/usr/bin/env bash
# Build a self-contained release bundle from a committed checkout.
set -euo pipefail

skip_android=0
for arg in "$@"; do
  case "$arg" in
    --skip-android) skip_android=1 ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

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

echo '==> Bundling pinned Jev Ultrafast browser wheel'
jev_source="$staging_dir/jev-ultrafast"
git clone --quiet https://github.com/browser-use/jev-ultrafast.git "$jev_source"
git -C "$jev_source" checkout --quiet 1231850a0bf1a0c0341fe408ef1668dbbfdfac46
uv build --wheel --out-dir "$package_dir/python" "$jev_source"

if ((skip_android)); then
  echo '==> Skipping Android receiver APK (--skip-android)'
else
  echo '==> Building Android receiver APK'
  (cd android-receiver && mise exec -- ./gradlew :app:assembleDebug --offline)
  install -Dm644 android-receiver/app/build/outputs/apk/debug/app-debug.apk \
    "$package_dir/android/omarchy-ai-receiver.apk"
fi

echo '==> Staging exact release source'
# Release archives are committed under dist/ for convenient installation.
# Exclude previous archives and original demo captures; neither is needed at
# runtime, and the raw media can push GitHub-hosted installers past 100 MB.
git archive --format=tar HEAD -- . \
  ':(exclude)dist' \
  ':(exclude)docs/media/original' \
  | tar -x -C "$package_dir"
chmod 0755 "$package_dir/install.sh"

tar -C "$staging_dir" -czf "$archive" "$(basename "$package_dir")"
(cd dist && sha256sum "$(basename "$archive")" >"$(basename "$archive").sha256")
echo "Built $archive"

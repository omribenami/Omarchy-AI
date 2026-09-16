#!/usr/bin/env bash
# Build a self-contained release bundle from a committed checkout.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo 'Refusing to package uncommitted source. Commit the release first.' >&2
  exit 1
fi

version="$(awk -F '"' '/^version = / { print $2; exit }' pyproject.toml)"
package_dir="dist/omarchy-ai-${version}-linux-x86_64"
archive="dist/omarchy-ai-${version}-linux-x86_64.tar.gz"
rm -rf "$package_dir" "$archive"
mkdir -p "$package_dir"

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
cat >"$package_dir/install.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
bundle_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$bundle_dir"
bash scripts/check-dependencies.sh
bash scripts/setup.sh
printf '\nReceiver APK: %s\n' "$bundle_dir/android/omarchy-ai-receiver.apk"
printf 'Install it on a connected Android TV with:\n  adb install -r "%s"\n' "$bundle_dir/android/omarchy-ai-receiver.apk"
EOF
chmod 0755 "$package_dir/install.sh"

tar -C dist -czf "$archive" "$(basename "$package_dir")"
rm -rf "$package_dir"
sha256sum "$archive" >"$archive.sha256"
echo "Built $archive"

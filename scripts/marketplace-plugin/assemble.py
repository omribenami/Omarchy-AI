#!/usr/bin/env python3
"""Build the one-plugin marketplace tree for omarchy-ai.settings.

The listing repository root is a single Quattro plugin: manifest.json,
Panel.qml (and any other files from the plugin directory), plus the
README and LICENSE templates in this directory. The plugin id, name,
kinds, and entry points are copied unchanged. The author field is set
to "Omri Ben Ami" for the public listing.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "quickshell" / "plugins" / "omarchy-ai.settings"
HERE = Path(__file__).resolve().parent
LISTING_AUTHOR = "Omri Ben Ami"
AUTHOR_RE = re.compile(r'("author"\s*:\s*)"(?:\\.|[^"\\])*"')


def listing_repo() -> str:
    for line in (HERE / "listing.env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        if key == "LISTING_REPO" and value:
            return value
    raise RuntimeError("LISTING_REPO is missing from scripts/marketplace-plugin/listing.env")


def assemble(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in dest.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    for path in sorted(PLUGIN.iterdir(), key=lambda item: item.name):
        if path.name.startswith("."):
            continue
        target = dest / path.name
        if path.is_dir():
            shutil.copytree(path, target)
        else:
            shutil.copy2(path, target)

    manifest_path = dest / "manifest.json"
    original = manifest_path.read_text(encoding="utf-8")
    updated, count = AUTHOR_RE.subn(rf'\1"{LISTING_AUTHOR}"', original, count=1)
    if count != 1:
        raise RuntimeError("manifest.json is missing an author string")
    manifest_path.write_text(updated, encoding="utf-8")

    shutil.copy2(HERE / "templates" / "LICENSE", dest / "LICENSE")
    readme = (HERE / "templates" / "README.md").read_text(encoding="utf-8")
    clone_url = f"https://github.com/{listing_repo()}.git"
    readme = readme.replace("@LISTING_CLONE_URL@", clone_url)
    (dest / "README.md").write_text(readme, encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} DEST", file=sys.stderr)
        sys.exit(2)
    assemble(Path(sys.argv[1]))


if __name__ == "__main__":
    main()

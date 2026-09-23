#!/usr/bin/env python3
"""Upload the built install archive as a GitHub Release asset.

Reads the version from pyproject.toml and publishes:

  dist/omarchy-ai-<version>-linux-x86_64.tar.gz
  dist/omarchy-ai-<version>-linux-x86_64.tar.gz.sha256

on the release tag v<version>. Creating the release also creates that git tag
at origin/main when the tag does not already exist. Re-running replaces those
two assets on the existing release.

Prefers the gh CLI. When gh is not on PATH, uses GH_TOKEN or GITHUB_TOKEN
(contents: write) against the GitHub REST API. Dry-run by default, same as
scripts/publish-via-myapi.py. That MyApi publisher still commits source; it
does not upload these bytes, because asset upload goes to uploads.github.com
as a raw body.

REST API, if you need to do this by hand:

1. GET https://api.github.com/repos/omribenami/Omarchy-AI/releases/tags/vX.Y.Z
   Authorization: Bearer $GH_TOKEN
   Accept: application/vnd.github+json
   X-GitHub-Api-Version: 2022-11-28

2. On 404, POST https://api.github.com/repos/omribenami/Omarchy-AI/releases
   Content-Type: application/json
   {"tag_name":"vX.Y.Z","target_commitish":"<origin/main sha>",
    "name":"Omarchy AI X.Y.Z","body":"<notes>","draft":false,"prerelease":false}

3. For each filename, DELETE any existing asset of that name
   (DELETE /repos/omribenami/Omarchy-AI/releases/assets/<asset_id>), then:
   POST https://uploads.github.com/repos/omribenami/Omarchy-AI/releases/<id>/assets?name=<filename>
   Content-Type: application/octet-stream
   Body: the raw file bytes.

Download counts: the release page shows a count beside each file. The API
field is assets[].download_count on the GET in step 1. gh prints the same
value as downloadCount:

  gh release view vX.Y.Z --repo omribenami/Omarchy-AI --json assets \\
    --jq '.assets[] | {name, downloadCount}'

The .tar.gz count is the package download count. The .sha256 file has its own.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from omarchy_ai.core.updates import REPOSITORY, bundle_names, version_tuple

ROOT = Path(__file__).resolve().parents[1]
REPO = REPOSITORY


def project_version(root: Path = ROOT) -> str:
    with (root / "pyproject.toml").open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    version_tuple(version)
    return version


def verify_checksum(archive: Path, checksum: Path) -> None:
    parts = checksum.read_text().strip().split()
    if len(parts) != 2 or parts[1].lstrip("*") != archive.name or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
        raise ValueError(f"Invalid checksum file: {checksum}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != parts[0].lower():
        raise ValueError("Release checksum mismatch")


def release_notes(version: str) -> str:
    package, checksum = bundle_names(version)
    base = f"https://github.com/{REPO}/releases/download/v{version}"
    return (
        f"Omarchy AI {version} for linux-x86_64.\n\n"
        "Verify the archive before installing:\n\n"
        f"    curl -fL -o {package} {base}/{package}\n"
        f"    curl -fL -o {checksum} {base}/{checksum}\n"
        f"    sha256sum -c {checksum}\n"
        f"    tar -xzf {package}\n"
        f"    cd omarchy-ai-{version}-linux-x86_64\n"
        "    bash install.sh\n"
    )


def prepare(root: Path, version: str, commit: str) -> dict:
    package, checksum_name = bundle_names(version)
    archive = root / "dist" / package
    checksum = root / "dist" / checksum_name
    if not archive.is_file() or not checksum.is_file():
        raise SystemExit(
            f"Missing {archive.name} or its .sha256. Run scripts/build-install-package.sh first."
        )
    verify_checksum(archive, checksum)
    return {
        "version": version,
        "tag": f"v{version}",
        "commit": commit,
        "archive": archive,
        "checksum": checksum,
        "title": f"Omarchy AI {version}",
        "notes": release_notes(version),
        "repo": REPO,
    }


def _status_path(line: str) -> str:
    path = line[3:] if len(line) > 3 else line
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path


def ensure_publishable_checkout(allowed_dirty: set[str]) -> str:
    status = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"], text=True)
    bad = [line for line in status.splitlines() if _status_path(line) not in allowed_dirty]
    if bad:
        raise SystemExit("Refusing to publish with unrelated local changes:\n" + "\n".join(bad))
    subprocess.run(["git", "fetch", "origin", "main"], check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    remote = subprocess.check_output(["git", "rev-parse", "origin/main"], text=True).strip()
    if head != remote:
        raise SystemExit(
            f"Refusing to publish: HEAD {head[:12]} is not origin/main {remote[:12]}. "
            "Push the version commit first."
        )
    return head


def gh_create_command(plan: dict) -> list[str]:
    return [
        "gh", "release", "create", plan["tag"],
        str(plan["archive"]), str(plan["checksum"]),
        "--repo", plan["repo"],
        "--title", plan["title"],
        "--notes", plan["notes"],
        "--target", plan["commit"],
    ]


def gh_upload_command(plan: dict) -> list[str]:
    return [
        "gh", "release", "upload", plan["tag"],
        str(plan["archive"]), str(plan["checksum"]),
        "--repo", plan["repo"],
        "--clobber",
    ]


def _release_missing(result) -> bool:
    if result.returncode == 0:
        return False
    text = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}".lower()
    return "release not found" in text or "http 404" in text


def publish_with_gh(plan: dict, run) -> str:
    view = run(["gh", "release", "view", plan["tag"], "--repo", plan["repo"]])
    if view.returncode == 0:
        uploaded = run(gh_upload_command(plan))
        if uploaded.returncode:
            raise SystemExit((uploaded.stderr or uploaded.stdout or "gh release upload failed").strip())
        return "updated"
    if not _release_missing(view):
        detail = f"{view.stderr or ''}{view.stdout or ''}".strip()
        raise SystemExit(detail or "gh release view failed")
    created = run(gh_create_command(plan))
    if created.returncode:
        raise SystemExit((created.stderr or created.stdout or "gh release create failed").strip())
    return "created"


def api_steps(plan: dict) -> str:
    payload = json.dumps({
        "tag_name": plan["tag"],
        "target_commitish": plan["commit"],
        "name": plan["title"],
        "body": plan["notes"],
        "draft": False,
        "prerelease": False,
    }, indent=2)
    package = plan["archive"].name
    checksum = plan["checksum"].name
    return (
        f"GitHub REST API, when gh is not installed. Token: GH_TOKEN or GITHUB_TOKEN "
        f"with contents: write on {plan['repo']}.\n"
        f"1. GET https://api.github.com/repos/{plan['repo']}/releases/tags/{plan['tag']}\n"
        "   Authorization: Bearer $GH_TOKEN\n"
        "   Accept: application/vnd.github+json\n"
        "   X-GitHub-Api-Version: 2022-11-28\n"
        f"2. On HTTP 404, POST https://api.github.com/repos/{plan['repo']}/releases\n"
        "   Content-Type: application/json\n"
        f"{payload}\n"
        f"3. For {package} and {checksum}: if that name is already in assets, "
        f"DELETE https://api.github.com/repos/{plan['repo']}/releases/assets/<asset_id>\n"
        f"   then POST https://uploads.github.com/repos/{plan['repo']}/releases/<release_id>/assets?name=<filename>\n"
        "   Content-Type: application/octet-stream\n"
        "   Body: the raw file bytes.\n"
        "Download count: assets[].download_count on the GET, or the number beside the file on the release page.\n"
        f"  gh release view {plan['tag']} --repo {plan['repo']} --json assets "
        "--jq '.assets[] | {name, downloadCount}'\n"
    )


def format_plan(plan: dict) -> str:
    create = " ".join(shlex.quote(part) for part in gh_create_command(plan))
    upload = " ".join(shlex.quote(part) for part in gh_upload_command(plan))
    return (
        f"Release {plan['tag']} at {plan['commit']}\n"
        f"  {plan['archive']}\n"
        f"  {plan['checksum']}\n"
        f"Create (when {plan['tag']} has no release yet):\n  {create}\n"
        f"Update (when the release already exists; replaces these two assets):\n  {upload}\n"
        f"{api_steps(plan)}"
    )


def github_token() -> str | None:
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


class GitHubApi:
    def __init__(self, token: str, opener=urllib.request.urlopen):
        self.token = token
        self.opener = opener

    def request(self, method: str, url: str, *, json_body=None, raw: bytes | None = None,
                content_type: str | None = None, timeout: int = 60):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "omarchy-ai-release",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            data = raw
            headers["Content-Type"] = content_type or "application/octet-stream"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener(req, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            if exc.code == 404 and method == "GET":
                return None
            raise RuntimeError(f"GitHub {method} {url} failed: HTTP {exc.code} {detail}") from exc
        if not body:
            return {}
        return json.loads(body)


def publish_with_api(plan: dict, api: GitHubApi) -> str:
    repo = plan["repo"]
    existing = api.request("GET", f"https://api.github.com/repos/{repo}/releases/tags/{plan['tag']}")
    created = existing is None
    if created:
        existing = api.request(
            "POST", f"https://api.github.com/repos/{repo}/releases",
            json_body={
                "tag_name": plan["tag"],
                "target_commitish": plan["commit"],
                "name": plan["title"],
                "body": plan["notes"],
                "draft": False,
                "prerelease": False,
            },
        )
    if not isinstance(existing, dict) or "id" not in existing:
        raise RuntimeError("GitHub release response did not include an id")
    assets = {}
    for asset in existing.get("assets") or []:
        if isinstance(asset, dict) and isinstance(asset.get("name"), str) and asset.get("id") is not None:
            assets[asset["name"]] = asset["id"]
    for path in (plan["archive"], plan["checksum"]):
        asset_id = assets.get(path.name)
        if asset_id is not None:
            api.request("DELETE", f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}")
        query = urllib.parse.urlencode({"name": path.name})
        api.request(
            "POST",
            f"https://uploads.github.com/repos/{repo}/releases/{existing['id']}/assets?{query}",
            raw=path.read_bytes(),
            timeout=600,
        )
    return "created" if created else "updated"


def _gh_run(args: list[str]):
    return subprocess.run(args, capture_output=True, text=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish the built install archive as a GitHub Release.")
    parser.add_argument("--publish", action="store_true", help="create or update the release and upload assets")
    args = parser.parse_args(argv)
    version = project_version(ROOT)
    package, checksum_name = bundle_names(version)
    commit = ensure_publishable_checkout({f"dist/{package}", f"dist/{checksum_name}"})
    plan = prepare(ROOT, version, commit)
    print(format_plan(plan))
    if not args.publish:
        print("Dry run only. Re-run with --publish to create or update the GitHub Release.")
        return 0
    if shutil.which("gh"):
        action = publish_with_gh(plan, _gh_run)
    else:
        token = github_token()
        if not token:
            raise SystemExit(
                "gh is not on PATH and GH_TOKEN/GITHUB_TOKEN is unset. "
                "Install GitHub CLI or export a contents:write token, then re-run. "
                "The API steps are printed above."
            )
        action = publish_with_api(plan, GitHubApi(token))
    print(f"{action.capitalize()} https://github.com/{plan['repo']}/releases/tag/{plan['tag']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Release publish failed: {error}", file=sys.stderr)
        raise SystemExit(1)

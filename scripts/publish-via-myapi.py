#!/usr/bin/env python3
"""Publish the committed source tree through MyApi's GitHub connection.

This is for environments without a local GitHub credential. It creates Git
blobs, a tree, one commit, and advances main only if GitHub still points at the
locally fetched origin/main commit.

Install archives are not part of that commit. `scripts/publish-github-release.py`
uploads `dist/omarchy-ai-<version>-linux-x86_64.tar.gz` and its `.sha256` as
assets on the GitHub Release `v<version>`. MyApi's GitHub proxy speaks JSON to
api.github.com; Release asset upload is a raw body to uploads.github.com, so
this script does not try to send the tarball through MyApi.
"""
from __future__ import annotations

import argparse
import base64
from pathlib import Path
import subprocess
import sys
import tomllib

from omarchy_ai.core.updates import RELEASE_ARCHIVE, version_tuple
from omarchy_ai.myapi.client import MyApiClient, MyApiError

OWNER = "omribenami"
REPOSITORY = "Omarchy-AI"
BRANCH = "main"


def git(*args: str, text: bool = True):
    command = ["git", *args]
    return subprocess.check_output(command, text=text).strip() if text else subprocess.check_output(command)


def response_data(result: dict) -> dict:
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or result.get("message") or "GitHub request failed")
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("GitHub returned an unexpected response")
    return data


def request(client: MyApiClient, path: str, method: str = "GET", body=None) -> dict:
    return response_data(client.call_service("github", path, method=method, body=body))


def changed_paths(base: str) -> list[tuple[str, str]]:
    lines = git("diff", "--name-status", "--no-renames", base, "HEAD").splitlines()
    return [(line.split("\t", 1)[0], line.split("\t", 1)[1]) for line in lines if "\t" in line]


def release_archive_paths(changes: list[tuple[str, str]]) -> list[str]:
    return [path for _status, path in changes if RELEASE_ARCHIVE.fullmatch(path)]


def commit_message(version: str) -> str:
    version_tuple(version)
    return f"Publish Omarchy AI {version}"


def project_version() -> str:
    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as source:
        return tomllib.load(source)["project"]["version"]


def entry_for(path: str) -> tuple[str, str]:
    line = git("ls-tree", "HEAD", "--", path)
    mode, _kind, _sha, recorded_path = line.split(maxsplit=3)
    return mode, recorded_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true", help="perform the GitHub write operations")
    args = parser.parse_args(argv)

    if git("status", "--porcelain"):
        parser.error("working tree must be clean")
    expected_parent = git("rev-parse", "origin/main")
    changes = changed_paths("origin/main")
    if not changes:
        print("No changes to publish.")
        return 0
    blocked = release_archive_paths(changes)
    if blocked:
        parser.error(
            "release archives are GitHub Release assets, not git blobs: "
            + ", ".join(blocked)
            + ". Remove them from the commit and run scripts/publish-github-release.py."
        )
    print(f"Prepared {len(changes)} paths against origin/main {expected_parent[:12]}")
    for status, path in changes:
        print(f"  {status:>2} {path}")
    if not args.publish:
        print("Dry run only. Re-run with --publish to create the commit through MyApi.")
        return 0

    client = MyApiClient()
    if not client.connected:
        parser.error("MyApi is not connected")
    ref = request(client, f"/repos/{OWNER}/{REPOSITORY}/git/ref/heads/{BRANCH}")
    remote_parent = (ref.get("object") or {}).get("sha")
    if remote_parent != expected_parent:
        parser.error(f"remote main moved ({remote_parent}); fetch and reconcile before retrying")
    base_commit = request(client, f"/repos/{OWNER}/{REPOSITORY}/git/commits/{remote_parent}")
    entries = []
    for status, path in changes:
        if status == "D":
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        mode, path = entry_for(path)
        raw = git("show", f"HEAD:{path}", text=False)
        blob = request(client, f"/repos/{OWNER}/{REPOSITORY}/git/blobs", "POST", {
            "content": base64.b64encode(raw).decode(), "encoding": "base64",
        })
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
        print(f"Uploaded {path}")
    tree = request(client, f"/repos/{OWNER}/{REPOSITORY}/git/trees", "POST", {
        "base_tree": base_commit["tree"]["sha"], "tree": entries,
    })
    message = commit_message(project_version())
    commit = request(client, f"/repos/{OWNER}/{REPOSITORY}/git/commits", "POST", {
        "message": message, "tree": tree["sha"], "parents": [remote_parent],
    })
    request(client, f"/repos/{OWNER}/{REPOSITORY}/git/refs/heads/{BRANCH}", "PATCH", {
        "sha": commit["sha"], "force": False,
    })
    print(f"Published https://github.com/{OWNER}/{REPOSITORY}/commit/{commit['sha']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MyApiError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Publish failed: {error}", file=sys.stderr)
        raise SystemExit(1)

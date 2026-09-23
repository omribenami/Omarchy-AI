"""Versioned GitHub Release bundles, cached wake notices, and isolated self-updates.

This file is also a standalone worker launched in its own systemd user unit,
so restarting the voice daemon cannot kill an update halfway through.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)
REPOSITORY = "omribenami/Omarchy-AI"
API = f"https://api.github.com/repos/{REPOSITORY}"
ROOT = Path(__file__).resolve().parents[3]
STATE = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "omarchy-ai/updates"
RELEASES = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "omarchy-ai/releases"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
SERVICE = "omarchy-ai.service"
CACHE_SECONDS = 900
_CHECK_LOCK = threading.Lock()
PACKAGE = re.compile(r"omarchy-ai-(\d+\.\d+\.\d+)-linux-x86_64\.tar\.gz\Z")
# Git path of a bundle that must be a Release asset, not a new git blob.
RELEASE_ARCHIVE = re.compile(
    r"dist/omarchy-ai-(\d+\.\d+\.\d+)-linux-x86_64\.tar\.gz(\.sha256)?\Z")


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Expected a stable major.minor.patch version")
    return tuple(map(int, value.split(".")))


def installed_version(root=ROOT):
    with (root / "pyproject.toml").open("rb") as source:
        return tomllib.load(source)["project"]["version"]


def _read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream)
        temp = Path(stream.name)
    temp.replace(path)


def bundle_names(version):
    """Archive and checksum filenames for one stable X.Y.Z package."""
    version_tuple(version)
    package = f"omarchy-ai-{version}-linux-x86_64.tar.gz"
    return package, package + ".sha256"


def _read_github(url, limit=2_000_000):
    request = urllib.request.Request(url, headers={"User-Agent": "Omarchy-AI-Updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        data = response.read(limit + 1)
        link = response.headers.get("Link", "")
    if len(data) > limit:
        raise ValueError("GitHub response too large")
    return json.loads(data), link


def _json(url):
    payload, _link = _read_github(url)
    return payload


def _next_link(header):
    """Next page of this repo's release list, or None. Other hosts are refused."""
    if not header:
        return None
    for part in header.split(","):
        bits = [piece.strip() for piece in part.split(";")]
        if not bits or not bits[0].startswith("<") or not bits[0].endswith(">"):
            continue
        if not any(bit in ('rel="next"', "rel='next'") for bit in bits[1:]):
            continue
        url = bits[0][1:-1]
        parsed = urllib.parse.urlparse(url)
        expected = f"/repos/{REPOSITORY}/releases"
        if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.path != expected:
            raise ValueError("Unexpected GitHub pagination link")
        return url
    return None


def _list_releases():
    releases = []
    url = API + "/releases?per_page=100"
    for _ in range(10):
        payload, link = _read_github(url, limit=8_000_000)
        if not isinstance(payload, list):
            raise ValueError("GitHub releases response was not a list")
        releases.extend(payload)
        url = _next_link(link)
        if not url:
            return releases
    raise ValueError("GitHub releases listing exceeded page limit")


def _candidate(version, package, *, source, commit=None):
    _package, checksum = bundle_names(version)
    if package != _package:
        raise ValueError("Release asset name does not match its version")
    if source == "release":
        base = f"https://github.com/{REPOSITORY}/releases/download/v{version}/"
        page = f"https://github.com/{REPOSITORY}/releases/tag/v{version}"
        tag = f"v{version}"
    elif source == "dist":
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Invalid GitHub commit")
        base = f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/dist/"
        page = f"https://github.com/{REPOSITORY}/tree/{commit}/dist"
        tag = None
    else:
        raise ValueError("Unknown bundle source")
    return {
        "latest_version": version,
        "commit": commit,
        "tag": tag,
        "package": package,
        "package_url": base + package,
        "checksum_url": base + checksum,
        "url": page,
        "source": source,
    }


def candidates_from_releases(releases):
    """Stable vX.Y.Z releases that publish both the archive and its checksum.

    demo-media, drafts, prereleases, and tags whose asset names do not match
    the tag version are ignored. Download URLs are built from the tag and
    filename, not from whatever URL the API echoes back.
    """
    if not isinstance(releases, list):
        raise ValueError("GitHub releases response was not a list")
    found = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        match = re.fullmatch(r"v(\d+\.\d+\.\d+)", tag) if isinstance(tag, str) else None
        if not match:
            continue
        version = match.group(1)
        package, checksum = bundle_names(version)
        names = {asset.get("name") for asset in release.get("assets") or []
                 if isinstance(asset, dict)}
        if package in names and checksum in names:
            found.append(_candidate(version, package, source="release"))
    return found


def candidates_from_dist(commit, entries):
    if not isinstance(entries, list):
        raise ValueError("GitHub dist listing was not a list")
    names = {item.get("name") for item in entries
             if isinstance(item, dict) and item.get("type") == "file"}
    found = []
    for name in names:
        match = PACKAGE.fullmatch(name) if isinstance(name, str) else None
        if match and name + ".sha256" in names:
            found.append(_candidate(match.group(1), name, source="dist", commit=commit))
    return found


def _dist_candidates():
    # Historical bundles committed under dist/ before Release assets existed.
    # Both files are pinned to one commit, same as the original updater.
    payload = _json(API + "/commits/main")
    commit = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid GitHub commit")
    try:
        entries = _json(API + "/contents/dist?ref=" + commit)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    return candidates_from_dist(commit, entries)


def _bundle_url_ok(url, filename):
    if not isinstance(url, str) or not isinstance(filename, str) or url.rsplit("/", 1)[-1] != filename:
        return False
    release = re.fullmatch(
        rf"https://github\.com/{re.escape(REPOSITORY)}/releases/download/v(\d+\.\d+\.\d+)/([^/]+)",
        url,
    )
    if release:
        version, name = release.groups()
        package, checksum = bundle_names(version)
        return name == filename and filename in {package, checksum}
    dist = re.fullmatch(
        rf"https://raw\.githubusercontent\.com/{re.escape(REPOSITORY)}/[0-9a-f]{{40}}/dist/([^/]+)",
        url,
    )
    if not dist or dist.group(1) != filename:
        return False
    package = filename[:-7] if filename.endswith(".sha256") else filename
    return PACKAGE.fullmatch(package) is not None and filename in {package, package + ".sha256"}


def _require_bundle_url(url, filename):
    if not _bundle_url_ok(url, filename):
        raise ValueError("Refusing unexpected update URL")


def discover():
    # Prefer a GitHub Release asset (tag vX.Y.Z) so downloads increment
    # download_count. A historical dist/ bundle is still a candidate: the
    # higher version wins, and the same version uses the Release asset.
    # A failure to list releases aborts the check instead of silently
    # installing whatever is still in dist/.
    candidates = candidates_from_releases(_list_releases())
    try:
        candidates.extend(_dist_candidates())
    except Exception as exc:
        if not candidates:
            raise
        log.warning("Historical dist bundle discovery failed: %s", exc)
    if not candidates:
        raise ValueError("No versioned install bundle with a checksum is published")
    return max(candidates, key=lambda item: (
        version_tuple(item["latest_version"]), item["source"] == "release"))


CHANGELOG = "CHANGELOG.md"
_SECTION = re.compile(r"^## \[(\d+\.\d+\.\d+|Unreleased)\](?:\s*-\s*(\S+))?\s*$")


def parse_changelog(text):
    """{version: {"date", "sections": {"Highlights": [bullet, ...], ...}}}.

    Bullets keep their wrapped continuation lines joined into one string, so
    each one is a complete sentence the assistant can read out."""
    releases, entry, section = {}, None, None
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        match = _SECTION.match(line)
        if match:
            entry = {"date": match[2], "sections": {}}
            releases[match[1]] = entry
            section = None
        elif entry is not None and line.startswith("### "):
            section = entry["sections"].setdefault(line[4:].strip(), [])
        elif line.startswith("## "):
            entry = section = None
        elif section is not None and re.match(r"^\s*[-*] ", line):
            section.append(re.sub(r"^\s*[-*] ", "", line).strip())
        elif section is not None and section and line.startswith("  ") and line.strip():
            section[-1] += " " + line.strip()
    return releases


def notes_between(releases, current, latest):
    """Published entries newer than `current`, up to `latest`, newest first."""
    picked = []
    for version, entry in releases.items():
        try:
            key = version_tuple(version)
        except ValueError:
            continue  # "Unreleased" is never announced
        if version_tuple(current) < key <= version_tuple(latest):
            picked.append((key, {"version": version, **entry}))
    return [entry for _, entry in sorted(picked, reverse=True)]


def _fetch_changelog(ref):
    if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{40}|v\d+\.\d+\.\d+", ref):
        raise ValueError("Changelog ref must be a commit or a vX.Y.Z tag")
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{ref}/{CHANGELOG}"
    request = urllib.request.Request(url, headers={"User-Agent": "Omarchy-AI-Updater"})
    with urllib.request.urlopen(request, timeout=5) as response:
        data = response.read(500_001)
    if len(data) > 500_000:
        raise ValueError("Changelog too large")
    return data.decode("utf-8", errors="replace")


def local_changelog(root=ROOT):
    try:
        return parse_changelog((root / CHANGELOG).read_text(encoding="utf-8"))
    except OSError:
        return {}


def check_updates(force=False):
    with _CHECK_LOCK:
        now = time.time()
        cache = _read(STATE / "check.json")
        current = installed_version()
        checked_at = cache.get("checked_at", 0)
        if not isinstance(checked_at, (float, int)):
            checked_at = 0
        if not force and 0 <= now - checked_at < CACHE_SECONDS:
            cache["current_version"] = current
            try:
                cache["available"] = bool(cache.get("ok") and version_tuple(cache["latest_version"]) > version_tuple(current))
                return cache
            except (KeyError, ValueError, TypeError):
                pass  # Refresh a malformed cache instead of breaking activation.
        try:
            release = discover()
            result = {**release, "ok": True, "checked_at": now, "current_version": current,
                      "available": version_tuple(release["latest_version"]) > version_tuple(current)}
            if result["available"]:
                # Same pinned commit as the bundle, so the notes describe
                # exactly what would be installed. Optional: a missing or
                # unreachable changelog must never hide the update itself.
                try:
                    # Release assets carry a tag, historical dist/ bundles a
                    # commit; either pins the changelog to what would install.
                    ref = release.get("commit") or release.get("tag")
                    result["release_notes"] = notes_between(
                        parse_changelog(_fetch_changelog(ref)), current, release["latest_version"])
                except Exception as exc:
                    result["release_notes"] = []
                    result["release_notes_error"] = f"Could not read release notes: {exc}"
        except Exception as exc:
            result = {"ok": False, "available": False, "checked_at": now,
                      "current_version": current, "error": f"Could not check GitHub: {exc}"}
            log.warning("%s", result["error"])
        _write(STATE / "check.json", result)
        return result


def _has_highlights(entries):
    return any(entry.get("sections", {}).get("Highlights") for entry in entries or [])


# One wake notice is read twice per session (instructions, then Gemini's
# spoken announcement), so "already announced" means announced more than
# this long ago, not "seen once".
_ANNOUNCE_GRACE_SECONDS = 180


def _post_update_notice():
    """Once, after a completed self-update: say so and offer the highlights."""
    status = _read(STATE / "install.json")
    try:
        version = installed_version()
        if status.get("state") != "completed" or status.get("version") != version:
            return ""
        announced = _read(STATE / "announced.json")
        now = time.time()
        if announced.get("version") == version and now - announced.get("at", 0) > _ANNOUNCE_GRACE_SECONDS:
            return ""
        if announced.get("version") != version:
            _write(STATE / "announced.json", {"version": version, "at": now})
        entry = local_changelog().get(version)
        if entry and entry["sections"].get("Highlights"):
            return (f"I was just updated to Omarchy AI version {version}. "
                    "Offer to go over what's new in it; call get_release_notes if the user wants that.")
        return f"I was just updated to Omarchy AI version {version}."
    except (KeyError, ValueError, TypeError, OSError):
        return ""


def wake_notice():
    """Read cached evidence only; never add network latency to session setup."""
    cache = _read(STATE / "check.json")
    try:
        if (cache.get("ok") and 0 <= time.time() - cache["checked_at"] < CACHE_SECONDS * 2
                and version_tuple(cache["latest_version"]) > version_tuple(installed_version())):
            notice = (f"Omarchy AI version {cache['latest_version']} is available. "
                      "I recommend updating. You can say 'update yourself' to install it.")
            # Only offer what can really be delivered: the highlights must
            # already be cached from the pinned changelog.
            if _has_highlights(cache.get("release_notes")):
                notice += (" I can also tell you what's new in it -- just ask for the highlights"
                           " (get_release_notes has them).")
            return notice
    except (KeyError, ValueError, TypeError, OSError):
        pass
    return _post_update_notice()


def release_notes(version=None):
    """Notes for a specific version, or for what an available update brings,
    or (when current) for the installed version. Never invents an entry."""
    current = installed_version()
    local = local_changelog()
    if version:
        version_tuple(version)
        cache = _read(STATE / "check.json")
        cached = {e.get("version"): e for e in cache.get("release_notes") or []}
        entry = cached.get(version) or ({"version": version, **local[version]} if version in local else None)
        if not entry:
            return {"ok": False, "installed_version": current,
                    "error": f"No changelog entry for {version} is available on this machine."}
        return {"ok": True, "installed_version": current, "releases": [entry]}
    cache = check_updates()
    if cache.get("available"):
        entries = cache.get("release_notes") or []
        if not entries:
            cache = check_updates(force=True)
            entries = cache.get("release_notes") or []
        if entries:
            return {"ok": True, "installed_version": current, "latest_version": cache["latest_version"],
                    "update_available": True, "releases": entries}
        return {"ok": False, "installed_version": current, "latest_version": cache.get("latest_version"),
                "update_available": True,
                "error": cache.get("release_notes_error") or "The published version has no changelog entry."}
    if current in local:
        return {"ok": True, "installed_version": current, "update_available": False,
                "releases": [{"version": current, **local[current]}]}
    return {"ok": False, "installed_version": current, "update_available": False,
            "error": f"No changelog entry for the installed version {current}."}


def update_status():
    return _read(STATE / "install.json") or {"state": "idle", "message": "No update has been requested."}


def request_update():
    release = check_updates(force=True)
    if not release["ok"]:
        return False, release["error"]
    if not release["available"]:
        return True, f"Already current: installed {release['current_version']}; latest published bundle {release['latest_version']}."
    # This is a separate service, not a detached child in the daemon cgroup.
    # The fixed unit name prevents duplicate updates across phone sessions.
    command = ["systemd-run", "--user", "--collect", "--unit=omarchy-ai-update",
               "--service-type=exec", "--property=TimeoutStartSec=30min"]
    for key in ("PATH", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "OMARCHY_PATH"):
        if key in os.environ:
            command += ["--setenv=" + key + "=" + os.environ[key]]
    command += [sys.executable, str(Path(__file__).resolve()), "--install", release["latest_version"], "--previous-root", str(ROOT)]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Could not start updater: {exc}"
    if proc.returncode:
        return False, "Updater did not start (another update may already be running): " + (proc.stderr or proc.stdout).strip()
    return True, (f"Update to {release['latest_version']} started in the background. "
                  "I will restart after installation. This conversation will disconnect then. "
                  "Installation is NOT complete yet; use get_update_status to check progress.")


def _command(args, *, cwd=None, timeout=600):
    subprocess.run(args, cwd=cwd, check=True, timeout=timeout)


def _download(url, path, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "Omarchy-AI-Updater"})
    with urllib.request.urlopen(request, timeout=30) as response, path.open("wb") as output:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError("Update download exceeds size limit")
            output.write(chunk)


def unpack_verified(archive, checksum, destination, version):
    name = f"omarchy-ai-{version}-linux-x86_64"
    parts = checksum.read_text().strip().split()
    if len(parts) != 2 or parts[1].lstrip("*") != archive.name or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
        raise ValueError("Invalid release checksum file")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != parts[0].lower():
        raise ValueError("Release checksum mismatch")
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if len(members) > 50000 or sum(m.size for m in members) > 2_000_000_000:
            raise ValueError("Release archive exceeds size limits")
        for member in members:
            path = Path(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != name
                    or not (member.isfile() or member.isdir())):
                raise ValueError("Unsafe release archive member")
        bundle.extractall(destination, filter="data")
    root = destination / name
    if installed_version(root) != version:
        raise ValueError("Bundle version does not match filename")
    for required in ("uv.lock", "scripts/setup.sh", "systemd/omarchy-ai.service"):
        if not (root / required).is_file():
            raise ValueError(f"Release missing {required}")
    return root


def _snapshot(directory):
    # setup.sh may change the service, shell plugins/layout and shell rc files.
    # Settings/API keys and conversation state are never replaced by updates.
    paths = [CONFIG / "systemd/user/omarchy-ai.service", CONFIG / "omarchy",
             Path.home() / ".bashrc", Path.home() / ".zshrc"]
    records = []
    for i, path in enumerate(paths):
        backup = directory / str(i)
        exists = path.exists()
        if exists:
            if path.is_dir():
                shutil.copytree(path, backup, symlinks=True)
            else:
                shutil.copy2(path, backup)
        records.append((path, backup, exists))
    return records


def _restore(records):
    for path, backup, existed in records:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        if existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            if backup.is_dir():
                shutil.copytree(backup, path, symlinks=True)
            else:
                shutil.copy2(backup, path)


def _healthy():
    # Check that the new process stays up, rather than just accepting restart.
    pid = None
    ready = False
    for _ in range(10):
        result = subprocess.run(["systemctl", "--user", "show", SERVICE,
                                 "--property=ActiveState", "--property=MainPID"],
                                capture_output=True, text=True, timeout=5, check=True)
        state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if state.get("ActiveState") != "active" or state.get("MainPID") in (None, "0"):
            raise RuntimeError("Updated assistant failed its startup check")
        if pid is not None and state["MainPID"] != pid:
            raise RuntimeError("Updated assistant restarted during startup")
        pid = state["MainPID"]
        control_path = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "omarchy-ai-control.sock"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(str(control_path))
                client.sendall(b"status\n")
                status = json.loads(client.recv(4096))
                ready = status.get("state") in {"listening", "starting", "active"}
        except (OSError, ValueError):
            ready = False
        time.sleep(1)
    if not ready:
        raise RuntimeError("Updated assistant did not become ready on its control socket")


def _report_failure(version, message, previous_root):
    """Best-effort: files a GitHub issue for a failed self-update if this
    machine has a token configured (see core/issues.py), so the failure is
    visible to the maintainer without the user having to do it by hand.
    Returns (issue_url, issue_error) -- exactly one is set, both go into
    install.json so get_update_status can report what actually happened
    (filed / no token / filing itself failed) instead of the live model
    guessing or promising something that didn't happen."""
    from . import issues
    try:
        import platform
        body = (
            f"Automatic report from a self-update failure.\n\n"
            f"- Target version: {version}\n"
            f"- Previous root: {previous_root}\n"
            f"- Platform: {platform.platform()}\n"
            f"- Python: {platform.python_version()}\n\n"
            f"Error:\n```\n{message}\n```\n"
        )
        ok, result = issues.file_issue(f"Self-update to {version} failed", body)
    except Exception as exc:  # noqa: BLE001 -- filing must never mask the real update failure
        log.warning("Could not file GitHub issue for update failure: %s", exc)
        return None, str(exc)
    return (result, None) if ok else (None, result)


def install(version, previous_root):
    version_tuple(version)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        records = []
        switched = False
        def status(state, message, **extra):
            _write(STATE / "install.json", {"state": state, "version": version,
                    "message": message, "updated_at": time.time(), "previous_root": str(previous_root),
                    **extra})
        try:
            status("preparing", "Downloading and verifying the release; current assistant remains running.")
            release = discover()
            if release["latest_version"] != version or version_tuple(version) <= version_tuple(installed_version(previous_root)):
                raise ValueError("Release changed or is not newer; check for updates again")
            RELEASES.mkdir(parents=True, exist_ok=True)
            destination = Path(tempfile.mkdtemp(prefix=f"{version}-", dir=RELEASES))
            archive = destination / release["package"]
            checksum = destination / (release["package"] + ".sha256")
            _require_bundle_url(release.get("package_url"), release["package"])
            _require_bundle_url(release.get("checksum_url"), checksum.name)
            _download(release["package_url"], archive, 1_000_000_000)
            _download(release["checksum_url"], checksum, 4096)
            new_root = unpack_verified(archive, checksum, destination, version)
            # Prepare all dependencies before touching the running installation.
            _command(["bash", "scripts/check-dependencies.sh"], cwd=new_root)
            _command(["uv", "venv", "--system-site-packages", "--python", "/usr/bin/python3", ".venv"], cwd=new_root)
            _command(["uv", "sync", "--locked"], cwd=new_root)
            _command([str(new_root / ".venv/bin/python"), "-c", "import omarchy_ai.core.daemon"], cwd=new_root)
            backup = destination / "rollback"
            backup.mkdir(mode=0o700)
            records = _snapshot(backup)
            status("installing", "Installing verified update and restarting the assistant.")
            # Allow the voice tool result to be spoken even with cached downloads.
            time.sleep(8)
            switched = True
            _command(["systemctl", "--user", "stop", SERVICE], timeout=120)
            _command(["bash", "scripts/setup.sh"], cwd=new_root)
            _command(["systemctl", "--user", "start", SERVICE], timeout=120)
            _healthy()
            status("completed", f"Updated to {version}; startup verified. Previous installation retained at {previous_root}.")
        except Exception as exc:
            message = str(exc)
            if switched:
                try:
                    _command(["systemctl", "--user", "stop", SERVICE], timeout=120)
                    _restore(records)
                    _command(["systemctl", "--user", "daemon-reload"])
                    _command(["systemctl", "--user", "start", SERVICE], timeout=120)
                    _command(["omarchy-shell", "shell", "rescanPlugins"])
                    _healthy()
                    message += "; previous installation restored and running"
                except Exception as rollback_error:
                    message += f"; rollback needs attention: {rollback_error}"
            issue_url, issue_error = _report_failure(version, message, previous_root)
            status("failed", message, issue_url=issue_url, issue_error=issue_error)
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", required=True)
    parser.add_argument("--previous-root", required=True, type=Path)
    args = parser.parse_args()
    install(args.install, args.previous_root)

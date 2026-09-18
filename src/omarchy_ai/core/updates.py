"""Versioned GitHub bundles, cached wake notices, and isolated self-updates.

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


def _json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Omarchy-AI-Updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("GitHub response too large")
    return json.loads(data)


def discover():
    # Bundles are published in dist/, not GitHub Releases (which also hosts
    # unversioned demo media). Pin BOTH downloads to one immutable commit.
    commit = _json(API + "/commits/main")["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid GitHub commit")
    contents = _json(API + "/contents/dist?ref=" + commit)
    names = {item["name"] for item in contents if item.get("type") == "file"}
    versions = [(version_tuple(match[1]), match[1], name) for name in names
                if (match := PACKAGE.fullmatch(name)) and name + ".sha256" in names]
    if not versions:
        raise ValueError("No versioned install bundle with a checksum is published")
    _, version, name = max(versions)
    return {"latest_version": version, "commit": commit, "package": name,
            "url": f"https://github.com/{REPOSITORY}/tree/{commit}/dist"}


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
        except Exception as exc:
            result = {"ok": False, "available": False, "checked_at": now,
                      "current_version": current, "error": f"Could not check GitHub: {exc}"}
            log.warning("%s", result["error"])
        _write(STATE / "check.json", result)
        return result


def wake_notice():
    """Read cached evidence only; never add network latency to session setup."""
    cache = _read(STATE / "check.json")
    try:
        if (cache.get("ok") and 0 <= time.time() - cache["checked_at"] < CACHE_SECONDS * 2
                and version_tuple(cache["latest_version"]) > version_tuple(installed_version())):
            return (f"Omarchy AI version {cache['latest_version']} is available. "
                    "I recommend updating. You can say 'update yourself' to install it.")
    except (KeyError, ValueError, TypeError, OSError):
        pass
    return ""


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


def install(version, previous_root):
    version_tuple(version)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        records = []
        switched = False
        def status(state, message):
            _write(STATE / "install.json", {"state": state, "version": version,
                    "message": message, "updated_at": time.time(), "previous_root": str(previous_root)})
        try:
            status("preparing", "Downloading and verifying the release; current assistant remains running.")
            release = discover()
            if release["latest_version"] != version or version_tuple(version) <= version_tuple(installed_version(previous_root)):
                raise ValueError("Release changed or is not newer; check for updates again")
            RELEASES.mkdir(parents=True, exist_ok=True)
            destination = Path(tempfile.mkdtemp(prefix=f"{version}-", dir=RELEASES))
            archive = destination / release["package"]
            checksum = destination / (release["package"] + ".sha256")
            base = f"https://raw.githubusercontent.com/{REPOSITORY}/{release['commit']}/dist/"
            _download(base + archive.name, archive, 1_000_000_000)
            _download(base + checksum.name, checksum, 4096)
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
            status("failed", message)
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", required=True)
    parser.add_argument("--previous-root", required=True, type=Path)
    args = parser.parse_args()
    install(args.install, args.previous_root)

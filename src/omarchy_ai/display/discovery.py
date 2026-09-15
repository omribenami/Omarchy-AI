"""Android TV discovery over mDNS (avahi) -- auto-discovery for casting and
the "which TV?" voice disambiguation flow, plus the two service types the
new-TV pairing walkthrough needs (`execution/actions.py`'s
`install_receiver_on_tv`).

Real, verified evidence this design is built on (`avahi-browse -a -t` run
live on this network, 2026-09-14 -- see STATUS.md/ADR-0001 for the full
trail): the network surfaces two different kinds of Android-TV-adjacent
mDNS service, and they are NOT interchangeable for this project's purpose:

- `_androidtvremote2._tcp` -- advertised by real Android TV OS devices (the
  protocol the official "Android TV Remote Control" / Google Home app uses
  to pair as a remote). Confirmed live: three real devices, each a genuine
  Android TV box/projector -- "Living Room TV" (192.168.1.191), "Idol TV"
  (192.168.1.203), "Philips 4K A1" (present but its resolve intermittently
  times out -- see `_run_avahi_browse`). This is the service type used
  here for cast targets: the closest real signal to "a device that could
  plausibly run our receiver app", even though the port it advertises
  (6466) is the remote-control protocol's own port, not ADB's.
- `_googlecast._tcp` -- a much broader "any Cast-capable device" service.
  Confirmed live to also include plain Chromecasts, Google Home/Nest smart
  speakers, and a Nest Hub smart display -- none of which run Android TV OS
  or can have an APK installed via adb. Concrete proof from the same live
  browse: one _googlecast._tcp entry (friendly name "Idol TV") shares its
  IP with an _androidtvremote2._tcp entry of the same name, but three more
  _googlecast._tcp entries ("Nest Audio", "Google Home", "Google Nest Hub")
  have no _androidtvremote2._tcp counterpart at all -- real evidence
  they're general smart-home noise, not adb-installable cast targets.
  Deliberately NOT browsed here.

The actual ADB debug port is separate from anything androidtvremote2
advertises -- this project's already-paired TV listens on the standard
5555 (classic `adb tcpip 5555` setup). androidtvremote2 discovery is used
only to learn *which IP* to try that against; a discovered device that
was never adb-enabled will still just fail to connect, the same as a
manually typed wrong IP would.

Pairing a brand-new, never-connected TV is a distinct real constraint, not
solved by the above: Android's own Wireless Debugging (Settings >
Developer options > Wireless debugging) only starts advertising
`_adb-tls-pairing._tcp` (while its "Pair device with pairing code" screen
is open) and `_adb-tls-connect._tcp` (whenever the toggle is on at all)
once the user has turned it on *on the TV itself* -- confirmed live on
this network: both come back empty right now, matching that no TV
currently has it enabled. There is no way to skip that first on-device
step; see `install_receiver_on_tv` for the guided flow built around it.

**A fourth service type, added after a real device was found invisible to
the above:** the user's HY300Pro Android projector (`_TV_ADB_ADDR`,
192.168.1.86 -- the very first TV this project ever paired, back in
Phase 0) advertises none of the three service types above -- confirmed
live, `avahi-browse -a -t` showed zero results for it under
`_androidtvremote2._tcp`, `_adb-tls-pairing._tcp`, or `_adb-tls-connect
._tcp`. It DOES advertise plain `_adb._tcp` (Android's classic
ADB-over-network service -- confirmed live: `avahi-browse -r -p -t
_adb._tcp` -> `adb-52001089a69606f2054;_adb._tcp;...;192.168.1.86;5555`,
and a direct query against that address, `adb shell getprop
ro.product.model`, returned `HY300Pro`, proving it really is the same
device). This is common on non-GMS-certified/budget Android boxes and
projectors: Wireless Debugging (which is all `_adb._tcp` signals -- the
device already has it toggled on) is a lower bar than being a
GMS-certified Android TV OS build that ships the Google Android TV Remote
Service. `discover_androidtv_devices()` below now also browses
`_adb._tcp` and folds in any device not already covered by an
`_androidtvremote2._tcp` result (deduped by IP) so devices like this one
still show up as real cast targets -- see that function's docstring for
how its (unhelpful, serial-based) mDNS name is resolved to something
usable.
"""

from __future__ import annotations

import logging
import re
import subprocess

log = logging.getLogger("omarchy_ai.display.discovery")

# The one TV that's been manually paired and proven working since Phase 0
# (STATUS.md's "Hardware-dependent findings" -- adb tcpip 5555, not
# Wireless Debugging). Lives here (not execution/actions.py, its original
# home) so both actions.py and registry.py can depend on discovery.py
# without a circular import between the two of them.
TV_ADB_ADDR = "192.168.1.86:5555"

# `avahi-browse -r -p -t` resolves every currently-advertised instance of a
# service type before terminating (the `-t` flag). A record that fails to
# resolve still costs a real wait -- confirmed live, timing this exact
# command against this network: ~5s wall-clock with one unresolvable
# instance present ("Philips 4K A1", intermittently). The subprocess
# timeout below is set comfortably above that observed cost, not guessed.
_BROWSE_TIMEOUT = 8.0

# avahi-browse's parseable (-p) output escapes non-alphanumeric bytes in
# names as decimal `\DDD` (e.g. a space is `\032`), and literal dots as
# `\.`. Confirmed against real captured output from this network.
_DECIMAL_ESCAPE_RE = re.compile(r"\\(\d{3})")


def _unescape(name: str) -> str:
    name = _DECIMAL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1))), name)
    return name.replace("\\.", ".").replace("\\\\", "\\")


def _run_avahi_browse(service_type: str) -> list[dict]:
    """Runs `avahi-browse -r -p -t <service_type>` and parses the resolved
    ("=") rows. Never raises -- returns [] if avahi-browse isn't installed,
    times out, or nothing resolves; callers are expected to have a
    fallback rather than treat empty as an error."""
    try:
        proc = subprocess.run(
            ["avahi-browse", "-r", "-p", "-t", service_type],
            capture_output=True,
            text=True,
            timeout=_BROWSE_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        log.warning("avahi-browse is not installed -- TV discovery unavailable")
        return []
    except subprocess.TimeoutExpired:
        log.warning("avahi-browse timed out browsing %s", service_type)
        return []

    if proc.stderr:
        # Real, expected noise: a "+seen" record whose resolve later times
        # out lands here, one line per attempt, not an error worth surfacing.
        for line in proc.stderr.splitlines():
            log.debug("avahi-browse: %s", line)

    records = []
    for line in proc.stdout.splitlines():
        if not line.startswith("="):
            continue  # "+" = seen, not yet resolved; only "=" rows carry an address
        fields = line.split(";")
        if len(fields) < 9:
            continue
        # =;iface;protocol;name;type;domain;hostname;address;port;txt...
        _, _iface, protocol, name, _type, _domain, hostname, address, port = fields[:9]
        records.append(
            {
                "name": _unescape(name),
                "protocol": protocol,
                "hostname": hostname,
                "address": address,
                "port": int(port) if port.isdigit() else None,
            }
        )
    return records


def _dedupe_prefer_ipv4(records: list[dict]) -> list[dict]:
    """One entry per device name. IPv4 preferred over IPv6 -- adb on this
    network has only ever been exercised against IPv4 addresses (the
    already-paired TV's address is IPv4); no reason to hand the model an
    IPv6 literal it, and adb, have never been proven against."""
    by_name: dict[str, dict] = {}
    for r in records:
        existing = by_name.get(r["name"])
        if existing is None or (existing["protocol"] == "IPv6" and r["protocol"] == "IPv4"):
            by_name[r["name"]] = r
    return list(by_name.values())


# Per-subprocess timeout for the adb calls used to resolve a friendly name
# for an _adb._tcp-only device (adb connect, then getprop). This sits on
# the voice assistant's hot path (list_cast_targets/start_casting can be
# called mid-conversation), so each call gets a short, hard timeout rather
# than adb's own default -- a device that's slow/unreachable falls back to
# the raw mDNS instance name instead of stalling discovery. `adb connect`
# against an already-connected device returns near-instantly (confirmed
# live: "already connected"), so this is generous, not tight.
_ADB_MODEL_TIMEOUT = 3.0

# Resolved model names are cached for the life of the process -- a device's
# model doesn't change between calls, and re-running two adb round trips on
# every single list_cast_targets call (this repo's own discovery.py hot-path
# concern) would be wasted work for a handful of stable home-network
# devices. Deliberately unbounded/no TTL: correctness-first, small list,
# per the task's own "don't over-engineer" call.
_adb_model_name_cache: dict[str, str] = {}


def _resolve_adb_model_name(address: str, port: int | None, fallback: str) -> str:
    """Best-effort friendly name for a device only seen via `_adb._tcp`
    (whose mDNS instance name is just `adb-<serial>`, not useful to read
    out loud) -- queries `ro.product.model` over adb. Falls back to
    `fallback` (the raw mDNS name) on any timeout/error rather than ever
    raising or blocking discovery."""
    addr = f"{address}:{port or 5555}"
    cached = _adb_model_name_cache.get(addr)
    if cached is not None:
        return cached

    name = fallback
    try:
        subprocess.run(
            ["adb", "connect", addr],
            capture_output=True, text=True, timeout=_ADB_MODEL_TIMEOUT, check=False,
        )
        proc = subprocess.run(
            ["adb", "-s", addr, "shell", "getprop", "ro.product.model"],
            capture_output=True, text=True, timeout=_ADB_MODEL_TIMEOUT, check=False,
        )
        model = proc.stdout.strip()
        if proc.returncode == 0 and model:
            name = model
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("could not resolve model name for %s via adb, using mDNS name %r", addr, fallback)

    _adb_model_name_cache[addr] = name
    return name


def discover_adb_tcp_devices() -> list[dict]:
    """Every plain `_adb._tcp` device currently on the network -- Android's
    classic Wireless-Debugging-on / ADB-over-network advertisement, a lower
    bar than `_androidtvremote2._tcp` (see module docstring for the real
    HY300Pro projector this was added for). [{"name", "address", "port"},
    ...], deduped IPv4-preferred like the other discover_* functions."""
    records = _dedupe_prefer_ipv4(_run_avahi_browse("_adb._tcp"))
    return [{"name": r["name"], "address": r["address"], "port": r["port"]} for r in records]


def discover_androidtv_devices() -> list[dict]:
    """Real mDNS discovery of cast targets: every `_androidtvremote2._tcp`
    device (the primary signal -- see module docstring), PLUS any
    `_adb._tcp`-only device not already covered by one of those (deduped by
    IP) -- devices with Wireless Debugging on but no Google Android TV
    Remote Service (common on non-GMS-certified/budget Android boxes and
    projectors, confirmed live against the HY300Pro projector). One entry
    per device -- [{"name", "address"}, ...]. Not cached (the merge itself
    is not cached), not hardcoded: reflects whatever is actually
    broadcasting at call time, except that an `_adb._tcp`-only device's
    *name* is resolved once via adb and cached (see
    `_resolve_adb_model_name`). Returns [] on any discovery failure or if
    nothing is found; callers decide the fallback."""
    androidtvremote = _dedupe_prefer_ipv4(_run_avahi_browse("_androidtvremote2._tcp"))
    devices = [{"name": r["name"], "address": r["address"]} for r in androidtvremote]

    known_addresses = {r["address"] for r in androidtvremote}
    for r in _dedupe_prefer_ipv4(_run_avahi_browse("_adb._tcp")):
        if r["address"] in known_addresses:
            continue  # already have a nicer androidtvremote2 name for this IP
        name = _resolve_adb_model_name(r["address"], r["port"], fallback=r["name"])
        devices.append({"name": name, "address": r["address"]})

    return devices


def discover_adb_tls_pairing() -> list[dict]:
    """New-TV pairing signal: `_adb-tls-pairing._tcp` is only advertised
    while a TV is actually showing its 'Pair device with pairing code'
    screen. Confirmed live on this network (2026-09-14): zero results,
    matching that no TV currently has that screen open -- an empty list
    here is the expected/common case, not a bug."""
    records = _dedupe_prefer_ipv4(_run_avahi_browse("_adb-tls-pairing._tcp"))
    return [{"name": r["name"], "address": r["address"], "port": r["port"]} for r in records]


def discover_adb_tls_connect() -> list[dict]:
    """The general wireless-debugging reconnect address: `_adb-tls-connect
    ._tcp` is advertised whenever a TV's Wireless debugging toggle is on at
    all (not just mid-pairing). Needed after a fresh `adb pair` because
    Android's own Wireless Debugging feature assigns a random port here --
    unlike the fixed 5555 the classic (USB-first) `adb tcpip 5555` method
    uses, which is what this project's already-paired TV was set up with."""
    records = _dedupe_prefer_ipv4(_run_avahi_browse("_adb-tls-connect._tcp"))
    return [{"name": r["name"], "address": r["address"], "port": r["port"]} for r in records]

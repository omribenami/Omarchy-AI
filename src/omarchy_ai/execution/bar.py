"""Discover live bar entries and address panels by exact plugin ID."""
import json
import os
import subprocess


def _ipc(*args):
    result = subprocess.run(
        ["omarchy-shell", "shell", *args], capture_output=True, text=True,
        timeout=8, env={**os.environ, "OMARCHY_PATH": os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy"},
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Shell call failed")
    return result.stdout.strip()


def entries():
    plugins = {p["id"]: p for p in json.loads(_ipc("listPlugins"))}
    layout = json.loads(_ipc("listShellConfig")).get("bar", {}).get("layout", {})
    result = []
    for section in ("left", "center", "right"):
        for position, entry in enumerate(layout.get(section, []), 1):
            ident = entry if isinstance(entry, str) else entry.get("id")
            plugin = plugins.get(ident, {})
            if not plugin.get("enabled"):
                continue
            result.append({"number": len(result) + 1, "id": ident,
                           "name": plugin.get("name", ident), "section": section,
                           "position": position})
    return result


def open_panel(target):
    # Require an exact ID from discovery, never fuzzy-dispatch an unrelated app.
    available = entries()
    match = next((entry for entry in available if entry["id"] == target), None)
    if match is None:
        return False, "Unknown or disabled bar icon. Call list_bar_icons and use an exact ID; ask the user which number if ambiguous."
    reply = _ipc("summon", target, "{}")
    if reply != "ok":
        return False, f"Shell could not open {target}: {reply}. This icon may not expose a panel. Do not substitute Agent or another command."
    return True, f"Shell accepted opening {match['name']} ({target}). Visual rendering is not independently verified."


def close_panel(target):
    """Dismiss a bar panel without toggling it or closing the app underneath."""
    match = next((entry for entry in entries() if entry["id"] == target), None)
    if match is None:
        return False, "Unknown or disabled bar icon. Call list_bar_icons and use an exact ID."
    # shell.hide is a void IPC method: success has an empty response, not 'ok'.
    # Never toggle here: a retry must not reopen a dismissed panel.
    reply = _ipc("hide", target)
    if reply:
        return False, f"Unexpected shell response closing {target}: {reply}"
    return True, (f"Dismiss request sent for {match['name']} ({target}). "
                  "Visual closure is not independently verified; inspect the screen if needed.")

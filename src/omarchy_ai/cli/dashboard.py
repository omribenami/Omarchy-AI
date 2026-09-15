"""Live terminal dashboard for the MyApi integration — `omarchy-ai-dashboard`.

Answers "which services are connected, and how much is each Omarchy AI
tool/service actually using them" without leaving the terminal. Two data
sources, refreshed at different rates on purpose:

- The connected-services list comes from one signed MyApi API call
  (MyApiClient.list_services), re-polled every REMOTE_REFRESH_SECONDS —
  slow enough not to hammer the API, since this loop otherwise redraws
  much more often.
- Call counts/success-rate come from the local usage log
  (myapi/usage.py), re-read every LOCAL_REFRESH_SECONDS — cheap (one file
  read, no network), so this is what makes the dashboard feel live: a
  voice conversation calling myapi_call updates this within a couple of
  seconds.

Uses `rich` (the only TUI dependency this project has — added specifically
for this) rather than hand-rolled curses/ANSI, for real color/table/panel
rendering with minimal code.
"""

from __future__ import annotations

import sys
import time

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import myapi
from ..myapi import usage as myapi_usage

ACCENT = "#39ff88"  # this project's own glow color — Watch Dogs overlay, phone bridge page
ERROR = "#ff6b6b"

LOCAL_REFRESH_SECONDS = 1.5
REMOTE_REFRESH_SECONDS = 60.0

# Relative-volume bar, same unicode-block vocabulary as the Watch Dogs
# overlay's ASCII/braille audio visualizer — a consistent visual language
# across this project's terminal/HUD surfaces rather than a one-off look
# just for this screen.
_BAR_CHARS = " ▁▂▃▄▅▆▇█"


def _bar_share(share: float, width: int = 12) -> str:
    """share is 0..1 of the whole (e.g. this service's fraction of total
    calls across every service) — the terminal equivalent of the
    omarchy-ai.myapi bar panel's proportional-fill usage rows."""
    filled = max(0.0, min(1.0, share))
    out = []
    for i in range(width):
        cell = filled * width - i
        idx = max(0, min(len(_BAR_CHARS) - 1, round(cell * (len(_BAR_CHARS) - 1))))
        out.append(_BAR_CHARS[idx] if cell > 0 else _BAR_CHARS[0])
    return "".join(out)


def _fmt_ago(ts: float) -> str:
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


class _DashboardState:
    def __init__(self) -> None:
        self.client = myapi.MyApiClient()
        self.connected_services: list[dict] | None = None
        self.remote_error: str | None = None
        self.last_remote_refresh = 0.0

    def refresh_remote(self, *, force: bool = False) -> None:
        if not self.client.connected:
            return
        now = time.time()
        if not force and (now - self.last_remote_refresh) < REMOTE_REFRESH_SECONDS:
            return
        self.last_remote_refresh = now
        try:
            resp = self.client.list_services()
        except myapi.MyApiError as e:
            self.remote_error = str(e)
            return
        self.remote_error = None
        services = resp.get("services") if isinstance(resp, dict) else resp
        self.connected_services = services if isinstance(services, list) else []


def _render(state: _DashboardState) -> Group:
    header = Text()
    header.append("OMARCHY AI ", style=f"bold {ACCENT}")
    header.append("— MyApi dashboard\n", style="bold")
    if not state.client.connected:
        header.append("Not connected. ", style=ERROR)
        header.append("Connect from the Omarchy AI settings panel (\"Connect services to Omarchy AI\").")
    elif state.remote_error:
        header.append(f"Connected, but couldn't refresh from myapiai.com just now: {state.remote_error}", style=ERROR)
    else:
        account = state.client.account or "MyApi"
        n = len(state.connected_services or [])
        header.append(f"Connected as {account} — {n} service(s) available.", style=ACCENT)

    header_panel = Panel(header, border_style=ACCENT, expand=True)

    agg = myapi_usage.aggregate()
    remote_names = {
        (s.get("id") or s.get("name") or "?") for s in (state.connected_services or []) if isinstance(s, dict)
    }
    all_names = sorted(set(agg) | remote_names)

    services_table = Table(title="Connected services", expand=True, border_style=ACCENT)
    services_table.add_column("Service")
    services_table.add_column("Connected", justify="center")
    services_table.add_column("Calls today", justify="right")
    services_table.add_column("Calls (2wk)", justify="right")
    services_table.add_column("Success", justify="right")
    services_table.add_column("Last used")
    services_table.add_column("Share")

    # Share of the whole, not "relative to the busiest service" — same
    # semantics as the omarchy-ai.myapi bar panel's usage rows (and, one
    # level further up, this bar's own Agents panel ModelRow: a row's fill
    # is its proportion of the total, not of the largest row), so the two
    # surfaces read the same way.
    total_calls = sum(s.get("calls_total", 0) for s in agg.values())
    if not all_names:
        services_table.add_row("—", "—", "—", "—", "—", "no MyApi calls yet", "")
    for name in all_names:
        stats = agg.get(name, {"calls_total": 0, "calls_today": 0, "ok_total": 0, "last_ts": 0.0})
        connected_mark = Text("✓", style=ACCENT) if name in remote_names else Text("·", style="dim")
        total = stats["calls_total"]
        ok_pct = f"{(stats['ok_total'] / total * 100):.0f}%" if total else "—"
        share = total / total_calls if total_calls else 0.0
        services_table.add_row(
            name,
            connected_mark,
            str(stats["calls_today"]),
            str(total),
            ok_pct,
            _fmt_ago(stats["last_ts"]),
            Text(_bar_share(share), style=ACCENT),
        )

    tool_table = Table(title="By Omarchy AI tool", expand=True, border_style=ACCENT)
    tool_table.add_column("Tool")
    tool_table.add_column("Calls (2wk)", justify="right")
    tool_table.add_column("Success", justify="right")
    for name in all_names:
        stats = agg.get(name)
        if not stats:
            continue
        total = stats["calls_total"]
        ok_pct = f"{(stats['ok_total'] / total * 100):.0f}%" if total else "—"
        tool_table.add_row(f"myapi_call(service={name})", str(total), ok_pct)
    if not any(agg.get(n) for n in all_names):
        tool_table.add_row("no tool calls yet", "—", "—")

    footer = Text("Ctrl+C to exit — refreshes usage every ~1.5s, service list every 60s.", style="dim")

    return Group(header_panel, services_table, tool_table, footer)


def main(argv: list[str] | None = None) -> int:
    console = Console()
    state = _DashboardState()
    state.refresh_remote(force=True)

    try:
        with Live(_render(state), console=console, refresh_per_second=4, screen=False) as live:
            while True:
                time.sleep(LOCAL_REFRESH_SECONDS)
                state.refresh_remote()
                live.update(_render(state))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

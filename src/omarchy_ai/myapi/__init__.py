"""MyApi (myapiai.com) integration — connects Omarchy AI to a user's
MyApi account (Gmail, Calendar, Drive, Notion, Slack, and 200+ other
connected services) so the voice assistant can call a real API instead of
opening a browser and taking screenshots.

See client.py for the connection/signing mechanics and usage.py for the
local call log the terminal dashboard (cli/dashboard.py) reads.
"""

from __future__ import annotations

from .client import MyApiClient, MyApiError, disconnect, is_connected

__all__ = ["MyApiClient", "MyApiError", "disconnect", "is_connected"]

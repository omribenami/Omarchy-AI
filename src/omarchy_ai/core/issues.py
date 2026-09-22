"""Report a problem as a new GitHub issue on this project's own repo.

Opt-in and per-machine: nothing is ever filed unless a personal token is
stored locally (cli/settings.py's set-github-issue-token writes it 0600,
same pattern as the Gemini/Vercel keys in config.py). No token ships with
any install. The intended token is a fine-grained PAT scoped to just
Issues: write on this one public repo -- bounded the same way anyone with
a GitHub account could already open an issue by hand; this only automates
that, it doesn't grant a machine anything a random visitor couldn't
already do through the web UI.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..config import CONFIG_DIR

log = logging.getLogger(__name__)

REPOSITORY = "omribenami/Omarchy-AI"
TOKEN_PATH = CONFIG_DIR / "github-issue-token"
_TITLE_LIMIT = 250
_BODY_LIMIT = 60_000  # GitHub's real cap is far higher; this is just a local sanity guard


def has_token() -> bool:
    return TOKEN_PATH.is_file()


def _read_token() -> str | None:
    try:
        token = TOKEN_PATH.read_text().strip()
    except OSError:
        return None
    return token or None


def file_issue(title: str, body: str) -> tuple[bool, str]:
    """POSTs a new issue to REPOSITORY. Returns (ok, message): message is
    the created issue's html_url on success, or a short human-readable
    failure reason on failure -- never a raw exception/traceback, since
    this message can end up read aloud or written into a local report."""
    token = _read_token()
    if not token:
        return False, "no GitHub issue token configured on this machine"
    title = (title or "").strip()[:_TITLE_LIMIT] or "Omarchy AI issue"
    payload = json.dumps({"title": title, "body": (body or "")[:_BODY_LIMIT]}).encode()
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/issues",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "omarchy-ai-issue-reporter",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read(200_000))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read(2000)).get("message", "")
        except (ValueError, OSError):
            pass
        log.warning("GitHub issue creation failed: HTTP %s %s", exc.code, detail)
        if exc.code == 401:
            return False, "GitHub token was rejected (invalid or expired)"
        if exc.code == 403:
            return False, "GitHub token lacks permission to file issues on this repo" + (f": {detail}" if detail else "")
        return False, f"GitHub rejected the issue (HTTP {exc.code})" + (f": {detail}" if detail else "")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("GitHub issue creation failed: %s", exc)
        return False, f"could not reach GitHub: {exc}"
    url = data.get("html_url")
    if not url:
        return False, "GitHub accepted the request but returned no issue URL"
    log.info("Filed GitHub issue: %s", url)
    return True, url

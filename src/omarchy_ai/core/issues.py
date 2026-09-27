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
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from ..config import CONFIG_DIR

log = logging.getLogger(__name__)

REPOSITORY = "omribenami/Omarchy-AI"
# Spoken names for repos the user reports to. Omarchy the OS moved from
# basecamp/omarchy to omacom/omarchy (gh repo view, 2026-09-26); the
# 2026-09-26 23:05-23:40 sessions spent 35 minutes failing to file an
# iwlwifi freeze there because report_issue could only reach this repo.
REPO_ALIASES = {
    "omarchy": "omacom/omarchy", "basecamp/omarchy": "omacom/omarchy",
    "omarchy-ai": REPOSITORY, "omarchy ai": REPOSITORY,
}
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
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


def resolve_repo(repo: str | None) -> str | None:
    """owner/name for a spoken or written repo; None if it is not one."""
    name = (repo or "").strip().removeprefix("https://github.com/").strip("/")
    if not name:
        return REPOSITORY
    name = REPO_ALIASES.get(name.lower(), name)
    return name if _REPO_RE.match(name) else None


def _file_with_gh(repo: str, title: str, body: str) -> tuple[bool, str]:
    """The user's own logged-in GitHub CLI: works for any repo they can
    open issues on, with no separate token."""
    try:
        done = subprocess.run(["gh", "issue", "create", "-R", repo, "--title", title, "--body-file", "-"],
                              input=body, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"gh could not file the issue: {exc}"
    url = next((w for w in done.stdout.split() if w.startswith("https://github.com/")), "")
    if done.returncode or not url:
        detail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
        return False, f"gh could not file the issue: {detail[0][:200]}"
    log.info("Filed GitHub issue with gh: %s", url)
    return True, url


def _gh_ready() -> bool:
    if not shutil.which("gh"):
        return False
    try:
        return subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def file_issue(title: str, body: str, repo: str | None = None, use_gh: bool = False) -> tuple[bool, str]:
    """Opens a new issue on `repo` (default REPOSITORY). Returns (ok,
    message): message is the created issue's html_url on success, or a
    short human-readable failure reason on failure -- never a raw
    exception/traceback, since this message can end up read aloud or
    written into a local report. Uses the stored token when there is one;
    `use_gh` (only for an issue the user asked for, never the automatic
    reports, which stay opt-in by token) falls back to the logged-in gh."""
    target = resolve_repo(repo)
    if not target:
        return False, f"{repo!r} is not a GitHub repository name (expected owner/name)"
    title = (title or "").strip()[:_TITLE_LIMIT] or "Omarchy AI issue"
    token = _read_token()
    if not token:
        if use_gh and _gh_ready():
            return _file_with_gh(target, title, (body or "")[:_BODY_LIMIT])
        return False, "no GitHub issue token configured on this machine" + (" and gh is not logged in" if use_gh else "")
    payload = json.dumps({"title": title, "body": (body or "")[:_BODY_LIMIT]}).encode()
    request = urllib.request.Request(
        f"https://api.github.com/repos/{target}/issues",
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

"""MyApi (myapiai.com) passthrough and Gmail tools.

Split out of the former execution/actions.py; see execution/actions/__init__.py."""

from __future__ import annotations

import json
import re
import shlex
import time
import base64
import urllib.parse
import urllib.request
from pathlib import Path

from ... import myapi
from ...config import load_config
from ...core import effects
from ...myapi import usage as myapi_usage
from .. import files as local_files
from .base import ActionResult


# --- MyApi (myapiai.com) passthrough ------------------------------------
# See src/omarchy_ai/myapi/. Only added to the model's tool list at all
# when a connection actually exists (voice/live.py's build_session_config
# checks myapi.is_connected() before including these) -- they still guard
# for "not connected" themselves too, since this same ACTIONS dict is also
# reachable from the phone bridge (phone/server.py), which doesn't share
# that per-session check.
#
# Reads and writes deliberately use separate actions. myapi_call remains
# GET-only; InputGuard applies a conversation-scoped exact-payload
# confirmation before myapi_write can reach this module.


def myapi_vault_list(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect it from the Omarchy AI settings panel first.")
    from ...myapi import vault
    try:
        return ActionResult(True, json.dumps(vault.labels(), ensure_ascii=False))
    except myapi.MyApiError as e:
        return ActionResult(False, f"couldn't read the MyApi vault: {e}")


def myapi_list_services(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect it from the Omarchy AI settings panel first.")
    try:
        result = myapi.MyApiClient().list_services()
    except myapi.MyApiError as e:
        return ActionResult(False, f"couldn't reach MyApi: {e}")
    return ActionResult(True, json.dumps(result))


def myapi_service_methods(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect it from the Omarchy AI settings panel first.")
    service = args.get("service")
    if not isinstance(service, str) or not service.strip():
        return ActionResult(False, "no service given")
    try:
        result = myapi.MyApiClient().service_methods(service.strip())
    except myapi.MyApiError as e:
        return ActionResult(False, f"couldn't reach MyApi: {e}")
    return ActionResult(True, json.dumps(result))


def _gmail_messages(value: object) -> list:
    """Find a provider's nested Gmail message list without assuming wrappers."""
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, list):
            return messages
        for child in value.values():
            found = _gmail_messages(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _gmail_messages(child)
            if found:
                return found
    return []


def _gmail_fallback_queries(query: str) -> list[str]:
    """Plan increasingly broad Gmail queries while preserving hard filters.

    Human text (plain terms plus from/to/subject values) may be unqualified,
    punctuation-normalized, or ORed. Dates, mailbox state, attachment, label,
    filename, size, and unknown operators remain attached to every attempt.
    """
    query = query.strip()
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()
    text_values: list[str] = []
    hard: list[str] = []
    text_operators = {"from", "to", "cc", "bcc", "subject"}
    boolean_words = {"or", "and"}
    for token in tokens:
        clean = token.strip()
        if not clean or clean.casefold() in boolean_words:
            continue
        if clean.startswith("-"):
            hard.append(clean)
            continue
        if ":" in clean:
            operator, value = clean.split(":", 1)
            if operator.casefold() in text_operators and value:
                text_values.append(value)
            else:
                hard.append(clean)
        else:
            text_values.append(clean.strip("(){}"))
    text_values = [value for value in text_values if value]
    if not text_values:
        return []

    def changed(value: str, replacement: str) -> str:
        return re.sub(r"(?<=\w)[‐‑‒–—-](?=\w)", replacement, value)

    def quote(value: str) -> str:
        return json.dumps(value, ensure_ascii=False) if any(c.isspace() for c in value) else value

    candidates: list[str] = []

    def add(values: list[str], *, alternatives: bool = False) -> None:
        values = list(dict.fromkeys(value for value in values if value))
        if not values:
            return
        text = " ".join(quote(value) for value in values)
        if alternatives and len(values) > 1:
            text = "{" + text + "}"
        candidate = " ".join([text, *hard]).strip()
        if candidate != query and candidate not in candidates:
            candidates.append(candidate)

    # Remove text-field qualifiers first, but keep all words as an AND query.
    add(text_values)
    spaced = [changed(value, " ") for value in text_values]
    collapsed = [changed(value, "") for value in text_values]
    add(spaced)
    add(collapsed)
    # Last resort: fetch a bounded union of candidates. Jev receives their
    # payloads and the original request, so it can identify the relevant one.
    alternatives: list[str] = []
    for original, with_space, without_space in zip(text_values, spaced, collapsed):
        alternatives.extend((original, with_space, without_space))
    add(alternatives, alternatives=True)
    return candidates[:5]


# The whole digest stays under the MyApi refinement threshold (16k chars), so
# she gets every message whole rather than a record Jev picks from the raw
# payload. 2026-09-30 13:29-13:35: from ~1 MB per search it picked the first
# message's attachment list or its headers, six searches in a row.
_GMAIL_DIGEST_CHARS = 14_000


def _gmail_digest(messages: list) -> list[dict]:
    """Each message as she needs it: who, when, what, the thread to reply in,
    and its text, shortened evenly to fit."""
    rows = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        preview = message.get("preview") if isinstance(message.get("preview"), dict) else {}
        attachments = [a.get("filename") for a in message.get("attachmentList") or []
                       if isinstance(a, dict) and a.get("filename")]
        rows.append({
            "message_id": message.get("messageId") or message.get("id"),
            "thread_id": message.get("threadId"),
            "date": message.get("messageTimestamp"),
            "from": message.get("sender"),
            "to": message.get("to"),
            "subject": message.get("subject") or preview.get("subject"),
            "unread": "UNREAD" in (message.get("labelIds") or []),
            "sent": "SENT" in (message.get("labelIds") or []),  # the user wrote it
            "attachments": attachments[:10],
            "text": " ".join(str(message.get("messageText") or preview.get("body") or "").split()),
        })
    if rows:
        fixed = sum(len(json.dumps({**row, "text": ""}, ensure_ascii=False)) for row in rows)
        room = max(200, (_GMAIL_DIGEST_CHARS - fixed) // len(rows))
        for row in rows:
            if len(row["text"]) > room:
                row["text"] = row["text"][:room] + "…"
    return rows


def _gmail_search_with_fallback(query: str, maximum: int = 10) -> tuple[dict, list[str]]:
    # Without the raw MIME payload: the message text, sender, subject, thread and
    # attachment names still come, 6x faster (16.6 s vs 2.7 s for five messages,
    # measured 2026-09-30). Attachment downloads fetch their message on their own.
    attempted = [query]
    result = _gmail_execute("GMAIL_FETCH_EMAILS", {
        "query": query, "max_results": maximum, "include_payload": False,
    })
    for candidate in _gmail_fallback_queries(query):
        if _gmail_messages(result):
            break
        attempted.append(candidate)
        result = _gmail_execute("GMAIL_FETCH_EMAILS", {
            "query": candidate, "max_results": maximum, "include_payload": False,
        })
    return result, attempted


def myapi_call(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect it from the Omarchy AI settings panel first.")
    service = args.get("service")
    path = args.get("path")
    method = str(args.get("method") or "GET").strip().upper()
    if not isinstance(service, str) or not service.strip():
        return ActionResult(False, "no service given")
    if not isinstance(path, str) or not path.strip():
        return ActionResult(False, "no path given")
    if method != "GET":
        return ActionResult(
            False,
            f"'{method}' isn't voice-reachable through MyApi yet -- only "
            "reading (GET) is, since there's no confirm/policy layer for "
            "anything that would send, create, or delete something on the "
            "user's behalf. Tell the user you can't do that yet, don't "
            "attempt it a different way.",
        )
    query = args.get("query")
    if query is not None and not isinstance(query, dict):
        return ActionResult(False, "query must be an object")

    started = time.monotonic()
    ok = False
    try:
        result = myapi.MyApiClient().call_service(service.strip(), path.strip(), method, query=query)
        # The generic Gmail REST search returns only IDs. Retry a zero-result
        # search through the read-only provider method, whose query planner
        # preserves hard filters while relaxing human text and whose payloads
        # give Jev enough evidence to select the relevant candidate.
        gmail_q = query.get("q") if isinstance(query, dict) else None
        if (service.strip().casefold() == "gmail"
                and path.strip().rstrip("/") == "/gmail/v1/users/me/messages"
                and isinstance(gmail_q, str) and gmail_q.strip()
                and not _gmail_messages(result)):
            fallback, attempted = _gmail_search_with_fallback(gmail_q.strip())
            if _gmail_messages(fallback):
                result = {"gmail_search": {"attempted": attempted, "matched_by": attempted[-1]},
                          "result": fallback}
        ok = True
        return ActionResult(True, json.dumps(result))
    except myapi.MyApiError as e:
        return ActionResult(False, f"MyApi call failed: {e}")
    finally:
        myapi_usage.record(
            service=service.strip(), path=path.strip(), method=method,
            ok=ok, duration_ms=(time.monotonic() - started) * 1000,
        )


def myapi_write(args: dict) -> ActionResult:
    """Run one already-confirmed external mutation through MyApi.

    Conversation-scoped confirmation is enforced by InputGuard before this
    function is reached. Keeping reads and writes as different tool names also
    lets the switchboard treat speculative reads optimistically without ever
    starting a mutation before its verdict.
    """
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect it from the Omarchy AI settings panel first.")
    service = args.get("service")
    path = args.get("path")
    method = str(args.get("method") or "").strip().upper()
    if not isinstance(service, str) or not service.strip():
        return ActionResult(False, "no service given")
    if not isinstance(path, str) or not path.strip():
        return ActionResult(False, "no path given")
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return ActionResult(False, "method must be POST, PUT, PATCH, or DELETE")
    query, body = args.get("query"), args.get("body")
    if query is not None and not isinstance(query, dict):
        return ActionResult(False, "query must be an object")
    if body is not None and not isinstance(body, dict):
        return ActionResult(False, "body must be an object")
    service, path = service.strip(), path.strip()

    def dispatch(key: str) -> ActionResult:
        started = time.monotonic()
        ok = False
        try:
            result = myapi.MyApiClient().call_service(
                service, path, method, query=query, body=body, idempotency_key=key,
            )
            ok = True
            return ActionResult(True, json.dumps(result))
        except myapi.MyApiError as exc:
            failed = ActionResult(False, f"MyApi action failed: {exc}")
            if exc.maybe_sent:
                raise effects.Uncertain(failed) from exc
            return failed
        finally:
            myapi_usage.record(service=service, path=path, method=method,
                               ok=ok, duration_ms=(time.monotonic() - started) * 1000)

    return effects.perform("myapi_write", "myapi_write",
                           {"service": service, "path": path, "method": method, "query": query, "body": body},
                           dispatch, target=f"{service}:{method} {path}")


def _myapi_execute(service: str, operation: str, arguments: dict, idem_key: str | None = None) -> ActionResult:
    """One /execute call. With idem_key (a journaled write, core/effects.py)
    it carries the Idempotency-Key header, and a call that may have reached
    MyApi without an answer raises effects.Uncertain instead of returning."""
    started = time.monotonic()
    ok = False
    try:
        extra = {"idempotency_key": idem_key} if idem_key else {}
        result = myapi.MyApiClient().request(
            "POST", f"/services/{service}/execute",
            body={"method": operation, "params": {"arguments": arguments}}, **extra,
        )
        ok = True
        return ActionResult(True, json.dumps(result))
    except myapi.MyApiError as exc:
        failed = ActionResult(False, f"MyApi action failed: {exc}")
        if idem_key and exc.maybe_sent:
            raise effects.Uncertain(failed) from exc
        return failed
    finally:
        myapi_usage.record(service=service, path=f"/execute:{operation}", method="POST(write)", ok=ok,
                           duration_ms=(time.monotonic() - started) * 1000)


def myapi_write_execute(service: str, operation: str, arguments: dict) -> ActionResult:
    """A state-changing /execute call, journaled and sent at most once per
    origin (core/effects.py). Gmail's GMAIL_SEND_EMAIL / GMAIL_REPLY_TO_THREAD
    take only recipients, subject and body (no custom headers), so the key
    cannot be put into the email itself; it travels as the Idempotency-Key
    header to MyApi."""
    return effects.perform("myapi_execute", operation, {"service": service, "arguments": arguments},
                           lambda key: _myapi_execute(service, operation, arguments, key),
                           target=f"{service}:{operation}")


def myapi_execute(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect it from the Omarchy AI settings panel first.")
    service, operation, arguments = args.get("service"), args.get("operation"), args.get("arguments")
    if not isinstance(service, str) or not service.strip():
        return ActionResult(False, "no service given")
    if not isinstance(operation, str) or not operation.strip():
        return ActionResult(False, "no operation given")
    if not isinstance(arguments, dict):
        return ActionResult(False, "arguments must be an object")
    return myapi_write_execute(service.strip(), operation.strip(), arguments)


def myapi_gmail_send(args: dict) -> ActionResult:
    required = ("recipient_email", "subject", "body")
    if any(not isinstance(args.get(key), str) or not args[key].strip() for key in required):
        return ActionResult(False, "recipient_email, subject, and body are required")
    arguments = {key: args[key] for key in (*required, "cc", "bcc") if args.get(key) is not None}
    arguments["user_id"] = "me"
    return myapi_write_execute("gmail", "GMAIL_SEND_EMAIL", arguments)


def myapi_gmail_reply(args: dict) -> ActionResult:
    required = ("thread_id", "recipient_email", "message_body")
    if any(not isinstance(args.get(key), str) or not args[key].strip() for key in required):
        return ActionResult(False, "thread_id, recipient_email, and message_body are required")
    arguments = {key: args[key] for key in (*required, "cc", "bcc") if args.get(key) is not None}
    arguments["user_id"] = "me"
    return myapi_write_execute("gmail", "GMAIL_REPLY_TO_THREAD", arguments)


def _gmail_execute(method: str, arguments: dict) -> dict:
    """Call the small, explicitly read-only Gmail execute allowlist.

    MyApi exposes these provider reads as POST /execute even though they
    don't alter a mailbox.  Keeping the method names here (rather than a
    generic POST tool) preserves the voice action boundary.
    """
    # This is MyApi's documented provider-tool endpoint, distinct from the
    # REST proxy used by myapi_call. Sending it through /proxy makes the
    # upstream receive an HTML page rather than a Gmail operation.
    return myapi.MyApiClient().request(
        "POST", "/services/gmail/execute",
        body={"method": method, "params": {"arguments": arguments}},
    )


def _walk_gmail_attachments(value: object, message_id: str | None = None) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        current_id = value.get("message_id") or value.get("messageId") or value.get("id") or message_id
        attachment_id = value.get("attachment_id") or value.get("attachmentId")
        filename = value.get("filename") or value.get("file_name") or value.get("fileName")
        if attachment_id:
            found.append({
                "message_id": current_id,
                "attachment_id": attachment_id,
                "filename": filename or "attachment",
                "mime_type": value.get("mimeType") or value.get("mime_type"),
                "size": value.get("size") or value.get("sizeEstimate"),
            })
        for child in value.values():
            found.extend(_walk_gmail_attachments(child, current_id))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_gmail_attachments(child, message_id))
    return found


def myapi_gmail_search_attachments(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect Gmail from the Omarchy AI settings panel first.")
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return ActionResult(False, "a Gmail search query is required")
    maximum = args.get("max_results", 10)
    if not isinstance(maximum, int) or not 1 <= maximum <= 50:
        return ActionResult(False, "max_results must be between 1 and 50")
    started = time.monotonic()
    ok = False
    try:
        result = _gmail_execute("GMAIL_FETCH_EMAILS", {
            "query": query.strip(), "max_results": maximum, "include_payload": True,
        })
        attachments = _walk_gmail_attachments(result)
        # Payloads can repeat the same MIME part at several nesting levels.
        unique = {(x["message_id"], x["attachment_id"]): x for x in attachments}
        ok = True
        return ActionResult(True, json.dumps({"attachments": list(unique.values())[:100]}))
    except myapi.MyApiError as exc:
        return ActionResult(False, f"Gmail search failed: {exc}")
    finally:
        myapi_usage.record(service="gmail", path="/execute:GMAIL_FETCH_EMAILS", method="POST(read)", ok=ok,
                           duration_ms=(time.monotonic() - started) * 1000)


def myapi_gmail_search(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect Gmail from the Omarchy AI settings panel first.")
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return ActionResult(False, "a Gmail search query is required")
    maximum = args.get("max_results", 10)
    if not isinstance(maximum, int) or not 1 <= maximum <= 20:
        return ActionResult(False, "max_results must be between 1 and 20")
    started = time.monotonic()
    ok = False
    try:
        result, attempted = _gmail_search_with_fallback(query.strip(), maximum)
        ok = True
        messages = _gmail_digest(_gmail_messages(result))
        return ActionResult(True, json.dumps({
            "gmail_search": {"attempted": attempted, "matched_by": attempted[-1] if messages else None},
            "messages": messages,
        }, ensure_ascii=False))
    except myapi.MyApiError as exc:
        return ActionResult(False, f"Gmail search failed: {exc}")
    finally:
        myapi_usage.record(service="gmail", path="/execute:GMAIL_FETCH_EMAILS", method="POST(read)", ok=ok,
                           duration_ms=(time.monotonic() - started) * 1000)


def _find_base64_payload(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("data", "content", "content_base64", "base64", "file_data"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for child in value.values():
            found = _find_base64_payload(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_base64_payload(child)
            if found:
                return found
    return None


def _find_download_url(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("s3url", "download_url", "downloadUrl", "url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and urllib.parse.urlparse(candidate).scheme == "https":
                return candidate
        for child in value.values():
            found = _find_download_url(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_download_url(child)
            if found:
                return found
    return None


def _download_url(url: str) -> bytes:
    """Download MyApi's short-lived HTTPS attachment URL with a hard cap."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > 30 * 1024 * 1024:
                raise local_files.FileAccessError("that attachment is over the 30 MB download limit")
            data = response.read(30 * 1024 * 1024 + 1)
    except (OSError, ValueError) as exc:
        raise local_files.FileAccessError(f"couldn't download the Gmail attachment: {exc}") from exc
    if len(data) > 30 * 1024 * 1024:
        raise local_files.FileAccessError("that attachment is over the 30 MB download limit")
    return data


def _safe_download_name(value: object) -> str:
    name = Path(str(value or "attachment")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(". ")
    return name[:180] or "attachment"


def myapi_gmail_download_attachment(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect Gmail from the Omarchy AI settings panel first.")
    message_id, attachment_id, filename = args.get("message_id"), args.get("attachment_id"), args.get("filename")
    if not all(isinstance(value, str) and value.strip() for value in (message_id, attachment_id, filename)):
        return ActionResult(False, "message_id, attachment_id, and filename are required")
    started = time.monotonic()
    ok = False
    try:
        result = _gmail_execute("GMAIL_GET_ATTACHMENT", {
            "message_id": message_id.strip(), "attachment_id": attachment_id.strip(), "file_name": filename.strip(),
        })
        encoded = _find_base64_payload(result)
        if encoded:
            try:
                raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            except (ValueError, TypeError) as exc:
                return ActionResult(False, f"Gmail returned invalid attachment data: {exc}")
        else:
            url = _find_download_url(result)
            if not url:
                return ActionResult(False, "Gmail returned no downloadable attachment data")
            raw = _download_url(url)
        destination = Path.home() / "Downloads" / "Omarchy_AI" / _safe_download_name(filename)
        # Preserve the earlier download rather than silently replacing it.
        if destination.exists():
            stem, suffix = destination.stem, destination.suffix
            for index in range(2, 1000):
                candidate = destination.with_name(f"{stem} ({index}){suffix}")
                if not candidate.exists():
                    destination = candidate
                    break
        saved = local_files.write_bytes(str(destination), raw, load_config())
        ok = True
        return ActionResult(True, f"downloaded {len(raw)} bytes to {saved}")
    except myapi.MyApiError as exc:
        return ActionResult(False, f"Gmail download failed: {exc}")
    except local_files.FileAccessError as exc:
        return ActionResult(False, str(exc))
    finally:
        myapi_usage.record(service="gmail", path="/execute:GMAIL_GET_ATTACHMENT", method="POST(read)", ok=ok,
                           duration_ms=(time.monotonic() - started) * 1000)

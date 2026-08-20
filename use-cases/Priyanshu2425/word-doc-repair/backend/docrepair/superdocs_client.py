"""The four-call contract: upload, edit instruction, approve, export.

SHARED, AND VENDORED. This file is a byte-for-byte copy of
`quota-aware-agent/quota_aware_agent/client.py` below this docstring, so each
build stands alone in the builds repository.

Vendoring has a cost and this project paid it: the multipart fix for BUG-015
landed in the original and not here, so Build B's styling path still sent an
empty body and got a 422 long after Build A was working. `test_the_vendored
_client_has_not_drifted` now fails the build if the two copies diverge.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

BASE = "https://api.superdocs.app"

# Terminal and non-terminal job states, from the docs' job lifecycle.
_TERMINAL = {"completed", "failed", "cancelled"}
_NEEDS_HUMAN = "awaiting_approval"


class Transport(Protocol):
    def request(self, method: str, path: str, **kw: Any) -> "Response": ...


@dataclass
class Response:
    status: int
    body: dict
    headers: dict = field(default_factory=dict)

    @property
    def usage(self) -> dict:
        """The usage block rides on every chat response. It is the only way to
        read the balance from an API-key context -- the account usage endpoints
        reject `sk_` keys with a 401."""
        return self.body.get("usage", {}) or {}


class SuperDocsError(RuntimeError):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"SuperDocs returned {status}: {body}")
        self.status = status
        self.body = body


class QuotaExhausted(SuperDocsError):
    """Raised only when the platform says so. Never inferred from our own count."""


class TransportFailure(RuntimeError):
    """The call did not produce a response, and we have to say which kind.

    The distinction is the whole point. A connection that was refused means the
    request never reached SuperDocs and cannot have been billed, so a rerun may
    safely repeat it. A read that timed out means the request very possibly did
    reach SuperDocs, was charged, and applied an edit -- we simply never heard
    the answer. Collapsing the two into "it failed" is how a retry pays twice.
    """

    def __init__(self, message: str, *, never_sent: bool) -> None:
        super().__init__(message)
        self.never_sent = never_sent


def provably_never_sent(exc: BaseException) -> bool:
    """True only when the request cannot have been billed.

    Deliberately conservative: the default answer is "we do not know", because
    the cost of wrongly believing a call was billed is one step reported to a
    person, and the cost of wrongly believing it was not is the user paying
    twice and possibly getting the same edit applied twice.
    """
    import socket

    if isinstance(exc, TransportFailure):
        return exc.never_sent
    if isinstance(exc, QuotaExhausted):
        # The request that carried this signal completed; it was answered.
        return False
    if isinstance(exc, SuperDocsError):
        # A 4xx was rejected before any work happened, so it was not billed.
        # A 5xx may have come from a gateway that had already passed the
        # request on, so it proves nothing.
        return exc.status < 500
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror))


def _encode_multipart(files: dict, fields: dict) -> tuple[bytes, str]:
    """Build a multipart/form-data body from {name: (filename, bytes)} plus
    plain fields. Returns (body, content_type).

    A fixed boundary would collide with content that happens to contain it, so
    it is derived from the payload -- deterministic for a given body, which
    keeps requests reproducible, and vanishingly unlikely to appear inside it.
    """
    import hashlib

    digest = hashlib.sha256()
    for name, (filename, content) in sorted(files.items()):
        digest.update(name.encode())
        digest.update(str(filename).encode())
        digest.update(content if isinstance(content, bytes) else str(content).encode())
    boundary = "----formdata" + digest.hexdigest()[:24]

    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += str(value).encode("utf-8") + b"\r\n"
    for name, (filename, content) in files.items():
        if isinstance(content, str):
            content = content.encode("utf-8")
        out += f"--{boundary}\r\n".encode()
        out += (f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n').encode()
        out += f"Content-Type: {_guess_type(filename)}\r\n\r\n".encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _guess_type(filename: str) -> str:
    import mimetypes

    return mimetypes.guess_type(str(filename))[0] or "application/octet-stream"


class HttpTransport:
    """Real transport. Imported lazily so the package needs no HTTP library
    installed to run its tests."""

    def __init__(self, api_key: str, base: str = BASE, timeout: float = 300.0) -> None:
        self._key = api_key
        self._base = base
        # ~300s is the platform gateway timeout for synchronous requests.
        self._timeout = timeout

    def request(self, method: str, path: str, **kw: Any) -> Response:
        import urllib.error
        import urllib.request

        url = self._base + path
        headers = {"Authorization": f"Bearer {self._key}"}
        data = None
        if "files" in kw:
            # Upload is multipart/form-data, not JSON. Encoded here rather than
            # with a library because this package has no dependencies -- and
            # because getting it wrong is invisible: the request still sends,
            # and the API answers 422 for a field it never received.
            data, content_type = _encode_multipart(kw["files"], kw.get("data", {}))
            headers["Content-Type"] = content_type
        elif "json" in kw:
            data = json.dumps(kw["json"]).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                raw = r.read()
                body = json.loads(raw) if raw and r.headers.get_content_type() == "application/json" else {"raw": raw}
                return Response(r.status, body, dict(r.headers))
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                body = json.loads(raw)
            except Exception:
                body = {"raw": raw.decode(errors="replace")}
            return Response(e.code, body, dict(e.headers or {}))
        except urllib.error.URLError as e:
            # Name the cause and the fix, and -- more importantly -- say whether
            # the request could have been billed, so the ledger can record the
            # truth rather than the convenient answer.
            never_sent = provably_never_sent(e)
            raise TransportFailure(
                f"could not reach {self._base} ({e.reason}). "
                + ("The connection was refused or the host did not resolve, so "
                   "the request never reached SuperDocs and was not billed — "
                   "check network access to api.superdocs.app and retry."
                   if never_sent else
                   "It is not known whether the request arrived, so it must not "
                   "be assumed unbilled — rerun and read what the operation "
                   "ledger reports about it."),
                never_sent=never_sent,
            ) from e
        except TimeoutError as e:
            # A read timeout is the ambiguous case by definition: the request
            # went out and the answer never came back.
            raise TransportFailure(
                f"no response from {self._base} within {self._timeout:.0f}s. "
                "SuperDocs may still be processing this — large documents and "
                "the deepest model settings take minutes — so the request may "
                "well have been accepted and billed. It is recorded as started "
                "and unconfirmed rather than retried.",
                never_sent=False,
            ) from e


#: What the upload endpoint parses each extension as. Verified against the live
#: API 2026-08-20: the **filename decides the parser**, not the bytes. HTML sent
#: as `report.docx` is answered `400 Invalid DOCX file: File is not a zip file`,
#: and the same bytes as `report.html` are accepted. `.txt` is accepted too and
#: parses the markup as literal text, which is worse than an error because it
#: succeeds.
_ZIP_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".odt"}
_PDF_EXTENSIONS = {".pdf"}
_TEXT_EXTENSIONS = {".html", ".htm", ".txt", ".md", ".markdown", ".rtf"}


def check_upload_name(filename: str, content: bytes) -> None:
    """Refuse a filename whose extension disagrees with the bytes.

    This is a deliberate hardcoded defence sitting in front of the intelligent
    path, not a guess about what the caller meant. The live API decides how to
    parse an upload from the extension alone, so `agent.run(..., "contract.docx",
    html_bytes)` reads perfectly and fails at the platform with a message about
    zip files, which names neither the cause nor the fix. Worse, the mismatch
    that does NOT error — HTML uploaded as `.txt` — succeeds and quietly parses
    the markup as literal text, and nobody finds out until the export.

    So it is checked here, before anything is sent, and the error says which
    two things disagreed and both ways to make them agree.
    """
    import os

    ext = os.path.splitext(str(filename))[1].lower()
    if not ext:
        raise ValueError(
            f"'{filename}' has no file extension. SuperDocs chooses how to parse "
            "an upload from the extension, so give one — '.html' for HTML, "
            "'.docx' for a Word file, '.pdf' for a PDF.")

    looks_like_zip = content[:4] == b"PK\x03\x04"
    looks_like_pdf = content[:4] == b"%PDF"

    if ext in _ZIP_EXTENSIONS and not looks_like_zip:
        raise ValueError(
            f"'{filename}' is named as a Word-family file but the bytes are not "
            "a zip archive, and SuperDocs parses uploads by extension — it would "
            "answer '400 Invalid DOCX file: File is not a zip file'. Either send "
            "the real .docx bytes, or rename this to '.html' if it is HTML.")
    if ext in _PDF_EXTENSIONS and not looks_like_pdf:
        raise ValueError(
            f"'{filename}' is named as a PDF but the bytes do not begin with "
            "'%PDF'. Send the real PDF bytes, or rename it to match what it is.")
    if ext in _TEXT_EXTENSIONS and (looks_like_zip or looks_like_pdf):
        raise ValueError(
            f"'{filename}' is named as text but the bytes are a "
            f"{'zip archive (a .docx, most likely)' if looks_like_zip else 'PDF'}. "
            "This would be accepted and parsed as literal text rather than as a "
            "document — rename it to match its contents.")


def pending_changes(job_body: dict) -> list[dict]:
    """Read the proposed changes off a job, whatever shape they arrive in.

    Two shapes exist and both are real:
      * `GET /v1/jobs/{id}` returns `metadata.pending_changes` as a plain LIST
        of change dicts. Verified against the live API 2026-08-19.
      * The SSE `proposed_change_batch` event delivers an envelope whose
        `content` is a JSON-encoded STRING needing a second parse.

    This lives in the client because both builds need it, and the copy that had
    it written separately handled only the envelope -- so it crashed on the
    shape the polling path actually returns.
    """
    meta = job_body.get("metadata") or {}
    pending = meta.get("pending_changes")
    if pending is None:
        for event in meta.get("intermediate_responses", []) or []:
            if event.get("type") == "proposed_change_batch":
                return parse_proposed_changes(event)
        return []
    if isinstance(pending, list):
        return list(pending)
    if isinstance(pending, str):
        return parse_proposed_changes({"content": pending})
    return parse_proposed_changes(pending)


def parse_proposed_changes(envelope: dict) -> list[dict]:
    """Trap 1. The batch arrives as a JSON string inside `content`.

    A single-change turn still arrives as a one-element `changes[]`, so this
    always returns a list and never special-cases the singular form.
    """
    content = envelope.get("content")
    if content is None:
        return list(envelope.get("changes", []))
    batch = json.loads(content) if isinstance(content, str) else content
    return list(batch.get("changes", []))


class SuperDocsClient:
    def __init__(self, transport: Transport, sleep: Callable[[float], None] = time.sleep) -> None:
        self._t = transport
        self._sleep = sleep
        #: Set once the platform has said the allowance is exhausted, including
        #: when it said so on a free call that was allowed to complete anyway.
        self.quota_exhausted = False

    def _check(self, r: Response, *, billable: bool = True) -> Response:
        """Raise on an error, and on the platform's own exhaustion signal.

        `billable=False` marks the free calls -- whoami and export. An exhausted
        allowance must not stop those: exports and downloads never cost
        operations, and the reserve exists precisely to promise that the work
        already done can still be exported. Raising here would break that
        promise at the exact moment it matters, turning "you always end up with
        a file" into "you end up with a session and an exception". The signal is
        still recorded, so the caller stops spending; it just does not block a
        call that costs nothing.
        """
        if r.status >= 400:
            raise SuperDocsError(r.status, r.body)
        if r.usage.get("quota_exhausted"):
            # The current request still completed; further billable ones will not.
            self.quota_exhausted = True
            if billable:
                raise QuotaExhausted(r.status, r.body)
        return r

    # --- call 0: the one authoritative balance read available to an agent key.
    def whoami(self) -> Response:
        # Free, and it is the call that tells you the allowance is gone. Being
        # refused by the exhaustion it exists to report would be absurd.
        return self._check(self._t.request("GET", "/v1/agents/whoami"),
                           billable=False)

    # --- call 1 of the contract: upload.
    def upload(self, session_id: str, filename: str, content: bytes) -> Response:
        check_upload_name(filename, content)
        return self._check(
            self._t.request(
                "POST", "/v1/documents/upload",
                files={"file": (filename, content)}, data={"session_id": session_id},
            )
        )

    # --- call 2: the edit instruction.
    def edit(self, session_id: str, message: str, approval_mode: str = "ask_every_time") -> Response:
        return self._check(
            self._t.request(
                "POST", "/v1/chat/async",
                json={"session_id": session_id, "message": message, "approval_mode": approval_mode},
            )
        )

    def job(self, job_id: str) -> Response:
        return self._check(self._t.request("GET", f"/v1/jobs/{job_id}"))

    def poll_job(self, job_id: str, deadline_s: float = 600.0, interval_s: float = 2.0,
                 on_wait: Callable[[float, str], None] | None = None) -> Response:
        """Trap 2. Silence is still processing.

        Returns as soon as the job is terminal *or* is waiting on a human. Gives
        up only at an explicit deadline, and says how long it waited -- a slow
        job is never reported as a crash.
        """
        waited = 0.0
        while True:
            r = self.job(job_id)
            status = r.body.get("status", "")
            if status in _TERMINAL or status == _NEEDS_HUMAN:
                return r
            if waited >= deadline_s:
                raise TimeoutError(
                    f"job {job_id} was still '{status}' after {waited:.0f}s. "
                    "That is a deadline this client imposed, not a platform failure -- "
                    "the job may still be running."
                )
            if on_wait:
                on_wait(waited, status)
            self._sleep(interval_s)
            waited += interval_s

    # --- call 3: approve, item by item.
    def approve(self, session_id: str, job_id: str, decisions: list[dict]) -> Response:
        """`decisions` is a list of {change_id, approved, feedback?}. Sent as a
        batch so a mixed approve/deny turn is one request, not one per change."""
        return self._check(
            self._t.request(
                "POST", f"/v1/chat/{session_id}/approve",
                json={"job_id": job_id, "approved": True, "changes": decisions},
            )
        )

    # --- call 4: export. Free, per the docs, and so never priced.
    def export(self, session_id: str, fmt: str = "docx") -> Response:
        """Free per the docs, so an exhausted allowance never blocks it."""
        return self._check(
            self._t.request("POST", "/v1/documents/export",
                            json={"session_id": session_id, "format": fmt}),
            billable=False,
        )

    @staticmethod
    def export_warnings(r: Response) -> list:
        """Exports can succeed with non-fatal issues, carried base64-encoded in
        `X-Export-Warnings`. Surfaced rather than swallowed -- a dropped field
        code is exactly the kind of thing a user should be told about."""
        import base64

        header = r.headers.get("X-Export-Warnings")
        if not header:
            return []
        try:
            return json.loads(base64.b64decode(header))
        except Exception:
            return []

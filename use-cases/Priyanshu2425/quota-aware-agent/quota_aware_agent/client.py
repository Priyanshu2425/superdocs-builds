"""The four-call contract: upload, edit instruction, approve, export.

Everything here is grounded in the SuperDocs documentation, and the three traps
the docs name by hand are handled explicitly rather than discovered later:

  Trap 1 -- the double parse. A `proposed_change_batch` envelope carries its
    payload as a JSON-encoded *string* in `content`. `parse_proposed_changes`
    does the second parse. Skipping it is the documented single most common
    reason integrators see empty diff cards with every field undefined.

  Trap 2 -- silence is not a crash. A job on a large document can run for
    minutes with no visible progress. `poll_job` treats a long quiet run as
    still processing and only gives up at an explicit deadline, reporting how
    long it waited rather than calling it a failure.

  Trap 3 -- the allowance. Every response carries a `usage` block; nothing here
    spends without handing that block back to the caller to reconcile.

Transport is injected. `HttpTransport` is the real one; the tests pass a fake,
which is why this file has no network dependency and the suite needs no key.
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
    boundary = "----attest" + digest.hexdigest()[:24]

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

    def _check(self, r: Response) -> Response:
        if r.status >= 400:
            raise SuperDocsError(r.status, r.body)
        if r.usage.get("quota_exhausted"):
            # The current request still completed; further billable ones will not.
            raise QuotaExhausted(r.status, r.body)
        return r

    # --- call 0: the one authoritative balance read available to an agent key.
    def whoami(self) -> Response:
        return self._check(self._t.request("GET", "/v1/agents/whoami"))

    # --- call 1 of the contract: upload.
    def upload(self, session_id: str, filename: str, content: bytes) -> Response:
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
        r = self._check(
            self._t.request("POST", "/v1/documents/export",
                            json={"session_id": session_id, "format": fmt})
        )
        return r

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

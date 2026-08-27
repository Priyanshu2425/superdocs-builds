"""A fake SuperDocs. Every response shape here is taken from the documented
payloads, including the double-parsed proposed-change envelope.

Two shapes were corrected on 2026-08-27 after a live run disagreed with this
file, and both corrections cost the build a bug:

  * `POST /v1/chat/async` returns **no usage block at all**, and the usage for
    that edit arrives inside `result` on the job that completes it. The fake
    used to put usage on the async call, which is the shape the code happened
    to read -- so every offline receipt reconciled and every live one said it
    could not. See BUG-102.
  * A `completed` job is not a job that changed something. It carries
    `result.document_changes.version_id`, and when the model declines the work
    that version is the one you uploaded. The fake used to have no versions at
    all, so "completed and identical" was a state no test could express. See
    BUG-101.

A fake that is kinder than the API is how both of those shipped.
"""

from __future__ import annotations

import json

from quota_aware_agent.client import Response


class FakeSuperDocs:
    def __init__(self, remaining: int = 500, monthly_limit: int = 500,
                 sections_per_edit: int = 1, slow_polls: int = 0,
                 exhaust_after: int | None = None, settle_polls: int = 1,
                 envelope_pending: bool = False,
                 usage_on_async: bool = False,
                 no_effect_steps: set[str] | None = None,
                 fail_steps: set[str] | None = None,
                 fail_steps_unknown: set[str] | None = None) -> None:
        self.remaining = remaining
        self.monthly_limit = monthly_limit
        self.sections_per_edit = sections_per_edit
        self.slow_polls = slow_polls
        self.exhaust_after = exhaust_after
        self.settle_polls = settle_polls
        self.envelope_pending = envelope_pending
        #: The synchronous `POST /v1/chat` does put usage on the response. Off
        #: by default because this fake drives the async path, which does not.
        self.usage_on_async = usage_on_async
        #: Instructions (matched as substrings) the model will decline: the job
        #: completes, proposes nothing, and leaves the version where it was.
        self.no_effect_steps = set(no_effect_steps or ())
        #: Instructions whose job ends `failed` with usage saying it was not
        #: billable -- so a retry is safe.
        self.fail_steps = set(fail_steps or ())
        #: Instructions whose job ends `failed` with no usage block at all, so
        #: whether it was billed is genuinely unknown.
        self.fail_steps_unknown = set(fail_steps_unknown or ())
        self._approved_at_poll: int | None = None
        self.calls: list[tuple[str, str]] = []
        self._polls = 0
        self._edits = 0
        self._mode = "normal"
        self._pending_usage: dict | None = None
        self.approved: list[dict] = []
        self.uploaded: list[str] = []
        #: The document's version. Moves only when a change is actually applied,
        #: which is the only reliable way to tell work from a polite refusal.
        self._version = 0

    # -- helpers ----------------------------------------------------------
    def _usage(self, ops: int) -> dict:
        self.remaining = max(0, self.remaining - ops)
        exhausted = (
            self.exhaust_after is not None and self._edits >= self.exhaust_after
        ) or self.remaining == 0
        return {
            "monthly_used": self.monthly_limit - self.remaining,
            "monthly_limit": self.monthly_limit,
            "monthly_remaining": self.remaining,
            "was_billable": ops > 0,
            "ops_charged": ops,
            "quota_exhausted": exhausted,
            "subscription_tier": "free",
        }

    def _deliver_usage(self) -> dict | None:
        """The edit's usage, handed over once by the job that completes it."""
        usage, self._pending_usage = self._pending_usage, None
        return usage

    def _version_id(self) -> str:
        return f"v{self._version}"

    def _result(self, response: str, *, changed: bool) -> dict:
        result = {
            "response": response,
            "document_changes": {
                "updated_html": "<p>after</p>" if changed else "<p>before</p>",
                "version_id": self._version_id(),
                "changes_summary": ("Document updated by AI" if changed
                                    else "No changes were made"),
                "pending_changes": None,
            },
        }
        usage = self._deliver_usage()
        if usage is not None:
            result["usage"] = usage
        return result

    def _mode_for(self, message: str) -> str:
        for tokens, mode in ((self.fail_steps, "failed"),
                             (self.fail_steps_unknown, "failed_unknown"),
                             (self.no_effect_steps, "no_effect")):
            if any(t in message for t in tokens):
                return mode
        return "normal"

    # -- transport --------------------------------------------------------
    def request(self, method: str, path: str, **kw) -> Response:
        self.calls.append((method, path))

        if path == "/v1/agents/whoami":
            return Response(200, {
                "account_id": "fake",
                "quota": {"tier": "free", "monthly_limit": self.monthly_limit,
                          "used": self.monthly_limit - self.remaining,
                          "remaining": self.remaining,
                          "resets_at": "2026-09-01T00:00:00+00:00"},
            })

        if path == "/v1/documents/upload":
            # The real endpoint answers 422 when the multipart `file` part is
            # absent. Modelled here because a fake that accepts anything is how
            # a transport that encoded nothing passed its tests.
            files = kw.get("files") or {}
            if "file" not in files:
                return Response(422, {"detail": [
                    {"type": "missing", "loc": ["body", "file"], "msg": "Field required"}
                ]})
            name, body = files["file"]
            # The live API parses by EXTENSION, not by sniffing the bytes: HTML
            # sent as report.docx is answered 400. Modelled here because a fake
            # that accepts any filename is how the demo shipped uploading HTML
            # under a .docx name and nobody found out until a live run.
            # Verified against the live API 2026-08-20.
            if (str(name).lower().endswith((".docx", ".xlsx", ".pptx", ".odt"))
                    and not bytes(body)[:4] == b"PK\x03\x04"):
                return Response(400, {"detail": "Invalid DOCX file: File is not a zip file"})
            self.uploaded.append(name)
            return Response(200, {"status": "ok", "chunks_count": 2,
                                  "version_id": self._version_id()})

        if path == "/v1/chat/async":
            self._edits += 1
            self._polls = 0
            self._approved_at_poll = None
            self._mode = self._mode_for((kw.get("json") or {}).get("message", ""))
            ops = max(1, -(-self.sections_per_edit // 25))
            # Charged now, reported later: the async call answers with a job id
            # and nothing else. Verified live 2026-08-27.
            usage = self._usage(ops)
            self._pending_usage = usage
            body = {"job_id": f"job-{self._edits}", "status": "pending",
                    "session_id": (kw.get("json") or {}).get("session_id", "")}
            if self.usage_on_async:
                body["usage"] = self._deliver_usage()
            return Response(200, body)

        if path.startswith("/v1/jobs/"):
            self._polls += 1
            if self._polls <= self.slow_polls:
                # Trap 2: a long quiet run. No usage block, still processing.
                return Response(200, {"status": "in_progress"})

            if self._mode in ("failed", "failed_unknown"):
                # `failed` is terminal and says nothing about billing on its
                # own. When the platform states `was_billable: false` a retry is
                # safe; when it states nothing, nobody knows. Both are real.
                body = {"status": "failed",
                        "error": "the model could not complete this edit"}
                usage = self._deliver_usage()
                if self._mode == "failed" and usage is not None:
                    body["result"] = {"usage": {**usage, "was_billable": False,
                                                "ops_charged": 0}}
                return Response(200, body)

            if self._mode == "no_effect":
                # Terminal, well-formed, billed -- and the document is exactly
                # as it was uploaded. This is what the live API returned on
                # 2026-08-27 with "🟡 0 of 1 asked could be completed."
                return Response(200, {
                    "status": "completed",
                    "metadata": {"pending_changes": []},
                    "result": self._result(
                        "🟡 0 of 1 asked could be completed.\n"
                        "I couldn't find what you asked me to change.",
                        changed=False),
                })

            if self._approved_at_poll is not None:
                # After approval the job resumes; it needs at least one more
                # poll before it is safe to export.
                if self._polls <= self._approved_at_poll + self.settle_polls:
                    return Response(200, {"status": "in_progress"})
                return Response(200, {
                    "status": "completed",
                    "result": self._result("Applied the approved change.",
                                           changed=True),
                })

            if self.envelope_pending:
                # The SSE `proposed_change_batch` shape: content is a JSON
                # *string* needing a second parse (Trap 1).
                pending = {"content": json.dumps({"changes": [
                    {"change_id": "ch_1", "chunk_id": "c1", "diff": "<p>after</p>"},
                ]})}
            else:
                # What GET /v1/jobs/{id} actually returns: a plain list.
                # Verified against the live API 2026-08-19.
                pending = [{"change_id": "ch_1", "chunk_id": "c1",
                            "old_html": "<p>before</p>", "new_html": "<p>after</p>"}]
            return Response(200, {
                "status": "awaiting_approval",
                "metadata": {"pending_changes": pending},
            })

        if path.endswith("/approve"):
            self.approved.extend(kw["json"]["changes"])
            # The real endpoint returns {"status": "ok"} and the job keeps
            # running; it reaches "completed" only on a later poll. Returning
            # "completed" here hid a race that silently exported the pre-edit
            # document. Verified live 2026-08-19.
            self._approved_at_poll = self._polls
            # The approved change is what moves the document forward.
            self._version += 1
            return Response(200, {"status": "ok", "usage": self._usage(0)})

        if path == "/v1/documents/export":
            import base64
            warn = base64.b64encode(json.dumps(
                [{"code": "field_code_unsupported", "detail": "a citation was skipped"}]
            ).encode()).decode()
            return Response(200, {"raw": b"DOCX"}, {"X-Export-Warnings": warn})

        return Response(404, {"error": "no such path"})
